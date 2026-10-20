# Work library spec

> This is the single source of truth: what the library looks like, where each thing goes, and
> what each room's guide contains are all decided here.
> Finalized 2026-09-05. **The text lives in the library itself at `_admin\library-spec.md`**
> (since 2026-09-23; edited by hand); the library root `README.md` is only a one-line pointer.
> Gateway's "Spec & health check" page (address `#spec`, not in the sidebar) shows this file as is
> and checks the whole library against the items in section 9.

## 1. In one sentence

**One room per project, stage folders inside the room, and only five positions in each stage.**
Evidence that comes in goes through the gate into `inbox`; work happens directly in `current`;
what goes out goes through the gate into `outbox`; process files all go into `archive`; this
project's glue scripts go into `tools`. There is no sixth position.

## 2. Floor plan

```text
<library>\
├── <short name>\                      one room per project (short name registered in ledger projects.folder)
│   ├── AGENTS.md                       project guide: project card, layout, ground rules, start here (section 5)
│   ├── CLAUDE.md                       one-line pointer: @AGENTS.md
│   └── <stage>\                        proposal / feasibility / preliminary-design / construction-drawings / construction-services / other
│       ├── AGENTS.md                   stage guide, only needed when the deliverables are complex (section 6)
│       ├── inbox\<date-source-subject>\       (1) evidence received, add-only, each folder is one ledger entry
│       ├── current\                    (2) the single current version, work happens right here
│       │   └── index.md                deliverables index: which deliverables exist now and which files make them up (optional, section 4.7)
│       ├── tools\                      this project's glue scripts + README (optional)
│       ├── outbox\<date-recipient-subject>\   (3) every batch sent out, add-only, each folder is one ledger entry
│       └── archive\                    (4) everything else: process files, dropped drafts, old material, temp folders
├── _general\<stage>\inbox\            incoming items that belong to no project
├── _reference\                        cross-project reference material, next to the rooms; the standards index's three categories live here
│   ├── standards\                     (1) technical standards: national/industry codes and guidelines
│   ├── client-regulations\            (2) rules, notices and management regulations issued by clients
│   ├── company-requirements\          (3) the company's own policies, template requirements, collected notices
│   └── (others)\                      company background, knowledge notes, approved-report library, cost norms... not indexed
├── _admin\                            the library's own records, migration logs, templates (templates\ lives here)
├── _drafts\                           change-notes drafts not yet issued; the program deletes them after the issue
├── README.md
└── ledger.db                          one ledger: projects, intake, issues, contacts
```

