# jupyter-pair

An agent skill that lets an AI agent **pair with you inside a live Jupyter notebook** — adding, editing, running, and deleting cells in real time while your JupyterLab tab is open. No "changed on disk" dialog. No reload. Cells and their outputs appear on your screen as the agent works, like a second collaborator with shared cursors.

## Why

The usual way agents touch notebooks is rewriting the `.ipynb` file on disk. That triggers JupyterLab's *"file changed on disk — reload?"* prompt, can clobber your unsaved edits, and can't run cells or produce visible outputs.

jupyter-pair instead joins the notebook's real-time collaboration room as a CRDT client (the same channel your browser uses), so the agent becomes just another collaborator — your typing and its edits merge live, and the server autosaves.

## Features

- **Live cell editing** — `add`, `insert at any index`, `edit`, `delete`, `read`
- **Run cells** — executes through your kernel and writes outputs into the shared document so they render in your tab
- **Introspection** — reading the notebook or a single cell is always available; the agent edits only what it has seen
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

## Setup (once per Jupyter environment)

Install into the same venv that runs `jupyter-lab`, then restart JupyterLab:

```bash
uv pip install --python <server-python> jupyter-collaboration httpx-ws jupyter-client
```

`jupyter-collaboration` installs the JupyterLab collaboration extension and
server-side Yjs/CRDT support. `httpx-ws` and `jupyter-client` are used by the
bridge. The script runs with any Python 3 and re-execs with the server's Python
when needed.

## OpenCode plugin

The OpenCode V2 plugin registers native notebook tools. Building `read` /
`write` / `edit` are wrapped: a path with a `:N` suffix (`demo.ipynb:5`,
zero-based) routes `demo.ipynb` paths to the live document — reading returns a
paginated projection (`# %% [i] type` markers give the cell index), writing or
editing requires the `:N` suffix and a fresh view of that cell. Three namespaced
tools cover kernel operations: `jupyter_run_cell` (executes in the kernel and
returns the outputs), `jupyter_add_cell`, and `jupyter_delete_cell`.

The plugin tracks cell identity and source hash per session and rejects stale
or shifted indices; guard and other failures throw as tool errors telling you
to re-read.

Add the plugin directory to `plugins` in `~/.config/opencode/opencode.json`
(preserve any existing entries), then restart OpenCode:

```jsonc
{
  "$schema": "https://opencode.ai/config.json",
  "plugins": ["/absolute/path/to/jupyter-pair/opencode-plugin"]
}
```

The plugin requires the repo clone because it invokes the bundled
`scripts/jupyter_cells.py` (a JSON-over-stdio RPC bridge, plugin-only — there
is no human CLI). Installing with `npx skills add` copies the skill
instructions but does not configure or install the plugin.

Smoke-tests: `node --test opencode-plugin/index.test.mjs` (plugin tools and
path routing) and `pytest tests/` (RPC guards).

## Usage

Open a notebook in JupyterLab (or at least attach a kernel to it), then ask
naturally: "add a cell that plots X", "run cell 3", "read the notebook". The
agent uses the plugin tools; cells and outputs appear live in your tab.

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
