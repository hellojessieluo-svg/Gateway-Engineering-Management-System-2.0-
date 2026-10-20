r"""Standards index: the current standards and regulations among cross-project reference
material, one per row (2026-09-08; split into categories 2026-09-21).

Disk is the source of truth -- under `_reference\` (common.config.DEFAULT_REFERENCE_ROOT,
by default ~/WorkLibrary/_reference) sit three parallel categories, and whatever is there gets
listed (the category table is common.config.STANDARD_CATEGORIES):

  Technical standards   `standards\`             national/sector technical standards and guidelines,
                                                 split inside into `originals\` `guides\` `pending\` `excerpts\`
  Client regulations    `client-regulations\`    rules, notices and procedures issued by clients; a flat
                                                 folder, one file per entry
  Company requirements  `company-requirements\`  the company's own policies, template requirements,
                                                 collected group notices; same as above

Code and title are split from a file name like `GB_T 11836-2023 Title`; if that fails, the whole
file name is the title -- client regulations and company requirements have no standard code,
and should not be forced into a standard's shape.
PDF, companion md and excerpts in `excerpts\` are matched by code. Filing into the library is
not handled here (the standards library has its own intake check); `pending\` is listed, never moved.

Only three things are filled in by hand: status (current / superseded / repealed / to verify),
scope (which stage, which kind of deliverable uses it) and notes, stored in the ledger's
`standards` table and matched to the entry on disk by "category::file stem"; if the file on
disk is gone, those fields stay in the ledger but are not shown in the UI.

Lives in the gate_api layer, like contacts: later an issue may record "which standards it
satisfies"; neither gate knows about it, it knows the ledger.
"""
from __future__ import annotations

import re
from datetime import datetime
from pathlib import Path

from common.config import STANDARD_CATEGORIES as CATEGORIES
from outbound_gate.ledger import Ledger


ORIGINALS = "originals"
GUIDES = "guides"
PENDING = "pending"
EXCERPTS = "excerpts"
STATUSES = ("current", "superseded", "repealed", "to verify")
DEFAULT_STATUS = "current"
# The hand-filled ledger fields hang on "category::file stem". The separator is :: -- it cannot
# appear in a Windows file name, so it never mixes with the stem itself (decided when the
# categories were split on 2026-09-21; the standards table was empty then, nothing to migrate).
KEY_SEP = "::"

SCHEMA = """
CREATE TABLE IF NOT EXISTS standards (
    key TEXT PRIMARY KEY,
    status TEXT NOT NULL DEFAULT 'current',
    superseded_by TEXT NOT NULL DEFAULT '',
    scope TEXT NOT NULL DEFAULT '',
    notes TEXT NOT NULL DEFAULT '',
    updated_at TEXT NOT NULL
);
"""

# "GB_T 11836-2023 Concrete and reinforced concrete sewer pipes" -> code + title
NAMED = re.compile(
    r"^(?P<code>[A-Z]{1,6}(?:[_/][A-Z]{1,3})?\s*[\d.\-]+-\d{4})\s+(?P<title>\S.*?)\s*$"
)
# The year is the last "-four digits" in the code: `DG_TJ 08-2100-2012` is 2012, not 2100
YEAR = re.compile(r"-(\d{4})(?=\D|$)")
DOC_SUFFIXES = (".pdf", ".md", ".docx", ".doc")


def _now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def ensure_table(ledger: Ledger) -> None:
    with ledger.session() as connection:
        connection.executescript(SCHEMA)


def compact_code(code: str) -> str:
    """Squash a code into a comparable prefix: `GB_T 11836-2023` -> `GB11836`, `SL_T 386-2025` -> `SL386`.
    The recommended-standard "_T" is dropped too -- excerpt folder names (`excerpt_SL386...`) leave it out."""
    head = re.sub(r"-\d{4}(?=\D|$).*$", "", code)
    head = re.sub(r"[_/]T(?=\s|\d)", "", head)
    return re.sub(r"[\s_/\\\-.]", "", head).upper()


def _scan_docs(folder: Path, kind: str, category: str) -> list[dict]:
    """One entry per stem (file name without extension); PDF and md each record their path."""
    if not folder.is_dir():
        return []
    stems: dict[str, dict] = {}
    for p in sorted(folder.iterdir()):
        if not p.is_file() or p.suffix.lower() not in DOC_SUFFIXES:
            continue
        if p.name.lower() in {"readme.md"}:
            continue
        item = stems.setdefault(
            p.stem, {"key": category + KEY_SEP + p.stem, "stem": p.stem,
                     "category": category, "kind": kind, "code": "", "title": p.stem,
                     "year": "", "pdf": "", "md": "", "doc": "", "excerpt": ""}
        )
        if p.suffix.lower() == ".pdf":
            item["pdf"] = str(p)
        elif p.suffix.lower() == ".md":
            item["md"] = str(p)
        else:
            # .doc/.docx: none in the standards library, plenty among client regulations and
            # company requirements (2026-09-21). Listed but unopenable is as good as unlisted,
            # so a path is recorded here too.
            item["doc"] = str(p)
    for item in stems.values():
        m = NAMED.match(item["stem"])
        if m:
            item["code"] = m.group("code").strip()
            item["title"] = m.group("title").strip()
            years = YEAR.findall(item["code"])
            item["year"] = years[-1] if years else ""
    return list(stems.values())


