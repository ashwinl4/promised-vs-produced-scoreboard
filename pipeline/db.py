"""
db.py -- the SQLite realisation of the five-table Source/Screen/Verify pipeline
(a medallion architecture; see "Why three stages" in README.md).

The original design targeted Postgres/Supabase; this module implements the exact
same table *shapes* in SQLite so the whole pipeline runs locally with zero
external services (stdlib `sqlite3` only). Every column, constraint, and the
`id` + `datetime` convention matches that design; the SQL is engine-portable
enough that swapping in Postgres later is a mechanical translation.

Six design questions the original note left open, and how they are settled here
(all documented in the README):
  1. Cross-stage lineage FKs ARE added (screen_extracted.source_collected_id,
     verify_verified.screen_extracted_id) -- full provenance.
  2. verify_verified.datetime == last-modified; a separate created_at records
     first entry.
  3. screen_check grain == one row per run, with the issue list in `report`.
  4. Screen rows are strictly tier 'P' (enforced by CHECK).
  5. Source.summary is optional (nullable).
  6. verify_verified has UNIQUE(project) -- one published row per project.
"""

from __future__ import annotations

import os
import shutil
import sqlite3
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from pipeline.schema_check import (
    V0_COLUMNS,
    INT_COLUMNS,
    FLOAT_COLUMNS,
    DERIVED_DATE_COLUMNS,
    RAW_DATE_COLUMNS,
)

DEFAULT_DB = Path(__file__).resolve().parent.parent / "outputs" / "scoreboard.db"


