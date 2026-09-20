# jupyter-pair

An agent skill that lets an AI agent **pair with you inside a live Jupyter notebook** — adding, editing, running, and deleting cells in real time while your JupyterLab tab is open. No "changed on disk" dialog. No reload. Cells and their outputs appear on your screen as the agent works, like a second collaborator with shared cursors.

## Why

The usual way agents touch notebooks is rewriting the `.ipynb` file on disk. That triggers JupyterLab's *"file changed on disk — reload?"* prompt, can clobber your unsaved edits, and can't run cells or produce visible outputs.

jupyter-pair instead joins the notebook's real-time collaboration room as a CRDT client (the same channel your browser uses), so the agent becomes just another collaborator — your typing and its edits merge live, and the server autosaves.

## Features

- **Live cell editing** — `add`, `insert at any index`, `edit`, `delete`, `list`, `read`
- **Run cells** — executes through your kernel and writes outputs into the shared document so they render in your tab
- **Introspection** — `exec` runs arbitrary code in the kernel for state debugging (`%whos`, probing objects) without touching any cell
- **Edited-since-viewed guard** — cells carry an invisible stamp in their metadata; the agent cannot `edit` or `run` a cell you changed since it last viewed it — the script refuses and tells it to `read` the cell first
- **Zero hardcoding** — discovers the running server, port, and auth token from Jupyter's runtime files; re-execs itself with the server's Python if needed

## Install

With the [skills CLI](https://github.com/vercel/skills):

```bash
npx skills add fakhirali/jupyter-pair
```

Or manually:

```bash
git clone https://github.com/fakhirali/jupyter-pair ~/.agents/skills/jupyter-pair
```

## Setup (once per venv)

The skill requires real-time collaboration on the Jupyter server. Install into the venv that runs `jupyter-lab`, then restart it:

```bash
uv pip install --python <venv-python> jupyter-collaboration httpx-ws
# restart jupyter-lab
```

The script runs with any Python 3 and finds the server's own interpreter for the CRDT dependencies.

## Usage

The skill's SKILL.md tells the agent what to do; you usually just ask naturally ("add a cell that plots X", "run cell 3", "check what's in the kernel"). Directly, the CLI is:

```bash
scripts/jupyter_cells.py NOTEBOOK.ipynb ACTION [args]
```

```text
list                                show every cell (with *edited*/*new* markers)
read INDEX                          print a cell's full source (marks it as viewed)
add  [--type code|markdown] [--index N] [--source S] [--run] [--timeout SEC]
run  INDEX [--timeout S]            execute the cell, write outputs live
exec [--source S] [--timeout S]     run code in the kernel, print result — no cell touched
edit INDEX [--source S]             replace a cell's source (preserves outputs)
delete INDEX
```

`--source` takes inline text, `@file`, or `-` (stdin).

### The edited-since-viewed guard

Every cell carries an `agent_seen` stamp in its `metadata`. `edit` and `run` refuse on any cell whose content changed since the agent last viewed it:

```
$ jupyter_cells.py nb.ipynb edit 0
cell 0 was edited since it was last viewed — view it first: run `read 0`, then retry
```

`list` shows `*edited*` (changed since the agent last saw it) and `*new*` (never viewed) markers. This keeps the agent from overwriting your keystrokes.

## How it works

1. Discovers the server via `~/Library/Jupyter/runtime/jpserver-*.json` (url, token, root dir)
2. `PUT /api/collaboration/session/<path>` → room id `<format>:<type>:<fileId>`
3. Connects as a CRDT client (`pycrdt`) to `ws://…/api/collaboration/room/<room_id>`
4. Mutates the shared `YNotebook` — CRDT merge delivers changes to every open tab
5. For `run`: connects to the kernel with `jupyter_client`, collects outputs, and writes them into the shared cell

Details and gotchas: [REFERENCE.md](REFERENCE.md).

## Requirements

- macOS/Linux with a local JupyterLab ≥ 4.x
- Python ≥ 3.13 on the server side (for pycrdt binary wheels)
- `jupyter-collaboration` + `httpx-ws` installed in the server's venv

## License

MIT
