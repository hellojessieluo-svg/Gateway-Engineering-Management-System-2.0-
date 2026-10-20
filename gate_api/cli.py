"""Gateway's command table: JSON in, JSON out.

The COMMANDS table below is the only one; the web service gate_api.server reuses it directly.
The command-line entry stays for troubleshooting and scripting:

    python -m gate_api.cli <command> <args.json>

The result is always one line of JSON on stdout: {"ok": true, "data": ...} or {"ok": false, "error": "..."}.
Arguments come from a file, not the command line, to avoid fighting shell escaping over
non-ASCII text and quotes.

Only two gates remain: intake and issue. The version gate and task gate were removed on
2026-08-15 -- work happens directly in `current\\`, and the story behind it goes into the
issue notes. Their tables were dropped from the ledger on 2026-09-03.
"""
from __future__ import annotations

import hashlib
import io
import json
import os
import re
import sqlite3
import sys
from datetime import date, datetime
from pathlib import Path

from inbound_gate.core import InboundGate
from inbound_gate.models import (
    SOURCE_CHANNELS,
    IntakeItem,
    IntakeRequest,
)
from common.config import AppConfig
from outbound_gate.core import OutboundGate
from outbound_gate.ledger import Ledger, provisional_number
from outbound_gate.models import (
    CHANNELS,
    ISSUE_ROLES,
    SELFCHECK_FILENAME,
    DEFAULT_PURPOSE,
    STAGES,
    STAGE_SEPARATOR,
    IssueRequest,
    SourceItem,
)
from common import places
from . import contacts, library_check, standards
from common.errors import ValidationError
from common.validators import sha256_file
from common.naming import (
    ensure_path_length,
    make_batch_folder_name,
    unique_batch_dir,
    validate_component,
    validate_free_text,
)


# ------------------------------------------------------------------ helpers


def _config(payload: dict) -> AppConfig:
    config = AppConfig.load()
    if payload.get("ledger_path"):
        config.ledger_path = payload["ledger_path"]
    if payload.get("library_root"):
        config.library_root = payload["library_root"]
    if payload.get("spec_path"):
        config.spec_path = payload["spec_path"]
    if payload.get("reference_root"):
        config.reference_root = payload["reference_root"]
    return config


def _ledger(payload: dict) -> Ledger:
    ledger = Ledger(Path(_config(payload).ledger_path))
    ledger.initialize()
    return ledger


def _inbound(payload: dict) -> InboundGate:
    config = _config(payload)
    return InboundGate(Path(config.library_root), config.ledger_path)


def _outbound(payload: dict) -> OutboundGate:
    config = _config(payload)
    return OutboundGate(Path(config.library_root), config.ledger_path)


def _rows(rows) -> list[dict]:
    return [dict(row) for row in rows]


def _parse_date(value: str, label: str) -> date:
    try:
        return datetime.strptime(value.strip(), "%Y-%m-%d").date()
    except ValueError as exc:
        raise ValueError(f"{label} must use the YYYY-MM-DD format, got \"{value}\".") from exc


# ------------------------------------------------------------------ shared


def _remember_stage(payload: dict, project_id: str, stage: str) -> None:
    """Register the stage used this time in the project list. The list emerges from use;
    nobody fills it in by hand.

    Failures don't interrupt filing: the files are already in the library, and a missed
    entry in a filter list shouldn't make anyone start over.
    """
    if not project_id or not stage:
        return
    try:
        cmd_project_stages({**payload, "project_id": project_id, "add": [stage]})
    except Exception:
        pass


def cmd_context(payload: dict) -> dict:
    """Everything the front end needs at startup: projects, the fixed lists, counts."""
    config = _config(payload)
    # The project list belongs to the issue gate. On an empty ledger (fresh install) let it
    # create its tables first, otherwise the SELECT on projects below would abort startup.
    _outbound(payload)
    ledger = _ledger(payload)
    with ledger.session() as connection:
        # stages: which stages this project actually has (comma-separated). Empty = not
        # registered yet, the UI lists all six.
        projects = _rows(
            connection.execute(
                "SELECT project_id, project_name, folder, stages FROM projects"
                " WHERE active = 1 ORDER BY updated_at DESC, project_id"
            )
        )
        try:
            todo = connection.execute(
                "SELECT COUNT(*) AS n FROM intakes WHERE todo = 1 AND status = 'filed'"
            ).fetchone()["n"]
        except sqlite3.OperationalError:
            todo = 0
    # stages is one string in the ledger and always leaves as an array -- the shape the UI
    # gets must be the same from both commands
    for project in projects:
        project["stages"] = [
            x for x in str(project.get("stages") or "").split(STAGE_SEPARATOR) if x
        ]
    return {
        "projects": projects,
        "stages": list(STAGES),
        "source_channels": list(SOURCE_CHANNELS),
        "channels": list(CHANNELS),
        "todo_count": todo,
        "ledger_path": config.ledger_path,
        "library_root": config.library_root,
        "today": date.today().isoformat(),
    }


def cmd_places(payload: dict) -> dict:
    """Where the three positions for this project/stage are and how much is in `current`.
    Addresses are computed, never asked for."""
    config = _config(payload)
    project_id = payload.get("project_id", "")
    stage = payload.get("stage", "")
    folder = _ledger(payload).folder_of(project_id)
    root = Path(config.library_root)
    # inbox and outbox both hang under the stage: no stage selected, no address to compute.
    ready = bool(stage)
    pool = places.pool_dir(root, folder, stage) if ready else None
    return {
        "folder": folder,
        "project_dir": str(places.project_dir(root, folder)),
        "inbox_dir": str(places.inbox_dir(root, folder, stage)) if ready else "",
        "outbox_dir": str(places.outbox_dir(root, folder, stage)) if ready else "",
        "pool_dir": str(pool) if pool else "",
        "pool_exists": bool(pool and pool.is_dir()),
        "pool_file_count": places.count_pool_files(pool) if pool else 0,
    }


# ------------------------------------------------------------------ projects


def cmd_project_create(payload: dict) -> dict:
    """New project: register it in the ledger and create its project room. Without an
    official ID it gets a stable temporary ID."""
    config = _config(payload)
    ledger = _ledger(payload)
    project_name = (payload.get("project_name") or "").strip()
    project_id = (payload.get("project_id") or "").strip()
    folder = validate_component(payload.get("folder") or "", "Short name")
    if not project_name:
        raise ValueError("Project name is required.")
    if project_id:
        validate_component(project_id, "Project ID")
    # Say so on a collision instead of silently reusing: name, ID and short name must each
    # differ from every project in the ledger
    with ledger.session() as connection:
        rows = connection.execute(
            "SELECT project_id, project_name, folder FROM projects"
        ).fetchall()
    for row in rows:
        if str(row["project_name"]) == project_name:
            raise ValueError(f"A project with this name already exists: {row['project_id']} {project_name}")
        if project_id and str(row["project_id"]).casefold() == project_id.casefold():
            raise ValueError(f"Project ID already in use: {row['project_id']} {row['project_name']}")
        if str(row["folder"] or "").casefold() == folder.casefold():
            raise ValueError(f"Short name already in use: {row['project_id']} {row['project_name']}")
    if project_id:
        ledger.register_project(project_id, project_name, folder)
    else:
        project_id = ledger.register_project_name(project_name, folder)
    house = places.project_dir(Path(config.library_root), folder)
    house.mkdir(parents=True, exist_ok=True)
    # A new room comes with a guide: the project card is filled from
    # _admin\templates\project-AGENTS.md, the rest is up to people (spec section 5)
    guide = library_check.seed_guide(Path(config.library_root), folder, project_id, project_name)
    return {
        "project_id": project_id,
        "project_name": project_name,
        "folder": folder,
        "project_dir": str(house),
        "guide_path": guide,
    }


def cmd_project_overview(payload: dict) -> dict:
    """The dashboard's projects overview: one row per room.

    The room's short name, how many intakes are waiting on me, where the room's AGENTS.md
    is -- addresses are computed as usual, never asked for or stored. Not every room has an
    AGENTS.md; whether it exists is returned too so the UI can grey out the button.
    """
    config = _config(payload)
    root = Path(config.library_root)
    ledger = _ledger(payload)
    with ledger.session() as connection:
        projects = _rows(
            connection.execute(
                "SELECT project_id, project_name, folder, stages FROM projects"
                " WHERE active = 1 ORDER BY updated_at DESC, project_id"
            )
        )
        todo: dict[str, int] = {}
        try:
            for row in connection.execute(
                "SELECT project_id, COUNT(*) AS n FROM intakes"
                " WHERE todo = 1 AND status = 'filed' GROUP BY project_id"
            ):
                todo[str(row["project_id"] or "")] = int(row["n"])
        except sqlite3.OperationalError:
            pass
    items = []
    for project in projects:
        folder = str(project["folder"] or "") or str(project["project_id"])
        house = places.project_dir(root, folder)
        guide = house / library_check.GUIDE
        items.append(
            {
                "project_id": project["project_id"],
                "project_name": project["project_name"],
                "folder": folder,
                "project_dir": str(house),
                "house_exists": house.is_dir(),
                "guide_path": str(guide),
                "has_guide": guide.is_file(),
                "stages": [
                    x for x in str(project["stages"] or "").split(STAGE_SEPARATOR) if x
                ],
                "todo_count": todo.get(str(project["project_id"]), 0),
                # Only a project on a temporary ID can be given an official ID
                "provisional": provisional_number(project["project_id"]) is not None,
            }
        )
    return {"projects": items}


def cmd_project_bind(payload: dict) -> dict:
    """Bind a temporary ID to an official ID: projects, issues and intakes move over together."""
    ledger = _ledger(payload)
    old_id = (payload.get("old_project_id") or "").strip()
    new_id = validate_component(payload.get("new_project_id") or "", "Official ID")
    if not old_id:
        raise ValueError("The current ID is required.")
    ledger.bind_project_id(old_id, new_id)
    return {"project_id": new_id}


# ------------------------------------------------------------------ intake


