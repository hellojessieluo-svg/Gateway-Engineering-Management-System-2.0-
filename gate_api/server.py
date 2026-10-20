"""Gateway's local service: the web page is the face, this is the hands.

The browser cannot touch the disk and does not need to -- every copy, rename, archive and
ledger write happens in this process; the page only displays and sends commands. The commands
are exactly those of gate_api.cli, just over a different transport.

    python -m gate_api.server            # start the service and open the browser
    python -m gate_api.server --no-open  # start the service only

Listens on 127.0.0.1 only and uses a token to keep other local programs out (the token is
stored in %LOCALAPPDATA%/Gateway/token.txt and reused across restarts).
The launcher pins the port to 8790 by default, see launch_gate_ui.cmd.
"""
from __future__ import annotations

import json
import mimetypes
import os
import secrets
import subprocess
import sys
import threading
import urllib.request
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from common.config import config_path

from .cli import COMMANDS


ROOT = Path(__file__).resolve().parents[1]
UI_DIR = ROOT / "ui"
# Who is running, where, and with which token. Kept in the user's own folder, unreadable to other accounts.
STATE_PATH = config_path().parent / "server.json"
# The token is persisted and reused so a bookmarked URL keeps working across restarts.
# It only keeps other local programs out, so keeping it fixed costs no security. Delete this file to get a new token.
TOKEN_PATH = STATE_PATH.with_name("token.txt")


def _load_or_create_token() -> str:
    try:
        token = TOKEN_PATH.read_text(encoding="utf-8").strip()
        if len(token) >= 16:
            return token
    except OSError:
        pass
    token = secrets.token_urlsafe(24)
    TOKEN_PATH.parent.mkdir(parents=True, exist_ok=True)
    TOKEN_PATH.write_text(token, encoding="utf-8")
    return token


TOKEN = _load_or_create_token()
SERVER: ThreadingHTTPServer | None = None


def _say(message: str) -> None:
    """Talk if there is a console, stay quiet otherwise.

    Started in the background (pythonw), sys.stdout is None and print raises on the spot;
    with stdout piped, non-ASCII text can hit the local ANSI code page. Neither may crash the service.
    """
    if sys.stdout is None:
        return
    try:
        print(message)
    except Exception:
        pass


def _python() -> str:
    return sys.executable


def _pick(mode: str, title: str, initial: str = "") -> dict:
    """Pop up a native picker. Runs in a separate process because tkinter only works on the main thread."""
    try:
        out = subprocess.run(
            [_python(), "-m", "gate_api.picker", mode, title, initial],
            cwd=str(ROOT),
            capture_output=True,
            timeout=600,
            env={**os.environ, "PYTHONIOENCODING": "utf-8"},
        )
        text = out.stdout.decode("utf-8").strip()
        return json.loads(text) if text else {}
    except Exception as exc:
        raise RuntimeError(f"Could not open the picker: {exc}") from exc


def cmd_pick_files(payload: dict) -> dict:
    result = _pick("files", payload.get("title") or "Select files", payload.get("initial", ""))
    # Bring the file sizes back too, so the page does not have to show a misleading "0 KB"
    items = []
    for p in result.get("paths") or []:
        path = Path(p)
        try:
            size = path.stat().st_size
        except OSError:
            size = 0
        items.append({"path": str(path), "name": path.name, "size": size})
    result["items"] = items
    return result


def cmd_pick_folder(payload: dict) -> dict:
    return _pick("folder", payload.get("title") or "Select folder", payload.get("initial", ""))


def cmd_open_path(payload: dict) -> dict:
    """Open a folder in Explorer -- the page cannot, so this side does it."""
    target = Path(payload.get("path", ""))
    if not target.exists():
        raise ValueError(f"Path does not exist: {target}")
    os.startfile(str(target))  # type: ignore[attr-defined]
    return {"opened": str(target)}


def cmd_list_folder(payload: dict) -> dict:
    """List the files in a folder, so "Add whole folder" can keep the hierarchy."""
    root = Path(payload.get("path", ""))
    if not root.is_dir():
        raise ValueError(f"Not a folder: {root}")
    items = []
    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        try:
            size = path.stat().st_size
        except OSError:
            continue
        items.append(
            {
                "path": str(path),
                "name": path.name,
                "size": size,
                "relative_name": str(path.relative_to(root)).replace("\\", "/"),
            }
        )
    return {"root": str(root), "items": items}


def cmd_shutdown(payload: dict) -> dict:
    """Stop the service from the page. Running in the background there is no window to close; this is that "close window"."""
    if SERVER is None:
        raise RuntimeError("The service is not running.")
    # Must be another thread: calling shutdown() on the request-handling thread waits on itself and hangs.
    threading.Thread(target=SERVER.shutdown, daemon=True).start()
    return {"stopped": True}


# Commands only the web page uses; everything else reuses the CLI set
EXTRA_COMMANDS = {
    "pick-files": cmd_pick_files,
    "pick-folder": cmd_pick_folder,
    "open-path": cmd_open_path,
    "list-folder": cmd_list_folder,
    "shutdown": cmd_shutdown,
}
ALL_COMMANDS = {**COMMANDS, **EXTRA_COMMANDS}


