---
name: jupyter-pair
description: Edit and run cells in a running JupyterLab notebook so they appear live in the open tab — no "changed on disk" dialog, no reload. Use when the user asks to add, insert, edit, replace, remove, or delete cells in a .ipynb, to run a cell and have its outputs appear in the notebook, or to read a notebook's cells, and a Jupyter server is running.
---

# Jupyter cells

Edit a notebook through its live CRDT collaboration room, not the file on disk.
Cells appear instantly in any open JupyterLab tab — the user's unsaved edits are
part of the same shared document, so nothing is overwritten — and the server
autosaves to disk. Writing the `.ipynb` file directly is the dialog path; the
tools are the live path.

## The notebook is a live, shared surface

The user is working in the same document while you work: typing in cells,
running them, fixing code, inserting or deleting cells — often concurrently
with you. You are a collaborator, not the owner.

- **Treat every cell index and every piece of cell text you hold as a snapshot,
  not the truth.** Re-read (`read NOTEBOOK.ipynb` for the whole notebook and
  fresh indices, `read NOTEBOOK.ipynb:N` for a target cell) whenever there is
  doubt or a gap in turns. A re-read is cheap (≈2s); a wrong mutation on stale
  state is refused (a wasted call) and a blind overwrite would clobber the
  user — re-reading is always the right skew.
- **Re-check program state after anything the user runs or you run.** Kernel
  variables, outputs, and side effects (files, models, servers) change from
  both sides. Before you reason about "current values" or "what failed",
  re-read the relevant cell(s) — including their `#|` output lines — rather
  than trusting earlier results.
