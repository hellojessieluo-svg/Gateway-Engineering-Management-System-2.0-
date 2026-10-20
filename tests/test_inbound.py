from __future__ import annotations

import json
import shutil
import tempfile
import unittest
import zipfile
from datetime import date
from pathlib import Path

from inbound_gate import InboundGate, IntakeItem, IntakeRequest
from common.places import GENERIC_FOLDER, INBOX_FOLDER, WITHDRAWN_FOLDER
from common.errors import ConflictError, ValidationError


def make_docx(path: Path, marker: str = "one") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("[Content_Types].xml", f"<Types>{marker}</Types>")
        archive.writestr("word/document.xml", f"<document>{marker}</document>")


class InboundGateIntakeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.base = Path(self.temp.name)
        self.sources = self.base / "sources"
        self.sources.mkdir()
        self.root = self.base / "library"
        self.gate = InboundGate(self.root)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def source(self, name: str, content: bytes = b"hello") -> Path:
        path = self.sources / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
        return path

    def request(self, items: list[IntakeItem], **overrides: object) -> IntakeRequest:
        values: dict[str, object] = {
            "stage": "construction-services",
            "project_id": "PRJ-2601",
            "project_name": "Riverside Wetland Restoration",
            "source_org": "North Bay Contractors",
            "source_channel": "WeChat",
            "received_date": date(2026, 8, 10),
            "subject": "pile testing",
            "items": items,
        }
        values.update(overrides)
        return IntakeRequest(**values)  # type: ignore[arg-type]

    def test_intake_keeps_original_names_and_records_ledger(self) -> None:
        report = self.source("pile-test-report (final).pdf", b"%PDF-1.7\n1 page\n%%EOF")
        photos = self.source("site-photos.zip")
        with zipfile.ZipFile(photos, "w") as archive:
            archive.writestr("a.jpg", "binary")

        result = self.gate.archive_intake(self.request([IntakeItem(report), IntakeItem(photos)]))

        expected_dir = (
            self.root / "PRJ-2601" / "construction-services" / INBOX_FOLDER
            / "2026.08.10-North Bay Contractors-pile testing"
        )
        self.assertEqual(result.archive_dir, expected_dir)
        self.assertTrue((expected_dir / "pile-test-report (final).pdf").is_file())
        self.assertTrue((expected_dir / "site-photos.zip").is_file())
        self.assertEqual(result.file_count, 2)
        self.assertFalse(result.duplicate)

        rows = self.gate.ledger.list_intakes()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["source_org"], "North Bay Contractors")
        self.assertEqual(rows[0]["archived"], 0)
        self.assertEqual(rows[0]["status"], "filed")
        files = self.gate.ledger.get_intake_files(rows[0]["intake_id"])
        self.assertEqual(
            {row["archived_name"] for row in files},
            {"pile-test-report (final).pdf", "site-photos.zip"},
        )
        self.assertEqual(files[0]["source_path"], str(report.resolve()))

    def test_unknown_format_is_accepted(self) -> None:
        photo = self.source("site.jpg", b"\xff\xd8\xff\xe0 not really a jpeg")
        result = self.gate.archive_intake(self.request([IntakeItem(photo)]))
        self.assertTrue((result.archive_dir / "site.jpg").is_file())
        files = self.gate.ledger.get_intake_files(result.record_id)
        checks = json.loads(files[0]["checks_json"])
        self.assertIn("only the content hash is recorded", " ".join(checks))

    def test_broken_known_format_is_archived_with_warning(self) -> None:
        broken = self.source("notes.docx", "this is not a real docx".encode("utf-8"))
        result = self.gate.archive_intake(self.request([IntakeItem(broken)]))
        self.assertTrue((result.archive_dir / "notes.docx").is_file())
        self.assertTrue(any("notes.docx" in text for text in result.warnings))

    def test_empty_and_temp_files_are_refused(self) -> None:
        empty = self.source("empty.pdf", b"")
        with self.assertRaises(ValidationError):
            self.gate.archive_intake(self.request([IntakeItem(empty)]))
        temporary = self.source("~$draft.docx", b"temp")
        with self.assertRaises(ValidationError):
            self.gate.archive_intake(self.request([IntakeItem(temporary)]))

    def test_same_batch_twice_is_recorded_once(self) -> None:
        report = self.source("contact-sheet.pdf", b"%PDF-1.7\ncontent\n%%EOF")
        first = self.gate.archive_intake(self.request([IntakeItem(report)]))
        second = self.gate.archive_intake(
            self.request([IntakeItem(report)], received_date=date(2026, 8, 12))
        )
        self.assertFalse(first.duplicate)
        self.assertTrue(second.duplicate)
        self.assertEqual(first.record_id, second.record_id)
        self.assertEqual(len(self.gate.ledger.list_intakes()), 1)
        self.assertEqual(self.gate.ledger.list_intakes()[0]["receipt_count"], 2)

    def test_second_batch_same_day_gets_suffix(self) -> None:
        first_file = self.source("one.txt", b"one")
        second_file = self.source("two.txt", b"two")
        first = self.gate.archive_intake(self.request([IntakeItem(first_file)]))
        second = self.gate.archive_intake(self.request([IntakeItem(second_file)]))
        self.assertEqual(first.archive_dir.name, "2026.08.10-North Bay Contractors-pile testing")
        self.assertEqual(second.archive_dir.name, "2026.08.10-North Bay Contractors-pile testing-2")

    def test_duplicate_names_in_one_batch(self) -> None:
        first = self.source("a/drawing.pdf", b"%PDF-1.7\na\n%%EOF")
        second = self.source("b/drawing.pdf", b"%PDF-1.7\nb\n%%EOF")
        result = self.gate.archive_intake(
            self.request([IntakeItem(first), IntakeItem(second)])
        )
        self.assertTrue((result.archive_dir / "drawing.pdf").is_file())
        self.assertTrue((result.archive_dir / "drawing (2).pdf").is_file())

    def test_folder_intake_keeps_structure(self) -> None:
        self.source("batch/drawings/plan.pdf", b"%PDF-1.7\na\n%%EOF")
        self.source("batch/photos/1.jpg", b"jpeg")
        items = [
            IntakeItem(self.sources / "batch" / "drawings" / "plan.pdf", "drawings/plan.pdf"),
            IntakeItem(self.sources / "batch" / "photos" / "1.jpg", "photos/1.jpg"),
        ]
        result = self.gate.archive_intake(self.request(items))
        self.assertTrue((result.archive_dir / "drawings" / "plan.pdf").is_file())
        self.assertTrue((result.archive_dir / "photos" / "1.jpg").is_file())

    def test_intake_without_project_goes_to_generic_folder(self) -> None:
        spec = self.source("standard.pdf", b"%PDF-1.7\nspec\n%%EOF")
        result = self.gate.archive_intake(
            self.request(
                [IntakeItem(spec)],
                project_id="",
                project_name="",
                source_org="Provincial Design Review Office",
            )
        )
        # <root>\_general\<stage>\inbox\<batch> -- three levels up is the general room
        self.assertEqual(result.archive_dir.parents[2].name, GENERIC_FOLDER)
        self.assertEqual(result.archive_dir.parent.name, INBOX_FOLDER)

    def test_archived_flag_is_ledger_only(self) -> None:
        """The "archive" flag is only a bit in the ledger (2026-09-08): ticked, the batch still sits directly under inbox\\."""
        item = self.source("procedure.docx")
        result = self.gate.archive_intake(
            self.request([IntakeItem(item)], archived=True)
        )
        self.assertEqual(result.archive_dir.parent.name, INBOX_FOLDER)
        with self.gate.ledger.session() as connection:
            row = connection.execute(
                "SELECT archived FROM intakes WHERE intake_id = ?", (result.record_id,)
            ).fetchone()
        self.assertEqual(int(row["archived"]), 1)

    def test_withdraw_moves_to_recoverable_area(self) -> None:
        report = self.source("contact-sheet.pdf", b"%PDF-1.7\ncontent\n%%EOF")
        result = self.gate.archive_intake(self.request([IntakeItem(report)]))
        target = self.gate.withdraw_intake(result.record_id)
        self.assertFalse(result.archive_dir.exists())
        self.assertTrue(target.is_dir())
        self.assertEqual(target.parent.name, WITHDRAWN_FOLDER)
        self.assertEqual(self.gate.ledger.list_intakes()[0]["status"], "withdrawn")

    def test_source_person_is_recorded_and_suggested(self) -> None:
        report = self.source("contact-sheet.pdf", b"%PDF-1.7\ncontent\n%%EOF")
        result = self.gate.archive_intake(
            self.request([IntakeItem(report)], source_person="Alice Chen")
        )

        row = self.gate.ledger.get_intake(result.record_id)
        self.assertEqual(row["source_person"], "Alice Chen")
        self.assertIn("Alice Chen", self.gate.ledger.list_source_persons())
        self.assertEqual(
            json.loads(row["manifest_json"])["source_person"], "Alice Chen"
        )

    def test_delete_moves_files_aside_and_clears_the_row(self) -> None:
        report = self.source("contact-sheet.pdf", b"%PDF-1.7\ncontent\n%%EOF")
        result = self.gate.archive_intake(self.request([IntakeItem(report)]))

        info = self.gate.delete_intake(result.record_id)

        # No trace in the ledger, yet not a single file lost -- what is deleted is the mistyped row, not the files
        self.assertEqual(self.gate.ledger.list_intakes(), [])
        self.assertFalse(result.archive_dir.exists())
        moved = Path(info["files_at"])
        self.assertEqual(moved.parent.name, WITHDRAWN_FOLDER)
        self.assertTrue((moved / "contact-sheet.pdf").is_file())
        with self.gate.ledger.session() as connection:
            for table in ("intake_files", "receipts"):
                left = connection.execute(
                    f"SELECT COUNT(*) AS n FROM {table} WHERE intake_id = ?",
                    (result.record_id,),
                ).fetchone()["n"]
                self.assertEqual(left, 0, table)

    def test_delete_after_withdraw_leaves_files_where_they_are(self) -> None:
        report = self.source("contact-sheet.pdf", b"%PDF-1.7\ncontent\n%%EOF")
        result = self.gate.archive_intake(self.request([IntakeItem(report)]))
        withdrawn = self.gate.withdraw_intake(result.record_id)

        info = self.gate.delete_intake(result.record_id)

        self.assertEqual(Path(info["files_at"]), withdrawn)
        self.assertTrue((withdrawn / "contact-sheet.pdf").is_file())
        self.assertEqual(self.gate.ledger.list_intakes(), [])

    def test_delete_tolerates_a_missing_archive_dir(self) -> None:
        report = self.source("contact-sheet.pdf", b"%PDF-1.7\ncontent\n%%EOF")
        result = self.gate.archive_intake(self.request([IntakeItem(report)]))
        shutil.rmtree(result.archive_dir)  # someone deleted it by hand in Explorer

        info = self.gate.delete_intake(result.record_id)

        self.assertEqual(info["files_at"], "")
        self.assertEqual(self.gate.ledger.list_intakes(), [])

    def test_delete_unknown_intake_is_refused(self) -> None:
        with self.assertRaises(ValueError):
            self.gate.delete_intake("no-such-record")

    def test_todo_flag_and_count(self) -> None:
        report = self.source("comments.pdf", b"%PDF-1.7\nc\n%%EOF")
        result = self.gate.archive_intake(self.request([IntakeItem(report)], todo=True))
        self.assertEqual(self.gate.ledger.todo_count(), 1)
        self.gate.set_todo(result.record_id, False)
        self.assertEqual(self.gate.ledger.todo_count(), 0)

    def test_export_csv(self) -> None:
        report = self.source("contact-sheet.pdf", b"%PDF-1.7\ncontent\n%%EOF")
        self.gate.archive_intake(self.request([IntakeItem(report)]))
        destination = self.base / "ledger.csv"
        self.gate.ledger.export_csv(destination)
        text = destination.read_text(encoding="utf-8-sig")
        self.assertIn("North Bay Contractors", text)
        self.assertIn("contact-sheet.pdf", text)


if __name__ == "__main__":
    unittest.main()
