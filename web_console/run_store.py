"""任务/执行历史持久化（SQLite）。

控制台的任务原本只存内存，进程一重启就清空。这里把每次执行
（run / regression / dashboard）落盘到 SQLite，重启后 任务 页仍能看到历史，
且已完成任务的完整日志也会保留。

设计：
- DB 文件放在 DATA_ROOT（仓库根 / exe 同目录），随数据一起存在，不入库。
- 用一把锁 + check_same_thread=False，兼容 Flask 请求线程与后台执行线程的并发写。
- 列表接口对 log 做截断（只保留摘要），详情接口返回完整日志。
"""
from __future__ import annotations

import json
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

_DB: Optional[sqlite3.Connection] = None
_LOCK = threading.Lock()
_LOG_PREVIEW = 800


def init_db(path: Path) -> None:
    """初始化（或打开）SQLite 库；幂等。"""
    global _DB
    with _LOCK:
        _DB = sqlite3.connect(str(path), check_same_thread=False)
        _DB.execute(
            """CREATE TABLE IF NOT EXISTS runs(
                id          TEXT PRIMARY KEY,
                kind        TEXT,
                pid         TEXT,
                status      TEXT,
                started     TEXT,
                finished    TEXT,
                exit_code   INTEGER,
                created_at  INTEGER,
                command     TEXT,
                log         TEXT,
                started_ts  INTEGER,
                finished_ts INTEGER
            )"""
        )
        # 兼容旧库：缺新列时补列（首次部署后已有数据也能继续用）
        for col in ("started_ts", "finished_ts"):
            try:
                _DB.execute(f"ALTER TABLE runs ADD COLUMN {col} INTEGER")
            except sqlite3.OperationalError:
                pass  # 可忽略：列已存在（旧库已补过），这正是本分支的预期命中
        _DB.execute(
            """CREATE TABLE IF NOT EXISTS snapshots(
                id        INTEGER PRIMARY KEY AUTOINCREMENT,
                pid       TEXT,
                ts        INTEGER,
                trigger   TEXT,
                scene     TEXT,
                total     INTEGER,
                passed    INTEGER,
                failed    INTEGER,
                skipped   INTEGER,
                all_pass  INTEGER,
                results   TEXT
            )"""
        )
        _DB.execute("CREATE INDEX IF NOT EXISTS idx_snaps_pid_ts ON snapshots(pid, ts)")
        _DB.commit()


def insert_run(t: Dict[str, Any]) -> None:
    with _LOCK:
        _DB.execute(
            "INSERT OR REPLACE INTO runs"
            "(id,kind,pid,status,started,finished,exit_code,created_at,command,log,"
            " started_ts,finished_ts) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                t["id"], t.get("kind"), t.get("pid"), t.get("status"),
                t.get("started"), t.get("finished"), t.get("exit_code"),
                t.get("created_at", int(time.time())), t.get("command", ""),
                t.get("log", ""),
                t.get("started_ts"), t.get("finished_ts"),
            ),
        )
        _DB.commit()


def update_run(tid: str, status: str, finished: Optional[str],
               exit_code: Optional[int], log: str,
               finished_ts: Optional[float] = None) -> None:
    with _LOCK:
        _DB.execute(
            "UPDATE runs SET status=?, finished=?, exit_code=?, log=?, finished_ts=? "
            "WHERE id=?",
            (status, finished, exit_code, log, finished_ts, tid),
        )
        _DB.commit()


def _row_to_dict(r: tuple) -> Dict[str, Any]:
    return {
        "id": r[0], "kind": r[1], "pid": r[2], "status": r[3],
        "started": r[4], "finished": r[5], "exit_code": r[6],
        "created_at": r[7], "command": r[8], "log": r[9] or "",
        "started_ts": r[10], "finished_ts": r[11],
    }


def list_runs(limit: int = 50, preview: bool = True) -> List[Dict[str, Any]]:
    with _LOCK:
        cur = _DB.execute(
            "SELECT id,kind,pid,status,started,finished,exit_code,created_at,command,log,"
            "started_ts,finished_ts "
            "FROM runs ORDER BY created_at DESC LIMIT ?", (limit,)
        )
        rows = cur.fetchall()
    out = [_row_to_dict(r) for r in rows]
    if preview:
        for t in out:
            if len(t["log"]) > _LOG_PREVIEW:
                t["log"] = t["log"][: _LOG_PREVIEW] + "\n…（完整日志见详情）"
    return out