def now_iso() -> str:
    """A UTC ISO-8601 timestamp -- the value of every `datetime` column."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def db_path() -> Path:
    """The picker's choice, else $SCOREBOARD_DB, else the default file.

    $MEDALLION_DB is still honoured as a deprecated alias, so shells and
    scripts set up before the scoreboard.db rename keep working."""
    if _ACTIVE is not None:
        return _ACTIVE
    override = os.getenv("SCOREBOARD_DB") or os.getenv("MEDALLION_DB")
    return Path(override) if override else DEFAULT_DB


# --------------------------------------------------------------------------- #
# Which database are we pointed at?                                            #
# --------------------------------------------------------------------------- #

# The pre-rename vocabulary. A database written before the Source/Screen/Verify
# rename still carries these table names; we can read those files, but we never
# write to them.
LEGACY_TABLES = {
    "source_collected": "bronze_collected",
    "screen_extracted": "silver_extracted",
    "screen_check":     "silver_check",
    "verify_verified":  "gold_verified",
    "verify_edits":     "gold_edits",
}
LEGACY_COLUMNS = {
    "screen_extracted": {"bronze_collected_id": "source_collected_id"},
    "screen_check":     {"silver_extracted_id": "screen_extracted_id"},
    "verify_verified":  {"silver_extracted_id": "screen_extracted_id"},
    "verify_edits":     {"gold_verified_id":    "verify_verified_id"},
}

# Set by the web app's database picker; overrides both the env var and the
# default. Process-local, so switching in the UI never touches anything on disk.
_ACTIVE: Path | None = None


def set_active_db(path: str | Path | None) -> None:
    """Point every later connect() at `path` (None = fall back to env/default)."""
    global _ACTIVE
    _ACTIVE = Path(path) if path is not None else None


def schema_flavour(path: str | Path) -> str:
    """'renamed' | 'legacy' | 'empty' | 'missing' -- which vocabulary `path` uses."""
    path = Path(path)
    if not path.exists():
        return "missing"
    try:
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        names = {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        conn.close()
    except sqlite3.Error:
        return "missing"
    if "source_collected" in names:
        return "renamed"
    if "bronze_collected" in names:
        return "legacy"
    return "empty"


def is_read_only(path: str | Path | None = None) -> bool:
    """Legacy-vocabulary files are readable but never written to."""
    return schema_flavour(path if path is not None else db_path()) == "legacy"


def discover_databases() -> list[dict]:
    """Every scoreboard*.db under outputs/, with its vocabulary and row count.

    Legacy `medallion*.db` files (the name this database used before the
    scoreboard.db rename) are listed too, so an older copy can still be opened.

    This is what the web app's picker lists. Row count is the sum across the
    five stage tables under whichever vocabulary the file uses, so a legacy and
    a renamed copy of the same data report the same number."""
    root = DEFAULT_DB.parent
    found = {p.resolve(): p
             for pattern in ("scoreboard*.db", "medallion*.db")
             for p in root.rglob(pattern)}
    out = []
    for path in sorted(found.values()):
        flavour = schema_flavour(path)
        rows = 0
        if flavour in ("renamed", "legacy"):
            tables = (list(LEGACY_TABLES) if flavour == "renamed"
                      else list(LEGACY_TABLES.values()))
            conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
            for t in tables:
                try:
                    rows += conn.execute(f"SELECT COUNT(*) FROM [{t}]").fetchone()[0]
                except sqlite3.OperationalError:
                    pass
            conn.close()
        out.append({
            "path": path,
            "rel": str(path.relative_to(root)),
            "flavour": flavour,
            "rows": rows,
            "active": path.resolve() == db_path().resolve(),
        })
    return out


def _legacy_view(src: Path) -> Path:
    """A throwaway renamed copy of a legacy database, for read-only browsing.

    ALTER TABLE ... RENAME on the original would rewrite a file we promised to
    leave alone, so instead we copy it to the temp dir and rename there. The copy
    is rebuilt whenever the source's size or mtime changes."""
    stat = src.stat()
    dest = (Path(tempfile.gettempdir()) /
            f"scoreboard_legacyview_{abs(hash((str(src.resolve()), stat.st_mtime_ns, stat.st_size)))}.db")
    if dest.exists():
        return dest
    tmp = dest.with_suffix(".partial")
    shutil.copyfile(src, tmp)
    conn = sqlite3.connect(str(tmp))
    conn.execute("PRAGMA legacy_alter_table = OFF")   # REFERENCES follow the rename
    for new, old in LEGACY_TABLES.items():
        conn.execute(f"ALTER TABLE {old} RENAME TO {new}")
    for table, cols in LEGACY_COLUMNS.items():
        for old, new in cols.items():
            conn.execute(f"ALTER TABLE {table} RENAME COLUMN {old} TO {new}")
    conn.commit()
    conn.close()
    tmp.replace(dest)
    return dest


SCOREBOARD_ROOT = Path(__file__).resolve().parent.parent


def _autoexport(target: Path) -> None:
    """Refresh outputs/csv_tables/ from `target`.

    Never raises. The database write has already committed by the time this
    runs, and a failed export must not turn a successful promotion into a
    traceback.
    """
    if os.getenv("SCOREBOARD_NO_AUTOEXPORT"):
        return
    try:
        if str(SCOREBOARD_ROOT) not in sys.path:
            sys.path.insert(0, str(SCOREBOARD_ROOT))
        from tools.export_tables import export_all
        export_all(db=target)
        print("(database changed -- refreshed outputs/csv_tables/)", file=sys.stderr)
    except Exception as exc:                                    # noqa: BLE001
        print(f"warning: could not refresh outputs/csv_tables/ ({exc}). "
              "Run `python3 scoreboard.py export` before committing.", file=sys.stderr)


class _SyncingConnection(sqlite3.Connection):
    """A connection that keeps the CSV exports in step with the database.

    scoreboard.db is committed, and git cannot diff a binary, so the CSVs beside
    it are how a change becomes readable in a review. Keeping them in step by
    hand does not work: it depends on remembering, at the moment you are
    thinking about something else.

    The export therefore happens where every writer already passes, at close.
    sqlite3 counts changed rows on a connection in `total_changes`, which is the
    entire dirty flag. Both the CLI and the web app reach the database through
    connect(), and the web app writes straight to the pipeline modules rather
    than through the CLI, so this is the only layer that catches both.

    Only the committed database is mirrored; see connect().
    """

    _mirror_to: Path | None = None

    def close(self) -> None:
        # Read the counter first: it is unavailable once the handle is closed.
        changed = self.total_changes
        target = self._mirror_to
        try:
            super().close()
        finally:
            if changed and target is not None:
                _autoexport(target)


