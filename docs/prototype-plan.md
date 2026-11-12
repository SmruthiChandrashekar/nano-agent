# Prototype Implementation Plan: Approve, Pause, Undo Console

**Goal:** a web console that sits on top of nano-agent and lets a user **approve** risky steps, **pause** the agent, and **undo** any step. Every feature comes from our survey (n = 12, 11 agent users, 9–11 Nov 2026).

**Rule for scope:** the marks come from the interface and the evidence. If time runs short, cut agent features, never UI features.

---

## 1. What we already get from nano-agent

| We need | nano-agent already has | File |
|---|---|---|
| A gate before every tool | `emit_approval(PreToolUse)` waits for yes/no | `events.py` |
| Skip a step | A "no" answer sends "Tool call denied by user" to the model, which carries on | `agent.py` |
| A live timeline | Every step is announced as an event, so we only add a new listener | `events.py`, `listeners/` |
| A fake agent for testing | `Provider` is an interface; the tests already use a scripted fake | `providers/base.py`, `tests/test_agent.py` |
| A plain-log comparison condition | Today's nano-agent with `skip_approval: true` | `config.py` |

**What's missing:** a web UI, risk rules, plan preview, pause, undo, summary, and study mode.

---

## 2. Architecture

```
Browser (static/index.html + app.js)
  Top bar: task · mode (Normal / Strict) · Pause · Stop
  Left: timeline            Right: Plan | Approval card | Paused | Summary
        ▲ events (JSON)              │ clicks (JSON)
        │          WebSocket /ws     ▼
server.py (FastAPI)  ← NEW. Replaces the terminal loop in main.py for the console
        │
        ▼
Agent (agent.py)  ← small changes: plan step, pause/stop, "reason" on events
  ├─ Provider: AnthropicProvider (live)  |  ReplayProvider (study)   ← NEW
  ├─ Tools: existing minus spawn_agent, plus delete_file, run inside sandbox/
  └─ EventBus listeners:
       web.py         → sends every event to the browser            ← NEW
       approval (web) → policy.py decides: run / ask / block        ← NEW
       checkpoint.py  → git commit after each change (for undo)     ← NEW
       logging.py     → unchanged (plus study log in study mode)
```

New dependencies: `fastapi`, `uvicorn[standard]` (`uv add fastapi "uvicorn[standard]"`). No frontend build step: plain HTML, CSS and JS.

---

## 3. Survey → feature → where it lives

| Survey finding (n = 12) | Feature | Code |
|---|---|---|
| Plan preview ranked **#1** (22 pts) | Plan screen before anything runs | `agent.py` (plan step), `static/` |
| Undo is the **weakest** area (2.7 / 5); 7/12 want "any single step" | Per-step Undo | `listeners/checkpoint.py` |
| 10/12 need **which files**, 7/12 the **exact changes** | Approval card: file → reason → diff | `policy.py`, `static/` |
| 9/12 would stop reading or turn approvals off if asked too often | Ask only before risky steps by default | `policy.py` |
| 6/12 want approval before every step | **Strict mode** toggle | `policy.py` |
| 10/12 want to **pause and check** | Pause / Resume / add a note | `agent.py` |
| 9/12 prefer a **notification** | Browser notification when it's waiting | `static/app.js` |
| 8/12 want "only files I choose" | Scope picker; outside paths are blocked | `policy.py` |
| Summary: files 12/12, commands 10/12, skipped/failed 9/12 | End summary | `server.py`, `static/` |

### Risk rules (from survey Q13; ≥70% "ask me first" → always asks)

| Action | Ask-first % | Normal mode | Strict mode | Tag |
|---|---|---|---|---|
| read / list / find | (not asked) | runs | runs | Runs on its own |
| edit / write file | 83% | **asks** | asks | Undoable |
| delete file | 100% | **asks** | asks | Undoable (moved to `.trash/`) |
| edit `.env` / config / settings | 92% | **asks** | asks | Sensitive |
| `git push` | 75% | **asks** | asks | **Permanent** |
| install packages | 58% | runs | asks | Your choice |
| other commands | 50% | runs | asks | Your choice |
| `rm`, `del`, `git reset --hard`, `git clean` | (delete-like) | **asks** | asks | Permanent |
| any path outside the chosen scope | (Q19) | **blocked** | blocked | Out of scope |

---

## 4. Files to add or change

