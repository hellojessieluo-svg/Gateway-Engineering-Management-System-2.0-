from __future__ import annotations

import csv
import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Iterator

from common.constants import STAGES, STAGE_SEPARATOR


SCHEMA_VERSION = 11

# Stable temporary IDs for projects that have no official ID yet: TMP-001, TMP-002...
# ASCII prefix since 2026-08-30: the ID ends up in file names, command lines and folder
# names, and non-ASCII characters need coaxing everywhere there (code pages, escaping,
# sorting). Old IDs in the ledger are changed over with project-bind.
PROVISIONAL_PREFIX = "TMP-"


def provisional_number(project_id: str) -> int | None:
    """The sequence number inside a temporary ID; None if it is not a temporary ID."""
    text = str(project_id)
    for prefix in (PROVISIONAL_PREFIX,):
        if text.startswith(prefix):
            suffix = text[len(prefix):]
            if suffix.isdigit():
                return int(suffix)
    return None


# Issue ledger (2026-09-03 cleanup). One row = one file, one stage, issued once on one day;
# the files sent with one click share a batch_id. No version numbers, no packaging, no categories.
#   file_name / file_stem / file_path / file_hash: the issued file itself (its name is its own)
#   output_dir: the batch folder; selfcheck_path: the one change-notes file shared by the batch
#   intake_id: the first intake replied to; all of them are linked in issue_intakes
#   role: this file's role (2026-09-22) -- "sent" actually went to the recipient, "kept" is only
#         stored with the batch and was not sent. Old rows are all backfilled as "sent". It is
#         only a word in the ledger and does not change location: both kinds sit in the same batch folder.
ISSUES_BODY = """(
    issue_id TEXT PRIMARY KEY,
    created_at TEXT NOT NULL,
    project_id TEXT NOT NULL COLLATE NOCASE,
    project_name TEXT NOT NULL DEFAULT '',
    stage TEXT NOT NULL,
    issue_date TEXT NOT NULL,
    batch_id TEXT NOT NULL DEFAULT '',
    file_name TEXT NOT NULL,
    file_stem TEXT NOT NULL,
    file_path TEXT NOT NULL,
    file_hash TEXT NOT NULL,
    output_dir TEXT NOT NULL,
    selfcheck_path TEXT NOT NULL DEFAULT '',
    intake_id TEXT NOT NULL DEFAULT '',
    archived INTEGER NOT NULL DEFAULT 0,
    status TEXT NOT NULL DEFAULT 'issued',
    manifest_json TEXT NOT NULL,
    role TEXT NOT NULL DEFAULT 'sent'
)"""

# Before 2026-08-15 one issue was a zip package, with one row per source file inside it; since
# then one ledger row is one file, and this holds that file's source path and check results.
ISSUE_FILES_BODY = """(
    file_id INTEGER PRIMARY KEY AUTOINCREMENT,
    issue_id TEXT NOT NULL REFERENCES issues(issue_id),
    source_path TEXT NOT NULL,
    original_name TEXT NOT NULL,
    file_name TEXT NOT NULL,
    extension TEXT NOT NULL,
    file_size INTEGER NOT NULL,
    source_hash TEXT NOT NULL,
    checks_json TEXT NOT NULL,
    UNIQUE(issue_id, file_name)
)"""

