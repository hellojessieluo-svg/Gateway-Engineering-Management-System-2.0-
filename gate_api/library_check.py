"""Work library health check: walks the whole library against the table in section 9 of the
library spec, item by item. It only reports; it never touches anything.

The full spec lives in the library at _admin\\library-spec.md (common.config.DEFAULT_SPEC_PATH);
this is its executable version. Every finding carries a room, a location, a problem and a
one-line hint, and the "Spec" page lists them as-is.
"""
from __future__ import annotations

import re
import sqlite3
from datetime import datetime
from pathlib import Path

from common.constants import SELFCHECK_FILENAME, SELFCHECK_SKELETON, STAGES
from common.places import (
    ARCHIVE_FOLDER,
    DRAFT_FOLDER,
    GENERIC_FOLDER,
    INBOX_FOLDER,
    OUTBOX_FOLDER,
    POOL_FOLDER,
    TOOLS_FOLDER,
    WITHDRAWN_FOLDER,
)
from outbound_gate.ledger import Ledger


POSITIONS = {INBOX_FOLDER, POOL_FOLDER, TOOLS_FOLDER, OUTBOX_FOLDER, ARCHIVE_FOLDER}
# Things at the library root that are not project rooms; _drafts\ is where change-notes
# drafts live (2026-09-26) and is not checked inside
INFRA = {GENERIC_FOLDER, "_reference", "_admin", ".tmp", DRAFT_FOLDER}
BATCH = re.compile(r"^\d{4}\.\d{2}\.\d{2}-")
GUIDE = "AGENTS.md"
# Claude Code only reads CLAUDE.md: the file next to the guide is a one-line pointer and does
# not count as a loose file (spec section 2)
GUIDE_POINTER = "CLAUDE.md"
GUIDE_POINTER_TEXT = "@AGENTS.md\n"
GUIDE_FILES = {GUIDE, GUIDE_POINTER}
GUIDE_SECTIONS = ("Project card", "Layout", "Ground rules", "Start here")
JUNK_NAMES = {"plot.log", "acad.err", "thumbs.db"}
JUNK_SUFFIXES = {".tmp", ".log", ".err"}
UNSORTABLE = re.compile(r"(?<![a-z])(final|latest|copy)(?![a-z])|-v\d+", re.IGNORECASE)
TEMPLATE_DIR = Path("_admin") / "templates"
# "Export" writes one health-check result out as markdown here (2026-09-11): for a person to
# read, or to hand whole to an AI to clean up. _admin\ is in INFRA, so it is never taken for a room.
REPORT_DIR = Path("_admin") / "health-checks"
# The library's three templates, all in _admin\templates\, viewable and editable on the
# "Spec" page (spec section 6). The first two are what a new room / new stage guide looks like;
# the third is the skeleton of the change notes -- the task card (intake-brief) and the issue
# page's "Write notes" both start from it. Missing is not broken: common.constants keeps a
# built-in fallback of the skeleton.
HOUSE_TEMPLATE = "project-AGENTS.md"
TEMPLATE_NAMES = (HOUSE_TEMPLATE, "stage-AGENTS.md", SELFCHECK_FILENAME)
BUILTIN_TEMPLATES = {SELFCHECK_FILENAME: SELFCHECK_SKELETON}


def template_path(root: Path, name: str) -> Path:
    """There are only these three templates; the name comes from the UI, so check the list before building a path."""
    if name not in TEMPLATE_NAMES:
        raise ValueError(f"There is no template named '{name}'.")
    return Path(root) / TEMPLATE_DIR / name


def selfcheck_skeleton(root: Path) -> str:
    """Skeleton of the change notes: the library's template first; if missing (or empty), the built-in one."""
    try:
        text = template_path(root, SELFCHECK_FILENAME).read_text(encoding="utf-8")
    except (OSError, ValueError):
        return SELFCHECK_SKELETON
    return text.replace("\r\n", "\n") if text.strip() else SELFCHECK_SKELETON


def write_template(root: Path, name: str, text: str) -> dict:
    """Save a template edited on the "Spec" page; creates the file (and the template folder) if missing."""
    if not str(text).strip():
        raise ValueError("A template cannot be saved empty.")
    path = template_path(root, name)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(str(text).replace("\r\n", "\n"), encoding="utf-8")
    return {"name": name, "path": str(path), "size": path.stat().st_size}


