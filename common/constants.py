"""Fixed lists that both gates agree on."""
from __future__ import annotations


# The issue gate only accepts these formats (archives came back on 2026-08-15: a zip you
# packed yourself is just one file). The intake gate takes anything and only uses this list
# to decide whether to run structural checks. .md is for the change notes.
ALLOWED_EXTENSIONS = {
    ".xlsx", ".xls", ".dwg", ".docx", ".pptx", ".ppt",
    ".pdf", ".md", ".zip", ".rar", ".7z",
}
SELFCHECK_EXTENSION = ".md"
# Inside a batch folder the notes always carry this name: they are the fixed cover document
# of every batch, not a deliverable, so "file names belong to people" does not apply (2026-08-27).
SELFCHECK_FILENAME = "change-notes.md"
# Each file in an issue batch has a role (2026-09-22): "sent" actually went to the recipient,
# "kept" is supporting material stored in the same batch folder but not sent (working
# calculations and the like). It is one word in the ledger: no extra folder, no extra table;
# a person ticks which files are kept, the program never guesses.
ISSUE_ROLES = ("sent", "kept")
DEFAULT_ISSUE_ROLE = "sent"
# Skeleton of the change notes (section order fixed 2026-09-05): the first four sections are
# results for whoever picks the work up, the last one is process. The task card
# (intake-brief) hands it to the AI doing the work, and the issue page drafts from it when
# empty -- this is the only definition, the two sides must not drift apart.
SELFCHECK_SKELETON = """# Change & self-check notes

## What changed
<!-- One line per change: which file, which spot, what it became, which comment it answers -->

## Known, not handled
<!-- Raised in the intake but not touched this time, one reason each; write "none" if empty -->

## How it was checked
<!-- One criterion per change: what you looked at, counted, compared against. "I looked" does not count -->

## Downstream sync
<!-- Which of drawings / quantities / documents changed, and whether the rest followed -->

## Appendix: Q&A log
<!-- Paste when archiving the conversation; one subsection per date if it spans several -->
"""

STAGES = (
    "proposal",
    "feasibility",
    "preliminary-design",
    "construction-drawings",
    "construction-services",
    "other",
)
# A project's registered stages are stored as one string joined with this (projects.stages).
STAGE_SEPARATOR = ","
