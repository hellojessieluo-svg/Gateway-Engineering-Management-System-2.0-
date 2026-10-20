"""Both gates end to end: intake -> issue -> next intake (in reply to that issue), one ledger tying them together.

Also checks the room layout along the way: evidence lands in the project room
(<short name>\\<stage>\\inbox, <short name>\\<stage>\\outbox), the folder name comes from
projects.folder, and falls back to the project ID when none is registered.
"""
from __future__ import annotations

import tempfile
import unittest
from datetime import date
from pathlib import Path

from common.constants import SELFCHECK_FILENAME
from common.places import DRAFT_FOLDER, INBOX_FOLDER, OUTBOX_FOLDER, WITHDRAWN_FOLDER
from gate_api import cli
from inbound_gate import InboundGate, IntakeItem, IntakeRequest
from outbound_gate import IssueRequest, OutboundGate, SourceItem


PROJECT = "PRJ-2601"
PROJECT_NAME = "Riverside Wetland Restoration"
STAGE = "construction-drawings"
CLIENT = "Acme Water Authority"


class TwoGateChainTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.base = Path(self.temp.name)
        self.sources = self.base / "sources"
        self.sources.mkdir()
        self.root = self.base / "library"
        self.ledger_path = self.root / "ledger.db"
        self.outbound = OutboundGate(self.root, self.ledger_path)
        self.inbound = InboundGate(self.root, self.ledger_path)
        # A fresh ledger has no projects; register this one without a short name
        self.outbound.ledger.register_project(PROJECT, PROJECT_NAME)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def payload(self) -> dict:
        """The two addresses the command layer needs: keep it away from this machine's real config."""
        return {"library_root": str(self.root), "ledger_path": str(self.ledger_path)}

    def make_dwg(self, name: str, marker: str = "one") -> Path:
        path = self.sources / name
        path.write_bytes(b"AC1032" + marker.encode("utf-8") + b"\x00" * 64)
        return path

    def make_pdf(self, name: str, marker: bytes = b"c") -> Path:
        path = self.sources / name
        path.write_bytes(b"%PDF-1.7\n" + marker + b"\n%%EOF")
        return path

    def intake(self, subject: str, path: Path, **extra):
        return self.inbound.archive_intake(
            IntakeRequest(
                project_id=PROJECT,
                project_name=PROJECT_NAME,
                source_org=CLIENT,
                source_channel="WeChat",
                received_date=date(2026, 8, 11),
                subject=subject,
                items=[IntakeItem(path)],
                stage=STAGE,
                **extra,
            )
        )

    def selfcheck_note(self) -> Path:
        note = self.sources / SELFCHECK_FILENAME
        note.write_text("# What changed\n- Entrance position\n", encoding="utf-8")
        return note

    def issue(self, path: Path, intake_id: str = ""):
        """An issue must hang on an intake: if none is given, log one on the spot so each test doesn't have to."""
        if not intake_id:
            letter = self.make_pdf(f"cause-{path.stem}.pdf", path.stem.encode("utf-8"))
            intake_id = self.intake(f"cause of {path.stem}", letter).record_id
        return self.outbound.create_issue(
            IssueRequest(
                project_id=PROJECT,
                project_name=PROJECT_NAME,
                stage=STAGE,
                issue_date=date(2026, 8, 11),
                purpose="submission",
                channels=["WeChat"],
                recipients=CLIENT,
                notes="Entrance moved per client comments, submitted for review",
                items=[SourceItem(path)],
                selfcheck_path=self.selfcheck_note(),
                intake_id=intake_id,
            )
        )

    def test_both_gates_share_one_ledger_file(self) -> None:
        self.assertTrue(self.ledger_path.exists())
        with self.outbound.ledger.session() as connection:
            tables = {
                row["name"]
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                )
            }
        for expected in ("projects", "issues", "intakes"):
            self.assertIn(expected, tables)

    def test_evidence_lands_in_the_project_room(self) -> None:
        opinion = self.intake("entrance relocation", self.make_pdf("client-comments.pdf"))
        # No short name registered: the folder name falls back to the project ID
        self.assertEqual(
            opinion.archive_dir,
            self.root / PROJECT / STAGE / INBOX_FOLDER
            / f"2026.08.11-{CLIENT}-entrance relocation",
        )

        # Issue has the same shape as intake: <room>\<stage>\outbox\<date-recipient-subject>\ (no subject here)
        result = self.issue(self.make_dwg("site-plan.dwg", "new"))
        self.assertEqual(
            result.output_dir,
            self.root / PROJECT / STAGE / OUTBOX_FOLDER / f"2026.08.11-{CLIENT}",
        )

    def test_registered_folder_wins_over_project_id(self) -> None:
        with self.outbound.ledger.session() as connection:
            connection.execute(
                "UPDATE projects SET folder = 'riverside' WHERE project_id = ?", (PROJECT,)
            )
        opinion = self.intake("second comments", self.make_pdf("comments-2.pdf", b"x"))
        self.assertEqual(opinion.archive_dir.parents[2].name, "riverside")
        result = self.issue(self.make_dwg("zoning-plan.dwg", "x"))
        self.assertEqual(result.output_dir.parents[1], self.root / "riverside" / STAGE)
        # The short name only decides what the room is called; the file name is untouched
        self.assertEqual(result.files[0].name, "zoning-plan.dwg")

    def test_issue_can_reply_to_multiple_intakes(self) -> None:
        """Replies-to is multi-select: one issue hangs on several intakes, all of them close, the chain stays whole."""
        first = self.intake("first matter", self.make_pdf("letter-a.pdf", b"a"))
        second = self.intake(
            "second matter", self.make_pdf("letter-b.pdf", b"b"), todo=True
        )
        result = self.outbound.create_issue(
            IssueRequest(
                project_id=PROJECT,
                project_name=PROJECT_NAME,
                stage=STAGE,
                issue_date=date(2026, 8, 11),
                purpose="submission",
                channels=["WeChat"],
                recipients=CLIENT,
                notes="One version answering two matters",
                items=[SourceItem(self.make_dwg("combined-reply.dwg", "multi"))],
                selfcheck_path=self.selfcheck_note(),
                intake_id="",
                intake_ids=[first.record_id, second.record_id],
            )
        )
        issue_id = result.files[0].issue_id
        with self.outbound.ledger.session() as connection:
            # The intake_id column stores the first one, the link table stores all of them
            row = connection.execute(
                "SELECT intake_id FROM issues WHERE issue_id = ?", (issue_id,)
            ).fetchone()
            self.assertEqual(row["intake_id"], first.record_id)
            linked = {
                r["intake_id"]
                for r in connection.execute(
                    "SELECT intake_id FROM issue_intakes WHERE issue_id = ?",
                    (issue_id,),
                )
            }
        self.assertEqual(linked, {first.record_id, second.record_id})
        # Both intakes count as answered -- closing is derived, not clicked
        listed = cli.cmd_intakes({**self.payload(), "project_id": PROJECT})["intakes"]
        counts = {r["intake_id"]: r["issue_count"] for r in listed}
        self.assertEqual(counts[first.record_id], 1)
        self.assertEqual(counts[second.record_id], 1)
        # Both intakes block deletion -- the second one no longer slips through
        blocked = cli.cmd_delete_intakes(
            {**self.payload(), "intake_ids": [second.record_id]}
        )
        self.assertEqual(len(blocked["failed"]), 1)
        # Relinking = the whole set replaced by that one; the column and the link table move together
        third = self.intake("third matter", self.make_pdf("letter-c.pdf", b"c"))
        cli.cmd_relink_issue(
            {**self.payload(), "issue_id": issue_id, "intake_id": third.record_id}
        )
        with self.outbound.ledger.session() as connection:
            row = connection.execute(
                "SELECT intake_id FROM issues WHERE issue_id = ?", (issue_id,)
            ).fetchone()
            relinked = [
                r["intake_id"]
                for r in connection.execute(
                    "SELECT intake_id FROM issue_intakes WHERE issue_id = ?",
                    (issue_id,),
                )
            ]
        self.assertEqual(row["intake_id"], third.record_id)
        self.assertEqual(relinked, [third.record_id])
        # Linking later: as many as at issue time -- the list given is the whole set after the change
        cli.cmd_relink_issue(
            {
                **self.payload(),
                "issue_id": issue_id,
                "intake_ids": [third.record_id, first.record_id],
            }
        )
        with self.outbound.ledger.session() as connection:
            row = connection.execute(
                "SELECT intake_id FROM issues WHERE issue_id = ?", (issue_id,)
            ).fetchone()
            both = {
                r["intake_id"]
                for r in connection.execute(
                    "SELECT intake_id FROM issue_intakes WHERE issue_id = ?",
                    (issue_id,),
                )
            }
        self.assertEqual(row["intake_id"], third.record_id)
        self.assertEqual(both, {third.record_id, first.record_id})

    def test_reply_links_next_intake_to_the_issue(self) -> None:
        result = self.issue(self.make_dwg("site-plan.dwg", "v1"))
        issue_id = result.files[0].issue_id
        review = self.intake(
            "review feedback", self.make_pdf("review-comments.pdf", b"review"),
            replies_to=issue_id,
        )
        with self.outbound.ledger.session() as connection:
            row = connection.execute(
                "SELECT replies_to, stage FROM intakes WHERE intake_id = ?",
                (review.record_id,),
            ).fetchone()
        self.assertEqual(row["replies_to"], issue_id)
        self.assertEqual(row["stage"], STAGE)

    def test_one_batch_is_one_row_in_the_ledger(self) -> None:
        """A bunch of files sent with one click = one row in the ledger, with the file names listed in it."""
        letter = self.make_pdf("client-comments.pdf", b"letter")
        source = self.intake("entrance comments", letter)
        drawing = self.make_dwg("site-plan.dwg", "v1")
        quantity = self.sources / "quantities.xlsx"
        quantity.write_bytes(b"PK\x03\x04")  # placeholder, overwritten by a real xlsx below
        import zipfile

        with zipfile.ZipFile(quantity, "w") as archive:
            archive.writestr("[Content_Types].xml", "<Types/>")
            archive.writestr("xl/workbook.xml", "<workbook/>")
        result = self.outbound.create_issue(
            IssueRequest(
                project_id=PROJECT,
                project_name=PROJECT_NAME,
                stage=STAGE,
                issue_date=date(2026, 8, 11),
                purpose="submission",
                channels=["WeChat"],
                recipients=CLIENT,
                notes="One batch, two files",
                items=[SourceItem(drawing), SourceItem(quantity)],
                selfcheck_path=self.selfcheck_note(),
                intake_id=source.record_id,
            )
        )

        rows = cli.cmd_issues(self.payload())["issues"]
        self.assertEqual(len(rows), 1, "Two files issued at once should be one row in the ledger")
        row = rows[0]
        self.assertEqual(row["file_count"], 2)
        self.assertEqual(sorted(row["files"]), ["quantities.xlsx", "site-plan.dwg"])
        self.assertEqual(sorted(row["issue_ids"]), sorted(f.issue_id for f in result.files))
        self.assertEqual(row["status"], "issued")

        # Withdraw one file of the batch: the row must show "partly withdrawn", not still "issued"
        self.outbound.withdraw_issue(result.files[0].issue_id)
        self.assertEqual(cli.cmd_issues(self.payload())["issues"][0]["status"], "partly withdrawn")

    def test_batch_counts_sent_and_kept_files(self) -> None:
        """A batch with kept files: the list counts "sent N, kept M", the detail gives each file its role (2026-09-22)."""
        letter = self.make_pdf("client-comments.pdf", b"letter")
        source = self.intake("entrance comments", letter, todo=True)
        drawing = self.make_dwg("site-plan.dwg", "v1")
        basis = self.make_dwg("calc-basis.dwg", "basis")
        created = cli.cmd_issue_create({
            **self.payload(),
            "project_id": PROJECT,
            "project_name": PROJECT_NAME,
            "stage": STAGE,
            "issue_date": "2026-08-11",
            "channels": ["WeChat"],
            "recipients": CLIENT,
            "items": [{"path": str(drawing)}, {"path": str(basis), "role": "kept"}],
            "selfcheck_path": str(self.selfcheck_note()),
            "intake_id": source.record_id,
        })
        self.assertEqual({f["name"]: f["role"] for f in created["files"]},
                         {"site-plan.dwg": "sent", "calc-basis.dwg": "kept"})
        # Both files and the notes lie together in the folder, no subfolders
        batch_dir = Path(created["output_dir"])
        self.assertEqual(sorted(p.name for p in batch_dir.iterdir()),
                         sorted([SELFCHECK_FILENAME, "site-plan.dwg", "calc-basis.dwg"]))
        # The intake closes as usual
        self.assertEqual(self.inbound.ledger.todo_count(), 0)

        row = cli.cmd_issues(self.payload())["issues"][0]
        self.assertEqual(row["file_count"], 2)
        self.assertEqual((row["sent_count"], row["kept_count"]), (1, 1))
        self.assertEqual(dict(zip(row["files"], row["roles"])),
                         {"site-plan.dwg": "sent", "calc-basis.dwg": "kept"})
        detail = cli.cmd_issue_detail({**self.payload(), "issue_id": created["files"][0]["issue_id"]})
        self.assertEqual({f["name"]: f["role"] for f in detail["files"]},
                         {"site-plan.dwg": "sent", "calc-basis.dwg": "kept"})

    def issue_via_cli(self, drawing: Path, intake_id: str, selfcheck: str) -> dict:
        return cli.cmd_issue_create({
            **self.payload(),
            "project_id": PROJECT,
            "project_name": PROJECT_NAME,
            "stage": STAGE,
            "issue_date": "2026-08-11",
            "channels": ["WeChat"],
            "recipients": CLIENT,
            "items": [{"path": str(drawing)}],
            "selfcheck_path": selfcheck,
            "intake_ids": [intake_id],
        })

    def test_draft_lives_in_library_tmp_and_goes_away_after_issue(self) -> None:
        """The notes draft lives in the library root's _drafts\\; once the issue is filed the program deletes the used one (2026-09-26)."""
        source = self.intake("entrance comments", self.make_pdf("client-comments.pdf", b"letter"), todo=True)
        # The task card points at, and "Write notes" saves to, the same file: <root>\_drafts\<id-stage>-change-notes.md
        expected = self.root / DRAFT_FOLDER / f"{PROJECT}-{STAGE}-change-notes.md"
        brief = cli.cmd_intake_brief({**self.payload(), "intake_id": source.record_id})["text"]
        self.assertIn(str(expected), brief)
        saved = cli.cmd_selfcheck_draft({
            **self.payload(), "project_id": PROJECT, "stage": STAGE,
            "text": "# Change & self-check notes\n## What changed\n- Entrance position\n",
        })
        self.assertEqual(Path(saved["path"]), expected)
        # The health check doesn't treat _drafts as a room
        report = cli.cmd_library_check(self.payload())
        self.assertFalse(any(x["room"] == DRAFT_FOLDER for x in report["findings"]))

        drawing = self.make_dwg("site-plan.dwg", "v1")
        created = self.issue_via_cli(drawing, source.record_id, saved["path"])
        self.assertEqual(created["draft_removed"], str(expected))
        self.assertFalse(expected.exists())
        landed = Path(created["output_dir"]) / SELFCHECK_FILENAME
        self.assertIn("Entrance position", landed.read_text(encoding="utf-8"))

        # Resend the whole batch unchanged: the program doesn't copy the notes, the batch's copy
        # is from last time and doesn't match the new draft -- the draft stays
        again = cli.cmd_selfcheck_draft({
            **self.payload(), "project_id": PROJECT, "stage": STAGE,
            "text": "# Change & self-check notes\n## What changed\n- Resent unchanged\n",
        })
        second = self.issue_via_cli(drawing, source.record_id, again["path"])
        self.assertTrue(all(f["reused"] for f in second["files"]))
        self.assertEqual(second["draft_removed"], "")
        self.assertTrue(Path(again["path"]).is_file())

    def test_hand_picked_note_outside_tmp_is_left_alone(self) -> None:
        """A .md picked by hand from elsewhere is the person's own file: not deleted after issuing (2026-09-26)."""
        source = self.intake("entrance comments", self.make_pdf("client-comments.pdf", b"letter"))
        note = self.selfcheck_note()
        created = self.issue_via_cli(self.make_dwg("site-plan.dwg", "v1"), source.record_id, str(note))
        self.assertEqual(created["draft_removed"], "")
        self.assertTrue(note.is_file())

    def test_backfilling_roles_marks_an_old_batch(self) -> None:
        """Old batches are all "sent"; issue-role marks the working file as "kept" by name, touching no file or folder."""
        letter = self.make_pdf("client-comments.pdf", b"letter")
        source = self.intake("entrance comments", letter)
        drawing = self.make_dwg("site-plan.dwg", "v1")
        basis = self.make_dwg("calc-basis.dwg", "basis")
        result = self.outbound.create_issue(
            IssueRequest(
                project_id=PROJECT,
                project_name=PROJECT_NAME,
                stage=STAGE,
                issue_date=date(2026, 8, 11),
                purpose="submission",
                channels=["WeChat"],
                recipients=CLIENT,
                items=[SourceItem(drawing), SourceItem(basis)],
                selfcheck_path=self.selfcheck_note(),
                intake_id=source.record_id,
            )
        )
        before = sorted(p.name for p in result.output_dir.iterdir())
        self.assertEqual(cli.cmd_issues(self.payload())["issues"][0]["kept_count"], 0)

        out = cli.cmd_issue_role({
            **self.payload(), "batch": result.batch_id, "roles": {"calc-basis.dwg": "kept"},
        })
        self.assertEqual([(f["file_name"], f["from"], f["to"]) for f in out["files"]],
                         [("calc-basis.dwg", "sent", "kept")])
        row = cli.cmd_issues(self.payload())["issues"][0]
        self.assertEqual((row["sent_count"], row["kept_count"]), (1, 1))
        self.assertEqual(sorted(p.name for p in result.output_dir.iterdir()), before)
        self.assertEqual(self.outbound.ledger.get_issue(result.files[1].issue_id)["role"], "kept")

        # Unknown roles and file names not in this batch are both refused
        with self.assertRaises(ValueError):
            cli.cmd_issue_role({**self.payload(), "batch": result.batch_id, "roles": {"calc-basis.dwg": "attachment"}})
        with self.assertRaises(ValueError):
            cli.cmd_issue_role({**self.payload(), "batch": result.batch_id, "roles": {"missing.pdf": "kept"}})

    def test_issue_list_filters_by_stage(self) -> None:
        """When an intake picks "in reply to", it should only see issues of the same stage (2026-09-18).

        A batch sent during preliminary design shouldn't show up on a construction-drawings
        intake; old rows with an empty stage (from before the redesign) can't be placed, so
        following the intake side's rule they are not hidden.
        """
        self.issue(self.make_dwg("site-plan.dwg"))  # construction-drawings
        letter = self.make_pdf("prelim-cause.pdf", b"letter")
        early = self.intake("preliminary design cause", letter)
        self.outbound.create_issue(
            IssueRequest(
                project_id=PROJECT,
                project_name=PROJECT_NAME,
                stage="preliminary-design",
                issue_date=date(2026, 8, 11),
                purpose="submission",
                channels=["WeChat"],
                recipients=CLIENT,
                notes="The preliminary design version",
                items=[SourceItem(self.make_dwg("prelim-site-plan.dwg", "early"))],
                selfcheck_path=self.selfcheck_note(),
                intake_id=early.record_id,
            )
        )

        everything = cli.cmd_issues(self.payload())["issues"]
        self.assertEqual(len(everything), 2, "Without a stage, both rows still come back")
        for stage in ("construction-drawings", "preliminary-design"):
            rows = cli.cmd_issues({**self.payload(), "stage": stage})["issues"]
            self.assertEqual([r["stage"] for r in rows], [stage])

        # Old rows: one with an empty stage must be visible under both stages, or it can never be linked
        with self.outbound.ledger.session() as connection:
            connection.execute("UPDATE issues SET stage = '' WHERE stage = 'preliminary-design'")
            connection.commit()
        rows = cli.cmd_issues({**self.payload(), "stage": "construction-drawings"})["issues"]
        self.assertEqual(sorted(r["stage"] for r in rows), ["", "construction-drawings"])

    def test_issue_row_carries_the_intake_it_answers(self) -> None:
        letter = self.make_pdf("client-comments.pdf", b"letter")
        source = self.intake("entrance comments", letter)
        result = self.issue(self.make_dwg("site-plan.dwg", "v1"), source.record_id)

        row = cli.cmd_issues(self.payload())["issues"][0]
        self.assertEqual(row["issue_id"], result.files[0].issue_id)
        self.assertEqual(row["intake_id"], source.record_id)
        self.assertEqual(row["intake_date"], "2026-08-11")
        self.assertEqual(row["intake_org"], CLIENT)
        self.assertEqual(row["intake_subject"], "entrance comments")

    def test_intake_detail_shows_files_and_the_issues_hanging_on_it(self) -> None:
        """A ledger row opens into a page: intake detail lists its files and the issues hanging on it; the folder is only an address."""
        letter = self.make_pdf("client-comments.pdf", b"letter")
        source = self.intake("entrance comments", letter, source_person="Carol Wu")
        result = self.issue(self.make_dwg("site-plan.dwg", "v1"), source.record_id)

        detail = cli.cmd_intake_detail({**self.payload(), "intake_id": source.record_id})
        self.assertEqual(detail["intake"]["subject"], "entrance comments")
        self.assertEqual(detail["intake"]["source_person"], "Carol Wu")
        self.assertEqual(Path(detail["intake"]["archive_dir"]), source.archive_dir)
        self.assertEqual([f["name"] for f in detail["files"]], ["client-comments.pdf"])
        self.assertIsNone(detail["replies_to"])
        self.assertEqual(len(detail["issues"]), 1)
        hanging = detail["issues"][0]
        self.assertEqual(hanging["issue_id"], result.files[0].issue_id)
        self.assertEqual(hanging["files"], ["site-plan.dwg"])
        self.assertEqual(hanging["recipients"], CLIENT)
        self.assertEqual(hanging["status"], "issued")
        self.assertNotIn("manifest_json", detail["intake"])
        with self.assertRaises(ValueError):
            cli.cmd_intake_detail({**self.payload(), "intake_id": "no-such-row"})

    def test_issue_detail_carries_the_whole_process(self) -> None:
        """Issue detail: every file in the batch, the intakes it answers, the delivery log, the notes text, later intakes in reply to it."""
        letter = self.make_pdf("client-comments.pdf", b"letter")
        source = self.intake("entrance comments", letter)
        result = self.issue(self.make_dwg("site-plan.dwg", "v1"), source.record_id)
        issue_id = result.files[0].issue_id
        # Next round: the client sends more comments in reply to this issue
        again = self.make_pdf("second-comments.pdf", b"again")
        follow = self.intake("second comments", again, replies_to=issue_id)

        before = cli.cmd_issue_detail({**self.payload(), "issue_id": issue_id})
        self.assertTrue(before["selfcheck"]["exists"])
        self.assertIn("Entrance position", before["selfcheck"]["text"])
        self.assertEqual(before["issue"]["status"], "issued")

        # Withdraw the whole batch: files and notes move to _withdrawn together; the detail must say "notes not on disk"
        self.outbound.withdraw_issue(issue_id)
        detail = cli.cmd_issue_detail({**self.payload(), "issue_id": issue_id})
        self.assertFalse(detail["selfcheck"]["exists"])
        self.assertEqual(detail["issue"]["issue_ids"], [issue_id])
        self.assertEqual(detail["issue"]["status"], "withdrawn")
        self.assertEqual(detail["issue"]["recipients"], CLIENT, "The withdrawal row has empty notes and must not hide the real ones")
        self.assertEqual(detail["issue"]["notes"], "Entrance moved per client comments, submitted for review")
        self.assertEqual([f["name"] for f in detail["files"]], ["site-plan.dwg"])
        self.assertEqual(detail["files"][0]["status"], "withdrawn")
        self.assertEqual([x["intake_id"] for x in detail["intakes"]], [source.record_id])
        self.assertEqual(detail["intakes"][0]["subject"], "entrance comments")
        self.assertEqual([x["intake_id"] for x in detail["followups"]], [follow.record_id])
        self.assertEqual([d["action"] for d in detail["deliveries"]][-1], "withdrawn")
        self.assertGreaterEqual(len(detail["deliveries"]), 2)
        self.assertIn("WeChat", detail["deliveries"][0]["channels"])
        with self.assertRaises(ValueError):
            cli.cmd_issue_detail({**self.payload(), "issue_id": "no-such-row"})

    def test_issuing_against_an_intake_settles_it(self) -> None:
        letter = self.make_pdf("client-comments.pdf", b"letter")
        source = self.intake("entrance comments", letter, todo=True)
        self.assertEqual(self.inbound.ledger.todo_count(), 1)

        drawing = self.make_dwg("site-plan.dwg", "v1")
        cli.cmd_issue_create({
            **self.payload(),
            "project_id": PROJECT,
            "project_name": PROJECT_NAME,
            "stage": STAGE,
            "issue_date": "2026-08-11",
            "purpose": "submission",
            "channels": ["WeChat"],
            "recipients": CLIENT,
            "notes": "Entrance moved per client comments, submitted for review",
            "items": [{"path": str(drawing)}],
            "selfcheck_path": str(self.selfcheck_note()),
            "intake_id": source.record_id,
        })

        # Sent means answered: the flag drops automatically and the row counts as done
        self.assertEqual(self.inbound.ledger.todo_count(), 0)
        row = next(
            x for x in cli.cmd_intakes({**self.payload(), "limit": 50})["intakes"]
            if x["intake_id"] == source.record_id
        )
        self.assertEqual(row["issue_count"], 1)
        self.assertEqual(row["todo"], 0)

    def test_withdrawing_puts_the_intake_back_in_the_queue(self) -> None:
        """Withdrawing = the files came back: the intake owes a reply again and must show up in the issue form's intake list.

        2026-09-21: issue_count used to count only issue_intakes links and ignore the issue's
        status, so the link kept after a withdrawal marked the intake "done" forever and it
        could no longer be found in the dropdown when resending.
        """
        letter = self.make_pdf("client-comments.pdf", b"letter")
        source = self.intake("entrance comments", letter)
        result = self.issue(self.make_dwg("site-plan.dwg", "v1"), source.record_id)

        def count() -> int:
            row = next(
                x for x in cli.cmd_intakes({**self.payload(), "limit": 50})["intakes"]
                if x["intake_id"] == source.record_id
            )
            return row["issue_count"]

        self.assertEqual(count(), 1)
        self.outbound.withdraw_issue(result.files[0].issue_id)
        self.assertEqual(count(), 0, "After a withdrawal the intake should be back to 'still owes a reply'")

        # Resend a version: the new issue hangs on it and it counts as answered again
        self.issue(self.make_dwg("site-plan.dwg", "v2"), source.record_id)
        self.assertEqual(count(), 1)

    def test_backfilling_subjects_renames_old_batches(self) -> None:
        """Backfilling subjects on old batches (2026-09-11): the folder is renamed, the three ledger paths and the log change with it."""
        letter = self.make_pdf("client-comments.pdf", b"letter")
        source = self.intake("entrance comments", letter)
        result = self.issue(self.make_dwg("site-plan.dwg", "v1"), source.record_id)
        # The outbox folder name before subjects existed: date-recipient
        old_dir = result.output_dir
        self.assertEqual(old_dir.name, f"2026.08.11-{CLIENT}")

        dry = cli.cmd_issue_subject({**self.payload()})
        self.assertTrue(dry["dry_run"])
        self.assertEqual(dry["problems"], [])
        plan = dry["plans"][0]
        # The candidate subject comes from the intake first -- the issue answers that matter, so it's this batch's title; the dry run touched nothing
        self.assertEqual(plan["subject"], "entrance comments")
        self.assertTrue(old_dir.is_dir())

        done = cli.cmd_issue_subject({
            **self.payload(),
            "subjects": {plan["batch"]: "entrance resubmission"},
            "apply": True,
        })
        self.assertFalse(done["dry_run"])
        new_dir = (
            self.root / PROJECT / STAGE / OUTBOX_FOLDER
            / f"2026.08.11-{CLIENT}-entrance resubmission"
        )
        self.assertTrue(new_dir.is_dir())
        self.assertFalse(old_dir.exists())
        self.assertTrue((new_dir / "site-plan.dwg").is_file())

        detail = cli.cmd_issue_detail(
            {**self.payload(), "issue_id": result.files[0].issue_id}
        )
        self.assertEqual(detail["issue"]["subject"], "entrance resubmission")
        self.assertEqual(detail["issue"]["output_dir"], str(new_dir))
        self.assertEqual(detail["files"][0]["path"], str(new_dir / "site-plan.dwg"))
        self.assertEqual(detail["selfcheck"]["path"], str(new_dir / SELFCHECK_FILENAME))
        self.assertTrue(Path(detail["selfcheck"]["path"]).is_file())

        # Once filled in it isn't touched again: the name is already right, only a "ledger only" no-op is left
        again = cli.cmd_issue_subject({**self.payload()})
        self.assertTrue(again["plans"][0].get("ledger_only"))

    def test_redating_a_batch_moves_the_folder_with_it(self) -> None:
        """Changing the issue date: the whole batch changes in the ledger and the folder is renamed -- addresses are always computed."""
        letter = self.make_pdf("client-comments.pdf", b"letter")
        source = self.intake("entrance comments", letter)
        result = self.issue(self.make_dwg("site-plan.dwg", "v1"), source.record_id)
        issue_id = result.files[0].issue_id
        batch = cli.cmd_issue_detail({**self.payload(), "issue_id": issue_id})["issue"]["batch_id"]
        old_dir = result.output_dir

        dry = cli.cmd_issue_redate(
            {**self.payload(), "batch": batch, "issue_date": "2026-08-05"}
        )
        self.assertTrue(dry["dry_run"])
        self.assertEqual(dry["plan"]["to_date"], "2026-08-05")
        self.assertTrue(old_dir.is_dir(), "A dry run must not touch anything")

        done = cli.cmd_issue_redate(
            {**self.payload(), "batch": batch, "issue_date": "2026-08-05", "apply": True}
        )
        self.assertFalse(done["dry_run"])
        new_dir = self.root / PROJECT / STAGE / OUTBOX_FOLDER / f"2026.08.05-{CLIENT}"
        self.assertTrue(new_dir.is_dir())
        self.assertFalse(old_dir.exists())
        self.assertTrue((new_dir / "site-plan.dwg").is_file())

        detail = cli.cmd_issue_detail({**self.payload(), "issue_id": issue_id})
        self.assertEqual(detail["issue"]["issue_date"], "2026-08-05")
        self.assertEqual(detail["issue"]["output_dir"], str(new_dir))
        self.assertEqual(detail["files"][0]["path"], str(new_dir / "site-plan.dwg"))
        self.assertEqual(detail["selfcheck"]["path"], str(new_dir / SELFCHECK_FILENAME))
        self.assertTrue(Path(detail["selfcheck"]["path"]).is_file())
        # The time of registration is a separate fact and does not follow the date
        self.assertTrue(str(detail["issue"]["created_at"]).startswith(date.today().isoformat()))

    def test_changing_recipients_renames_the_batch_folder(self) -> None:
        """Wrong recipient: the whole batch changes in the ledger and the folder is renamed -- addresses are always computed."""
        result = self.issue(self.make_dwg("site-plan.dwg", "v1"))
        issue_id = result.files[0].issue_id
        batch = cli.cmd_issue_detail({**self.payload(), "issue_id": issue_id})["issue"]["batch_id"]
        old_dir = result.output_dir

        dry = cli.cmd_issue_recipients(
            {**self.payload(), "batch": batch, "recipients": "Alice Chen"}
        )
        self.assertTrue(dry["dry_run"])
        self.assertEqual(dry["plan"]["was"], CLIENT)
        self.assertEqual(dry["plan"]["now"], "Alice Chen")
        self.assertTrue(old_dir.is_dir(), "A dry run must not touch anything")

        done = cli.cmd_issue_recipients(
            {**self.payload(), "batch": batch, "recipients": "Alice Chen", "apply": True}
        )
        self.assertFalse(done["dry_run"])
        new_dir = self.root / PROJECT / STAGE / OUTBOX_FOLDER / "2026.08.11-Alice Chen"
        self.assertTrue(new_dir.is_dir())
        self.assertFalse(old_dir.exists())
        self.assertTrue((new_dir / "site-plan.dwg").is_file())

        detail = cli.cmd_issue_detail({**self.payload(), "issue_id": issue_id})
        self.assertEqual(detail["issue"]["recipients"], "Alice Chen")
        self.assertEqual(detail["issue"]["output_dir"], str(new_dir))
        self.assertEqual(detail["files"][0]["path"], str(new_dir / "site-plan.dwg"))
        self.assertEqual(detail["selfcheck"]["path"], str(new_dir / SELFCHECK_FILENAME))
        self.assertTrue(Path(detail["selfcheck"]["path"]).is_file())
        # A subject that wasn't given must not appear out of nowhere: the folder is still "date-recipient"
        self.assertEqual(detail["issue"]["subject"], "")

    def test_changing_a_source_org_renames_the_intake_folder(self) -> None:
        """Wrong source (a person recorded as the organization): ledger and folder name change together."""
        opinion = self.intake("site photos", self.make_pdf("photos.pdf", b"photo"))
        old_dir = opinion.archive_dir

        dry = cli.cmd_intake_source(
            {**self.payload(), "intake_id": opinion.record_id, "source_org": "Alice Chen"}
        )
        self.assertTrue(dry["dry_run"])
        self.assertTrue(dry["changed"])
        self.assertEqual(dry["was"], CLIENT)
        self.assertTrue(old_dir.is_dir(), "A dry run must not touch anything")

        done = cli.cmd_intake_source(
            {
                **self.payload(),
                "intake_id": opinion.record_id,
                "source_org": "Alice Chen",
                "apply": True,
            }
        )
        new_dir = self.root / PROJECT / STAGE / INBOX_FOLDER / "2026.08.11-Alice Chen-site photos"
        self.assertEqual(Path(done["to"]), new_dir)
        self.assertTrue(new_dir.is_dir())
        self.assertFalse(old_dir.exists())
        self.assertTrue((new_dir / "photos.pdf").is_file())

        detail = cli.cmd_intake_detail({**self.payload(), "intake_id": opinion.record_id})
        self.assertEqual(detail["intake"]["source_org"], "Alice Chen")
        self.assertEqual(detail["intake"]["archive_dir"], str(new_dir))
        # The receipt log carries the same wrong name and changes with it
        with self.inbound.ledger.session() as connection:
            orgs = {
                str(row["source_org"])
                for row in connection.execute(
                    "SELECT source_org FROM receipts WHERE intake_id = ? AND action != 'withdrawn'",
                    (opinion.record_id,),
                )
            }
        self.assertEqual(orgs, {"Alice Chen"})

        # Same name: no folder shuffling and no error
        again = cli.cmd_intake_source(
            {**self.payload(), "intake_id": opinion.record_id, "source_org": "Alice Chen"}
        )
        self.assertFalse(again["changed"])

    def test_intake_with_an_issue_hanging_on_it_cannot_be_deleted(self) -> None:
        letter = self.make_pdf("client-comments.pdf", b"letter")
        source = self.intake("entrance comments", letter)
        self.issue(self.make_dwg("site-plan.dwg", "v1"), source.record_id)
        spare = self.intake("one nobody hangs on", self.make_pdf("idle.pdf", b"idle"))

        result = cli.cmd_delete_intakes(
            {**self.payload(), "intake_ids": [source.record_id, spare.record_id]}
        )

        # The one with an issue hanging on it is blocked, the other is deleted -- one failure doesn't drag down the rest
        self.assertEqual([f["intake_id"] for f in result["failed"]], [source.record_id])
        self.assertIn("would break the chain", result["failed"][0]["error"])
        self.assertEqual([d["intake_id"] for d in result["deleted"]], [spare.record_id])
        self.assertIsNotNone(self.inbound.ledger.get_intake(source.record_id))

    def test_withdrawn_evidence_stays_in_its_room(self) -> None:
        opinion = self.intake("sent by mistake", self.make_pdf("sent-by-mistake.pdf", b"z"))
        target = self.inbound.withdraw_intake(opinion.record_id)
        self.assertEqual(
            target.parent, self.root / PROJECT / STAGE / INBOX_FOLDER / WITHDRAWN_FOLDER
        )
        issued = self.issue(self.make_dwg("issued-by-mistake.dwg", "z"))
        moved = self.outbound.withdraw_issue(issued.files[0].issue_id)
        self.assertEqual(moved.parent, self.root / PROJECT / STAGE / OUTBOX_FOLDER / WITHDRAWN_FOLDER)


if __name__ == "__main__":
    unittest.main()