| File | New / change | What it does |
|---|---|---|
| `src/nano_agent/server.py` | NEW | FastAPI app: serves `static/`, one WebSocket `/ws`, runs one agent task at a time |
| `src/nano_agent/policy.py` | NEW | `classify(tool, params, mode, scope) → {decision, tag, permanent}`. Pure function, easy to test |
| `src/nano_agent/listeners/web.py` | NEW | Turns every event into a JSON message for the browser |
| `src/nano_agent/listeners/web_approval.py` | NEW | Uses `policy.py`; for "ask", sends an approval card and **awaits** the browser's reply (an `asyncio.Future`). Replaces the blocking `console.input` |
| `src/nano_agent/listeners/checkpoint.py` | NEW | After each changing step: `git add -A && git commit`; keeps step → commit; `undo(step)` runs `git revert` |
| `src/nano_agent/preview.py` | NEW | Builds what the card shows: a diff (`difflib`) for edit/write, plus "used by" for delete (search the sandbox for the file name, e.g. `main.py: import config_old`) |
| `src/nano_agent/tools/delete_file.py` | NEW | Moves the file into `sandbox/.trash/` (undoable) |
| `src/nano_agent/providers/replay.py` | NEW | `RecordingProvider` saves every response to JSONL; `ReplayProvider` plays them back |
| `src/nano_agent/agent.py` | CHANGE | Plan step; pause/stop gate; `reason` on events; notes while paused (details below) |
| `src/nano_agent/events.py` | CHANGE | Add `PlanProposed`, `Paused`, `Resumed`; add `step_id` and `reason` to `PreToolUse`/`PostToolUse` |
| `src/nano_agent/tools/__init__.py` | CHANGE | Add `get_console_tools()`: everything except `spawn_agent`, plus `delete_file` |
| `static/index.html`, `app.js`, `style.css` | NEW | The console UI |
| `sandbox_fixtures/cleanup_task/` | NEW | Demo project: `main.py` (imports `config_old`), `utils.py`, `config_old.json`, `old_notes.txt` |
| `tests/test_policy.py`, `test_checkpoint.py`, `test_pause.py`, `test_replay.py` | NEW | See section 7 |

**Why remove `spawn_agent`:** a child agent approves all its own tools automatically, and its steps never reach the screen. That breaks oversight.

### Changes inside `agent.py` (kept small)

