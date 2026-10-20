from __future__ import annotations

import re
from datetime import date
from pathlib import Path

from common.errors import ValidationError


INVALID_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
DISCOURAGED_WORDS = ("final", "latest", "new", "copy")
# The intake gate uses this to stop overlong batch folder names; the issue gate no longer
# makes up names, so this is its only remaining use.
MAX_FILENAME_LENGTH = 180
WINDOWS_RESERVED_NAMES = {
    "CON",
    "PRN",
    "AUX",
    "NUL",
    *(f"COM{number}" for number in range(1, 10)),
    *(f"LPT{number}" for number in range(1, 10)),
}


def validate_component(value: str, label: str, *, allow_empty: bool = False) -> str:
    value = value.strip()
    if not value and not allow_empty:
        raise ValidationError(f"{label} cannot be empty.")
    if not value:
        return ""
    if INVALID_CHARS.search(value):
        raise ValidationError(f"{label} contains characters Windows does not allow in file names: {value}")
    if value.endswith((" ", ".")):
        raise ValidationError(f"{label} cannot end with a space or a period.")
    if value.split(".", 1)[0].upper() in WINDOWS_RESERVED_NAMES:
        raise ValidationError(f"{label} cannot be a Windows reserved name: {value}")
    lowered = value.casefold()
    for word in DISCOURAGED_WORDS:
        # Whole words only: "renewal" or "finalize" are fine, "plan_final" is not
        if re.search(rf"(?<![a-z]){re.escape(word.casefold())}(?![a-z])", lowered):
            raise ValidationError(f"{label} should not contain the unsortable word \"{word}\".")
    return value


def validate_free_text(value: str, label: str, *, allow_empty: bool = False) -> str:
    """Validate free text that goes into a folder name (source, subject, recipient).

    Unlike validate_component, words like "final" are not banned -- organization and
    person names are decided by the other side; the program only stops characters
    Windows rejects and overlong names. Both gates share this one (intake source/subject,
    issue recipient/subject).
    """
    value = value.strip()
    if not value and not allow_empty:
        raise ValidationError(f"{label} cannot be empty.")
    if not value:
        return ""
    if INVALID_CHARS.search(value):
        raise ValidationError(f"{label} contains characters Windows does not allow in file names: {value}")
    if value.endswith((" ", ".")):
        raise ValidationError(f"{label} cannot end with a space or a period.")
    if value.split(".", 1)[0].upper() in WINDOWS_RESERVED_NAMES:
        raise ValidationError(f"{label} cannot be a Windows reserved name: {value}")
    if len(value) > 60:
        raise ValidationError(f"{label} must be at most 60 characters, got {len(value)}: {value}")
    return value


def format_date(value: date) -> str:
    return value.strftime("%Y.%m.%d")


# 2026-08-15: issuing no longer renames files -- people decide what a file is called and the
# program does not change a single character. The old make_filename / make_selfcheck_name
# were removed together with the "<id>-<short name>-<stage>-...-v<version>" scheme;
# versions are no longer written into file names.


def make_batch_folder_name(issue_date: date, recipients: str, subject: str = "") -> str:
    r"""Batch folder name: date-recipient-subject.

    The subject was added on 2026-09-11 so it lines up exactly with the intake's
    "date-source-subject" -- from the folder name alone you could not tell what two
    batches sent to the same person on the same day were about.
    The subject is a short field (same validation as intake) and is not the same thing as
    the full-sentence notes in the ledger: notes stay in the ledger only. The subject may be
    empty (falls back to "date-recipient" when old records cannot supply one); the 60 old
    batches present at the change were filled in and renamed with `issue-subject`.
    """
    recipients = validate_free_text(recipients, "Recipient")
    subject = validate_free_text(subject, "Subject", allow_empty=True)
    pieces = [format_date(issue_date), recipients]
    if subject:
        pieces.append(subject)
    name = "-".join(pieces)
    if len(name) > MAX_FILENAME_LENGTH:
        raise ValidationError(f"Issue batch folder name is longer than {MAX_FILENAME_LENGTH} characters: {name}")
    return name


def unique_batch_dir(parent: Path, name: str) -> Path:
    """A second batch on the same day, same subject, same recipient gets -2. Never overwrites an existing batch."""
    candidate = parent / name
    index = 2
    while candidate.exists():
        candidate = parent / f"{name}-{index}"
        index += 1
    return candidate


def ensure_path_length(path: Path, limit: int = 240) -> None:
    if len(str(path)) > limit:
        raise ValidationError(f"Path is longer than {limit} characters; shorten some names or the output folder: {path}")
