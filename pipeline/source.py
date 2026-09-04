"""
source.py -- Source stage operations (`source_collected`).

A Source row is a raw *lead*: two required source links (promise + status), an
optional promised-date link, and an optional context summary. No extraction, no
typing, no verification happens here -- that is entirely the later stages' job.
This module is deliberately thin: insert a lead, list leads, fetch one.
"""

from __future__ import annotations

import sqlite3

from pipeline.db import now_iso
from pipeline.schema_check import NULL_STRINGS


def _clean(value: str | None) -> str | None:
    """Trim, and treat a missing value written as text as the absence it is.

    Source cells are URLs and prose, so 'None' or 'null' here is never a real
    answer -- it is str(None) from whatever handed the lead over. Blanking it at
    the door keeps a cell that only looks populated out of the database.
    """
    v = (value or "").strip()
    if not v or v.lower() in NULL_STRINGS:
        return None
    return v


def insert_lead(
    conn: sqlite3.Connection,
    promise_source: str,
    status_source: str,
    promised_date_source: str | None = None,
    summary: str | None = None,
    collected_via: str | None = None,
) -> int:
    """Insert one `source_collected` lead. Returns its new id.

    Only promise_source and status_source are required (necessary); the other
    three are optional. `collected_via` is a provenance label naming the entry
    path that produced this lead (prompt1 | prompt2 | seed | api | manual);
    NULL means unrecorded. Duplicates are permitted by design -- there is no
    unique constraint, because two collectors filing the same links is
    tolerated; dedup is a Screen/Verify concern, not Source's.
    """
    promise_source = (promise_source or "").strip()
    status_source = (status_source or "").strip()
    if not promise_source or not status_source:
        raise ValueError("promise_source and status_source are both required")

    cur = conn.execute(
        """
        INSERT INTO source_collected
            (datetime, promise_source, status_source, promised_date_source, summary, collected_via)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (
            now_iso(),
            promise_source,
            status_source,
            _clean(promised_date_source),
            _clean(summary),
            _clean(collected_via),
        ),
    )
    conn.commit()
    return int(cur.lastrowid)


def list_leads(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM source_collected ORDER BY id"
    ).fetchall()


def get_lead(conn: sqlite3.Connection, lead_id: int) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM source_collected WHERE id = ?", (lead_id,)
    ).fetchone()
