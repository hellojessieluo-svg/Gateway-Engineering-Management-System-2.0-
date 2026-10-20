# {{short_name}} ({{project_id}}) - project guide

Full name: {{full_name}}. Library-level rules are in the library spec; this file only covers this project.

## Project card

| Item | Value |
|---|---|
| Project ID | `{{project_id}}` (without an official ID, use TMP-xxx first; once assigned, use "Assign official ID" in Projects overview) |
| Full name | {{full_name}} |
| Room folder | `{{short_name}}` |
| Stages | (proposal / feasibility / preliminary-design / construction-drawings / construction-services / other -- list only the ones that exist) |
| My role | (the whole report / one discipline / combining / drawings...) |
| Client and contacts | (organization - name - role; phone numbers are in Gateway's "Contacts") |
| Key dates | (review, submission, approval dates) |

## Layout

```text
{{short_name}}\
├── AGENTS.md
└── <stage>\
    ├── inbox\  outbox\     managed by the gates, don't put things in by hand
    ├── current\            (name the drawers: calculations\ drawings\ documents\, or this project's own organization)
    ├── tools\              (if any, say where the entry script and README are)
    └── archive\
```

## Ground rules

- (Things already decided that must not be reopened, one per line, with date and source: which file is the data's source of truth, water levels / sections / coefficients, pitfalls others have hit)
-

## Start here

1. Read this file and the stage guide (if there is one).
2. Open `<stage>\current\` and confirm which file is the single current version.
3. Log all intake and issues through Gateway.
