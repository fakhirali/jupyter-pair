#!/usr/bin/env python3
"""RPC bridge: edit a live Jupyter notebook cell-by-cell through the Yjs
collaboration room on behalf of the jupyter-pair OpenCode plugin.

Cells appear immediately in any open JupyterLab tab — no "changed on disk"
dialog, no reload. Requires jupyter-collaboration on the Jupyter server.

Usage (machine only, no human CLI):

    echo '<json-request>' | jupyter_cells.py NOTEBOOK.ipynb rpc

The request is one JSON object read from stdin with an `op` field — snapshot,
read, add, write, edit, run, delete — carrying optimistic-concurrency fields (cell_id + expected_hash; numeric
`index` remains accepted on read/add for compatibility). The response is a
single JSON object on stdout:
{"ok": true, ...} or {"ok": false, "error": "..."}. Requests are the plugin's
interface; use the plugin tools, not this script, from a session.
"""
import asyncio
import json
import os
import re
import subprocess
import sys
from urllib.parse import quote
import urllib.request
from queue import Empty
from pathlib import Path

RUNTIME_DIR = Path(
    os.environ.get("JUPYTER_RUNTIME_DIR", Path.home() / "Library/Jupyter/runtime")
)

USAGE = __doc__


def all_servers():
    servers = []
    for f in sorted(RUNTIME_DIR.glob("jpserver-*.json"), key=lambda p: p.stat().st_mtime, reverse=True):
        try:
            info = json.loads(f.read_text())
        except Exception:
            continue
        if "token" in info:
            servers.append(info)
    return servers


def find_server(nb_path: Path):
    """Return (base_url, token, pid, server_root) for the server serving nb_path."""
    servers = all_servers()
    for info in servers:
        if str(nb_path).startswith(os.path.realpath(info["root_dir"]) + os.sep):
            return (info["url"].rstrip("/"), info["token"], info["pid"],
                    Path(os.path.realpath(info["root_dir"])))
    raise SystemExit(
        f"No running jupyter server serves {nb_path}.\n"
        f"Servers found: {[(s['root_dir'], s['port']) for s in servers]}\n"
        "Fix: start jupyter-lab in the notebook's directory (or a parent of it), "
        "or pass the notebook's full path."
    )


def suggest_notebook(nb_path: Path):
    """Descriptive error when the notebook path does not exist (e.g. renamed)."""
    import difflib
    candidates = []
    for info in all_servers():
        root = Path(os.path.realpath(info["root_dir"]))
        candidates += [str(h) for h in root.glob(f"**/{nb_path.name}")]
        candidates += [str(h) for h in root.rglob("*.ipynb")
                       if ".ipynb_checkpoints" not in h.parts]
    candidates = list(dict.fromkeys(candidates))
    close_names = set(difflib.get_close_matches(nb_path.name,
                                                [Path(c).name for c in candidates], n=5))
    matches = [c for c in candidates if Path(c).name in close_names][:5]
    lines = [f"Notebook not found: {nb_path}"]
    if matches:
        lines.append("Closest matches under running servers (use one of these paths):")
        lines += [f"  {m}" for m in matches]
    else:
        lines.append("No similarly-named notebook exists under any running server's root dir.")
    raise SystemExit("\n".join(lines))


def server_python(pid: int) -> str | None:
    """Path of the python interpreter running the jupyter-lab server."""
    try:
        cmd = subprocess.run(["ps", "-p", str(pid), "-o", "command="],
                             capture_output=True, text=True).stdout.strip()
        parts = cmd.split()
        return parts[0] if parts and parts[0].endswith("python3") else None
    except Exception:
        return None