def export_report(root: Path, result: dict, errors_only: bool = False, now: datetime | None = None) -> dict:
    """Write a check_library result as a markdown report: _admin\\health-checks\\health-check-date-time.md.

    One section per room, one table per room (level / location / problem / what to do); locations
    are paths relative to the library. errors_only exports only "error" findings -- matching the
    "Errors only" checkbox in the UI. Only writes the file, touches nothing else in the library.
    """
    root = Path(root)
    now = now or datetime.now()
    items = [x for x in result.get("findings", []) if not errors_only or x.get("level") == "error"]
    cell = lambda v: str(v or "").replace("|", "\\|").replace("\n", " ")
    lines = [
        f"# Library health check {now:%Y-%m-%d %H:%M}",
        "",
        f"- Library: `{root}`",
        f"- Rooms: {result.get('rooms', 0)}; errors {result.get('errors', 0)}, warnings {result.get('warnings', 0)}"
        + (" (this report: errors only)" if errors_only else ""),
        f"- {len(items)} findings in this report. Locations are relative to the library; nothing was changed. Fix things, then click \"Check again\".",
        "",
    ]
    if not items:
        lines.append("All clean, nothing to report.")
    rooms: dict[str, list[dict]] = {}
    for x in items:
        rooms.setdefault(str(x.get("room") or ""), []).append(x)
    for room, rows in rooms.items():
        lines += [f"## {room} ({len(rows)})", "", "| Level | Location | Problem | What to do |", "| --- | --- | --- | --- |"]
        lines += [f"| {cell(x['level'])} | {cell(x['where'])} | {cell(x['what'])} | {cell(x['hint'])} |" for x in rows]
        lines.append("")
    path = root / REPORT_DIR / f"health-check-{now:%Y%m%d-%H%M%S}.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines), encoding="utf-8")
    return {"path": str(path), "count": len(items), "errors": result.get("errors", 0), "warnings": result.get("warnings", 0)}


def _hidden(p: Path) -> bool:
    return p.name.startswith(".")


class Findings:
    def __init__(self) -> None:
        self.items: list[dict] = []

    def add(self, level: str, room: str, where: str, what: str, hint: str = "") -> None:
        self.items.append({"level": level, "room": room, "where": where, "what": what, "hint": hint})


def _check_gate(f: Findings, room: str, stage_dir: Path, gate: str, known: set[str], root: Path) -> None:
    gate_dir = stage_dir / gate
    rel = lambda p: str(p.relative_to(root))  # noqa: E731

    if not gate_dir.is_dir():
        return
    for sub in sorted(gate_dir.iterdir()):
        if sub.is_file():
            f.add("error", room, rel(sub), "loose file under gate", "inbox/outbox may only contain batch folders")
        elif sub.name == WITHDRAWN_FOLDER:
            continue  # withdrawn pile: folder names carry a timestamp and never follow the batch shape, not checked
        elif sub.name == ARCHIVE_FOLDER:
            # Ticking "archive" between 2026-09-03 and 09-08 added this layer; since 09-08 the flag only writes the ledger, so the layer should be gone
            f.add("error", room, rel(sub), "old archive layer under gate", "\"Archive\" is only a flag in the ledger: move the batches inside up under the gate, then run intake-resync / issue-resync")
        elif BATCH.match(sub.name):
            if str(sub).casefold() not in known:
                f.add("error", room, rel(sub), "batch not in ledger", "run intake-resync / issue-resync to add it to the ledger, or move it into archive\\")
        else:
            f.add("error", room, rel(sub), "folder not in ledger", "an old category layer or a hand-made folder: move batches inside up under the gate, the rest into archive\\")


def _check_guide(f: Findings, room: str, house: Path, root: Path) -> None:
    guide = house / GUIDE
    if not guide.is_file():
        f.add("warning", room, str(guide.relative_to(root)), "missing guide", f"add one from _admin\\templates\\{HOUSE_TEMPLATE}")
        return
    try:
        text = guide.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return
    missing = [s for s in GUIDE_SECTIONS if s not in text]
    if missing:
        f.add("warning", room, str(guide.relative_to(root)), "incomplete guide: missing " + ", ".join(missing), "spec section 5: all four sections are required")


def _check_templates(f: Findings, root: Path) -> None:
    """Whether the three templates exist (spec section 6). Missing is only a warning: new rooms get no guide, the self-check skeleton falls back to the built-in one."""
    for name in TEMPLATE_NAMES:
        path = template_path(root, name)
        if path.is_file() and path.read_text(encoding="utf-8", errors="replace").strip():
            continue
        hint = "on the Spec page under \"Templates\", click \"Edit\" and save once to create it"
        if name in BUILTIN_TEMPLATES:
            hint += "; works without it too, the program has a built-in skeleton as fallback"
        f.add("warning", "_admin", str(TEMPLATE_DIR / name), "missing template", hint)


