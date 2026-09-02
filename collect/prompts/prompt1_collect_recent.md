# PROMPT 1 — Collect up to 5 NEW Source leads by web search, with a strict "most recent status" check

You are running as a single Claude Code call. You have been shown
`docs/cli.md`, the pipeline's command reference. Do **only** what this prompt says.

**Where to run:** from the `scoreboard/` directory —
`python -m pipeline.cli ...`.

**You must use web fetch / scraping** for everything — do not rely on in-model
knowledge for either the announcement or (especially) the current status.

**You must run the real pipeline code** — a lead enters Source only via
`python -m pipeline.cli source-add`. Do not invent scripts.

## How many to collect this call: up to 5, one at a time

Collect **up to five** new qualifying projects in this call. Five is a **ceiling,
not a quota**: if you cannot find five that genuinely clear every bar, collect
fewer — even zero — and stop. **Never** loosen the thresholds to reach the number
(the canonical prompt's rule to output nothing before forcing a weak result still
holds).

Do them **one at a time**, each as its own record and its own `source-add`. Do
**not** batch several projects into one JSON object or one insert — "handle exactly
one project at a time" applies per record.

**Dedup rides on the database, not your memory.** Because every `source-add`
commits immediately, a lead you just added is instantly visible to the next
`source-prompt`. So refresh the exclusion list before each pick rather than trying
to remember what you've done.

To keep the five productive, **vary your search axis** across them — a different
sector, state, and announcement year each time — so you cover new ground instead of
circling the same few famous megaprojects.

### Prioritise $1B and above — but keep what you find below it

The Scoreboard is being made **complete at $1B first**, then lowered in later
phases. So when choosing between candidates, prefer the larger: a $3B plant is
worth more to this phase than a $300M one.

This is a priority, **not a filter**. The inclusion floor is still $100M capital
**or** 200 direct jobs. If you come across a qualifying project below $1B, collect
it — do not discard it and do not go looking for a bigger one instead. Those rows
cost nothing sitting in Source, and they are what makes the next phase cheap.

What you must never do is loosen the $100M / 200-job floor to reach a number. The
rule against forcing a weak result outranks both the ceiling of five and this
preference for size.

## The per-project loop — repeat up to 5 times

1. **Refresh the rules + exclusion list.** Run:
   ```
   python -m pipeline.cli source-prompt
   ```
   That renders `prompt_source_collected.md` **plus the live "do not collect these"
   lists** (verify + in-flight Source/Screen — including every lead you added earlier
   in this same call). It defines what qualifies: U.S. single facility; announced
   capital ≥ $100M **OR** ≥ 200 direct jobs; announced Jan 2017–today; one of the
   defined manufacturing sectors. Follow it; do not restate or weaken it.
2. **Find ONE genuinely new qualifying project** — via `web_search` / `web_fetch`,
   not in-model knowledge — that is **not** in the refreshed lists above.
3. **Do the most-recent-status check** (next section) for its `status_source`.
4. **Write a distinctive `summary`** — company + facility + state + what was
   announced (e.g. "Micron announced ~$15B DRAM fab in Boise, ID in Sep 2022").
   This summary is the dedup key the next iteration will see, so make it clearly
   identify the project.
5. **Ingest the lead** with the real command:
   ```
   python -m pipeline.cli source-add --json lead.json --via prompt1   # or: --json - --via prompt1
   ```
   The `--via prompt1` flag records this lead's provenance in
   `source_collected.collected_via` (open-web discovery) — always include it.

Then go back to step 1 for the next project. **Stop as soon as** you can't find
another genuinely-new qualifying project, or you've collected five.

## The critical rule (applies to every project): the "produced" side must be the MOST RECENT status

Work from **today's actual date**, whatever it is when this runs — do not assume a
date from an earlier session. For each `status_source` — the "produced" half of the promised-vs-produced pair — you
**MUST** be certain you are getting the **most recent** entry on the facility's
current state, not a stale article:

- `web_search` for the newest reporting (target the current year / the latest
  available) and `web_fetch` it to confirm it reflects where the project stands
  **as of today** (producing / at volume / delayed / cancelled).
- Always prefer the freshest credible update. If the most recent source you can find
  is still old, say so explicitly in the `summary`.
- The whole point of the pair is that "produced" describes the present — a stale
  status silently corrupts the lag/slip computed downstream.

## Output shape

Each lead is the single JSON object (`promise_source`, `status_source`, optional
`promised_date_source`, `summary`) in the *Output format* the printed prompt
specifies — one per `source-add`. Source holds links + a neutral summary only. No
smoke testing needed.
