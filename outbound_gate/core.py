from __future__ import annotations

import json
import os
import shutil
import sqlite3
import tempfile
import uuid
from datetime import datetime
from pathlib import Path

from common.errors import ConflictError, StorageError, ValidationError
from .ledger import Ledger
from .models import (
    CHANNELS,
    ISSUE_ROLES,
    PURPOSES,
    SELFCHECK_EXTENSION,
    SELFCHECK_FILENAME,
    STAGES,
    CheckedFile,
    IssuedFile,
    IssueRequest,
    IssueResult,
    KNOWN_NAME_ONLY_PROJECTS,
    KNOWN_PROJECTS,
)
from common.places import WITHDRAWN_FOLDER, outbox_dir
from common.naming import (
    ensure_path_length,
    make_batch_folder_name,
    unique_batch_dir,
    validate_component,
    validate_free_text,
)
from common.validators import sha256_file, validate_source_file


class OutboundGate:
    def __init__(
        self,
        library_root: Path | str,
        ledger_path: Path | str | None = None,
    ):
        # The root is the whole library: issues land under <root>\<project room>\...\outbox\;
        # there is no top-level issue area any more.
        self.library_root = Path(library_root)
        # Without ledger_path, fall back to the ledger at the root, so tests and old data still work.
        self.ledger = Ledger(
            Path(ledger_path) if ledger_path else self.library_root / "ledger.db"
        )
        self.ledger.initialize()
        self.ledger.seed_projects(KNOWN_PROJECTS)
        self.ledger.seed_project_names(KNOWN_NAME_ONLY_PROJECTS)

    def _validate_request(self, request: IssueRequest) -> list[sqlite3.Row]:
        validate_component(request.project_id, "Project ID")
        validate_component(request.project_name, "Project name", allow_empty=True)
        validate_component(request.stage, "Stage")
        if request.stage not in STAGES:
            raise ValidationError(f"Unsupported stage: {request.stage}")
        if request.purpose not in PURPOSES:
            raise ValidationError(f"Unsupported purpose: {request.purpose}")
        unknown_channels = set(request.channels) - set(CHANNELS)
        if unknown_channels:
            raise ValidationError(f"Unsupported channel: {', '.join(sorted(unknown_channels))}")
        if not request.channels:
            raise ValidationError("Choose at least one channel.")
        # Recipient and subject go into the batch folder name (date-recipient-subject), so
        # illegal characters and excessive length must be stopped before anything happens
        validate_free_text(request.recipients, "Recipient")
        validate_free_text(request.subject, "Subject", allow_empty=True)
        if not request.items:
            raise ValidationError("Add at least one file to issue.")
        # Each file's role can only be "sent" or "kept" (2026-09-22)
        for item in request.items:
            if item.role not in ISSUE_ROLES:
                raise ValidationError(
                    f"File role must be {' / '.join(ISSUE_ROLES)}: {Path(item.path).name} is marked \"{item.role}\""
                )
        return self._check_intake(request)

    def _check_intake(self, request: IssueRequest) -> list[sqlite3.Row]:
        """An issue must reply to an intake (at least one, possibly several) -- the cause-and-effect chain must not break."""
        if not request.intake_ids:
            raise ValidationError("An issue must be linked to an intake: log it under \"Log intake\" first.")
        intakes: list[sqlite3.Row] = []
        for intake_id in request.intake_ids:
            intake = self.ledger.get_intake(intake_id)
            if intake is None:
                raise ValidationError(f"Linked intake not found in the ledger: {intake_id}")
            if intake["status"] != "filed":
                raise ValidationError("The linked intake has been withdrawn and cannot be the reason for an issue.")
            owner = str(intake["project_id"] or "")
            if owner and owner.casefold() != request.project_id.casefold():
                raise ValidationError("The linked intake does not belong to this project.")
            intakes.append(intake)
        return intakes

    def _check_selfcheck(self, path: Path) -> tuple[Path, int, str]:
        """Change & self-check notes: required, must be .md, must not be empty."""
        resolved = Path(path).expanduser().resolve(strict=False)
        if not str(path):
            raise ValidationError("An issue must include change & self-check notes (.md).")
        if resolved.suffix.lower() != SELFCHECK_EXTENSION:
            raise ValidationError(f"Change & self-check notes must be a .md file: {resolved.name}")
        size, digest, _ = validate_source_file(resolved)
        return resolved, size, digest

    def _plan(
        self, request: IssueRequest
    ) -> tuple[list[CheckedFile], list[tuple[sqlite3.Row, CheckedFile]], list[str]]:
        """Check each file and decide which are new content and which are being sent again unchanged.

        No version numbers since 2026-08-15: which version it is, is for the file name to say;
        the program only records "this file, this stage, this day, issued once". A file with
        the same name can be issued many times, one row each, told apart by issue date and
        batch folder.
        """
        checked: list[CheckedFile] = []
        reused: list[tuple[sqlite3.Row, CheckedFile]] = []
        warnings: list[str] = []
        base_keys: set[str] = set()
        source_paths: set[str] = set()

        for item in request.items:
            resolved = Path(item.path).expanduser().resolve(strict=False)
            path_key = os.path.normcase(str(resolved))
            if path_key in source_paths:
                raise ValidationError(f"The same source file was added twice: {resolved}")
            source_paths.add(path_key)

            base_name = resolved.stem
            base_key = base_name.casefold()
            if base_key in base_keys:
                raise ValidationError(
                    "Two files in this batch share a name and would clash in the batch folder: "
                    f"{base_name}{resolved.suffix}"
                )
            base_keys.add(base_key)

            size, source_hash, checks = validate_source_file(resolved)
            same = self.ledger.find_same_content(
                request.project_id, base_name, request.stage, source_hash
            )
            # Issuing does not rename: what goes out is the file under its own name.
            entry = CheckedFile(
                source_path=resolved,
                name=resolved.name,
                stem=base_name,
                extension=resolved.suffix.lower(),
                size=size,
                source_hash=source_hash,
                checks=checks,
                role=item.role,
            )
            if same is not None:
                warnings.append(
                    f"{resolved.name} is byte-for-byte identical to the copy issued on {same['issue_date']}; "
                    "no new record was created, only one \"sent again\" entry."
                )
                reused.append((same, entry))
            else:
                checked.append(entry)
        return checked, reused, warnings

    def create_issue(self, request: IssueRequest) -> IssueResult:
        """One issue: a batch of files, one ledger row per file.

        On-disk shape: <project room>\\<stage>\\outbox\\<dated batch>\\
                          original-name.ext (each file, not a character changed)
                          change-notes.md (the notes you picked, one per batch)
        """
        intakes = self._validate_request(request)
        intake_labels = [
            f"{row['received_date']} {row['source_org']}"
            f"{' · ' + row['subject'] if row['subject'] else ''}"
            for row in intakes
        ]
        selfcheck_source, _, selfcheck_hash = self._check_selfcheck(request.selfcheck_path)
        folder = self.ledger.folder_of(request.project_id)
        checked_files, reused, warnings = self._plan(request)

        batch_id = str(uuid.uuid4())
        created_at = datetime.now().astimezone().isoformat(timespec="seconds")

        # The whole batch is old content: nothing to write, just record "sent again" under each original row
        if not checked_files:
            for row, entry in reused:
                self._add_delivery(row["issue_id"], request, created_at, "reused")
            first = reused[0][0]
            return IssueResult(
                batch_id=batch_id,
                output_dir=Path(first["output_dir"]),
                selfcheck_path=Path(first["selfcheck_path"] or ""),
                files=[
                    IssuedFile(
                        issue_id=row["issue_id"],
                        name=Path(row["file_path"]).name,
                        path=Path(row["file_path"]),
                        source_hash=entry.source_hash,
                        reused=True,
                        role=str(row["role"] or "sent"),
                    )
                    for row, entry in reused
                ],
                warnings=warnings,
            )

        # Issue mirrors intake: <stage>\outbox\<date-recipient-subject>\. The "Archive" flag
        # (archived) only goes into the ledger and does not change location (2026-09-08; ticking
        # it between 09-03 and 09-08 added an extra "archive" level, since flattened)
        stage_dir = outbox_dir(self.library_root, folder, request.stage)
        # Inside the batch folder the notes are normalized to change-notes.md -- every batch has
        # exactly this one document, so its name is set by the rules, not by whatever the draft
        # happened to be called (2026-08-27). The source file is not touched.
        selfcheck_name = SELFCHECK_FILENAME
        clash = [item for item in checked_files if item.name == selfcheck_name]
        if clash:
            raise ValidationError(
                f"A file in this batch has the same name as the change & self-check notes: {selfcheck_name}"
            )
        stage_dir.mkdir(parents=True, exist_ok=True)
        final_dir = unique_batch_dir(
            stage_dir,
            make_batch_folder_name(
                request.issue_date, request.recipients, request.subject
            ),
        )
        for item in checked_files:
            ensure_path_length(final_dir / item.name)
        ensure_path_length(final_dir / selfcheck_name)

        temp_parent = self.library_root / ".tmp"
        temp_parent.mkdir(parents=True, exist_ok=True)
        temp_dir = Path(tempfile.mkdtemp(prefix="issue-", dir=temp_parent))
        moved_to_final = False
        try:
            for item in checked_files:
                self._copy_verified(item.source_path, temp_dir / item.name, item.source_hash)
            self._copy_verified(selfcheck_source, temp_dir / selfcheck_name, selfcheck_hash)

            with self.ledger.immediate_transaction() as connection:
                if final_dir.exists():
                    raise ConflictError(f"Target batch folder already exists, refusing to overwrite: {final_dir}")
                temp_dir.rename(final_dir)
                moved_to_final = True
                final_selfcheck = final_dir / selfcheck_name

                connection.execute(
                    """
                    INSERT INTO projects(
                        project_id, project_name, active, created_at, updated_at
                    ) VALUES (?, ?, 1, ?, ?)
                    ON CONFLICT(project_id) DO UPDATE SET
                        project_name = excluded.project_name,
                        active = 1,
                        updated_at = excluded.updated_at
                    """,
                    (request.project_id, request.project_name, created_at, created_at),
                )

                issued: list[IssuedFile] = []
                for item in checked_files:
                    issue_id = str(uuid.uuid4())
                    manifest = {
                        "schema_version": 3,
                        "issue_id": issue_id,
                        "batch_id": batch_id,
                        "created_at": created_at,
                        "project_id": request.project_id,
                        "project_name": request.project_name,
                        "folder": folder,
                        "stage": request.stage,
                        "file_stem": item.stem,
                        "issue_date": request.issue_date.isoformat(),
                        "archived": request.archived,
                        "role": item.role,
                        "purpose": request.purpose,
                        "channels": request.channels,
                        "recipients": request.recipients,
                        "subject": request.subject,
                        "notes": request.notes,
                        "intake_id": request.intake_id,
                        "intake_ids": request.intake_ids,
                        "intake_label": "; ".join(intake_labels),
                        "selfcheck": {
                            "path": str(final_selfcheck),
                            "source_path": str(selfcheck_source),
                            "sha256": selfcheck_hash,
                        },
                        "file": {
                            "source_path": str(item.source_path),
                            "name": item.name,
                            "extension": item.extension,
                            "size": item.size,
                            "sha256": item.source_hash,
                            "checks": item.checks,
                        },
                    }
                    connection.execute(
                        """
                        INSERT INTO issues(
                            issue_id, created_at, project_id, project_name, stage,
                            issue_date, batch_id, file_name, file_stem, file_path,
                            file_hash, output_dir, selfcheck_path, intake_id,
                            archived, status, manifest_json, role
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'issued', ?, ?)
                        """,
                        (
                            issue_id,
                            created_at,
                            request.project_id,
                            request.project_name,
                            request.stage,
                            request.issue_date.isoformat(),
                            batch_id,
                            item.name,
                            item.stem,
                            str(final_dir / item.name),
                            item.source_hash,
                            str(final_dir),
                            str(final_selfcheck),
                            request.intake_id,
                            1 if request.archived else 0,
                            json.dumps(manifest, ensure_ascii=False),
                            item.role,
                        ),
                    )
                    # Every many-to-many link goes into the link table; the intake_id column keeps only the first
                    connection.executemany(
                        "INSERT INTO issue_intakes(issue_id, intake_id) VALUES (?, ?)",
                        [(issue_id, x) for x in request.intake_ids],
                    )
                    connection.execute(
                        """
                        INSERT INTO issue_files(
                            issue_id, source_path, original_name,
                            file_name, extension, file_size, source_hash, checks_json
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            issue_id,
                            str(item.source_path),
                            item.name,
                            item.name,
                            item.extension,
                            item.size,
                            item.source_hash,
                            json.dumps(item.checks, ensure_ascii=False),
                        ),
                    )
                    connection.execute(
                        """
                        INSERT INTO deliveries(
                            delivery_id, issue_id, recorded_at, purpose,
                            channels_json, recipients, subject, notes, action
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'issued')
                        """,
                        (
                            str(uuid.uuid4()),
                            issue_id,
                            created_at,
                            request.purpose,
                            json.dumps(request.channels, ensure_ascii=False),
                            request.recipients,
                            request.subject,
                            request.notes,
                        ),
                    )
                    issued.append(
                        IssuedFile(
                            issue_id=issue_id,
                            name=item.name,
                            path=final_dir / item.name,
                            source_hash=item.source_hash,
                            reused=False,
                            role=item.role,
                        )
                    )

                # Files in this batch sent again unchanged: recorded under their original rows
                for row, entry in reused:
                    connection.execute(
                        """
                        INSERT INTO deliveries(
                            delivery_id, issue_id, recorded_at, purpose,
                            channels_json, recipients, subject, notes, action
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'reused')
                        """,
                        (
                            str(uuid.uuid4()),
                            row["issue_id"],
                            created_at,
                            request.purpose,
                            json.dumps(request.channels, ensure_ascii=False),
                            request.recipients,
                            request.subject,
                            request.notes,
                        ),
                    )
                    issued.append(
                        IssuedFile(
                            issue_id=row["issue_id"],
                            name=row["file_name"],
                            path=Path(row["file_path"]),
                            source_hash=entry.source_hash,
                            reused=True,
                            # A reuse points at the original row, so the role is the original one; change it with issue-role
                            role=str(row["role"] or "sent"),
                        )
                    )

            return IssueResult(
                batch_id=batch_id,
                output_dir=final_dir,
                selfcheck_path=final_dir / selfcheck_name,
                files=issued,
                warnings=warnings,
            )
        except Exception:
            if moved_to_final and final_dir.exists():
                self._rollback_dir(final_dir)
            raise
        finally:
            if temp_dir.exists():
                shutil.rmtree(temp_dir, ignore_errors=True)

    def _copy_verified(self, source: Path, destination: Path, expected_hash: str) -> None:
        """Copy and verify on the spot: if the source changed during the copy, or the copy does not match, it is void."""
        before_stat = source.stat()
        shutil.copy2(source, destination)
        after_stat = source.stat()
        if (
            before_stat.st_size != after_stat.st_size
            or before_stat.st_mtime_ns != after_stat.st_mtime_ns
        ):
            raise StorageError(f"Source file changed during copying: {source}")
        if sha256_file(destination) != expected_hash:
            raise StorageError(f"Hash mismatch after copying: {source}")

    def _add_delivery(
        self, issue_id: str, request: IssueRequest, recorded_at: str, action: str
    ) -> None:
        self.ledger.add_delivery(
            issue_id=issue_id,
            delivery_id=str(uuid.uuid4()),
            recorded_at=recorded_at,
            purpose=request.purpose,
            channels=request.channels,
            recipients=request.recipients,
            subject=request.subject,
            notes=request.notes,
            action=action,
        )

    def _rollback_dir(self, target: Path) -> None:
        try:
            resolved_root = self.library_root.resolve()
            resolved_target = target.resolve()
            if resolved_target != resolved_root and resolved_root in resolved_target.parents:
                shutil.rmtree(resolved_target)
        except Exception:
            pass

    def _withdrawn_root(self, project_id: str, stage: str) -> Path:
        """Withdrawn items follow their project room and stage; there is no top-level withdrawn pile."""
        # Old records from before the change have an empty stage: fall back to "other"
        fallback = stage or "other"
        withdrawn_root = (
            outbox_dir(self.library_root, self.ledger.folder_of(project_id), fallback)
            / WITHDRAWN_FOLDER
        )
        if withdrawn_root.exists() and withdrawn_root.is_symlink():
            raise StorageError(f"Withdrawn folder is a link, refusing to proceed: {withdrawn_root}")
        withdrawn_root.mkdir(parents=True, exist_ok=True)
        return withdrawn_root

    def _move_issue_to_withdrawn(self, row: sqlite3.Row) -> Path:
        """Move one issue's file into the project room's "_withdrawn". Shared by withdraw and delete.

        One batch folder holds the files of several rows, so a withdrawal only moves its own
        file; once the whole batch is withdrawn, the leftover notes and empty folder move along
        too, so no empty shell is left behind.
        """
        issue_id = str(row["issue_id"])
        root = self.library_root.resolve()
        source = Path(row["file_path"])
        if not source.exists() or not source.is_file():
            raise StorageError(f"Issued file no longer exists, cannot withdraw: {source}")
        if source.is_symlink():
            raise StorageError(f"Issued file is a link, refusing to withdraw: {source}")
        resolved_source = source.resolve()
        if root not in resolved_source.parents:
            raise StorageError(f"Issued file is outside this library, refusing to withdraw: {resolved_source}")

        withdrawn_root = self._withdrawn_root(row["project_id"], str(row["stage"] or ""))
        stamp = datetime.now().astimezone().strftime("%Y%m%d-%H%M%S")
        target = withdrawn_root / f"{stamp}-{issue_id[:8]}-{source.name}"
        if target.exists():
            raise StorageError(f"Withdrawal target already exists: {target}")
        shutil.move(str(resolved_source), str(target))
        self._sweep_empty_batch(Path(row["output_dir"]), withdrawn_root, stamp)
        return target

    def _sweep_empty_batch(self, batch_dir: Path, withdrawn_root: Path, stamp: str) -> None:
        """When a batch folder holds only the notes (or nothing), move that along too."""
        if not batch_dir.is_dir() or batch_dir.is_symlink():
            return
        remaining = [p for p in batch_dir.iterdir() if p.is_file()]
        if any(p.suffix.lower() != SELFCHECK_EXTENSION for p in remaining):
            return
        for leftover in remaining:
            target = withdrawn_root / f"{stamp}-{leftover.name}"
            if not target.exists():
                shutil.move(str(leftover), str(target))
        try:
            batch_dir.rmdir()
        except OSError:
            return
        # If this was the last batch under a category level, the empty level goes too -- no empty shells
        try:
            batch_dir.parent.rmdir()
        except OSError:
            pass

    def withdraw_issue(self, issue_id: str) -> Path:
        """Withdraw one issue: the file moves into "_withdrawn", the ledger keeps a red entry, always recoverable."""
        row = self.ledger.get_issue(issue_id)
        if row is None:
            raise ValueError("Issue record not found.")
        if row["status"] != "issued":
            raise ValueError("Only \"issued\" records can be withdrawn.")

        source = Path(row["file_path"])
        target = self._move_issue_to_withdrawn(row)
        withdrawn_at = datetime.now().astimezone().isoformat(timespec="seconds")
        try:
            self.ledger.mark_withdrawn(
                issue_id, str(target.parent), str(target), withdrawn_at
            )
        except Exception:
            if target.exists() and not source.exists():
                shutil.move(str(target), str(source))
            raise
        return target

    def withdraw_batch(self, batch_id: str) -> list[Path]:
        """Withdraw a whole batch: "Withdraw last" in the UI withdraws every file sent with that one click."""
        rows = self.ledger.list_batch(batch_id)
        alive = [row for row in rows if row["status"] == "issued"]
        if not alive:
            raise ValueError("Nothing in this batch can be withdrawn.")
        return [self.withdraw_issue(row["issue_id"]) for row in alive]

    def delete_issue(self, issue_id: str) -> dict:
        """Delete a wrongly registered issue: the file moves into "_withdrawn", the ledger row is removed outright."""
        row = self.ledger.get_issue(issue_id)
        if row is None:
            raise ValueError("Issue record not found.")
        source = Path(row["file_path"])
        # Do not block if the file was deleted by hand: the row is being removed anyway
        files_at = ""
        if source.is_file():
            files_at = (
                str(self._move_issue_to_withdrawn(row))
                if row["status"] == "issued"
                else str(source)
            )
        unlinked = self.ledger.delete_issue(issue_id)
        return {"issue_id": issue_id, "files_at": files_at, "unlinked_intakes": unlinked}