def _scan_library(root: Path, category: str) -> dict:
    """The standards library's internal structure: originals + guides, excerpts matched by code, pending listed but not moved."""
    items = (_scan_docs(root / ORIGINALS, "standard", category)
             + _scan_docs(root / GUIDES, "guide", category))
    excerpts = []
    folder = root / EXCERPTS
    if folder.is_dir():
        excerpts = [p for p in sorted(folder.iterdir())]
    for item in items:
        if not item["code"]:
            continue
        prefix = compact_code(item["code"])
        for p in excerpts:
            name = re.sub(r"^excerpt[_\-\s]*", "", p.name, flags=re.IGNORECASE)
            if compact_code(name[: len(prefix) + 4]).startswith(prefix):
                item["excerpt"] = str(p)
                break
    pending = []
    folder = root / PENDING
    if folder.is_dir():
        pending = [
            {"name": p.name, "path": str(p), "category": category}
            for p in sorted(folder.iterdir())
            if p.is_file() and p.suffix.lower() in DOC_SUFFIXES
        ]
    return {"items": items, "pending": pending}


def _scan_flat(root: Path, category: str) -> dict:
    """Flat folder: one entry per file, also reading one level of subfolders (the kind clients split by year).
    Having no code is normal -- `compact_code` is no use here, sorting goes by file name."""
    items = _scan_docs(root, "regulation", category)
    if root.is_dir():
        for sub in sorted(p for p in root.iterdir() if p.is_dir()):
            if sub.name.startswith(("_", ".")):
                continue
            items += _scan_docs(sub, "regulation", category)
    return {"items": items, "pending": []}


def scan(root: Path, categories=None) -> dict:
    r"""What is on disk: each of the three categories scanned on its own, merged into one list of entries carrying a category.

    `root` is `_reference\`, no longer a single standards library -- that layer is what changed on 2026-09-21.
    """
    root = Path(root)
    categories = list(categories or CATEGORIES)
    items: list[dict] = []
    pending: list[dict] = []
    cats: list[dict] = []
    for spec in categories:
        folder = root / spec["folder"]
        got = (_scan_library(folder, spec["name"]) if spec.get("layout") == "library"
               else _scan_flat(folder, spec["name"]))
        items += got["items"]
        pending += got["pending"]
        cats.append({"name": spec["name"], "folder": spec["folder"],
                     "path": str(folder), "exists": folder.is_dir(),
                     "layout": spec.get("layout", "flat"), "count": len(got["items"])})
    # Categories in table order; within a category, standards by code, guides and regulations by name
    order = {spec["name"]: i for i, spec in enumerate(categories)}
    items.sort(key=lambda x: (order.get(x["category"], 99), x["kind"] != "standard",
                              compact_code(x["code"]) if x["code"] else "~" + x["title"]))
    return {"root": str(root), "exists": root.is_dir(),
            "categories": cats, "items": items, "pending": pending}


def overlay(ledger: Ledger) -> dict[str, dict]:
    ensure_table(ledger)
    with ledger.session() as connection:
        rows = connection.execute(
            "SELECT key, status, superseded_by, scope, notes, updated_at FROM standards"
        ).fetchall()
    return {str(r["key"]): dict(r) for r in rows}


def list_standards(root: Path, ledger: Ledger) -> dict:
    data = scan(root)
    hand = overlay(ledger)
    for item in data["items"]:
        h = hand.get(item["key"]) or {}
        item["status"] = h.get("status") or DEFAULT_STATUS
        item["superseded_by"] = h.get("superseded_by") or ""
        item["scope"] = h.get("scope") or ""
        item["notes"] = h.get("notes") or ""
        item["updated_at"] = h.get("updated_at") or ""
    # Counts per category (the line in the UI's title bar); the standards-library category splits further into standard/guide
    data["counts"] = {c["name"]: c["count"] for c in data["categories"]}
    data["counts"]["pending"] = len(data["pending"])
    data["kinds"] = sorted({x["kind"] for x in data["items"]},
                           key=lambda k: ("standard", "guide", "regulation").index(k)
                           if k in ("standard", "guide", "regulation") else 9)
    data["statuses"] = list(STATUSES)
    return data


# ------------------------------------------------------------------ commands


def cmd_standards(root: Path, ledger: Ledger, payload: dict) -> dict:
    return list_standards(root, ledger)


def cmd_standard_save(root: Path, ledger: Ledger, payload: dict) -> dict:
    """Change one entry's status / superseded by / scope / notes. Only entries that exist on disk."""
    key = str(payload.get("key") or "").strip()
    if not key:
        raise ValueError("Missing the standard's key.")
    known = {x["key"] for x in scan(root)["items"]}
    if key not in known:
        raise ValueError(f"'{key}' is not in _reference -- the index only knows what is on disk.")
    status = str(payload.get("status") or DEFAULT_STATUS).strip()
    if status not in STATUSES:
        raise ValueError(f"Status must be one of: {' / '.join(STATUSES)}")
    superseded_by = str(payload.get("superseded_by") or "").strip()
    scope = str(payload.get("scope") or "").strip()
    notes = str(payload.get("notes") or "").strip()
    ensure_table(ledger)
    now = _now()
    with ledger.immediate_transaction() as connection:
        connection.execute(
            "INSERT INTO standards(key, status, superseded_by, scope, notes, updated_at)"
            " VALUES (?, ?, ?, ?, ?, ?)"
            " ON CONFLICT(key) DO UPDATE SET status = excluded.status,"
            " superseded_by = excluded.superseded_by, scope = excluded.scope,"
            " notes = excluded.notes, updated_at = excluded.updated_at",
            (key, status, superseded_by, scope, notes, now),
        )
    return {"key": key, "status": status, "superseded_by": superseded_by,
            "scope": scope, "notes": notes, "updated_at": now}
