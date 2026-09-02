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
    DERIVED_DATE_COLUMNS,
    RAW_DATE_COLUMNS,
    check_row,
)


def _coerce(col: str, value) -> object:
    """Normalise a cell for storage: '' -> NULL; the two int columns -> int."""
    if value is None:
        return None
    if isinstance(value, str):
        value = value.strip()
        if value == "":
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


def insert_extracted(
    conn: sqlite3.Connection,
    row: dict,
    source_collected_id: int | None = None,
) -> int:
    """Insert one `screen_extracted` row. Returns its id.

    `row` is a mapping of (some of) the 17 v0 columns. Missing columns become
    NULL. verification_tier is forced to 'P' -- Screen is provisional by
    construction, so whatever the extractor claimed is overridden here.
    """
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
