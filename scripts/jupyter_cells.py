#!/usr/bin/env python3
"""Edit a live Jupyter notebook cell-by-cell through the Yjs collaboration room.

Cells appear immediately in any open JupyterLab tab — no "changed on disk"
dialog, no reload. Requires jupyter-collaboration on the Jupyter server.

usage: jupyter_cells.py NOTEBOOK.ipynb ACTION [args]
actions:
  list                        show index / type / first line of every cell
  read INDEX                  print full source of one cell
  add [--type code|markdown] [--index N] [--source TEXT|@file|-] [--run] [--timeout S]
  run INDEX [--timeout S]       execute the cell in the kernel, write outputs live
  exec [--source TEXT|@file|-] [--timeout S]
                                run arbitrary code in the kernel (state inspection;
                                prints stdout/result, does not touch any cell)
  edit INDEX [--source TEXT|@file|-]        replaces the cell (code: clears outputs)
  delete INDEX
source defaults to '-' (stdin) if stdin is piped, else required.
"""
import asyncio
import json
import os
import subprocess
import sys
import urllib.request
from pathlib import Path

RUNTIME_DIR = Path(
    os.environ.get("JUPYTER_RUNTIME_DIR", Path.home() / "Library/Jupyter/runtime")
)

USAGE = __doc__


def find_server(nb_path: Path):
    """Return (base_url, token, pid) for the running server serving nb_path."""
    servers = []
    for f in sorted(RUNTIME_DIR.glob("jpserver-*.json"), key=lambda p: p.stat().st_mtime, reverse=True):
        try:
            info = json.loads(f.read_text())
        except Exception:
            continue
        if "token" not in info:
            continue
        servers.append(info)
        if str(nb_path).startswith(os.path.realpath(info["root_dir"]) + os.sep):
            return info["url"].rstrip("/"), info["token"], info["pid"]
    raise SystemExit(
        f"No running jupyter server serves {nb_path}.\n"
        f"Servers found: {[(s['root_dir'], s['port']) for s in servers]}"
    )


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


def http_json(method: str, url: str, token: str, body=None):
    req = urllib.request.Request(
        url, data=json.dumps(body).encode() if body is not None else None, method=method,
        headers={"Authorization": f"token {token}", "Content-Type": "application/json"})
    return json.load(urllib.request.urlopen(req, timeout=10))


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
                      "will run when the current cell finishes")
            msg_id = kc.execute(code)
            deadline = time.monotonic() + timeout
            while not done.is_set():
                msg = kc.get_iopub_msg(timeout=2)
                if msg.get("parent_header", {}).get("msg_id") != msg_id:
                    continue
                hook(msg)
                if time.monotonic() > deadline:
                    raise TimeoutError(f"cell did not finish in {timeout}s")
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


def describe_cell(c):
    src = "".join(c["source"]) if isinstance(c["source"], list) else c["source"]
    first = src.strip().splitlines()
    return f"{c['cell_type']:8} {first[0][:70] if first else ''}"


STAMP_KEY = "agent_seen"
GUARD_MSG = ("cell {i} was edited since it was last viewed — view it first: "
             "run `read {i}`, then retry")


def cell_hash(c):
    src = "".join(c["source"]) if isinstance(c["source"], list) else c["source"]
    import hashlib
    return hashlib.sha1(src.encode()).hexdigest()


def view_state(ynb, i):
    """One of 'fresh' (stamp matches), 'edited' (stamp stale), 'new' (never stamped)."""
    ycell = ynb.ycells[i]
    stamp = dict(ycell["metadata"]).get(STAMP_KEY, {}).get("hash")
    if stamp is None:
        return "new", stamp
    return ("fresh" if stamp == cell_hash(ynb.get_cell(i)) else "edited"), stamp


def stamp_cell(ynb, i):
    ynb.ycells[i]["metadata"][STAMP_KEY] = {"hash": cell_hash(ynb.get_cell(i))}


def check_view(ynb, i):
    """Guard: refuse to mutate a cell the agent hasn't freshly viewed."""
    state, _ = view_state(ynb, i)
    if state != "fresh":
        print(GUARD_MSG.format(i=i))
        return False
    return True


def get_source(arg: str | None) -> str:
    if arg is None or arg == "-":
        return sys.stdin.read()
    if arg.startswith("@"):
        return Path(arg[1:]).read_text()
    return arg


def parse_flags(rest):
    """Parse an optional leading INDEX plus --type/--index/--source/--run/--timeout flags."""
    args = {}
    if rest and not rest[0].startswith("--"):
        args["index"] = int(rest[0])
        rest = rest[1:]
    it = iter(rest)
    for flag in it:
        if not flag.startswith("--"):
            raise SystemExit(f"unexpected argument: {flag}")
        key = flag[2:].replace("-", "_")
        if key in ("run",):
            args[key] = True
        else:
            args[key] = next(it, None)
    if "index" in args:
        args["index"] = int(args["index"])
    if "timeout" in args:
        args["timeout"] = float(args["timeout"])
    return args