def cmd_project_rename(payload: dict) -> dict:
    """Rename a project: its short name (folder) and/or its project name.

    The short name is the room's address; changing it renames the whole room -- the folder
    on disk is renamed and the absolute paths stored in the ledger (intake batch folders,
    issued files and notes) are rewritten. The project name is just text in the ledger;
    changing it doesn't touch the disk. If neither is given, nothing happens.
    """
    config = _config(payload)
    ledger = _ledger(payload)
    root = Path(config.library_root)
    project_id = (payload.get("project_id") or "").strip()
    if not project_id:
        raise ValueError("Project ID is required.")
    new_folder = (payload.get("folder") or "").strip()
    new_name = (payload.get("project_name") or "").strip()
    if new_folder:
        validate_component(new_folder, "Short name")
    if not new_folder and not new_name:
        raise ValueError("Give a new short name, a new project name, or both.")

    with ledger.session() as connection:
        row = connection.execute(
            "SELECT project_id, project_name, folder FROM projects"
            " WHERE project_id = ? COLLATE NOCASE",
            (project_id,),
        ).fetchone()
        if not row:
            raise ValueError(f"Project not found: {project_id}")
        others = _rows(
            connection.execute(
                "SELECT project_id, project_name, folder FROM projects"
                " WHERE project_id != ? COLLATE NOCASE",
                (project_id,),
            )
        )
    project_id = str(row["project_id"])
    old_folder = str(row["folder"] or "") or project_id
    old_name = str(row["project_name"] or "")
    # Collisions are reported the same way as in project-create, never silently reused
    for other in others:
        if new_folder and str(other["folder"] or "").casefold() == new_folder.casefold():
            raise ValueError(f"Short name already in use: {other['project_id']} {other['project_name']}")
        if new_name and str(other["project_name"]) == new_name:
            raise ValueError(f"A project with this name already exists: {other['project_id']} {other['project_name']}")

    moved = False
    old_dir = places.project_dir(root, old_folder)
    new_dir = old_dir
    if new_folder and new_folder != old_folder:
        new_dir = places.project_dir(root, new_folder)
        if new_dir.exists() and new_dir.resolve() != old_dir.resolve():
            raise ValueError(f"The target room already exists; sort it out first: {new_dir}")
        if old_dir.is_dir():
            old_dir.rename(new_dir)
            moved = True

    now = datetime.now().astimezone().isoformat(timespec="seconds")
    rewritten = 0
    with ledger.immediate_transaction() as connection:
        connection.execute(
            "UPDATE projects SET folder = ?, project_name = ?, updated_at = ?"
            " WHERE project_id = ? COLLATE NOCASE",
            (new_folder or old_folder, new_name or old_name, now, project_id),
        )
        if new_name:
            for table in ("intakes", "issues"):
                try:
                    connection.execute(
                        f"UPDATE {table} SET project_name = ?"
                        " WHERE project_id = ? COLLATE NOCASE",
                        (new_name, project_id),
                    )
                except sqlite3.OperationalError:
                    pass
        if moved:
            # The ledger stores absolute paths; when the room is renamed they must follow,
            # or the ledger points at nothing. Only match "exactly this room" or "under this
            # room": a bare prefix match would hit neighbours whose names start with it --
            # renaming "riverside" to "riverside-wetland" would also LIKE-match
            # "riverside-east\..." and turn it into
            # "riverside-wetland-east\..." (found before a rename on 2026-09-18).
            prefix = str(old_dir)
            target = str(new_dir)
            columns = {
                "intakes": ("archive_dir",),
                "issues": ("file_path", "output_dir", "selfcheck_path"),
            }
            for table, cols in columns.items():
                for column in cols:
                    try:
                        cursor = connection.execute(
                            f"UPDATE {table} SET {column} ="
                            f" ? || substr({column}, ?)"
                            f" WHERE {column} = ? OR {column} LIKE ? || '%'",
                            (target, len(prefix) + 1, prefix, prefix + os.sep),
                        )
                    except sqlite3.OperationalError:
                        continue
                    rewritten += cursor.rowcount or 0
    return {
        "project_id": project_id,
        "folder": new_folder or old_folder,
        "project_name": new_name or old_name,
        "old_folder": old_folder,
        "project_dir": str(new_dir),
        "dir_moved": moved,
        "paths_rewritten": rewritten,
    }


def cmd_project_stages(payload: dict) -> dict:
    """Register which stages this project actually has.

    Some projects only ever have "other"; listing all six is noise. Once registered, both
    gates' stage dropdowns list only the registered stages; a genuinely new stage is picked
    from "Other stage..." and registered automatically.
    So this is a filter, not a gate: the list saves the eyes, it never blocks work.

    `stages` replaces the whole list; `add` appends (this is what both gates use when filing).
    Always stored in STAGES order, so the UI gets it sorted.
    """
    project_id = str(payload.get("project_id") or "").strip()
    if not project_id:
        raise ValueError("A project ID is required to register stages.")
    ledger = _ledger(payload)
    replace = payload.get("stages")
    add = [str(x).strip() for x in (payload.get("add") or []) if str(x).strip()]

    with ledger.immediate_transaction() as connection:
        row = connection.execute(
            "SELECT stages FROM projects WHERE project_id = ? COLLATE NOCASE",
            (project_id,),
        ).fetchone()
        if row is None:
            raise ValueError(f"No project with this ID: {project_id}")
        current = {x for x in str(row["stages"] or "").split(STAGE_SEPARATOR) if x}
        if replace is not None:
            wanted = {str(x).strip() for x in replace if str(x).strip()}
        else:
            wanted = current | set(add)
        unknown = wanted - set(STAGES)
        if unknown:
            raise ValueError("Unsupported stage: " + ", ".join(sorted(unknown)))
        ordered = [x for x in STAGES if x in wanted]
        connection.execute(
            "UPDATE projects SET stages = ?, updated_at = ? WHERE project_id = ?"
            " COLLATE NOCASE",
            (
                STAGE_SEPARATOR.join(ordered),
                datetime.now().astimezone().isoformat(timespec="seconds"),
                project_id,
            ),
        )
    return {"project_id": project_id, "stages": ordered}


def cmd_intake_suggestions(payload: dict) -> dict:
    # The source person was removed from the UI on 2026-08-15: the organization is what
    # people remember; nobody looked back at the person who handled it.
    # Since 2026-09-03 the contacts list comes first: registered companies and people,
    # followed by sources already used in the ledger.
    names, orgs = contacts.names_and_orgs(_ledger(payload))
    used = _inbound(payload).ledger.list_source_orgs()
    return {"source_orgs": list(dict.fromkeys(orgs + names + used))}


def cmd_intakes(payload: dict) -> dict:
    """Intake list. By default only the current project (plus general intakes with no
    project); with a stage given it is narrowed to that stage.

    Only "filed" by default; pass include_withdrawn for the full history including withdrawals.
    The short name comes from projects: that is the room's address, the ID is just an ID.
    """
    ledger = _ledger(payload)
    project_id = payload.get("project_id", "")
    clauses = [] if payload.get("include_withdrawn") else ["i.status = 'filed'"]
    params: list = []
    if project_id and not payload.get("all"):
        clauses.append("(i.project_id = ? COLLATE NOCASE OR i.project_id = '')")
        params.append(project_id)
    # Stage joined the filter on 2026-08-28: when an issue is linked to intakes, only the
    # same stage should be visible -- feasibility comments mixed into a preliminary-design
    # list get picked by mistake and nothing stops it. Old records filed before the
    # restructure have an empty stage; if the stage can't be told, don't hide it (same
    # reasoning as general intakes with an empty project_id above).
    stage = (payload.get("stage") or "").strip()
    if stage and not payload.get("all"):
        clauses.append("(i.stage = ? OR i.stage = '')")
        params.append(stage)
    params.append(int(payload.get("limit", 80)))
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    with ledger.session() as connection:
        try:
            rows = connection.execute(
                "SELECT i.intake_id, i.created_at, i.archived, i.project_id, i.project_name,"
                " COALESCE(NULLIF(p.folder, ''), i.project_id) AS folder,"
                " i.source_org, i.source_person, i.received_date, i.subject,"
                " i.archive_dir, i.file_count, i.todo, i.status, i.stage, i.replies_to,"
                # Whether it's done is judged by evidence, not the checkbox: an issue
                # linked to it means it has been answered
                # (multiple intakes per issue go through the issue_intakes table since 2026-08-20)
                # 2026-09-21: a withdrawn issue doesn't count. Withdrawn = the files came back,
                # the intake is owed a reply again and belongs back in the issue form's list;
                # the link table keeps the link as a trace of the withdrawal, so check status here.
                " (SELECT COUNT(*) FROM issue_intakes x JOIN issues s"
                "    ON s.issue_id = x.issue_id"
                "  WHERE x.intake_id = i.intake_id AND s.status <> 'withdrawn')"
                "   AS issue_count"
                " FROM intakes i LEFT JOIN projects p ON p.project_id = i.project_id"
                f" {where}"
                " ORDER BY i.received_date DESC, i.created_at DESC LIMIT ?",
                params,
            ).fetchall()
        except sqlite3.OperationalError:
            rows = []
    return {"intakes": _rows(rows)}


def cmd_intake_archive(payload: dict) -> dict:
    gate = _inbound(payload)
    items = [
        IntakeItem(item["path"], item.get("relative_name", ""))
        for item in payload.get("items") or []
    ]
    request = IntakeRequest(
        archived=bool(payload.get("archived")),
        project_id=payload.get("project_id", ""),
        project_name=payload.get("project_name", ""),
        source_org=payload.get("source_org", ""),
        source_person=payload.get("source_person", ""),
        source_channel=payload.get("source_channel", ""),
        received_date=_parse_date(payload.get("received_date", ""), "Received date"),
        subject=payload.get("subject", ""),
        items=items,
        todo=bool(payload.get("todo")),
        notes=payload.get("notes", ""),
        stage=payload.get("stage", ""),
        replies_to=payload.get("replies_to", ""),
    )
    result = gate.archive_intake(request)
    _remember_stage(payload, request.project_id, request.stage)
    return {
        "intake_id": result.record_id,
        "archive_dir": str(result.archive_dir),
        "file_count": result.file_count,
        "total_size": result.total_size,
        "duplicate": result.duplicate,
        "warnings": result.warnings,
    }


def cmd_intake_brief(payload: dict) -> dict:
    """The "whole picture" of one intake: plain text that can be pasted straight to an AI.

    This is what the "Copy" button on each dashboard row fetches: where the folder is, which
    project and stage, who sent it, what it is about, which files are inside, which issue it
    replies to, where the current work and the outbox are. Nobody has to piece this together
    for the AI by hand any more -- it is all in one block.
    """
    config = _config(payload)
    root = Path(config.library_root)
    ledger = _ledger(payload)
    intake_id = str(payload.get("intake_id") or "").strip()
    if not intake_id:
        raise ValueError("Which intake? Pass intake_id.")
    with ledger.session() as connection:
        row = connection.execute(
            "SELECT i.*, COALESCE(NULLIF(p.folder, ''), i.project_id) AS folder"
            " FROM intakes i LEFT JOIN projects p ON p.project_id = i.project_id"
            " WHERE i.intake_id = ?",
            (intake_id,),
        ).fetchone()
        if row is None:
            raise ValueError("Intake record not found.")
        files = [
            str(r["archived_name"])
            for r in connection.execute(
                "SELECT archived_name FROM intake_files WHERE intake_id = ? ORDER BY file_id",
                (intake_id,),
            )
        ]
        reply = None
        if row["replies_to"]:
            reply = connection.execute(
                "SELECT i.issue_date, i.file_name, i.batch_id, i.output_dir,"
                " (SELECT d.recipients FROM deliveries d WHERE d.issue_id = i.issue_id"
                "  AND d.action != 'withdrawn' ORDER BY d.recorded_at DESC LIMIT 1) AS recipients,"
                " (SELECT d.subject FROM deliveries d WHERE d.issue_id = i.issue_id"
                "  AND d.action != 'withdrawn' ORDER BY d.recorded_at DESC LIMIT 1) AS subject"
                " FROM issues i WHERE i.issue_id = ?",
                (row["replies_to"],),
            ).fetchone()
            if reply and reply["batch_id"]:
                names = [
                    str(r["file_name"])
                    for r in connection.execute(
                        "SELECT file_name FROM issues WHERE batch_id = ? ORDER BY created_at, rowid",
                        (reply["batch_id"],),
                    )
                ]
                reply = {**dict(reply), "files": names}
            elif reply:
                reply = {**dict(reply), "files": [str(reply["file_name"])]}

    folder = str(row["folder"] or "") or places.GENERIC_FOLDER
    stage = str(row["stage"] or "")
    project = (
        f"{row['project_name']} ({row['project_id']})" if row["project_id"]
        else f"No project ({places.GENERIC_FOLDER})"
    )
    who = str(row["source_org"] or "(source not set)")
    if row["source_person"]:
        who += f" · {row['source_person']}"
    lines = [
        f"Folder: {row['archive_dir']}",
        f"Project: {project}    Stage: {stage or '(not set)'}",
        f"Received: {row['received_date']}    Source: {who}    Subject: {row['subject'] or '(not set)'}",
        f"Files: {len(files)}",
    ]
    if row["notes"]:
        lines.append(f"Notes: {row['notes']}")
    if reply:
        lines.append(
            f"In reply to: {reply['issue_date']} to {reply['recipients'] or '(not recorded)'}"
            f"{' · ' + reply['subject'] if reply['subject'] else ''}"
            f": {', '.join(reply['files'])}    Folder: {reply['output_dir']}"
        )
    if stage:
        lines.append(f"Current work (do the work here): {places.pool_dir(root, folder, stage)}")
        lines.append(f"Issue to: {places.outbox_dir(root, folder, stage)}")
    if row["status"] != "filed":
        lines.append(f"Status: {row['status']}")
    # The closing contract is on the card too: where the notes go and what they look like.
    # A room's AGENTS.md covers only the project itself; the gates' rules travel on the task
    # card, so the AI doing the work doesn't have to look for them elsewhere (2026-09-05).
    draft = _draft_path(root, row["project_id"], stage)
    lines += [
        "",
        f"When done, write a \"{SELFCHECK_FILENAME}\"; it goes out with this batch."
        f" Write it to the library's {places.DRAFT_FOLDER} folder: {draft}",
        "(Write only this one, nowhere else; the issue page's \"Write notes\" button reads the same file."
        f" When it goes into the batch folder the program renames it to \"{SELFCHECK_FILENAME}\","
        f" and after the issue the program deletes the draft from {places.DRAFT_FOLDER}.)"
        " The skeleton is below: fill it in section by section, don't change the headings:",
        "",
        library_check.selfcheck_skeleton(root).rstrip("\n"),
    ]
    return {"intake_id": intake_id, "text": "\n".join(lines)}


