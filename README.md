# Gateway

Gateway is a single-user, local web app for running an engineering work library: one folder
("room") per project, stage folders inside it, and a fixed set of positions per stage.
It logs what comes in (**intake**) and what goes out (**issues**) in one SQLite ledger, keeps
the files as add-only evidence, and runs a health check against the library spec.
It is pure Python standard library plus a single HTML page, served on `127.0.0.1` only.

> What was cut and why: see the "What we cut" section in [AGENTS.md](./AGENTS.md).
> This file only describes how the program is used and what it looks like now.

## Quick start

**Requirements**

- Windows (the launcher is a `.cmd`, the file picker is native Windows).
- Python 3.10+ with no third-party packages. [Pillow](https://pypi.org/project/pillow/) is
  only needed if you regenerate the icons with [tools/build_icon.py](./tools/build_icon.py).

**Set up a library**

1. Create the library root folder, e.g. `C:\Users\you\WorkLibrary` (the default is
   `~/WorkLibrary`).
2. Copy [examples/library/_admin/](./examples/library/_admin/) into it. That gives you the
   library spec (`_admin\library-spec.md`) and the three templates (`_admin\templates\`).
   The ledger (`ledger.db`) is created on first use.
3. Optionally write `%LOCALAPPDATA%\Gateway\config.json` if your library is not at the default
   location (every key is optional; missing keys use the defaults):

```json
{
  "library_root": "C:\\Users\\you\\WorkLibrary",
  "ledger_path": "C:\\Users\\you\\WorkLibrary\\ledger.db",
  "spec_path": "C:\\Users\\you\\WorkLibrary\\_admin\\library-spec.md",
  "reference_root": "C:\\Users\\you\\WorkLibrary\\_reference"
}
```

**Start**

Double-click [launch_gate_ui.cmd](./launch_gate_ui.cmd). It starts the background server and
opens the page in your browser. Environment variables:

- `GATE_PYTHON`: the Python to use, if it is not found automatically.
- `GATE_PORT`: the port to listen on (default `8790`).

**Run the tests**

```powershell
python -m unittest discover
```

The library layout and its rules are written up in [docs/library-spec.md](./docs/library-spec.md).

---

One entry point for two jobs: **intake** (what other people send me) and **issues** (what I
send out). The work in between is not Gateway's business: you edit files directly in the
project room's `<stage>\current\`.

> v3 (2026-08-15) removed v2's version gate, task gate and temporary workspace: the
> commits/tasks ledger was never really used between being built and being removed, and
> "assemble a work package, commit, roll back" was pure friction for a one-person workflow.
> The reasons behind changes now go into the **change notes** of each issue (required).
> Version numbers went the same day: which version a file is, is something people say in the
> file name; the program only records "this file, this stage, issued once on this day".

## Shape: a local server plus a web page

[launch_gate_ui.cmd](./launch_gate_ui.cmd) starts a local server that listens only on
`127.0.0.1` and opens the page in the browser.

The server runs in the background with **no console window** (it uses `pythonw.exe`). So:

- **To stop it**: "Quit" in the top right of the page.
- **Launching again does not start a second one**: on startup it first asks whether the running
  instance is alive (`/alive`, needs the token) and, if so, just opens that instance's page.
  The address and token are kept in `%LOCALAPPDATA%\Gateway\server.json`, which is deleted
  when the server stops.
- **If nothing happens when you launch it**: look at `server-error.log` in the same folder.
  Exceptions from the background process have no window to show up in, so they land there.
- **Only one process may bind the port** (2026-09-08): on Windows the standard library's default
  SO_REUSEADDR let a second process bind 8790 as well, and with several servers stacked on one
  port only the earliest got any requests. That is now switched off: if 8790 is taken by
  another program the server does not start, the error goes to `server-error.log`, and
  `netstat -ano | findstr :8790` shows who holds the port.

### Configuration

The config file is `%LOCALAPPDATA%\Gateway\config.json`. It can be absent, in which case all
defaults apply (see [common/config.py](./common/config.py)). `server.json`, `token.txt` and
`server-error.log` live in the same folder. The keys are the four shown in Quick start.

To try something against a throwaway library: point `LOCALAPPDATA` at a temporary folder, put
the sandbox library and ledger in its `Gateway\config.json`, then run
`python -m gate_api.server <port> --no-open`; the token is in `token.txt` in the same folder.

The browser never touches the disk and does not need to: copying, archiving and writing the
ledger all happen in the server process; the page only displays and sends commands.
**Neither gate accepts drag and drop** (since 2026-08-25): files always come in through
"Add files...", "Add a whole folder..." or "Pick from current...", which open the **native**
picker; on the issue side it starts browsing in the `current` folder.

- Interface: [ui/index.html](./ui/index.html) (one file, no external dependencies)
- Server: [gate_api/server.py](./gate_api/server.py); its commands are exactly those of
  [gate_api/cli.py](./gate_api/cli.py)
- Business logic lives in `inbound_gate/` and `outbound_gate/`; reskinning the UI does not
  affect them

## Structure: two gates

| Gate | Input | Character of the rules | Output | Code | Main tests |
|---|---|---|---|---|---|
| **Intake gate** | Files other people sent | No renaming, no rejecting, no versioning | A batch folder under `<short name>\<stage>\inbox\` plus an `intakes` record | `inbound_gate/` | `test_inbound.py` |
| **Issue gate** | Files from `current` | No renaming, validated, never overwritten | A batch folder under `<short name>\<stage>\outbox\` plus `issues` records (one entry per batch in the ledger view) | `outbound_gate/` | `test_core.py` |

The two gates never import each other's business logic. They are chained through ledger fields:

```text
issue  --intakes.replies_to-->  next incoming item (which submission a review comment answers)
intake <--issue_intakes------   issue (which incoming items this version answers -- at least one,
                                multiple allowed, the chain must not break;
                                issues.intake_id still keeps the first one; many-to-many since 2026-08-20)
```

Shared by both: **one ledger** (`ledger.db` at the library root; each gate writes its own
tables), **one set of project IDs** (the `projects` table; the issue gate's project registry is
the single source), and **one interface**. Cross-gate tests are in `test_chain.py`.

### Addresses are computed: places.py

The library is a row of project rooms. Project ID -> short name (`projects.folder`; falls back
to the ID if not registered) -> positions. Since 2026-08-20 both inbox and outbox hang under
the stage -- **every document has a stage** (the stage is required when logging intake):

```text
<library>\<short name>\<stage>\inbox\<date-source-subject>\       evidence, add-only
<library>\<short name>\<stage>\current\                           the single current version; work happens here
<library>\<short name>\<stage>\outbox\<date-recipient-subject>\   evidence, add-only (a same-name second batch gets -2)
```

The algorithm lives in [common/places.py](./common/places.py); the UI gets addresses through
the `places` command and never asks a person to type one. Incoming items that belong to no
project go to `_general\<stage>\inbox\`. Withdrawn items go to each stage's `inbox\_withdrawn\`
and `outbox\_withdrawn\`.

Batch folder names are `YYYY.MM.DD-<source>-<subject>` (inbox) and
`YYYY.MM.DD-<recipient>-<subject>` (outbox).

**The two gates archive in the same shape**: the outbox level is `date-recipient-subject`,
lining up segment by segment with the inbox's `date-source-subject` (the subject was added on
2026-09-11; before that it was only `date-recipient`, and the same day `issue-subject` filled
in and renamed all 60 old batches). The subject is a short, optional field; the "notes" are
still one full sentence in the ledger and never go into a folder name.
A name clash (same day, same recipient, same subject) gets `-2`.

**Categories were removed on 2026-09-03** (previously eight for intake and seven for issues,
each with its own folder level): choosing one of seven every time took thought and still came
out inconsistent; the benefit did not cover the hassle. Each gate now has just one
**"Archive" checkbox** -- tick it for things that matter, meaning "keep this well".
**Since 2026-09-08 it is only a mark in the ledger and does not move files**: every batch sits
directly under its gate folder (ticking it between 09-03 and 09-08 added an extra `archive\`
level; that was flattened on 09-08). In the ledger it is one bit each, `intakes.archived` /
`issues.archived`; the old `category` column was dropped together with its values.

### Ledger/disk mismatch: the disk wins (2026-08-29)

Intake and issues are both add-only evidence, but people do add files to a batch folder by
hand, save a CAD file in place, or move a document from one batch to another.
**The truth is on disk** -- files are the real thing, the ledger is the books; when they do not
match you fix the books, you do not delete people's files. Two reconcile commands (dry run by
default, `apply=true` to actually do it):

| Command | What it does |
| --- | --- |
| `intake-resync` | Rescans intake batch folders and recomputes the file list, hashes and `content_signature`. Rejects nothing -- 0-byte files, `.bak`, temp files are all recorded; they are already there, and recording them is how you see them |
| `issue-resync` | Recomputes issue batches: rows that exist in the ledger but are gone from disk are repointed, in order, to the extra files found on disk; leftover dead rows are removed (through `Ledger.delete_issue`, which also unlinks deliveries and intake links). Old batches from before 08-15 ("package + issue manifest.json") are skipped |

There is also `selfcheck-rename`: it renames the assorted self-check notes in old batches
("placeholder.md", "New Markdown document.md", ...) to `change-notes.md` and updates the ledger.
If the file in the ledger was renamed by a person and the folder holds exactly one unlogged
`.md`, that one is taken -- the disk wins.

Old on-disk shapes: `migrate-stage-layout` (moving inbox/outbox under the stage) ran on
2026-08-20; `migrate-drop-category-layout` (flattening the category level) only ever did a dry
run on 09-03, and the category levels on disk were flattened by hand on 09-05 following the
spec. Both commands have since been removed from the command table.

### Ledger clean-up (2026-09-03)

Five redesigns in three weeks had left three generations of dead structure in the ledger: the
five tables of the version/task gates, six `package_*`/`section`/`after_commit_id`/`task_id`
columns on `issues` named after "packaging", and two category columns on `issue_files`.
On 2026-09-03 it was rebuilt clean in one go:

```text
issues       one row = one file, one stage, issued once on one day
  file_name / file_stem / file_path / file_hash   the file that went out
  output_dir / selfcheck_path                     batch folder; one change-notes file per batch
  batch_id / intake_id / archived / status        one batch_id per batch; all links in issue_intakes
  role                                            sent / kept (2026-09-22): kept = stored with the batch, not sent
issue_files  before 2026-08-15 one issue was a zip, one row per source file in it; after that one row per entry
deliveries   delivery log (recipient, channel and notes live here; a withdrawal is logged too)
```

**Kept files (2026-09-22)**: in a batch, some files really went to the recipient (the reply,
the combined PDF, the CAD zip) and some are only the basis for this version (the calculation
workbook zip), stored with the batch but not sent. Both kinds **sit together in the same batch
folder** -- no subfolder, no separate ledger -- and only `issues.role` says `sent` or `kept`;
old entries were all backfilled as `sent`. The issue page has a "Kept" checkbox per row, off by
default; the ledger list counts "sent N, kept M", and the detail page shows each file's role.
Old batches get the mark through `issue-role` (`batch` + `roles` {file name: role}, or
`issue_id` + `role`), which changes that one word and touches no file or folder name.
A person ticks which files are kept; the program never guesses.

An old ledger is rebuilt by `Ledger._rebuild_legacy` the first time it is opened, when it sees a
`package_path` column (it first saves `ledger-backup.db` through the backup API, runs in one
transaction, and rolls back if foreign keys do not line up); on the intake side
`IntakeLedger.initialize` drops `task_id` along the way. Once every old ledger has been opened
once, both pieces can go.

### Shared tools live in common/

A toolbox that both gates use, moved out of `outbound_gate\` into its own `common/` package on
2026-09-03:

| Tool | Where |
|---|---|
| Exception types (`ValidationError`, `ConflictError`, `StorageError`) | `common/errors.py` |
| File name validity (`validate_component`, `ensure_path_length`) | `common/naming.py` |
| SHA-256 and structural checks (`sha256_file`, `structural_checks`) | `common/validators.py` |
| Constants (stage list `STAGES`, extension allow-list, change-notes file name) | `common/constants.py` |
| Project room address algorithm (`folder_in_connection`, `inbox_dir`, `pool_dir`, `outbox_dir`) | `common/places.py` |
| Configuration (`AppConfig`, `config_path`) | `common/config.py` |

Reading and writing the project registry (`Ledger`) still belongs to the issue gate
(`outbound_gate/ledger.py`) -- project IDs are the issue gate's business.
`common` imports no gate; both gates import it. **If you change this toolbox, run both gates'
tests.**

### One more layer on top: the entry point

Dependencies point one way only -- `gate_api` knows both gates, and neither gate knows
`gate_api` exists:

```text
  ui/index.html --> gate_api        --> two gates --> ledger.db
                    server runs the service
                    cli defines the command table (one copy, shared by both)
```

## Interface: left navigation plus a content area

The left column is "where to go", the right area is "what to do". The top bar shows a
breadcrumb and summary counts from the ledger.

```text
+--------------------+------------------------------------------+
| Work               | Work / (2) Issue files    to do: 2 items |
|  Dashboard         +------------------------------------------+
|  (1) Log intake    |                                          |
|  (2) Issue files   |   content area                           |
| Projects           |                                          |
|  Projects overview |                                          |
|  Standards index   |                                          |
| Ledger             |                                          |
|  Ledger            |                                          |
|  Contacts          |                                          |
| Other              |                                          |
|  System info       |                                          |
|  Quit              |                                          |
+--------------------+------------------------------------------+
```

- **Dashboard**: the count and list of incoming items still to do. An item is closed by the
  issue that replies to it; there is no "Close" button. Clicking a row does not open the folder
  either (removed 2026-09-03: too easy to trigger by accident; open folders from the ledger).
  **Each row has "No action needed"** (2026-09-18): it means "I don't need to reply to this";
  one click changes `intakes.todo` in the ledger (`set-todo`) and the row leaves the list.
  **This is a ledger fact, not a local view setting** -- the old "collapse" checkbox stored in
  localStorage was removed the same day; one table should not carry two mechanisms. The way
  back is in the table header: "N items need no action - show" lists them greyed out below
  (criterion: `todo=0` and never replied to; an item with an issue attached is closed, not on
  this list), each with "Needs action" to switch it back.
  Ledger and library paths and the operation log moved to **System info** on 2026-09-21.
- **Projects overview**: everything about the project registry is on this page -- one row per
  room (short name, ID, project name, incoming items waiting on me). Click the short name to
  open the room; click "AGENTS.md" to open the room's project guide (greyed out if missing).
  Since 2026-10-20 guides are always called `AGENTS.md`; the `CLAUDE.md` next to it holds only
  the one-line pointer `@AGENTS.md`, and the health check does not count it as a loose file.
  **New project** and **assign official ID** are done here too: changing the project registry
  belongs on the registry page, the two gate forms only log intake and issues.
  A project without an official ID gets a stable temporary ID like `TMP-001`; assigning the
  official ID later updates `projects`/`intakes`/`issues` together.
- **(1) Log intake**: choose the project (or "No project - goes to _general"), stage, source and
  subject, then "Add files..." or "Add a whole folder...", and file it. All five are required
  ("Archive" is an optional checkbox); the room it will land in is shown right next to the source.
- **(2) Issue files**: choose project and stage, and **the `current` folder's address and file
  count show up immediately** (click to open; the picker starts there). **Replies to**
  (required: tick at least one, several allowed; without one you cannot issue; only incoming
  items of **the same project and stage** still waiting for a reply are listed -- feasibility
  comments should not appear on a preliminary-design issue), **recipient**, and
  **change notes (one .md)** are required. Files go out one by one, named after themselves,
  with no version number.
- **Every incoming item on the dashboard has "Copy"** (2026-09-03): the folder, project/stage,
  source/subject, file count, which issue it replies to, the `current` folder and where the
  issue will go are assembled into plain text on the clipboard (`intake-brief` command) --
  paste it to an AI and it has the full brief (the task card).
- **Change-notes drafts live in `_drafts\` at the library root** (2026-09-26; before that on the
  desktop): one per project ID and stage, `_drafts\<id>-<stage>-change-notes.md`
  (`gate_api.cli._draft_path`, folder name `places.DRAFT_FOLDER`). The task card points to it,
  "Write notes" on the issue page saves to it, and the issue picks it -- all the same file.
  **After the issue is filed, the program deletes the draft it used** (`_consume_draft`): only
  in `_drafts\`, and only after comparing it byte for byte with the copy that landed in the
  batch -- when a whole batch is re-sent unchanged the program does not copy the notes, so a
  mismatching draft is kept; an `.md` picked from elsewhere is the person's file and is never
  deleted. The health check treats `_drafts\` as library admin, not a room. Do not confuse it
  with `.tmp\` at the root (the gates' copy area, which should always be empty).
- **Contacts** (2026-09-03): name, company, title (optional) and phone, grouped by company
  (`gate_api/contacts.py`, table `contacts` in the same ledger). You do not type them from
  scratch: names that appeared as an intake source or an issue recipient but are not registered
  yet are offered as candidates, one click fills the form. The intake "source" and the issue
  "recipient" dropdowns list contacts first, then names used before in the ledger.
- **Spec & health check** (2026-09-05; reachable at `#spec`, not in the sidebar since
  2026-09-21): the "Charter" tab renders the library spec as is -- there is only one copy
  (`config.spec_path`, default `<library>\_admin\library-spec.md`; if it is missing, the library
  root's `README.md` is shown). "Open file" takes you to the source to edit it -- this page is
  read-only and the file is the single source of truth (`spec-read`, with a small built-in
  markdown renderer, no external libraries). Next to it are every room's `AGENTS.md` and the
  three templates in `_admin\templates\`.
  "Health check" runs `library-check` ([gate_api/library_check.py](./gate_api/library_check.py))
  following section 9 of the spec: loose files in rooms, non-standard positions, folders not in
  the ledger, ledger/disk mismatches, missing guides, missing templates, log junk, unsortable
  names, unregistered rooms. It only reports and never fixes anything; click a row to open the
  folder. "Export" (2026-09-11, `library-check-export`) reruns the check, writes it as one
  markdown file with a section per room to `_admin\health-checks\health-check-YYYYmmdd-HHMMSS.md`
  and opens it; with "Errors only" ticked it exports just the errors -- handing the whole file
  to the AI doing the clean-up works well.
  Creating a new project drops in a project guide from the template plus its `CLAUDE.md` pointer
  (`library_check.seed_guide`).
- **Templates can be edited in place** (2026-09-07): `_admin\templates\` holds exactly three --
  `project-AGENTS.md`, `stage-AGENTS.md` and `change-notes.md`. The third is the skeleton at the
  end of the task card that "Copy" produces, and the starting draft of "Write notes" on the issue
  page: it used to be hard-coded in `common/constants.py`; now **the library's template wins,
  and the built-in one is only the fallback when the template is missing**
  (`library_check.selfcheck_skeleton`). On the "Templates" tab, "Edit", change, save
  (`template-save`) writes back to the original file in `_admin\templates\`; only these three
  names are accepted, anything else is refused.
- **Standards index** (2026-09-08, split into categories 2026-09-21): scans **three parallel
  categories** under `_reference\` (`config.reference_root`, default `<library>\_reference`) and
  lists whatever is on disk. The category table is `STANDARD_CATEGORIES` in
  `common/config.py`; adding a fourth category = one more row there plus that folder on disk:

  | Category | Folder | How it is read |
  |---|---|---|
  | Technical standards | `standards\` | `layout: library` -- lists what is in `originals\` and `guides\`; code, title and year are parsed from file names like `XX 123-2014 Title`; the PDF, companion md and `excerpts\` are matched by code; `pending\` is listed, never moved (filing checks belong to the standards library itself) |
  | Client regulations | `client-regulations\` | `layout: flat` -- a flat folder, the next level of subfolders is included too; no standard code, the whole file stem is the title |
  | Company requirements | `company-requirements\` | same as above |

  Only four fields are filled in by hand -- status (current / superseded / repealed / to verify),
  superseded by, applies to, notes -- stored in the ledger's `standards` table
  (`gate_api/standards.py`, commands `standards` / `standard-save`), keyed by
  "category::file stem" (`KEY_SEP`; `::` cannot appear in a Windows file name, so it never mixes
  with the stem). The page filters by category, kind and status.
  Linking standards to deliverables (declaring which standards an issue satisfies) is not built yet.
- **Ledger**: intake and issues **on one page** (2026-09-21; split into two pages on 2026-09-08,
  earlier one page with two tables). The list only shows the columns you need to recognize an
  entry; **click the "subject" (intake) or the notes (issue) to open the entry's detail page in a
  new tab** -- file list, folder links, which issue this intake replies to, which batches were
  sent afterwards; for an issue also every incoming item it answers, the delivery log (one row
  per send and per withdrawal), and the change-notes text.
  Detail pages have their own address (`#intake/<id>`, `#issue/<id>`) you can share; the server
  side is the `intake-detail` / `issue-detail` commands. Clicking a row no longer opens the
  folder; open it from the detail page.
  Withdrawn entries are shown in red -- the ledger has to remember "received, then withdrawn".
  Each row has a checkbox on the left; "Delete selected" removes entries that were **logged
  with a typo**.

### Interface rules

1. **Say what is missing before the button is pressed.** Required fields carry a red star; while
   something is missing the main button is grey and next to it says
   "2 missing: source, files"; clicking that line jumps to the first missing field. Ordinary
   validation never pops up a dialog.
2. **Never ask people for addresses.** `current`, inbox and outbox are all computed from
   (project, stage); the interface shows them and never lets you type them.
3. **Withdrawing is not deleting.** Each gate has "Withdraw last", and the button says which
   entry it will withdraw; the files move into the room's `_withdrawn\` and can always be found.
4. **Deleting removes words, not files.** "Delete selected" in the ledger is a different thing:
   withdrawing means "received, then withdrawn" and leaves a red entry; deleting means "this row
   should never have existed" (a typo when logging) and leaves no trace in the ledger.
   The files still go to `_withdrawn\` first -- nothing is destroyed; only the ledger rows go.

### Desktop shortcut

Point a shortcut at `launch_gate_ui.cmd` and use `assets/app.ico` as its icon (a "GW" mark,
generated by [tools/build_icon.py](./tools/build_icon.py)).

On Windows systems whose ANSI code page is not Unicode-capable (e.g. en-US, code page 1252),
`WScript.Shell`'s `CreateShortcut`/`Save` converts paths to ANSI, so:

- it cannot create a `.lnk` with a non-ASCII name (`Unable to save shortcut "???.lnk"`);
- it cannot read one either (it returns an empty object, which looks broken but is not);
- the fix: create it under an ASCII name, then rename it with .NET `System.IO.File.Move`;
- the **body** of every `.cmd`/`.vbs` is ASCII only -- non-ASCII text may appear only in file names.

---

# Issue gate

## Basic use

1. Choose project and stage -- the `current` folder's address and file count show up immediately;
2. Choose **which incoming items this replies to**: an issue needs a reason; if the project has
   no incoming item yet, log one in (1) first;
3. Fill in the **recipient** (tick "Archive" for important ones), pick one **change notes (.md)**
   file, and **write the notes** (the one sentence in the ledger);
4. "Pick from current..." to choose the drawings, sheets and documents to send -- the program
   does not pack anything, but a zip you packed yourself is accepted;
5. The table lists exactly the file names that will go out; click "Issue";
6. Open the batch folder from the result path and drag the files into your messenger or upload
   them to the client's portal.

**The program does not change a single character of a file name** (since 2026-08-15): a file goes
out under the name it has; the old automatic `id-shortname-stage-originalname-vversion` naming was
removed. One batch of files lands in one batch folder
(`<room>\<stage>\outbox\<date-recipient-subject>\`) with one change-notes file for the whole
batch, normalized on arrival to `change-notes.md`. **Folder names belong to the program, file
names belong to people** -- the two do not conflict: the batch folder is an address the program
computes from the ledger, not anyone's file.

**Registered project stages** (2026-08-30): `projects.stages` records which stages a project
actually has, and both gates' stage dropdowns list only those -- some projects only ever have
"other", and showing all six is noise. **This is a filter, not a gate**: the dropdown ends with
"+ Other stage...", which expands to all six, and when the entry is filed the program registers
the new stage itself (`_remember_stage`). Projects with no registered stages still list all six.
The initial values were not entered by hand -- when the column was added, the program derived
them from the ledger (which stages this project had received or issued anything for).
Corrections are made on the Projects overview page, `project-stages` command.

**Channel, single choice**: WeChat, Email, USB drive, MIS, Paper, In person, Self-archive, Other.

A project without an official ID yet can be registered by name only; the system assigns a stable
temporary ID such as `TMP-001`. Once the official ID is assigned, bind it in the project registry
and existing issue records follow.

## Supported formats

- Excel: `.xlsx`, `.xls` - CAD: `.dwg` - Word: `.docx` - PowerPoint: `.pptx`, `.ppt` -
  PDF: `.pdf` - change notes: `.md`
- **Archives (`.zip`/`.rar`/`.7z`) are accepted**: the program no longer renames anything, so a
  package you packed yourself is just one file; it goes out under its own name and is one entry
  in the ledger. `.zip` still gets an integrity test, and unreadable ones are stopped.
  (The program never packs anything itself -- you pack it, it does not pack for you.)

The minimum checks are: file readable, extension and basic structure, Office container integrity,
PDF page count, DWG version signature, and SHA-256 identical before and after copying.

## No more version numbers (2026-08-15)

Versioning was removed entirely: the `version`/`version_label` columns on `issues` and their
unique constraint are gone (after the 2026-09-03 clean-up the whole table is in its new shape; see
"Ledger clean-up" above), and `versions.py` was deleted. **Which version something is, is
something you say in the file name**; the program stays out of it. The remaining rules are:

- Send the same file name as many times as you like; issue date and batch folder tell them apart;
- **One issue is one entry in the ledger**: a set of files is one event, and the file names are
  listed inside that entry (tied together by `batch_id`). Underneath, `issues` still has one row
  per file -- each keeps its own hash and withdrawal status, and a batch with some files withdrawn
  shows as "partly withdrawn";
- Content unchanged to the byte (same project + same file name + same stage + same SHA-256): no
  second entry is created; the existing one just records "sent again";
- Issued files are never taken back or overwritten; a batch folder with the same name gets `-2`.

---

# Intake gate

Issues are "what I send out", intake is "what other people give me", and the rules are the exact
opposite: **no renaming, no rejecting, no versioning**.

## Intake rules

- The sender's original file names are kept; batches are told apart by folder:
  `date received-source-subject`;
- "Add a whole folder" keeps the original subfolder structure;
- Another batch on the same day from the same source with the same subject gets `-2` on its
  folder, never overwriting;
- Any format is accepted: png, rar, txt, files without an extension;
- Known formats still get structural checks, but **a failed check is only recorded as a warning
  and the file is filed anyway** -- something already received cannot be refused;
- Only Office temp files, unfinished downloads and 0-byte files are refused;
- When a whole batch is identical to an earlier one, it is not stored twice; only a "received
  again" entry is recorded.

---

## Tests

```powershell
python -m unittest discover -v
```

Tests build their own temporary library and ledger and clean up afterwards; they never touch
your real library.

## Current limits

The program does not drive messengers or client portals, does not manage multi-level review,
stamping, transmittal forms or formal records management, and does not check engineering
correctness such as quantities, CAD geometry, coordinates or elevations. On the intake side it
does not watch the downloads folder, does not detect the source automatically, and does not send
due-date reminders.

## License

Licensed under the Apache License, Version 2.0. See [LICENSE](./LICENSE).