def get_run(tid: str) -> Optional[Dict[str, Any]]:
    with _LOCK:
        cur = _DB.execute(
            "SELECT id,kind,pid,status,started,finished,exit_code,created_at,command,log,"
            "started_ts,finished_ts "
            "FROM runs WHERE id=?", (tid,)
        )
        r = cur.fetchone()
    return _row_to_dict(r) if r else None


# ---------------------------------------------------------------------------
# 回归快照：每次 run / regression / rerun 完成后留档一份结果，用于趋势分析
# ---------------------------------------------------------------------------
def insert_snapshot(pid: str, summary: Dict[str, Any],
                    trigger: str = "regression", scene: Optional[str] = None) -> None:
    """把一份回归结果写入快照表。summary 即 regression.json 的内容。"""
    results = summary.get("results") or []
    total = summary.get("total", len(results))
    if not total:
        return  # 空结果不留档（例如执行异常），避免污染趋势
    with _LOCK:
        _DB.execute(
            "INSERT INTO snapshots(pid,ts,trigger,scene,total,passed,failed,skipped,"
            "all_pass,results) VALUES(?,?,?,?,?,?,?,?,?,?)",
            (
                pid, int(time.time()), trigger, scene,
                total, summary.get("passed", 0), summary.get("failed", 0),
                summary.get("skipped", 0), 1 if summary.get("all_pass") else 0,
                json.dumps(results, ensure_ascii=False),
            ),
        )
        _DB.commit()


def count_snapshots(pid: str) -> int:
    """某项目的快照条数。

    用途：控制台侧写入前的**幂等去重**探针（见 `app._should_record_snapshot`）。
    run / regression 的子进程自己会写一条快照，控制台靠"本次任务期间条数有没有增加"
    来判断是否还需要兜底补写，避免同一任务留两份 → 失败计数与趋势点数被翻倍。
    """
    with _LOCK:
        cur = _DB.execute("SELECT COUNT(*) FROM snapshots WHERE pid=?", (pid,))
        return int(cur.fetchone()[0])


def list_snapshots(pid: Optional[str] = None, limit: int = 60,
                   since: Optional[int] = None) -> List[Dict[str, Any]]:
    """按时间正序返回快照（画图需要正序）。

    since: 只返回 ts>=since 的快照。必须在 SQL 里过滤——若先 LIMIT 再在内存里筛，
    拿到的是"最近 N 条里再筛"，历史一多就会漏掉区间内更早的记录。
    """
    where = "WHERE ts>=?" if since is not None else ""
    wpid = (f"{where} AND pid=?" if where else "WHERE pid=?") if pid else where
    args: List[Any] = ([since] if since is not None else []) + ([pid] if pid else []) + [limit]
    sql = ("SELECT id,pid,ts,trigger,scene,total,passed,failed,skipped,all_pass,results "
           f"FROM snapshots {wpid} ORDER BY ts DESC LIMIT ?")
    with _LOCK:
        cur = _DB.execute(sql, args)
        rows = cur.fetchall()
    out: List[Dict[str, Any]] = []
    for r in rows:
        try:
            results = json.loads(r[10] or "[]")
        except Exception:
            results = []
        out.append({
            "id": r[0], "pid": r[1], "ts": r[2], "trigger": r[3], "scene": r[4],
            "total": r[5], "passed": r[6], "failed": r[7], "skipped": r[8],
            "all_pass": bool(r[9]), "results": results,
        })
    out.reverse()   # 正序：左=早，右=晚
    return out


def delete_project(pid: str) -> None:
    """项目被删除时，一并清掉它的任务历史与回归快照，避免留下孤儿数据。"""
    with _LOCK:
        _DB.execute("DELETE FROM snapshots WHERE pid=?", (pid,))
        _DB.execute("DELETE FROM runs WHERE pid=?", (pid,))
        _DB.commit()


def scene_history(snaps: List[Dict[str, Any]], name: str,
                  limit: int = 12) -> List[Dict[str, Any]]:
    """从一批快照里抽出某个场景的历史结果序列（用于场景级趋势条）。"""
    seq: List[Dict[str, Any]] = []
    for s in snaps:
        for r in s.get("results") or []:
            if str(r.get("name", "")) == name:
                seq.append({"ts": s["ts"], "result": r.get("result"),
                            "trigger": s.get("trigger"), "detail": r.get("detail")})
                break
    return seq[-limit:]
