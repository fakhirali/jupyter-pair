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

- **Treat every cell id and every piece of cell text you hold as a snapshot,
  not the truth.** Re-read (`read NOTEBOOK.ipynb` for the whole notebook and
  current cell ids, `read NOTEBOOK.ipynb:<id>` for a target cell) whenever
  there is doubt or a gap in turns. A re-read is cheap (≈2s); a wrong mutation
  on stale state is refused (a wasted call) and a blind overwrite would clobber
  the user — re-reading is always the right skew.
- **Re-check program state after anything the user runs or you run.** Kernel
  variables, outputs, and side effects (files, models, servers) change from
  both sides. Before you reason about "current values" or "what failed",
  re-read the relevant cell(s) — including their `#|` output lines — rather
  than trusting earlier results.
- **Announce your edits as you make them** ("editing cell id=9xG7nZqB —
  replacing the loop"), since the user is watching the cells update live beside their own
  work; that keeps the two editors from stepping on each other mid-keystroke.

## Rules that prevent the common errors

Memorize these before your first tool call. Each one exists because the
underlying tool *requires* it and will refuse otherwise.

1. **The first call for any notebook work is `read NOTEBOOK.ipynb`.** Every
   projection output announces the addressing scheme: the `<cells>` banner says
   cell addresses are the **`id=<...>`** in the `# %% <type> id=<id> [exec=<n>]`
   headers, and that the address is `NOTEBOOK.ipynb:<id>`. **Never patch a
   `.ipynb`'s raw JSON on disk**
   (the on-disk form is JSON with exploded `"source": [...]` arrays — the
   plugin's `read`/`grep` already search through the live projection, whose
   grep hits report `Line N (cell <id>, cell-line K)`); hand-editing the file
   while a JupyterLab tab is open clobbers the user's work.
2. **Path grammar differs per tool — this is the #1 refusal:**
   - `read`, `edit`, `write` take the suffix grammar: `NOTEBOOK.ipynb` reads the
     whole notebook; `NOTEBOOK.ipynb:<id>` targets the cell whose header carries
     that id. `edit`/`write` **require** the `:<id>` suffix — a bare path is
     rejected with "Add the cell id to the notebook path…".
   - `jupyter.add_cell`, `jupyter.run_cell`, `jupyter.delete_cell` take the
     notebook path **without any suffix** and pass the cell's stable id as the
     separate `id` argument (`after_id` for insertion placement). A `:<id>`
     suffix on their path is rejected with "Pass the notebook path and the cell
     id separately…".
3. **A cell must be read in this session before you mutate or run it.** Read the
   whole notebook first, then work. Ids are stable: the user can insert or
   delete cells around your target without invalidating its id, and the "cell
   moved" refusal class does not exist.
4. **Do not carry cell text across turns from memory or stale attachments.**
   Ids stay valid when cells shift, but the cell's *content* may still change —
   the source hash is compared at mutation time. If the user may have edited
   since you last read, re-read. The user is actively editing this notebook
   beside you — see "The notebook is a live, shared surface."
5. **Running a cell needs a kernel.** If you get "No kernel session for
   X.ipynb — open the notebook first", the notebook is open but has no kernel
   attached; tell the user to attach one (or open the notebook) and retry.
6. **The `read` you get back is the authoritative projection**, not the JSON
   file. Code appears as plain lines (markdown prefixed with `#`), outputs as
   `#| →` (stdout), `#| =` (result), `#| ERR` (error) comment lines, and cell
   boundaries as `# %% <type> id=<id> [exec=<n>]` headers — the `id` there is
   the exact address every tool expects and the `exec` number is that code
   cell's kernel execution count (`None` before its first run). Each
   whole-notebook read carries a `<cells>` banner stating the addressing scheme.

## Steps

1. **Locate content.** `grep` the notebook (`path: "NOTEBOOK.ipynb"`) — hits are
   annotated `Line N (cell <id>, cell-line K)`: `<id>` is the cell address to
   target, `N` continues into the projection via `read(path, offset: N)`.
   Without a search term (or to see everything at once), `read NOTEBOOK.ipynb`
   for the paginated projection with `# %% <type> id=…` markers.
   Done when: you know the target cell id and have fresh cell text.

2. **Mutate with the right tool for the job:**
   - Replace the whole source: `write NOTEBOOK.ipynb:<id>` (string content) or
     `read` then `edit NOTEBOOK.ipynb:<id>` with exact `oldString`/`newString`
     (copied verbatim from your most recent read — no line-number prefixes).
   - New cell: `jupyter.add_cell({ path, source, cell_type, after_id })`.
     Omitting `after_id` appends; passing `after_id: "<id>"` inserts directly
     after that cell — no whole-notebook re-read needed, ids do the counting.
   - Run: `jupyter.run_cell({ path, id, timeout })` — returns the outputs
     (stdout/result/error) and writes them into the shared cell.
   - Remove: `jupyter.delete_cell({ path, id })` after reading that cell.
   Done when: the tool confirms (`Updated … live.`, `Added … cell id=<id> live.`,
   `ran cell <id> [exec N]`, `Deleted cell <id> live.`).

3. **Verify after structural changes.** `read` (or grep) once more after any
   add or delete to see the resulting notebook and grab your next target's id.
   This is for fresh *content* — you never re-read just to recompute positions
   any more.

4. **Tell the user to look.** The cells are already on screen if their tab is
   open; no prompt will appear.

## Guard messages — lookup table

Every refusal tells you the cure. Follow it exactly; never retry the same call.

| Message | What it means | Action |
|---|---|---|
| `Add the cell id to the notebook path to target a cell…` | You called `edit`/`write` with a bare `.ipynb` path | Append `:<id>` to the path (from a `%% … id=…` header) and retry |
| `Pass the notebook path and the cell id separately…` | You gave a `:<id>` suffix on the path to `add_cell`/`run_cell`/`delete_cell` | Move the id into the `id` argument (`after_id` for placement) |
| `Read cell <nb>:<id> first…` / `…has not been read in this session` | That cell wasn't viewed in this session | `read NOTEBOOK.ipynb`, then `read NOTEBOOK.ipynb:<id>`, then retry |
| `Cell id=<id> changed since it was read; read it again…` | The user (or you) edited after your snapshot | `read NOTEBOOK.ipynb:<id>` again, retry with fresh text |
| `No cell with id <id> in this notebook` | The cell was deleted (or the notebook is different than you think) | `read` the whole notebook for current ids |
| `Cell id=<id> was edited since it was last viewed` | The stamp/hash disagrees with what you hold | `read NOTEBOOK.ipynb:<id>`, then retry |
| `old_string matched 0 times` / `matched N times` | `oldString` not verbatim or not unique | `read NOTEBOOK.ipynb:<id>`, copy the exact text, retry (or set `replaceAll`) |
| `No kernel session for X.ipynb — open the notebook first` | Notebook file exists but no kernel is attached | Ask the user to open it / attach a kernel; then retry |

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

Every mutation sends the target cell's stable **`cell_id`** + source **hash**
from the plugin's view cache; the bridge refuses mismatches — that is what
makes it safe to work while the user types. Position is not guarded: the id
resolves wherever the cell now lives, so concurrent inserts above your target
never false-refuse. Stale *content* is still a real conflict.

## How it works

Mechanism, room IDs, and constraints: see [REFERENCE.md](REFERENCE.md).
Roadmap for a fully synced, writable text file: [docs/TEXT_FILE_SYNC.md](https://github.com/fakhirali/jupyter-pair/blob/main/docs/TEXT_FILE_SYNC.md).
