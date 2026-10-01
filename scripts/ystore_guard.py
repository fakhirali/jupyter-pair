#!/usr/bin/env python3
"""Reset the Jupyter YStore when pycrdt changes, then launch jupyter-lab.

`.jupyter_ystore.db` (in the current directory) holds CRDT update history for
live collaboration sessions. It is disposable — the `.ipynb` files are the
source of truth. Updates written by an older pycrdt and replayed by a newer
one can make JupyterLab's Yjs handler throw and silently drop document updates
(missing cell execution numbers, stale cells), so the store must be rebuilt
whenever the installed pycrdt version changes.

Run it with the same python that runs jupyter-lab, instead of jupyter-lab:

    .venv/bin/python scripts/ystore_guard.py [jupyter-lab args...]

The last-seen pycrdt version is recorded in `.pycrdt_version` next to the
store; add both to the .gitignore of the workspace you run the server from.
"""

import os
import shutil
import sys
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path

ystore = Path(".jupyter_ystore.db")
marker = Path(".pycrdt_version")


def guard() -> None:
    """Delete the YStore if pycrdt changed since the last run, then record it."""
    try:
        current = version("pycrdt")
    except PackageNotFoundError:
        sys.exit("pycrdt not installed in this python — run the guard with the "
                 "same python that runs jupyter-lab.")
    last = marker.read_text().strip() if marker.exists() else None
    if current == last:
        return
    if ystore.exists() or last is not None:
        for stale in [ystore, *ystore.parent.glob(ystore.name + "-*")]:
            stale.unlink(missing_ok=True)
        print(f"pycrdt {last or 'unknown'} -> {current}: reset YStore")
    marker.write_text(current + "\n")


def main() -> None:
    guard()
    lab = Path(sys.executable).parent / "jupyter-lab"
    if not lab.exists():
        lab = shutil.which("jupyter-lab")
    if lab is None:
        sys.exit("jupyter-lab not found next to this python or on PATH; "
                 "run the guard, then start jupyter-lab yourself.")
    os.execv(lab, [str(lab), *sys.argv[1:]])


if __name__ == "__main__":
    main()
