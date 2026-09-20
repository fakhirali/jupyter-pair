---
name: jupyter-pair
description: Edit and run cells in a running JupyterLab notebook so they appear live in the open tab — no "changed on disk" dialog, no reload. Use when the user asks to add, insert, edit, replace, remove, or delete cells in a .ipynb, to run a cell and have its outputs appear in the notebook, or to list or read a notebook's cells, and a Jupyter server is running.
---

# Jupyter cells

Edit a notebook through its live CRDT collaboration room, not the file on disk. Cells appear instantly in any open JupyterLab tab — the user's unsaved edits are part of the same shared document, so nothing is overwritten — and the server autosaves to disk. Writing the `.ipynb` file directly is the dialog path; the script is the live path.

## Steps

1. **Inspect first.** `python3 scripts/jupyter_cells.py NOTEBOOK.ipynb list`
   Done when: one line per cell prints (`index  type  first line`). This proves the server, token discovery, and CRDT deps all work before you mutate anything.

2. **Mutate.** `read`, `add`, `edit`, `delete` — full syntax in the actions table below. `--source` takes `'inline text'`, `@path/to/file`, or `-` (stdin). For multi-cell work, run the script once per cell.
   Done when: the command prints `live doc: X -> Y cells` followed by `disk: Y cells` — the disk line is the server confirming it persisted the change. `X -> Y` must match the change you intended (edit keeps the count equal).

3. **Tell the user to look.** The cells are already on screen if their tab is open; no prompt will appear.

## Actions

```
list                                show every cell: index, type, first line
read INDEX                          print a cell's full source
add  [--type code|markdown] [--index N] [--source S] [--run] [--timeout S]
                                    no --index = append; --run executes after adding
run  INDEX [--timeout S]            execute in the kernel, write outputs live
edit INDEX [--source S]             replaces the cell (code: clears outputs)
delete INDEX
```

`--source` defaults to stdin when piped. Prefer `@file` for long sources.
`run`/`--run` connect directly to the kernel (jupyter_client) and then write
the collected outputs into the shared CRDT cell, so results render in the
user's tab too. Long-running cells (e.g. a dev server) will hit the default
60s timeout — pass `--timeout`, and stop the cell in the UI afterwards.

For debugging, introspect the kernel's state with `exec` — it touches no cell,
so it never pollutes the notebook: `%whos`, `list(locals().keys())`, or
targeted probes like `type(x), getattr(x, 'shape', None)`. Result prints to
stdout; the notebook stays untouched.

## Setup (once per venv)

The script runs with any Python; it auto-re-execs with the Jupyter server's own interpreter, which is where the deps must live:

1. Install into the venv that runs `jupyter-lab`:
   `uv pip install --python <venv-python> jupyter-collaboration httpx-ws`
2. Restart `jupyter-lab` — the collaboration extension loads at startup.
3. Verify: run `list` on any notebook. Done when: cell list prints without error.

One-time per server restart, not per session.

## Troubleshooting

- `No running jupyter server serves …` — the notebook is not under any running server's root dir. Start `jupyter-lab` from the workspace root (or its parent).
- Deps error — the script prints the exact install command; run it and restart `jupyter-lab`.
- `room doc never synced` — the room could not be loaded. Check the notebook is reachable via the server's contents API.
- No live appearance and a dialog on reload — `jupyter-collaboration` is not installed on the server; you silently fell back to nothing. Never fall back to writing the `.ipynb` by hand while a tab is open: the user's save then clobbers the edit.
- `warning: kernel busy or unresponsive — request queued` — a cell is occupying the kernel (e.g. an input-loop REPL or dev server). `run`/`exec` sit behind it until it finishes or the kernel is interrupted. One execution queue per kernel: nothing external can interleave.

## Edited-since-viewed guards

Every cell carries an `agent_seen` hash in its `metadata` (written in-place in
the CRDT doc — the hash value never needs to be shown to the agent):

- `add` stamps what it wrote; `read` refreshes the stamp (viewing).
- `edit` and `run` are **gated**: a stale or missing stamp (`*edited*` / `*new*`
  in `list`) makes them refuse with a message telling the agent to `read` the
  cell first. `list` shows the markers but does not refresh stamps — listing
  is not viewing.
- The hash lives in cell `metadata`, so it persists in the notebook file and
  the agent only ever sees the boolean, never the hash itself.

## How it works

Mechanism, room IDs, and constraints: see [REFERENCE.md](REFERENCE.md).
