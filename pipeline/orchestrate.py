"""
orchestrate.py -- the moves between the stages.

This is the "move between the stages" logic the prompt asked for, shared by both
interfaces (CLI and web). Every step here works whether it is being automated or
a human is doing it:

  * Source and Screen pt-1 can be done manually (source.insert_lead /
    screen.insert_extracted) OR computationally (run_source_ai / run_screen_ai).
  * Screen pt-2 (the check) is always deterministic/computational.
  * Verify promotion is the human gate (verify.promote), and nothing here
    an explicit, clearly-labelled auto-promote for the "all the way to verify"
    convenience option, which bypasses that gate on purpose.
"""

from __future__ import annotations

import re
import sqlite3

from pipeline import source, screen, verify, llm


def existing_project_names(conn: sqlite3.Connection) -> list[str]:
    """Project names already PUBLISHED in the verify table (`verify_verified`).

    This is the authoritative "already covered, don't collect these" set the
    source collector is steered away from. It is read straight from the verify
    scoreboard -- the pipeline's final product -- NOT from Source/Screen: verify is the
    single authority for "already have it," and dedup of in-flight Source/Screen
    leads is a later-stage concern. Fed to `llm.render_source_prompt` (both the
    Claude Code and the direct-API path) as the live exclusion list."""
    names = set()
    for r in conn.execute("SELECT project FROM verify_verified").fetchall():
        if r["project"]:
            names.add(r["project"])
    return sorted(names)


_SUMMARY_DASH = re.compile(r"\s+(?:\u2014|\u2013|--)\s+")
_SUMMARY_STOP = re.compile(r"(?<=[a-z0-9)])\.\s+(?=[A-Z])")


def _summary_head(summary: str, floor: int = 80, cap: int = 140) -> str:
    """The identifying head of a Source summary, for use as an exclusion entry.

    Source rows carry no project name, so the summary is their only identifier
    -- but it is written for a human reader and averages ~310 characters. Every
    source worker reads the whole exclusion list on every iteration, so a long
    run pays for that length once per row per iteration: at N=300 the untrimmed
    summaries cost ~2.5M tokens more than their heads do.

    Collectors write summaries as `Title -- prose. Operator: X.`, and that title
    ("Eli Lilly Houston API Plant") makes a better entry than the prose. It is
    ~130 characters, and it usually names the company *and* the site -- which is
    what the prompt's exclude-sites-not-companies rule needs in order to tell
    one Eli Lilly plant from another. A title shorter than `floor` is not
    trusted to stand alone ("Nucor plate mill" leaves the town on the far side
    of the dash), so those read on to the floor instead; measured over the
    archived corpus that lifts entries retaining a place name from 46% to 73%,
    at 42% of the full summary length rather than 30%. Summaries written in
    some other shape fall back to the first sentence, then to a word-boundary
    cut at `cap`.
    """
    summary = (summary or "").strip()
    if not summary:
        return ""
    for pattern in (_SUMMARY_DASH, _SUMMARY_STOP):
        m = pattern.search(summary)
        # Ignore a match so early it cannot be a title (a stray "--"), or so
        # late that keeping only the head would save nothing.
        if m and 8 <= m.start() <= cap:
            end = m.start() + (1 if pattern is _SUMMARY_STOP else 0)
            if end >= floor:
                return summary[:end].strip()
            # Title too terse to stand alone ("Nucor plate mill" when the town
            # is on the far side of the dash). Keep reading to the floor.
            break
    if len(summary) <= cap:
        return summary
    cut = summary.rfind(" ", 0, cap)
    return summary[:cut if cut > floor else cap].rstrip() + "\u2026"


