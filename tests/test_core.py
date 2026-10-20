"""Issue gate: one ledger row per file, each file name keeps its own history.

Redesign of 2026-08-15: the program neither packs nor renames (a zip you packed yourself is
accepted), there are no version numbers, some naming fields were dropped, every issue must
hang on an intake and carry one change & self-check notes file.
These tests guard exactly those rules.
"""
from __future__ import annotations

import json
import os
import sqlite3
import tempfile
import unittest
import zipfile
from datetime import date
from pathlib import Path
from unittest import mock

from common.config import config_path
from common.constants import SELFCHECK_FILENAME
from inbound_gate import InboundGate, IntakeItem, IntakeRequest
from outbound_gate import IssueRequest, OutboundGate, SourceItem
from outbound_gate.ledger import Ledger
from common.errors import ValidationError
from common.places import OUTBOX_FOLDER, WITHDRAWN_FOLDER


PROJECT = "PRJ-2601"
PROJECT_NAME = "Riverside Wetland Restoration"
STAGE = "feasibility"
RECIPIENT = "Ben Zhao"


def make_minimal_xlsx(path: Path, marker: str = "one") -> None:
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("[Content_Types].xml", f"<Types>{marker}</Types>")
        archive.writestr("xl/workbook.xml", f"<workbook>{marker}</workbook>")


def make_minimal_docx(path: Path, marker: str = "one") -> None:
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("[Content_Types].xml", f"<Types>{marker}</Types>")
        archive.writestr("word/document.xml", f"<document>{marker}</document>")


def make_minimal_dwg(path: Path, marker: str = "one") -> None:
    path.write_bytes(b"AC1032" + marker.encode("utf-8") + b"\x00" * 64)


class OutboundGateCoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.base = Path(self.temp.name)
        self.sources = self.base / "sources"
        self.sources.mkdir()
        self.output = self.base / "outbound"
        self.gate = OutboundGate(self.output)
        self.inbound = InboundGate(self.output, self.output / "ledger.db")
        self.selfcheck = self.sources / SELFCHECK_FILENAME
        self.selfcheck.write_text(
            "# What changed\n- Longitudinal slope adjusted per client comments\n", encoding="utf-8"
        )
        # In a real library every project has a short name (the room's door plate)
        self.gate.ledger.register_project(PROJECT, PROJECT_NAME, "riverside")
        self.intake_id = self._make_intake()

    def tearDown(self) -> None:
        self.temp.cleanup()

    def _make_intake(self, project_id: str = PROJECT) -> str:
        letter = self.sources / f"letter-{project_id}.pdf"
        letter.write_bytes(b"%PDF-1.7\nx\n%%EOF")
        return self.inbound.archive_intake(
            IntakeRequest(
                project_id=project_id,
                project_name=PROJECT_NAME,
                source_org="Acme Water Authority",
                source_channel="WeChat",
                received_date=date(2026, 8, 8),
                subject="revision comments",
                items=[IntakeItem(letter)],
                stage=STAGE,
            )
        ).record_id

    def request(self, items: list[SourceItem], **extra) -> IssueRequest:
        payload = dict(
            project_id=PROJECT,
            project_name=PROJECT_NAME,
            stage=STAGE,
            issue_date=date(2026, 8, 9),
            purpose="review",
            channels=["WeChat"],
            recipients=RECIPIENT,
            notes="Automated test",
            items=items,
            selfcheck_path=self.selfcheck,
            intake_id=self.intake_id,
        )
        payload.update(extra)
        return IssueRequest(**payload)

    # ------------------------------------------------------------ naming and landing on disk

    def test_each_file_gets_its_own_row_and_keeps_its_name(self) -> None:
        drawing = self.sources / "navigation-review.dwg"
        make_minimal_dwg(drawing)
        quantity = self.sources / "quantities.xlsx"
        make_minimal_xlsx(quantity)

        result = self.gate.create_issue(self.request([SourceItem(drawing), SourceItem(quantity)]))

        # Issuing doesn't rename: what goes out carries these two files' own names
        names = sorted(f.name for f in result.files)
        self.assertEqual(names, ["navigation-review.dwg", "quantities.xlsx"])
        # The batch folder mirrors intake: <stage>\outbox\<date-recipient-subject>\ (no subject here)
        self.assertEqual(result.output_dir.name, f"2026.08.09-{RECIPIENT}")
        self.assertEqual(result.output_dir.parent.name, OUTBOX_FOLDER)
        self.assertEqual(result.output_dir.parents[1].name, STAGE)
        for issued in result.files:
            self.assertTrue(issued.path.is_file())
        # One ledger row per file
        self.assertEqual(len({f.issue_id for f in result.files}), 2)
        row = self.gate.ledger.get_issue(result.files[0].issue_id)
        self.assertEqual(row["intake_id"], self.intake_id)
        self.assertEqual(row["batch_id"], result.batch_id)
        self.assertIn("change-notes", row["selfcheck_path"])

    def test_selfcheck_note_lands_in_the_batch_folder(self) -> None:
        drawing = self.sources / "navigation-review.dwg"
        make_minimal_dwg(drawing)
        result = self.gate.create_issue(self.request([SourceItem(drawing)]))
        self.assertTrue(result.selfcheck_path.is_file())
        # Inside the batch folder the notes always carry this name ("file names belong to people" is for deliverables)
        self.assertEqual(result.selfcheck_path.name, SELFCHECK_FILENAME)
        self.assertEqual(result.selfcheck_path.parent, result.output_dir)

    def test_selfcheck_note_is_renamed_but_the_source_is_not(self) -> None:
        """A casual name is normalized inside the batch folder; the person's own file is left untouched."""
        drawing = self.sources / "navigation-review.dwg"
        make_minimal_dwg(drawing)
        note = self.sources / "quick-check-notes.md"
        note.write_text("# What changed\n- Just trying\n", encoding="utf-8")
        result = self.gate.create_issue(
            self.request([SourceItem(drawing)], selfcheck_path=note)
        )
        self.assertEqual(result.selfcheck_path.name, SELFCHECK_FILENAME)
        self.assertTrue(result.selfcheck_path.is_file())
        self.assertTrue(note.is_file())
        self.assertEqual(
            result.selfcheck_path.read_text(encoding="utf-8"),
            note.read_text(encoding="utf-8"),
        )

    def test_the_same_name_can_go_out_again_and_again(self) -> None:
        """With versions gone: the same file name can go out as often as you like, one row each time."""
        drawing = self.sources / "navigation-review.dwg"
        make_minimal_dwg(drawing, "one")
        first = self.gate.create_issue(self.request([SourceItem(drawing)]))
        make_minimal_dwg(drawing, "two")
        second = self.gate.create_issue(self.request([SourceItem(drawing)]))

        self.assertNotEqual(first.files[0].issue_id, second.files[0].issue_id)
        self.assertEqual(second.files[0].name, "navigation-review.dwg")
        # A second batch on the same day to the same person: the folder gets -2, never overwrites
        self.assertEqual(second.output_dir.name, f"2026.08.09-{RECIPIENT}-2")
        self.assertTrue(first.files[0].path.is_file())

    def test_unchanged_content_does_not_create_a_second_row(self) -> None:
        drawing = self.sources / "navigation-review.dwg"
        make_minimal_dwg(drawing)
        first = self.gate.create_issue(self.request([SourceItem(drawing)]))
        again = self.gate.create_issue(self.request([SourceItem(drawing)]))

        self.assertTrue(again.files[0].reused)
        self.assertEqual(again.files[0].issue_id, first.files[0].issue_id)
        # The ledger gains one "sent again" entry
        self.assertEqual(self.gate.ledger.delivery_count(first.files[0].issue_id), 2)

    def test_the_ledger_carries_no_legacy_residue(self) -> None:
        """Cleanup of 2026-09-03: no column or table left by the version gate, task gate or packaging may remain."""
        with self.gate.ledger.session() as connection:
            columns = {
                row["name"] for row in connection.execute("PRAGMA table_info(issues)")
            }
            tables = {
                row["name"]
                for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")
            }
        for dead in ("version", "version_label", "package_path", "package_name",
                     "package_hash", "section", "after_commit_id", "task_id",
                     "content_signature"):
            self.assertNotIn(dead, columns)
        for dead_table in ("pools", "commits", "commit_files", "commit_basis", "tasks"):
            self.assertNotIn(dead_table, tables)

    def test_legacy_ledger_is_rebuilt_on_first_open(self) -> None:
        """An old ledger (package_* columns, category columns, task table) is rebuilt clean on first open, data moved across."""
        legacy = self.base / "old-ledger.db"
        connection = sqlite3.connect(legacy)
        connection.executescript(
            """
            CREATE TABLE projects (
                project_id TEXT PRIMARY KEY COLLATE NOCASE,
                project_name TEXT NOT NULL DEFAULT '',
                active INTEGER NOT NULL DEFAULT 1,
                created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
                folder TEXT NOT NULL DEFAULT '', stages TEXT NOT NULL DEFAULT '');
            CREATE TABLE issues (
                issue_id TEXT PRIMARY KEY, created_at TEXT NOT NULL,
                project_id TEXT NOT NULL COLLATE NOCASE, project_name TEXT NOT NULL DEFAULT '',
                section TEXT NOT NULL, stage TEXT NOT NULL, issue_date TEXT NOT NULL,
                output_dir TEXT NOT NULL, package_path TEXT NOT NULL,
                package_name TEXT NOT NULL, package_hash TEXT NOT NULL,
                content_signature TEXT NOT NULL, manifest_json TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'issued',
                after_commit_id TEXT NOT NULL DEFAULT '', task_id TEXT NOT NULL DEFAULT '',
                batch_id TEXT NOT NULL DEFAULT '', intake_id TEXT NOT NULL DEFAULT '',
                selfcheck_path TEXT NOT NULL DEFAULT '', archived INTEGER NOT NULL DEFAULT 0,
                version INTEGER NOT NULL DEFAULT 1);
            CREATE TABLE issue_files (
                file_id INTEGER PRIMARY KEY AUTOINCREMENT,
                issue_id TEXT NOT NULL REFERENCES issues(issue_id),
                source_path TEXT NOT NULL, original_name TEXT NOT NULL,
                result_type TEXT NOT NULL, qualifier TEXT NOT NULL DEFAULT '',
                generated_name TEXT NOT NULL, extension TEXT NOT NULL,
                file_size INTEGER NOT NULL, source_hash TEXT NOT NULL,
                checks_json TEXT NOT NULL, UNIQUE(issue_id, generated_name));
            CREATE TABLE deliveries (
                delivery_id TEXT PRIMARY KEY,
                issue_id TEXT NOT NULL REFERENCES issues(issue_id),
                recorded_at TEXT NOT NULL, purpose TEXT NOT NULL, channels_json TEXT NOT NULL,
                recipients TEXT NOT NULL DEFAULT '', notes TEXT NOT NULL DEFAULT '',
                action TEXT NOT NULL);
            CREATE TABLE tasks (task_id TEXT PRIMARY KEY);
            CREATE TABLE commits (commit_id TEXT PRIMARY KEY);
            INSERT INTO issues VALUES ('i1', 't', 'PRJ-2601', 'Riverside', 'site-plan',
                'construction-drawings', '2026-08-01', 'D:\\out', 'D:\\out\\site-plan.dwg',
                'site-plan.dwg', 'HASH', 'SIG', '{}', 'issued', '', 'task-1', 'b1', 'n1',
                'D:\\out\\notes.md', 1, 2);
            INSERT INTO issue_files(issue_id, source_path, original_name, result_type,
                generated_name, extension, file_size, source_hash, checks_json)
                VALUES ('i1', 'D:\\src\\site-plan.dwg', 'site-plan.dwg', 'drawing',
                        'site-plan.dwg', '.dwg', 10, 'HASH', '[]');
            INSERT INTO deliveries VALUES ('d1', 'i1', 't', 'issue', '["WeChat"]',
                'Acme Water Authority', '', 'issued');
            """
        )
        connection.commit()
        connection.close()

        ledger = Ledger(legacy)
        ledger.initialize()

        row = ledger.get_issue("i1")
        self.assertEqual(row["file_name"], "site-plan.dwg")
        self.assertEqual(row["file_stem"], "site-plan")
        self.assertEqual(row["file_path"], "D:\\out\\site-plan.dwg")
        self.assertEqual(row["file_hash"], "HASH")
        self.assertEqual(row["batch_id"], "b1")
        self.assertEqual(row["archived"], 1)
        self.assertEqual(ledger.get_issue_files("i1")[0]["file_name"], "site-plan.dwg")
        self.assertEqual(ledger.delivery_count("i1"), 1)
        with ledger.session() as connection:
            tables = {
                r["name"]
                for r in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")
            }
        self.assertNotIn("tasks", tables)
        self.assertNotIn("commits", tables)
        # A backup was made before touching anything
        self.assertTrue((self.base / "old-ledger-backup.db").is_file())
        # Opening again does not rebuild again (the backup is not refreshed)
        stamp = (self.base / "old-ledger-backup.db").stat().st_mtime_ns
        Ledger(legacy).initialize()
        self.assertEqual((self.base / "old-ledger-backup.db").stat().st_mtime_ns, stamp)

    # ---------------------------------------------------------------- two hard rules

    def test_archives_go_out_like_any_other_file(self) -> None:
        """Archives accepted again: the program no longer renames, so the package you made is just one file."""
        bundle = self.sources / "1001-channel-site-plan.zip"
        make_minimal_xlsx(bundle)  # a zip with valid content

        result = self.gate.create_issue(self.request([SourceItem(bundle)]))

        issued = result.files[0]
        self.assertEqual(issued.name, "1001-channel-site-plan.zip")
        self.assertTrue(issued.path.is_file())

    def test_broken_archive_is_still_refused(self) -> None:
        """Accepting archives doesn't mean not checking them: a zip that can't be read is still stopped."""
        broken = self.sources / "broken.zip"
        broken.write_bytes(b"PK\x03\x04not really a zip")
        with self.assertRaises(ValidationError):
            self.gate.create_issue(self.request([SourceItem(broken)]))

    def test_issue_without_intake_is_refused(self) -> None:
        drawing = self.sources / "navigation-review.dwg"
        make_minimal_dwg(drawing)
        with self.assertRaises(ValidationError):
            self.gate.create_issue(self.request([SourceItem(drawing)], intake_id=""))
        with self.assertRaises(ValidationError):
            self.gate.create_issue(self.request([SourceItem(drawing)], intake_id="no-such-row"))

    def test_intake_from_another_project_is_refused(self) -> None:
        drawing = self.sources / "navigation-review.dwg"
        make_minimal_dwg(drawing)
        other = self._make_intake("PRJ-2602")
        with self.assertRaises(ValidationError) as caught:
            self.gate.create_issue(self.request([SourceItem(drawing)], intake_id=other))
        self.assertIn("does not belong to this project", str(caught.exception))

    def test_selfcheck_note_is_required_and_must_be_md(self) -> None:
        drawing = self.sources / "navigation-review.dwg"
        make_minimal_dwg(drawing)
        with self.assertRaises(ValidationError):
            self.gate.create_issue(self.request([SourceItem(drawing)], selfcheck_path=""))
        not_md = self.sources / "notes.docx"
        make_minimal_docx(not_md)
        with self.assertRaises(ValidationError):
            self.gate.create_issue(self.request([SourceItem(drawing)], selfcheck_path=not_md))

    def test_subject_goes_into_the_batch_folder_name(self) -> None:
        """2026-09-11: the outbox folder name lines up with intake -- date-recipient-subject."""
        drawing = self.sources / "navigation-review.dwg"
        make_minimal_dwg(drawing)
        result = self.gate.create_issue(
            self.request([SourceItem(drawing)], subject="reply to navigation review")
        )
        self.assertEqual(result.output_dir.name, f"2026.08.09-{RECIPIENT}-reply to navigation review")
        with self.gate.ledger.session() as connection:
            row = connection.execute(
                "SELECT subject FROM deliveries WHERE issue_id = ?",
                (result.files[0].issue_id,),
            ).fetchone()
        self.assertEqual(str(row["subject"]), "reply to navigation review")

    def test_subject_may_be_empty(self) -> None:
        """The subject is an optional short field: without it the name falls back to "date-recipient", like old batches."""
        drawing = self.sources / "navigation-review.dwg"
        make_minimal_dwg(drawing)
        result = self.gate.create_issue(self.request([SourceItem(drawing)], subject=""))
        self.assertEqual(result.output_dir.name, f"2026.08.09-{RECIPIENT}")

    def test_subject_that_cannot_be_a_folder_name_is_refused(self) -> None:
        drawing = self.sources / "navigation-review.dwg"
        make_minimal_dwg(drawing)
        with self.assertRaises(ValidationError):
            self.gate.create_issue(
                self.request([SourceItem(drawing)], subject="navigation/review")
            )

    def test_recipient_is_required(self) -> None:
        drawing = self.sources / "navigation-review.dwg"
        make_minimal_dwg(drawing)
        with self.assertRaises(ValidationError):
            self.gate.create_issue(self.request([SourceItem(drawing)], recipients=""))

    def test_archived_flag_is_ledger_only(self) -> None:
        """The "Archive" flag is one bit in the ledger (2026-09-08): ticked or not, the batch lies directly under outbox\\."""
        drawing = self.sources / "navigation-review.dwg"
        make_minimal_dwg(drawing)
        result = self.gate.create_issue(
            self.request([SourceItem(drawing)], archived=True)
        )
        self.assertEqual(result.output_dir.parent.name, OUTBOX_FOLDER)
        self.assertTrue((result.output_dir / "navigation-review.dwg").is_file())
        with self.gate.ledger.session() as connection:
            row = connection.execute(
                "SELECT archived FROM issues WHERE issue_id = ?", (result.files[0].issue_id,)
            ).fetchone()
        self.assertEqual(int(row["archived"]), 1)

    def test_file_role_is_ledger_only_and_defaults_to_sent(self) -> None:
        """Kept files (2026-09-22): same batch folder as sent files, just one extra word in the ledger."""
        drawing = self.sources / "navigation-review.dwg"
        make_minimal_dwg(drawing)
        basis = self.sources / "calc-workbook.xlsx"
        make_minimal_xlsx(basis)
        result = self.gate.create_issue(
            self.request([SourceItem(drawing), SourceItem(basis, role="kept")])
        )
        # Same place: both files sit directly in the batch folder
        self.assertTrue((result.output_dir / "navigation-review.dwg").is_file())
        self.assertTrue((result.output_dir / "calc-workbook.xlsx").is_file())
        roles = {f.name: f.role for f in result.files}
        self.assertEqual(roles, {"navigation-review.dwg": "sent", "calc-workbook.xlsx": "kept"})
        with self.gate.ledger.session() as connection:
            rows = connection.execute(
                "SELECT file_name, role, manifest_json FROM issues WHERE batch_id = ?",
                (result.batch_id,),
            ).fetchall()
        ledger_roles = {r["file_name"]: r["role"] for r in rows}
        self.assertEqual(ledger_roles, roles)
        for r in rows:
            self.assertEqual(json.loads(r["manifest_json"])["role"], ledger_roles[r["file_name"]])

    def test_unknown_file_role_is_refused(self) -> None:
        drawing = self.sources / "navigation-review.dwg"
        make_minimal_dwg(drawing)
        with self.assertRaises(ValidationError):
            self.gate.create_issue(self.request([SourceItem(drawing, role="attachment")]))

    def test_recipient_that_cannot_be_a_folder_name_is_refused(self) -> None:
        """The recipient goes into the folder name: a name with a slash must be stopped before any file moves."""
        drawing = self.sources / "navigation-review.dwg"
        make_minimal_dwg(drawing)
        with self.assertRaises(ValidationError):
            self.gate.create_issue(
                self.request([SourceItem(drawing)], recipients="Acme/North Bay")
            )

    def test_two_files_with_the_same_name_in_one_batch_are_refused(self) -> None:
        first = self.sources / "navigation-review.dwg"
        make_minimal_dwg(first)
        folder = self.sources / "elsewhere"
        folder.mkdir()
        second = folder / "navigation-review.dwg"
        make_minimal_dwg(second, "two")
        with self.assertRaises(ValidationError):
            self.gate.create_issue(self.request([SourceItem(first), SourceItem(second)]))

    # ------------------------------------------------------------ withdraw and delete

    def test_withdraw_batch_moves_every_file_aside(self) -> None:
        drawing = self.sources / "navigation-review.dwg"
        make_minimal_dwg(drawing)
        quantity = self.sources / "quantities.xlsx"
        make_minimal_xlsx(quantity)
        result = self.gate.create_issue(self.request([SourceItem(drawing), SourceItem(quantity)]))

        moved = self.gate.withdraw_batch(result.batch_id)

        self.assertEqual(len(moved), 2)
        for target in moved:
            self.assertTrue(target.is_file())
            self.assertEqual(target.parent.name, WITHDRAWN_FOLDER)
        # After the whole batch is withdrawn the notes go too; no empty batch folder is left behind
        self.assertFalse(result.output_dir.exists())
        row = self.gate.ledger.get_issue(result.files[0].issue_id)
        self.assertEqual(row["status"], "withdrawn")

    def test_withdrawing_one_file_leaves_its_batch_mates_alone(self) -> None:
        drawing = self.sources / "navigation-review.dwg"
        make_minimal_dwg(drawing)
        quantity = self.sources / "quantities.xlsx"
        make_minimal_xlsx(quantity)
        result = self.gate.create_issue(self.request([SourceItem(drawing), SourceItem(quantity)]))

        self.gate.withdraw_issue(result.files[0].issue_id)

        self.assertFalse(result.files[0].path.exists())
        self.assertTrue(result.files[1].path.is_file())

    def test_delete_clears_the_row_and_keeps_the_file(self) -> None:
        drawing = self.sources / "navigation-review.dwg"
        make_minimal_dwg(drawing)
        result = self.gate.create_issue(self.request([SourceItem(drawing)]))

        info = self.gate.delete_issue(result.files[0].issue_id)

        self.assertIsNone(self.gate.ledger.get_issue(result.files[0].issue_id))
        self.assertTrue(Path(info["files_at"]).is_file())

    def test_manifest_records_the_causal_chain(self) -> None:
        drawing = self.sources / "navigation-review.dwg"
        make_minimal_dwg(drawing)
        result = self.gate.create_issue(self.request([SourceItem(drawing)]))
        row = self.gate.ledger.get_issue(result.files[0].issue_id)
        manifest = json.loads(row["manifest_json"])
        self.assertEqual(manifest["intake_id"], self.intake_id)
        self.assertEqual(manifest["file_stem"], "navigation-review")
        self.assertEqual(manifest["recipients"], RECIPIENT)
        self.assertIn("change-notes", manifest["selfcheck"]["path"])


