"""Contacts ledger: name, company, title (optional), phone, viewed grouped by company (2026-09-03).

Lives in the gate_api layer because it feeds dropdown candidates to both gates (the intake's
"source", the issue's "recipient"); neither gate knows about it. The table lives in the
same ledger.db.

Nobody types from scratch: names that appear as intake sources or issue recipients in the
ledger but are not registered yet are listed as "candidates" -- one click fills the form,
add a company and a phone and it is an entry.
"""
from __future__ import annotations

import re
import sqlite3
import uuid
from datetime import datetime

from outbound_gate.ledger import Ledger


SCHEMA = """
CREATE TABLE IF NOT EXISTS contacts (
    contact_id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    org TEXT NOT NULL DEFAULT '',
    title TEXT NOT NULL DEFAULT '',
    phone TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(name, org)
);
"""

# Cells holding several at once, like "Alice Chen, Acme Water Authority" or "Ben Zhao; Carol Wu",
# are split and each piece counts as its own candidate
SPLIT = re.compile(r"[,;/]+")


def _now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def ensure_table(ledger: Ledger) -> None:
    with ledger.session() as connection:
        connection.executescript(SCHEMA)


def list_contacts(ledger: Ledger) -> list[dict]:
    ensure_table(ledger)
    with ledger.session() as connection:
        rows = connection.execute(
            "SELECT contact_id, name, org, title, phone, updated_at FROM contacts"
            " ORDER BY org = '', org, name"
        ).fetchall()
    return [dict(r) for r in rows]


def candidates(ledger: Ledger, known: list[dict]) -> list[dict]:
    """Names seen in the ledger but not registered yet. Sources: intake source org / source person, issue recipients."""
    taken = {str(c["name"]).casefold() for c in known} | {
        str(c["org"]).casefold() for c in known if c["org"]
    }
    seen: dict[str, dict] = {}

    def add(raw: str, kind: str, when: str) -> None:
        for piece in SPLIT.split(str(raw or "")):
            name = piece.strip()
            if not name or name.casefold() in taken:
                continue
            key = name.casefold()
            item = seen.get(key)
            if item is None:
                seen[key] = {"name": name, "kind": kind, "last": when, "times": 1}
            else:
                item["times"] += 1
                if when > item["last"]:
                    item["last"], item["kind"] = when, kind

    with ledger.session() as connection:
        try:
            for row in connection.execute(
                "SELECT source_org, source_person, received_date FROM intakes"
                " WHERE status = 'filed'"
            ):
                add(row["source_org"], "intake", str(row["received_date"]))
                add(row["source_person"], "intake", str(row["received_date"]))
        except sqlite3.OperationalError:
            pass
        for row in connection.execute(
            "SELECT recipients, recorded_at FROM deliveries WHERE action != 'withdrawn'"
        ):
            add(row["recipients"], "issue", str(row["recorded_at"])[:10])
    return sorted(seen.values(), key=lambda x: (x["last"], x["times"]), reverse=True)


def names_and_orgs(ledger: Ledger) -> tuple[list[str], list[str]]:
    """Dropdown candidates for the two gates: registered person names and company names, one list each."""
    rows = list_contacts(ledger)
    names = [r["name"] for r in rows]
    orgs = list(dict.fromkeys(r["org"] for r in rows if r["org"]))
    return names, orgs


# ------------------------------------------------------------------ commands


def cmd_contacts(ledger: Ledger, payload: dict) -> dict:
    rows = list_contacts(ledger)
    return {"contacts": rows, "candidates": candidates(ledger, rows)}


def cmd_contact_save(ledger: Ledger, payload: dict) -> dict:
    """Create or edit an entry. Name is required; same name + same company is kept only once."""
    ensure_table(ledger)
    name = str(payload.get("name") or "").strip()
    org = str(payload.get("org") or "").strip()
    title = str(payload.get("title") or "").strip()
    phone = str(payload.get("phone") or "").strip()
    contact_id = str(payload.get("contact_id") or "").strip()
    if not name:
        raise ValueError("Name is required.")
    now = _now()
    with ledger.immediate_transaction() as connection:
        clash = connection.execute(
            "SELECT contact_id FROM contacts WHERE name = ? AND org = ? AND contact_id != ?",
            (name, org, contact_id),
        ).fetchone()
        if clash:
            raise ValueError(f"This contact already exists: {name} {org or '(no company)'}")
        if contact_id:
            cursor = connection.execute(
                "UPDATE contacts SET name = ?, org = ?, title = ?, phone = ?, updated_at = ?"
                " WHERE contact_id = ?",
                (name, org, title, phone, now, contact_id),
            )
            if cursor.rowcount != 1:
                raise ValueError("Contact not found.")
        else:
            contact_id = str(uuid.uuid4())
            connection.execute(
                "INSERT INTO contacts(contact_id, name, org, title, phone, created_at, updated_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?)",
                (contact_id, name, org, title, phone, now, now),
            )
    return {"contact_id": contact_id, "name": name, "org": org, "title": title, "phone": phone}


def cmd_contact_delete(ledger: Ledger, payload: dict) -> dict:
    ensure_table(ledger)
    ids = [str(x) for x in (payload.get("contact_ids") or []) if str(x)]
    if payload.get("contact_id"):
        ids.append(str(payload["contact_id"]))
    with ledger.immediate_transaction() as connection:
        deleted = 0
        for contact_id in ids:
            deleted += connection.execute(
                "DELETE FROM contacts WHERE contact_id = ?", (contact_id,)
            ).rowcount
    return {"deleted": deleted}
