# Jupyter cells — reference

## Mechanism

JupyterLab (4.x with `jupyter-collaboration`) loads each open document as a CRDT
(Yjs) doc, synchronized over a websocket room. An external client can join the
room and mutate the shared document; CRDT merge propagates the change to every
connected browser with no file-change dialog, and the server's autosave
(`document_save_delay`, ~1s) persists it to disk.

The script's chain:

1. `GET <runtime>/jpserver-*.json` → pick the server whose `root_dir` contains
   the notebook; that file also carries `url`, `token`, and `pid`.
2. `PUT /api/collaboration/session/<path>` with `{"format": "json", "type": "notebook"}`
   → `fileId` and `sessionId`. **The room ID is not the path**: it is
   `<format>:<type>:<fileId>` (e.g. `json:notebook:8833ab9a-…`). Connecting with
   the path as the room name creates a phantom empty room.
3. Websocket to `ws://<server>/api/collaboration/room/<room_id>?sessionId=…&token=…`
   with an `Authorization: token` header.
4. `pycrdt.Provider(doc, HttpxWebsocket(ws, room_id))` inside `async with`
   (the provider must be started — a plain `started.wait()` without `start()`
   hangs forever), then `jupyter_ydoc.ydocs["notebook"](ydoc)`.
5. Wait for sync = cell count stable across two 0.25s polls. Then mutate and
   sleep ~2.5s so updates flush and the server autosaves.

## CRDT API notes (jupyter_ydoc 4.x, pycrdt 0.14.x)

- `ynb.ycells` is a `pycrdt.Array` — supports `insert`, `pop`, `append`; it has
  **no** `delete` method.
- `append_cell(dict)` appends; `set_cell(i, dict)` replaces; `create_ycell(dict)`
  returns a `Map` for `insert`.
- A cell dict needs `cell_type`, `source` (str or list), `metadata`. `code`
  cells get `outputs: []` and `execution_state: "idle"` on creation —
  `set_cell` therefore clears outputs; for output-bearing cells, append a new
  cell instead.
- Package layout moved over versions: the client provider is `pycrdt.Provider`
  (was `pycrdt_websocket.WebsocketProvider`), the websocket channel lives at
  `pycrdt.websocket.websocket.HttpxWebsocket`, and the doc classes are
  `jupyter_ydoc.ydocs["notebook"]` (a dict, not attributes).

## Constraints

- Notebook files only; other formats need different `format`/`type` and a
  different ydoc class.
- **Code cells must carry `execution_count` (can be `None`) and `outputs`** or
  the server refuses to save ("Notebook JSON is invalid"). The script handles
  this for `add`; keep it in mind when hand-building cell dicts.
- `run` talks to the kernel via `jupyter_client.BlockingKernelClient` against
  the kernel's connection file (`find_connection_file(<kernel_id>)`), inside
  `asyncio.to_thread`. A bare `KernelClient()` fails (`ChannelABC() takes no
  arguments`) — the blocking class is required. `wait_for_ready()` before
  `execute()` is not optional: without it the iopub subscription lags and
  outputs come back empty.
- Editing the ws-channel message format by hand (JSON with a `channel` key) is
  rejected by ipykernel as "Invalid message" — use jupyter_client instead.
- **Edited-since-viewed stamps**: `metadata["agent_seen"] = {"hash": <sha1 of source>}`.
  Write stamps in-place on the CRDT cell (`ynb.ycells[i]["metadata"][STAMP_KEY] =
  {...}`) — nested dicts auto-convert to YMaps; `set_cell` would clear outputs.
  A stale hash = the user (or anything else) changed the source since the agent
  last viewed/edited it. Metadata shows up in the saved notebook file — it is
  the deliberate trade-off for state that travels with the notebook.
- `wait_for_ready` fails while the kernel is busy (a cell with an input loop or
  infinite loop holds the shell queue; kernel_info cannot interleave). The
  script warns and queues instead of hanging — but the target work still runs
  after the busy cell ends. Introspection (`exec`) needs the kernel idle or at
  least between cells.
- One server per notebook path: the script picks the most recent
  `jpserver-*.json` whose root dir contains the notebook.
- Rooms persist after the last client leaves (`auto_clean_rooms=False`), so
  editing a notebook that is open but untouched elsewhere is safe; a room can
  still be stale if the browser tab died mid-session — `list` before mutating.
- The server must be the one with `jupyter-collaboration`; the script finds its
  interpreter via `ps -p <pid>` and re-execs itself, so run it with any python.
