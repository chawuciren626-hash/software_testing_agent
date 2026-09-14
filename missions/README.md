# Missions

Missions are YAML files that describe what the agentic test framework should do. Each file
contains a `missions:` list; each entry is a single isolated test run.

## Schema

```yaml
missions:
  - thread_id: "<unique_id>"   # required — also routes to a graph (see below)
    prompt: >                  # required — natural-language instructions for the agent
      <multi-line text>
    goal:                      # optional — machine-checkable objectives (see below)
      must_visit: ["/", "/admin/products"]
      require_actions: 3
      forbid_unreachable: true
```

### `thread_id`

* Must be unique per run. It keys the persistent SQLite checkpoint and the artifact
  directory (`report_<thread_id>/`).
* Reusing the same `thread_id` resumes the prior conversation. Pass `--clear-checkpoints`
  to reset checkpoints while keeping learned memory, `--clear-learned` for the inverse,
  or `--clear-all` to wipe everything.
* **Routing keywords**: if `thread_id` contains any of `accessibility`, `a11y`,
  `data_heavy`, `data-heavy`, `impatient`, `returning`, `explorer`, `chaos`, or
  `autonomous`, the mission is dispatched to the **advanced** graph. Otherwise it
  runs on the **standard** 3-persona swarm.

### `prompt`

* Free-form natural language. The supervisor reads it and decides which specialist agent
  should drive the test.
* Keep prompts concise and task-oriented. Prefer the smallest context that identifies the
  page/flow, interactions, and expected verification. Let agents disclose additional MCP,
  Skill, DOM, and PR context only when needed.
* Mention the user persona or risk area you want exercised so the supervisor routes to the
  right agent.
* Use placeholders for app-specific values — for example `<YOUR_APP>`, `<APP_URL>`,
  `<example_search_term>`, `<dashboard_path>` — and replace them before running.

### `goal` (optional) — 让"达成"由**程序**判定

The loop's stop/stop-achieved decisions are **not** taken from the model's self-assessment.
After each mission the harness computes a verdict from deterministic assertions
(`orchestration/guardrails.py`); the `goal` block is how you tell it what to check.

| Field                | Default | Meaning                                                              |
|----------------------|---------|----------------------------------------------------------------------|
| `must_visit`         | `[]`    | Paths/URLs that must actually have been reached (parent matches children). |
| `require_actions`    | `1`     | Minimum number of **successful** recorded actions in the action tape. |
| `forbid_unreachable` | `true`  | Fail if any action hit a **connection-level** error (`net::ERR_*`, refused/timeout). Business errors (4xx/5xx, validation) do **not** count. |

Omit the block and you get the defaults above (`require_actions: 1` +
`forbid_unreachable: true`) — no need to know the app's URL structure in advance.

The verdict is written to `report_<thread_id>/result.json` and appended to the report as
「程序判定（权威）」. It is one of:

* `achieved` — every checkable assertion passed;
* `unachieved` — at least one failed;
* `unknown` — **nothing could be checked** (e.g. you turned every assertion off, or there is
  no visit trail at all). `unknown` is deliberate: we do not guess — it is never reported as
  a pass.

The mission's end reason is enumerated too (`completed` / `blocked` / `max-turns` /
`budget-exhausted` / `error`) — see the hard limits below.

### Hard resource limits

Separate from the **soft** `--max-steps` (which only resets the step counter and redirects
exploration), the supervisor enforces **hard** caps and terminates when they are hit:

| Env var               | Default                     | Meaning                                    |
|-----------------------|-----------------------------|--------------------------------------------|
| `AGENT_MAX_TURNS`     | `max(4 * max_steps, 40)`    | Absolute supervisor turns; never reset.    |
| `AGENT_TOKEN_BUDGET`  | `0` (unlimited)             | Token spend ceiling (agent-call usage only). |
| `AGENT_STEP_TIMEOUT`  | `0` (off)                   | Per-turn wall-clock timeout, in seconds.   |

## Standard agents

| Agent                      | Specialization                                                        |
|----------------------------|-----------------------------------------------------------------------|
| `new_user_agent`           | Tests onboarding flows, discoverability, default states, empty states |
| `power_user_agent`         | Keyboard shortcuts, bulk operations, advanced filters, edge cases     |
| `adversarial_user_agent`   | Deliberately breaks things (malformed inputs, back-button abuse)      |

## Advanced agents

| Agent                      | Specialization                                                        |
|----------------------------|-----------------------------------------------------------------------|
| `impatient_user_agent`     | Rapid interactions, cancels operations, refreshes during load         |
| `accessibility_user_agent` | Validates WCAG, screen reader nav, keyboard-only interaction          |
| `data_heavy_user_agent`    | Uploads large files, thousands of records, excessively long strings   |
| `returning_user_agent`     | Stale sessions, cached pages, outdated bookmarks                      |
| `explorer_agent`           | Autonomous chaos exploration; finds crashes, timeouts, regressions    |

## Writing a new mission

1. Pick a `thread_id` that signals the persona or area under test (e.g. `smoke_new_user_01`).
2. Write a concise prompt that tells the agent **what to do** and **what to verify**, not how.
3. Reference the persona or risk area the supervisor should route to.
4. If the test should run on an advanced agent, include one of the advanced routing keywords
   in the `thread_id`; for autonomous exploration, use `explorer`, `chaos`, or `autonomous`.

Each supported agent has a generic mission template named `<agent>.yaml` in this folder to help you get started.

## Regression missions

Instead of writing missions manually, you can auto-generate them from the bug catalog:

```bash
agent-explorer --regression --headed
```

This queries the cross-session bug catalog for open bugs and historically flaky areas,
generates missions targeting those pages, and runs them. Agents receive the
`recall_past_findings` tool to query past bugs, sessions, and quirks for any page area
before testing it.

## Cross-session memory

Agents learn across sessions. After each mission, the framework records session summaries
and bug fingerprints. After each batch, **Langmem's prompt optimizer** reflects on session
outcomes and generates improved prompts for each agent and routing rules for the supervisor.
On subsequent runs, agents receive optimized prompts with learned context (effective
strategies, things to avoid, known page structures) and the supervisor routes based on
risk-scored page prioritization.

Agents can also proactively record observations using the `record_observation` tool
(powered by Langmem) and recall past findings via semantic search when an embedding
model is configured (see `config.yaml > llm.embedding_model`).