def ensure_deps(pid: int):
    """Re-exec this script with the server's python if CRDT deps are missing."""
    python = server_python(pid)
    try:
        import httpx_ws, jupyter_ydoc, pycrdt  # noqa: F401
    except ImportError:
        if not python:
            raise SystemExit(
                "CRDT deps missing and server python not found.\n"
                "Install into the server's venv: uv pip install --python <venv-python> "
                "jupyter-collaboration httpx-ws, then restart jupyter-lab."
            )
        r = subprocess.run([python, "-c", "import httpx_ws, jupyter_ydoc, pycrdt"],
                           capture_output=True)
        if r.returncode == 0:
            os.execv(python, [python, __file__, *sys.argv[1:]])
        raise SystemExit(
            f"jupyter-collaboration/httpx-ws not installed in {python}.\n"
            f"Fix: uv pip install --python {python} jupyter-collaboration httpx-ws, "
            "then restart jupyter-lab."
        )


def http_json(method: str, url: str, token: str, body=None, what=""):
    import urllib.error
    req = urllib.request.Request(
        url, data=json.dumps(body).encode() if body is not None else None, method=method,
        headers={"Authorization": f"token {token}", "Content-Type": "application/json"})
    try:
        return json.load(urllib.request.urlopen(req, timeout=10))
    except urllib.error.HTTPError as e:
        if e.code == 404 and what:
            raise SystemExit(
                f"{what} not found on the server ({url}). "
                "The file may have been renamed, moved, or deleted — run "
                f"`{sys.argv[0]} <notebook.ipynb> list` with the correct path, or check "
                "the notebook's current location in the JupyterLab file browser.")
        raise


async def run_kernel(base, token, nb_name, code, timeout):
    """Execute code in the notebook's kernel; return (outputs, execution_count).

    Uses jupyter_client directly against the kernel's connection file — no
    websocket protocol hand-rolling.
    """
    import asyncio
    import threading
    import time
    from jupyter_client import find_connection_file
    from jupyter_client import BlockingKernelClient

    sessions = http_json("GET", f"{base}/api/sessions", token)
    kernel = next((s["kernel"] for s in sessions if s["path"] == nb_name), None)
    if kernel is None:
        kernels = http_json("GET", f"{base}/api/kernels", token)
        if len(kernels) == 1:
            print(f"note: no session for {nb_name} (notebook closed or renamed) — "
                  "using the server's only kernel", file=sys.stderr)
            kernel = kernels[0]
    if kernel is None:
        raise SystemExit(f"No kernel session for {nb_name} — open the notebook first.")
    cf = find_connection_file(kernel["id"])

    outputs: list = []
    done = threading.Event()

    def hook(msg):
        t = msg["header"]["msg_type"]
        c = msg.get("content", {})
        if t == "stream":
            outputs.append({"output_type": "stream", "name": c.get("name", "stdout"),
                            "text": c.get("text", "")})
        elif t in ("execute_result", "display_data"):
            outputs.append({"output_type": t, "data": c.get("data", {}), "metadata": {}})
        elif t == "error":
            outputs.append({"output_type": "error", "ename": c["ename"],
                            "evalue": c["evalue"], "traceback": c["traceback"]})
        elif t == "status" and c.get("execution_state") == "idle":
            done.set()

    def execute():
        kc = BlockingKernelClient()
        kc.load_connection_file(cf)
        kc.start_channels()
        try:
            try:
                kc.wait_for_ready(timeout=15)
            except RuntimeError:
                print("warning: kernel busy or unresponsive — request queued, "
                      "will run when the current cell finishes", file=sys.stderr)
            msg_id = kc.execute(code)
            deadline = time.monotonic() + timeout
            while not done.is_set():
                if time.monotonic() > deadline:
                    raise TimeoutError(f"cell did not finish in {timeout}s")
                try:
                    msg = kc.get_iopub_msg(timeout=5)
                except Empty:
                    continue  # quiet period (long cell, tool exec, LLM latency)
                if msg.get("parent_header", {}).get("msg_id") != msg_id:
                    continue
                hook(msg)
            # execution_count comes from the shell-channel reply
            reply = kc.get_shell_msg(timeout=10)
            while reply["parent_header"].get("msg_id") != msg_id:
                reply = kc.get_shell_msg(timeout=10)
            if reply["content"].get("status") == "error" and not any(
                    o.get("output_type") == "error" for o in outputs):
                c = reply["content"]
                outputs.append({"output_type": "error", "ename": c.get("ename", ""),
                                "evalue": c.get("evalue", ""),
                                "traceback": c.get("traceback", [])})
            return outputs, reply["content"].get("execution_count")
        finally:
            kc.stop_channels()

    return await asyncio.to_thread(execute)