1. **Reason.** The model's text in the same reply as a tool call ("I'll delete this because…") is passed as `PreToolUse.reason`. That's the "Why" line on the card.
2. **Plan step.** Before the normal loop, make one call that offers only a `propose_plan` tool (steps: action, target). Emit `PlanProposed` and wait for approval. Answer the plan's `tool_use` with a `tool_result` ("Plan approved", or "Plan rejected: …"), then continue with the full tool set. If the model replies with plain text instead, show it as the plan.
3. **Pause.** An `asyncio.Event`, checked at the top of every turn and before every tool. Pause takes effect *after the current step* (a running shell command isn't cut off). The UI says "Pausing after this step…".
4. **Note while paused.** It's added as a text block next to the pending `tool_result`s, so the model reads it on its next call.
5. **Stop.** Ends the run cleanly. Any tool calls not yet answered get the result "Stopped by user", so the history stays valid.
6. **After an undo**, a note is added: "User undid step 3 (edit utils.py)". Otherwise the model thinks the change is still there.

### WebSocket messages

Browser → server:
`start {task, scope, mode}` · `plan_decision {approve | reject, note}` · `decision {step_id, approve | skip}` · `pause` · `resume {note?}` · `stop` · `undo {step_id}` · `set_mode {normal | strict}`

Server → browser:
`plan {steps}` · `step {step_id, tool, params, tag, status: running | waiting | done | skipped | failed}` · `approval {step_id, file, reason, diff, used_by, permanent}` · `result {step_id, text}` · `paused` · `undone {step_id}` · `summary {files_changed, commands_run, skipped_failed, undoable}` · `error {message}`

---

## 5. Build phases

Each phase ends with something you can click.

| Phase | Build | Done when | Owner |
|---|---|---|---|
| **0. Foundation** | `server.py`, WebSocket, `web.py`, timeline in the browser, sandbox folder copied from the fixture, `spawn_agent` removed | You type a task in the browser and watch steps appear live | C + B |
| **1. Approval card** | `policy.py`, `web_approval.py`, `preview.py`, `delete_file`, card with file / why / diff / used-by, Approve and Skip buttons, notification | "Read" steps run on their own; deleting `config_old.json` stops at a card showing `main.py: import config_old` | C + B |
| **2. Plan preview** | `propose_plan` step, plan screen with risk tags, Approve / Reject | Nothing touches files until the plan is approved | C + B |
| **3. Undo + summary** | `checkpoint.py`, Undo button on every finished step, the agent is told about undos, end summary | Undo of step 2 restores the file, and the summary lists it as undone | C + B |
| **4. Pause / Stop** | Pause gate, note box, Stop | Pause → add "keep config_old.json" → Resume, and the agent follows the note | C + B |
| **5. Study mode** | `RecordingProvider`, `ReplayProvider`, 3 recorded runs with one planted mistake each, plain-log page, sandbox reset per run, study log | A participant can do both conditions 3 times, with identical agent behaviour each time | D |
| **6. Polish** | Strict mode toggle, scope picker, keyboard shortcuts (A = approve, S = skip, P = pause) | Usable without help in a hallway test | B |

### Order and dates
- **Before the presentation (by 12 Nov):** Phases 0–1. Put a screenshot of the real approval card on slide 7.
- **After the presentation:** Phases 2–4, then 5, then 6. Run the heuristic review with 3 evaluators, then the user study, using real data only.

### Team
| Person | Phases |
|---|---|
| C (backend) | Server, policy, agent changes, checkpoints |
| B (frontend) | All screens, notifications, polish |
| D (evaluation) | Study mode, planted mistakes, study logs, analysis |
| A (research) | Fixture project, demo script, hallway tests, contacting the 6 volunteers |

Each phase goes on its own git branch and is merged into `main` once its "done when" check passes.

---

## 6. Study mode (for the evaluation)

- **Record:** run the live agent on the fixture with `RecordingProvider`, which saves `runs/<task>.jsonl`.
- **Plant the mistake:** edit one recorded response, e.g. change the delete target to `config_old.json`. Use the top real mistakes from the survey: **deleted something** (5/11), **ran an unwanted command** (5/11), **couldn't tell what changed** (5/11).
- **Replay:** `ReplayProvider` returns the same responses in the same order. The tools still really run on a fresh copy of the fixture.
  - Known limit: if the participant skips a step, the replay still continues with the recorded next step. That's fine for measuring whether and when they catch the mistake.
- **Two conditions:**
  - **Console** (our UI)
  - **Plain log** (scrolling text, Stop button only, everything auto-approved)
  - Counterbalance the order across participants.
- **Study log** (`study_logs/<participant>_<condition>.jsonl`): a timestamp for every event and every click. From it:
  - **time to intervene** = planted step shown → Pause / Skip / Stop
  - **harmful action missed** = the planted step actually ran
  - then SUS and the 5 control statements from the survey, asked again

---

## 7. Tests (keep the existing 87 passing)

| Test file | Checks |
|---|---|
| `test_policy.py` | Every row of the risk-rules table, in both modes; out-of-scope paths blocked; `git push` marked permanent |
| `test_pause.py` | Pause stops before the next tool; resume continues; Stop leaves a valid history (every `tool_use` has a `tool_result`) |
| `test_checkpoint.py` | In a temporary git repo: edit → commit → undo restores the file; a conflicting undo reports "can't undo on its own" |
| `test_replay.py` | Record, then replay, gives identical responses |
| `test_preview.py` | Diff for edit/write; "used by" finds `import config_old` |

Run with `uv run pytest`. All tests stay credential-free, using fake providers like the existing tests.

---

## 8. Demo script (also the scenario on slide 5)

1. Task: "Clean up unused files". Scope: the fixture folder. Mode: Normal.
2. The plan appears: 5 steps, with "Delete config_old.json" tagged Undoable / asks. → **Approve plan**
3. List and read run on their own (timeline ticks). Edit `utils.py` → card with diff → **Approve**
4. Delete `config_old.json` → card shows `main.py: import config_old` → **Skip**
5. **Pause** → note "don't delete config files" → **Resume**
6. Summary → **Undo** the `utils.py` edit → the file is restored

Use a **replayed** run on stage so the mistake happens on cue. Keep a recorded video as backup.

---

## 9. Safety notes

- The agent only works inside `sandbox/`, a copy of the fixture. Never point it at real projects during demos or studies.
- `run_bash` still has no sandbox. Commands that ask for approval are the protection, so test the rules in `policy.py` carefully.
- Undo only covers files inside the sandbox's git repo. `git push` and other effects outside the computer can't be undone, which is why they always ask.