def inflight_project_hints(conn: sqlite3.Connection) -> list[str]:
    """Identifiers for leads already in flight in Source/Screen but not yet in Verify.

    Verify is a human-only gate, so `verify_verified` stays empty during automated
    collection. If the source collector saw only the verify exclusion list, it would
    re-collect the same top project on every iteration -- the duplicate-source bug.
    These hints -- Screen `project` names, plus the identifying head of the
    `summary` for source rows not yet screened (source rows have no project
    name of their own; see `_summary_head`) -- are what actually stop duplicate
    collection within and across `gather` runs. Because `source.insert_lead`
    commits immediately, a lead collected earlier in the same batch is already
    visible here on the next call. Fed to `llm.render_source_prompt` alongside the
    verify list, under a distinct "already in flight" section."""
    hints = set()
    for r in conn.execute("SELECT project FROM screen_extracted").fetchall():
        project = (r["project"] or "").strip()
        if project:
            hints.add(project)
    # Summaries only for leads that have not been screened yet: once a lead
    # reaches Screen it has a `project` name for the same site, and listing
    # both names the project twice.
    for r in conn.execute(
        "SELECT summary FROM source_collected s WHERE NOT EXISTS "
        "(SELECT 1 FROM screen_extracted e WHERE e.source_collected_id = s.id)"
    ).fetchall():
        summary = _summary_head(r["summary"] or "")
        if summary:
            hints.add(summary)
    return sorted(hints)


def filter_by_thresholds(
    conn: sqlite3.Connection,
    capital_min: int = 0,
    jobs_min: int = 0,
    op: str = "AND",
    stage: str = "verify",
) -> list[sqlite3.Row]:
    """Explore-filter: rows clearing a capital and/or jobs threshold.

    The inclusion floor in the checker is fixed ($100M OR 200 jobs). This lets
    you *explore* alternative thresholds and combinators over what's already
    collected -- e.g. "$1B AND 2,000 jobs", or "$500M AND 400 jobs" -- WITHOUT
    changing that gate. It is a plain parameterised SQL query:

        SELECT * FROM <table>
        WHERE promised_capital_usd >= ?  <AND|OR>  promised_jobs >= ?

    `op` is 'AND' or 'OR' (anything else is treated as 'OR'); `stage` is 'verify'
    or 'screen'. Both the table and the combiner come from fixed whitelists, so
    the string interpolation is injection-safe; the two thresholds are bound
    parameters.
    """
    table = {"verify": "verify_verified", "screen": "screen_extracted"}.get(stage)
    if table is None:
        raise ValueError(f"stage must be 'verify' or 'screen', got {stage!r}")
    combiner = "AND" if str(op).strip().upper() == "AND" else "OR"
    sql = (
        f"SELECT * FROM {table} "
        f"WHERE promised_capital_usd >= ? {combiner} promised_jobs >= ? "
        "ORDER BY promised_capital_usd DESC, promised_jobs DESC"
    )
    return conn.execute(sql, (int(capital_min or 0), int(jobs_min or 0))).fetchall()


def run_source_ai(conn: sqlite3.Connection) -> tuple[int, dict]:
    """AI Source: collect one new lead from the web and store it.

    Returns (source_id, lead_dict). Raises llm.LLMUnavailable on failure.
    """
    lead = llm.collect_source_lead(
        avoid_projects=existing_project_names(conn),
        avoid_inflight=inflight_project_hints(conn),
    )
    bid = source.insert_lead(
        conn,
        promise_source=lead["promise_source"],
        status_source=lead["status_source"],
        promised_date_source=lead.get("promised_date_source"),
        summary=lead.get("summary"),
        collected_via="api",
    )
    return bid, lead


def run_screen_ai(conn: sqlite3.Connection, source_id: int) -> tuple[int, dict]:
    """AI Screen pt-1: extract a 17-column row from a stored Source lead.

    Returns (screen_id, row_dict). Raises llm.LLMUnavailable on failure.
    """
    lead_row = source.get_lead(conn, source_id)
    if lead_row is None:
        raise ValueError(f"no source_collected lead with id {source_id}")
    lead = {
        "promise_source": lead_row["promise_source"],
        "status_source": lead_row["status_source"],
        "promised_date_source": lead_row["promised_date_source"],
        "summary": lead_row["summary"],
    }
    row = llm.extract_screen_row(lead)
    sid = screen.insert_extracted(conn, row, source_collected_id=source_id)
    return sid, row