def trunc(s, n):
    return s[:n] + ("..." if len(s) > n else "")


STAMP_KEY = "agent_seen"


def cell_hash(c):
    src = "".join(c["source"]) if isinstance(c["source"], list) else c["source"]
    import hashlib
    return hashlib.sha1(src.encode()).hexdigest()


def find_index_by_id(ynb, cell_id):
    """Resolve a cell id to its current index (linear scan; notebook-sized)."""
    for i in range(len(ynb.ycells)):
        if ynb.get_cell(i).get("id") == cell_id:
            return i
    return None


def view_state(ynb, i):
    """One of 'fresh' (stamp matches), 'edited' (stamp stale), 'new' (never stamped)."""
    ycell = ynb.ycells[i]
    stamp = dict(ycell["metadata"]).get(STAMP_KEY, {}).get("hash")
    if stamp is None:
        return "new", stamp
    return ("fresh" if stamp == cell_hash(ynb.get_cell(i)) else "edited"), stamp


def stamp_cell(ynb, i):
    ynb.ycells[i]["metadata"][STAMP_KEY] = {"hash": cell_hash(ynb.get_cell(i))}


def cell_data(ynb, i):
    cell = ynb.get_cell(i)
    source = "".join(cell["source"]) if isinstance(cell["source"], list) else cell["source"]
    outputs = []
    for output in cell.get("outputs", []):
        kind = output.get("output_type")
        if kind == "stream":
            outputs.append({"output_type": kind, "name": output.get("name", "stdout"),
                            "text": trunc(output.get("text", ""), 4000)})
        elif kind in ("execute_result", "display_data"):
            data = output.get("data", {})
            outputs.append({"output_type": kind,
                            "text": trunc(data.get("text/plain", ""), 4000),
                            "mime_types": list(data)})
        elif kind == "error":
            outputs.append({"output_type": kind, "ename": output.get("ename", ""),
                            "evalue": trunc(output.get("evalue", ""), 500),
                            "traceback": (output.get("traceback") or [])[-8:]})
    return {"index": i, "id": cell.get("id"), "cell_type": cell["cell_type"],
            "source": source, "source_hash": cell_hash(cell),
            "execution_count": cell.get("execution_count"), "outputs": outputs}


def render_projection(ynb):
    """Whole-notebook percent-format projection; outputs as '#| ' comment lines.

    Each cell header carries the cell's stable id (the nbformat cell id, which
    every tool takes as its address) and, for code cells, the kernel
    execution number.
    """
    lines = []
    for i in range(len(ynb.ycells)):
        c = ynb.get_cell(i)
        src = "".join(c["source"]) if isinstance(c["source"], list) else c["source"]
        if c["cell_type"] == "markdown":
            src = "\n".join(f"# {ln}" for ln in src.splitlines())
        else:
            src = trunc(src, 4000)
        marker = f"# %% {c['cell_type']} id={c.get('id') or 'unset'}"
        if c["cell_type"] == "code":
            marker += f" exec={c.get('execution_count') or 'None'}"
        lines += [marker, src]
        for o in c.get("outputs") or []:
            t = o.get("output_type")
            if t == "stream":
                body = trunc(o.get("text", ""), 2000).strip() or "(empty stream)"
                lines += [f"#| → {ln}" for ln in body.splitlines()]
            elif t == "execute_result":
                body = trunc(o.get("data", {}).get("text/plain", ""), 2000).strip()
                lines += [f"#| = {ln}" for ln in body.splitlines() or ["(empty result)"]]
            elif t == "error":
                lines.append(f"#| ERR {o.get('ename')}: {trunc(str(o.get('evalue', '')), 200)}")
            elif t == "display_data":
                lines.append(f"#| [display: {','.join(o.get('data', {}).keys())}]")
        lines.append("")
    return "\n".join(lines)


