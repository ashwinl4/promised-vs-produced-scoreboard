"""
screen.py -- Screen stage operations (`screen_extracted` + `screen_check`).

Screen pt 1 (`screen_extracted`): the extraction result -- one project row in
the 17-column v0_out shape, always at verification_tier 'P'. Extraction problems
belong in the `flag` cell, never dropped or guessed.

Screen pt 2 (`screen_check`): the deterministic, computer-based verification.
Running it is just calling schema_check.check_row() (which *is*
schema.py) against a stored row and persisting the verdict
plus a pointer back to the row it judged.
"""

from __future__ import annotations

import json
import sqlite3

from pipeline.db import now_iso
from pipeline.dates import enrich as enrich_dates, DATE_TRIPLES
from pipeline.schema_check import (
    V0_COLUMNS,
    INT_COLUMNS,
    NULL_STRINGS,
    DATE_COLUMN_NULL_STRINGS,
    DERIVED_DATE_COLUMNS,
    RAW_DATE_COLUMNS,
    check_row,
)


def _coerce(col: str, value) -> object:
    """Normalise a cell for storage: '' -> NULL; the two int columns -> int.

    A missing value written as text ('None', 'null', 'undefined') is blanked
    here, at the door, rather than stored and flagged later. It is not data --
    it is what a serializer emits when handed nothing -- and stored as-is it
    reads as a populated cell to everything downstream. The two first-output
    columns exempt only the overlap with the date sentinels: there 'n/a' is a
    documented DATE_SENTINEL meaning "no calendar date", a real answer -- but
    'None' is not, and must be blanked there too.
    """
    if value is None:
        return None
    if isinstance(value, str):
        value = value.strip()
        if value == "":
            return None
        bad = (DATE_COLUMN_NULL_STRINGS
               if col in ("promised_first_output", "actual_first_output")
               else NULL_STRINGS)
        if value.lower() in bad:
            return None
    if col in INT_COLUMNS and value is not None:
        s = str(value).replace(",", "").replace("_", "").replace("$", "").strip()
        if s == "":
            return None
        try:
            return int(float(s)) if "." in s else int(s)
        except ValueError:
            # Leave un-parseable numerics as text so the checker flags them.
            return str(value)
    return value


class DuplicateExtraction(Exception):
    """A Screen row already exists for this Source lead."""


class RemovalBlocked(Exception):
    """The Screen row cannot be removed, because something downstream cites it."""


def extracted_for_source(conn: sqlite3.Connection, source_collected_id: int) -> list[int]:
    """Screen row ids already extracted from a given Source lead."""
    return [r["id"] for r in conn.execute(
        "SELECT id FROM screen_extracted WHERE source_collected_id = ? ORDER BY id",
        (source_collected_id,),
    ).fetchall()]


def distinct_project_count(conn: sqlite3.Connection) -> int:
    """How many distinct projects Screen holds, not how many rows.

    A lead extracted twice is one project, and the collection loop stops when it
    has added enough PROJECTS -- so this, not COUNT(*), is what it must count.
    Rows with no lineage (added by hand, source_collected_id NULL) cannot be
    compared to anything, so each counts once: -id gives every one of them a key
    of its own that can never collide with a real lead id.
    """
    return int(conn.execute(
        "SELECT COUNT(*) FROM (SELECT 1 FROM screen_extracted "
        "GROUP BY COALESCE(source_collected_id, -id))"
    ).fetchone()[0])


def remove_extracted(conn: sqlite3.Connection, screen_id: int) -> dict:
    """Delete one Screen row and the checks that judged it. Returns what went.

    Refuses when the row has been published: a Verify row names the Screen row
    it came from, and deleting it would leave published research data pointing
    at nothing. Retract the Verify row first if that is really the intent.
    """
    row = get_extracted(conn, screen_id)
    if row is None:
        raise ValueError(f"no screen_extracted row with id {screen_id}")
    published = [r["id"] for r in conn.execute(
        "SELECT id FROM verify_verified WHERE screen_extracted_id = ?", (screen_id,)
    ).fetchall()]
    if published:
        raise RemovalBlocked(
            f"screen #{screen_id} was published as verify "
            f"#{', #'.join(str(v) for v in published)}. Removing it would leave "
            "that published row citing a source row that no longer exists."
        )
    n_checks = conn.execute(
        "DELETE FROM screen_check WHERE screen_extracted_id = ?", (screen_id,)
    ).rowcount
    conn.execute("DELETE FROM screen_extracted WHERE id = ?", (screen_id,))
    conn.commit()
    return {"id": screen_id, "project": row["project"], "checks": n_checks}


