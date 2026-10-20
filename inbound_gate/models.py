from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from pathlib import Path


SOURCE_CHANNELS = ("WeChat", "Email", "USB drive", "MIS", "Paper scan", "In person", "Other")

# Position constants such as "_general" and "_withdrawn" all live in common.places.

STATUS_ARCHIVED = "filed"
STATUS_WITHDRAWN = "withdrawn"

# Intake does not restrict extensions: whatever the other side sends is stored; only
# temporary and empty files are stopped.
BLOCKED_SUFFIXES = {".tmp", ".crdownload", ".part"}
OFFICE_TEMP_PREFIX = "~$"


@dataclass(slots=True)
class IntakeItem:
    """One source file to be filed.

    relative_name is only used when "adding a whole folder", to keep the original
    hierarchy inside the batch folder. Incoming files always keep their original names.
    """

    path: Path | str
    relative_name: str = ""

    def __post_init__(self) -> None:
        self.path = Path(self.path)
        self.relative_name = self.relative_name.strip().replace("\\", "/").strip("/")


@dataclass(slots=True)
class IntakeRequest:
    """One intake registration for an incoming item."""

    project_id: str
    project_name: str
    source_org: str
    source_channel: str
    received_date: date
    subject: str
    items: list[IntakeItem]
    todo: bool = False
    # Categories removed 2026-09-03: only the "important or not" switch is left (the UI calls
    # it "Archive"). Since 2026-09-08 it is purely a ledger mark and never moves files.
    archived: bool = False
    notes: str = ""
    stage: str = ""
    # The person who handled it on the other side. An organization is an organization and a
    # person is a person -- the ledger should say whom to check back with. Optional.
    source_person: str = ""
    # Closes the cause-and-effect chain: which issue this intake responds to (review
    # comments most often). Leave empty if you cannot remember.
    replies_to: str = ""

    def __post_init__(self) -> None:
        self.project_id = self.project_id.strip()
        self.project_name = self.project_name.strip()
        self.source_org = self.source_org.strip()
        self.source_person = self.source_person.strip()
        self.source_channel = self.source_channel.strip()
        self.subject = self.subject.strip()
        self.notes = self.notes.strip()
        self.stage = self.stage.strip()
        self.replies_to = self.replies_to.strip()


@dataclass(slots=True)
class CheckedIntakeFile:
    source_path: Path
    original_name: str
    archived_name: str
    extension: str
    size: int
    source_hash: str
    checks: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


@dataclass(slots=True)
class IntakeResult:
    kind: str
    record_id: str
    archive_dir: Path
    file_count: int
    total_size: int
    duplicate: bool = False
    warnings: list[str] = field(default_factory=list)
