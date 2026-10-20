"""Standards index: rows scanned from the three categories under _reference, plus hand-filled status/scope/notes."""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from common.config import STANDARD_CATEGORIES
from gate_api import cli, standards
from outbound_gate.core import OutboundGate


(TECH, CLIENT, COMPANY) = (c["name"] for c in STANDARD_CATEGORIES)
(TECH_DIR, CLIENT_DIR, COMPANY_DIR) = (c["folder"] for c in STANDARD_CATEGORIES)


class StandardsTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        base = Path(self.temp.name)
        self.root = base / "library"
        self.ledger_path = self.root / "ledger.db"
        OutboundGate(self.root, self.ledger_path)
        self.ref = base / "_reference"
        self.stds = self.ref / TECH_DIR
        orig = self.stds / standards.ORIGINALS
        orig.mkdir(parents=True)
        (orig / "GB 50139-2014 Inland navigation standard.pdf").write_bytes(b"%PDF")
        (orig / "GB 50139-2014 Inland navigation standard.md").write_text("# x", encoding="utf-8")
        (orig / "GB_T 11836-2023 Concrete and reinforced concrete sewer pipes.pdf").write_bytes(b"%PDF")
        (orig / "DG_TJ 08-2100-2012 Constructed wetland wastewater treatment.pdf").write_bytes(b"%PDF")
        (orig / "SL_T 386-2025 Slope and retaining wall design for water projects.pdf").write_bytes(b"%PDF")
        (orig / "readme.md").write_text("notes", encoding="utf-8")
        guides = self.stds / standards.GUIDES
        guides.mkdir()
        (guides / "Termite control guide for water projects.pdf").write_bytes(b"%PDF")
        (self.stds / standards.EXCERPTS / "excerpt_GB50139 Inland navigation standard").mkdir(parents=True)
        (self.stds / standards.EXCERPTS / "excerpt_SL386 Slope and retaining wall design for water projects").mkdir()
        (self.stds / standards.PENDING).mkdir()
        (self.stds / standards.PENDING / "JTS 181-2016_Waterway engineering design code.pdf").write_bytes(b"%PDF")
        # The other two categories are flat folders: no code, the whole file name is the title (2026-09-21)
        methods = self.ref / CLIENT_DIR
        (methods / "2024").mkdir(parents=True)
        (methods / "Survey and design management rules 2024 revision notice.pdf").write_bytes(b"%PDF")
        (methods / "Notice on issuing the management rules.doc").write_bytes(b"\xd0\xcf")
        (methods / "2024" / "Acme Water Authority notice 2024-56.pdf").write_bytes(b"%PDF")
        rules = self.ref / COMPANY_DIR
        rules.mkdir()
        (rules / "Drafting baseline requirements.md").write_text("# x", encoding="utf-8")

    def tearDown(self) -> None:
        self.temp.cleanup()

    def payload(self, **extra) -> dict:
        return {
            "ledger_path": str(self.ledger_path),
            "library_root": str(self.root),
            "reference_root": str(self.ref),
            **extra,
        }

    def test_scan_parses_names_and_matches_excerpts(self) -> None:
        data = cli.cmd_standards(self.payload())
        self.assertTrue(data["exists"])
        self.assertEqual(data["counts"],
                         {TECH: 5, CLIENT: 3, COMPANY: 1, "pending": 1})
        self.assertEqual([c["name"] for c in data["categories"]],
                         [TECH, CLIENT, COMPANY])
        self.assertTrue(all(c["exists"] for c in data["categories"]))
        by_code = {x["code"]: x for x in data["items"] if x["code"]}
        gb = by_code["GB 50139-2014"]
        self.assertEqual(gb["title"], "Inland navigation standard")
        self.assertEqual(gb["year"], "2014")
        self.assertTrue(gb["pdf"].endswith(".pdf"))
        self.assertTrue(gb["md"].endswith(".md"))
        self.assertIn("excerpt_GB50139", gb["excerpt"])
        self.assertEqual(gb["status"], "current")
        gbt = by_code["GB_T 11836-2023"]
        self.assertEqual(gbt["excerpt"], "")
        self.assertEqual(gbt["md"], "")
        # The year is the last four-digit number; the "_T" does not stop the excerpt from matching
        self.assertEqual(by_code["DG_TJ 08-2100-2012"]["year"], "2012")
        self.assertIn("excerpt_SL386", by_code["SL_T 386-2025"]["excerpt"])
        guide = next(x for x in data["items"] if x["kind"] == "guide")
        self.assertEqual(guide["title"], "Termite control guide for water projects")
        self.assertEqual(guide["code"], "")
        self.assertNotIn("readme", [x["key"] for x in data["items"]])
        self.assertEqual(data["pending"][0]["name"], "JTS 181-2016_Waterway engineering design code.pdf")
        # Standards come before guides; standards are ordered by compacted code
        self.assertEqual([x["kind"] for x in data["items"]],
                         ["standard"] * 4 + ["guide"] + ["regulation"] * 4)
        self.assertEqual([x["category"] for x in data["items"]],
                         [TECH] * 5 + [CLIENT] * 3 + [COMPANY])
        # The two flat categories: no code, the file stem is the title; one level of subfolders is read too
        flat = {x["title"]: x for x in data["items"] if x["category"] == CLIENT}
        self.assertEqual(sorted(flat),
                         ["Acme Water Authority notice 2024-56", "Notice on issuing the management rules",
                          "Survey and design management rules 2024 revision notice"])
        # A .doc must carry its path too: listed but unopenable is as good as unlisted
        self.assertTrue(flat["Notice on issuing the management rules"]["doc"].endswith(".doc"))
        self.assertEqual(flat["Notice on issuing the management rules"]["pdf"], "")
        self.assertEqual(flat["Survey and design management rules 2024 revision notice"]["doc"], "")
        self.assertEqual(flat["Acme Water Authority notice 2024-56"]["code"], "")
        self.assertEqual(flat["Acme Water Authority notice 2024-56"]["key"],
                         CLIENT + "::Acme Water Authority notice 2024-56")

    def test_hand_fields_overlay_disk_rows(self) -> None:
        saved = cli.cmd_standard_save(self.payload(
            key=TECH + "::GB 50139-2014 Inland navigation standard", status="to verify",
            scope="feasibility - navigation study", notes="check the 2024 revision"
        ))
        self.assertEqual(saved["status"], "to verify")
        data = cli.cmd_standards(self.payload())
        gb = next(x for x in data["items"] if x["code"] == "GB 50139-2014")
        self.assertEqual((gb["status"], gb["scope"], gb["notes"]),
                         ("to verify", "feasibility - navigation study", "check the 2024 revision"))
        # Saving again edits the entry, it does not add a second one
        cli.cmd_standard_save(self.payload(key=TECH + "::GB 50139-2014 Inland navigation standard", status="current"))
        data = cli.cmd_standards(self.payload())
        gb = next(x for x in data["items"] if x["code"] == "GB 50139-2014")
        self.assertEqual(gb["status"], "current")
        self.assertEqual(gb["scope"], "")

    def test_save_rejects_unknown_key_and_bad_status(self) -> None:
        with self.assertRaises(ValueError):
            cli.cmd_standard_save(self.payload(key=TECH + "::SL 252-2017 Classification of water projects", status="current"))
        with self.assertRaises(ValueError):
            # The same stem under the wrong category is unknown too: status is keyed on "category::stem"
            cli.cmd_standard_save(self.payload(key=COMPANY + "::GB 50139-2014 Inland navigation standard", status="current"))
        with self.assertRaises(ValueError):
            cli.cmd_standard_save(self.payload(key=TECH + "::GB 50139-2014 Inland navigation standard", status="whatever"))

    def test_missing_root_is_reported_not_raised(self) -> None:
        data = cli.cmd_standards(self.payload(reference_root=str(self.ref / "missing")))
        self.assertFalse(data["exists"])
        self.assertEqual(data["items"], [])
        self.assertFalse(any(c["exists"] for c in data["categories"]))


if __name__ == "__main__":
    unittest.main()