def search_hits(ynb, cell, i, rx, before=3, after=3):
    """Regex over a cell's FULL source (no truncation); each hit carries the
    cell's address/id plus surrounding context lines so the caller can see the
    neighborhood without touching the cell."""
    src = "".join(cell["source"]) if isinstance(cell["source"], list) else cell["source"]
    lines = src.split("\n")
    hits = []
    for n, line in enumerate(lines, start=1):
        for m in rx.finditer(line):
            lo, hi = max(1, n - before), min(len(lines), n + after)
            context = "\n".join(f"{k}: {lines[k - 1]}" for k in range(lo, hi + 1))
            hits.append({"index": i, "id": cell.get("id"), "cell_type": cell["cell_type"],
                         "execution_count": cell.get("execution_count"),
                         "cell_line": n, "text": trunc(line, 2000),
                         "context": trunc(context, 4000),
                         "match_start": m.start(), "match_end": m.end()})
    return hits


def rpc_response(**result):
    print(json.dumps({"ok": True, **result}))


def rpc_error(message):
    print(json.dumps({"ok": False, "error": str(message)}))


def rpc_target(ynb, request):
    """Resolve the target cell from a cell_id (the address) and apply the
    content-freshness guards. Returns (index, cell, error): a stale position is
    not a conflict — the id resolves wherever the cell now lives — but a stale
    source hash, or an unread cell, is real and must be refused."""
    cell_id = request.get("cell_id")
    i = find_index_by_id(ynb, cell_id) if cell_id else None
    if i is None:
        return None, None, (
            f"No cell with id {cell_id!r} in this notebook — re-read it "
            "(`read NOTEBOOK.ipynb`) to get current cell ids.")
    cell = ynb.get_cell(i)
    if not request.get("expected_hash"):
        return None, None, (
            f"Cell id={cell_id} has not been read in this session; read it "
            "before writing or running.")
    if cell_hash(cell) != request["expected_hash"]:
        return None, None, (
            f"Cell id={cell_id} changed since it was read; read it again "
            "before retrying.")
    if view_state(ynb, i)[0] == "edited":
        return None, None, (
            f"Cell id={cell_id} was edited since it was last viewed — "
            "read NOTEBOOK.ipynb:" + cell_id + ", then retry.")
    return i, cell, None


async def do_run(ynb, request, base, token, nb_name):
    """Run one cell in the kernel and write the outputs into the shared doc."""
    idx = request["index"]
    cell = ynb.get_cell(idx)
    if cell["cell_type"] != "code":
        raise SystemExit(f"cell {idx} is {cell['cell_type']}, only code cells run")
    src = "".join(cell["source"]) if isinstance(cell["source"], list) else cell["source"]
    outputs, exec_count = await run_kernel(base, token, nb_name, src,
                                           request.get("timeout") or 60)
    ynb.set_cell(idx, {**cell, "outputs": outputs, "execution_count": exec_count})
    await asyncio.sleep(2.5)  # flush outputs to the room; server autosaves
    return outputs, exec_count


