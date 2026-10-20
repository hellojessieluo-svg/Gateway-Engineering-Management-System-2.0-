from __future__ import annotations

import csv
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from .models import STATUS_ARCHIVED, STATUS_WITHDRAWN


SCHEMA_VERSION = 6


class IntakeLedger:
    """Intake ledger: registration, de-duplication and receipts log for incoming items."""

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

    def initialize(self) -> None:
        with self.session() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS metadata (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS intakes (
                    intake_id TEXT PRIMARY KEY,
                    created_at TEXT NOT NULL,
                    archived INTEGER NOT NULL DEFAULT 0,
                    project_id TEXT NOT NULL DEFAULT '' COLLATE NOCASE,
                    project_name TEXT NOT NULL DEFAULT '',
                    source_org TEXT NOT NULL,
                    source_person TEXT NOT NULL DEFAULT '',
                    source_channel TEXT NOT NULL DEFAULT '',
                    received_date TEXT NOT NULL,
                    subject TEXT NOT NULL DEFAULT '',
                    archive_dir TEXT NOT NULL,
                    content_signature TEXT NOT NULL,
                    file_count INTEGER NOT NULL,
                    total_size INTEGER NOT NULL,
                    todo INTEGER NOT NULL DEFAULT 0,
                    handled_at TEXT NOT NULL DEFAULT '',
                    notes TEXT NOT NULL DEFAULT '',
                    manifest_json TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'filed',
                    stage TEXT NOT NULL DEFAULT '',
                    replies_to TEXT NOT NULL DEFAULT '',
                    UNIQUE(archive_dir)
                );

                CREATE INDEX IF NOT EXISTS ix_intakes_project
                    ON intakes(project_id, received_date DESC);
                CREATE INDEX IF NOT EXISTS ix_intakes_content
                    ON intakes(content_signature);

                CREATE TABLE IF NOT EXISTS intake_files (
                    file_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    intake_id TEXT NOT NULL REFERENCES intakes(intake_id),
                    source_path TEXT NOT NULL,
                    original_name TEXT NOT NULL,
                    archived_name TEXT NOT NULL,
                    extension TEXT NOT NULL,
                    file_size INTEGER NOT NULL,
                    source_hash TEXT NOT NULL,
                    checks_json TEXT NOT NULL,
                    warnings_json TEXT NOT NULL DEFAULT '[]',
                    UNIQUE(intake_id, archived_name)
                );

                CREATE INDEX IF NOT EXISTS ix_intake_files_hash
                    ON intake_files(source_hash);

                CREATE TABLE IF NOT EXISTS receipts (
                    receipt_id TEXT PRIMARY KEY,
                    intake_id TEXT NOT NULL REFERENCES intakes(intake_id),
                    recorded_at TEXT NOT NULL,
                    source_org TEXT NOT NULL DEFAULT '',
                    source_channel TEXT NOT NULL DEFAULT '',
                    received_date TEXT NOT NULL DEFAULT '',
                    notes TEXT NOT NULL DEFAULT '',
                    action TEXT NOT NULL
                );
                """
            )
            # 2026-09-03 cleanup: add later columns to old ledgers, drop columns left by removed features.
            # This block can go once every old ledger has been opened once.
            intake_columns = {
                row["name"] for row in connection.execute("PRAGMA table_info(intakes)")
            }
            for column, ddl in (
                ("stage", "TEXT NOT NULL DEFAULT ''"),
                ("replies_to", "TEXT NOT NULL DEFAULT ''"),
                ("source_person", "TEXT NOT NULL DEFAULT ''"),
                ("archived", "INTEGER NOT NULL DEFAULT 0"),
            ):
                if column not in intake_columns:
                    connection.execute(f"ALTER TABLE intakes ADD COLUMN {column} {ddl}")
            if "category" in intake_columns:
                # Categories removed 2026-09-03. The old index sits on the column being dropped;
                # DROP COLUMN fails unless the index goes first
                connection.execute("DROP INDEX IF EXISTS ix_intakes_project")
                connection.execute("ALTER TABLE intakes DROP COLUMN category")
                connection.execute(
                    "CREATE INDEX IF NOT EXISTS ix_intakes_project"
                    " ON intakes(project_id, received_date DESC)"
                )
            if "task_id" in intake_columns:
                # Task gate removed 2026-08-15
                connection.execute("ALTER TABLE intakes DROP COLUMN task_id")
            connection.execute(
                "INSERT OR REPLACE INTO metadata(key, value)"
                " VALUES('schema_version_inbound', ?)",
                (str(SCHEMA_VERSION),),
            )

    # ------------------------------------------------------------------ de-duplication

    def find_same_intake(self, project_id: str, content_signature: str) -> sqlite3.Row | None:
        self.initialize()
        with self.session() as connection:
            return connection.execute(
                """
                SELECT * FROM intakes
                WHERE project_id = ? COLLATE NOCASE
                  AND content_signature = ? AND status = ?
                ORDER BY created_at DESC LIMIT 1
                """,
                (project_id, content_signature, STATUS_ARCHIVED),
            ).fetchone()


    def find_file_hash(self, source_hash: str) -> list[sqlite3.Row]:
        """Look up files with the same hash among intakes, to warn "this was received before"."""
        self.initialize()
        with self.session() as connection:
            return connection.execute(
                """
                SELECT f.original_name, f.archived_name,
                       i.received_date AS happened_on, i.source_org AS source,
                       i.subject, i.archive_dir, 'intake' AS kind
                FROM intake_files AS f
                JOIN intakes AS i ON i.intake_id = f.intake_id
                WHERE f.source_hash = ? AND i.status = ?
                ORDER BY happened_on DESC
                """,
                (source_hash, STATUS_ARCHIVED),
            ).fetchall()

    # ---------------------------------------------------------------- dropdown suggestions

    def list_source_orgs(self, limit: int = 50) -> list[str]:
        self.initialize()
        with self.session() as connection:
            rows = connection.execute(
                """
                SELECT source_org, MAX(created_at) AS last_used FROM intakes
                WHERE source_org != '' GROUP BY source_org
                ORDER BY last_used DESC LIMIT ?
                """,
                (limit,),
            ).fetchall()
            return [str(row["source_org"]) for row in rows]

    def list_source_persons(self, limit: int = 50) -> list[str]:
        self.initialize()
        with self.session() as connection:
            rows = connection.execute(
                """
                SELECT source_person, MAX(created_at) AS last_used FROM intakes
                WHERE source_person != '' GROUP BY source_person
                ORDER BY last_used DESC LIMIT ?
                """,
                (limit,),
            ).fetchall()
            return [str(row["source_person"]) for row in rows]

    # ------------------------------------------------------------------ listing

    def list_intakes(self, limit: int = 200) -> list[sqlite3.Row]:
        self.initialize()
        with self.session() as connection:
            return connection.execute(
                """
                SELECT i.*,
                       (SELECT COUNT(*) FROM receipts r
                        WHERE r.intake_id = i.intake_id AND r.action != 'withdrawn')
                           AS receipt_count
                FROM intakes i
                ORDER BY i.received_date DESC, i.created_at DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()


    def todo_count(self) -> int:
        self.initialize()
        with self.session() as connection:
            row = connection.execute(
                "SELECT COUNT(*) AS count FROM intakes WHERE todo = 1 AND status = ?",
                (STATUS_ARCHIVED,),
            ).fetchone()
            return int(row["count"])

    def get_intake(self, intake_id: str) -> sqlite3.Row | None:
        self.initialize()
        with self.session() as connection:
            return connection.execute(
                "SELECT * FROM intakes WHERE intake_id = ?", (intake_id,)
            ).fetchone()


    def get_intake_files(self, intake_id: str) -> list[sqlite3.Row]:
        self.initialize()
        with self.session() as connection:
            return connection.execute(
                "SELECT * FROM intake_files WHERE intake_id = ? ORDER BY file_id",
                (intake_id,),
            ).fetchall()

    # ------------------------------------------------------------------ status updates

    def add_receipt(
        self,
        receipt_id: str,
        intake_id: str,
        recorded_at: str,
        source_org: str,
        source_channel: str,
        received_date: str,
        notes: str,
        action: str,
    ) -> None:
        with self.session() as connection:
            connection.execute(
                """
                INSERT INTO receipts(
                    receipt_id, intake_id, recorded_at, source_org,
                    source_channel, received_date, notes, action
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    receipt_id,
                    intake_id,
                    recorded_at,
                    source_org,
                    source_channel,
                    received_date,
                    notes,
                    action,
                ),
            )

    def set_todo(self, intake_id: str, todo: bool, handled_at: str) -> None:
        with self.session() as connection:
            cursor = connection.execute(
                "UPDATE intakes SET todo = ?, handled_at = ? WHERE intake_id = ?",
                (1 if todo else 0, "" if todo else handled_at, intake_id),
            )
            if cursor.rowcount != 1:
                raise ValueError("Intake record not found.")

    def mark_intake_withdrawn(
        self, intake_id: str, archive_dir: str, withdrawn_at: str
    ) -> None:
        with self.immediate_transaction() as connection:
            cursor = connection.execute(
                """
                UPDATE intakes SET status = ?, archive_dir = ?
                WHERE intake_id = ? AND status = ?
                """,
                (STATUS_WITHDRAWN, archive_dir, intake_id, STATUS_ARCHIVED),
            )
            if cursor.rowcount != 1:
                raise ValueError("The record does not exist or has already been withdrawn.")
            connection.execute(
                """
                INSERT INTO receipts(
                    receipt_id, intake_id, recorded_at, source_org,
                    source_channel, received_date, notes, action
                ) VALUES (lower(hex(randomblob(16))), ?, ?, '', '', '', '', 'withdrawn')
                """,
                (intake_id, withdrawn_at),
            )

    def restage_intake(self, intake_id: str, stage: str, archive_dir: str) -> None:
        """The wrong stage was picked at registration: change which stage this intake belongs to, together with its new address.

        The stage is part of the address (room\\stage\\inbox\\), so these two columns only
        change together -- changing stage alone would point the ledger at a place that does
        not exist. The caller moves the files first; this only touches the ledger.
        Only "filed" records qualify: withdrawn files sit in "_withdrawn", a different location.
        """
        with self.immediate_transaction() as connection:
            cursor = connection.execute(
                """
                UPDATE intakes SET stage = ?, archive_dir = ?
                WHERE intake_id = ? AND status = ?
                """,
                (stage, archive_dir, intake_id, STATUS_ARCHIVED),
            )
            if cursor.rowcount != 1:
                raise ValueError("The record does not exist or is not \"filed\".")

    def change_intake_source(
        self, intake_id: str, source_org: str, archive_dir: str
    ) -> None:
        """The source was mistyped at registration: change this intake's source, together with its new address.

        The source is part of the address (inbox\\<date-source-subject>), so these two
        columns only change together. The receipts log entry carries the same mistyped word
        and is fixed too; the withdrawal entry is left alone -- it records the "withdrawn"
        action and never had a source. The caller renames the files first; this only touches
        the ledger. Only "filed" records qualify: withdrawn files sit in "_withdrawn", a
        different location.
        """
        with self.immediate_transaction() as connection:
            cursor = connection.execute(
                """
                UPDATE intakes SET source_org = ?, archive_dir = ?
                WHERE intake_id = ? AND status = ?
                """,
                (source_org, archive_dir, intake_id, STATUS_ARCHIVED),
            )
            if cursor.rowcount != 1:
                raise ValueError("The record does not exist or is not \"filed\".")
            connection.execute(
                "UPDATE receipts SET source_org = ?"
                " WHERE intake_id = ? AND action != 'withdrawn'",
                (source_org, intake_id),
            )

    def delete_intake(self, intake_id: str) -> None:
        """Erase this intake from the ledger, together with its file rows and receipts.

        This is not the same as withdrawing: a withdrawal leaves a trace (the red entry stays
        in the ledger), a deletion leaves none. A record registered with a typo should never
        have been in the ledger; keeping it is just noise. The caller moves the files into
        "_withdrawn" first; this only touches the database.
        """
        with self.immediate_transaction() as connection:
            connection.execute("DELETE FROM receipts WHERE intake_id = ?", (intake_id,))
            connection.execute(
                "DELETE FROM intake_files WHERE intake_id = ?", (intake_id,)
            )
            cursor = connection.execute(
                "DELETE FROM intakes WHERE intake_id = ?", (intake_id,)
            )
            if cursor.rowcount != 1:
                raise ValueError("Intake record not found.")

    # ------------------------------------------------------------------ export

    def export_csv(self, destination: Path | str) -> Path:
        self.initialize()
        destination = Path(destination)
        destination.parent.mkdir(parents=True, exist_ok=True)
        temporary = destination.with_suffix(destination.suffix + ".tmp")
        with self.session() as connection:
            intakes = connection.execute(
                "SELECT * FROM intakes ORDER BY received_date DESC, created_at DESC"
            ).fetchall()
            intake_files: dict[str, list[sqlite3.Row]] = {}
            for row in connection.execute(
                "SELECT intake_id, original_name, archived_name, source_path, source_hash"
                " FROM intake_files ORDER BY file_id"
            ):
                intake_files.setdefault(row["intake_id"], []).append(row)

        headers = [
            "Type",
            "Record ID",
            "Registered at",
            "Archive",
            "Project ID",
            "Project name",
            "Source",
            "Source person",
            "Channel",
            "Received date",
            "Subject",
            "File count",
            "Total size",
            "To do",
            "Status",
            "Batch folder",
            "Notes",
            "Original file paths",
            "Filed file names",
            "File SHA256",
        ]
        try:
            with temporary.open("w", encoding="utf-8-sig", newline="") as stream:
                writer = csv.writer(stream)
                writer.writerow(headers)
                for row in intakes:
                    files = intake_files.get(row["intake_id"], [])
                    writer.writerow(
                        [
                            "intake",
                            row["intake_id"],
                            row["created_at"],
                            "yes" if row["archived"] else "",
                            row["project_id"],
                            row["project_name"],
                            row["source_org"],
                            row["source_person"],
                            row["source_channel"],
                            row["received_date"],
                            row["subject"],
                            row["file_count"],
                            row["total_size"],
                            "yes" if row["todo"] else "",
                            row["status"],
                            row["archive_dir"],
                            row["notes"],
                            " | ".join(item["source_path"] for item in files),
                            " | ".join(item["archived_name"] for item in files),
                            " | ".join(item["source_hash"] for item in files),
                        ]
                    )
            temporary.replace(destination)
        finally:
            if temporary.exists():
                temporary.unlink()
        return destination
