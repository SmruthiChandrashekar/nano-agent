# Design Prototype Plan: Approve, Pause, Undo Console

This plan covers the **UI/UX prototype**: the screens, what's on each, how they link, and the steps from paper sketch to a clickable Figma prototype. The coded version (docs/prototype-plan.md) is built from these same screens.

Survey numbers below: n = 12, of whom 11 are agent users (9–11 Nov 2026).

---

## 1. Fidelity path

```
Paper sketches  →  Lo-fi wireframes  →  Hallway test  →  Hi-fi clickable  →  Heuristic review  →  Fix
 (Crazy 8s)        (grey boxes)         (2–3 people)     (Figma)             (3 evaluators)       (v2)
```

| Stage | Tool | Output | Who |
|---|---|---|---|
| Sketch | Paper, 8 sketches per person per key screen | Photos plus a vote on the best ideas | All 4 |
| Lo-fi | Figma, greyscale boxes, real labels, no colour | 8 screens (section 2) | B, with A |
| Hallway test | Lo-fi printed or on a laptop, think-aloud | List of problems and what changed | A |
| Hi-fi | Figma: colour, icons, real text, linked frames | Clickable demo scenario (section 4) | B |
| Heuristic review | Nielsen's 10 heuristics, severity 0–4 | Issue list, top fixes | D + 2 others |

---

## 2. Screens

| # | Screen | What's on it | Survey reason | HCI principle |
|---|---|---|---|---|
| S1 | **Start task** | Task box · scope picker ("Only these files / this folder") · mode switch **Normal / Strict** · Start | 8/12 "only files I choose"; 6/12 want every step asked | Locus of control (Shneiderman) |
| S2 | **Plan preview** | Numbered steps; each has a risk tag and the file it touches · "Touches 4 files" · **Approve plan** · Edit step · Remove step · Reject | Plan preview ranked **#1** | Gulf of execution (Norman) |
| S3 | **Running: live timeline** | Done steps ✓ with Undo · current step "running…" · next step greyed · top bar with **Pause** and **Stop** always visible | "Can tell what it's doing": 2.8/5 | Visibility of system status (Nielsen) |
| S4 | **Approval card** | File path, large · "Why: …" · diff (lines marked **Removed** / **Added**) · warning "Used by main.py, line 3" · badge **Undoable** or **Permanent** · Approve · Skip · Edit · "Allow for this task" | Which files 10/12, exact changes 7/12, why 5/12 | Prevent errors (Shneiderman), gulf of evaluation |
| S5 | **Paused** | "Paused after step 3" · Resume · **Add a note** ("change instructions") · Undo last step · Stop | Pause and check 10/12; change instructions 4/12 | User control and freedom (Nielsen) |
| S6 | **Undo confirm** | "Undo step 3: Edit utils.py" · before/after view · warning if a later step depends on it · Undo · Cancel | Undo weakest area (2.7/5); any single step 7/12 | Easy reversal (Shneiderman) |
| S7 | **Notification** | Toast: "Agent needs you: Delete config_old.json" | Notification 9/12; 9/11 don't watch every step | Visibility of system status |
| S8 | **Summary** | Files changed · Commands run · Skipped or failed · Still undoable (each with Undo) · Undo whole task | Files 12/12, commands 10/12, skipped/failed 9/12 | Closure (Shneiderman), gulf of evaluation |
| B0 | **Plain log** (baseline for testing) | Scrolling text log · Stop button only | Comparison condition | — |

### States to draw for the step row (used on S2, S3, S8)
`Queued` · `Running` · `Waiting for you` · `Done` · `Skipped` · `Failed` · `Undone`. Each state gets an icon **and** a word, never colour alone.

---

## 3. Visual rules

| Element | Rule |
|---|---|
| Risk tags | **Runs on its own** (green) · **Asks first** (amber) · **Permanent** (red). Always icon + word |
| Buttons on a Permanent card | **Skip** is the safer default; Approve needs a deliberate click (no Enter-to-approve) |
| Pause / Stop | Same place on every screen (top right); never hidden in a menu |
| Diff | Red/green **plus** the words "Removed" / "Added" (colour-blind safe) |
| Text | File paths in a monospace font; plain words, no git jargon ("Undo this step", not "Revert commit") |
| Contrast | Body text at least 4.5:1 |