async def yjson(ynb, request, base, token, nb_name):
    op = request.get("op")
    if op == "snapshot":
        cells = [cell_data(ynb, i) for i in range(len(ynb.ycells))]
        for i in range(len(ynb.ycells)):
            stamp_cell(ynb, i)
        await asyncio.sleep(1)
        rpc_response(cells=cells, text=render_projection(ynb))
        return
    if op == "read":
        i = None
        cell_id = request.get("cell_id")
        if cell_id:
            i = find_index_by_id(ynb, cell_id)
            if i is None:
                rpc_error(f"No cell with id {cell_id!r} in this notebook — re-read it to get current cell ids.")
                return
        else:
            i = request.get("index")
            if not isinstance(i, int) or not 0 <= i < len(ynb.ycells):
                rpc_error(f"Invalid cell index {i!r}; pass a cell id (`cell_id`) or a numeric cell index within {len(ynb.ycells)}.")
                return
        stamp_cell(ynb, i)
        await asyncio.sleep(1)
        data = cell_data(ynb, i)
        # source_line pages into long cells: return numbered lines [start, start+limit)
        source_line = request.get("source_line")
        if isinstance(source_line, int) and source_line > 0:
            full = data["source"].split("\n")
            start = max(1, source_line)
            limit = request.get("source_limit") or 150
            window = full[start - 1:start - 1 + limit]
            numbered = "\n".join(f"{k}: {v}" for k, v in
                                 zip(range(start, start + len(window)), window))
            data = {**data, "source": numbered, "source_offset": start,
                    "total_source_lines": len(full), "outputs": []}
        rpc_response(cell=data)
        return

    if op == "search":
        pattern = request.get("pattern")
        if not isinstance(pattern, str) or not pattern:
            rpc_error("search needs a non-empty pattern")
            return
        scope = None
        cell_filter = request.get("cell_id")
        if cell_filter:
            fi = find_index_by_id(ynb, cell_filter)
            if fi is None:
                rpc_error(f"No cell with id {cell_filter!r} in this notebook — re-read it to get current cell ids.")
                return
            scope = [fi]
        try:
            source = re.escape(pattern) if request.get("literal") else pattern
            flags = 0 if request.get("case_sensitive") else re.IGNORECASE
            rx = re.compile(source, re.MULTILINE | flags)
        except re.error as exc:
            rpc_error(f"Invalid regex pattern: {exc}")
            return
        cells_scope = scope if scope is not None else range(len(ynb.ycells))
        hits = []
        for i in cells_scope:
            hits.extend(search_hits(ynb, ynb.get_cell(i), i, rx))
        rpc_response(hits=hits[:500], hit_cap=len(hits) > 500)
        return

    if op in ("write", "edit", "run", "delete"):
        i, cell, error = rpc_target(ynb, request)
        if error:
            rpc_error(error)
            return
        if op == "run":
            _, execution_count = await do_run(
                ynb, {"index": i, "timeout": request.get("timeout")},
                base, token, nb_name)
            rpc_response(index=i, execution_count=execution_count,
                         outputs=cell_data(ynb, i)["outputs"])
            return
        if op == "delete":
            ynb.ycells.pop(i)
        else:
            source = request.get("source")
            if op == "edit":
                old = request.get("old_string")
                new = request.get("new_string")
                if not isinstance(old, str) or not old:
                    rpc_error("edit needs a non-empty old_string")
                    return
                count = cell["source"].count(old)
                replace_all = request.get("replace_all") is True
                if count == 0 or (count != 1 and not replace_all):
                    qualifier = "at least once" if replace_all else "exactly once"
                    rpc_error(f"old_string matched {count} times in cell id={request.get('cell_id')}; it must match {qualifier}")
                    return
                source = cell["source"].replace(old, new if isinstance(new, str) else "",
                                                -1 if replace_all else 1)
            if not isinstance(source, str):
                rpc_error(f"{op} needs source text")
                return
            updated = {"cell_type": cell["cell_type"], "source": source,
                       "metadata": cell.get("metadata", {}), "id": cell.get("id")}
            if cell["cell_type"] == "code":
                updated["outputs"] = cell.get("outputs", [])
                updated["execution_count"] = cell.get("execution_count")
            ynb.set_cell(i, updated)
            stamp_cell(ynb, i)
        await asyncio.sleep(2.5)
        rpc_response(index=i, cell=cell_data(ynb, i) if op != "delete" else None)
        return

    if op == "add":
        source = request.get("source", "")
        cell_type = request.get("cell_type", "code")
        if cell_type not in ("code", "markdown") or not isinstance(source, str):
            rpc_error("add needs source text and cell_type 'code' or 'markdown'")
            return
        cell = {"cell_type": cell_type, "source": source, "metadata": {}}
        if cell_type == "code":
            cell.update(execution_count=None, outputs=[])
        after_id = request.get("after_id")
        index = request.get("index")
        if after_id is not None:
            fi = find_index_by_id(ynb, after_id)
            if fi is None:
                rpc_error(f"No cell with id {after_id!r} in this notebook — re-read it to get current cell ids.")
                return
            index = fi + 1
            ynb.ycells.insert(index, ynb.create_ycell(cell))
        elif index is None:
            ynb.append_cell(cell)
            index = len(ynb.ycells) - 1
        elif isinstance(index, int) and 0 <= index <= len(ynb.ycells):
            if request.get("expected_count") != len(ynb.ycells):
                rpc_error("Notebook cells shifted since the last view; read the notebook again before inserting.")
                return
            before = ynb.get_cell(index - 1).get("id") if index else None
            after = ynb.get_cell(index).get("id") if index < len(ynb.ycells) else None
            if before != request.get("before_id") or after != request.get("after_id"):
                rpc_error("Cells around this insertion point moved; read the notebook again before inserting.")
                return
            ynb.ycells.insert(index, ynb.create_ycell(cell))
        else:
            rpc_error(f"Invalid insertion index {index!r}; notebook has {len(ynb.ycells)} cells.")
            return
        stamp_cell(ynb, index)
        await asyncio.sleep(2.5)
        rpc_response(index=index, cell=cell_data(ynb, index))
        return

    rpc_error(f"Unknown jupyter-pair operation: {op!r}")


