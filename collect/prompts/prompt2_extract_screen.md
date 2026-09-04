# PROMPT 2 — Extract up to 3 Screen rows from Source leads (standardized input)

You are running as a single Claude Code call. You have been shown
`docs/cli.md`, the pipeline's command reference. Do **only** what this prompt says.

**Where to run:** from the `scoreboard/` directory —
`python3 -m pipeline.cli ...`.

**You must use web fetch / scraping** to read the lead's links — extract only what
the sources actually state, never from in-model knowledge.

**You must run the real pipeline code** — Screen is entered and checked only via the
existing `screen-add` and `screen-check` commands. Do not invent scripts, and do not
compute lag/slip or the `*_dt` dates yourself (the pipeline derives those).

## How many to extract this call: up to 3, one at a time

Extract **up to three** leads in this call. Three is a **ceiling, not a quota**:
if only one lead is left, do that one and stop. Never lower the standard of an
extraction to reach the number — a thin row costs more to fix later than it saves
now.

Do them **one at a time**: a full cycle per lead — render its prompt, read its
sources, `screen-add`, `screen-check` — and only then start the next. Do **not**
read three leads' sources first and write three rows at the end. Each row must be
committed before the next begins, so an interruption leaves finished rows behind
rather than a half-read batch.

**Why three and not one.** Every call pays a fixed start-up cost — reading
`docs/cli.md`, orienting, checking where the database stands — and that cost is
the same whether it produces one row or three. Screening one row per call spent
about 13 turns per row against the Source stage's 6.6, almost entirely because
Source amortises that start-up over five projects and Screen was paying it every
time.

## The standardized input is the pipeline's own rendered prompt

Almost all of this already exists — do not rewrite the extraction rules. Ask which
leads still need screening — that listing *is* the work queue, and it is one line
per lead rather than four:

```
python3 -m pipeline.cli source-list --unscreened --brief    # choose an id, N
```

Re-run it before **each** pick rather than remembering your earlier choices. Every
`screen-add` commits immediately, so a lead you just did disappears from that list
on its own — the database is the record, not your memory of this call.

Then print the **standardized** Screen operating prompt for that lead and follow it
exactly:

```
python3 -m pipeline.cli screen-prompt --source-id N
```

That output is `prompt_screen_extracted.md` + the live sector vocabulary + this
lead's links + the exact JSON hand-back format. It is the single source of truth for
the 17 fields, the `*_raw → token → *_dt` date rules, and what the deterministic
checker enforces. Follow it verbatim.

One part of that prompt gets misread often: the **sector vocabulary is closed**. Use
one of the strings it lists, copied exactly. If none fits, the answer is `Other`, and
you name the candidate in `flag` — never a sector name you coined, never an edit to
`SECTORS` in `schema.py`, and never `sectors-add`.

## Your job

1. `web_fetch` the lead's `promise_source` / `status_source` (and
   `promised_date_source`, if present) and read them.
2. Produce the one JSON row in the shape the printed prompt specifies — each date as
   both a normalized **token** and its verbatim **`*_raw`** partner; digits only for
   `promised_capital_usd` / `promised_jobs`; surface any problem in `flag`. A clean
   extraction **omits** `flag` — never write the word `None`, which is a missing value
   that reads as a present one. Same for any other cell you have nothing for. Omit
   `lag_years`, `slip_years`, the `*_dt` columns, and `verification_tier` — the
   pipeline derives/forces those.
3. Ingest and check with the real commands:
   ```
   python3 -m pipeline.cli screen-add --json scratch/row.json --source-id N
   python3 -m pipeline.cli screen-check --id <new screen id>
   ```
   Read the verdict. If it FAILs on something the sources let you fix, correct the
   JSON and re-add **with `--replace`**:
   ```
   python3 -m pipeline.cli screen-add --json scratch/row.json --source-id N --replace
   ```
   A plain re-add is refused, and that refusal is deliberate: without it the last run
   left two rows for one project, both counted, with no way to delete either. One
   Source lead is one Screen row. Never `screen-remove` — that is a human command.

   No smoke testing beyond running the check.

Then go back to the work-queue listing and start the next lead. **Stop as soon as**
there are no unscreened leads left, or you have extracted three.

Everything at this stage is provisional (`verification_tier = P`). Verify promotion is
a separate **human** gate and is **not** part of this prompt.