- **Announce your edits as you make them** ("editing cell 12 — replacing the
  loop"), since the user is watching the cells update live beside their own
  work; that keeps the two editors from stepping on each other mid-keystroke.

## Rules that prevent the common errors

Memorize these before your first tool call. Each one exists because the
underlying tool *requires* it and will refuse otherwise.

1. **The first call for any notebook work is `read NOTEBOOK.ipynb`.** Every
   projection output announces the addressing scheme: the `<cells>` banner says
   cell numbers are the `(N)` in the `# %% [N] type` headers, and that the
   address is `NOTEBOOK.ipynb:N`. **Never patch a `.ipynb`'s raw JSON on disk**
   (the on-disk form is JSON with exploded `"source": [...]` arrays — the
   plugin's `read`/`grep` already search through the live projection, whose
   grep hits report `Line N (cell C, cell-line K)`); hand-editing the file
   while a JupyterLab tab is open clobbers the user's work.
2. **Path grammar differs per tool — this is the #1 refusal:**
   - `read`, `edit`, `write` take the suffix grammar: `NOTEBOOK.ipynb` reads the
     whole notebook; `NOTEBOOK.ipynb:N` targets cell N (zero-based).
     `edit`/`write` **require** the `:N` suffix — a bare path is rejected with
     "Add :N to the notebook path…".
   - `jupyter.add_cell`, `jupyter.run_cell`, `jupyter.delete_cell` take the
     notebook path **without any `:N`** and pass the cell index as the separate
     `index` argument. A `:N` suffix on their path is rejected with "Pass the
     notebook path and index separately…".
3. **A cell must be read in this session before you mutate or run it.** Read the
   whole notebook first, then work. After any add or delete, later indices
   shift — re-read before touching anything at a higher index.
4. **Do not carry cell indices across turns from memory or stale attachments.**
   If the user may have edited since you last read, re-read. The user is
   actively editing this notebook beside you — see "The notebook is a live,
   shared surface."
5. **Running a cell needs a kernel.** If you get "No kernel session for
   X.ipynb — open the notebook first", the notebook is open but has no kernel
   attached; tell the user to attach one (or open the notebook) and retry.
6. **The `read` you get back is the authoritative projection**, not the JSON
   file. Code appears as plain lines (markdown prefixed with `#`), outputs as
   `#| →` (stdout), `#| =` (result), `#| ERR` (error) comment lines, cell
   boundaries as `# %% [i] code|markdown` lines — the `i` there is the exact
   index every tool expects, and each whole-notebook read carries a `<cells>`
   banner stating the addressing scheme.

## Steps

1. **Locate content.** `grep` the notebook (`path: "NOTEBOOK.ipynb"`) — hits are
   annotated `Line N (cell C, cell-line K)`: `C` is the cell index to target,
   `N` continues into the projection via `read(path, offset: N)`. Without a
   search term (or to see everything at once), `read NOTEBOOK.ipynb` for the
   paginated projection with `# %% [i]` markers.
   Done when: you know the target cell index and have fresh cell text.

2. **Mutate with the right tool for the job:**
   - Replace the whole source: `write NOTEBOOK.ipynb:N` (string content) or
     `read` then `edit NOTEBOOK.ipynb:N` with exact `oldString`/`newString`
     (copied verbatim from your most recent read — no line-number prefixes).
   - New cell: `jupyter.add_cell({ path, source, cell_type, index })`. Omitting
     `index` appends. **To insert at an index you must have read the WHOLE
     notebook in this session first** (insertion guards verify counts and
     neighbors). `index` is zero-based, *before* the cell that will end up at
     that position — e.g. `index: 5` inserts so the new cell becomes cell 5.
   - Run: `jupyter.run_cell({ path, index, timeout })` — returns the outputs
     (stdout/result/error) and writes them into the shared cell.
   - Remove: `jupyter.delete_cell({ path, index })` after reading that cell.
   Done when: the tool confirms (`Updated … live.`, `Added … cell N live.`,
   `ran cell N [exec_count]`, `Deleted cell N live.`).

3. **Verify after structural changes.** `read` the notebook again after any
   add or delete — both to confirm to the user and to refresh your index
   knowledge for follow-up work.

4. **Tell the user to look.** The cells are already on screen if their tab is
   open; no prompt will appear.

## Guard messages — lookup table

Every refusal tells you the cure. Follow it exactly; never retry the same call.

| Message | What it means | Action |
|---|---|---|
| `Add :N to the notebook path to target a cell…` | You called `edit`/`write` with a bare `.ipynb` path | Append `:CELL` to the path and retry |
| `Pass the notebook path and index separately…` | You gave `:N` on the path to `add_cell`/`run_cell`/`delete_cell` | Move the number into the `index` argument |
| `Read cell N first…` / `…read it before writing or running` | That cell wasn't viewed in this session | `read NOTEBOOK.ipynb:N`, then retry |
| `Cell N changed since it was read; read the cell again…` | The user (or you) edited after your snapshot | `read NOTEBOOK.ipynb:N` again, retry with fresh text |
| `Cell N moved or was replaced…` | Cells were inserted/deleted; your index now points elsewhere | `read` the whole notebook, re-derive the index |
| `Notebook cells shifted… read the notebook again before inserting` | `add_cell` with `index` after a stale view | `read` whole notebook, retry |
| `old_string matched 0 times` / `matched N times` | `oldString` not verbatim or not unique | `read NOTEBOOK.ipynb:N`, copy the exact text, retry (or set `replaceAll`) |
| `No kernel session for X.ipynb — open the notebook first` | Notebook file exists but no kernel is attached | Ask the user to open it / attach a kernel; then retry |
| `Invalid insertion index N…` | Index out of range | `read` the notebook to see the real cell count |

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

## Troubleshooting

- `No running jupyter server serves …` — the notebook is not under any running
  server's root dir. Start `jupyter-lab` from the workspace root (or its parent).
- Deps error — the script prints the exact install command; run it and restart
  `jupyter-lab`.
- No live appearance and a dialog on reload — `jupyter-collaboration` is not
  installed on the server. Never fall back to writing the `.ipynb` by hand
  while a tab is open: the user's save then clobbers the edit.
- `warning: kernel busy or unresponsive — request queued` — a cell is occupying
  the kernel (e.g. an input-loop REPL or dev server). One execution queue per
  kernel: nothing external can interleave.
- The per-session view cache lives in OpenCode's process; after a plugin
  hot-reload or session restart the guards re-arm — re-read before mutating.

## Edited-since-viewed guards

Every mutation sends the target cell's expected `id` + source **hash** (inserts
add `expected_count`/`before_id`/`after_id`) from the plugin's view cache; the
bridge refuses mismatches — that is what makes it safe to work while the user
types.

## How it works

Mechanism, room IDs, and constraints: see [REFERENCE.md](REFERENCE.md).
Roadmap for a fully synced, writable text file: [docs/TEXT_FILE_SYNC.md](https://github.com/fakhirali/jupyter-pair/blob/main/docs/TEXT_FILE_SYNC.md).