class Handler(BaseHTTPRequestHandler):
    server_version = "GateUI"

    def log_message(self, fmt: str, *args) -> None:  # keep the console quiet
        pass

    # ------------------------------------------------------------ basics
    def _send(self, code: int, body: bytes, content_type: str) -> None:
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _json(self, payload: dict, code: int = 200) -> None:
        self._send(code, json.dumps(payload, ensure_ascii=False).encode("utf-8"), "application/json; charset=utf-8")

    def _authorized(self) -> bool:
        # Local machine + token only, so other programs on the same machine can't call in
        if self.headers.get("X-Gate-Token") == TOKEN:
            return True
        query = parse_qs(urlparse(self.path).query)
        return query.get("t", [""])[0] == TOKEN

    # ------------------------------------------------------------ GET
    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        route = parsed.path
        if route in ("/", "/index.html"):
            if not self._authorized():
                self._send(403, "Wrong token. Please open this page from the launcher.".encode("utf-8"), "text/plain; charset=utf-8")
                return
            page = (UI_DIR / "index.html").read_bytes()
            self._send(200, page, "text/html; charset=utf-8")
            return
        if route == "/alive":
            # The launcher uses this to tell whether one is already running. It needs the token,
            # so only whoever can read the state file (that is, the owner) can ask.
            self._json({"alive": True} if self._authorized() else {"alive": False},
                       200 if self._authorized() else 403)
            return
        # Static assets (icons and such)
        candidate = (UI_DIR / route.lstrip("/")).resolve()
        if candidate.is_file() and str(candidate).startswith(str(UI_DIR.resolve())):
            ctype = mimetypes.guess_type(str(candidate))[0] or "application/octet-stream"
            self._send(200, candidate.read_bytes(), ctype)
            return
        self._send(404, b"not found", "text/plain")

    # ------------------------------------------------------------ POST
    def do_POST(self) -> None:
        if not self._authorized():
            self._json({"ok": False, "error": "Wrong token"}, 403)
            return
        route = urlparse(self.path).path
        length = int(self.headers.get("Content-Length") or 0)

        if not route.startswith("/api/"):
            self._json({"ok": False, "error": "Unknown route"}, 404)
            return
        command = route[len("/api/") :]
        if command not in ALL_COMMANDS:
            self._json({"ok": False, "error": f"Unknown command: {command}"}, 404)
            return
        try:
            raw = self.rfile.read(length) if length else b"{}"
            payload = json.loads(raw.decode("utf-8") or "{}")
        except Exception as exc:
            self._json({"ok": False, "error": f"Request is not valid JSON: {exc}"}, 400)
            return
        try:
            self._json({"ok": True, "data": ALL_COMMANDS[command](payload)})
        except Exception as exc:
            self._json({"ok": False, "error": str(exc), "type": type(exc).__name__})


class ExclusiveServer(ThreadingHTTPServer):
    """Only one process may bind the port.

    The standard library turns SO_REUSEADDR on by default, which on Windows lets a second process
    bind the same port: several services stack on one port and only the earliest gets requests
    (seen 2026-09-08). With it off, a second one fails to bind on the spot and lands in server-error.log.
    """

    allow_reuse_address = False


def _already_running() -> str:
    """If one is already running, return its address.

    Running in the background has no window, so a repeated double-click is invisible -- without
    this check a second service would quietly start and two processes would write the same ledger.
    """
    try:
        state = json.loads(STATE_PATH.read_text(encoding="utf-8"))
        request = urllib.request.Request(
            f"http://127.0.0.1:{state['port']}/alive",
            headers={"X-Gate-Token": state["token"]},
        )
        with urllib.request.urlopen(request, timeout=1.5) as response:
            if json.loads(response.read().decode("utf-8")).get("alive"):
                return str(state["url"])
    except Exception:
        return ""
    return ""


def serve(open_browser: bool = True, port: int = 0) -> None:
    global SERVER
    running = _already_running()
    if running:
        _say(f"Gateway is already running: {running}")
        if open_browser:
            webbrowser.open(running)
        return

    try:
        httpd = ExclusiveServer(("127.0.0.1", port), Handler)
    except OSError as exc:
        # The port is taken by another program (or the previous instance has not fully exited) -- better not to start than to stack a second one
        raise RuntimeError(
            f"Could not bind port {port or '(random)'}: {exc}. "
            f"Most likely another program holds it (see netstat -ano | findstr :{port or ''}), "
            "or a previous Gateway has not fully exited."
        ) from exc
    SERVER = httpd
    actual = httpd.server_address[1]
    url = f"http://127.0.0.1:{actual}/?t={TOKEN}"
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    STATE_PATH.write_text(
        json.dumps({"port": actual, "token": TOKEN, "url": url}, ensure_ascii=False),
        encoding="utf-8",
    )
    _say(f"Gateway service started: {url}")
    _say("Click \"Quit\" at the top right of the page to stop the service.")
    if open_browser:
        threading.Timer(0.6, lambda: webbrowser.open(url)).start()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        SERVER = None
        httpd.server_close()
        STATE_PATH.unlink(missing_ok=True)


if __name__ == "__main__":
    # The port is optional: without one the OS picks a free port, so it doesn't fight other programs
    ports = [int(a) for a in sys.argv[1:] if a.isdigit()]
    try:
        serve(open_browser="--no-open" not in sys.argv, port=ports[0] if ports else 0)
    except Exception:
        # Started in the background there is no window and nobody would see the exception -- keep a copy, so "double-click did nothing" leaves evidence
        import traceback

        crash = STATE_PATH.with_name("server-error.log")
        try:
            crash.parent.mkdir(parents=True, exist_ok=True)
            crash.write_text(traceback.format_exc(), encoding="utf-8")
        except Exception:
            pass
        raise
