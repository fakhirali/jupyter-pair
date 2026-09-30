# jupyter-pair

> **OpenCode 2 required.** The plugin targets OpenCode **V2** (`opencode v2.x`,
> the `Plugin.define` / `ctx.tool.*` API). On OpenCode 1.x the plugin will not
> load; the bundled bridge script has no other consumer.

An agent skill that lets an AI agent **pair with you inside a live Jupyter notebook** — adding, editing, running, and deleting cells in real time while your JupyterLab tab is open. No "changed on disk" dialog. No reload. Cells and their outputs appear on your screen as the agent works, like a second collaborator with shared cursors.

## Why

The usual way agents touch notebooks is rewriting the `.ipynb` file on disk. That triggers JupyterLab's *"file changed on disk — reload?"* prompt, can clobber your unsaved edits, and can't run cells or produce visible outputs.

jupyter-pair instead joins the notebook's real-time collaboration room as a CRDT client (the same channel your browser uses), so the agent becomes just another collaborator — your typing and its edits merge live, and the server autosaves.

## Features

- **Live cell editing** — `add`, `insert after any cell`, `edit`, `delete`, `read`
- **Run cells** — executes through your kernel and writes outputs into the shared document so they render in your tab
- **Notebook-aware native tools** — `read`, `write`, `edit`, and `grep` understand `.ipynb` paths: each cell is addressed by its stable **cell id** (`demo.ipynb:9xG7nZqB`) shown in the projection's `# %% <type> id=… [exec=…]` headers, and grep hits report the cell id and line directly
- **Edited-since-viewed guard** — the agent cannot mutate or run a cell whose content changed since it last viewed it; it must re-read first, so it never clobbers your keystrokes (positions are *not* guarded — your inserts/deletes never false-refuse the agent)
- **Zero hardcoding** — discovers the running server, port, and auth token from Jupyter's runtime files; re-execs itself with the server's Python if needed

## Install

### npm-style (once the package is published)

```sh
opencode plugin add jupyter-pair-opencode-plugin
# or pin: opencode plugin add jupyter-pair-opencode-plugin@0.2.0
```

OpenCode installs the tarball (plugin + bundled Python bridge) and manages it
from there; unpinned versions auto-update in the background. The skill:

```sh
npx skills add fakhirali/jupyter-pair
```

### From a clone (local path, hot-reloadable)

The local path is what you need during development — OpenCode watches it and
picks up changes:

```bash
git clone https://github.com/fakhirali/jupyter-pair
```

Then in `~/.config/opencode/opencode.json` (preserve any existing entries):

```jsonc
{
  "$schema": "https://opencode.ai/config.json",
  "plugins": ["/absolute/path/to/jupyter-pair/opencode-plugin"]
}
```

The plugin tarball/clone is self-contained (bridge at
`opencode-plugin/scripts/jupyter_cells.py`). Either way, install the skill
alongside: `npx skills add /path/to/jupyter-pair`.

### Publishing a new npm release

```sh
cd opencode-plugin && npm publish
```

`npm pack` produces `jupyter-pair-opencode-plugin-<version>.tgz` containing
`index.mjs` and `scripts/jupyter_cells.py` only.

### Jupyter environment (once per machine that runs `jupyter-lab`)

Install the collaboration stack into the same virtual environment that runs
`jupyter-lab`, then restart JupyterLab:

```bash
uv pip install --python <server-python> jupyter-collaboration httpx-ws jupyter-client
```

`jupyter-collaboration` provides the JupyterLab extension and server-side
Yjs/CRDT support; `httpx-ws` and `jupyter-client` are used by the bridge for
live edits and kernel execution.

### Verify the install

1. Start `jupyter-lab` in (or as an ancestor of) the directory containing your
   notebooks — the bridge discovers servers from `~/Library/Jupyter/runtime/`,
   and the notebook must live under the server's root dir.
2. Open a notebook in JupyterLab and attach a kernel (`run_cell` requires one).
3. In **OpenCode 2**, `read demo.ipynb` — done when you see the paginated
   projection with `# %% <type> id=… [exec=…]` headers and no server/dependency
   error.
4. Try `demo.ipynb:<id>` edits and `jupyter.run_cell`: cells and outputs update
   live in your JupyterLab tab.

Smoke-tests: `node --test opencode-plugin/index.test.mjs` (plugin tools and
path routing) and `pytest tests/` (RPC guards).

## Usage

Ask naturally: "add a cell that plots X", "grep the notebook for
knapsack". The agent uses the plugin tools; cells and outputs appear live in
your tab. Tool conventions the agent follows: `read` (bare or `:<id>` path),
`edit`/`write` (require `:<id>`), grep (annotates hits with cell ids), and
`jupyter.add_cell` / `jupyter.run_cell` / `jupyter.delete_cell` (plain path +
`id` argument, `after_id` for insertion placement).

## How it works

1. Discovers the server via `~/Library/Jupyter/runtime/jpserver-*.json` (url, token, root dir)
2. `PUT /api/collaboration/session/<path>` → room id `<format>:<type>:<fileId>`
3. Connects as a CRDT client (`pycrdt`) to `ws://…/api/collaboration/room/<room_id>`
4. Mutates the shared `YNotebook` — CRDT merge delivers changes to every open tab
5. For `run`: connects to the kernel with `jupyter_client`, collects outputs, and writes them into the shared cell

Details and gotchas: [REFERENCE.md](REFERENCE.md).

## Requirements

- **OpenCode 2** (`opencode v2.x`) — the plugin uses the V2 plugin API
  (V1 `server()` hooks won't see these tools)
- macOS/Linux with a local JupyterLab ≥ 4.x
- Python ≥ 3.13 on the server side (for pycrdt binary wheels)
- `jupyter-collaboration` + `httpx-ws` installed in the server's venv

## License

MIT