def cmd_withdraw_intake(payload: dict) -> dict:
    gate = _inbound(payload)
    target = gate.withdraw_intake(payload["intake_id"])
    return {"moved_to": str(target)}


def cmd_intake_restage(payload: dict) -> dict:
    """The wrong stage was picked when filing: move this intake to another stage (2026-09-21).

    The stage is part of the address, so this command moves the folder and updates the
    ledger, both together. Dry run by default, `apply=true` to act; afterwards the new
    stage is registered in the project's stage list.
    """
    gate = _inbound(payload)
    stage = (payload.get("stage") or "").strip()
    plan = gate.restage_intake(payload["intake_id"], stage, bool(payload.get("apply")))
    if plan.get("changed") and not plan.get("dry_run"):
        ledger = _ledger(payload)
        with ledger.session() as connection:
            row = connection.execute(
                "SELECT project_id FROM intakes WHERE intake_id = ?",
                (payload["intake_id"],),
            ).fetchone()
        if row and row["project_id"]:
            _remember_stage(payload, str(row["project_id"]), stage)
    return plan


def cmd_intake_source(payload: dict) -> dict:
    """The source was entered wrong when filing: change this intake's source (2026-09-21).

    The counterpart of issue-recipients. The source is part of the address, so the rename
    and the ledger change happen together; the address is still computed by the program.
    Dry run by default, `apply=true` to act.
    """
    gate = _inbound(payload)
    return gate.change_source(
        str(payload.get("intake_id") or "").strip(),
        str(payload.get("source_org") or ""),
        bool(payload.get("apply")),
    )


# --------------------------------------------------- delete (rows filed by mistake)
# Withdraw means "received, then taken back" and leaves a red line in the ledger; delete
# means "this row should never have existed" and leaves no trace. Neither destroys files:
# delete also moves the folder into the room's _withdrawn first, then erases the row.


def _delete_many(ids, delete_one, id_field: str) -> dict:
    """Delete a list in one go. One failure doesn't drag down the rest -- the worst thing a
    bulk delete can do is roll back the whole lot halfway through."""
    deleted: list[dict] = []
    failed: list[dict] = []
    for record_id in ids:
        try:
            deleted.append(delete_one(record_id))
        except Exception as exc:
            failed.append({id_field: record_id, "error": str(exc)})
    return {"deleted": deleted, "failed": failed}


def _issues_hanging_on(ledger: Ledger, intake_id: str) -> list[str]:
    """Issues hanging on this intake. The chain must not break, so ask this before deleting."""
    with ledger.session() as connection:
        try:
            rows = connection.execute(
                "SELECT issue_date, file_name, batch_id, issue_id FROM issues"
                " WHERE issue_id IN"
                "  (SELECT issue_id FROM issue_intakes WHERE intake_id = ?)"
                " ORDER BY created_at",
                (intake_id,),
            ).fetchall()
        except sqlite3.OperationalError:
            return []
    # One line per issue batch: the files of one batch shouldn't be read out four times
    seen: dict[str, list[str]] = {}
    order: list[str] = []
    for row in rows:
        key = row["batch_id"] or f"single:{row['issue_id']}"
        if key not in seen:
            seen[key] = [f"{row['issue_date']} {row['file_name']}"]
            order.append(key)
        else:
            seen[key].append(str(row["file_name"]))
    labels = []
    for key in order:
        head, *rest = seen[key]
        labels.append(head + (f" and {len(rest)} more" if rest else ""))
    return labels


def cmd_delete_intakes(payload: dict) -> dict:
    # The two gates don't know each other, this layer knows both: so "check for issues
    # hanging on it before deleting an intake" lives here.
    gate = _inbound(payload)
    ledger = _ledger(payload)

    def delete_one(intake_id: str) -> dict:
        hanging = _issues_hanging_on(ledger, intake_id)
        if hanging:
            listed = ", ".join(hanging[:3])
            more = f" ({len(hanging)} in all)" if len(hanging) > 3 else ""
            raise ValueError(
                f"Issues hang on this intake ({listed}{more}); deleting it would break the chain."
                " Delete those issues first."
            )
        return gate.delete_intake(intake_id)

    return _delete_many(payload.get("intake_ids") or [], delete_one, "intake_id")


def cmd_delete_issues(payload: dict) -> dict:
    gate = _outbound(payload)
    return _delete_many(payload.get("issue_ids") or [], gate.delete_issue, "issue_id")


def cmd_set_todo(payload: dict) -> dict:
    gate = _inbound(payload)
    gate.set_todo(payload["intake_id"], bool(payload.get("todo")))
    return {"intake_id": payload["intake_id"], "todo": bool(payload.get("todo"))}


# ------------------------------------------------------------------ issue


def cmd_issue_suggestions(payload: dict) -> dict:
    # People from the contacts list come first, followed by recipients already used in the ledger
    ledger = _ledger(payload)
    names, _ = contacts.names_and_orgs(ledger)
    return {"recipients": list(dict.fromkeys(names + ledger.list_recipients()))}


# ------------------------------------------------------------------ spec and health check


def cmd_spec_read(payload: dict) -> dict:
    """The spec, the templates, every room's guide -- for the "Spec" page."""
    config = _config(payload)
    return library_check.read_spec(
        Path(config.library_root), _ledger(payload), spec_path=Path(config.spec_path),
    )


def cmd_template_save(payload: dict) -> dict:
    """Save a template edited on the "Spec" page back to _admin\\templates\\. Only the three known names are accepted."""
    config = _config(payload)
    return library_check.write_template(
        Path(config.library_root), str(payload.get("name") or ""), payload.get("text") or "",
    )


def cmd_library_check(payload: dict) -> dict:
    """Health-check the whole library against spec section 9; only reports, never touches anything."""
    config = _config(payload)
    return library_check.check_library(Path(config.library_root), _ledger(payload))


def cmd_library_check_export(payload: dict) -> dict:
    """Run a health check and write the result as markdown into _admin\\health-checks\\, returning the path; errors_only exports only "error" findings."""
    config = _config(payload)
    result = library_check.check_library(Path(config.library_root), _ledger(payload))
    return library_check.export_report(
        Path(config.library_root), result, errors_only=bool(payload.get("errors_only"))
    )


# ------------------------------------------------------------------ contacts


def cmd_contacts(payload: dict) -> dict:
    return contacts.cmd_contacts(_ledger(payload), payload)


def cmd_contact_save(payload: dict) -> dict:
    return contacts.cmd_contact_save(_ledger(payload), payload)


def cmd_contact_delete(payload: dict) -> dict:
    return contacts.cmd_contact_delete(_ledger(payload), payload)


# ------------------------------------------------------------------ standards index


def cmd_standards(payload: dict) -> dict:
    """One row per standard or regulation in the three _reference categories (disk is the truth), plus the hand-filled status / applies to / notes."""
    config = _config(payload)
    return standards.cmd_standards(Path(config.reference_root), _ledger(payload), payload)


def cmd_standard_save(payload: dict) -> dict:
    config = _config(payload)
    return standards.cmd_standard_save(Path(config.reference_root), _ledger(payload), payload)


# The issue preview command was removed on 2026-08-15: the name is the file's own and
# versions are gone entirely, so "look at what it will become before issuing" has nothing
# left to show -- the UI just lists the file names.


def cmd_issue_create(payload: dict) -> dict:
    gate = _outbound(payload)
    # Each item may carry a role (sent / kept), default "sent" (2026-09-22)
    items = [
        SourceItem(item["path"], role=str(item.get("role") or "sent"))
        for item in payload.get("items") or []
    ]
    request = IssueRequest(
        project_id=payload.get("project_id", ""),
        project_name=payload.get("project_name", ""),
        stage=payload.get("stage", ""),
        issue_date=_parse_date(payload.get("issue_date", ""), "Issue date"),
        purpose=payload.get("purpose") or DEFAULT_PURPOSE,
        channels=payload.get("channels") or [],
        archived=bool(payload.get("archived")),
        recipients=payload.get("recipients", ""),
        subject=payload.get("subject", ""),
        items=items,
        selfcheck_path=payload.get("selfcheck_path", ""),
        intake_id=payload.get("intake_id", ""),
        intake_ids=[str(x) for x in (payload.get("intake_ids") or [])],
        notes=payload.get("notes", ""),
    )
    result = gate.create_issue(request)
    _remember_stage(payload, request.project_id, request.stage)
    # Once it has gone out, the draft in _drafts has done its job (2026-09-26); failing to
    # delete it must not turn a success into a failure
    try:
        draft_removed = _consume_draft(
            Path(_config(payload).library_root), request.selfcheck_path, result.selfcheck_path
        )
    except OSError as exc:
        draft_removed = ""
        result.warnings.append(f"Issue done, but the draft in {places.DRAFT_FOLDER} could not be deleted: {exc}")
    # Answered means no longer to do: the intakes this issue hangs on are closed right away.
    # "Done" shouldn't be something a person clicks -- sent out means replied.
    # It lives in this layer because only this layer knows both gates (the issue gate doesn't import the intake gate).
    for intake_id in request.intake_ids:
        try:
            _inbound(payload).set_todo(intake_id, False)
        except Exception as exc:  # the issue is already on disk; a failed close must not turn success into failure
            result.warnings.append(f"Issue done, but intake {intake_id} could not be closed automatically: {exc}")
    return {
        "batch_id": result.batch_id,
        "output_dir": str(result.output_dir),
        "selfcheck_path": str(result.selfcheck_path),
        "files": [
            {
                "issue_id": f.issue_id,
                "name": f.name,
                "path": str(f.path),
                "reused": f.reused,
                "role": f.role,
            }
            for f in result.files
        ],
        "draft_removed": draft_removed,
        "warnings": result.warnings,
    }


def cmd_withdraw_issue(payload: dict) -> dict:
    gate = _outbound(payload)
    if payload.get("batch_id"):
        targets = gate.withdraw_batch(payload["batch_id"])
        return {"moved_to": str(targets[0].parent), "count": len(targets)}
    target = gate.withdraw_issue(payload["issue_id"])
    return {"moved_to": str(target), "count": 1}


