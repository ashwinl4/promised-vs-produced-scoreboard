# The Promised vs. Produced Scoreboard

A US factory gets announced with a capital figure, a job count, and a target date
for first output. Years later the outcomes vary widely. Some plants are producing
at volume, while others have slipped by several years or been cancelled. There is
little public data tracking which is which.

This directory builds that data. It collects the announcement and the current
status for each project, extracts them into a fixed set of columns, and publishes
the rows a person has checked. The result is the **Scoreboard**, stored in one
SQLite file, `outputs/scoreboard.db`, which grows as projects are added.

What you clone is a **multi-command CLI**. One entry point, `scoreboard.py`, with
a subcommand for each step: read the data, add to it, check a row, publish it.
`python3 scoreboard.py --help` lists them all.

A row is published only after someone opens its two source links and confirms the
figures match. The automated check catches malformed and out-of-range values, but
it cannot tell whether a source says what the row claims. That is why a person
signs off on every published row.

**Contents**

- [What counts as a project](#what-counts-as-a-project)
- [See the Scoreboard](#see-the-scoreboard)
- [How a project gets in](#how-a-project-gets-in)
- [Add data](#add-data)
- [Publish a row](#publish-a-row)
- [What is in here](#what-is-in-here)
- [Notes](#notes)

---

## What counts as a project

A project is in scope when **all** of these hold:

| | Rule |
|---|---|
| **Where** | a single physical facility in the United States |
| **When** | announced January 2017 or later |
| **Size** | announced capital **≥ $100,000,000** **OR** **≥ 200** direct promised jobs (either one qualifies) |
| **Sector** | one of these ten:<br>1. Aerospace and Defense<br>2. Auto Assembly<br>3. Battery<br>4. Chemicals and Plastics<br>5. Food and Beverage<br>6. Machinery<br>7. Pharmaceuticals<br>8. Semiconductors<br>9. Solar<br>10. Steel<br><br>…or `Other`, for a manufacturing project that genuinely fits none of the ten. `Other` is a last resort, not a bucket: if it starts filling up, the list above is wrong. |

Direct jobs only. "Regional," "supported," "induced," and construction-phase job
claims do not count toward the 200.

These rules are enforced in code by
[`pipeline/schema.py`](pipeline/schema.py).
If this table and that file ever disagree, the file is correct.

---

## See the Scoreboard

Three ways in, depending on what you want to do.

### 1. Open the CSV — nothing installed

The published rows are exported to a flat file. Open it in Excel, a text editor,
or anything else:

```
outputs/csv_tables/scoreboard_verify.csv
```

`scoreboard_source.csv` and `scoreboard_screen.csv` beside it hold the two
earlier stages, and `scoreboard_screen_check.csv` and
`scoreboard_verify_edits.csv` hold the audit trail. Regenerate all five with
`python3 scoreboard.py export`.

### 2. Use the command line — nothing installed

Python 3.9 or newer, standard library only. From this directory:

```bash
python3 scoreboard.py status             # row counts per stage
python3 scoreboard.py verify-list        # the published scoreboard
python3 scoreboard.py screen-list        # rows waiting for review (-> = unverified)
python3 scoreboard.py verify-show --id 6 # one row + its edit history
python3 scoreboard.py --help             # every command
```

Those read only. Every command that writes is named as such below.

### 3. Run the web app — needs two packages

```bash
pip install -r pipeline/requirements.txt
python3 scoreboard.py webapp             # then open http://localhost:8100
```

The `webapp` command runs uvicorn in process, so it works whether or not the
`uvicorn` script landed on your `PATH`. `--port` moves it, `--reload` restarts on
source changes, and `--db` composes, so `python3 scoreboard.py --db /tmp/try.db
webapp` reviews a copy instead of the real data.

Reading the data is the *least* of what this is for. Its real job is the
**review workflow**: putting a Screen row beside its two sources so you can check
them, correct cells, and promote it to Verify with the reason recorded. If you
just want to look, options 1 and 2 are faster and need no install. See
[`webapp/`](webapp/).

The CSVs in option 1 carry every column of their table, in table order, sorted by
`id`, with NULLs as empty cells. Three hold the Scoreboard; the two named
`_check` and `_edits` hold the audit trail, which is exported because
`scoreboard.db` is committed and git cannot diff a binary. Without them a commit
could add fifty check runs, or a correction to a published figure, and show
nothing but "scoreboard.db changed". `tools/export_tables.py` takes `--db` to
read a different database and `--out-dir` to write somewhere else. `python3 scoreboard.py export`
is the same exporter as a command; `--out-dir` works there too.

---

## How a project gets in

```
  SOURCE          →   SCREEN            →   VERIFY
  the links           one typed row         the published row
  collected           + a schema check      tier V1 / V2
  (AI or human)       tier P (provisional)  (human only)
```

| Stage | Holds | Who does it |
|---|---|---|
| **Source** | the source links and a one-line summary. No figures are recorded yet. | AI or human |
| **Screen** | the 17-column row extracted from those links, plus a schema check returning `FAIL`, `PASS`, or `CLEAN` | AI or human, then the checker |
| **Verify** | the published row. Later corrections are logged with a reason. | human only |

A `FAIL` at Screen blocks promotion. A `PASS` means the row is shaped correctly
and its values are in range. It says nothing about whether the sources support the
figures. Nothing is promoted to Verify automatically.

<a id="medallion"></a>
<details>
<summary><b>Why three stages: this is a medallion architecture</b></summary>

The data-lakehouse pattern popularized by Databricks. Data moves through
stages, and each stage is kept rather than overwritten, so a published figure can
always be traced back to the links it came from.

The convention names those stages Bronze, Silver, and Gold. This project renames
them after what happens at each step. The mapping is exact:

| Here | Convention | Table(s) |
|---|---|---|
| **Source** | Bronze | `source_collected` |
| **Screen** | Silver | `screen_extracted`, `screen_check` |
| **Verify** | Gold | `verify_verified`, `verify_edits` |

Wherever this project says "medallion," it means this pattern and this mapping.
Databases written before the rename still carry the Bronze/Silver/Gold table
names. The pipeline can read those files but never writes to them.

</details>

---

## Add data

One command finds new projects and extracts them into rows:

```bash
python3 scoreboard.py collect --n 10
```

Each iteration starts a fresh headless Claude Code worker that searches the web
and writes through the CLI, so this path needs the `claude` CLI installed and
logged in once (run `claude`, then `/login`). It checks that before it starts.
Stopping with Ctrl-C is safe, and re-running picks up where you left off because
the database de-duplicates.

Check the plan before spending anything:

```bash
python3 scoreboard.py collect --n 5 --dry-run
```

Each stage starts one worker per iteration, so `--n 10` across both stages is 20
or more worker calls. Start small.

### Four ways in, and only two of them need Anthropic

Collection is the only part of the Scoreboard that touches a model at all. The
data model, the checker, the human gate, the review loop, the exports and the
coverage measure are standard-library Python with no provider anywhere.

| Path | What you run | Needs | Works with |
|---|---|---|---|
| **manual** | `source-add`, `screen-add` | nothing | you and a browser |
| **prompt** | `source-prompt`, `screen-prompt` | nothing, no key | **any assistant that can search the web** |
| **direct API** | `source-collect`, `screen-extract`, `tools/gather.py` | `ANTHROPIC_API_KEY` | Anthropic only |
| **loops** | `collect` | the `claude` CLI, logged in | Claude Code only |

The **prompt** row is the one worth knowing about. `source-prompt` prints text
and nothing else. Paste it into ChatGPT, Gemini, Perplexity, Claude, or read it
yourself and do the searching by hand; whatever comes back is one JSON object,
and `source-add --json` ingests it. Nothing in that loop is Anthropic-specific,
and the operating prompts say nothing about which model is reading them.

The two automated paths are the Anthropic-specific ones. They are faster, not
more capable: all four write the same rows through the same functions and face
the same schema check and the same human gate. Nothing reaches Verify without a
person, by any route.

Per-stage settings are in [`docs/collecting.md`](docs/collecting.md). The manual
and copy-the-prompt paths are in [`docs/cli.md`](docs/cli.md).

---

## Publish a row

Find a row that passed its check and read it:

```bash
python3 scoreboard.py screen-list --by-capital   # biggest first; -> = still unverified
python3 scoreboard.py screen-show --id 42        # the row and its sources
```

Work largest first. The Scoreboard is made complete from the top down, so
wherever you stop, the claim above that point holds.

Open its `promise_source` and `status_source` links. Confirm they support the
capital figure, the job count, and the dates. Then publish it:

```bash
python3 scoreboard.py verify-promote --screen-id 42 --tier V1 \
    --flag "Resolved: two independent sources agree on the announced date."
```

The tier records how deeply you checked: `V1` if you confirmed each figure
against one source, `V2` if you confirmed it against two independent ones.
Publishing the same project twice is refused.

Corrections after publication always require a reason, and are written to
`verify_edits`:

```bash
python3 scoreboard.py verify-edit --id 6 \
    --set current_status="AT VOLUME" --desc "Re-read the Q3 release."
```

[`docs/verify_methods.md`](docs/verify_methods.md)
covers what to look for while reviewing.

---

## What is in here

Each of the three pipeline directories has its own README.

| Path | What it is |
|---|---|
| [`scoreboard.py`](scoreboard.py) | **The entry point.** A thin launcher for the pipeline CLI. |
| [`pipeline/`](pipeline/) | **Part 3, the pipeline.** The five tables, the commands, the web interface, and the promotion gate. Parts 1 and 2 both write through it. Also holds `schema.py`, which defines the columns, the sector list, and the size floor. |
| [`collect/`](collect/) | **Part 2, ongoing collection.** The loops that find new projects and extract them, and the prompts they hand to each worker. |
| [`webapp/`](webapp/) | The browser interface, for reviewing rows against their sources and promoting them. |
| [`tools/`](tools/) | Scripts for an existing database. Two are also CLI commands: `export` and `coverage`. The other two, bulk CSV load and batch collection over the API, stay scripts because they are rare and sharp. |
| [`outputs/`](outputs/) | `scoreboard.db`, plus `csv_tables/` holding a flat CSV export of each stage. |

Longer reference, kept out of this file:

- [`docs/cli.md`](docs/cli.md) — every command, the manual and Claude Code paths,
  and a short offline test.
- [`docs/collecting.md`](docs/collecting.md) — the collection loops in full.
- [`docs/schema.md`](docs/schema.md) — the five tables, the date handling, and how
  `flag` changes meaning between stages.

---

## Notes

- Run every command from this directory, the one holding this README.
- **The CSV exports keep themselves in step.** Any command that changes
  `outputs/scoreboard.db` refreshes `outputs/csv_tables/` as it closes, so the
  two are never committed out of sync. The database is committed and git cannot
  diff a binary, so those CSVs are how a change becomes readable in a review.
  This happens for the committed database only: a `--db` copy never overwrites
  the real exports. `SCOREBOARD_NO_AUTOEXPORT=1` turns it off, and any command
  will then warn you that the CSVs have fallen behind.
- The database is `outputs/scoreboard.db`. Override it with `SCOREBOARD_DB=/path/to/other.db`
  or `--db` on any command. Most commands open it read-write, so copy the file
  first if you want to experiment.
- Keep the database on a local disk. SQLite locking is unreliable over network
  and virtual-machine file shares, which shows up as `database is locked`.
- Only the web interface needs installed dependencies
  (`pipeline/requirements.txt`). Nothing here requires an
  API key except the optional direct-API collection path.
