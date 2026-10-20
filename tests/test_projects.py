"""New projects and official IDs: the command layer takes JSON and returns JSON; ledger and project room are set up in one go."""
from __future__ import annotations

import tempfile
import unittest
from datetime import date
from pathlib import Path

from gate_api import cli
from inbound_gate import InboundGate, IntakeItem, IntakeRequest
from outbound_gate import OutboundGate


class ProjectCommandTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.base = Path(self.temp.name)
        self.root = self.base / "library"
        self.ledger_path = self.root / "ledger.db"
        self.outbound = OutboundGate(self.root, self.ledger_path)
        # A fresh ledger has no projects; one with an official ID is registered for the collision tests
        self.outbound.ledger.register_project("PRJ-2601", "Lakeshore Park", "lakeshore")

    def tearDown(self) -> None:
        self.temp.cleanup()

    def payload(self, **extra) -> dict:
        return {
            "library_root": str(self.root),
            "ledger_path": str(self.ledger_path),
            **extra,
        }

    def test_create_without_id_gets_provisional_id_and_house(self):
        data = cli.cmd_project_create(
            self.payload(project_name="Riverside Wetland Restoration", folder="riverside")
        )
        self.assertTrue(data["project_id"].startswith("TMP-"))
        self.assertTrue((self.root / "riverside").is_dir())
        with self.outbound.ledger.session() as connection:
            row = connection.execute(
                "SELECT folder FROM projects WHERE project_id = ?",
                (data["project_id"],),
            ).fetchone()
        self.assertEqual(row["folder"], "riverside")

    def test_create_with_formal_id(self):
        data = cli.cmd_project_create(
            self.payload(project_name="North Canal Dredging", folder="north-canal", project_id="PRJ-2602")
        )
        self.assertEqual(data["project_id"], "PRJ-2602")
        with self.outbound.ledger.session() as connection:
            row = connection.execute(
                "SELECT project_name, folder FROM projects WHERE project_id = 'PRJ-2602'"
            ).fetchone()
        self.assertEqual(row["project_name"], "North Canal Dredging")
        self.assertEqual(row["folder"], "north-canal")

    def test_duplicates_are_rejected_not_silently_reused(self):
        cli.cmd_project_create(self.payload(project_name="Riverside Wetland Restoration", folder="riverside"))
        with self.assertRaises(ValueError):  # same name
            cli.cmd_project_create(self.payload(project_name="Riverside Wetland Restoration", folder="another-room"))
        with self.assertRaises(ValueError):  # short name clash
            cli.cmd_project_create(self.payload(project_name="Some Other Project", folder="riverside"))
        with self.assertRaises(ValueError):  # ID clashes with a registered project (case does not count)
            cli.cmd_project_create(
                self.payload(project_name="Yet Another Project", folder="yet-another-room", project_id="prj-2601")
            )

    def test_bind_moves_projects_issues_and_intakes(self):
        data = cli.cmd_project_create(
            self.payload(project_name="Riverside Wetland Restoration", folder="riverside")
        )
        old_id = data["project_id"]
        # An intake hangs on the temporary ID; after binding it must follow, history may not come loose
        source = self.base / "letter.pdf"
        source.write_bytes(b"%PDF-1.7\nx\n%%EOF")
        InboundGate(self.root, self.ledger_path).archive_intake(
            IntakeRequest(
                project_id=old_id,
                project_name="Riverside Wetland Restoration",
                source_org="Acme Water Authority",
                source_channel="WeChat",
                received_date=date(2026, 8, 20),
                subject="kickoff materials",
                items=[IntakeItem(source)],
                stage="construction-drawings",
            )
        )
        cli.cmd_project_bind(
            self.payload(old_project_id=old_id, new_project_id="PRJ-2604")
        )
        with self.outbound.ledger.session() as connection:
            stranded = connection.execute(
                "SELECT COUNT(*) AS n FROM intakes WHERE project_id = ?", (old_id,)
            ).fetchone()["n"]
            moved = connection.execute(
                "SELECT COUNT(*) AS n FROM intakes WHERE project_id = 'PRJ-2604'"
            ).fetchone()["n"]
            project = connection.execute(
                "SELECT folder FROM projects WHERE project_id = 'PRJ-2604'"
            ).fetchone()
        self.assertEqual(stranded, 0)
        self.assertEqual(moved, 1)
        self.assertEqual(project["folder"], "riverside")

    def _archive_one(self, project_id: str, project_name: str, name: str) -> None:
        """File one intake into a room, so the ledger stores an absolute path."""
        source = self.base / name
        source.write_bytes(b"%PDF-1.7\nx\n%%EOF")
        InboundGate(self.root, self.ledger_path).archive_intake(
            IntakeRequest(
                project_id=project_id,
                project_name=project_name,
                source_org="Acme Water Authority",
                source_channel="WeChat",
                received_date=date(2026, 9, 18),
                subject="kickoff materials",
                items=[IntakeItem(source)],
                stage="construction-drawings",
            )
        )

    def test_rename_folder_rewrites_paths_without_touching_prefix_neighbours(self):
        """Renaming a room may only touch that room: a neighbour whose name starts with it keeps every path in the ledger unchanged.

        Found on 2026-09-18 while renaming "riverside" to "riverside-west": path rewriting
        used a bare-prefix LIKE, so "riverside-east\\..." matched too and would have become
        "riverside-west-east\\...".
        """
        mine = cli.cmd_project_create(
            self.payload(project_name="Riverside West Reach", folder="riverside")
        )
        neighbour = cli.cmd_project_create(
            self.payload(project_name="Riverside East Reach", folder="riverside-east")
        )
        self._archive_one(mine["project_id"], "Riverside West Reach", "my-letter.pdf")
        self._archive_one(neighbour["project_id"], "Riverside East Reach", "neighbour-letter.pdf")
        with self.outbound.ledger.session() as connection:
            before = connection.execute(
                "SELECT archive_dir FROM intakes WHERE project_id = ?",
                (neighbour["project_id"],),
            ).fetchone()["archive_dir"]

        result = cli.cmd_project_rename(
            self.payload(project_id=mine["project_id"], folder="riverside-west")
        )

        self.assertTrue(result["dir_moved"])
        self.assertTrue((self.root / "riverside-west").is_dir())
        self.assertFalse((self.root / "riverside").exists())
        self.assertTrue((self.root / "riverside-east").is_dir())
        with self.outbound.ledger.session() as connection:
            moved = connection.execute(
                "SELECT archive_dir FROM intakes WHERE project_id = ?",
                (mine["project_id"],),
            ).fetchone()["archive_dir"]
            untouched = connection.execute(
                "SELECT archive_dir FROM intakes WHERE project_id = ?",
                (neighbour["project_id"],),
            ).fetchone()["archive_dir"]
        # Our own row moves along, the neighbour's stays put
        self.assertTrue(moved.startswith(str(self.root / "riverside-west")))
        self.assertEqual(untouched, before)
        self.assertEqual(result["paths_rewritten"], 1)

    def test_bind_rejects_taken_id(self):
        data = cli.cmd_project_create(
            self.payload(project_name="Riverside Wetland Restoration", folder="riverside")
        )
        with self.assertRaises(ValueError):
            cli.cmd_project_bind(
                self.payload(old_project_id=data["project_id"], new_project_id="PRJ-2601")
            )

    def test_stages_are_registered_and_ordered(self):
        """Stage registration is a filter, not a gate: replace, append, reject junk; stored in STAGES order."""
        data = cli.cmd_project_create(
            self.payload(project_name="Riverside Wetland Restoration", folder="riverside")
        )
        pid = data["project_id"]
        # Replace the whole list, deliberately given backwards -- it must be stored in STAGES order
        result = cli.cmd_project_stages(
            self.payload(project_id=pid, stages=["construction-drawings", "feasibility"])
        )
        self.assertEqual(result["stages"], ["feasibility", "construction-drawings"])
        # Append: this is what both gates use when filing
        result = cli.cmd_project_stages(self.payload(project_id=pid, add=["preliminary-design"]))
        self.assertEqual(result["stages"], ["feasibility", "preliminary-design", "construction-drawings"])
        # Unknown stages may not slip into the registry
        with self.assertRaises(ValueError):
            cli.cmd_project_stages(self.payload(project_id=pid, add=["made-up"]))
        # Always leaves as an array, the UI doesn't have to split a string
        context = cli.cmd_context(self.payload())
        mine = [p for p in context["projects"] if p["project_id"] == pid][0]
        self.assertEqual(mine["stages"], ["feasibility", "preliminary-design", "construction-drawings"])

    def test_stage_registry_starts_empty_and_means_show_all(self):
        """Never registered = empty list = the UI lists all six. Empty is not an error, it just hasn't emerged yet."""
        data = cli.cmd_project_create(
            self.payload(project_name="Riverside Wetland Restoration", folder="riverside")
        )
        context = cli.cmd_context(self.payload())
        mine = [p for p in context["projects"] if p["project_id"] == data["project_id"]][0]
        self.assertEqual(mine["stages"], [])


if __name__ == "__main__":
    unittest.main()