def cmd_relink_issue(payload: dict) -> dict:
    """Re-link an issue already in the ledger to another intake (or link an old record after the fact).

    Only the link in the ledger moves; not a single file on disk is touched -- so this does
    not go through "withdraw and issue again". Once linked, that intake is closed right away,
    the same rule as when issuing (see cmd_issue_create).
    Arguments: one of issue_id / issue_ids / batch_id,
    plus intake_id or intake_ids (as many as an issue can link; empty = unlink).
    """
    _inbound(payload)  # an old library may not have the intakes table yet
    ledger = _ledger(payload)
    issue_ids = [str(x) for x in (payload.get("issue_ids") or [])]
    if payload.get("issue_id"):
        issue_ids.append(str(payload["issue_id"]))
    if payload.get("batch_id"):
        issue_ids = [str(row["issue_id"]) for row in ledger.list_batch(payload["batch_id"])]
    if not issue_ids:
        raise ValueError("Which issue should be re-linked? Pass issue_id, issue_ids or batch_id.")

    # The list given is the whole set after the change (order as given; the first goes into issues.intake_id)
    intake_ids = [str(x) for x in (payload.get("intake_ids") or []) if str(x)]
    if payload.get("intake_id"):
        intake_ids.append(str(payload["intake_id"]))
    intake_ids = list(dict.fromkeys(intake_ids))

    intakes = []
    for intake_id in intake_ids:
        intake = ledger.get_intake(intake_id)
        if intake is None:
            raise ValueError(f"The intake to link is not in the ledger: {intake_id}")
        if intake["status"] != "filed":
            raise ValueError("The intake to link has been withdrawn; it can't be the reason for an issue.")
        intakes.append(intake)
    for issue_id in issue_ids:
        issue = ledger.get_issue(issue_id)
        if issue is None:
            raise ValueError(f"Issue record not found: {issue_id}")
        for intake in intakes:
            owner = str(intake["project_id"])
            if owner and owner.casefold() != str(issue["project_id"]).casefold():
                raise ValueError("The intake to link does not belong to this project.")

    ledger.relink_issue(issue_ids, intake_ids)
    warnings: list[str] = []
    for intake_id in intake_ids:
        try:
            _inbound(payload).set_todo(intake_id, False)
        except Exception as exc:  # the link is already changed; a failed close must not turn success into failure
            warnings.append(f"Re-linked, but that intake could not be closed automatically: {exc}")
    return {
        "issue_ids": issue_ids,
        "intake_id": intake_ids[0] if intake_ids else "",
        "intake_ids": intake_ids,
        "warnings": warnings,
    }


def cmd_issue_role(payload: dict) -> dict:
    """Change the role of some files in an issue: sent <-> kept (2026-09-22).

    For marking old batches after the fact (everything before 09-22 is "sent"). Only that one
    word in the ledger is written; no file is touched and the folder name stays -- the role is
    not part of the address.
    Arguments: `roles` is a {file name: role} map, with `batch` (batch_id) to look the names up
    in that batch; or `issue_id` + `role` to change a single row. File names are unique within
    a batch (blocked at issue time), so the file name is a good enough key. Returns each row's
    role before and after.
    """
    role_map = {str(k): str(v).strip() for k, v in (payload.get("roles") or {}).items()}
    issue_id = str(payload.get("issue_id") or "").strip()
    batch = str(payload.get("batch") or payload.get("batch_id") or "").strip()
    if issue_id:
        role_map = {issue_id: str(payload.get("role") or "").strip()}
    if not role_map:
        raise ValueError("Which files? Pass batch + roles ({file name: sent/kept}), or issue_id + role.")
    bad = sorted({r for r in role_map.values() if r not in ISSUE_ROLES})
    if bad:
        raise ValueError(f"A file's role can only be {' / '.join(ISSUE_ROLES)}; not recognised: {', '.join(bad)}")
    ledger = _ledger(payload)
    changed: list[dict] = []
    with ledger.immediate_transaction() as connection:
        if issue_id:
            rows = connection.execute(
                "SELECT issue_id, batch_id, file_name, role FROM issues WHERE issue_id = ?",
                (issue_id,),
            ).fetchall()
            if not rows:
                raise ValueError(f"No such issue in the ledger: {issue_id}")
        else:
            if not batch:
                raise ValueError("Changing by file name needs batch (batch_id).")
            rows = connection.execute(
                "SELECT issue_id, batch_id, file_name, role FROM issues"
                " WHERE batch_id = ? ORDER BY created_at, rowid",
                (batch,),
            ).fetchall()
            if not rows:
                raise ValueError(f"No such batch in the ledger: {batch}")
            names = {str(r["file_name"]) for r in rows}
            missing = sorted(set(role_map) - names)
            if missing:
                raise ValueError(f"These files are not in this batch: {', '.join(missing)}")
        for row in rows:
            key = issue_id if issue_id else str(row["file_name"])
            if key not in role_map:
                continue
            new_role = role_map[key]
            old_role = str(row["role"] or "sent")
            if new_role != old_role:
                connection.execute(
                    "UPDATE issues SET role = ? WHERE issue_id = ?",
                    (new_role, str(row["issue_id"])),
                )
            changed.append(
                {
                    "issue_id": str(row["issue_id"]),
                    "file_name": str(row["file_name"]),
                    "from": old_role,
                    "to": new_role,
                }
            )
    return {"batch": batch or str(rows[0]["batch_id"] or ""), "files": changed}


def _group_batches(rows: list[dict], limit: int) -> list[dict]:
    """The lot of files sent out with one click = one entry in the ledger.

    The ledger is still one row per file (each keeps its own hash and withdrawal status), but
    for **viewing** they are gathered by `batch_id` into one entry: the file names are listed
    inside; date / project / notes / replied-to intake are shared by the whole batch anyway.
    Old records have no batch_id and each stands alone.
    """
    batches: dict[str, dict] = {}
    order: list[str] = []
    for row in rows:
        # Old records have no batch_id (before the restructure one issue was one row); each stands alone
        key = row.get("batch_id") or f"single:{row['issue_id']}"
        batch = batches.get(key)
        if batch is None:
            batch = {**row, "issue_ids": [], "files": [], "statuses": set()}
            batches[key] = batch
            order.append(key)
        batch["issue_ids"].append(row["issue_id"])
        batch["files"].append(row["file_name"])
        batch["statuses"].add(row["status"])
        # 2026-09-22: each file's role comes alongside its name (same index); the batch counts "sent N, kept M"
        batch.setdefault("roles", []).append(str(row.get("role") or "sent"))
    out: list[dict] = []
    for key in order[:limit]:
        batch = batches[key]
        statuses = batch.pop("statuses")
        # Part of a batch withdrawn and part kept must be visible, not reported as "issued"
        batch["status"] = statuses.pop() if len(statuses) == 1 else "partly withdrawn"
        batch["file_count"] = len(batch["issue_ids"])
        roles = batch.get("roles") or []
        batch["kept_count"] = sum(1 for r in roles if r == "kept")
        batch["sent_count"] = len(roles) - batch["kept_count"]
        out.append(batch)
    return out


def cmd_issues(payload: dict) -> dict:
    """Issue list: one entry per issue batch, not one per file.

    Every issue hangs on an intake (`issues.intake_id`); that intake comes along here too --
    the ledger must show at a glance "whose request is this version answering", and the
    issue half alone can't tell cause and effect.
    """
    _inbound(payload)  # make sure the intakes table exists, or the LEFT JOIN below empties the result
    ledger = _ledger(payload)
    clauses: list[str] = []
    params: list = []
    if payload.get("project_id"):
        clauses.append("i.project_id = ? COLLATE NOCASE")
        params.append(payload["project_id"])
    # Filter by stage following cmd_intakes (2026-09-18): when an intake picks "in reply to",
    # only the same stage should be visible -- a batch issued in preliminary-design doesn't
    # belong on a construction-drawings intake form. Old records from before the restructure
    # have an empty stage; if the stage can't be told, don't hide it, same as on the intake side.
    stage = (payload.get("stage") or "").strip()
    if stage:
        clauses.append("(i.stage = ? OR i.stage = '')")
        params.append(stage)
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    limit = int(payload.get("limit", 60))
    # Rows are fetched per file and only shrink once grouped into batches: fetch more, then
    # cut, so the last batch isn't missing files
    params.append(max(limit * 10, 200))
    with ledger.session() as connection:
        try:
            rows = connection.execute(
                "SELECT i.issue_id, i.batch_id, i.created_at, i.project_id, i.project_name,"
                " i.stage, i.archived, i.role,"
                " i.issue_date, i.file_name, i.output_dir, i.status,"
                " COALESCE(NULLIF(p.folder, ''), i.project_id) AS folder,"
                # Notes and recipients are both on the delivery log: take the latest non-withdrawal one
                " (SELECT d.notes FROM deliveries d WHERE d.issue_id = i.issue_id"
                "  AND d.action != 'withdrawn'"  # the withdrawal row has empty notes and must not hide the real ones
                "  ORDER BY d.recorded_at DESC, d.rowid DESC LIMIT 1) AS notes,"
                " (SELECT d.recipients FROM deliveries d WHERE d.issue_id = i.issue_id"
                "  AND d.action != 'withdrawn'"
                "  ORDER BY d.recorded_at DESC, d.rowid DESC LIMIT 1) AS recipients,"
                " (SELECT d.subject FROM deliveries d WHERE d.issue_id = i.issue_id"
                "  AND d.action != 'withdrawn'"
                "  ORDER BY d.recorded_at DESC, d.rowid DESC LIMIT 1) AS subject,"
                # The linked intake: whose request this version answers. There may be more than
                # one -- the detail shows the first (intake_id column), intake_count gives the total
                " i.intake_id, n.received_date AS intake_date, n.archived AS intake_archived,"
                " n.source_org AS intake_org, n.source_person AS intake_person,"
                " n.subject AS intake_subject, n.archive_dir AS intake_dir,"
                " n.status AS intake_status,"
                " (SELECT COUNT(*) FROM issue_intakes x WHERE x.issue_id = i.issue_id)"
                "   AS intake_count"
                " FROM issues i LEFT JOIN projects p ON p.project_id = i.project_id"
                " LEFT JOIN intakes n ON n.intake_id = i.intake_id"
                f" {where} ORDER BY i.created_at DESC LIMIT ?",
                params,
            ).fetchall()
        except sqlite3.OperationalError:
            rows = []
    return {"issues": _group_batches(_rows(rows), limit)}


def cmd_undoable(payload: dict) -> dict:
    """Each gate's "latest operation that can still be undone"; the UI uses it to light up the withdraw buttons."""
    ledger = _ledger(payload)
    project_id = payload.get("project_id", "")
    out: dict = {"intake": None, "issue": None}
    with ledger.session() as connection:
        try:
            row = connection.execute(
                "SELECT intake_id, received_date, source_org, subject, file_count, archive_dir"
                " FROM intakes WHERE status = 'filed'"
                " ORDER BY created_at DESC LIMIT 1"
            ).fetchone()
            if row:
                out["intake"] = {
                    "id": row["intake_id"],
                    "label": f"{row['received_date']} {row['source_org']}"
                    f"{' · ' + row['subject'] if row['subject'] else ''}"
                    f" ({row['file_count']} file{'' if row['file_count'] == 1 else 's'})",
                    "dir": row["archive_dir"],
                }
        except sqlite3.OperationalError:
            pass

        try:
            clauses = ["status = 'issued'"]
            params: list = []
            if project_id:
                clauses.append("project_id = ? COLLATE NOCASE")
                params.append(project_id)
            # The unit of withdrawal is "the batch sent with the last click", not one file in it
            row = connection.execute(
                "SELECT batch_id, issue_id, file_name, stage, issue_date,"
                " output_dir, created_at"
                f" FROM issues WHERE {' AND '.join(clauses)}"
                " ORDER BY created_at DESC LIMIT 1",
                params,
            ).fetchone()
            if row:
                batch = (
                    connection.execute(
                        "SELECT file_name FROM issues"
                        " WHERE batch_id = ? AND status = 'issued'"
                        " ORDER BY created_at, rowid",
                        (row["batch_id"],),
                    ).fetchall()
                    if row["batch_id"]
                    else [row]
                )
                head = str(batch[0]["file_name"])
                more = f" and {len(batch) - 1} more" if len(batch) > 1 else ""
                out["issue"] = {
                    "id": row["issue_id"],
                    "batch_id": row["batch_id"],
                    "label": f"{row['issue_date']} {head}"
                    f"{'-' + str(row['stage']) if row['stage'] else ''}{more}",
                    "dir": row["output_dir"],
                }
        except sqlite3.OperationalError:
            pass
    return out


# ---------------------------------------------------------------- notes drafts
# Every issue must come with change & self-check notes. It used to be that you had to write
# a .md somewhere else first and then pick it; now it can be written in the UI too: it lands
# as a draft and is copied into the batch folder as a source file when issuing.
# One per project + stage, and continuing to write means this one; the task card, the
# "Write notes" button and the file picked when issuing all point at it.
# It has moved twice: the program's own folder (%LOCALAPPDATA%\Gateway\...) was impossible
# for people to find, so on 2026-09-21 it moved to the desktop; drafts already issued piled
# up there, so on 2026-09-26 it moved into the library root's _drafts\
# (places.DRAFT_FOLDER), and the program deletes a draft once it has been issued (_consume_draft).
# The file name carries "ID-stage" to be recognisable (e.g. PRJ-2601-construction-drawings-change-notes.md).