The room level **only allows** stage folders and `AGENTS.md` (hidden tool folders such as
`.claude\` excepted). The stage level **only allows** the five positions and `AGENTS.md`.
Anything else at these two levels is a violation, and the health check reports it.
Next to a guide there may be a `CLAUDE.md` whose whole body is the one line `@AGENTS.md` -- it
points tools that only read that name to the guide and carries no content of its own.

## 3. What goes in each of the five positions

| Position | What goes in | Who puts it there | Can it change? |
|---|---|---|---|
| `inbox\` | Originals other people gave me: comments, submissions, survey data, approvals, screenshots | Only the gate (Gateway "(1) Log intake") | No renaming, deleting or adding. If files were added by hand, run `intake-resync` so the ledger catches up |
| `current\` | The **single** current version of this stage: working files, drawings, reports, presentations | People and AI | Edit directly; back up binary files before editing (section 4.4) |
| `tools\` | Scripts, parameter tables and recipes written only for this project, with a README stating the data's source of truth and where things came from | People and AI | Change freely; general-purpose parts don't belong here, keep them in your general toolbox |
| `outbox\` | Every batch of originals sent out + that batch's `change-notes.md` | Only the gate ("(2) Issue files") | No renaming, no deleting. Withdrawals go through the gate into `_withdrawn\` |
| `archive\` | Process files, dropped drafts, old versions, temp folders, snapshots, old task folders | People and AI | Just drop things in, no structure required |

**Every folder under `inbox` and `outbox` is one ledger entry.** The "Archive" checkbox when
logging is only a mark in the ledger ("keep this well") and does not change the location --
there should be no `archive\` level under inbox or outbox (2026-09-08).
A folder on disk that the ledger does not know is reported as "batch not in ledger" or "folder not
in ledger"; the other way round, an entry in the ledger with nothing on disk is reported as
"ledger/disk mismatch".

## 4. How `current` is organized

1. **Keep one copy of each thing; no duplicate names anywhere in `current`.** No `-v2`, `-final`,
   `-revised`, `-latest`, `-copy`. Which version it is, is the issue batch's business, not the
   file name's.
2. **Three drawers by default**: `calculations\` (the data's source of truth: CAD/model working
   files, quantity workbooks), `drawings\` (exported dwg, combined PDF sets, drawing lists),
   `documents\` (report chapters, presentations, notes). No loose files at the top (except the
   deliverables index `index.md`). If there is only one deliverable, drawers are optional.
3. **Stages with complex deliverables define their own organization** (processing chains,
   volumes, drawing numbers) in the stage guide; decided exceptions are in each stage's `AGENTS.md`.
4. **Back up binary files before editing them** (dwg/docx/xlsx/pptx). Backups live in a
   file-history store outside the library, not as copies inside the room.
5. **No log junk in the room**: `plot.log`, `acad.err`, `*.tmp`, `~$*` are deleted after use.
   This means the ones scattered in **current**. `tools\` is the exception: a tool's own run logs
   and caches (`logs\`, run records, `cache\`) are part of the tool and are often looked back at
   before editing drawings, so they are kept and are not junk (2026-09-11).
6. Source material, half-finished pieces and replaced old versions go into the same stage's
   `archive\`, not left in `current`.
7. **Deliverables index `current\index.md`** (2026-10-20, optional): records which deliverables
   this stage has **right now** and which existing files make up each one.
   One three-column table, for example (a real index lists files that actually exist on disk):

   | Deliverable | Files | Notes |
   |---|---|---|
   | Preliminary design report | `documents/report.docx`, `calculations/cost-estimate.xlsx` | |
   | Construction drawing set | `drawings/` | references the whole folder |

   - Files are written as **paths relative to index.md**, separated with `/`; wrap each path in
     backticks and separate them with commas (file names may contain commas themselves); a whole
     folder ends with `/`.
   - Reference only: don't copy, move or change files; don't invent deliverable numbers -- let file
     and folder names say what it is.
   - Deliverables are added as work goes on; no full list up front. Only update a row when its
     makeup changes (files added or removed, deliverables split or merged); editing a file's
     contents needs no change.
   - Only current deliverables: no old versions, progress or intake/issue history -- that is what
     `archive\` and the ledger are for.
   - Don't decide something is a current deliverable from its folder name alone; when unsure, a
     person decides.
   - Don't create an empty index for a stage that hasn't started or doesn't need one; no index at
     the room level. The guide states the rules, the index states what exists now; neither
     replaces the other.

## 5. What the project guide AGENTS.md contains

One at the root of each room. When a new project is created, Gateway drops one in from
`_admin\templates\project-AGENTS.md` (together with the one-line `CLAUDE.md` pointer); a person
fills it in afterwards.
**Four sections are required; if any is missing the health check reports "incomplete guide"**:

1. **Project card** (a table): project ID in the ledger, full name, room folder (short name),
   which stages exist, my role, client and contact people, key dates.
2. **Layout**: a tree of what this room actually looks like, highlighting the drawers in
   `current` and where stage guides are.
3. **Ground rules**: things already decided for this project that must not be reopened -- which
   file is the data's source of truth, design values such as water levels, sections and
   coefficients, pitfalls others have hit. Each one should carry a date and a source (a note or a
   ledger batch) where possible.
4. **Start here**: what a newcomer (or a fresh AI session) should read first and which folder to
   open.

Optional: review follow-up progress, drawing assignment table, open items. **No list of
addresses** (Gateway computes addresses), and don't copy library-level rules (point to this file).

## 6. What a stage guide AGENTS.md contains

Only written when the deliverables need their own organizing rules (for example a multi-step
processing chain for a preliminary design, or volumes and drawing numbers for a set of
construction drawings). The template is `_admin\templates\stage-AGENTS.md`. Contents:

1. **Where this set of deliverables ends** (the boundary of responsibility, what is not in this
   folder);
2. **Organization**: how folders are split, whether each level may be edited by hand, whether it
   can be rebuilt if deleted;
3. **How changes propagate**: if an upstream piece changes, which downstream pieces must be
   reissued;
4. **Naming and numbering**: volumes, drawing number rules, file name format;
5. **Tool entry points**: which script to run, which parameter table it reads;
6. **Decided exceptions and known, unhandled issues**.

Rules only, no addresses.

## 7. Everyday actions

| Action | How | Where it lands |
|---|---|---|
| Something arrives | Gateway "(1) Log intake": project, stage, source and subject are required; tick "Archive" for important ones | `<stage>\inbox\<date-source-subject>\` |
| Work | Edit directly in `current\`; process files go into `archive\` as you go | `<stage>\current\` |
| Send something out | "(2) Issue files": reply to at least one incoming item, attach one change-notes file, write one line of notes | `<stage>\outbox\<date-recipient-subject>\` |
| Received / sent by mistake | "Withdraw last" in the interface; the files go to `_withdrawn\`, the ledger keeps a red entry | `inbox\_withdrawn\`, `outbox\_withdrawn\` |
| Typo when logging | Tick it in the ledger view and "Delete selected": gone from the ledger, files still go to `_withdrawn\` | same as above |
| Files added to inbox/outbox by hand | Run `intake-resync` / `issue-resync` (dry run by default) so the ledger catches up with the disk | the ledger changes, files don't |
| New project | "Projects overview -> + New project": registers it, creates the room, drops in AGENTS.md; no official ID yet gets `TMP-xxx` | `<short name>\` |
| Hand a job to an AI | On the dashboard, click "Copy" on the incoming item and paste the whole thing (the change-notes skeleton at the end comes from `_admin\templates\change-notes.md`) | -- |
| Change the task card / guide templates | "Spec & health check -> Templates", pick one, click "Edit", save | `_admin\templates\` |

## 8. Other things at the library root

- `_general\`: incoming items that belong to no project go here; shaped like a project room's stages.
- `_reference\`: cross-project reference. **It lives at the library root next to the rooms** --
  these things belong to no single project and should not go through a gate (they are not
  "evidence received", they are material you look things up in). Read-only, never gated.
  - **The standards index's three categories** (decided 2026-09-21): Gateway's "Standards index"
    page scans these three parallel folders under `_reference\` and lists whatever is on disk
    (`config.reference_root` points to `_reference\`; the category table is
    `STANDARD_CATEGORIES` in `common/config.py`; a fourth category = one more row there + that
    folder on disk).

    | Category | Folder | What it looks like inside |
    |---|---|---|
    | Technical standards | `standards\` | Split further into `originals\` `guides\` `pending\` `excerpts\`; the code is parsed from file names like `XX 123-2014 Title`; filing checks belong to the standards library's own `readme.md` |
    | Client regulations | `client-regulations\` | A flat folder (the next level of subfolders is included too), one file per entry; no standard code, the whole file name is the title |
    | Company requirements | `company-requirements\` | Same as above |

    The four fields status / superseded by / applies to / notes are filled in by hand and stored in
    the ledger's `standards` table, matched by "category::file stem".
  - Other folders under `_reference\` (company and colleague information, project background,
    knowledge notes, approved-report library, cost norms...) are not indexed -- the index only
    covers "rules you have to follow", not general reference material.
- `_admin\`: the library's own records (plans and logs of each redesign, ledger backups,
  **templates\**). `templates\` always holds three files, which can be **viewed and edited directly**
  on Gateway's "Spec & health check -> Templates" page (click "Save" to write back to the file):

  | Template | Who uses it |
  |---|---|
  | `project-AGENTS.md` | Dropped into a new room as its project guide when a project is created (section 5) |
  | `stage-AGENTS.md` | Followed by hand when a stage guide is needed (section 6) |
  | `change-notes.md` | Skeleton of the notes every issue must carry: it is what ends the task card copied from the dashboard, and the starting point of "Write notes" on the issue page when empty |

  Changing a template only affects files generated afterwards; files already written are left
  alone. The health check warns about any of the three that is missing; `change-notes.md` works
  even when missing -- the program has the same skeleton built in as a fallback.
  Health-check exports go to `_admin\health-checks\health-check-YYYYmmdd-HHMMSS.md`.
- `ledger.db`: the one ledger. Don't edit it by hand; change it through Gateway's commands.
  `-backup*.db` files are what the program saves before an operation on the ledger.
- `.tmp\`: the gates' temporary copy folder; it should always be empty.
- `_drafts\`: where change-notes drafts live (2026-09-26; before that on the desktop). One per
  "ID-stage", named `<id>-<stage>-change-notes.md`; the task card points to it and "Write notes"
  on the issue page saves to it. Once an issue is filed and the copy in the batch matches the
  draft byte for byte, the program deletes the draft; an `.md` picked by hand from elsewhere is
  never deleted. The health check does not look inside. Not the same thing as `.tmp\` above.
- `README.md`: a one-line pointer to this file.

## 9. Health-check items (run automatically on Gateway's "Spec & health check" page)

| Level | Check | Reported as |
|---|---|---|
| error | A folder other than a stage, or a loose file, at the room level (`AGENTS.md` / `CLAUDE.md` excepted) | non-stage folder in room / loose file in room |
| error | A folder other than the five positions, or a loose file, at the stage level (`AGENTS.md` / `CLAUDE.md` excepted) | non-standard position / loose file in stage |
| error | A loose file directly under inbox/outbox | loose file under gate |
| error | An `archive\` level under inbox/outbox (left over from 2026-09-03 to 09-08) | old archive layer under gate |
| error | A batch-shaped folder (`YYYY.MM.DD-...`) under inbox/outbox that the ledger does not know | batch not in ledger |
| error | Any other folder under inbox/outbox (old category level, hand-made folder) | folder not in ledger |
| error | An inbox/outbox folder in the ledger does not exist on disk | ledger/disk mismatch |
| warning | The room has no AGENTS.md, or it lacks any of "Project card / Layout / Ground rules / Start here" | missing guide / incomplete guide |
| warning | Log junk in the room (plot.log, acad.err, *.tmp, *.log, *.err, ~$*); the `archive\` and `tools\` trees are exempt | log junk |
| warning | A file name in `current` contains final / latest / copy / -v plus a number | unsortable name |
| warning | A room on disk not registered in the ledger, or registered but with no room on disk | room not registered / room missing |
| warning | Any of the three templates in `_admin\templates\` is missing (or empty) | missing template |

The health check only reports and never touches anything. Deal with findings following sections
3 and 4; when tidying in bulk, first copy the folders you are about to touch, as a whole, to a
snapshot outside the library, then move things.

## 10. Don'ts

1. Don't put things into `inbox\` or `outbox\` by hand, rename or delete files there.
2. Don't keep two versions in `current`.
3. Don't put loose files at the room level or the stage level.
4. Don't make `-backup` or `-copy` files in the room; backups are the program's job.
5. Don't write general-purpose parts into a project's `tools\`; they belong in your general
   toolbox outside the library.
6. Don't change the tables in `ledger.db`; change the ledger through `gate_api.cli` commands.
7. An AI never deletes people's files to fit the ledger; on a ledger/disk mismatch the disk wins,
   fix the ledger.

## 11. History

| Date | What changed |
|---|---|
| 2026-08-13 | Four shared gate areas (inbox store / workspace / archive / outbox store), located by `<id>\<stage>\` |
| 2026-08-15 | Back to project rooms located by `<short name>\`; version gate, task gate and temporary workspace removed; issues keep their file names, no version numbers |
| 2026-08-20 | inbox and outbox moved under the stage; the room level reduced to "stages + guide"; the separate project card file merged into the room guide |
| 2026-08-28 | The last structural exceptions in the library brought into the standard five positions |
| 2026-09-03 | The eight intake / seven issue categories removed, leaving one checkbox (ledger changed the same day; category levels on disk flattened by 09-05) |
| 2026-09-05 | This spec finalized: four required guide sections, health-check items built into Gateway's "Spec & health check" page, new projects get a guide automatically; old category levels on disk flattened |
| 2026-09-07 | The change-notes skeleton moved from the program into `_admin\templates\`; the three templates editable on the spec page; health check gains "missing template" |
| 2026-09-08 | The checkbox renamed "Archive" and made only a mark in the ledger, no longer adding a folder level; the only such level on disk flattened. Ledger entries get detail pages |
| 2026-09-11 | The standards library moved into `_reference\standards\`. Outbox batch folders gain the subject (`date-recipient-subject`), old batches renamed. Health check relaxed: run logs and caches under `tools\` are no longer reported as "log junk" (sections 4.5 and 9) |
| 2026-09-21 | Three parallel categories under `_reference\`: `standards\`, `client-regulations\`, `company-requirements\`; the "Standards index" page filters by category (section 8). Config key becomes `reference_root`; the `standards` table is keyed by "category::file stem". The "Spec & health check" page leaves the sidebar (`#spec` still works); intake and issue ledgers merged into one page |
| 2026-09-23 | The spec text moved into `_admin\library-spec.md`; the library root `README.md` keeps only a pointer |
| 2026-09-26 | Change-notes drafts moved from the desktop into `_drafts\` at the library root (sections 2 and 8); the program deletes the draft it used after the issue. The health check treats `_drafts\` as library admin, not a room |
| 2026-10-20 | Room and stage guides are always named `AGENTS.md`; the `CLAUDE.md` next to them is only the one-line pointer `@AGENTS.md`; the two guide templates renamed `project-AGENTS.md` and `stage-AGENTS.md`. New optional deliverables index `current\index.md` (sections 2 and 4.7) |
