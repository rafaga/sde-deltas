# sde-deltas

Consecutive, build-to-build deltas of the EVE Online Static Data Export (SDE): for every new build, what was
added, removed or changed compared with the previous build, down to field level.

> **Unofficial project.** No official affiliation or endorsement by CCP Games is stated or implied.
> The data in `deltas/` is derived from CCP's SDE and is the property of CCP hf. See [Data and licenses](#data-and-licenses).

## What is published

| Path | Content |
|---|---|
| `deltas/index.json` | The chain of builds: for each one, its previous build, release date, verification status and size. |
| `deltas/<build>/delta.jsonl.gz` | The delta from the previous build to `<build>` (format below). |
| `deltas/<build>/manifest.json` | Build metadata, per-table change counts, record counts of every table (`counts`), file hashes, verification result and the CCP notice. |
| `deltas/<build>/summary.csv` | Per table: IDs announced by CCP's changelog vs IDs actually found. |
| `deltas/<build>/verification.csv` | Mismatches between CCP's changelog and the data (empty when everything agrees). |
| `deltas/<build>/schema_delta.csv` | Field additions, removals and renames per table. |
| `CHANGELOG.md` | A readable list of what changed in each build (tables and counts only, no values). |

The first published delta is build **3031812** (base 3030547). The change of format that happened between builds
2960198 and 3030547 is not included.

## Delta format (`formatVersion` 1)

`delta.jsonl.gz` is gzip-compressed JSON Lines, UTF-8. Apply a build's lines **in file order**, on top of your copy
of the previous build. Records are the same JSON objects the SDE `jsonl` export uses, without the `_key` field.

```jsonc
// Schema operations: apply to every record of the table BEFORE the lines below.
{"table":"mapMoons","op":"schema","kind":"rename_table","from":"oldName"}
{"table":"mapMoons","op":"schema","kind":"rename_path","from":"planetAttributes.[].k","to":"attributes.[].k"}
{"table":"mapMoons","op":"schema","kind":"rename_path","from":"nameID.en","to":"name.en","reason":"string_resolution"}
{"table":"types","op":"schema","kind":"drop_path","path":"name.it"}
{"table":"types","op":"schema","kind":"add_path","path":"isRepackable"}   // informative: values come with the records
// Records
{"table":"types","id":587,"op":"added","record":{...}}                       // full record
{"table":"types","id":588,"op":"removed"}
{"table":"types","id":589,"op":"changed","fields":[{"path":"volume","old":10,"new":12},
                                                    {"path":"isRepackable","new":true}]}
```

- `id` keeps the type of the SDE `_key` (integer, or string/UUID for some tables).
- In `fields`, values are complete (never truncated). A missing `old` means the field is new in that record; a missing
  `new` means the field was removed from it.
- Paths in `fields` use list indices (`messages.[5].en`); in `schema` operations they use `[]` (`messages.[].en`).
  A `rename_path` keeps list positions.
- Whatever a schema operation already explains is not repeated per record, and a `changed` line that would end up
  with no fields is not emitted. Applying the schema operations and then the records to build N-1 gives build N exactly
  (this is tested).
- A table that appears or disappears entirely is a set of `added` or `removed` lines, without schema operations.
- Renames are detected only when the values are identical in every record that exists in both builds; a rename that
  also changes values is reported as `drop_path` + `add_path` plus the new values per record. Changes from a list to an
  object are also reported that way.

## Consuming it

1. Read `deltas/index.json` and walk the chain from your build to `latestBuild` (each entry has `lastBuild`).
2. For each build, download `deltas/<build>/delta.jsonl.gz` and `manifest.json`, check the SHA-256 in `files`, and apply
   the lines in order.
3. Decide what to do with tables you do not use: ignore them. For tables you do use, treat `add_path`, `drop_path`,
   `rename_path` and `rename_table` as a signal that your mapping may need attention.
4. Optionally compare your copy with `counts` in the last build's `manifest.json`: the number of records (distinct IDs)
   of every table in that build, changed or not. A different count means your copy drifted from the SDE. Builds
   published before `counts` was added don't have it.

`manifest.json` has `"verification": {"status": "ok" | "warn"}`. `warn` means the data disagrees with CCP's own
changelog (for example, CCP's changelog omitting a removed record) or the changelog chain is incomplete; the details are
in `verification.csv`. The delta is still published: the data is the source of truth.

## How it is produced

`scripts/process_latest.py` (Python 3, standard library only; developed on 3.14, run by the workflow on 3.12) looks up the latest build, follows the chain of CCP's
per-build changelogs, downloads the two SDE zips of each pending pair, compares **every** table (so changes that CCP's changelog does not
list are caught too), checks the result against the changelog, and writes `deltas/<build>/`. It is idempotent and a GitHub Actions workflow runs it
on a schedule. Run it yourself with:

```bash
export SDE_USER_AGENT="my-tool (https://example.org/contact)"   # identify yourself to CCP's servers
python scripts/process_latest.py --dry-run          # list pending builds
python scripts/process_latest.py --max-builds 1     # process one
python -m unittest discover -s tests                # tests (the real-data ones are skipped without the zips)
```

## Data and licenses

The source code in this repository (`scripts/`, `tests/`, `.github/`) is under the [MIT license](LICENSE). That license
covers **only the code**, not the data in `deltas/`.

The data in `deltas/` is derived from the EVE Online Static Data Export and is the property of CCP hf.; it is provided
for non-commercial use under the [EVE Developer License Agreement](https://developers.eveonline.com/license-agreement).
This repository does not relicense it. See [NOTICE](NOTICE).

© 2014 CCP hf. All rights reserved. "EVE", "EVE Online", "CCP", and all related logos and images are trademarks or
registered trademarks of CCP hf.

If CCP asks for any of this data to be removed, it will be removed from this repository within 7 days of the notice.