def _draft_dir(root: Path) -> Path:
    return Path(root) / places.DRAFT_FOLDER


def _draft_path(root: Path, project_id: str, stage: str) -> Path:
    key = "-".join(
        part for part in (str(project_id or "").strip(), str(stage or "").strip()) if part
    ) or "no-project"
    key = re.sub(r'[\\/:*?"<>|]+', "_", key)
    return _draft_dir(root) / f"{key}-{SELFCHECK_FILENAME}"


def _payload_draft_path(payload: dict) -> Path:
    config = _config(payload)
    return _draft_path(Path(config.library_root), payload.get("project_id"), payload.get("stage"))


def _consume_draft(root: Path, source: str, landed: Path) -> str:
    """Delete the used draft once the issue is in the ledger (2026-09-26). Returns the deleted path, or an empty string.

    Only drafts in _drafts\\ are deleted -- a .md picked by hand from elsewhere is the
    person's own file and is left alone. Before deleting, compare it byte for byte with the
    copy that landed in the batch: when the whole batch is old content the program doesn't
    copy the notes at all, so the batch's copy is from last time, doesn't match, and the
    draft stays.
    """
    if not str(source or "").strip():
        return ""
    path = Path(source).expanduser()
    try:
        in_draft_dir = path.resolve().parent == _draft_dir(root).resolve()
    except OSError:
        return ""
    if not in_draft_dir or not path.is_file() or not Path(landed).is_file():
        return ""
    if sha256_file(path) != sha256_file(Path(landed)):
        return ""
    path.unlink()
    return str(path)


def cmd_selfcheck_draft(payload: dict) -> dict:
    """Save the text written in the UI as a draft .md and return its path."""
    text = str(payload.get("text") or "")
    if not text.strip():
        raise ValueError("The change & self-check notes cannot be empty.")
    path = _payload_draft_path(payload)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text.replace("\r\n", "\n"), encoding="utf-8")
    return {"path": str(path), "name": path.name, "size": path.stat().st_size}


def cmd_selfcheck_read(payload: dict) -> dict:
    """Read a .md's text for the UI to keep editing; if it can't be read, start from the skeleton -- not an error."""
    raw = str(payload.get("path") or "").strip()
    path = Path(raw).expanduser() if raw else _payload_draft_path(payload)
    if path.suffix.lower() != ".md" or not path.is_file():
        # There is only one skeleton: the library's template _admin\templates\change-notes.md;
        # if it is missing, the built-in one in common.constants. The UI keeps no copy of its own.
        config = _config(payload)
        skeleton = library_check.selfcheck_skeleton(Path(config.library_root))
        return {"path": str(path), "text": skeleton, "exists": False}
    return {"path": str(path), "text": path.read_text(encoding="utf-8"), "exists": True}


def cmd_selfcheck_rename(payload: dict) -> dict:
    """Rename the self-check notes in old batches to "change-notes.md".

    Since 2026-08-27 the notes get this name when an issue lands; earlier batches are all
    over the place: "placeholder.md", "New Markdown Document.md", "selfcheck.md"... one style
    per batch, so when browsing you can't tell which file is the notes. This command renames
    the file on disk and updates selfcheck_path in the ledger.

    If the source file was renamed by hand (the one in the ledger is gone), and the batch
    folder holds exactly one .md that is not in the ledger, that is taken as the notes --
    disk is the truth, a person changed it. Anything that can't be identified is reported
    and left alone. Dry run by default, apply=true to act.
    """
    apply = bool(payload.get("apply"))
    ledger = _ledger(payload)

    renames: list[dict] = []
    db_updates: list[tuple] = []
    already: list[str] = []
    problems: list[str] = []

    with ledger.session() as connection:
        rows = list(
            connection.execute(
                "SELECT issue_id, file_name, selfcheck_path, output_dir, status"
                " FROM issues WHERE selfcheck_path <> '' ORDER BY created_at"
            )
        )

    # One set of notes per batch: group by selfcheck_path, one rename per group
    groups: dict[str, list] = {}
    for row in rows:
        groups.setdefault(str(row["selfcheck_path"]), []).append(row)

    for old_raw, group in groups.items():
        old_path = Path(old_raw)
        batch_dir = old_path.parent
        target = batch_dir / SELFCHECK_FILENAME
        ids = ", ".join(str(r["issue_id"])[:8] for r in group)

        if old_path.name == SELFCHECK_FILENAME and old_path.is_file():
            already.append(str(old_path))
            continue

        source = old_path if old_path.is_file() else None
        if source is None:
            # The one in the ledger is gone: if the batch folder has exactly one .md not in the ledger, that's it
            if not batch_dir.is_dir():
                problems.append(f"Batch folder is gone ({ids}): {batch_dir}")
                continue
            issued = {str(r["file_name"]) for r in group}
            loose = [
                q
                for q in sorted(batch_dir.iterdir())
                if q.is_file() and q.suffix.lower() == ".md" and q.name not in issued
            ]
            if len(loose) != 1:
                problems.append(
                    f"The notes in the ledger are not on disk, and no single .md can be identified in the folder"
                    f" ({ids}, {len(loose)} candidates): {old_raw}"
                )
                continue
            source = loose[0]

        if source.name == SELFCHECK_FILENAME:
            # The name is already right, the ledger just points elsewhere (renamed by hand) -- fix the ledger only
            db_updates.extend((str(source), r["issue_id"]) for r in group)
            renames.append({"from": str(old_raw), "to": str(source), "ledger_only": True})
            continue
        if target.exists():
            problems.append(f"Target already exists, refusing to overwrite ({ids}): {target}")
            continue
        renames.append({"from": str(source), "to": str(target)})
        db_updates.extend((str(target), r["issue_id"]) for r in group)

    result = {
        "dry_run": not apply,
        "renames": renames,
        "db_updates": len(db_updates),
        "already_named": len(already),
        "problems": problems,
    }
    if not apply:
        return result
    if problems:
        raise ValueError("Some batches can't be identified; sort them out first: " + "; ".join(problems))

    done: list[tuple[Path, Path]] = []
    try:
        for item in renames:
            if item.get("ledger_only"):
                continue
            src, dst = Path(item["from"]), Path(item["to"])
            if dst.exists():
                raise ValueError(f"Target already exists, refusing to overwrite: {dst}")
            src.rename(dst)
            done.append((src, dst))
        with ledger.immediate_transaction() as connection:
            connection.executemany(
                "UPDATE issues SET selfcheck_path = ? WHERE issue_id = ?", db_updates
            )
    except Exception:
        for src, dst in reversed(done):
            if dst.exists() and not src.exists():
                dst.rename(src)
        raise

    missing: list[str] = []
    with ledger.session() as connection:
        for row in connection.execute(
            "SELECT issue_id, selfcheck_path FROM issues WHERE selfcheck_path <> ''"
        ):
            q = Path(str(row["selfcheck_path"]))
            if q.name != SELFCHECK_FILENAME or not q.is_file():
                missing.append(f"{str(row['issue_id'])[:8]}: {q}")
    result["missing_after"] = missing
    return result


# 2026-09-11: issue folder names gained a "subject"; old batches are still "date-recipient".
# Backfilling the subject follows the selfcheck-rename pattern: dry run for a person to look
# at first, act only with apply.
# Notes starting with "[Backfill" are a ledger-backfill template, not this batch's subject;
# skip them when proposing candidates.
LEDGER_BACKFILL_MARK = "[Backfill"
SUBJECT_MAX = 40


def _clean_subject(text: str) -> str:
    """Turn a sentence into a short subject that fits a folder name: drop line breaks,
    replace characters Windows rejects, cut to length.

    Cutting prefers a punctuation mark and only hard-cuts when there is none -- a hard cut
    can leave a dangling "(", which is removed as well.
    """
    text = re.sub(r"\s+", " ", str(text or "")).strip()
    text = text.replace("/", "-").replace("\\", "-")
    text = re.sub(r'[<>:"|?*`]', "", text).strip(" .,;-")
    if len(text) > SUBJECT_MAX:
        head = text[:SUBJECT_MAX]
        cut = max(head.rfind(c) for c in ",.:;")
        text = head[:cut] if cut >= SUBJECT_MAX // 2 else head
        if text.count("(") > text.count(")"):
            text = text[: text.rfind("(")]
    return text.strip(" .,;-")


def _subject_from_old_name(old_name: str, issue_date: str, recipients: str) -> str:
    """The leftover topic in an old folder name.

    Before 2026-08-15 the program named folders itself (`date-topic-vN`), and the topic was
    exactly what that batch was -- more accurate than a hastily written note. Strip the date,
    the recipient, the collision number and the version tail, and what's left is it.
    """
    stem = old_name
    for prefix in (issue_date.replace("-", "."), recipients):
        if prefix and stem.startswith(prefix):
            stem = stem[len(prefix):].lstrip("-")
    stem = re.sub(r"-v?\d+$", "", stem)
    return "" if stem == recipients else _clean_subject(stem)


def _guess_subject(
    notes: str,
    intake_subject: str,
    old_name: str,
    issue_date: str,
    recipients: str,
    file_names: list[str],
) -> str:
    """Subject candidate for an old batch.

    The intake's subject comes first: the issue answers that request, so it is this batch's
    title. Next is the topic in the old folder name (named by the old program), and only then
    the notes -- notes are often a throwaway line ("as needed", "keep it simple") that says
    nothing as a folder name. Long backfill-template lines ("[Backfill...") are always skipped.
    """
    candidates = [
        intake_subject,
        _subject_from_old_name(old_name, issue_date, recipients),
        notes,
        Path(file_names[0]).stem if file_names else "",
    ]
    for raw in candidates:
        if not raw or str(raw).startswith(LEDGER_BACKFILL_MARK):
            continue
        picked = _clean_subject(raw)
        if picked:
            return picked
    return ""


