# Command reference (Part 3)

*Package: [`../pipeline/`](../pipeline/)*

Every pipeline step is one command. The everyday ones are in the
[scoreboard README](../README.md); this is all of them, plus the manual and
Claude Code paths and an offline test. Run everything from `aici/scoreboard`.

## What you need
| You want to… | Install | Key? |
|---|---|---|
| Run the pipeline core + CLI (source/screen/verify moves, checks) | **nothing** — Python 3.9+ stdlib | no |
| Use the web interface (`webapp`) | `pip install -r pipeline/requirements.txt` (FastAPI + uvicorn) | no |
| **Claude Code path** for the AI steps (recommended, no key) | nothing extra | **no** |
| Direct-API AI steps (`source-collect` / `screen-extract` / `automate`) | `pip install anthropic` | yes (`ANTHROPIC_API_KEY`) |

Run every command **from the `scoreboard/` directory**, e.g.

```bash
cd aici/scoreboard
python -m pipeline.cli --help
```

`config.env` in this directory is optional: put `SCOREBOARD_DB` or an API key there
and `tools/gather.py` will read it (a real shell variable always wins). It is
gitignored. The database defaults to `outputs/scoreboard.db`.


## The modules behind the commands

Each stage is a small module the two interfaces share:

- `db.py` — the five-table SQLite schema + connection helpers.
- `source.py` — insert / list Source leads.
- `screen.py` — insert extracted rows; `run_check()` (Screen pt-2).
- `schema_check.py` — **is** `screen_check`: it loads the canonical
  `schema.py` and runs its row validator, returning the
  `FAIL / PASS / CLEAN` verdict + the issue list.
- `verify.py` — `promote()` (the human gate) and `edit()` (writes a `verify_edits`
  row in the **same transaction** as every Verify update).
- `orchestrate.py` — the moves between the stages (`automate_all`, the AI runners, the explore-`filter`).
- `dates.py` — the deterministic date standardization. Each date is kept as a
  **`*_raw` → token → `*_dt`** chain: the extractor supplies the verbatim source text
  (`*_raw`) and a clean normalized token; this module resolves the *token* to a `*_dt`
  DATETIME (a fuzzy range → its "healthy middle") and computes the float `lag_years` /
  `slip_years` (with `-1` "to be completed" / `-2` "cancelled" sentinels).
- `llm.py` — the AI steps, in two flavours (Claude Code prompts vs. direct API).


## The two interfaces

#### A. CLI

The initialisation point from a terminal. Every step, one command each:

```bash
python -m pipeline.cli initdb            # create the 5 tables
python -m pipeline.cli status            # row counts per stage

# Source
python -m pipeline.cli source-add --promise URL --status URL [--summary "..."]
python -m pipeline.cli source-add --json lead.json      # ingest a JSON lead
python -m pipeline.cli source-prompt                    # <-- Claude Code path
python -m pipeline.cli source-collect                   # direct API (needs key)
python -m pipeline.cli source-list

# Screen
python -m pipeline.cli screen-prompt --source-id N      # <-- Claude Code path
python -m pipeline.cli screen-add --json row.json --source-id N
python -m pipeline.cli screen-extract --source-id N     # direct API (needs key)
python -m pipeline.cli screen-check --id N              # or --all
python -m pipeline.cli screen-list [--by-capital]

# Verify (the human gate) + edits
python -m pipeline.cli verify-promote --screen-id N --tier V1 [--flag "..."] [--set col=val]
python -m pipeline.cli verify-edit --id N --set current_status="..." --desc "why"
python -m pipeline.cli verify-show --id N                 # row + edit history
python -m pipeline.cli verify-list

# End-to-end (direct API)
python -m pipeline.cli automate --n 3 [--auto-promote --tier V1]

# Explore thresholds beyond the fixed floor (a plain SQL query; AND/OR)
python -m pipeline.cli filter --capital 1000000000 --jobs 2000 --op OR --stage verify
python -m pipeline.cli filter --capital 500000000  --jobs 400  --op AND --stage screen

# The extensible sector vocabulary
python -m pipeline.cli sectors-list
python -m pipeline.cli sectors-add "Aerospace"
```