def connect(path: str | Path | None = None) -> sqlite3.Connection:
    """Open a connection with row access by name and FK enforcement on.

    A legacy-vocabulary file is opened through a read-only renamed copy, so the
    rest of the code can query `source_collected` / `verify_verified` without
    knowing (and without the original file ever being modified)."""
    target = Path(path) if path is not None else db_path()
    if schema_flavour(target) == "legacy":
        # Read-only, so it can never dirty anything and never needs mirroring.
        conn = sqlite3.connect(f"file:{_legacy_view(target)}?mode=ro", uri=True)
    else:
        conn = sqlite3.connect(str(target), factory=_SyncingConnection)
        # Mirror to CSV only for the database that is committed. A --db copy, a
        # SCOREBOARD_DB pointed elsewhere, or the web app's picker must never
        # write their rows over the real exports.
        try:
            if target.resolve() == DEFAULT_DB.resolve():
                conn._mirror_to = target
        except OSError:
            pass
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


# --------------------------------------------------------------------------- #
# DDL -- the five tables (Source / Screen x2 / Verify x2)                        #
# --------------------------------------------------------------------------- #

# The 17 v0 columns as SQL fragments, plus each date's derived *_dt and verbatim
# *_raw partners. Capital and jobs are INTEGER; lag_years/slip_years are REAL
# (computed floats with -1/-2 sentinels); the normalized date *tokens* stay TEXT
# (the checker owns what they may hold). Every date cell carries a TEXT *_dt
# (its resolved ISO date) and a TEXT *_raw (the exact source text it came from).
def _v0_column_ddl(tier_check: str) -> str:
    lines = []
    for col in V0_COLUMNS:
        if col in INT_COLUMNS:
            lines.append(f"    {col} INTEGER")
        elif col in FLOAT_COLUMNS:
            lines.append(f"    {col} REAL")   # computed lag/slip (with -1/-2 sentinels)
        elif col == "verification_tier":
            lines.append(f"    verification_tier TEXT NOT NULL {tier_check}")
        elif col in ("project", "sector", "state", "announced", "current_status"):
            lines.append(f"    {col} TEXT NOT NULL")
        else:
            lines.append(f"    {col} TEXT")
    # Append the derived DATETIME interpretations (ISO 'YYYY-MM-DD', or NULL for a
    # sentinel date) and the verbatim source text each date was extracted from.
    for col in list(DERIVED_DATE_COLUMNS) + list(RAW_DATE_COLUMNS):
        lines.append(f"    {col} TEXT")
    return ",\n".join(lines)


