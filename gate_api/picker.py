"""Native file pickers.

Files dragged into a browser carry content but no path, while the version gate needs the real
location in the work pool, so a "server pops up a native dialog" route is kept. tkinter must run
on the main thread and the HTTP handler runs on a worker thread, so this module is invoked as
a separate process.

    python -m gate_api.picker files  [title]
    python -m gate_api.picker folder [title] [initial folder]

The result is one line of JSON: {"paths": [...]} or {"path": "..."}. Empty if cancelled.
"""
from __future__ import annotations

import json
import sys
import tkinter as tk
from pathlib import Path
from tkinter import filedialog


def _root() -> tk.Tk:
    root = tk.Tk()
    root.withdraw()
    # After withdraw the window is not mapped to the screen, so topmost only takes effect
    # after one update, and the dialog must hang off this root (parent=root) to inherit
    # topmost -- otherwise it sits behind the browser window and looks like "clicking did nothing".
    root.attributes("-topmost", True)
    root.update()
    return root


def main(argv: list[str]) -> int:
    mode = argv[0] if argv else "files"
    title = argv[1] if len(argv) > 1 else "Select"
    initial = argv[2] if len(argv) > 2 else ""
    root = _root()
    try:
        # On Windows tkinter always returns forward-slash paths (C:/Users/you/WorkLibrary/...).
        # Normalize to the native form before handing them on: downstream code that takes the
        # folder name by splitting on \ finds no separator at all in a forward-slash path and
        # ends up using the whole path as the name.
        if mode == "folder":
            path = filedialog.askdirectory(title=title, initialdir=initial or None, parent=root)
            result = {"path": str(Path(path)) if path else ""}
        else:
            paths = filedialog.askopenfilenames(title=title, initialdir=initial or None, parent=root)
            result = {"paths": [str(Path(item)) for item in paths]}
    finally:
        root.destroy()
    sys.stdout.buffer.write(json.dumps(result, ensure_ascii=False).encode("utf-8"))
    sys.stdout.buffer.write(b"\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
