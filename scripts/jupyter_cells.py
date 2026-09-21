#!/usr/bin/env python3
"""Edit a live Jupyter notebook cell-by-cell through the Yjs collaboration room.

Cells appear immediately in any open JupyterLab tab — no "changed on disk"
dialog, no reload. Requires jupyter-collaboration on the Jupyter server.

usage: jupyter_cells.py NOTEBOOK.ipynb ACTION [args]
actions:
  list                        near-full dump: every cell's [exec_count], type,
                              source and outputs, generously truncated ('...').
                              Views all cells (refreshes the seen-stamps).
  read INDEX                  print full source of one cell + outputs (refreshes stamp)
  add [--type code|markdown] [--index N] [--source TEXT|@file|-] [--run] [--timeout S]
  run INDEX [--timeout S]       execute the cell in the kernel, write outputs live
  exec [--source TEXT|@file|-] [--timeout S]
                                run read-only code in the kernel for state
                                inspection (prints stdout/result, touches no
                                cell) — never for side effects, use cells for that
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
    """Return (base_url, token, pid) for the running server serving nb_path."""
    servers = all_servers()
    for info in servers:
        if str(nb_path).startswith(os.path.realpath(info["root_dir"]) + os.sep):
            return info["url"].rstrip("/"), info["token"], info["pid"]
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


def trunc(s, n):
    return s[:n] + ("..." if len(s) > n else "")


def describe_cell(c):
    src = "".join(c["source"]) if isinstance(c["source"], list) else c["source"]
    first = src.strip().splitlines()
    return f"{c['cell_type']:8} {first[0][:70] if first else ''}"


def output_summary(c):
    """Truncated status of a cell's outputs: errored, result, or stream tail."""
    outs = c.get("outputs") or []
    for o in outs:
        if o.get("output_type") == "error":
            return f"  ERR {o.get('ename', '')}: {trunc(str(o.get('evalue', '')), 80)}"
    last = outs[-1] if outs else None
    if not last:
        return ""
    t = last.get("output_type")
    if t == "stream":
        lines = last.get("text", "").strip().splitlines()
        return f"  > {trunc(lines[-1], 70)}" if lines else ""
    if t == "execute_result":
        lines = last.get("data", {}).get("text/plain", "").strip().splitlines()
        return f"  = {trunc(lines[0], 70)}" if lines else ""
    if t == "display_data":
        return f"  [{','.join(last.get('data', {}).keys())}]"
    return f"  [{t}]"


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
        if key in ("run", "brief"):
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
            n_before = len(ynb.ycells)

            if action not in ("list", "exec"):
                i = args.get("index")
                if i is None:
                    raise SystemExit(f"action `{action}` needs a cell index — run "
                                     "`list` first to see indices")
                if i < 0 or i >= n_before:
                    raise SystemExit(
                        f"cell {i} does not exist — the notebook has {n_before} cells "
                        f"(indices 0..{n_before - 1}). Run `list` to see current indices; "
                        "indices shift when cells are inserted or deleted.")

            if action == "list":
                for i in range(n_before):
                    c = ynb.get_cell(i)
                    state, _ = view_state(ynb, i)
                    mark = {"fresh": "", "edited": "  *edited*", "new": "  *new*"}[state]
                    first = ("".join(c["source"]) if isinstance(c["source"], list)
                             else c["source"])
                    ec = (f"[{str(c.get('execution_count') or '-'):>3}]"
                          if c["cell_type"] == "code" else "     ")
                    # full-ish view: whole file, generous truncation
                    hdr = f"{i:3} {ec} {c['cell_type']:8}{mark}".rstrip()
                    print(hdr)
                    src = first if first.strip() else "(empty)"
                    for ln in trunc(src, 600).splitlines() or ["(empty)"]:
                        print(f"     | {ln}")
                    outs = c.get("outputs") or []
                    for o in outs:
                        t = o.get("output_type")
                        if t == "stream":
                            txt = trunc(o.get("text", ""), 400).strip()
                            for ln in txt.splitlines() or ["(empty stream)"]:
                                print(f"     > {ln}")
                        elif t == "execute_result":
                            txt = trunc(o.get("data", {}).get("text/plain", ""), 400).strip()
                            for ln in txt.splitlines() or ["(empty result)"]:
                                print(f"     = {ln}")
                        elif t == "error":
                            tb = o.get("traceback") or []
                            print(f"     ERR {o.get('ename')}: {o.get('evalue')}")
                            for ln in tb[-6:]:
                                print(f"     ERR {ln}")
                        elif t == "display_data":
                            print(f"     [display: {','.join(o.get('data', {}).keys())}]")
                for i in range(n_before):  # a full view refreshes all stamps
                    stamp_cell(ynb, i)
                await asyncio.sleep(1)
                return

            if action == "read":
                c = ynb.get_cell(args["index"])
                print("".join(c["source"]) if isinstance(c["source"], list) else c["source"])
                for o in c.get("outputs") or []:
                    t = o.get("output_type")
                    if t == "stream":
                        print("\n--- stream (stdout) ---\n" + trunc(o["text"], 2000))
                    elif t == "execute_result":
                        print("\n--- result ---\n"
                              + trunc(o.get("data", {}).get("text/plain", ""), 2000))
                    elif t == "error":
                        print(f"\n--- error ---\n{o['ename']}: {o['evalue']}")
                    elif t == "display_data":
                        print(f"\n--- display: {','.join(o.get('data', {}).keys())} ---")
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
    if not nb_path.exists():
        suggest_notebook(nb_path)
    action, rest = argv[1], argv[2:]

    base, token, pid = find_server(nb_path)
    ensure_deps(pid)

    args = parse_flags(rest)

    try:
        asyncio.run(yedit(nb_path, action, args))
    except SystemExit:
        raise
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
            if exits[0].code:
                print(exits[0].code, file=sys.stderr)
            sys.exit(1)
        raise


if __name__ == "__main__":
    main()