Components to build once in Figma and reuse:
- Top bar
- Step row (7 states)
- Risk tag (3 kinds)
- Approval card
- Diff block
- Toast
- Summary section
- Primary and secondary buttons

---

## 4. Clickable demo scenario: "Clean up unused files"

Link these frames in Figma. For "agent is working" moments, use the **After delay** trigger (about 1.5 s) so the prototype moves on by itself, like a real agent.

| Frame | Shows | User action → next frame |
|---|---|---|
| F1 | S1 Start: task typed, scope = project folder, Normal mode | Click **Start** → F2 |
| F2 | S2 Plan: 5 steps; step 4 "Delete config_old.json" tagged Asks first | Click **Approve plan** → F3 |
| F3 | S3 Steps 1–2 (list, read) tick by themselves | After delay → F4 |
| F4 | S4 Card: Edit utils.py, diff of 4 lines | Click **Approve** → F5 |
| F5 | S3 Step 3 done, step 4 starting | After delay → F6 |
| F6 | S7 Toast + S4 card: Delete config_old.json, **Used by main.py, line 3**, Permanent badge | Click **Skip** → F7 |
| F7 | S3 Step 4 marked Skipped; step 5 running | Click **Pause** → F8 |
| F8 | S5 Paused, note typed: "Don't delete config files" | Click **Resume** → F9 |
| F9 | S8 Summary: 1 changed, 1 skipped, 1 undoable | Click **Undo** on utils.py → F10 |
| F10 | S6 Undo confirm with before/after | Click **Undo** → F11 |
| F11 | S8 Summary updated: utils.py shows Undone | End |

Baseline version (B0): one frame with the same steps scrolling as plain text (After delay), with only **Stop**. The delete happens with no card.

This same scenario is the slide 5 scenario, the live demo, and task 1 in the study.

---

## 5. Hallway test (lo-fi, 2–3 people, about 10 minutes each)

Tasks, given out loud:
1. "Start a cleanup of this project, but only let it touch the `src` folder."
2. "Something about to happen looks wrong. Stop it without stopping the whole task."
3. "You changed your mind about the edit to utils.py. Reverse it."

Watch for and note:
- where they hesitate
- buttons they can't find
- words they misread
- whether they notice the "Used by main.py" warning before approving

Fix the top 3 issues before hi-fi. Keep a before/after pair of screens for the paper.

---

## 6. Heuristic review (after hi-fi; 3 evaluators)

- Each evaluator works alone through F1–F11 with Nielsen's 10 heuristics and gives each issue a severity from 0 to 4.
- Merge the lists, then fix everything rated 3–4.
- This meets the paper rule of a heuristic evaluation by 3+ evaluators. Do it **in addition to** the user study, not instead of it.

---

## 7. Timeline

| When | Do |
|---|---|
| **Wed 11 Nov, morning** | Crazy-8 sketches for S2, S3, S4, S8 (the key screens); vote |
| **Wed 11 Nov, afternoon** | Lo-fi wireframes S1–S8 + B0 |
| **Wed 11 Nov, evening** | Hallway test (2–3 people); fix the top 3 issues |
| **Thu 12 Nov, morning** | Hi-fi frames F1–F11, linked and clickable. Screenshots into slides 6–7. Send the PDF 24 h before the slot |
| **13/14 Nov** | Present: click through F1–F11 live (video as backup) |
| **After** | Heuristic review → fixes → user study → coded console (docs/prototype-plan.md) |

### Who does what
| Person | Task |
|---|---|
| A (research) | Runs the sketch session and the hallway test; keeps the change log |
| B (design) | Lo-fi and hi-fi Figma, the component set, linking the frames |
| C (build) | Checks every screen can actually be built on nano-agent; starts Phase 0 code from the lo-fi |
| D (evaluation) | Builds the baseline frame B0, the heuristic review sheet and the study script |

---

## 8. Evidence to keep (for the slides and the paper's Methodology section)

- Photos of sketches and the vote
- Lo-fi screens, plus the hallway-test notes and what changed
- Hi-fi screens and the Figma prototype link
- The heuristic issue list with severities

Together these show the Gould & Lewis process: users first, measure, iterate. They also let someone else repeat the design process, which the paper rubric asks for.

**Fallback for the study:** if the coded console isn't ready in time, the Figma prototype with After-delay frames can run the comparison itself. Screen-record each session to measure time to intervene.