def cmd_issue_subject(payload: dict) -> dict:
    """Backfill the subject of old issue batches: write the ledger and rename the batch folder to "date-recipient-subject".

    Before issue folder names gained a "subject" on 2026-09-11, batches were all named
    "date-recipient", and from the folder name alone two batches sent to the same person on
    the same day couldn't be told apart. This command fills it in per batch:
    without `subjects` it scans the whole library and proposes candidates (notes -> intake
    subject -> file name); with them it uses what a person decided -- the key is `batch_id`,
    or `issue_id` for old records without one.
    Folder names belong to the program: new names go through make_batch_folder_name /
    unique_batch_dir as usual, collisions get -2.
    The ledger's three paths (output_dir / file_path / selfcheck_path) and deliveries.subject
    follow; withdrawn files were moved to `_withdrawn` long ago, and paths not under this batch
    folder are left alone.
    Dry run by default, apply=true to act; if a rename fails halfway, folders already renamed are rolled back.
    """
    apply = bool(payload.get("apply"))
    given = {str(k): str(v) for k, v in (payload.get("subjects") or {}).items()}
    only = {str(x) for x in (payload.get("batches") or [])}
    ledger = _ledger(payload)

    with ledger.session() as connection:
        rows = list(
            connection.execute(
                "SELECT i.issue_id, i.batch_id, i.issue_date, i.project_id, i.stage,"
                " i.output_dir, i.file_path, i.file_name, i.selfcheck_path, i.status,"
                " (SELECT d.recipients FROM deliveries d WHERE d.issue_id = i.issue_id"
                "  AND d.action != 'withdrawn' ORDER BY d.recorded_at DESC, d.rowid DESC LIMIT 1)"
                "   AS recipients,"
                " (SELECT d.subject FROM deliveries d WHERE d.issue_id = i.issue_id"
                "  AND d.action != 'withdrawn' ORDER BY d.recorded_at DESC, d.rowid DESC LIMIT 1)"
                "   AS subject,"
                " (SELECT d.notes FROM deliveries d WHERE d.issue_id = i.issue_id"
                "  AND d.action != 'withdrawn' ORDER BY d.recorded_at DESC, d.rowid DESC LIMIT 1)"
                "   AS notes,"
                " n.subject AS intake_subject"
                " FROM issues i LEFT JOIN intakes n ON n.intake_id = i.intake_id"
                " ORDER BY i.issue_date, i.created_at"
            )
        )

    groups: dict[str, list] = {}
    for row in rows:
        groups.setdefault(str(row["batch_id"]) or str(row["issue_id"]), []).append(row)

    plans: list[dict] = []
    problems: list[str] = []
    # Target folders already promised in this round, plus old folders not yet renamed: two
    # batches on the same day, to the same person, about the same thing (it happens) must each
    # get their own number, never the same address
    taken = {str(Path(str(r["output_dir"]))).casefold() for r in rows}
    for key, group in groups.items():
        if only and key not in only:
            continue
        head = group[0]
        old_dir = Path(str(head["output_dir"]))
        recipients = str(head["recipients"] or "")
        subject = _clean_subject(
            given.get(key)
            if key in given
            else (
                str(head["subject"] or "")
                or _guess_subject(
                    str(head["notes"] or ""),
                    str(head["intake_subject"] or ""),
                    old_dir.name,
                    str(head["issue_date"]),
                    recipients,
                    [str(r["file_name"]) for r in group],
                )
            )
        )
        item = {
            "batch": key,
            "issue_date": str(head["issue_date"]),
            "project_id": str(head["project_id"]),
            "stage": str(head["stage"]),
            "recipients": recipients,
            "subject": subject,
            "from": str(old_dir),
            "notes": str(head["notes"] or "")[:60],
            "intake_subject": str(head["intake_subject"] or ""),
            "file_count": len(group),
        }
        if not subject:
            problems.append(f"Can't work out a subject, give it one ({key[:8]}): {old_dir}")
            continue
        if not recipients:
            problems.append(f"No recipient in the ledger, can't build the folder name ({key[:8]}): {old_dir}")
            continue
        if not old_dir.is_dir():
            problems.append(f"Batch folder is not on disk ({key[:8]}): {old_dir}")
            continue
        try:
            new_name = make_batch_folder_name(
                _parse_date(str(head["issue_date"]), "Issue date"), recipients, subject
            )
        except Exception as exc:
            problems.append(f"Invalid name ({key[:8]}): {exc}")
            continue
        if new_name == old_dir.name:
            item["to"] = str(old_dir)
            # The name is already right: this address stays taken, don't let a later batch land on it
            item["ledger_only"] = True
            plans.append(item)
            continue
        taken.discard(str(old_dir).casefold())
        new_dir = unique_batch_dir(old_dir.parent, new_name)
        index = 2
        while str(new_dir).casefold() in taken:
            new_dir = unique_batch_dir(old_dir.parent, f"{new_name}-{index}")
            index += 1
        taken.add(str(new_dir).casefold())
        try:
            for child in old_dir.iterdir():
                ensure_path_length(new_dir / child.name)
        except ValidationError as exc:
            problems.append(f"Path would be too long after renaming ({key[:8]}): {exc}")
            continue
        item["to"] = str(new_dir)
        item["_group"] = group
        plans.append(item)

    view = [{k: v for k, v in q.items() if not k.startswith("_")} for q in plans]
    result = {"dry_run": not apply, "plans": view, "problems": problems}
    if not apply:
        return result

    done: list[tuple[Path, Path]] = []
    try:
        for plan in plans:
            if plan.get("ledger_only"):
                continue
            src, dst = Path(plan["from"]), Path(plan["to"])
            if dst.exists():
                raise ValueError(f"Target already exists, refusing to overwrite: {dst}")
            src.rename(dst)
            done.append((src, dst))
        with ledger.immediate_transaction() as connection:
            for plan in plans:
                src, dst = str(plan["from"]), str(plan["to"])
                for row in plan.get("_group", groups[plan["batch"]]):
                    issue_id = str(row["issue_id"])
                    updates = {"output_dir": dst} if src != dst else {}
                    # Withdrawn files were moved to _withdrawn long ago; only change paths still in this batch folder
                    for column in ("file_path", "selfcheck_path"):
                        old_value = str(row[column] or "")
                        if old_value and old_value.startswith(src):
                            updates[column] = dst + old_value[len(src):]
                    if str(row["status"]) != "issued" and "output_dir" in updates:
                        # When the whole batch was withdrawn, output_dir points at _withdrawn, not this folder
                        if not str(row["output_dir"] or "").startswith(src):
                            updates.pop("output_dir")
                    if updates:
                        connection.execute(
                            "UPDATE issues SET "
                            + ", ".join(f"{c} = ?" for c in updates)
                            + " WHERE issue_id = ?",
                            (*updates.values(), issue_id),
                        )
                    connection.execute(
                        "UPDATE deliveries SET subject = ?"
                        " WHERE issue_id = ? AND action != 'withdrawn'",
                        (plan["subject"], issue_id),
                    )
    except Exception:
        for src, dst in reversed(done):
            if dst.exists() and not src.exists():
                dst.rename(src)
        raise
    return result


def cmd_issue_redate(payload: dict) -> dict:
    """Change the issue date of a batch: write the ledger and rename the batch folder with it.

    The form's date defaults to today, so when backfilling old work (actually written and sent
    two weeks ago) it's easy to end up with today's date. The issue date is "when it happened"
    for that ledger entry; when it changes, the batch folder must follow -- folder names belong
    to the program, and the shape is always "date-recipient-subject".
    The key is `batch_id`, or `issue_id` for old records without one; every row of the batch changes.
    The ledger's three paths (output_dir / file_path / selfcheck_path) follow;
    withdrawn files were moved to `_withdrawn` long ago, and paths not under this batch folder
    are left alone. The recording time (created_at / deliveries.recorded_at) is not touched --
    that is "when it was entered", a different fact. Dry run by default, apply=true to act;
    if the rename fails it is rolled back.
    """
    apply = bool(payload.get("apply"))
    key = str(payload.get("batch") or payload.get("batch_id") or "").strip()
    if not key:
        raise ValueError("Which batch? Pass batch (batch_id; issue_id for old records).")
    new_date = _parse_date(str(payload.get("issue_date") or ""), "Issue date")
    ledger = _ledger(payload)

    with ledger.session() as connection:
        rows = list(
            connection.execute(
                "SELECT i.issue_id, i.batch_id, i.issue_date, i.project_id, i.stage,"
                " i.output_dir, i.file_path, i.file_name, i.selfcheck_path, i.status,"
                " (SELECT d.recipients FROM deliveries d WHERE d.issue_id = i.issue_id"
                "  AND d.action != 'withdrawn' ORDER BY d.recorded_at DESC, d.rowid DESC LIMIT 1)"
                "   AS recipients,"
                " (SELECT d.subject FROM deliveries d WHERE d.issue_id = i.issue_id"
                "  AND d.action != 'withdrawn' ORDER BY d.recorded_at DESC, d.rowid DESC LIMIT 1)"
                "   AS subject"
                " FROM issues i WHERE i.batch_id = ? OR i.issue_id = ?"
                " ORDER BY i.created_at, i.rowid",
                (key, key),
            )
        )
    if not rows:
        raise ValueError(f"No such batch in the ledger: {key}")

    head = rows[0]
    old_dir = Path(str(head["output_dir"]))
    recipients = str(head["recipients"] or "")
    subject = str(head["subject"] or "")
    plan = {
        "batch": str(head["batch_id"] or head["issue_id"]),
        "from_date": str(head["issue_date"]),
        "to_date": new_date.isoformat(),
        "recipients": recipients,
        "subject": subject,
        "from": str(old_dir),
        "to": str(old_dir),
        "file_count": len(rows),
        "files": [str(r["file_name"]) for r in rows],
    }
    if not recipients:
        raise ValueError(f"No recipient in the ledger, can't compute the folder name: {old_dir}")
    if not old_dir.is_dir():
        raise ValueError(f"Batch folder is not on disk; resync before changing the date: {old_dir}")

    new_name = make_batch_folder_name(new_date, recipients, subject)
    if new_name != old_dir.name:
        new_dir = unique_batch_dir(old_dir.parent, new_name)
        for child in old_dir.iterdir():
            ensure_path_length(new_dir / child.name)
        plan["to"] = str(new_dir)

    result = {"dry_run": not apply, "plan": plan}
    if not apply:
        return result

    src, dst = Path(plan["from"]), Path(plan["to"])
    renamed = False
    if src != dst:
        if dst.exists():
            raise ValueError(f"Target already exists, refusing to overwrite: {dst}")
        src.rename(dst)
        renamed = True
    try:
        with ledger.immediate_transaction() as connection:
            for row in rows:
                updates = {"issue_date": new_date.isoformat()}
                if src != dst:
                    # When the whole batch was withdrawn, output_dir points at _withdrawn, not this batch folder
                    if str(row["output_dir"] or "") == str(src):
                        updates["output_dir"] = str(dst)
                    for column in ("file_path", "selfcheck_path"):
                        old_value = str(row[column] or "")
                        if old_value and old_value.startswith(str(src)):
                            updates[column] = str(dst) + old_value[len(str(src)):]
                connection.execute(
                    "UPDATE issues SET "
                    + ", ".join(f"{c} = ?" for c in updates)
                    + " WHERE issue_id = ?",
                    (*updates.values(), str(row["issue_id"])),
                )
    except Exception:
        if renamed and dst.exists() and not src.exists():
            dst.rename(src)
        raise
    return result


def cmd_issue_recipients(payload: dict) -> dict:
    """Change the recipient of a batch: write the ledger and rename the batch folder with it (2026-09-21).

    When filing, a person got recorded as a company ("North Bay Contractors" should have been
    "Ben Zhao"), or the wrong name was simply picked. The recipient is part of the address
    (outbox\\<date-recipient-subject>), so changing it = rename + ledger change, both together --
    folder names belong to the program, the shape is always computed by make_batch_folder_name.
    The key is `batch_id`, or `issue_id` for old records without one; every row of the batch changes.
    The ledger changes deliveries.recipients and the three paths (output_dir / file_path /
    selfcheck_path); the withdrawal entry is left alone, withdrawn files were moved to
    `_withdrawn` long ago, and paths not under this batch folder are left alone. The manifest
    is a snapshot of that moment and is not touched either.
    Dry run by default, apply=true to act; if the rename fails it is rolled back.
    """
    apply = bool(payload.get("apply"))
    key = str(payload.get("batch") or payload.get("batch_id") or "").strip()
    if not key:
        raise ValueError("Which batch? Pass batch (batch_id; issue_id for old records).")
    recipients = validate_free_text(str(payload.get("recipients") or ""), "Recipient")
    ledger = _ledger(payload)

    with ledger.session() as connection:
        rows = list(
            connection.execute(
                "SELECT i.issue_id, i.batch_id, i.issue_date, i.project_id, i.stage,"
                " i.output_dir, i.file_path, i.file_name, i.selfcheck_path, i.status,"
                " (SELECT d.recipients FROM deliveries d WHERE d.issue_id = i.issue_id"
                "  AND d.action != 'withdrawn' ORDER BY d.recorded_at DESC, d.rowid DESC LIMIT 1)"
                "   AS recipients,"
                " (SELECT d.subject FROM deliveries d WHERE d.issue_id = i.issue_id"
                "  AND d.action != 'withdrawn' ORDER BY d.recorded_at DESC, d.rowid DESC LIMIT 1)"
                "   AS subject"
                " FROM issues i WHERE i.batch_id = ? OR i.issue_id = ?"
                " ORDER BY i.created_at, i.rowid",
                (key, key),
            )
        )
    if not rows:
        raise ValueError(f"No such batch in the ledger: {key}")

    head = rows[0]
    old_dir = Path(str(head["output_dir"]))
    subject = str(head["subject"] or "")
    plan = {
        "batch": str(head["batch_id"] or head["issue_id"]),
        "issue_date": str(head["issue_date"]),
        "was": str(head["recipients"] or ""),
        "now": recipients,
        "subject": subject,
        "from": str(old_dir),
        "to": str(old_dir),
        "file_count": len(rows),
        "files": [str(r["file_name"]) for r in rows],
    }
    if not old_dir.is_dir():
        raise ValueError(f"Batch folder is not on disk; resync before changing the recipient: {old_dir}")

    new_name = make_batch_folder_name(
        _parse_date(str(head["issue_date"]), "Issue date"), recipients, subject
    )
    if new_name != old_dir.name:
        new_dir = unique_batch_dir(old_dir.parent, new_name)
        for child in old_dir.iterdir():
            ensure_path_length(new_dir / child.name)
        plan["to"] = str(new_dir)

    result = {"dry_run": not apply, "plan": plan}
    if not apply:
        return result

    src, dst = Path(plan["from"]), Path(plan["to"])
    renamed = False
    if src != dst:
        if dst.exists():
            raise ValueError(f"Target already exists, refusing to overwrite: {dst}")
        src.rename(dst)
        renamed = True
    try:
        with ledger.immediate_transaction() as connection:
            for row in rows:
                updates: dict[str, str] = {}
                if src != dst:
                    # When the whole batch was withdrawn, output_dir points at _withdrawn, not this batch folder
                    if str(row["output_dir"] or "") == str(src):
                        updates["output_dir"] = str(dst)
                    for column in ("file_path", "selfcheck_path"):
                        old_value = str(row[column] or "")
                        if old_value and old_value.startswith(str(src)):
                            updates[column] = str(dst) + old_value[len(str(src)):]
                if updates:
                    connection.execute(
                        "UPDATE issues SET "
                        + ", ".join(f"{c} = ?" for c in updates)
                        + " WHERE issue_id = ?",
                        (*updates.values(), str(row["issue_id"])),
                    )
                connection.execute(
                    "UPDATE deliveries SET recipients = ?"
                    " WHERE issue_id = ? AND action != 'withdrawn'",
                    (recipients, str(row["issue_id"])),
                )
    except Exception:
        if renamed and dst.exists() and not src.exists():
            dst.rename(src)
        raise
    return result


