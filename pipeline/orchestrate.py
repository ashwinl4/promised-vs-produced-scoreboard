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


def inflight_project_hints(conn: sqlite3.Connection) -> list[str]:
    """Identifiers for leads already in flight in Source/Screen but not yet in Verify.

    Verify is a human-only gate, so `verify_verified` stays empty during automated
    collection. If the source collector saw only the verify exclusion list, it would
    re-collect the same top project on every iteration -- the duplicate-source bug.
    These hints -- Screen `project` names and Source `summary` lines (source rows
    have no project name of their own) -- are what actually stop duplicate
    collection within and across `gather` runs. Because `source.insert_lead`
    commits immediately, a lead collected earlier in the same batch is already
    visible here on the next call. Fed to `llm.render_source_prompt` alongside the
    verify list, under a distinct "already in flight" section."""
    hints = set()
    for r in conn.execute("SELECT project FROM screen_extracted").fetchall():
        project = (r["project"] or "").strip()
        if project:
            hints.add(project)
    for r in conn.execute("SELECT summary FROM source_collected").fetchall():
        summary = (r["summary"] or "").strip()
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
