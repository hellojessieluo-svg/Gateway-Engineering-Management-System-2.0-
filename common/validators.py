from __future__ import annotations

import hashlib
import re
import time
import zipfile
from pathlib import Path

from common.errors import ValidationError
from common.constants import ALLOWED_EXTENSIONS


OFFICE_TEMP_PREFIX = "~$"
FORBIDDEN_SUFFIXES = {".bak", ".sv$"}
DWG_SIGNATURE = re.compile(rb"AC10\d{2}")
OLE_SIGNATURE = bytes.fromhex("D0CF11E0A1B11AE1")


def sha256_file(path: Path, chunk_size: int = 1024 * 1024) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(chunk_size):
            digest.update(chunk)
    return digest.hexdigest().upper()

ZIP_RETRY_TIMES = 3
ZIP_RETRY_SECONDS = 1.0
RECENT_WRITE_SECONDS = 60


def _require_zip_members(path: Path, members: set[str], label: str) -> None:
    # While Word or a script is still writing a large file, the zip directory has not hit the
    # disk yet and a half-read raises BadZipFile. The file is not broken, just unfinished:
    # wait and retry (2026-09-03, hit by a 100 MB merged document).
    last_exc: Exception | None = None
    for attempt in range(ZIP_RETRY_TIMES):
        try:
            with zipfile.ZipFile(path) as archive:
                names = set(archive.namelist())
                missing = members - names
                if missing:
                    raise ValidationError(
                        f"{path.name} is not a valid {label} file, missing: {', '.join(sorted(missing))}"
                    )
                bad_member = archive.testzip()
                if bad_member:
                    raise ValidationError(f"{path.name} has a corrupt internal file: {bad_member}")
            return
        except zipfile.BadZipFile as exc:
            last_exc = exc
            if attempt + 1 < ZIP_RETRY_TIMES:
                time.sleep(ZIP_RETRY_SECONDS)
    age = time.time() - path.stat().st_mtime
    if age < RECENT_WRITE_SECONDS:
        raise ValidationError(
            f"{path.name} was still being written {int(age)} seconds ago and may not be fully saved: wait until it is saved, then issue."
        ) from last_exc
    raise ValidationError(f"{path.name} is not a readable {label} file.") from last_exc


def _validate_pdf(path: Path) -> list[str]:
    with path.open("rb") as stream:
        header = stream.read(5)
        if header != b"%PDF-":
            raise ValidationError(f"The content of {path.name} does not match the .pdf extension.")
    try:
        from pypdf import PdfReader

        reader = PdfReader(str(path))
        if len(reader.pages) < 1:
            raise ValidationError(f"{path.name} has no readable pages.")
        return [f"PDF structure readable, {len(reader.pages)} pages"]
    except ImportError:
        with path.open("rb") as stream:
            stream.seek(max(0, path.stat().st_size - 2048))
            if b"%%EOF" not in stream.read():
                raise ValidationError(f"{path.name} has no PDF end marker.")
        return ["PDF header and end marker valid"]
    except ValidationError:
        raise
    except Exception as exc:
        raise ValidationError(f"The PDF structure of {path.name} cannot be read: {exc}") from exc


def structural_checks(path: Path) -> list[str]:
    """Run structural checks by extension and return the findings; raise ValidationError on a structural problem.

    When issuing, any failure fails the whole batch; when logging intake the same checks
    are only recorded as warnings, because a file already received cannot be refused just
    because it is damaged.
    """
    suffix = path.suffix.lower()
    checks: list[str] = []
    if suffix == ".xlsx":
        _require_zip_members(path, {"[Content_Types].xml", "xl/workbook.xml"}, "Excel")
        checks.append("Excel container structure valid")
    elif suffix == ".docx":
        _require_zip_members(path, {"[Content_Types].xml", "word/document.xml"}, "Word")
        checks.append("Word container structure valid")
    elif suffix == ".pptx":
        _require_zip_members(path, {"[Content_Types].xml", "ppt/presentation.xml"}, "PowerPoint")
        checks.append("PowerPoint container structure valid")
    elif suffix == ".xls":
        with path.open("rb") as stream:
            if stream.read(8) != OLE_SIGNATURE:
                raise ValidationError(f"The content of {path.name} does not match the legacy .xls extension.")
        checks.append("Legacy Excel file signature valid")
    elif suffix == ".ppt":
        with path.open("rb") as stream:
            if stream.read(8) != OLE_SIGNATURE:
                raise ValidationError(f"The content of {path.name} does not match the legacy .ppt extension.")
        checks.append("Legacy PowerPoint file signature valid")
    elif suffix == ".dwg":
        with path.open("rb") as stream:
            signature = stream.read(6)
        if not DWG_SIGNATURE.fullmatch(signature):
            raise ValidationError(f"The DWG version signature of {path.name} is not recognized.")
        checks.append(f"DWG version signature valid ({signature.decode('ascii')})")
    elif suffix == ".pdf":
        checks.extend(_validate_pdf(path))
    elif suffix == ".zip":
        try:
            with zipfile.ZipFile(path) as archive:
                bad_member = archive.testzip()
                if bad_member:
                    raise ValidationError(f"{path.name} has a corrupt internal file: {bad_member}")
        except zipfile.BadZipFile as exc:
            raise ValidationError(f"{path.name} is not a valid ZIP file.") from exc
        checks.append("ZIP integrity test passed")

    return checks


def validate_source_file(path: Path) -> tuple[int, str, list[str]]:
    if not path.exists() or not path.is_file():
        raise ValidationError(f"Source file does not exist or is not a file: {path}")
    if path.name.startswith(OFFICE_TEMP_PREFIX):
        raise ValidationError(f"Cannot issue an Office temporary file: {path.name}")
    suffix = path.suffix.lower()
    if suffix in FORBIDDEN_SUFFIXES:
        raise ValidationError(f"Cannot issue a CAD backup or autosave file: {path.name}")
    if suffix not in ALLOWED_EXTENSIONS:
        raise ValidationError(f"Unsupported file format: {path.name}")
    size = path.stat().st_size
    if size <= 0:
        raise ValidationError(f"File size is 0: {path.name}")

    checks = ["File exists, is readable and not empty"]
    checks.extend(structural_checks(path))
    return size, sha256_file(path), checks


def test_zip(path: Path) -> None:
    try:
        with zipfile.ZipFile(path) as archive:
            if not archive.namelist():
                raise ValidationError("The generated package is empty.")
            bad_member = archive.testzip()
            if bad_member:
                raise ValidationError(f"The generated package has a corrupt file: {bad_member}")
    except zipfile.BadZipFile as exc:
        raise ValidationError("The generated package cannot be read.") from exc