def cmd_intake_resync(payload: dict) -> dict:
    """Disk is the truth: recompute one intake's file list.

    Intake is meant to be add-only, but people do add files to the batch folder, open and
    save a CAD file in place, move things around. The truth is on disk -- the files are the
    real thing, the ledger is the books; when the books don't match, fix the books, not the
    other way round by deleting someone's files. This command rescans archive_dir, recomputes
    each file's size and SHA-256, rebuilds intake_files, updates file_count / total_size /
    content_signature along the way, and notes in `notes` when it was reconciled with the disk.

    Unlike a regular intake, nothing is rejected here -- 0-byte files, temp files, .bak are all
    recorded; they are already in the batch folder, and recording them is how they become
    visible. Anything odd goes into warnings.
    Dry run by default; apply=true writes the ledger. Without intake_ids the whole ledger is checked.
    """
    apply = bool(payload.get("apply"))
    only = {str(x).strip() for x in (payload.get("intake_ids") or []) if str(x).strip()}
    ledger = _ledger(payload)
    checked_at = datetime.now().astimezone().isoformat(timespec="seconds")

    def sha256_of(path: Path) -> str:
        digest = hashlib.sha256()
        with open(path, "rb") as handle:
            for block in iter(lambda: handle.read(1 << 20), b""):
                digest.update(block)
        return digest.hexdigest().upper()

    changes: list[dict] = []
    problems: list[str] = []
    with ledger.session() as connection:
        rows = list(
            connection.execute(
                "SELECT intake_id, received_date, source_org, subject, archive_dir,"
                " file_count, total_size, notes, status FROM intakes ORDER BY created_at"
            )
        )
        old_files = {}
        for row in connection.execute(
            "SELECT intake_id, archived_name, source_hash FROM intake_files"
        ):
            old_files.setdefault(str(row["intake_id"]), {})[
                str(row["archived_name"]).replace("\\", "/")
            ] = str(row["source_hash"]).upper()

    for row in rows:
        intake_id = str(row["intake_id"])
        if only and intake_id not in only:
            continue
        directory = Path(str(row["archive_dir"]))
        tag = (
            f"{intake_id[:8]} {row['received_date']} {row['source_org']}"
            f"·{row['subject'] or '(no subject)'}"
        )
        if not directory.is_dir():
            if row["status"] == "filed":
                problems.append(f"Batch folder is gone ({tag}): {directory}")
            continue

        entries: list[dict] = []
        warnings: list[str] = []
        for item in sorted(directory.rglob("*")):
            if not item.is_file():
                continue
            name = str(item.relative_to(directory)).replace("\\", "/")
            size = item.stat().st_size
            # Hash 0-byte files too: an empty file's sha256 is a fixed value, leaving it blank wouldn't match elsewhere
            digest = sha256_of(item)
            if not size:
                warnings.append(f"{name}: 0 bytes, recorded as is")
            entries.append(
                {
                    "archived_name": name,
                    "extension": item.suffix.lower(),
                    "file_size": size,
                    "source_hash": digest,
                }
            )

        was = old_files.get(intake_id, {})
        now = {e["archived_name"]: e["source_hash"] for e in entries}
        added = sorted(set(now) - set(was))
        removed = sorted(set(was) - set(now))
        rehashed = sorted(n for n in set(now) & set(was) if now[n] != was[n])
        if not (added or removed or rehashed):
            continue
        changes.append(
            {
                "intake_id": intake_id,
                "tag": tag,
                "archive_dir": str(directory),
                "added": added,
                "removed": removed,
                "rehashed": rehashed,
                "file_count": len(entries),
                "total_size": sum(e["file_size"] for e in entries),
                "warnings": warnings,
                "_entries": entries,
                "_notes": str(row["notes"] or ""),
            }
        )

    view = [
        {k: v for k, v in item.items() if not k.startswith("_")} for item in changes
    ]
    result = {"dry_run": not apply, "changes": view, "problems": problems}
    if not apply:
        return result

    with ledger.immediate_transaction() as connection:
        for item in changes:
            entries = item["_entries"]
            connection.execute(
                "DELETE FROM intake_files WHERE intake_id = ?", (item["intake_id"],)
            )
            connection.executemany(
                "INSERT INTO intake_files(intake_id, source_path, original_name,"
                " archived_name, extension, file_size, source_hash, checks_json,"
                " warnings_json) VALUES(?, '', ?, ?, ?, ?, ?, ?, ?)",
                [
                    (
                        item["intake_id"],
                        Path(e["archived_name"]).name,
                        e["archived_name"],
                        e["extension"],
                        e["file_size"],
                        e["source_hash"],
                        json.dumps(["Reconciled with disk; intake structural checks not run"], ensure_ascii=False),
                        json.dumps([], ensure_ascii=False),
                    )
                    for e in entries
                ],
            )
            # Content signature uses the intake gate's algorithm: sort all file hashes, then hash again
            signature = hashlib.sha256(
                json.dumps(
                    sorted(e["source_hash"] for e in entries),
                    ensure_ascii=False,
                    separators=(",", ":"),
                ).encode("utf-8")
            ).hexdigest().upper()
            note = (str(item["_notes"]) + " " if item["_notes"] else "") + (
                f"{checked_at[:10]} reconciled with disk: file list recomputed from what is in the batch folder"
                f" ({len(item['added'])} added, {len(item['removed'])} gone,"
                f" {len(item['rehashed'])} changed)."
            )
            connection.execute(
                "UPDATE intakes SET file_count = ?, total_size = ?,"
                " content_signature = ?, notes = ? WHERE intake_id = ?",
                (
                    item["file_count"],
                    item["total_size"],
                    signature,
                    note,
                    item["intake_id"],
                ),
            )
    return result


# Before 2026-08-15 issues went "package + issue manifest .json"; loose files in those batch
# folders were left there by the old program
PACKAGED_ERA_END = "2026-08-15"


def cmd_issue_resync(payload: dict) -> dict:
    """Disk is the truth: recompute one issue batch's file list.

    Same idea as intake-resync, but issues have one more layer: one ledger row per file, so a
    missing file means pulling a row and a replaced file means changing one. Rows that are
    "in the ledger but gone from disk" are, in order, repointed at files that are "on disk but
    not in the ledger" (name, path and hash change together), and leftover dead rows are
    pulled out by the root (via Ledger.delete_issue, which also unlinks deliveries and intakes).

    Extra files on disk with no dead row to take them don't get a made-up ledger entry: they
    are reported, that case belongs to a regular issue. The self-check notes are not compared
    (they are each batch's fixed cover document, not a deliverable).
    Dry run by default; apply=true to act. Without output_dirs the whole library is checked.
    """
    apply = bool(payload.get("apply"))
    only = {str(x).strip().casefold() for x in (payload.get("output_dirs") or [])}
    config = _config(payload)
    gate = OutboundGate(config.library_root, config.ledger_path)
    ledger = gate.ledger

    def sha256_of(path: Path) -> str:
        digest = hashlib.sha256()
        with open(path, "rb") as handle:
            for block in iter(lambda: handle.read(1 << 20), b""):
                digest.update(block)
        return digest.hexdigest().upper()

    with ledger.session() as connection:
        rows = list(
            connection.execute(
                "SELECT issue_id, issue_date, output_dir, file_path, file_name,"
                " file_hash, selfcheck_path, status FROM issues"
                " WHERE status = 'issued' ORDER BY created_at"
            )
        )

    batches: dict[str, list] = {}
    for row in rows:
        batches.setdefault(str(row["output_dir"]), []).append(row)

    plans: list[dict] = []
    problems: list[str] = []
    old_era: list[str] = []
    for out_dir, group in batches.items():
        if only and out_dir.casefold() not in only:
            continue
        # Before 2026-08-15 issues had the old "zip + issue manifest .json" shape:
        # loose files in the batch folder were left by the old program, not added later by a person; comparing is pointless
        if min(str(r["issue_date"]) for r in group) < PACKAGED_ERA_END:
            old_era.append(out_dir)
            continue
        directory = Path(out_dir)
        if not directory.is_dir():
            problems.append(f"Batch folder is gone: {out_dir}")
            continue
        selfchecks = {
            Path(str(r["selfcheck_path"])).name for r in group if r["selfcheck_path"]
        }
        on_disk = {
            q.name: q
            for q in sorted(directory.iterdir())
            if q.is_file() and q.name not in selfchecks
        }
        named = {str(r["file_name"]): r for r in group}
        new_files = [n for n in on_disk if n not in named]
        dead_rows = [named[n] for n in sorted(named) if n not in on_disk]
        if not new_files and not dead_rows:
            continue
        if len(new_files) > len(dead_rows):
            problems.append(
                f"{len(new_files)} extra files on disk but only {len(dead_rows)} empty rows in the ledger to reuse;"
                f" the extra ones need a regular issue: {out_dir}"
            )
            continue
        reassign = [
            {
                "issue_id": str(dead_rows[i]["issue_id"]),
                "from": str(dead_rows[i]["file_name"]),
                "to": name,
            }
            for i, name in enumerate(new_files)
        ]
        drop = [
            {"issue_id": str(r["issue_id"]), "file_name": str(r["file_name"])}
            for r in dead_rows[len(new_files):]
        ]
        plans.append(
            {
                "output_dir": out_dir,
                "reassign": reassign,
                "drop": drop,
                "_disk": {n: on_disk[n] for n in new_files},
            }
        )

    view = [{k: v for k, v in q.items() if not k.startswith("_")} for q in plans]
    result = {
        "dry_run": not apply,
        "plans": view,
        "problems": problems,
        "skipped_old_era": old_era,
    }
    if not apply:
        return result
    if problems:
        raise ValueError("Some batches don't match up; sort them out before reconciling: " + "; ".join(problems))

    for plan in plans:
        with ledger.immediate_transaction() as connection:
            for item in plan["reassign"]:
                target = plan["_disk"][item["to"]]
                digest = sha256_of(target)
                connection.execute(
                    "UPDATE issues SET file_name = ?, file_path = ?,"
                    " file_hash = ?, file_stem = ? WHERE issue_id = ?",
                    (target.name, str(target), digest, target.stem, item["issue_id"]),
                )
        # Pulling rows must go through Ledger.delete_issue: deliveries, file rows and intake links are unlinked together
        for item in plan["drop"]:
            ledger.delete_issue(item["issue_id"])
    return result


