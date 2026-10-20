# Gateway - guide for AI agents changing this repo

**This repo is the tool, not the files.** The tool lives here; the files it manages live in a
work library -- a row of project rooms. The library spec (the floor plan) is
[docs/library-spec.md](./docs/library-spec.md). A real deployment keeps its spec inside the
library at `<library>\_admin\library-spec.md` (`common.config.DEFAULT_SPEC_PATH`);
[examples/library/_admin/library-spec.md](./examples/library/_admin/library-spec.md) is a copy.
**Read the spec before touching this repo**, otherwise you will be changing the program that
decides where things go without knowing where things go.
What was cut and why is in "What we cut" below -- don't let cut things grow back; this file and
the README only describe the present.

The rooms in one line: one room per project, stage folders inside the room, and the positions
all hang under the stage (redesigned 2026-08-20).

```text
<short name>\<stage>\inbox\ -> <short name>\<stage>\current\ -> <short name>\<stage>\outbox\
 (1) incoming, evidence       (2) single current version,    (3) issued, evidence
                                  work happens in place
```

**Both gates archive in the same shape**: intake goes to `inbox\<date-source-subject>\`, issues to
`outbox\<date-recipient-subject>\` (the "subject" was added on 2026-09-11; before that it was only
`date-recipient`, and from the folder name you could not tell apart two batches sent to the same
person on the same day).
**All old batches were renamed the same day**: 60 batches got a subject through `issue-subject`
and were renamed; the subject was inferred from the incoming item's subject, the topic in the old
folder name, then the notes, in that order, and recorded after a person had reviewed it.
The subject is a short, optional field and is not the same thing as the full-sentence "notes"
in the ledger: notes only go into the ledger.
Note that "recipient" and "subject" are both stored in the `deliveries` table; `issues` has no
such columns.

**Categories were removed on 2026-09-03 and replaced with one "Archive" checkbox** (remember this
before changing the repo): the eight intake categories and seven issue categories were pulled
out together with the folder level each one had -- choosing one of seven was too much hassle.
Now the only question is "does this matter?". **Since 2026-09-08 the checkbox is only a mark in
the ledger** (meaning "keep this well") and **does not change where files go** -- every batch sits
directly under its gate folder; ticking it between 09-03 and 09-08 added an extra
`inbox\archive\` / `outbox\archive\` level, which was flattened on 09-08, and the health check
reports that level as "old archive layer under gate".
In the ledger it is one bit each, `intakes.archived` / `issues.archived` (the old `category`
column was DROPped together with its values). Don't confuse it with `archive\`, the fourth
position under a stage (the drawer for process files).

Since 2026-08-15 there are only **two gates**: intake and issue. The version gate, the task gate
and the temporary workspace were removed -- work happens directly in `current`, and the story
behind each change goes into the issue's change notes.

Issue rules from the same redesign (remember these before changing the repo, or you will write
it the old way):
**one issue, one ledger entry** -- a set of files sent out is one event and shows up as one entry
(tied by `batch_id`, gathered by `gate_api.cli._group_batches`; underneath, `issues` still has one
row per file, because each file's hash and withdrawal status must be kept; a batch with some
files withdrawn shows as "partly withdrawn");
the program does not pack files, but zip/rar/7z archives you packed yourself are accepted (once
names belong to people, an archive is just one file);
**the program never renames files** -- what goes out carries the file's own name; the automatic
`id-shortname-stage-...-vversion` naming was removed (`make_filename`/`make_selfcheck_name` were
deleted along with it);
**there are no version numbers** -- the `version`/`version_label` columns and their unique
constraint were removed from `issues` (`versions.py` was deleted). **The ledger was rebuilt clean
on 2026-09-03**: `issues` now only has `file_name/file_stem/file_path/file_hash` to describe the
file, with no `package_*`, `section`, `task_id`, `after_commit_id` or `content_signature`; the
`pools/commits/tasks` tables and the like are gone.
Old ledgers are rebuilt by `Ledger._rebuild_legacy` the first time they are opened (a
`-backup.db` is saved first).
Which version a file is, is something people say in the file name; the program only records
"this file, this stage, issued once on this day"; the same file name can be issued again and
again, told apart by issue date and batch folder;
**folder names belong to the program, file names belong to people** -- the batch folder is an
address the program computes from the ledger, so the "no renaming" rule does not apply to it;
the files sent with one click are one batch (`batch_id`; withdrawing, deleting and the ledger view
all work per batch);
an issue must reply to at least one incoming item, several allowed (since 2026-08-20 the
many-to-many link is the `issue_intakes` table; `issues.intake_id` still holds the first one), and
must come with change notes (`issues.selfcheck_path`). **The skeleton of the notes has one source
of truth: the library's template `_admin\templates\change-notes.md`** (editable since 2026-09-07;
section order fixed 2026-09-05: what changed / known, not handled / how it was checked /
downstream sync / appendix: Q&A log); only when the template is missing or empty does it fall
back to the built-in skeleton in `common/constants.py` -- both go through
`library_check.selfcheck_skeleton`, never read `SELFCHECK_SKELETON` directly. The task card from
`intake-brief` hands it, together with the draft path, to the AI doing the work, and the issue
page drafts from it when empty. Gate matters are delivered by the task card; a project room's
`AGENTS.md` only covers the project itself and does not repeat issue rules.
**Drafts live in `_drafts\<id>-<stage>-change-notes.md` at the library root** (moved from the
desktop on 2026-09-26; `places.DRAFT_FOLDER`). Once an issue is filed and the copy in the batch
matches byte for byte, the program deletes the draft (`cli._consume_draft`); it only deletes
drafts in `_drafts\`, never an `.md` picked from elsewhere.
`_drafts\` and `.tmp\` (the gates' copy area, should always be empty) are different things; the
health check treats both as library admin.

**Kept files** (2026-09-22): files in a batch that were not sent to the recipient and are only
stored as the basis (a calculation workbook zip) are marked `kept` in `issues.role`; everything
else is `sent` (old entries were all backfilled as `sent`). **It is only one word in the ledger**:
no subfolder, no separate ledger -- both kinds sit together in the same batch folder; the issue
page has a checkbox per row, and the list counts "sent N, kept M".
Old batches get the mark through `issue-role`. Don't grow a folder level for kept files -- the
explicit requirement is "stored together".

**On a ledger/disk mismatch, the disk wins** (2026-08-29): inbox and outbox are add-only in name,
but in practice people add files by hand, save in place, and move things around. Don't delete
people's files to fit the ledger -- `intake-resync` / `issue-resync` rescan the folders and fix the
books, `selfcheck-rename` normalizes the change notes in old batches to `change-notes.md`, and
`issue-subject` fills in the subject for old issue batches and renames their folders to the new
shape (renaming folders is the program's job; a clash gets -2).
All four are dry runs by default; they only act with `apply=true`.

**Registered project stages** (2026-08-30): `projects.stages` stores which stages a project
actually has (joined with `,`), and both gates' stage dropdowns filter by it. It is a filter, not a
gate -- the dropdown keeps a "+ Other stage..." option, and choosing it registers the stage
automatically (`gate_api.cli._remember_stage`). The initial values of this column were derived
from the ledger, not entered by a person.

## What we cut

New cuts get a new row.

| What was cut | Date | Why |
|---|---|---|
| Version gate, task gate, temporary workspace (commits / tasks ledger) | 2026-08-15 | Never really used between being built and being removed; "assemble a work package, commit, roll back" is pure friction for a one-person workflow. Reasons now go into the change notes |
| Version numbers, automatic `id-shortname-stage-originalname-vversion` naming | 2026-08-15 | Once the program stopped writing `-v2` into file names, the version number was just a number in the ledger nobody looked at, and its unique constraint bit back. Which version it is, is something people say in the file name |
| Laying the four gates out as a pipeline | 2026-08-15 | Went with the shrink to two gates; (1) and (2) are now plain menu items |
| Drag and drop (intake 08-15, issues 08-25) | 2026-08 | The native picker is used instead; on the issue side, browsing from `current` is more precise |
| Manual "Close" button | 2026-08-20 | An item can only be closed by the issue that replies to it |
| Intake notes box | 2026-08-25 | Once the subject was required, nobody wrote notes |
| Eight intake / seven issue categories, each with its own folder level | 2026-09-03 | Choosing one of seven took thought every time and still came out inconsistent; the benefit did not cover the hassle. Only an "Archive" checkbox remains |
| The "Archive" checkbox adding an `archive\` folder level | 2026-09-08 | A checkbox is a mark, not a location; the only such level was flattened |
| The program packing zips for you | 2026-08-15 | You pack the archive; the program does not pack it for you |
| A second instance for a stage-less personal library (with its stage-less mode, the `GATE_CONFIG` switch, per-instance colors and program name) | decided 2026-09-23, removed 09-26 | Zero usage: 0 intake and 0 issues between 09-07 and 09-23 |

- **The reasons for not doing something are worth more than the reasons for doing it, and cut
  things must not quietly grow back**: cut features based on usage data, and when something is
  out, pull it out by the roots (columns, tables, constraints and code all go; nothing is hidden).

## Repo structure

`README.md` is the full version; here are only three things that are easy to trip over:

1. **The two gates never import each other's business logic**; they are connected through the
   ledger `<library>\ledger.db` (`intakes.replies_to` points at `issues`). To see dependencies,
   look at the data flow -- don't draw architectural conclusions from imports.
2. **Shared tools live in `common\`** (`errors.py` `naming.py` `validators.py` `constants.py`
   `places.py` `config.py`, moved out of `outbound_gate\` on 2026-09-03). `common` imports no
   gate; both gates import it. **If you change this toolbox, run both gates' tests.**
   The address algorithm is in `common/places.py`: project ID -> `projects.folder` (short name)
   -> the room's positions.
   Reading and writing the project registry (`Ledger`) is still in `outbound_gate/ledger.py`.
3. **`gate_api` knows both gates; neither gate knows `gate_api`.** Dependencies point one way.
   The contacts ledger (`gate_api/contacts.py`, 2026-09-03) also lives in this layer: it feeds
   dropdown candidates to both gates, and neither gate knows about it. The library health check
   (`gate_api/library_check.py`, 2026-09-05) likewise: it is the executable version of section 9
   of the library spec (the text lives in the library's `_admin\`,
   `common.config.DEFAULT_SPEC_PATH`); if you change the spec, change it too.
   There is only one spec, and `read_spec` returns it (as `charter`); the UI's "Charter" tab shows
   it read-only -- edit the file itself.
   The three templates in `_admin\templates\` are its business too (`TEMPLATE_NAMES` /
   `write_template`, UI command `template-save`): any name outside the list is refused -- don't let
   the UI build paths.
   The standards index (`gate_api/standards.py`, 2026-09-08) is in this layer as well: rows are
   scanned from under `_reference\` (the disk wins), and only the four fields status /
   superseded by / applies to / notes are stored, in the ledger's `standards` table; it does not
   move files and does not do filing.
   **Since 2026-09-21 there are three parallel categories**: `standards\` (technical standards,
   internally split into originals / guides / pending / excerpts), `client-regulations\` and
   `company-requirements\` (the last two are flat folders, one file per entry, no codes). The
   category table is `STANDARD_CATEGORIES` in `common/config.py`, the config key is
   `reference_root`; the `standards` table's primary key is "category::file stem".

## Running the tests

```powershell
python -m unittest discover
```

Python standard library only -- don't add dependencies.

## Three pitfalls that bite

1. **The body of `.cmd` / `.vbs` files must be ASCII only.** On a machine with a non-Unicode ANSI
   code page (e.g. en-US, code page 1252) these files are read as ANSI, and UTF-8 non-ASCII text
   in them **fails silently** (no error, it just doesn't run). Non-ASCII may only appear in file
   names. Likewise `WScript.Shell` cannot create a `.lnk` with a non-ASCII name -- create it under
   an ASCII name, then rename it with .NET `File.Move`.
2. **Printing non-ASCII to stdout crashes when stdout is a pipe** (`UnicodeEncodeError: cp1252`).
   Running in a console is fine; as soon as output goes through `| tail` or is captured by a tool,
   it blows up. Set `PYTHONIOENCODING=utf-8` when debugging.
3. **The server runs in the background (`pythonw.exe`) with no console window.** If it won't
   start, look at `%LOCALAPPDATA%\Gateway\server-error.log` -- exceptions have nowhere else to go.
   Stop the server with "Quit" in the top right of the page. Launching again does not start a
   second one: it first asks `/alive` with the token, and if the server is alive it just opens
   the page.
   **The UI (`ui/index.html`) is read from disk on every request, so a reload picks up changes;
   Python changes need a server restart.**

## After a change

Verify UI changes in a browser (if the server is already running, just reload the page).
If you changed the spatial structure, update the library spec
([docs/library-spec.md](./docs/library-spec.md) and its copy in `examples/library/_admin/`) to match.