SCHEMA = f"""
CREATE TABLE IF NOT EXISTS metadata (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS projects (
    project_id TEXT PRIMARY KEY COLLATE NOCASE,
    project_name TEXT NOT NULL DEFAULT '',
    folder TEXT NOT NULL DEFAULT '',
    stages TEXT NOT NULL DEFAULT '',
    active INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS issues {ISSUES_BODY};
CREATE INDEX IF NOT EXISTS ix_issues_file
    ON issues(project_id, file_stem, stage, file_hash);
CREATE INDEX IF NOT EXISTS ix_issues_batch ON issues(batch_id);

CREATE TABLE IF NOT EXISTS issue_files {ISSUE_FILES_BODY};
CREATE INDEX IF NOT EXISTS ix_issue_files_hash ON issue_files(source_hash);

CREATE TABLE IF NOT EXISTS deliveries (
    delivery_id TEXT PRIMARY KEY,
    issue_id TEXT NOT NULL REFERENCES issues(issue_id),
    recorded_at TEXT NOT NULL,
    purpose TEXT NOT NULL,
    channels_json TEXT NOT NULL,
    recipients TEXT NOT NULL DEFAULT '',
    subject TEXT NOT NULL DEFAULT '',
    notes TEXT NOT NULL DEFAULT '',
    action TEXT NOT NULL
);

-- 2026-08-20: several intakes can be replied to; many-to-many goes through this link table. issues.intake_id keeps the first one.
CREATE TABLE IF NOT EXISTS issue_intakes (
    issue_id TEXT NOT NULL REFERENCES issues(issue_id),
    intake_id TEXT NOT NULL,
    UNIQUE(issue_id, intake_id)
);
CREATE INDEX IF NOT EXISTS ix_issue_intakes_intake ON issue_intakes(intake_id);
"""

# Column names on issues before the cleanup -> their names now. version, after_commit_id,
# task_id and content_signature, left over from the version gate / task gate / packaging eras,
# have no counterpart any more and are dropped.
LEGACY_ISSUE_COLUMNS = {
    "package_name": "file_name",
    "section": "file_stem",
    "package_path": "file_path",
    "package_hash": "file_hash",
}
LEGACY_TABLES = ("pools", "commits", "commit_files", "commit_basis", "tasks")