def _check_pool(f: Findings, room: str, pool: Path, root: Path) -> None:
    """No unsortable names like "final / copy / -v2" in the current folder (spec section 3)."""
    if not pool.is_dir():
        return
    # Only the current folder's own files and one level of drawers; deep trees from unpacked third-party zips don't count
    for p in pool.rglob("*"):
        if not p.is_file() or _hidden(p) or p.name.startswith("~$"):
            continue
        if len(p.relative_to(pool).parts) > 2:
            continue
        if UNSORTABLE.search(p.stem):
            f.add("warning", room, str(p.relative_to(root)), "unsortable name", "keep a single version in current, move older ones into archive\\")


def check_library(root: Path, ledger: Ledger) -> dict:
    """Health-check the whole library."""
    root = Path(root)
    f = Findings()
    known: set[str] = set()
    registered: dict[str, str] = {}
    with ledger.session() as connection:
        try:
            for r in connection.execute("SELECT archive_dir FROM intakes"):
                known.add(str(r["archive_dir"]).casefold())
            for r in connection.execute("SELECT archive_dir FROM intakes WHERE status = 'filed'"):
                if not Path(str(r["archive_dir"])).is_dir():
                    f.add("error", _room_of(root, r["archive_dir"]), str(r["archive_dir"]), "ledger/disk mismatch: intake folder not on disk", "bring it back, or delete this row in the ledger view")
        except sqlite3.OperationalError:
            pass
        for r in connection.execute("SELECT DISTINCT output_dir FROM issues"):
            known.add(str(r["output_dir"]).casefold())
        for r in connection.execute("SELECT DISTINCT output_dir FROM issues WHERE status = 'issued'"):
            if not Path(str(r["output_dir"])).is_dir():
                f.add("error", _room_of(root, r["output_dir"]), str(r["output_dir"]), "ledger/disk mismatch: issue folder not on disk", "bring it back, or delete this batch in the ledger view")
        for r in connection.execute("SELECT project_id, folder FROM projects WHERE active = 1"):
            folder = str(r["folder"] or "") or str(r["project_id"])
            registered[folder.casefold()] = str(r["project_id"])

    seen_rooms: set[str] = set()
    for house in sorted(root.iterdir()):
        if not house.is_dir() or house.name in INFRA or _hidden(house):
            continue
        room = house.name
        seen_rooms.add(room.casefold())
        if room.casefold() not in registered:
            f.add("warning", room, room, "room not registered", "register it under Projects overview \"+ New project\", or move it out of the library root if it is not a project room")
        _check_guide(f, room, house, root)
        for item in sorted(house.iterdir()):
            if _hidden(item):
                continue
            if item.is_file():
                if item.name not in GUIDE_FILES:
                    f.add("error", room, str(item.relative_to(root)), "loose file in room", "move into some stage's archive\\; delete log junk")
                continue
            if item.name not in STAGES:
                f.add("error", room, str(item.relative_to(root)), "non-stage folder in room", "merge into some stage's current or archive (usually other\\)")
                continue
            for pos in sorted(item.iterdir()):
                if _hidden(pos):
                    continue
                if pos.is_file():
                    if pos.name not in GUIDE_FILES:
                        f.add("error", room, str(pos.relative_to(root)), "loose file in stage", "move into archive\\ or current\\")
                    continue
                if pos.name not in POSITIONS:
                    f.add("error", room, str(pos.relative_to(root)), "non-standard position", "only five positions: inbox / current / tools / outbox / archive")
            _check_gate(f, room, item, INBOX_FOLDER, known, root)
            _check_gate(f, room, item, OUTBOX_FOLDER, known, root)
            _check_pool(f, room, item / POOL_FOLDER, root)
        _check_junk(f, room, house, root)

    for folder, project_id in registered.items():
        if folder not in seen_rooms and not (root / folder).is_dir():
            f.add("warning", folder, folder, f"room missing: {project_id}", "registered but not on disk: create it, or the project has been deactivated")

    # Spec section 6 requires all three templates
    _check_templates(f, root)

    order = {"error": 0, "warning": 1}
    f.items.sort(key=lambda x: (order.get(x["level"], 9), x["room"], x["where"]))
    return {
        "root": str(root),
        "errors": sum(1 for x in f.items if x["level"] == "error"),
        "warnings": sum(1 for x in f.items if x["level"] == "warning"),
        "rooms": len(seen_rooms),
        "findings": f.items,
    }