class ConfigPathTests(unittest.TestCase):
    def test_config_lives_under_localappdata(self) -> None:
        """Config and service state files have one home: %LOCALAPPDATA%\\Gateway\\ (no longer redirectable since the second instance was removed on 2026-09-26)."""
        with mock.patch.dict(os.environ, {"LOCALAPPDATA": str(Path("C:/temp"))}):
            self.assertEqual(config_path(), Path("C:/temp") / "Gateway" / "config.json")

    def test_config_with_a_bom_still_loads(self) -> None:
        from common.config import AppConfig

        with tempfile.TemporaryDirectory() as temp:
            with mock.patch.dict(os.environ, {"LOCALAPPDATA": temp}):
                config_path().parent.mkdir(parents=True)
                config_path().write_bytes(b"\xef\xbb\xbf" + json.dumps({"library_root": "X:/lib"}).encode())
                self.assertEqual(AppConfig.load().library_root, "X:/lib")


class UnsortableWordTests(unittest.TestCase):
    def test_only_whole_words_are_rejected(self) -> None:
        from common.naming import validate_component
        from gate_api.library_check import UNSORTABLE

        for ok in ("renewal", "finalize", "copyright"):
            self.assertEqual(validate_component(ok, "Short name"), ok)
            self.assertIsNone(UNSORTABLE.search(ok), ok)
        for bad in ("plan_final", "site-copy", "Latest plan"):
            with self.assertRaises(ValidationError):
                validate_component(bad, "Short name")
            self.assertIsNotNone(UNSORTABLE.search(bad), bad)
        self.assertIsNotNone(UNSORTABLE.search("site-plan-v2"))


if __name__ == "__main__":
    unittest.main()
