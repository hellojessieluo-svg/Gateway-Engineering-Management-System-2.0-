from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

from common.constants import (  # noqa: F401  lists shared by both gates live in common
    ALLOWED_EXTENSIONS,
    DEFAULT_ISSUE_ROLE,
    ISSUE_ROLES,
    SELFCHECK_EXTENSION,
    SELFCHECK_FILENAME,
    STAGES,
    STAGE_SEPARATOR,
)


# Kept for compatibility with historical ledger entries. New desktop issues use
# DEFAULT_PURPOSE automatically; users only choose the actual delivery channel.
DEFAULT_PURPOSE = "issue"
PURPOSES = (
    DEFAULT_PURPOSE, "exchange", "review", "submission", "MIS upload", "self-archive", "other"
)
# Channel: how the files were sent. Mirrors the intake gate's SOURCE_CHANNELS; single choice.
CHANNELS = ("WeChat", "Email", "USB drive", "MIS", "Paper", "In person", "Self-archive", "Other")

# Projects registered up front when a new ledger is opened for the first time. Empty in the
# public edition: a fresh ledger starts with no projects.
KNOWN_PROJECTS: tuple[tuple[str, str], ...] = ()
KNOWN_NAME_ONLY_PROJECTS: tuple[str, ...] = ()


@dataclass(slots=True)
class SourceItem:
    """One file to issue. Its name is its own name -- there is nothing else to fill in.

    Since 2026-09-22 it also has a `role`: "sent" (default, actually sent to the recipient)
    or "kept" (supporting material such as calculation workbooks, stored with the batch but
    not sent). Ledger only; it does not change where the file goes.
    """

    path: Path | str
    role: str = DEFAULT_ISSUE_ROLE

    def __post_init__(self) -> None:
        self.path = Path(self.path)
        self.role = (self.role or DEFAULT_ISSUE_ROLE).strip() or DEFAULT_ISSUE_ROLE


@dataclass(slots=True)
class IssueRequest:
    """One issue = one batch of files, but the ledger has one row per file.

    No version numbers since 2026-08-15: which version it is, is for the file name to say.
    """

    project_id: str
    project_name: str
    stage: str
    issue_date: date
    purpose: str
    channels: list[str]
    recipients: str
    items: list[SourceItem]
    # Change & self-check notes: required, one .md
    selfcheck_path: Path | str
    # Which intake this issue responds to. The cause-and-effect chain must not break; required.
    intake_id: str
    # Categories removed 2026-09-03: only the "important or not" switch is left (the UI calls
    # it "Archive"). Since 2026-09-08 it is purely a ledger mark and never moves files.
    archived: bool = False
    # 2026-09-11: the outbox folder name gained the subject, matching intake
    # (date-recipient-subject). Short field, goes into the folder name; notes is still the
    # full sentence and only goes into the ledger.
    subject: str = ""
    notes: str = ""
    # 2026-08-20: several intakes can be replied to. If given, this is used (intake_id takes
    # the first one); old callers passing only intake_id keep working.
    intake_ids: list[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        self.project_id = self.project_id.strip()
        self.project_name = self.project_name.strip()
        self.stage = self.stage.strip()
        self.purpose = self.purpose.strip()
        self.channels = [item.strip() for item in self.channels if item.strip()]
        self.recipients = self.recipients.strip()
        self.subject = self.subject.strip()
        self.notes = self.notes.strip()
        # Normalize: intake_ids is the single source of truth (blanks and duplicates dropped,
        # order kept); intake_id is always the first one
        merged = [self.intake_id.strip()] + [str(x).strip() for x in self.intake_ids]
        self.intake_ids = list(dict.fromkeys(x for x in merged if x))
        self.intake_id = self.intake_ids[0] if self.intake_ids else ""
        self.selfcheck_path = Path(self.selfcheck_path)


@dataclass(slots=True)
class CheckedFile:
    """One file checked and ready to go out. name is its own file name; the program does not change it."""

    source_path: Path
    name: str
    stem: str
    extension: str
    size: int
    source_hash: str
    checks: list[str] = field(default_factory=list)
    role: str = DEFAULT_ISSUE_ROLE


@dataclass(slots=True)
class IssuedFile:
    issue_id: str
    name: str
    path: Path
    source_hash: str
    reused: bool
    role: str = DEFAULT_ISSUE_ROLE


@dataclass(slots=True)
class IssueResult:
    batch_id: str
    output_dir: Path
    selfcheck_path: Path
    files: list[IssuedFile] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