def _check_junk(f: Findings, room: str, house: Path, root: Path) -> None:
    junk: dict[Path, list[str]] = {}
    for p in house.rglob("*"):
        if p.is_file() and (p.name.lower() in JUNK_NAMES or p.suffix.lower() in JUNK_SUFFIXES or p.name.startswith("~$")):
            # archive is the drawer for process files, and a tool's run logs (logs\, run records, caches)
            # are part of the tool -- the spec's "log junk" means the ones scattered among results
            # (second exception, added 2026-09-11).
            if ARCHIVE_FOLDER in p.parts or TOOLS_FOLDER in p.parts:
                continue
            junk.setdefault(p.parent, []).append(p.name)
    # One finding per folder, otherwise a temp tree can flood forty lines
    for parent, names in sorted(junk.items()):
        f.add("warning", room, str(parent.relative_to(root)), f"log junk: {len(names)} files ({', '.join(names[:3])}{'...' if len(names) > 3 else ''})", "delete after use, don't keep it in the room")


def _room_of(root: Path, raw: str) -> str:
    try:
        return Path(str(raw)).relative_to(root).parts[0]
    except (ValueError, IndexError):
        return "?"


def read_spec(
    root: Path,
    ledger: Ledger,
    spec_path: Path | None = None,
) -> dict:
    """The spec, the templates, every room's guide -- everything the "Spec" page shows.

    There is only one spec: spec_path first; if it is missing, fall back to the library root's README.md.
    """
    root = Path(root)

    def text_of(p: Path) -> str:
        try:
            return p.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return ""

    readme = root / "README.md"
    if spec_path is not None and Path(spec_path).is_file():
        readme = Path(spec_path)
    charter = {
        "name": readme.stem,
        "role": "The library's charter: what the library looks like, what guides contain; the health check runs section 9",
        "path": str(readme),
        "exists": readme.is_file(),
        "text": text_of(readme),
    }
    templates = {}
    for name in TEMPLATE_NAMES:
        p = root / TEMPLATE_DIR / name
        exists = p.is_file()
        # If the file is missing, show the built-in one as a draft (saving it creates the template);
        # those without a built-in (the two guide templates) show blank, and the UI says "not yet".
        templates[name] = {
            "path": str(p),
            "exists": exists,
            "text": text_of(p) if exists else BUILTIN_TEMPLATES.get(name, ""),
            "builtin": not exists and name in BUILTIN_TEMPLATES,
        }
    guides = []
    with ledger.session() as connection:
        rows = connection.execute(
            "SELECT project_id, project_name, folder FROM projects WHERE active = 1"
            " ORDER BY updated_at DESC, project_id"
        ).fetchall()
    for r in rows:
        folder = str(r["folder"] or "") or str(r["project_id"])
        house = root / folder
        guide = house / GUIDE
        stage_guides = [
            {"stage": s.name, "path": str(s / GUIDE), "text": text_of(s / GUIDE)}
            for s in sorted(house.iterdir())
            if s.is_dir() and s.name in STAGES and (s / GUIDE).is_file()
        ] if house.is_dir() else []
        guides.append(
            {
                "project_id": str(r["project_id"]),
                "project_name": str(r["project_name"]),
                "folder": folder,
                "path": str(guide),
                "exists": guide.is_file(),
                "text": text_of(guide) if guide.is_file() else "",
                "stages": stage_guides,
            }
        )
    return {
        "charter": charter,
        "templates": templates,
        "guides": guides,
    }


def seed_guide(root: Path, folder: str, project_id: str, project_name: str) -> str:
    """Give a new room a guide from the template plus its CLAUDE.md pointer; existing ones are left alone. Returns the path written, or empty."""
    root = Path(root)
    house = root / folder
    guide = house / GUIDE
    template = root / TEMPLATE_DIR / HOUSE_TEMPLATE
    if guide.exists() or not template.is_file():
        return ""
    text = template.read_text(encoding="utf-8", errors="replace")
    for key, value in (("{{short_name}}", folder), ("{{project_id}}", project_id), ("{{full_name}}", project_name)):
        text = text.replace(key, value)
    house.mkdir(parents=True, exist_ok=True)
    guide.write_text(text, encoding="utf-8")
    pointer = house / GUIDE_POINTER
    if not pointer.exists():
        pointer.write_bytes(GUIDE_POINTER_TEXT.encode("utf-8"))
    return str(guide)
