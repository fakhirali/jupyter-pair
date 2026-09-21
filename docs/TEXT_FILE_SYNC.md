# Paired text file — design notes

Status: **idea / design**. The `text` action is the interim step (a read-only
projection); the full design below adds a synced, writable text file.

## Goal

Let an agent work on a Jupyter notebook **as a plain text file**:

- read and traverse it with ordinary bash text tools (grep, sed, awk, diff)
- write to it like a text file
- have every change reflected in the live notebook seamlessly (CRDT room,
  open JupyterLab tab updates with no reload dialog)

Why bother: agents are ~2x more efficient on percent-cell text than on
.ipynb JSON — the Duke Colab experiment measured 1.94x tokens, 1.71x model
calls, 2.32x runtime for the notebook workflow
(https://ai.colab.duke.edu/colab-ai-blog/all-blogs/how-file-format-shapes-agent-work-an-experiment-with-jupyter-notebooks/).

## The text format

jupytext percent format plus embedded outputs. One synced file per notebook,
next to it:

```
00_BashAgent.pynb        <- synced text (agent's working surface)
00_BashAgent.ipynb       <- notebook (browser + kernel, source of truth)
```

```python
# %% [1] code
import os

#| → stream: training started
#| = 42

# %% [2] markdown
# ## Results
# The model converges in 3 epochs.
```

- `# %% [i] <type>` marks cells; `[i]` is the cell index at dump time.
- Every output line starts with `#|` — outputs are a build artifact: the agent
  can read them, but sync ignores anything under `#|`.
- The file stays valid Python. Outputs hold streams, results, errors
  (text only); rich outputs are placeholders referencing sidecar files
  (`.pynb-outputs/cell-3-0.png`).

## Sync: 3-way merge, per cell

Two writers (agent edits the text, human edits JupyterLab) need a merge base.
Keep a snapshot `{cell_id: hash}` of the state at the last sync.

| text vs base | notebook vs base | resolution |
|---|---|---|
| changed | unchanged | apply agent's cell text to the CRDT |
| unchanged | changed | regenerate that cell's text (user edits flow out) |
| unchanged | unchanged | no-op |
| changed | changed | **conflict**: refuse that cell, ask the agent to reconcile |

Then always rewrite the text file wholesale from the notebook, so outputs are
current. The edited-since-viewed guard still applies per cell.

## Sync triggers

1. `sync` action (explicit, deterministic) — parse text → merge → apply via
   CRDT → rewrite text file. One round trip.
2. `watch` action (endgame) — hold the CRDT connection open; watch the file
   (fsevents/polling, ~1s debounce) and the ydoc; rewrite whichever side
   changed. Agent edits the file → notebook updates live; user runs a cell →
   the file's output block updates under the agent.

## Rules and limits

- **Execution stays in the kernel.** The agent runs cells with `run`, never by
  "executing" the text file: notebook semantics (out-of-order cells, kernel
  state) are not script semantics. `run --changed` is a natural companion —
  run exactly the cells a sync touched.
- **Outputs are read-only in the text file** (anything under `#|` is discarded
  on sync).
- **Cell-level merge only** — agents rewrite whole cells naturally; line-level
  merging inside a cell buys nothing.
- When a paired file exists, it is the agent's *only* write path: structured
  `edit`/`add` must rewrite the text file too (or be rejected), or the two
  surfaces drift.
- The `.pynb` file is derived state — deleting it is safe; `sync` regenerates.
- Interop: by using jupytext percent markers, the file can be paired with
  real jupytext later; the `#| ` output lines are plain comments to jupytext.

## Interim step (shipped)

`text` action: dumps the live notebook as the same percent-format text
(outputs as `#| ` lines) to stdout. The agent pipes it through grep/sed for
traversal and understanding, and still makes changes through the structured
commands (`edit`, `add`, `run`, ...) which carry the guards. No file, no sync
— the text view refreshes stamps like a full `list` view.
