from __future__ import annotations

from pathlib import Path

from common.errors import ValidationError
from common.constants import ALLOWED_EXTENSIONS
from common.validators import sha256_file, structural_checks

from .models import BLOCKED_SUFFIXES, OFFICE_TEMP_PREFIX


def inspect_file(path: Path) -> tuple[int, str, list[str], list[str]]:
    """Inspect a file about to be filed; return size, hash, check results and warnings.

    Intake works the opposite way from issuing: the file has already been received, so it
    cannot be refused because the format is unknown or the content is faulty. Only "things
    that should never be taken in" raise -- temporary files, empty files; every other problem
    is recorded as a warning and stays visible after filing.
    """
    if not path.exists() or not path.is_file():
        raise ValidationError(f"File does not exist or is not a file: {path}")
    if path.name.startswith(OFFICE_TEMP_PREFIX):
        raise ValidationError(f"This is an Office temporary file and will not be filed: {path.name}")
    suffix = path.suffix.lower()
    if suffix in BLOCKED_SUFFIXES:
        raise ValidationError(f"This is an unfinished download; download it completely first: {path.name}")
    size = path.stat().st_size
    if size <= 0:
        raise ValidationError(f"File size is 0, it may not have finished transferring: {path.name}")

    checks = ["File exists, is readable and not empty"]
    warnings: list[str] = []
    if suffix in ALLOWED_EXTENSIONS:
        try:
            checks.extend(structural_checks(path))
        except ValidationError as exc:
            warnings.append(f"{path.name}: {exc}")
            checks.append("Structural check failed; filed as is")
    else:
        checks.append(f"Format without structural checks ({suffix or 'no extension'}); only the content hash is recorded")

    return size, sha256_file(path), checks, warnings


def human_size(size: int) -> str:
    value = float(size)
    for unit in ("B", "KB", "MB", "GB"):
        if value < 1024 or unit == "GB":
            return f"{value:.0f} {unit}" if unit == "B" else f"{value:.1f} {unit}"
        value /= 1024
    return f"{value:.1f} GB"