#### B. Web interface

**Usually the most intuitive way to run this for humans** — every step in the CLI list above is
built into the UI, so a normal session never needs the terminal.

```bash
pip install -r pipeline/requirements.txt
python3 scoreboard.py webapp --reload --port 8100
# open http://localhost:8100
```

Server-rendered HTML, plain forms, no JS build step. The dashboard shows the five
stage counts and the automate panel; the Source and Screen pages carry all
three paths (manual / Claude Code / API); the **Verify** pages are the point of
emphasis — promoting a passing Screen row is the human gate, and each Verify row
has a full 17-field edit form whose saves are recorded in `verify_edits` with a
required reason.


### Running the AI steps with Claude Code (no Anthropic API key)

This is the recommended way to do the "AI" Source/Screen steps without a key: the
pipeline renders the exact operating prompt, **you** run it in a web-search
assistant like Claude Code, and paste the JSON it returns back in.

**Source — find one new project:**

```bash
# 1. Print the Source prompt (already excludes projects you've collected):
python -m pipeline.cli source-prompt
# 2. Paste it into Claude Code (it web-searches and ends with one JSON object).
# 3. Ingest that JSON:
python -m pipeline.cli source-add --json -   # then paste the JSON, Ctrl-D
#    (or save it to lead.json and: source-add --json lead.json)
```

**Screen — extract the row for a lead:**

```bash
python -m pipeline.cli screen-prompt --source-id 7   # prints the prompt w/ the links
# run it in Claude Code, then:
python -m pipeline.cli screen-add --json row.json --source-id 7
python -m pipeline.cli screen-check --id 11
```

In the **web app** the same flow is a button: Source → "Show Source prompt to
run" (copy, run in Claude Code, paste JSON → "Ingest lead JSON"); Screen → enter
a Source id → "Show Screen prompt to run" → paste the row JSON.

> The direct-API path (`source-collect` / `screen-extract` / `automate`) does the
> same thing automatically via the Anthropic Messages API with the web-search +
> web-fetch tools and a JSON-Schema structured-output call — but it needs
> `ANTHROPIC_API_KEY`. The Claude Code path above needs no key.


### A little test (offline, no key, on a copy of the database)

Everything below reads and writes a **copy**, so the committed database is
untouched. From `aici/scoreboard`:

```bash
cp outputs/scoreboard.db /tmp/try.db

# 1. What is in it
python3 scoreboard.py --db /tmp/try.db status
python3 scoreboard.py --db /tmp/try.db screen-list
#   -> each row shows check=CLEAN / PASS / FAIL

# 2. Act as the human gate: publish a row that passed its check
python3 scoreboard.py --db /tmp/try.db verify-promote --screen-id 42 --tier V1 \
    --flag "Resolved: two independent sources agree on the announced date."

# 3. Correct it -- the change is logged with its reason
python3 scoreboard.py --db /tmp/try.db verify-edit --id 34 \
    --set current_status="AT VOLUME (corrected)" \
    --desc "Tightened status wording after re-reading the release."
python3 scoreboard.py --db /tmp/try.db verify-show --id 34
#   -> the row PLUS an "edit history" line from verify_edits

# 4. Try to publish the same project twice -- the gate refuses
python3 scoreboard.py --db /tmp/try.db verify-promote --screen-id 42 --tier V1
#   -> promotion blocked: project already in verify_verified

rm /tmp/try.db
```

**Expected:** the promote succeeds, the second one is blocked, and `verify-show`
prints the edited value with one edit-history entry.

Starting from an **empty** database instead works the same way, except there is
nothing to promote yet: `verify-list` will tell you what to run to collect and
extract some rows first.

```bash
python3 scoreboard.py --db /tmp/empty.db initdb
python3 scoreboard.py --db /tmp/empty.db verify-list
```

Then start the web app (`python3 scoreboard.py webapp`) and click through
the same steps, ending on a Verify detail page to try the edit form.

---