async def do_run(ynb, args, base, token, nb_name):
    """Run one cell in the kernel and write the outputs into the shared doc."""
    idx = args["index"]
    cell = ynb.get_cell(idx)
    if cell["cell_type"] != "code":
        raise SystemExit(f"cell {idx} is {cell['cell_type']}, only code cells run")
    src = "".join(cell["source"]) if isinstance(cell["source"], list) else cell["source"]
    outputs, exec_count = await run_kernel(base, token, nb_name, src,
                                           args.get("timeout") or 60)
    ynb.set_cell(idx, {**cell, "outputs": outputs, "execution_count": exec_count})
    await asyncio.sleep(2.5)  # flush outputs to the room; server autosaves
    kinds = [o.get("output_type") for o in outputs]
    print(f"ran cell {idx} [{exec_count}]: {kinds}")


async def yedit(nb_path: Path, action, args):
    from httpx_ws import aconnect_ws
    from pycrdt import Doc, Provider
    from pycrdt.websocket.websocket import HttpxWebsocket
    from jupyter_ydoc import ydocs

    base, token, _ = find_server(nb_path)
    session = http_json("PUT", f"{base}/api/collaboration/session/{nb_path.name}", token,
                        {"format": "json", "type": "notebook"})
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
            n_before = len(ynb.ycells)

            if action == "list":
                for i in range(n_before):
                    state, _ = view_state(ynb, i)
                    mark = {"fresh": "", "edited": "  *edited*", "new": "  *new*"}[state]
                    print(f"{i:3} {describe_cell(ynb.get_cell(i))}{mark}")
                return

            if action == "read":
                c = ynb.get_cell(args["index"])
                print("".join(c["source"]) if isinstance(c["source"], list) else c["source"])
                stamp_cell(ynb, args["index"])  # viewing refreshes the stamp
                await asyncio.sleep(1)
                return

            if action == "add":
                src = get_source(args.get("source"))
                cell = {"cell_type": args.get("type", "code"), "source": src, "metadata": {}}
                if cell["cell_type"] == "code":
                    cell["execution_count"] = None
                    cell["outputs"] = []
                if args.get("index") is None:
                    ynb.append_cell(cell)
                    idx = len(ynb.ycells) - 1
                else:
                    idx = args["index"]
                    ynb.ycells.insert(idx, ynb.create_ycell(cell))
                stamp_cell(ynb, idx)  # agent authored it; fresh view
            elif action == "run":
                if not check_view(ynb, args["index"]):
                    return
                await do_run(ynb, args, base, token, nb_path.name)
                return
            elif action == "exec":
                src = get_source(args.get("source"))
                outputs, ec = await run_kernel(base, token, nb_path.name, src,
                                               args.get("timeout") or 60)
                for o in outputs:
                    if o["output_type"] == "stream":
                        print(o["text"], end="")
                    elif o["output_type"] == "execute_result" and "text/plain" in o["data"]:
                        print(o["data"]["text/plain"], end="")
                    elif o["output_type"] == "error":
                        print(f"{o['ename']}: {o['evalue']}")
                return
            elif action == "edit":
                if not check_view(ynb, args["index"]):
                    return
                old = ynb.get_cell(args["index"])
                cell = {"cell_type": old["cell_type"], "source": get_source(args.get("source")),
                        "metadata": old.get("metadata", {})}
                if old["cell_type"] == "code":  # preserve outputs across edits
                    cell["outputs"] = old.get("outputs", [])
                    cell["execution_count"] = old.get("execution_count")
                ynb.set_cell(args["index"], cell)
                stamp_cell(ynb, args["index"])
            elif action == "delete":
                ynb.ycells.pop(args["index"])
            else:
                raise SystemExit(f"unknown action {action!r}")

            await asyncio.sleep(2.5)  # flush CRDT updates; server autosaves
            print(f"live doc: {n_before} -> {len(ynb.ycells)} cells")

            if args.get("run") and action == "add":
                idx = len(ynb.ycells) - 1 if args.get("index") is None else args["index"]
                await do_run(ynb, {"index": idx, "timeout": args.get("timeout")},
                             base, token, nb_path.name)


def main():
    argv = sys.argv[1:]
    if len(argv) < 2 or argv[1] in ("-h", "--help"):
        raise SystemExit(USAGE)
    nb_path = Path(argv[0]).expanduser().resolve()
    action, rest = argv[1], argv[2:]

    base, token, pid = find_server(nb_path)
    ensure_deps(pid)

    args = parse_flags(rest)

    asyncio.run(yedit(nb_path, action, args))

    # completion check: server has persisted the shared doc to disk
    disk = http_json("GET", f"{base}/api/contents/{nb_path.name}", token)["content"]
    print(f"disk: {len(disk['cells'])} cells")


if __name__ == "__main__":
    main()