def insert_extracted(
    conn: sqlite3.Connection,
    row: dict,
    source_collected_id: int | None = None,
    replace: bool = False,
) -> int:
    """Insert one `screen_extracted` row. Returns its id.

    `row` is a mapping of (some of) the 17 v0 columns. Missing columns become
    NULL. verification_tier is forced to 'P' -- Screen is provisional by
    construction, so whatever the extractor claimed is overridden here.

    One Source lead extracts to one Screen row. A second attempt on the same
    lead raises DuplicateExtraction unless `replace` is set, in which case the
    earlier row (and its checks) are removed first. The N=20 run is why: a row
    failed its check over a bad cell, the extractor corrected the JSON and added
    it again, and both copies stayed -- one project counted twice, with no way
    to delete either. Refusing here is what stops that from being possible.
    """
    if source_collected_id is not None:
        existing = extracted_for_source(conn, source_collected_id)
        if existing and not replace:
            raise DuplicateExtraction(
                f"source #{source_collected_id} is already extracted as screen "
                f"#{', #'.join(str(e) for e in existing)}. To correct that row, "
                f"re-add with --replace; to keep both, they are not the same "
                f"project and one of them has the wrong --source-id."
            )
    else:
        existing = []

    values = {c: _coerce(c, row.get(c)) for c in V0_COLUMNS}
    values["verification_tier"] = "P"  # invariant at this stage
    # Deterministically derive the *_dt columns and the float lag/slip from the
    # normalized date tokens -- whatever the extractor put in lag_years/slip_years
    # is overwritten here so two models that agree on the dates agree on lag/slip.
    values = enrich_dates(values)

    # Store the verbatim source text of each date (the raw -> token -> dt chain).
    # If the caller supplied no distinct verbatim capture, fall back to the
    # normalized token so the raw cell still reflects what was extracted: the API
    # path provides real verbatim; seed/manual paths reuse the token string.
    for raw_col, token_col, _dt_col in DATE_TRIPLES:
        raw_val = _coerce(raw_col, row.get(raw_col))
        values[raw_col] = raw_val if raw_val is not None else values.get(token_col)

    all_cols = list(V0_COLUMNS) + list(DERIVED_DATE_COLUMNS) + list(RAW_DATE_COLUMNS)
    cols = ["datetime", "source_collected_id"] + all_cols
    placeholders = ", ".join("?" for _ in cols)
    params = [now_iso(), source_collected_id] + [values[c] for c in all_cols]

    cur = conn.execute(
        f"INSERT INTO screen_extracted ({', '.join(cols)}) VALUES ({placeholders})",
        params,
    )
    conn.commit()

    # Supersede only once the replacement is safely in. Removing first looked
    # tidier and was wrong: this INSERT can still fail (a NOT NULL column the
    # corrected JSON forgot, say), and by then the row being corrected would
    # already be deleted -- a bad row replaced by no row at all. Caught in
    # testing, on exactly that failure.
    for old_id in existing:
        remove_extracted(conn, old_id)

    return int(cur.lastrowid)


def row_to_v0_dict(row: sqlite3.Row) -> dict:
    """Extract just the 17 v0 columns from a screen/verify row, as a plain dict."""
    return {c: row[c] for c in V0_COLUMNS}


def run_check(conn: sqlite3.Connection, screen_extracted_id: int) -> dict:
    """Run the deterministic checker over one screen row and persist the result.

    Returns the check dict (result_status/n_errors/n_warnings/report) and writes
    a `screen_check` row pointing back at screen_extracted_id.
    """
    src = conn.execute(
        "SELECT * FROM screen_extracted WHERE id = ?", (screen_extracted_id,)
    ).fetchone()
    if src is None:
        raise ValueError(f"no screen_extracted row with id {screen_extracted_id}")

    result = check_row(row_to_v0_dict(src))

    conn.execute(
        """
        INSERT INTO screen_check
            (datetime, screen_extracted_id, result_status, n_errors, n_warnings, report)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (
            now_iso(),
            screen_extracted_id,
            result["result_status"],
            result["n_errors"],
            result["n_warnings"],
            json.dumps(result["report"]),
        ),
    )
    conn.commit()
    return result


def latest_check(conn: sqlite3.Connection, screen_extracted_id: int) -> sqlite3.Row | None:
    """The most recent checker run for a given screen row (or None)."""
    return conn.execute(
        """
        SELECT * FROM screen_check
        WHERE screen_extracted_id = ?
        ORDER BY id DESC LIMIT 1
        """,
        (screen_extracted_id,),
    ).fetchone()


def list_extracted(conn: sqlite3.Connection,
                   by_capital: bool = False) -> list[sqlite3.Row]:
    """Screen rows, by id (insertion order) or largest capital first.

    Capital order is how verification is meant to proceed: the Scoreboard is made
    complete from the top down, so wherever review stops, the claim above that
    point is intact. promised_capital_usd is TEXT, so it is cast for sorting and
    rows without a figure sort last."""
    if by_capital:
        return conn.execute(
            "SELECT * FROM screen_extracted "
            "ORDER BY CAST(NULLIF(promised_capital_usd, '') AS INTEGER) DESC "
            "NULLS LAST, id"
        ).fetchall()
    return conn.execute("SELECT * FROM screen_extracted ORDER BY id").fetchall()


def get_extracted(conn: sqlite3.Connection, screen_id: int) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM screen_extracted WHERE id = ?", (screen_id,)
    ).fetchone()
