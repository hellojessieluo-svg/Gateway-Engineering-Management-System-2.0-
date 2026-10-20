from __future__ import annotations

from datetime import date
from pathlib import Path

from common.errors import ValidationError
from common.naming import (
    INVALID_CHARS,
    MAX_FILENAME_LENGTH,
    format_date,
    validate_free_text,
)

# validate_free_text lives in the shared toolbox, one copy for both gates:
# intake uses it for source and subject, issue for recipient -- free text that goes into folder names.
__all__ = ["validate_free_text", "intake_folder_name", "unique_directory", "archived_name"]


def intake_folder_name(received_date: date, source_org: str, subject: str) -> str:
    """Intake batch folder name: date-source-subject."""
    source_org = validate_free_text(source_org, "Source")
    subject = validate_free_text(subject, "Subject", allow_empty=True)
    pieces = [format_date(received_date), source_org]
    if subject:
        pieces.append(subject)
    name = "-".join(pieces)
    if len(name) > MAX_FILENAME_LENGTH:
        raise ValidationError(f"Intake folder name is longer than {MAX_FILENAME_LENGTH} characters: {name}")
    return name


def unique_directory(parent: Path, base_name: str) -> Path:
    """Another batch on the same day from the same source with the same subject gets a number appended instead of overwriting."""
    candidate = parent / base_name
    index = 2
    while candidate.exists():
        candidate = parent / f"{base_name}-{index}"
        index += 1
        if index > 99:
            raise ValidationError(f"Too many intake folders with the same name, change the subject: {base_name}")
    return candidate


def archived_name(item_path: Path, relative_name: str, taken: set[str]) -> str:
    """Incoming files keep their original names; a number is appended only on a clash within the same batch."""
    if relative_name:
        parts = [part for part in relative_name.split("/") if part]
        if not parts:
            raise ValidationError(f"Invalid relative file path: {relative_name}")
        for part in parts:
            if part in (".", "..") or INVALID_CHARS.search(part):
                raise ValidationError(f"Invalid relative file path: {relative_name}")
        name = "/".join(parts)
    else:
        name = item_path.name

    if name.casefold() not in taken:
        return name

    head, _, leaf_name = name.rpartition("/")
    prefix = f"{head}/" if head else ""
    leaf = Path(leaf_name)
    index = 2
    while True:
        candidate = f"{prefix}{leaf.stem} ({index}){leaf.suffix}"
        if candidate.casefold() not in taken:
            return candidate
        index += 1
        if index > 99:
            raise ValidationError(f"Too many files with the same name in one batch: {name}")