class Ledger:
    def __init__(self, database_path: Path | str):
        self.database_path = Path(database_path)

    def connect(self) -> sqlite3.Connection:
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(self.database_path, timeout=30)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA journal_mode = WAL")
        return connection

    @contextmanager
    def session(self) -> Iterator[sqlite3.Connection]:
        connection = self.connect()
        try:
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    @contextmanager
    def immediate_transaction(self) -> Iterator[sqlite3.Connection]:
        connection = self.connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    # ------------------------------------------------------------ create / upgrade

    def initialize(self) -> None:
        self._rebuild_legacy()
        with self.session() as connection:
            connection.executescript(SCHEMA)
            # 2026-09-11: the outbox folder name gained the subject; like the recipient it is
            # recorded on the delivery log. The CREATE statements are IF NOT EXISTS, so existing
            # old ledgers can only get the column added (old rows have an empty subject).
            if "subject" not in self._columns(connection, "deliveries"):
                connection.execute(
                    "ALTER TABLE deliveries ADD COLUMN subject TEXT NOT NULL DEFAULT ''"
                )
            # 2026-09-22: every issued file gained a role (sent / kept). Old rows are all
            # backfilled as "sent" -- everything issued before then really was sent; kept files
            # did not exist as a concept yet.
            if "role" not in self._columns(connection, "issues"):
                connection.execute(
                    "ALTER TABLE issues ADD COLUMN role TEXT NOT NULL DEFAULT 'sent'"
                )
            connection.execute(
                "INSERT OR REPLACE INTO metadata(key, value)"
                " VALUES('schema_version_outbound', ?)",
                (str(SCHEMA_VERSION),),
            )

    @staticmethod
    def _columns(connection: sqlite3.Connection, table: str) -> list[str]:
        return [row["name"] for row in connection.execute(f"PRAGMA table_info({table})")]

    def _rebuild_legacy(self) -> None:
        """2026-09-03, one-off: rebuild a ledger left by three earlier redesigns into the clean shape.

        An old ledger is recognized by issues still having a package_path column. Before
        touching anything, a <name>-backup.db copy is made with the backup API; the whole
        rebuild runs in one transaction and rolls back if foreign keys do not line up.
        Once every old ledger has been opened once, this function can go.
        """
        probe = self.connect()
        try:
            tables = {
                row["name"]
                for row in probe.execute("SELECT name FROM sqlite_master WHERE type='table'")
            }
            old_columns = self._columns(probe, "issues") if "issues" in tables else []
        finally:
            probe.close()
        if "package_path" not in old_columns:
            return

        backup = self.database_path.with_name(
            self.database_path.stem + "-backup" + self.database_path.suffix
        )
        # Under WAL, copying the .db directly misses commits, so the backup API is required;
        # connections must be closed explicitly, or on Windows the backup file stays locked
        src, dst = sqlite3.connect(str(self.database_path)), sqlite3.connect(str(backup))
        try:
            src.backup(dst)
        finally:
            dst.close()
            src.close()

        connection = self.connect()
        try:
            connection.execute("PRAGMA foreign_keys = OFF")
            connection.execute("BEGIN IMMEDIATE")

            # issues: copy by column name, old names mapped to new, missing columns get defaults
            connection.execute(f"CREATE TABLE issues_new {ISSUES_BODY}")
            targets = self._columns(connection, "issues_new")
            rename = {new: old for old, new in LEGACY_ISSUE_COLUMNS.items()}
            picks = []
            for column in targets:
                source = rename.get(column, column)
                if source in old_columns:
                    picks.append(source)
                else:
                    picks.append(
                        "0" if column == "archived" else "'sent'" if column == "role" else "''"
                    )
            connection.execute(
                f"INSERT INTO issues_new({', '.join(targets)})"
                f" SELECT {', '.join(picks)} FROM issues"
            )
            for index in ("ix_issues_chain", "ix_issues_content", "ix_issues_batch"):
                connection.execute(f"DROP INDEX IF EXISTS {index}")
            connection.execute("DROP TABLE issues")
            connection.execute("ALTER TABLE issues_new RENAME TO issues")

            # issue_files: drop the two category columns; generated_name is file_name
            if "issue_files" in tables:
                connection.execute(f"CREATE TABLE issue_files_new {ISSUE_FILES_BODY}")
                connection.execute(
                    "INSERT INTO issue_files_new(file_id, issue_id, source_path, original_name,"
                    " file_name, extension, file_size, source_hash, checks_json)"
                    " SELECT file_id, issue_id, source_path, original_name, generated_name,"
                    " extension, file_size, source_hash, checks_json FROM issue_files"
                )
                connection.execute("DROP INDEX IF EXISTS ix_issue_files_generated_name")
                connection.execute("DROP INDEX IF EXISTS ix_issue_files_hash")
                connection.execute("DROP TABLE issue_files")
                connection.execute("ALTER TABLE issue_files_new RENAME TO issue_files")

            for table in LEGACY_TABLES:
                connection.execute(f"DROP TABLE IF EXISTS {table}")
            if "metadata" in tables:
                connection.execute(
                    "DELETE FROM metadata WHERE key IN"
                    " ('schema_version_task', 'schema_version_version')"
                )
            # Add missing columns to projects (ledgers old enough to lack folder/stages)
            if "projects" in tables:
                project_columns = self._columns(connection, "projects")
                for column, ddl in (
                    ("active", "INTEGER NOT NULL DEFAULT 1"),
                    ("folder", "TEXT NOT NULL DEFAULT ''"),
                    ("stages", "TEXT NOT NULL DEFAULT ''"),
                ):
                    if column not in project_columns:
                        connection.execute(f"ALTER TABLE projects ADD COLUMN {column} {ddl}")
                        if column == "stages":
                            self._seed_stages_from_ledger(connection)

            broken = connection.execute("PRAGMA foreign_key_check").fetchall()
            if broken:
                raise RuntimeError(
                    "Foreign keys do not line up after rebuilding the issue ledger;"
                    " rolled back, the ledger was not changed."
                )
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.execute("PRAGMA foreign_keys = ON")
            connection.close()

    @staticmethod
    def _seed_stages_from_ledger(connection: sqlite3.Connection) -> None:
        """When the stages column is new, infer its initial value from stages already used in the ledger."""
        used: dict[str, set[str]] = {}
        for table in ("intakes", "issues"):
            try:
                rows = connection.execute(
                    f"SELECT DISTINCT project_id, stage FROM {table}"
                    " WHERE project_id <> '' AND stage <> ''"
                )
            except sqlite3.OperationalError:
                continue
            for row in rows:
                used.setdefault(str(row["project_id"]), set()).add(str(row["stage"]))
        if not used:
            return
        connection.executemany(
            "UPDATE projects SET stages = ? WHERE project_id = ? COLLATE NOCASE",
            [
                (STAGE_SEPARATOR.join(s for s in STAGES if s in stages), project_id)
                for project_id, stages in used.items()
            ],
        )

    # ------------------------------------------------------------ read

    def folder_of(self, project_id: str) -> str:
        """The project room's folder name; falls back to the ID if not registered (see places.folder_in_connection)."""
        from common.places import folder_in_connection

        with self.session() as connection:
            return folder_in_connection(connection, project_id)

    def find_same_content(
        self, project_id: str, file_stem: str, stage: str, file_hash: str
    ) -> sqlite3.Row | None:
        """Has this file, at this stage, with identical content, been issued before?

        If so, do not create another row; just record "sent again" under the original one.
        """
        with self.session() as connection:
            return connection.execute(
                """
                SELECT * FROM issues
                WHERE project_id = ? COLLATE NOCASE AND file_stem = ? AND stage = ?
                  AND file_hash = ? AND status = 'issued'
                ORDER BY created_at DESC LIMIT 1
                """,
                (project_id, file_stem, stage, file_hash),
            ).fetchone()

    def list_batch(self, batch_id: str) -> list[sqlite3.Row]:
        """The rows issued with one click. "Withdraw last" withdraws the whole batch."""
        if not batch_id:
            return []
        with self.session() as connection:
            return connection.execute(
                "SELECT * FROM issues WHERE batch_id = ? ORDER BY created_at, rowid",
                (batch_id,),
            ).fetchall()

    def get_intake(self, intake_id: str) -> sqlite3.Row | None:
        """The intake ledger is written by the intake gate; the issue gate only takes a look: is the linked intake still there, does it belong to this project."""
        if not intake_id:
            return None
        with self.session() as connection:
            try:
                return connection.execute(
                    "SELECT intake_id, project_id, project_name, status, source_org,"
                    " subject, received_date, archive_dir"
                    " FROM intakes WHERE intake_id = ?",
                    (intake_id,),
                ).fetchone()
            except sqlite3.OperationalError:
                return None

    def get_issue(self, issue_id: str) -> sqlite3.Row | None:
        with self.session() as connection:
            return connection.execute(
                "SELECT * FROM issues WHERE issue_id = ?", (issue_id,)
            ).fetchone()

    def get_issue_files(self, issue_id: str) -> list[sqlite3.Row]:
        with self.session() as connection:
            return connection.execute(
                "SELECT * FROM issue_files WHERE issue_id = ? ORDER BY file_id",
                (issue_id,),
            ).fetchall()

    def delivery_count(self, issue_id: str) -> int:
        with self.session() as connection:
            row = connection.execute(
                "SELECT COUNT(*) AS count FROM deliveries WHERE issue_id = ?",
                (issue_id,),
            ).fetchone()
            return int(row["count"])

    def list_recipients(self, limit: int = 50) -> list[str]:
        with self.session() as connection:
            rows = connection.execute(
                """
                SELECT recipients, MAX(recorded_at) AS last_used
                FROM deliveries WHERE recipients != ''
                GROUP BY recipients ORDER BY last_used DESC LIMIT ?
                """,
                (limit,),
            ).fetchall()
            return [str(row["recipients"]) for row in rows]

    def list_recent_issues(self, limit: int = 100) -> list[sqlite3.Row]:
        with self.session() as connection:
            return connection.execute(
                """
                SELECT i.*,
                       (SELECT COUNT(*) FROM deliveries d
                        WHERE d.issue_id = i.issue_id AND d.action != 'withdrawn')
                           AS delivery_count,
                       (SELECT d.recipients FROM deliveries d
                        WHERE d.issue_id = i.issue_id AND d.action != 'withdrawn'
                        ORDER BY d.recorded_at DESC, d.rowid DESC LIMIT 1)
                           AS latest_recipients
                FROM issues i
                ORDER BY i.created_at DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()

    # ------------------------------------------------------------ write

    def add_delivery(
        self,
        issue_id: str,
        delivery_id: str,
        recorded_at: str,
        purpose: str,
        channels: list[str],
        recipients: str,
        subject: str,
        notes: str,
        action: str,
    ) -> None:
        with self.session() as connection:
            connection.execute(
                """
                INSERT INTO deliveries(
                    delivery_id, issue_id, recorded_at, purpose,
                    channels_json, recipients, subject, notes, action
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    delivery_id,
                    issue_id,
                    recorded_at,
                    purpose,
                    json.dumps(channels, ensure_ascii=False),
                    recipients,
                    subject,
                    notes,
                    action,
                ),
            )

    def mark_withdrawn(
        self, issue_id: str, output_dir: str, file_path: str, withdrawn_at: str
    ) -> None:
        with self.immediate_transaction() as connection:
            cursor = connection.execute(
                """
                UPDATE issues
                SET status = 'withdrawn', output_dir = ?, file_path = ?
                WHERE issue_id = ? AND status = 'issued'
                """,
                (output_dir, file_path, issue_id),
            )
            if cursor.rowcount != 1:
                raise ValueError("The record does not exist or has already been withdrawn.")
            connection.execute(
                """
                INSERT INTO deliveries(
                    delivery_id, issue_id, recorded_at, purpose,
                    channels_json, recipients, subject, notes, action
                ) VALUES (lower(hex(randomblob(16))), ?, ?, 'issue', '[]', '', '', '', 'withdrawn')
                """,
                (issue_id, withdrawn_at),
            )

    def relink_issue(self, issue_ids: list[str], intake_ids: list[str]) -> int:
        """Change which intakes an existing issue is linked to. No file moves; only the link in the ledger changes.

        The list given is the **whole set** after the change; `issues.intake_id` keeps the
        first one. An empty set unlinks.
        """
        primary = intake_ids[0] if intake_ids else ""
        with self.immediate_transaction() as connection:
            for issue_id in issue_ids:
                cursor = connection.execute(
                    "UPDATE issues SET intake_id = ? WHERE issue_id = ?",
                    (primary, issue_id),
                )
                if cursor.rowcount != 1:
                    raise ValueError(f"Issue record not found: {issue_id}")
                connection.execute(
                    "DELETE FROM issue_intakes WHERE issue_id = ?", (issue_id,)
                )
                connection.executemany(
                    "INSERT INTO issue_intakes(issue_id, intake_id) VALUES (?, ?)",
                    [(issue_id, x) for x in intake_ids],
                )
            return len(issue_ids)

    def delete_issue(self, issue_id: str) -> int:
        """Erase this issue from the ledger, with its file rows and delivery log. Returns how many intakes were unlinked.

        A withdrawal leaves a trace, a deletion leaves none -- a record registered with a typo
        should never have been in the ledger. The caller moves the files into "_withdrawn"
        first; this only touches the database.
        """
        with self.immediate_transaction() as connection:
            connection.execute("DELETE FROM deliveries WHERE issue_id = ?", (issue_id,))
            connection.execute("DELETE FROM issue_files WHERE issue_id = ?", (issue_id,))
            connection.execute("DELETE FROM issue_intakes WHERE issue_id = ?", (issue_id,))
            unlinked = 0
            try:
                # The other end of the chain: intakes that say "I reply to this issue" get unlinked
                unlinked = connection.execute(
                    "UPDATE intakes SET replies_to = '' WHERE replies_to = ?", (issue_id,)
                ).rowcount
            except sqlite3.OperationalError:
                pass  # an issue-only ledger has no intakes table
            cursor = connection.execute(
                "DELETE FROM issues WHERE issue_id = ?", (issue_id,)
            )
            if cursor.rowcount != 1:
                raise ValueError("Issue record not found.")
            return unlinked

    # ------------------------------------------------------------ projects

    def seed_projects(self, projects: tuple[tuple[str, str], ...]) -> None:
        now = datetime.now().astimezone().isoformat(timespec="seconds")
        with self.session() as connection:
            for project_id, project_name in projects:
                connection.execute(
                    """
                    INSERT OR IGNORE INTO projects(
                        project_id, project_name, active, created_at, updated_at
                    ) VALUES (?, ?, 1, ?, ?)
                    """,
                    (project_id, project_name, now, now),
                )
                connection.execute(
                    """
                    UPDATE projects SET project_name = ?
                    WHERE project_id = ? COLLATE NOCASE AND project_name = ''
                    """,
                    (project_name, project_id),
                )

    def seed_project_names(self, project_names: tuple[str, ...]) -> None:
        """Give known projects without an official ID a stable temporary ID; existing records are left alone."""
        now = datetime.now().astimezone().isoformat(timespec="seconds")
        with self.immediate_transaction() as connection:
            rows = connection.execute("SELECT project_id FROM projects").fetchall()
            used_numbers = [
                number
                for row in rows
                if (number := provisional_number(row["project_id"])) is not None
            ]
            next_number = max(used_numbers, default=0) + 1
            for project_name in project_names:
                existing = connection.execute(
                    "SELECT 1 FROM projects WHERE project_name = ? LIMIT 1",
                    (project_name,),
                ).fetchone()
                if existing:
                    continue
                provisional_id = f"{PROVISIONAL_PREFIX}{next_number:03d}"
                next_number += 1
                connection.execute(
                    """
                    INSERT INTO projects(
                        project_id, project_name, active, created_at, updated_at
                    ) VALUES (?, ?, 1, ?, ?)
                    """,
                    (provisional_id, project_name, now, now),
                )

    def register_project(self, project_id: str, project_name: str, folder: str = "") -> None:
        now = datetime.now().astimezone().isoformat(timespec="seconds")
        with self.session() as connection:
            connection.execute(
                """
                INSERT INTO projects(
                    project_id, project_name, folder, active, created_at, updated_at
                ) VALUES (?, ?, ?, 1, ?, ?)
                ON CONFLICT(project_id) DO UPDATE SET
                    project_name = excluded.project_name,
                    folder = CASE WHEN excluded.folder != ''
                             THEN excluded.folder ELSE projects.folder END,
                    active = 1,
                    updated_at = excluded.updated_at
                """,
                (project_id, project_name, folder, now, now),
            )

    def register_project_name(self, project_name: str, folder: str = "") -> str:
        """Register a project by name only and return the stable temporary ID the system assigns."""
        now = datetime.now().astimezone().isoformat(timespec="seconds")
        with self.immediate_transaction() as connection:
            existing = connection.execute(
                """
                SELECT project_id FROM projects
                WHERE project_name = ?
                ORDER BY active DESC, updated_at DESC LIMIT 1
                """,
                (project_name,),
            ).fetchone()
            if existing:
                connection.execute(
                    """
                    UPDATE projects SET active = 1, updated_at = ?,
                        folder = CASE WHEN ? != '' THEN ? ELSE folder END
                    WHERE project_id = ? COLLATE NOCASE
                    """,
                    (now, folder, folder, existing["project_id"]),
                )
                return str(existing["project_id"])

            rows = connection.execute("SELECT project_id FROM projects").fetchall()
            used_numbers = [
                number
                for row in rows
                if (number := provisional_number(row["project_id"])) is not None
            ]
            provisional_id = f"{PROVISIONAL_PREFIX}{max(used_numbers, default=0) + 1:03d}"
            connection.execute(
                """
                INSERT INTO projects(
                    project_id, project_name, folder, active, created_at, updated_at
                ) VALUES (?, ?, ?, 1, ?, ?)
                """,
                (provisional_id, project_name, folder, now, now),
            )
            return provisional_id

    def bind_project_id(self, old_project_id: str, new_project_id: str) -> None:
        """Bind a temporary ID to an official ID; existing issue records move over with it."""
        now = datetime.now().astimezone().isoformat(timespec="seconds")
        with self.immediate_transaction() as connection:
            existing = connection.execute(
                "SELECT project_id FROM projects WHERE project_id = ? COLLATE NOCASE",
                (old_project_id,),
            ).fetchone()
            if not existing:
                raise ValueError(f"Original project ID does not exist: {old_project_id}")
            conflict = connection.execute(
                "SELECT project_id FROM projects WHERE project_id = ? COLLATE NOCASE",
                (new_project_id,),
            ).fetchone()
            if conflict and str(conflict["project_id"]).casefold() != old_project_id.casefold():
                raise ValueError(f"Official project ID is already taken: {new_project_id}")
            if old_project_id.casefold() == new_project_id.casefold():
                return

            connection.execute(
                "UPDATE issues SET project_id = ? WHERE project_id = ? COLLATE NOCASE",
                (new_project_id, old_project_id),
            )
            try:
                # The intake ledger has to follow too, or the intake history loses its project after binding
                connection.execute(
                    "UPDATE intakes SET project_id = ? WHERE project_id = ? COLLATE NOCASE",
                    (new_project_id, old_project_id),
                )
            except sqlite3.OperationalError:
                pass  # an old ledger may not have an intakes table yet (intake gate never used)
            connection.execute(
                "UPDATE projects SET project_id = ?, updated_at = ?"
                " WHERE project_id = ? COLLATE NOCASE",
                (new_project_id, now, old_project_id),
            )

    def set_project_active(self, project_id: str, active: bool) -> None:
        now = datetime.now().astimezone().isoformat(timespec="seconds")
        with self.session() as connection:
            cursor = connection.execute(
                "UPDATE projects SET active = ?, updated_at = ?"
                " WHERE project_id = ? COLLATE NOCASE",
                (1 if active else 0, now, project_id),
            )
            if cursor.rowcount != 1:
                raise ValueError(f"Project ID does not exist: {project_id}")

    def list_projects(self, *, active_only: bool = True) -> list[sqlite3.Row]:
        with self.session() as connection:
            sql = "SELECT project_id, project_name, active FROM projects"
            if active_only:
                sql += " WHERE active = 1"
            sql += " ORDER BY updated_at DESC, project_id"
            return connection.execute(sql).fetchall()

    # ------------------------------------------------------------ export

    def export_csv(self, destination: Path | str) -> Path:
        destination = Path(destination)
        destination.parent.mkdir(parents=True, exist_ok=True)
        temporary = destination.with_suffix(destination.suffix + ".tmp")
        with self.session() as connection:
            rows = connection.execute(
                """
                SELECT
                    i.issue_id, i.created_at, i.project_id, i.project_name,
                    i.file_name, i.stage, i.issue_date, i.status,
                    i.file_path, i.file_hash,
                    d.delivery_id, d.recorded_at, d.purpose, d.channels_json,
                    d.recipients, d.notes, d.action
                FROM issues i
                LEFT JOIN deliveries d ON d.issue_id = i.issue_id
                ORDER BY i.created_at DESC, d.recorded_at
                """
            ).fetchall()
            files_by_issue: dict[str, list[sqlite3.Row]] = {}
            for row in connection.execute(
                "SELECT issue_id, source_path, source_hash FROM issue_files ORDER BY file_id"
            ).fetchall():
                files_by_issue.setdefault(row["issue_id"], []).append(row)

        headers = [
            "Issue ID", "Created at", "Project ID", "Project name", "File name", "Stage",
            "Issue date", "Status", "File path", "File SHA256", "Recorded at", "Purpose", "Channels",
            "Recipients", "Action", "Notes", "Original file paths", "Source file SHA256",
        ]
        try:
            with temporary.open("w", encoding="utf-8-sig", newline="") as stream:
                writer = csv.writer(stream)
                writer.writerow(headers)
                for row in rows:
                    files = files_by_issue.get(row["issue_id"], [])
                    try:
                        channels = ", ".join(json.loads(row["channels_json"] or "[]"))
                    except (ValueError, TypeError):
                        channels = row["channels_json"] or ""
                    writer.writerow(
                        [
                            row["issue_id"], row["created_at"], row["project_id"],
                            row["project_name"], row["file_name"], row["stage"],
                            row["issue_date"], row["status"], row["file_path"],
                            row["file_hash"], row["recorded_at"] or "",
                            row["purpose"] or "", channels, row["recipients"] or "",
                            row["action"] or "", row["notes"] or "",
                            " | ".join(item["source_path"] for item in files),
                            " | ".join(item["source_hash"] for item in files),
                        ]
                    )
            temporary.replace(destination)
        finally:
            if temporary.exists():
                temporary.unlink()
        return destination
