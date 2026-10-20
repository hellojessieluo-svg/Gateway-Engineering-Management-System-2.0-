from __future__ import annotations

import json
import os
from dataclasses import dataclass, asdict
from pathlib import Path


# Root of the work library: one room per project (<short name>\<stage>\inbox, current, outbox),
# with the ledger at the root. The four top-level gate areas were removed on 2026-08-15.
DEFAULT_LIBRARY_ROOT = Path.home() / "WorkLibrary"
DEFAULT_LEDGER_PATH = DEFAULT_LIBRARY_ROOT / "ledger.db"
# The library spec lives in the library's own _admin\. The "Spec & health check" page renders
# this file; if it is missing, the library root's README.md is shown instead.
DEFAULT_SPEC_PATH = DEFAULT_LIBRARY_ROOT / "_admin" / "library-spec.md"
# Cross-project reference material (_reference\): sits next to the rooms, read-only, never gated.
DEFAULT_REFERENCE_ROOT = DEFAULT_LIBRARY_ROOT / "_reference"
# The three categories the "Standards index" page scans (2026-09-21: besides technical
# standards, client regulations and company requirements are tracked too; all three sit side
# by side under _reference\). folder is the on-disk name; layout says how to read it:
#   library = the standards library's internal structure (originals\ guides\ pending\ excerpts\);
#   flat    = a flat folder, one file per entry.
# To add a fourth category, add a row here and create the folder; the page picks it up.
STANDARD_CATEGORIES = (
    {"name": "Technical standards", "folder": "standards", "layout": "library"},
    {"name": "Client regulations", "folder": "client-regulations", "layout": "flat"},
    {"name": "Company requirements", "folder": "company-requirements", "layout": "flat"},
)


# The config file and the service's state files (server.json / token.txt / server-error.log)
# all live in this one folder.
def config_path() -> Path:
    base = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local"))
    return base / "Gateway" / "config.json"


@dataclass(slots=True)
class AppConfig:
    library_root: str = str(DEFAULT_LIBRARY_ROOT)
    ledger_path: str = str(DEFAULT_LEDGER_PATH)
    spec_path: str = str(DEFAULT_SPEC_PATH)
    reference_root: str = str(DEFAULT_REFERENCE_ROOT)

    @classmethod
    def load(cls) -> "AppConfig":
        path = config_path()
        try:
            # utf-8-sig: Windows PowerShell 5 and some editors save JSON with a BOM
            data = json.loads(path.read_text(encoding="utf-8-sig"))
        except (FileNotFoundError, OSError, ValueError, TypeError):
            return cls()
        return cls(
            library_root=str(data.get("library_root") or DEFAULT_LIBRARY_ROOT),
            ledger_path=str(data.get("ledger_path") or DEFAULT_LEDGER_PATH),
            spec_path=str(data.get("spec_path") or DEFAULT_SPEC_PATH),
            reference_root=str(data.get("reference_root") or DEFAULT_REFERENCE_ROOT),
        )

    def save(self) -> None:
        path = config_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(".tmp")
        temporary.write_text(
            json.dumps(asdict(self), ensure_ascii=False, indent=2), encoding="utf-8"
        )
        temporary.replace(path)