# ---------------------------------------------------------------- record detail pages
# A ledger row opens into a page (2026-09-08): the list only carries enough columns to tell
# which entry it is; the whole story is in the detail.
# Intake detail: the intake itself, its files, the issue it replies to, every issue batch hanging on it.
# Issue detail: every file in the batch (each with its withdrawal status), every intake it
# answers, the delivery log, the self-check notes text.
# Folders are only given as addresses; the UI turns them into links (open-path), this side never opens Explorer.

def _recipients_and_notes(connection, issue_id: str) -> tuple[str, str, str]:
    """This batch's recipient / subject / notes -- all three are on the delivery log; take the latest non-withdrawal one."""
    row = connection.execute(
        "SELECT recipients, subject, notes FROM deliveries"
        " WHERE issue_id = ? AND action != 'withdrawn'"
        " ORDER BY recorded_at DESC, rowid DESC LIMIT 1",
        (issue_id,),
    ).fetchone()
    if row is None:
        return ("", "", "")
    return (str(row["recipients"]), str(row["subject"] or ""), str(row["notes"]))


def _batch_summary(connection, issue_id: str) -> dict | None:
    """One issue batch as one entry (same rules as cmd_issues), for the "in reply to / hanging issues" rows of the detail page."""
    head = connection.execute("SELECT * FROM issues WHERE issue_id = ?", (issue_id,)).fetchone()
    if head is None:
        return None
    if head["batch_id"]:
        members = connection.execute(
            "SELECT issue_id, file_name, status FROM issues WHERE batch_id = ?"
            " ORDER BY created_at, rowid",
            (head["batch_id"],),
        ).fetchall()
    else:
        members = [head]
    statuses = {str(m["status"]) for m in members}
    recipients, subject, notes = _recipients_and_notes(
        connection, str(members[0]["issue_id"])
    )
    return {
        "issue_id": str(members[0]["issue_id"]),
        "batch_id": str(head["batch_id"] or ""),
        "issue_date": str(head["issue_date"]),
        "stage": str(head["stage"]),
        "output_dir": str(head["output_dir"]),
        "recipients": recipients,
        "subject": subject,
        "notes": notes,
        "files": [str(m["file_name"]) for m in members],
        "status": statuses.pop() if len(statuses) == 1 else "partly withdrawn",
    }


def cmd_intake_detail(payload: dict) -> dict:
    _inbound(payload)
    ledger = _ledger(payload)
    intake_id = str(payload.get("intake_id") or "").strip()
    if not intake_id:
        raise ValueError("Which intake? Pass intake_id.")
    with ledger.session() as connection:
        row = connection.execute(
            "SELECT i.*, COALESCE(NULLIF(p.folder, ''), i.project_id) AS folder"
            " FROM intakes i LEFT JOIN projects p ON p.project_id = i.project_id"
            " WHERE i.intake_id = ?",
            (intake_id,),
        ).fetchone()
        if row is None:
            raise ValueError("Intake record not found.")
        files = _rows(
            connection.execute(
                "SELECT archived_name AS name, original_name, extension, file_size AS size,"
                " source_hash AS hash, source_path"
                " FROM intake_files WHERE intake_id = ? ORDER BY file_id",
                (intake_id,),
            )
        )
        replies_to = _batch_summary(connection, str(row["replies_to"])) if row["replies_to"] else None
        # Issues hanging on this intake: each batch reported once (the link table has one row per file)
        seen: set[str] = set()
        issues: list[dict] = []
        for link in connection.execute(
            "SELECT x.issue_id FROM issue_intakes x JOIN issues i ON i.issue_id = x.issue_id"
            " WHERE x.intake_id = ? ORDER BY i.created_at, i.rowid",
            (intake_id,),
        ):
            batch = _batch_summary(connection, str(link["issue_id"]))
            if batch is None:
                continue
            key = batch["batch_id"] or batch["issue_id"]
            if key in seen:
                continue
            seen.add(key)
            issues.append(batch)
    intake = {k: row[k] for k in row.keys() if k not in ("manifest_json", "content_signature")}
    return {"intake": intake, "files": files, "replies_to": replies_to, "issues": issues}


def cmd_issue_detail(payload: dict) -> dict:
    _inbound(payload)
    ledger = _ledger(payload)
    issue_id = str(payload.get("issue_id") or "").strip()
    if not issue_id:
        raise ValueError("Which issue? Pass issue_id (any file's in the batch will do).")
    with ledger.session() as connection:
        head = connection.execute(
            "SELECT i.*, COALESCE(NULLIF(p.folder, ''), i.project_id) AS folder"
            " FROM issues i LEFT JOIN projects p ON p.project_id = i.project_id"
            " WHERE i.issue_id = ?",
            (issue_id,),
        ).fetchone()
        if head is None:
            raise ValueError("Issue record not found.")
        if head["batch_id"]:
            members = connection.execute(
                "SELECT issue_id, file_name, file_path, file_hash, status, role FROM issues"
                " WHERE batch_id = ? ORDER BY created_at, rowid",
                (head["batch_id"],),
            ).fetchall()
        else:
            members = [head]
        files = []
        for m in members:
            size = connection.execute(
                "SELECT file_size FROM issue_files WHERE issue_id = ? ORDER BY file_id LIMIT 1",
                (m["issue_id"],),
            ).fetchone()
            files.append(
                {
                    "issue_id": str(m["issue_id"]),
                    "name": str(m["file_name"]),
                    "path": str(m["file_path"]),
                    "hash": str(m["file_hash"]),
                    "status": str(m["status"]),
                    "role": str(m["role"] or "sent"),
                    "size": int(size["file_size"]) if size else None,
                }
            )
        member_ids = [str(m["issue_id"]) for m in members]
        marks = ",".join("?" * len(member_ids))
        deliveries = _rows(
            connection.execute(
                f"SELECT d.issue_id, d.recorded_at, d.purpose, d.channels_json, d.recipients,"
                f" d.subject, d.notes, d.action, i.file_name FROM deliveries d"
                f" JOIN issues i ON i.issue_id = d.issue_id"
                f" WHERE d.issue_id IN ({marks}) ORDER BY d.recorded_at, d.rowid",
                member_ids,
            )
        )
        for d in deliveries:
            try:
                d["channels"] = json.loads(d.pop("channels_json") or "[]")
            except ValueError:
                d["channels"] = []
        # Intakes it answers: shared by the whole batch, taken from the first file's links; old records without link rows fall back to issues.intake_id
        intake_ids = [
            str(r["intake_id"])
            for r in connection.execute(
                "SELECT intake_id FROM issue_intakes WHERE issue_id = ? ORDER BY rowid",
                (member_ids[0],),
            )
        ] or ([str(head["intake_id"])] if head["intake_id"] else [])
        intakes = []
        for iid in intake_ids:
            r = connection.execute(
                "SELECT intake_id, received_date, source_org, source_person, subject,"
                " archive_dir, status, file_count FROM intakes WHERE intake_id = ?",
                (iid,),
            ).fetchone()
            intakes.append(dict(r) if r else {"intake_id": iid, "missing": True})
        # Later intakes "in reply to" this issue: the chain must be visible going forward too
        followups = _rows(
            connection.execute(
                f"SELECT intake_id, received_date, source_org, source_person, subject,"
                f" archive_dir, status, file_count FROM intakes"
                f" WHERE replies_to IN ({marks}) ORDER BY received_date, created_at",
                member_ids,
            )
        )
    statuses = {f["status"] for f in files}
    recipients, subject, notes = ("", "", "")
    for d in reversed(deliveries):
        if d["action"] != "withdrawn":
            recipients = str(d["recipients"])
            subject = str(d["subject"] or "")
            notes = str(d["notes"])
            break
    issue = {
        k: head[k] for k in head.keys()
        if k not in ("manifest_json", "file_name", "file_stem", "file_path", "file_hash")
    }
    issue.update(
        {
            "status": statuses.pop() if len(statuses) == 1 else "partly withdrawn",
            "recipients": recipients,
            "subject": subject,
            "notes": notes,
            "issue_ids": member_ids,
        }
    )
    selfcheck = ""
    sc_path = Path(str(head["selfcheck_path"] or ""))
    if str(head["selfcheck_path"]) and sc_path.is_file():
        try:
            selfcheck = sc_path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            selfcheck = ""
    return {
        "issue": issue,
        "files": files,
        "intakes": intakes,
        "followups": followups,
        "deliveries": deliveries,
        "selfcheck": {"path": str(head["selfcheck_path"] or ""), "exists": bool(selfcheck), "text": selfcheck},
    }


COMMANDS = {
    "context": cmd_context,
    "places": cmd_places,
    "project-create": cmd_project_create,
    "project-bind": cmd_project_bind,
    "project-rename": cmd_project_rename,
    "project-stages": cmd_project_stages,
    "project-overview": cmd_project_overview,
    "intake-suggestions": cmd_intake_suggestions,
    "intakes": cmd_intakes,
    "intake-archive": cmd_intake_archive,
    "intake-brief": cmd_intake_brief,
    "intake-detail": cmd_intake_detail,
    "issue-detail": cmd_issue_detail,
    "intake-resync": cmd_intake_resync,
    "issue-resync": cmd_issue_resync,
    "withdraw-intake": cmd_withdraw_intake,
    "intake-restage": cmd_intake_restage,
    "intake-source": cmd_intake_source,
    "delete-intakes": cmd_delete_intakes,
    "delete-issues": cmd_delete_issues,
    "set-todo": cmd_set_todo,
    "issue-suggestions": cmd_issue_suggestions,
    "spec-read": cmd_spec_read,
    "template-save": cmd_template_save,
    "library-check": cmd_library_check,
    "library-check-export": cmd_library_check_export,
    "contacts": cmd_contacts,
    "contact-save": cmd_contact_save,
    "contact-delete": cmd_contact_delete,
    "standards": cmd_standards,
    "standard-save": cmd_standard_save,
    "issue-create": cmd_issue_create,
    "issues": cmd_issues,
    "withdraw-issue": cmd_withdraw_issue,
    "relink-issue": cmd_relink_issue,
    "undoable": cmd_undoable,
    "selfcheck-draft": cmd_selfcheck_draft,
    "selfcheck-read": cmd_selfcheck_read,
    "selfcheck-rename": cmd_selfcheck_rename,
    "issue-subject": cmd_issue_subject,
    "issue-redate": cmd_issue_redate,
    "issue-recipients": cmd_issue_recipients,
    "issue-role": cmd_issue_role,
}


def main(argv: list[str]) -> int:
    if not argv or argv[0] not in COMMANDS:
        payload = {"ok": False, "error": "Unknown command. Available: " + ", ".join(COMMANDS)}
        sys.stdout.buffer.write(json.dumps(payload, ensure_ascii=False).encode("utf-8"))
        sys.stdout.buffer.write(b"\n")
        return 2
    args: dict = {}
    if len(argv) > 1 and argv[1]:
        # utf-8-sig: JSON written by hand or by PowerShell often carries a BOM; it must be tolerated here.
        args = json.loads(io.open(argv[1], encoding="utf-8-sig").read() or "{}")
    try:
        result = {"ok": True, "data": COMMANDS[argv[0]](args)}
    except Exception as exc:  # the front end only understands JSON, so exceptions are wrapped in JSON too
        result = {"ok": False, "error": str(exc), "type": type(exc).__name__}
    sys.stdout.buffer.write(json.dumps(result, ensure_ascii=False).encode("utf-8"))
    sys.stdout.buffer.write(b"\n")
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
