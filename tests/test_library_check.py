"""Health check: at least one case per row of the table in spec section 9; new projects get a guide automatically."""
from __future__ import annotations

import tempfile
import unittest
from datetime import date
from pathlib import Path

from common.constants import SELFCHECK_FILENAME, SELFCHECK_SKELETON
from common.places import INBOX_FOLDER, OUTBOX_FOLDER, POOL_FOLDER
from gate_api import cli, library_check
from inbound_gate import InboundGate, IntakeItem, IntakeRequest
from outbound_gate import OutboundGate


class LibraryCheckTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name) / "library"
        self.ledger_path = self.root / "ledger.db"
        OutboundGate(self.root, self.ledger_path)
        self.inbound = InboundGate(self.root, self.ledger_path)
        (self.root / "_admin" / "templates").mkdir(parents=True)
        (self.root / "_admin" / "templates" / "project-AGENTS.md").write_text(
            "# {{short_name}} ({{project_id}})\n## Project card\n{{full_name}}\n## Layout\n## Ground rules\n## Start here\n",
            encoding="utf-8",
        )
        (self.root / "README.md").write_text("# Spec\n", encoding="utf-8")

    def tearDown(self) -> None:
        self.temp.cleanup()

    def payload(self, **extra) -> dict:
        # Point the spec back at the temporary library root, so the copy on this machine is never read
        return {
            "ledger_path": str(self.ledger_path),
            "library_root": str(self.root),
            "spec_path": str(self.root / "README.md"),
            **extra,
        }

    def whats(self, data: dict) -> set[str]:
        return {x["what"].split(": ")[0] for x in data["findings"]}

    def test_new_project_gets_a_guide_and_a_clean_room_passes(self) -> None:
        data = cli.cmd_project_create(self.payload(project_name="Riverside Wetland Restoration", folder="riverside"))
        guide = self.root / "riverside" / "AGENTS.md"
        self.assertTrue(guide.is_file())
        self.assertEqual(data["guide_path"], str(guide))
        text = guide.read_text(encoding="utf-8")
        self.assertIn("riverside", text)
        self.assertIn(data["project_id"], text)
        self.assertIn("Riverside Wetland Restoration", text)
        # The CLAUDE.md next to it is only a one-line pointer; at room or stage level it is not a loose file
        self.assertEqual((self.root / "riverside" / "CLAUDE.md").read_bytes(), b"@AGENTS.md\n")
        stage = self.root / "riverside" / "preliminary-design"
        (stage / POOL_FOLDER).mkdir(parents=True)
        (stage / "AGENTS.md").write_text("# preliminary-design\n", encoding="utf-8")
        (stage / "CLAUDE.md").write_bytes(b"@AGENTS.md\n")
        (stage / POOL_FOLDER / "index.md").write_text("| Deliverable | Files | Notes |\n", encoding="utf-8")
        report = cli.cmd_library_check(self.payload())
        mine = [x for x in report["findings"] if x["room"] == "riverside"]
        self.assertEqual(mine, [])

    def test_each_rule_fires(self) -> None:
        cli.cmd_project_create(self.payload(project_name="Riverside Wetland Restoration", folder="riverside"))
        house = self.root / "riverside"
        stage = house / "preliminary-design"
        (house / "plot.log").write_text("x", encoding="utf-8")                          # loose file in room + log junk
        (house / "BIM-delivery").mkdir()                                                 # non-stage folder in room
        (stage / "tmp").mkdir(parents=True)                                              # non-standard position
        (stage / INBOX_FOLDER / "exchange" / "2026.08.01-client-x").mkdir(parents=True)  # folder not in ledger
        (stage / OUTBOX_FOLDER / "2026.08.02-client").mkdir(parents=True)                # batch folder, but not in the ledger
        (stage / POOL_FOLDER).mkdir()
        (stage / POOL_FOLDER / "site-plan-final.dwg").write_bytes(b"AC1032")             # unsortable name
        (house / "AGENTS.md").write_text("# riverside\n## Project card\n", encoding="utf-8")  # incomplete guide
        (self.root / "stray-room").mkdir()                                               # room not registered
        report = cli.cmd_library_check(self.payload())
        whats = self.whats(report)
        for expected in (
            "loose file in room", "non-stage folder in room", "non-standard position", "folder not in ledger",
            "batch not in ledger", "unsortable name", "incomplete guide", "room not registered", "log junk",
        ):
            self.assertIn(expected, whats, expected)
        self.assertGreater(report["errors"], 0)
        self.assertGreater(report["warnings"], 0)

    def test_registered_batches_pass_and_missing_ones_are_reported(self) -> None:
        letter = Path(self.temp.name) / "a.pdf"
        letter.write_bytes(b"%PDF-1.7\nx\n%%EOF")
        result = self.inbound.archive_intake(
            IntakeRequest(
                project_id="PRJ-2601", project_name="Riverside Wetland Restoration", source_org="Acme Water Authority",
                source_channel="WeChat", received_date=date(2026, 9, 3), subject="comments",
                items=[IntakeItem(letter)], stage="feasibility",
            )
        )
        report = cli.cmd_library_check(self.payload())
        self.assertNotIn("batch not in ledger", self.whats(report))
        # Someone moved the folder away: ledger/disk mismatch
        result.archive_dir.rename(result.archive_dir.with_name("moved-away"))
        report = cli.cmd_library_check(self.payload())
        self.assertIn("ledger/disk mismatch", self.whats(report))

    def test_missing_template_is_reported_and_saving_one_clears_it(self) -> None:
        # setUp only put the project template in place; the other two are missing -- the check should warn (spec sections 8, 9)
        report = cli.cmd_library_check(self.payload())
        missing = {x["where"] for x in report["findings"] if x["what"] == "missing template"}
        self.assertEqual(len(missing), 2)
        for name in ("stage-AGENTS.md", SELFCHECK_FILENAME):
            self.assertTrue(any(name in w for w in missing), name)
        cli.cmd_template_save(self.payload(name=SELFCHECK_FILENAME, text="# Skeleton\n## What changed\n"))
        report = cli.cmd_library_check(self.payload())
        missing = {x["where"] for x in report["findings"] if x["what"] == "missing template"}
        self.assertEqual(len(missing), 1)

    def test_export_writes_a_markdown_report_per_room(self) -> None:
        # "Export": rerun the check and write one markdown per room section into _admin\health-checks\; the "Errors only" checkbox maps to errors_only
        (self.root / "loose-room").mkdir()
        (self.root / "loose-room" / "stray.txt").write_text("x", encoding="utf-8")
        out = cli.cmd_library_check_export(self.payload())
        path = Path(out["path"])
        self.assertEqual(path.parent, self.root / "_admin" / "health-checks")
        self.assertTrue(path.name.startswith("health-check-") and path.suffix == ".md")
        text = path.read_text(encoding="utf-8")
        self.assertIn("## loose-room (", text)
        self.assertIn("| error | loose-room\\stray.txt | loose file in room |", text)
        self.assertIn("missing template", text)
        self.assertEqual(out["count"], out["errors"] + out["warnings"])
        only = cli.cmd_library_check_export(self.payload(errors_only=True, ))
        text = Path(only["path"]).read_text(encoding="utf-8")
        self.assertEqual(only["count"], only["errors"])
        self.assertNotIn("missing template", text)
        self.assertIn("this report: errors only", text)
        # The report folder itself must not be reported as a room by the next check
        again = cli.cmd_library_check(self.payload())
        self.assertFalse(any(x["room"] == "_admin" and "health-checks" in x["where"] for x in again["findings"]))

    def test_template_save_only_takes_the_three_names(self) -> None:
        self.assertIn("template-save", cli.COMMANDS)  # the UI calls it by this name
        with self.assertRaises(ValueError):
            cli.cmd_template_save(self.payload(name="other.md", text="x"))
        with self.assertRaises(ValueError):
            cli.cmd_template_save(self.payload(name="..\\..\\escape.md", text="x"))
        with self.assertRaises(ValueError):
            cli.cmd_template_save(self.payload(name=SELFCHECK_FILENAME, text="   \n"))

    def test_selfcheck_skeleton_prefers_the_template_on_disk(self) -> None:
        root = self.root
        self.assertEqual(library_check.selfcheck_skeleton(root), SELFCHECK_SKELETON)  # fallback
        cli.cmd_template_save(self.payload(name=SELFCHECK_FILENAME, text="# Self-check\n## My own skeleton\n"))
        self.assertIn("My own skeleton", library_check.selfcheck_skeleton(root))
        # The task card and the issue page's "Write notes" must both follow the edited template
        letter = Path(self.temp.name) / "a.pdf"
        letter.write_bytes(b"%PDF-1.7\nx\n%%EOF")
        result = self.inbound.archive_intake(
            IntakeRequest(
                project_id="PRJ-2601", project_name="Riverside Wetland Restoration", source_org="Acme Water Authority",
                source_channel="WeChat", received_date=date(2026, 9, 3), subject="comments",
                items=[IntakeItem(letter)], stage="feasibility",
            )
        )
        brief = cli.cmd_intake_brief(self.payload(intake_id=result.record_id))
        self.assertIn("My own skeleton", brief["text"])
        # Before a draft is written, "Write notes" on the issue page also gets the template (pointing at a path that doesn't exist)
        draft = cli.cmd_selfcheck_read(self.payload(path=str(self.root / "not-written-yet.md")))
        self.assertFalse(draft["exists"])
        self.assertIn("My own skeleton", draft["text"])

    def test_spec_read_returns_charter_templates_and_guides(self) -> None:
        cli.cmd_project_create(self.payload(project_name="Riverside Wetland Restoration", folder="riverside"))
        (self.root / "riverside" / "preliminary-design").mkdir()
        (self.root / "riverside" / "preliminary-design" / "AGENTS.md").write_text(
            "# preliminary-design - deliverable layout\n", encoding="utf-8"
        )
        data = cli.cmd_spec_read(self.payload())
        # Only one spec comes back (here pointed at the temporary root's README.md), never a second one
        self.assertNotIn("charters", data)
        self.assertEqual(data["charter"]["name"], "README")
        self.assertTrue(data["charter"]["exists"])
        self.assertTrue(data["templates"]["project-AGENTS.md"]["exists"])
        # When the change-notes template is not on disk, the built-in skeleton is shown (saving it creates the file)
        skeleton = data["templates"][SELFCHECK_FILENAME]
        self.assertFalse(skeleton["exists"])
        self.assertTrue(skeleton["builtin"])
        self.assertIn("## What changed", skeleton["text"])
        guide = next(g for g in data["guides"] if g["folder"] == "riverside")
        self.assertTrue(guide["exists"])
        self.assertTrue(guide["path"].endswith("AGENTS.md"))
        self.assertIn("Project card", guide["text"])
        self.assertEqual([s["stage"] for s in guide["stages"]], ["preliminary-design"])
        self.assertIn("deliverable layout", guide["stages"][0]["text"])
