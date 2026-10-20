r"""Fixed positions inside a project room: addresses are computed, never stored.

The library = one room per project under the root (folder named by its short name, registered
in projects.folder). Since 2026-08-20 a room only holds stage folders, and inbox/outbox hang
under the stage -- every document belongs to a stage:

    <root>\<short name>\<stage>\inbox\<date-source-subject>\     evidence, add-only
    <root>\<short name>\<stage>\current\                         the single current version, work happens here
    <root>\<short name>\<stage>\outbox\<date-recipient-subject>\  evidence, add-only

A project without a registered short name falls back to its ID as the folder name; intake
that belongs to no project goes to `_general\`.
"""
from __future__ import annotations

import sqlite3
from pathlib import Path


GENERIC_FOLDER = "_general"
INBOX_FOLDER = "inbox"
OUTBOX_FOLDER = "outbox"
POOL_FOLDER = "current"
WITHDRAWN_FOLDER = "_withdrawn"
# Where this project's glue scripts live (spec section 2). A tool's own run logs and caches
# are part of it; the health check does not report .log files under this tree (2026-09-11).
TOOLS_FOLDER = "tools"
# The fourth position under a stage: process files, dropped drafts, old material (spec section 2).
# Unrelated to the ledger's "archive" flag (intakes/issues.archived): since 2026-09-08 that
# flag is only a mark and never moves files.
ARCHIVE_FOLDER = "archive"
# Where change-notes drafts live at the library root (2026-09-26): one
# <id>-<stage>-change-notes.md per draft; once an issue is filed and the copy in the batch
# matches byte for byte, the program deletes the draft.
# Not the same as the root's .tmp\ -- that one is the gates' copy area and should always be empty.
DRAFT_FOLDER = "_drafts"


def folder_in_connection(connection: sqlite3.Connection, project_id: str) -> str:
    """A project room's folder name. Not found (old ledger, not registered) falls back to the ID."""
    if not project_id:
        return GENERIC_FOLDER
    try:
        row = connection.execute(
            "SELECT folder FROM projects WHERE project_id = ? COLLATE NOCASE",
            (project_id,),
        ).fetchone()
    except sqlite3.OperationalError:
        return project_id
    folder = row["folder"] if row else ""
    return str(folder) if folder else project_id


def project_dir(root: Path | str, folder: str) -> Path:
    return Path(root) / folder


def stage_dir(root: Path | str, folder: str, stage: str) -> Path:
    """The level above the three positions: room\\stage."""
    return Path(root) / folder / stage


def inbox_dir(root: Path | str, folder: str, stage: str) -> Path:
    return stage_dir(root, folder, stage) / INBOX_FOLDER


def outbox_dir(root: Path | str, folder: str, stage: str) -> Path:
    return stage_dir(root, folder, stage) / OUTBOX_FOLDER


def pool_dir(root: Path | str, folder: str, stage: str) -> Path:
    return stage_dir(root, folder, stage) / POOL_FOLDER


# CAD autosaves, Office lock files, system junk: not counted as files in the current folder
NOISE_SUFFIXES = {".bak", ".dwl", ".dwl2", ".tmp", ".sv$", ".ac$"}
NOISE_NAMES = {"thumbs.db", "desktop.ini"}


def is_noise(path: Path) -> bool:
    if path.suffix.lower() in NOISE_SUFFIXES:
        return True
    if path.name.lower() in NOISE_NAMES:
        return True
    return path.name.startswith("~$")


def count_pool_files(pool: Path) -> int:
    if not pool.is_dir():
        return 0
    return sum(1 for p in pool.rglob("*") if p.is_file() and not is_noise(p))
