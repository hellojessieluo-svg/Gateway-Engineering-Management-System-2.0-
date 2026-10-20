"""Contacts ledger: three fields, grouped by company, candidates emerge from the ledger, feeds both gates' dropdowns."""
from __future__ import annotations

import tempfile
import unittest
from datetime import date
from pathlib import Path

from gate_api import cli
from inbound_gate import InboundGate, IntakeItem, IntakeRequest
from outbound_gate import OutboundGate


class ContactsTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name) / "library"
        self.ledger_path = self.root / "ledger.db"
        OutboundGate(self.root, self.ledger_path)
        self.inbound = InboundGate(self.root, self.ledger_path)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def payload(self, **extra) -> dict:
        return {"ledger_path": str(self.ledger_path), "library_root": str(self.root), **extra}

    def intake(self, source_org: str) -> None:
        letter = Path(self.temp.name) / f"{len(source_org)}.pdf"
        letter.write_bytes(b"%PDF-1.7\n" + source_org.encode("utf-8") + b"\n%%EOF")
        self.inbound.archive_intake(
            IntakeRequest(
                project_id="PRJ-2601", project_name="Riverside Wetland Restoration", source_org=source_org,
                source_channel="WeChat", received_date=date(2026, 9, 3), subject="comments",
                items=[IntakeItem(letter)], stage="feasibility",
            )
        )

    def test_candidates_come_from_the_ledger_and_shrink_when_registered(self) -> None:
        self.intake("Alice Chen, Hill & Partners Engineering")
        data = cli.cmd_contacts(self.payload())
        names = {c["name"] for c in data["candidates"]}
        self.assertEqual(names, {"Alice Chen", "Hill & Partners Engineering"})
        self.assertEqual(data["contacts"], [])

        saved = cli.cmd_contact_save(
            self.payload(name="Alice Chen", org="Hill & Partners Engineering", title="Chief engineer", phone="555-0139")
        )
        data = cli.cmd_contacts(self.payload())
        self.assertEqual([c["name"] for c in data["contacts"]], ["Alice Chen"])
        # Both the person and the company are registered now; neither should remain a candidate
        self.assertEqual(data["candidates"], [])

        # Same name at the same company may not be entered twice
        with self.assertRaises(ValueError):
            cli.cmd_contact_save(self.payload(name="Alice Chen", org="Hill & Partners Engineering"))
        # Change the phone
        cli.cmd_contact_save(
            self.payload(contact_id=saved["contact_id"], name="Alice Chen", org="Hill & Partners Engineering", title="Chief engineer", phone="555-0138")
        )
        row = cli.cmd_contacts(self.payload())["contacts"][0]
        self.assertEqual((row["phone"], row["title"]), ("555-0138", "Chief engineer"))

    def test_contacts_feed_both_gates_suggestions(self) -> None:
        cli.cmd_contact_save(self.payload(name="Carol Wu", org="Acme Water Authority"))
        orgs = cli.cmd_intake_suggestions(self.payload())["source_orgs"]
        self.assertEqual(orgs[:2], ["Acme Water Authority", "Carol Wu"])
        recipients = cli.cmd_issue_suggestions(self.payload())["recipients"]
        self.assertEqual(recipients[0], "Carol Wu")

    def test_delete(self) -> None:
        saved = cli.cmd_contact_save(self.payload(name="David Li"))
        self.assertEqual(cli.cmd_contact_delete(self.payload(contact_id=saved["contact_id"]))["deleted"], 1)
        self.assertEqual(cli.cmd_contacts(self.payload())["contacts"], [])