SCHEMA = f"""
-- SOURCE ------------------------------------------------------------------- --
-- One collected *lead*: the source links + a context summary. No processing.
-- Duplicates are allowed by design (no unique constraint).
CREATE TABLE IF NOT EXISTS source_collected (
    id                    INTEGER PRIMARY KEY AUTOINCREMENT,
    datetime              TEXT NOT NULL,                 -- collected-at
    promise_source        TEXT NOT NULL,                 -- required
    status_source         TEXT NOT NULL,                 -- required
    promised_date_source  TEXT,                          -- optional
    summary               TEXT,                          -- context only
    collected_via         TEXT                           -- provenance: entry path (prompt1|prompt2|seed|api|manual); NULL = unrecorded
);

-- SCREEN pt 1 -------------------------------------------------------------- --
-- One extracted *project* row in the 17-column v0_out shape. Always tier 'P'.
CREATE TABLE IF NOT EXISTS screen_extracted (
    id                    INTEGER PRIMARY KEY AUTOINCREMENT,
    datetime              TEXT NOT NULL,                 -- extracted-at
    source_collected_id   INTEGER REFERENCES source_collected(id),
{_v0_column_ddl("DEFAULT 'P' CHECK (verification_tier = 'P')")}
);

-- SCREEN pt 2 -------------------------------------------------------------- --
-- One deterministic checker run over one screen_extracted row (schema.py).
CREATE TABLE IF NOT EXISTS screen_check (
    id                    INTEGER PRIMARY KEY AUTOINCREMENT,
    datetime              TEXT NOT NULL,                 -- checked-at
    screen_extracted_id   INTEGER NOT NULL REFERENCES screen_extracted(id),
    result_status         TEXT NOT NULL,                 -- FAIL | PASS | CLEAN
    n_errors              INTEGER NOT NULL DEFAULT 0,
    n_warnings            INTEGER NOT NULL DEFAULT 0,
    report                TEXT                           -- JSON list of issue objects
);

-- VERIFY --------------------------------------------------------------------- --
-- One published project row. Tier is V1/V2 -- never P. datetime = last-modified.
CREATE TABLE IF NOT EXISTS verify_verified (
    id                    INTEGER PRIMARY KEY AUTOINCREMENT,
    datetime              TEXT NOT NULL,                 -- last-modified (edits bump this)
    created_at            TEXT NOT NULL,                 -- first reached Verify
    screen_extracted_id   INTEGER REFERENCES screen_extracted(id),
{_v0_column_ddl("CHECK (verification_tier <> 'P')")},
    UNIQUE (project)
);

-- VERIFY audit --------------------------------------------------------------- --
-- One edit to a Verify row. Provenance for post-publication changes.
CREATE TABLE IF NOT EXISTS verify_edits (
    id                    INTEGER PRIMARY KEY AUTOINCREMENT,
    datetime              TEXT NOT NULL,                 -- edited-at
    verify_verified_id      INTEGER NOT NULL REFERENCES verify_verified(id),
    edit_description      TEXT NOT NULL
);
"""


def _ensure_columns(conn: sqlite3.Connection, table: str, columns: list[str]) -> None:
    """Additively add any missing TEXT `columns` to `table` (SQLite ALTER TABLE
    ADD COLUMN is non-destructive). Lets a database created before the *_dt /
    *_raw date columns existed pick them up without a rebuild or data loss."""
    existing = {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}
    for col in columns:
        if col not in existing:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} TEXT")


def init_db(conn: sqlite3.Connection) -> None:
    """Create the five tables if they don't already exist, then migrate older
    databases forward by adding any date columns they predate.

    No-ops on a legacy-vocabulary database: running the DDL there would add five
    empty Source/Screen/Verify tables alongside the Bronze/Silver/Gold ones,
    quietly changing a file we only ever read."""
    names = {r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}
    if "bronze_collected" in names and "source_collected" not in names:
        return
    conn.executescript(SCHEMA)
    # Both extracted/verified tables carry the derived *_dt and verbatim *_raw
    # date partners; add them to any pre-existing table that lacks them.
    date_partner_cols = list(DERIVED_DATE_COLUMNS) + list(RAW_DATE_COLUMNS)
    for table in ("screen_extracted", "verify_verified"):
        _ensure_columns(conn, table, date_partner_cols)
    # Source gained a provenance column (which entry path collected the lead);
    # add it to any database created before that column existed.
    _ensure_columns(conn, "source_collected", ["collected_via"])
    conn.commit()


def table_counts(conn: sqlite3.Connection) -> dict[str, int]:
    """Row counts per table -- the pipeline's at-a-glance status."""
    tables = [
        "source_collected",
        "screen_extracted",
        "screen_check",
        "verify_verified",
        "verify_edits",
    ]
    counts = {}
    for t in tables:
        try:
            counts[t] = conn.execute(f"SELECT COUNT(*) AS n FROM {t}").fetchone()["n"]
        except sqlite3.OperationalError:
            counts[t] = 0
    return counts
