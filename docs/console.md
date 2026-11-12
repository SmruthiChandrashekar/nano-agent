# nano-agent app (Approve · Pause · Undo)

A local, Claude Code-style app for nano-agent:
- a **sidebar** with your projects (folders) and their chats
- a **home screen** to start a task
- a **chat view** where the agent's steps, plans and approval cards appear in the conversation

Risky steps wait for you, you can pause at any time, and every change can be undone.

## Run it with Gemini

From the `code/` folder:

```bash
uv sync
```

Set your key for this PowerShell session (get one at https://aistudio.google.com/apikey):

```powershell
$env:GEMINI_API_KEY="your-key"
```

Start the app, then open http://127.0.0.1:8765:

```bash
uv run nano-agent-console
```

Click **Import a folder** (the folder icon in the sidebar) to add a project. You can also start the app with a project already imported:

```bash
uv run nano-agent-console --workspace C:\path\to\a\test-project
```

**Only import folders you're comfortable letting an agent change.** Shell commands are not sandboxed.

Projects and chats are saved in `%USERPROFILE%\.nano-agent` (change it with `--home`). Chats can be continued after a restart. With Gemini, the replies' thought signatures are saved too, so continued chats keep working.

### Model
- The default is `gemini-3.7-flash`. Change it with `--model`, or with `model:` in `code/nano-agent.yml`.
- Check current model names at https://ai.google.dev/gemini-api/docs/models
- To use Claude instead: `--provider anthropic` and set `ANTHROPIC_API_KEY`.

## Demo mode (no API key, for the presentation)

```bash
uv run nano-agent-console --demo
```

- A **Demo project** appears in the sidebar.
- A new chat there resets the project and plays the "Clean up unused files" scenario with a scripted model. Every tool runs for real on that project: the "Still used by main.py" warning, the test run, pause notes and undo all behave for real.
- Follow-up messages get a short scripted answer.

In VS Code / Claude Code, `.claude/launch.json` has a `console-demo` entry that starts it.

## Using it

| Where | What you can do |
|---|---|
| Sidebar | New chat · search chats · import a folder · open a chat · start a chat in a project (+) · delete a chat · remove a project from the list (the folder is kept). A dot shows a running chat; amber means it is waiting for you |
| Home | Type a task, pick the project, choose **Plan first** and **Risky steps / Every change**, or start from a suggestion. Recent chats are listed below |
| Chat | Approve or reject the plan · approve or skip risky steps (keys **A** / **S**) · **Pause** (key **P**), then type a note or undo something · **Stop** · **Undo** on any changed step · **Undo all changes from this turn** · rename the chat |
| While it works | Anything you type becomes a note the agent reads before its next step |
| Permissions (sliders icon) | What asks first (survey defaults; deletes and pushes always ask) · which files it may touch · sound alerts |

## Code map

| File | Job |
|---|---|
| `server.py` | Web server: projects/chats API, folder browser, WebSocket, one runtime per chat |
| `store.py` | Saves projects and chats as JSON; folder browsing for the import dialog |
| `static/app.html` | The app UI (sidebar, home, chat, permissions drawer, import dialog) |
| `agent.py` | The loop, with plan-first, pause/resume with notes, stop, and the reason for each step |
| `policy.py` | Risk rules from the survey (Q13): what runs alone, what asks, what is out of scope |
| `preview.py` | Diff and "still used by" search for approval cards |
| `checkpoint.py` | Undo: a private git history in `<project>/.nano-agent/` (never your `.git`) |
| `workspace_tools.py` | Tools bound to a project folder, so several projects can be open at once |
| `tools/delete_file.py` | Deletes go to `.trash/` so they can be undone |
| `providers/gemini.py` | Gemini provider; keeps thought signatures, and can save and restore them |
| `demo.py` | Scripted model and demo project |

## Tests

```bash
uv run pytest
```

The tests use mocked models, so no API key is needed.