async def yedit(nb_path: Path, args):
    from httpx_ws import aconnect_ws
    from pycrdt import Doc, Provider
    from pycrdt.websocket.websocket import HttpxWebsocket
    from jupyter_ydoc import ydocs

    base, token, _, server_root = find_server(nb_path)
    relative_path = nb_path.relative_to(server_root).as_posix()
    session_path = quote(relative_path, safe="/")
    session = http_json("PUT", f"{base}/api/collaboration/session/{session_path}", token,
                        {"format": "json", "type": "notebook"},
                        what=f"Notebook '{nb_path.name}'")
    room_id = f"{session['format']}:{session['type']}:{session['fileId']}"
    url = (f"{base.replace('http', 'ws', 1)}/api/collaboration/room/{room_id}"
           f"?sessionId={session['sessionId']}&token={token}")

    async with aconnect_ws(url, headers={"Authorization": f"token {token}"}) as ws:
        ydoc = Doc()
        provider = Provider(ydoc, HttpxWebsocket(ws, room_id))
        async with provider:
            ynb = ydocs["notebook"](ydoc)
            # wait for the server to sync document state (stable cell count)
            last = -1
            for _ in range(40):
                await asyncio.sleep(0.25)
                if len(ynb.ycells) == last and last >= 0:
                    break
                last = len(ynb.ycells)
            await yjson(ynb, args, base, token, nb_path.name)


def main():
    argv = sys.argv[1:]
    if len(argv) != 2 or argv[1] == "-h" or argv[1] == "--help":
        raise SystemExit(USAGE)
    nb_path = Path(argv[0]).expanduser().resolve()
    if not nb_path.exists():
        suggest_notebook(nb_path)

    try:
        base, token, pid, _ = find_server(nb_path)
        ensure_deps(pid)
        args = json.load(sys.stdin)
    except (json.JSONDecodeError, SystemExit) as e:
        rpc_error(getattr(e, "code", None) or f"invalid RPC request: {e}")
        sys.exit(1)

    try:
        asyncio.run(yedit(nb_path, args))
    except SystemExit as e:
        rpc_error(e.code or "request failed")
        sys.exit(1)
    except BaseExceptionGroup as eg:
        # anyio wraps errors raised inside the CRDT task group; surface the
        # descriptive message instead of a wall of traceback
        def leaves(group):
            for exc in group.exceptions:
                if isinstance(exc, BaseExceptionGroup):
                    yield from leaves(exc)
                else:
                    yield exc
        exits = [e for e in leaves(eg) if isinstance(e, SystemExit)]
        if exits:
            rpc_error(exits[0].code or "request failed")
            sys.exit(1)
        raise


if __name__ == "__main__":
    main()
