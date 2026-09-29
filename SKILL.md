---
name: jupyter-pair
description: Edit and run cells in a running JupyterLab notebook so they appear live in the open tab — no "changed on disk" dialog, no reload. Use when the user asks to add, insert, edit, replace, remove, or delete cells in a .ipynb, to run a cell and have its outputs appear in the notebook, or to list or read a notebook's cells, and a Jupyter server is running.
---

# Jupyter cells

Edit a notebook through its live CRDT collaboration room, not the file on disk. Cells appear instantly in any open JupyterLab tab — the user's unsaved edits are part of the same shared document, so nothing is overwritten — and the server autosaves to disk. Writing the `.ipynb` file directly is the dialog path; the tools are the live path.

## Prerequisites (one-time)

1. Use JupyterLab 4.x and install the collaboration extension **in the same
   virtual environment that runs `jupyter-lab`**:
   `uv pip install --python <server-python> jupyter-collaboration httpx-ws jupyter-client`
2. Restart JupyterLab after installation. `jupyter-collaboration` supplies the
   JupyterLab extension and server-side Yjs/CRDT support; `httpx-ws` and
   `jupyter-client` are used by the bridge for live edits and kernel execution.
3. Enable the OpenCode V2 plugin by adding the cloned repo's `opencode-plugin`
   directory to `plugins` in `~/.config/opencode/opencode.json`, then restart
   OpenCode:
   `"plugins": ["/absolute/path/to/jupyter-pair/opencode-plugin"]`.
   Installing with `npx skills add` installs this skill, not the OpenCode plugin.
4. Verify by reading a notebook served by that JupyterLab with the `read` tool.
   Done when its cells print without a server/dependency error.

## Steps

1. **Inspect first.** Reading the notebook views it and refreshes all
   viewed-stamps, which unlocks `edit`/`run`/`delete` on every cell:
   - whole notebook: `read NOTEBOOK.ipynb` (paginated `# %% [i] type` markers
     give the zero-based cell index to target)
   - one cell: `read NOTEBOOK.ipynb:N`
   Done when: the dump prints cell types, sources, and outputs with no
   server/dependency error.

2. **Mutate.** Use `edit`/`write` with the `NOTEBOOK.ipynb:N` path shorthand,
   or `jupyter.add_cell` / `jupyter.run_cell` / `jupyter.delete_cell` with
   `path` + zero-based `index`. Read a cell (or the notebook) before touching it —
   the tools refuse stale views.
   Done when: the tool confirms (`Updated ... live.`, `Added ... cell ... live.`
   — or `ran cell N [exec_count]` followed by its outputs). Read afterwards to
   confirm content and outputs.

3. **Tell the user to look.** The cells are already on screen if their tab is
   open; no prompt will appear.

## Tool reference

- `read` / `write` / `edit` (native tools, wrapped for notebooks): pass a path
  with an optional `:N` suffix — `demo.ipynb` reads the projection of the whole
  notebook; `demo.ipynb:5` targets cell 5 (zero-based) for read/edit/write.
- `jupyter.run_cell({ path, index, timeout? })` — execute in the kernel and
  return the outputs (`--- stdout ---`, `--- result ---`, `--- error ---`).
  Outputs are written into the shared CRDT cell so they render in the user's
  tab too. Long-running cells (e.g. a dev server) hit the default 60s timeout —
  pass a larger `timeout`, and stop the cell in the UI afterwards.
- `jupyter.add_cell({ path, source, cell_type?, index? })` — no `index` appends.
  Read the whole notebook before inserting at an index.
- `jupyter.delete_cell({ path, index })` — read the cell first.

`run_cell` connects directly to the kernel (jupyter_client), then writes the
collected outputs into the shared cell.

## Troubleshooting

- `No running jupyter server serves …` — the notebook is not under any running
  server's root dir. Start `jupyter-lab` from the workspace root (or its parent).
- Deps error — the script prints the exact install command; run it and restart
  `jupyter-lab`.
- `room doc never synced` — the room could not be loaded. Check the notebook is
  reachable via the server's contents API.
- No live appearance and a dialog on reload — `jupyter-collaboration` is not
  installed on the server; you silently fell back to nothing. Never fall back
  to writing the `.ipynb` by hand while a tab is open: the user's save then
  clobbers the edit.
- `No kernel session for …` — the notebook is open on disk but no kernel is
  attached; ask the user to open it (or attach a kernel) and retry.
- `warning: kernel busy or unresponsive — request queued` — a cell is occupying
  the kernel (e.g. an input-loop REPL or dev server). One execution queue per
  kernel: nothing external can interleave.
- The per-session view cache lives in OpenCode's process; after a plugin
  hot-reload or session restart the guards re-arm — re-read before mutating.

## Edited-since-viewed guards

Every mutation request carries the cell's expected `id` and source **hash**
(with insertion guards: `expected_count`, `before_id`, `after_id`) from the
plugin's view cache; the bridge refuses mismatches with a message telling the
agent to `read` again. So you can never clobber the user's keystrokes made
since you last looked.

## How it works

Mechanism, room IDs, and constraints: see [REFERENCE.md](REFERENCE.md).
Roadmap for a fully synced, writable text file: [docs/TEXT_FILE_SYNC.md](https://github.com/fakhirali/jupyter-pair/blob/main/docs/TEXT_FILE_SYNC.md).
