"""
cli.py -- the command-line interface to the medallion pipeline.

This is the initialisation point of the pipeline from a terminal. Every step is
here, each (where the stage allows it) offering a manual OR computational path,
plus an `automate` command that runs a lead all the way to Verify.

Run it from the scoreboard directory. `scoreboard.py` is the documented entry
point and is a thin launcher for this file:

    python3 scoreboard.py --help
    python3 scoreboard.py status

The long form is equivalent, and is what the collection loops call internally:

    python3 -m pipeline.cli status

(Also runnable as `python pipeline/cli.py ...` thanks to the path shim below.)
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys

# --- Allow both `python -m pipeline.cli` and `python pipeline/cli.py` -------- #
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# The operating prompts contain non-ASCII (>=, ~) and the Windows console
# defaults to cp1252; force UTF-8 so `source-prompt` / `screen-prompt` print.
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8")
    except Exception:
        pass

from pipeline import source, screen, verify, orchestrate as orch, llm  # noqa: E402
from pipeline.db import (  # noqa: E402
    DEFAULT_DB, connect, db_path, init_db, table_counts,
)
from pipeline.schema_check import (  # noqa: E402
    V0_COLUMNS,
    DERIVED_DATE_COLUMNS,
    RAW_DATE_COLUMNS,
    all_sectors,
    register_sector,
)
from pipeline.llm import LLMUnavailable  # noqa: E402


# --------------------------------------------------------------------------- #
# Small print helpers                                                          #
# --------------------------------------------------------------------------- #

def _print_row(row, cols):
    for c in cols:
        val = row[c] if c in row.keys() else ""
        print(f"  {c:22} {val if val is not None else ''}")


def _parse_set(pairs: list[str]) -> dict:
    out = {}
    for p in pairs or []:
        if "=" not in p:
            raise SystemExit(f"--set expects col=value, got {p!r}")
        k, v = p.split("=", 1)
        out[k.strip()] = v
    return out


def _report_check(result: dict) -> None:
    print(f"  verdict: {result['result_status']} "
          f"({result['n_errors']} error(s), {result['n_warnings']} warning(s))")
    for issue in result["report"]:
        print(f"    {issue['level']:5} {issue['column']:22} {issue['message']}")


# --------------------------------------------------------------------------- #
# Commands                                                                     #
# --------------------------------------------------------------------------- #

def cmd_initdb(conn, args):
    init_db(conn)
    print(f"initialised database at {conn.execute('PRAGMA database_list').fetchone()[2]}")


def cmd_status(conn, args):
    counts = table_counts(conn)
    print("Pipeline status")
    print("=" * 40)
    print(f"  SOURCE  source_collected  {counts['source_collected']:>4}")
    print(f"  SCREEN  screen_extracted  {counts['screen_extracted']:>4}")
    print(f"          screen_check      {counts['screen_check']:>4}")
    print(f"  VERIFY  verify_verified   {counts['verify_verified']:>4}")
    print(f"          verify_edits      {counts['verify_edits']:>4}")


def cmd_webapp(conn, args):
    # FastAPI and uvicorn are the only dependencies in this project, and only
    # this command needs them. Import here so every other command keeps
    # running on a bare standard library.
    try:
        import uvicorn
    except ImportError:
        raise SystemExit(
            "The web interface needs two packages that the CLI does not:\n"
            "    pip install -r pipeline/requirements.txt\n"
            "Every other command here runs on the standard library alone."
        )

    # uvicorn holds the process until Ctrl-C, so hand the connection back now
    # rather than leaving it open for the life of the server.
    conn.close()

    # flush: uvicorn logs to stderr, and a redirected stdout would otherwise
    # buffer these until exit and print them after the server's own banner.
    print(f"Review interface on http://{args.host}:{args.port}   (Ctrl-C to stop)",
          flush=True)
    print(f"database: {db_path()}", flush=True)
    if args.host not in ("127.0.0.1", "localhost"):
        print(f"warning: {args.host} exposes this to your network, and the pages "
              "have no authentication.", flush=True)

    # The app is named as a string rather than imported. uvicorn resolves it in
    # the worker process, which is what --reload needs, and it keeps this module
    # from importing its sibling webapp package.
    uvicorn.run("webapp.main:app", host=args.host, port=args.port, reload=args.reload)


def _landing(conn, prog: str) -> None:
    """What a bare invocation prints.

    The full --help is around ninety lines, which is the right reference and the
    wrong greeting. This is the greeting: where the data stands, and three doors.
    """
    counts = table_counts(conn)
    published = _published_projects(conn)
    waiting = sum(1 for r in conn.execute("SELECT project FROM screen_extracted")
                  if r["project"] not in published)

    colour = _use_colour()
    bold = (lambda t: f"{_ANSI['bold']}{t}{_ANSI['off']}") if colour else (lambda t: t)
    cyan = (lambda t: f"{_ANSI['cyan']}{t}{_ANSI['off']}") if colour else (lambda t: t)

    print(bold("Promised vs. Produced Scoreboard"))
    print(f"{counts['source_collected']} collected, "
          f"{counts['screen_extracted']} screened, "
          f"{counts['verify_verified']} published, "
          f"{waiting} waiting for review")
    print()

    # Three doors are the right greeting for a database with data in it. An
    # empty one needs the one door that leads somewhere.
    if not counts["screen_extracted"] and not counts["verify_verified"]:
        print("This database is empty. Collect some projects first:")
        print()
        print(f"  N=5 DRY_RUN=1 bash {cyan('collect/all.sh')}"
              "   see the plan")
        print(f"  N=5 bash {cyan('collect/all.sh')}"
              "             do it")
        print()
        print("It runs headless Claude Code workers, so the `claude` CLI has to be")
        print("installed and logged in once.")
    else:
        print("Try one of these:")
        print(f"  {prog} {cyan('verify-list')}     the published data")
        print(f"  {prog} {cyan('review')}          work the queue, one row at a time")
        print(f"  {prog} {cyan('webapp')}          the same, in a browser")
        print(f"  {prog} {cyan('collect')}         add new projects (--dry-run first)")
    print()
    print(f"Every command:  {prog} --help")


def _warn_if_exports_stale() -> None:
    """Warn when the CSVs are older than the database.

    Writes through connect() refresh the CSVs on their own. This catches what
    that cannot: a database edited with raw sqlite3, restored from an older
    copy, or written while SCOREBOARD_NO_AUTOEXPORT was set. Two stat calls, no
    reads, so it costs nothing on every command.
    """
    try:
        if db_path().resolve() != DEFAULT_DB.resolve() or not DEFAULT_DB.exists():
            return
        csvs = list((DEFAULT_DB.parent / "csv_tables").glob("scoreboard_*.csv"))
        if not csvs:
            return
        newest = max(p.stat().st_mtime for p in csvs)
        # A couple of seconds of slack: the export runs just after the write, so
        # equal-second timestamps are the normal case, not a discrepancy.
        if DEFAULT_DB.stat().st_mtime > newest + 2:
            print("note: outputs/scoreboard.db is newer than its CSV exports.\n"
                  "      run `python3 scoreboard.py export` before committing.",
                  file=sys.stderr)
    except OSError:
        pass


# --- Collection ------------------------------------------------------------ #

def cmd_collect(conn, args):
    from pipeline.db import SCOREBOARD_ROOT
    script = SCOREBOARD_ROOT / "collect" / "all.sh"
    if not script.exists():
        raise SystemExit(f"collection script not found: {script}")

    # all.sh reads four knobs from the environment. Everything else it
    # understands (SOURCE_EFFORT, SCREEN_VERBOSE, MODEL, the SOURCE_/SCREEN_
    # prefixes) is left to pass through untouched, so the shell form loses
    # nothing by being wrapped.
    env = dict(os.environ)
    env["N"] = str(args.n)
    env["ONLY"] = args.only
    if args.dry_run:
        env["DRY_RUN"] = "1"
    if args.continue_on_fail:
        env["CONTINUE_ON_FAIL"] = "1"

    # The loop spawns workers that write through this same CLI, and each of
    # those closes its own connection, so the CSV exports stay current without
    # this process doing anything. Hand ours back before replacing the process.
    conn.close()

    # exec rather than subprocess: the script installs its own INT/TERM trap,
    # and replacing the process leaves Ctrl-C behaving exactly as documented
    # instead of racing a Python KeyboardInterrupt against it.
    os.chdir(SCOREBOARD_ROOT)
    os.execvpe("bash", ["bash", str(script)], env)


# --- tools/ commands ------------------------------------------------------- #
#
# These import from tools/ inside the function, never at module load. That keeps
# the CLI free of them at start-up, leaves each script runnable on its own, and
# avoids a cycle: tools/load_csv.py imports this package.

def cmd_export(conn, args):
    from tools.export_tables import export_all, ALL_TABLES
    # export_all opens the database read-only itself, and honours the same
    # SCOREBOARD_DB that --db has already set.
    counts = export_all(out_dir=args.out_dir)
    for stage, n in counts.items():
        print(f"scoreboard_{stage}.csv  <- {ALL_TABLES[stage]}  ({n} rows)")


def cmd_coverage(conn, args):
    from tools import coverage
    # Rebuild the argv coverage.py parses, rather than reaching into its
    # internals, so the measurement stays defined in exactly one place.
    argv = []
    if args.selftest:
        argv.append("--selftest")
    if args.against:
        argv += ["--against", args.against]
    if args.stage:
        argv += ["--stage", args.stage]
    if args.min_capital is not None:
        argv += ["--min-capital", str(args.min_capital)]
    if args.show_covered:
        argv.append("--show-covered")
    raise SystemExit(coverage.main(argv))


# --- Guided review --------------------------------------------------------- #

def _fmt_money(raw) -> str:
    t = str(raw or "").strip()
    return f"${int(t):,}" if t.isdigit() else (t or "(none)")


def _fmt_int(raw) -> str:
    t = str(raw or "").strip()
    return f"{int(t):,}" if t.isdigit() else (t or "(none)")


def _published_projects(conn) -> set:
    """Project names already in Verify. verify_verified is UNIQUE(project), so
    the name is what decides whether a Screen row still needs work."""
    return {r["project"] for r in conn.execute("SELECT project FROM verify_verified")}


def _ask(prompt: str, allowed: str) -> str:
    opts = "/".join(allowed)
    while True:
        try:
            answer = input(f"  {prompt}  [{opts}] ").strip().lower()
        except EOFError:
            print()
            raise SystemExit("stopped, nothing written.")
        if answer in allowed:
            return answer
        print(f"    answer one of {opts}")


def _ask_text(prompt: str) -> str:
    while True:
        try:
            text = input(f"  {prompt}\n  > ").strip()
        except EOFError:
            print()
            raise SystemExit("stopped, nothing written.")
        if text:
            return text
        print("    required: this is recorded on the row as the reason it was published.")


def cmd_review(conn, args):
    # Interactive by definition. The collection loops shell out to this CLI from
    # headless workers, so blocking on input without a terminal would hang them.
    if not sys.stdin.isatty():
        raise SystemExit(
            "review needs a terminal, and stdin is not one.\n"
            "Scripted equivalents: screen-list, screen-show, verify-promote."
        )

    published = _published_projects(conn)
    queue = [r for r in screen.list_extracted(conn, by_capital=True)
             if r["project"] not in published]
    if args.id is not None:
        queue = [r for r in queue if r["id"] == args.id]
        if not queue:
            raise SystemExit(f"screen row #{args.id} is not waiting for review "
                             "(already published, or no such row)")

    # A FAIL blocks promotion, so those rows are not reviewable here.
    blocked, ready = [], []
    for row in queue:
        chk = screen.latest_check(conn, row["id"])
        (blocked if (chk and chk["result_status"] == "FAIL") else ready).append(row)

    if not ready:
        print("Nothing waiting for review.")
        if blocked:
            print(f"{len(blocked)} row(s) are blocked by a FAILing check. "
                  "See screen-check --id N.")
        return

    colour = _use_colour()
    bold = (lambda t: f"{_ANSI['bold']}{t}{_ANSI['off']}") if colour else (lambda t: t)
    cyan = (lambda t: f"{_ANSI['cyan']}{t}{_ANSI['off']}") if colour else (lambda t: t)

    print(bold(f"Review: {len(ready)} row(s) waiting, largest capital first"))
    if blocked:
        print(f"({len(blocked)} more blocked by a FAILing check, not shown)")
    print("Open each row's two links, then answer from what they say.")
    print("y = the source supports it, n = it does not, s = skip, q = stop.")
    print("Nothing is written until you confirm at the end of a row.")

    done = skipped = 0
    try:
        for row in ready:
            sid = row["id"]
            chk = screen.latest_check(conn, sid)
            verdict = chk["result_status"] if chk else "(unchecked)"

            print()
            print("-" * 72)
            print(f"{bold('[screen #' + str(sid) + ']')} {row['project']}")
            print(f"    {row['sector']} - {row['state']} - check={verdict}")
            print()
            facts = [
                ("promised capital", _fmt_money(row["promised_capital_usd"])),
                ("promised jobs", _fmt_int(row["promised_jobs"])),
                ("announced", row["announced"] or "(none)"),
                ("promised output", row["promised_first_output"] or "(none)"),
                ("actual output", row["actual_first_output"] or "(none)"),
            ]
            for label, value in facts:
                print(f"    {label:18} {value}")
            print()
            print(f"    promise source  {cyan(row['promise_source'] or '(none)')}")
            print(f"    status source   {cyan(row['status_source'] or '(none)')}")
            print()

            # Ask only about figures the row actually carries.
            questions = [(label, value) for label, value in facts[:3]
                         if value not in ("(none)", "")]
            action = None          # stays None when every answer is y
            for label, value in questions:
                answer = _ask(f"{label} {value} supported?", "ynsq")
                if answer != "y":
                    action = answer
                    break
            if action == "q":
                break
            if action == "s":
                skipped += 1
                continue
            if action == "n":
                skipped += 1
                print()
                print(f"  Leaving #{sid} unpublished: a figure disagrees with its source.")
                print("  Correct it as you publish, once you know the right value:")
                print(f"      python3 {ENTRY} verify-promote --screen-id {sid} --tier V1 \\")
                print("          --set promised_jobs=<correct> --flag \"...\"")
                continue

            print()
            depth = _ask("checked against one source or two independent ones?", "12q")
            if depth == "q":
                break
            tier = "V1" if depth == "1" else "V2"
            reason = _ask_text("In one sentence, what resolved it?")

            print()
            print(f"  python3 {ENTRY} verify-promote --screen-id {sid} "
                  f"--tier {tier} \\")
            print(f"      --flag \"{reason}\"")
            confirm = _ask("publish this row?", "ynq")
            if confirm == "q":
                break
            if confirm != "y":
                skipped += 1
                print("  not published.")
                continue

            try:
                gid = verify.promote(conn, sid, verification_tier=tier, flag=reason)
            except verify.PromotionBlocked as exc:
                print(f"  blocked: {exc}")
                skipped += 1
                continue
            done += 1
            print(f"  published as verify #{gid} (tier {tier}).")
    except KeyboardInterrupt:
        print()

    remaining = len(ready) - done
    print()
    print(f"Published {done}, left {skipped} for later. {remaining} still waiting.")


# --- Source ---------------------------------------------------------------- #

def cmd_source_add(conn, args):
    if args.json:
        # Ingest a JSON lead (e.g. the object Claude Code handed back).
        lead = json.load(sys.stdin) if args.json == "-" else json.load(open(args.json, encoding="utf-8"))
        bid = source.insert_lead(
            conn,
            promise_source=lead.get("promise_source", ""),
            status_source=lead.get("status_source", ""),
            promised_date_source=lead.get("promised_date_source"),
            summary=lead.get("summary"),
            collected_via=args.via or lead.get("collected_via"),
        )
    else:
        if not (args.promise and args.status):
            raise SystemExit("give --promise and --status, or --json PATH")
        bid = source.insert_lead(
            conn,
            promise_source=args.promise,
            status_source=args.status,
            promised_date_source=args.date,
            summary=args.summary,
            collected_via=args.via or "manual",
        )
    print(f"inserted source_collected #{bid}")


def cmd_source_prompt(conn, args):
    # Print the Source operating prompt to run in Claude Code (web search).
    # No API key needed: run it there, then feed the JSON back with `source-add --json -`.
    print(llm.render_source_prompt(
        avoid_projects=orch.existing_project_names(conn),
        avoid_inflight=orch.inflight_project_hints(conn),
    ))


def cmd_screen_prompt(conn, args):
    lead_row = source.get_lead(conn, args.source_id)
    if lead_row is None:
        raise SystemExit(f"no source lead #{args.source_id}")
    lead = {
        "promise_source": lead_row["promise_source"],
        "status_source": lead_row["status_source"],
        "promised_date_source": lead_row["promised_date_source"],
        "summary": lead_row["summary"],
        "source_collected_id": lead_row["id"],
    }
    print(llm.render_screen_prompt(lead))


def cmd_source_collect(conn, args):
    try:
        bid, lead = orch.run_source_ai(conn)
    except LLMUnavailable as e:
        raise SystemExit(f"AI collect failed: {e}")
    print(f"collected source_collected #{bid}")
    print(json.dumps(lead, indent=2))


def cmd_source_list(conn, args):
    rows = source.list_leads(conn)
    if not rows:
        print("(no source leads)")
    for r in rows:
        via = r["collected_via"]
        print(f"[source #{r['id']}]{f'  via={via}' if via else ''} {r['summary'] or ''}")
        print(f"  promise: {r['promise_source']}")
        print(f"  status : {r['status_source']}")
        if r["promised_date_source"]:
            print(f"  date   : {r['promised_date_source']}")


# --- Screen ---------------------------------------------------------------- #

def cmd_screen_add(conn, args):
    if args.json == "-":
        row = json.load(sys.stdin)
    else:
        with open(args.json, encoding="utf-8") as fh:
            row = json.load(fh)
    sid = screen.insert_extracted(conn, row, source_collected_id=args.source_id)
    print(f"inserted screen_extracted #{sid} (tier forced to P)")


def cmd_screen_extract(conn, args):
    try:
        sid, row = orch.run_screen_ai(conn, args.source_id)
    except LLMUnavailable as e:
        raise SystemExit(f"AI extract failed: {e}")
    print(f"extracted screen_extracted #{sid} from source #{args.source_id}")
    print(json.dumps(row, indent=2))


def cmd_screen_check(conn, args):
    if args.all:
        ids = [r["id"] for r in screen.list_extracted(conn)]
    elif args.id:
        ids = [args.id]
    else:
        raise SystemExit("give --id N or --all")
    for sid in ids:
        result = screen.run_check(conn, sid)
        print(f"[screen #{sid}]")
        _report_check(result)


def cmd_screen_list(conn, args):
    rows = screen.list_extracted(conn, by_capital=getattr(args, "by_capital", False))
    if not rows:
        print("(no screen rows)")
        return
    published = {r["project"] for r in conn.execute(
        "SELECT project FROM verify_verified")}
    for r in rows:
        chk = screen.latest_check(conn, r["id"])
        verdict = chk["result_status"] if chk else "(unchecked)"
        raw = (r["promised_capital_usd"] or "")
        cap = f"${int(raw)/1e9:>6.1f}B" if str(raw).strip().isdigit() else "    n/a"
        mark = "  " if r["project"] in published else "->"   # -> still needs review
        print(f"{mark}[screen #{r['id']}] {cap}  {r['project']}  "
              f"({r['sector']}, {r['state']})  check={verdict}")


def cmd_screen_show(conn, args):
    r = screen.get_extracted(conn, args.id)
    if r is None:
        raise SystemExit(f"no screen row #{args.id}")
    print(f"[screen #{r['id']}] from source #{r['source_collected_id']}")
    _print_row(r, list(V0_COLUMNS) + list(DERIVED_DATE_COLUMNS) + list(RAW_DATE_COLUMNS))
    chk = screen.latest_check(conn, r["id"])
    if chk:
        print(f"  latest check: {chk['result_status']}")


# --- Verify ------------------------------------------------------------------ #

def cmd_verify_promote(conn, args):
    overrides = _parse_set(args.set)
    try:
        gid = verify.promote(
            conn,
            args.screen_id,
            verification_tier=args.tier,
            flag=args.flag,
            overrides=overrides,
            force=args.force,
        )
    except verify.PromotionBlocked as e:
        raise SystemExit(f"promotion blocked: {e}")
    print(f"promoted screen #{args.screen_id} -> verify_verified #{gid} (tier {args.tier})")


def cmd_verify_edit(conn, args):
    changes = _parse_set(args.set)
    verify.edit(conn, args.id, changes, edit_description=args.desc)
    print(f"edited verify_verified #{args.id}; logged in verify_edits")


def cmd_verify_list(conn, args):
    rows = verify.list_verified(conn)
    if not rows:
        counts = table_counts(conn)
        print("The Verify stage is empty -- nothing has been published yet.")
        print()
        if counts["screen_extracted"]:
            print(f"There are {counts['screen_extracted']} row(s) waiting at Screen. Verifying is a")
            print("human step: read a row against its two sources, then publish it.")
            print()
            print("  python3 scoreboard.py screen-list            # find one that PASSed")
            print("  python3 scoreboard.py screen-show --id N     # read it and its sources")
            print("  python3 scoreboard.py verify-promote --screen-id N --tier V1")
        elif counts["source_collected"]:
            print(f"There are {counts['source_collected']} lead(s) at Source but none extracted yet.")
            print("Extract them into Screen rows first:")
            print()
            print("  ADD=5 bash collect/screen.sh")
        else:
            print("The database is empty, so there is nothing to verify. Collect some")
            print("projects first -- verifying assumes rows already exist:")
            print()
            print("  N=5 DRY_RUN=1 bash collect/all.sh   # see the plan")
            print("  N=5 bash collect/all.sh             # collect + extract")
        return
    for r in rows:
        n_edits = len(verify.list_edits(conn, r["id"]))
        print(f"[verify #{r['id']}] {r['project']}  tier={r['verification_tier']}  "
              f"edits={n_edits}")


def cmd_verify_show(conn, args):
    r = verify.get_verified(conn, args.id)
    if r is None:
        raise SystemExit(f"no verify row #{args.id}")
    print(f"[verify #{r['id']}] created {r['created_at']}  last-modified {r['datetime']}")
    _print_row(r, list(V0_COLUMNS) + list(DERIVED_DATE_COLUMNS) + list(RAW_DATE_COLUMNS))
    edits = verify.list_edits(conn, r["id"])
    if edits:
        print("  edit history:")
        for e in edits:
            print(f"    {e['datetime']}  {e['edit_description']}")


# --- Sectors (the extensible vocabulary) ----------------------------------- #

def cmd_sectors_list(conn, args):
    print("Sector vocabulary (base set + runtime registrations):")
    for s in sorted(all_sectors()):
        print(f"  - {s}")


def cmd_sectors_add(conn, args):
    if register_sector(args.name):
        print(f"registered new sector: {args.name}")
    else:
        print(f"sector {args.name!r} is blank or already known -- nothing added")


# --- Filter (explore thresholds) ------------------------------------------- #

def cmd_filter(conn, args):
    rows = orch.filter_by_thresholds(
        conn, capital_min=args.capital, jobs_min=args.jobs, op=args.op, stage=args.stage
    )
    combiner = "AND" if args.op.upper() == "AND" else "OR"
    print(f"Explore-filter on {args.stage}_verified"
          if args.stage == "verify" else f"Explore-filter on {args.stage}_extracted")
    print(f"  capital >= ${int(args.capital):,}  {combiner}  jobs >= {int(args.jobs):,}")
    print("=" * 64)
    if not rows:
        print("(no matching rows)")
    for r in rows:
        cap = r["promised_capital_usd"]
        jobs = r["promised_jobs"]
        cap_s = f"${cap:,}" if cap is not None else "—"
        jobs_s = f"{jobs:,}" if jobs is not None else "—"
        print(f"[{args.stage} #{r['id']}] {r['project']:<32} capital={cap_s:>16}  jobs={jobs_s}")
    print(f"\n{len(rows)} row(s) match.")


# --- Automate -------------------------------------------------------------- #

def cmd_automate(conn, args):
    results = orch.automate_all(
        conn, n=args.n, auto_promote=args.auto_promote, promote_tier=args.tier
    )
    for i, res in enumerate(results):
        print(f"--- lead {i + 1} ---")
        if "error" in res:
            print(f"  ERROR: {res['error']}")
            continue
        print(f"  source #{res['source_id']} -> screen #{res['screen_id']}  "
              f"check={res['check']['result_status']}")
        if "verify_id" in res:
            print(f"  verify: {res.get('verify_note')}  "
                  + (f"(#{res['verify_id']})" if res["verify_id"] else ""))


# --------------------------------------------------------------------------- #
# Argument parsing                                                            #
# --------------------------------------------------------------------------- #

def _prog_name() -> str:
    """What the user actually typed.

    The usage line must never name a program nobody invoked. `scoreboard.py` is
    the documented entry point; `python3 -m ...cli` is the equivalent long form
    and should say so rather than claiming to be `cli.py`.
    """
    base = os.path.basename(sys.argv[0] or "")
    if base in ("", "cli.py", "__main__.py"):
        return "python3 -m pipeline.cli"
    return f"python3 {base}"


# Examples show the documented entry point rather than whatever the caller
# typed: `python3 -m pipeline.cli` is 45 characters and
# pushes every example line past the width of a terminal. The usage line above
# already says what you invoked.
ENTRY = "scoreboard.py"

# Marks a heading for the colouriser. Paired, and stripped when colour is off,
# so the plain text is unchanged. \x01 cannot appear in argparse output.
_H = "\x01"


# --------------------------------------------------------------------------- #
# Colour                                                                       #
# --------------------------------------------------------------------------- #

_ANSI = {
    "bold": "\033[1m",
    "dim": "\033[2m",
    "cyan": "\033[36m",
    "green": "\033[32m",
    "yellow": "\033[33m",
    "red": "\033[31m",
    "off": "\033[0m",
}


def _use_colour(stream=None) -> bool:
    """Colour only a real terminal, and honour the usual environment switches.

    NO_COLOR is the cross-tool convention (no-color.org); CLICOLOR_FORCE is
    what lets the test below check the escapes without a tty.
    """
    if os.environ.get("NO_COLOR"):
        return False
    if os.environ.get("CLICOLOR_FORCE"):
        return True
    if os.environ.get("TERM") in ("dumb", ""):
        return False
    stream = stream or sys.stdout
    return bool(getattr(stream, "isatty", lambda: False)())


def _paint(text: str, colour: bool) -> str:
    """Colour already-rendered help text.

    Applied after argparse has laid the text out, never before: argparse sizes
    its columns with len(), which counts escape bytes, so colouring earlier
    would shift every alignment by the width of the codes.
    """
    if not colour:
        return text.replace(_H, "")

    c = _ANSI

    def wrap(name, s):
        return f"{c[name]}{s}{c['off']}"

    out = []
    for line in text.split("\n"):
        # Headings, marked in the epilog with _H ... _H.
        if _H in line:
            parts = line.split(_H)
            line = "".join(
                wrap("bold", seg) if i % 2 else seg for i, seg in enumerate(parts)
            )
            out.append(line)
            continue

        # argparse's own section headings.
        if re.match(r"^(usage:|positional arguments:|optional arguments:|options:)", line):
            head = line.split(":", 1)
            line = wrap("bold", head[0] + ":") + (head[1] if len(head) > 1 else "")
            out.append(line)
            continue

        # A subcommand in the command list: four spaces, a name, two spaces.
        m = re.match(r"^(    )([a-z][a-z0-9-]*)(\s\s)", line)
        if m:
            line = m.group(1) + wrap("cyan", m.group(2)) + line[m.end(2):]

        # The entry point where it opens an example line.
        line = re.sub(r"(?<![\w.-])(" + re.escape(ENTRY) + r")\b",
                      lambda m: wrap("cyan", m.group(1)), line)

        # The path tags already carried by the per-command help strings.
        line = re.sub(r"\[(manual|prompt|API|Claude Code|human gate)\]",
                      lambda m: wrap("yellow", m.group(0)), line)

        # The three check verdicts, wherever they are mentioned.
        line = re.sub(r"\bFAIL\b", lambda m: wrap("red", m.group(0)), line)
        line = re.sub(r"\b(PASS|CLEAN)\b", lambda m: wrap("green", m.group(0)), line)

        out.append(line)
    return "\n".join(out)


class _Parser(argparse.ArgumentParser):
    """An ArgumentParser that colours its help on the way out."""

    def format_help(self):
        return _paint(super().format_help(), _use_colour())

    def format_usage(self):
        return _paint(super().format_usage(), _use_colour(sys.stderr))


def _epilog(prog: str) -> str:
    """The top-level epilog: what a flat list of 20 names cannot say."""
    return f"""\
{_H}the three stages{_H}
  SOURCE   the two links, collected           AI or human
  SCREEN   the 17-column row and a check      AI or human, then the checker
  VERIFY   the published row                  human only

{_H}examples{_H}  (written as `{ENTRY}`; the `-m` form takes the same arguments)

  {_H}read the data (nothing here writes){_H}
    {ENTRY} status                        row counts per stage
    {ENTRY} verify-list                   the published Scoreboard
    {ENTRY} verify-show --id 6            one row and its edits
    {ENTRY} screen-list --by-capital      the review queue
    {ENTRY} screen-show --id 42           a row and its two sources
    {ENTRY} sectors-list                  the sector vocabulary
    {ENTRY} filter --capital 1000000000   rows over $1B
    {ENTRY} filter --jobs 2000 --capital 5000000000 --op OR
    {ENTRY} filter --stage screen --jobs 1000

  {_H}review and publish (the human gate){_H}
    {ENTRY} screen-list --by-capital      -> marks an unverified row
    {ENTRY} screen-show --id 42           open both links and read them
    {ENTRY} verify-promote --screen-id 42 --tier V1 \\
        --flag "Resolved: both sources agree on the announced date."

    V1 = confirmed against one source
    V2 = confirmed against two independent sources

  {_H}work the queue, guided{_H}
    {ENTRY} review                        one row at a time, prompted
    {ENTRY} review --id 42                just that row

  {_H}review in the browser{_H}
    {ENTRY} webapp                        http://127.0.0.1:8100
    {ENTRY} --db /tmp/try.db webapp       the same, against a copy

  {_H}fix a cell while publishing it{_H}
    {ENTRY} verify-promote --screen-id 42 --tier V2 \\
        --set promised_jobs=1200 --set state=AZ \\
        --flag "Job count corrected; the later filing supersedes."

  {_H}change a published row (logged; --desc required){_H}
    {ENTRY} verify-edit --id 6 \\
        --set actual_first_output=2026-03 \\
        --desc "Q1 2026 earnings call confirmed first output."
    {ENTRY} verify-show --id 6            read the edit back

  {_H}add data by hand{_H}
    {ENTRY} source-add --promise https://example.com/announced \\
        --status https://example.com/latest --summary "Acme fab, AZ"
    cat lead.json | {ENTRY} source-add --json -
    {ENTRY} screen-add --json row.json --source-id 12
    {ENTRY} screen-check --id 57          a FAIL blocks promotion
    {ENTRY} screen-check --all            recheck every Screen row

  {_H}collect by pasting a prompt (no key, any assistant){_H}
    {ENTRY} source-prompt                 paste it anywhere that
    {ENTRY} screen-prompt --source-id 12  can search the web, then
                                          source-add / screen-add it back

  {_H}collect over the API (needs ANTHROPIC_API_KEY){_H}
    {ENTRY} source-collect                one new lead from the web
    {ENTRY} screen-extract --source-id 12 that lead into a row
    {ENTRY} automate --n 5                five leads, Source to Screen

  {_H}collect automatically (the usual path; needs the claude CLI){_H}
    {ENTRY} collect --n 5 --dry-run       the plan, spending nothing
    {ENTRY} collect --n 10                both stages, 10 rows each
    {ENTRY} collect --only screen --n 5   one stage

  {_H}try things on a copy{_H}
    cp outputs/scoreboard.db /tmp/try.db
    {ENTRY} --db /tmp/try.db automate --n 3 --auto-promote

  {_H}get the data out, and measure it{_H}
    {ENTRY} export                        writes outputs/csv_tables/
    {ENTRY} coverage --against ref.csv    recall against a known list
    {ENTRY} coverage --selftest           check the matcher, no database

  {_H}scripts that are not part of the normal flow{_H}
    python3 tools/load_csv.py --csv rows.csv --dry-run   bulk import
    python3 tools/gather.py --n-source 5 --dry-run       batch API collection
    tools/README.md                                      what each one does

{_H}the database{_H}
  outputs/scoreboard.db unless overridden by --db PATH or $SCOREBOARD_DB.

  --db is a global flag and goes before the command:
      {ENTRY} --db other.db status    correct
      {ENTRY} status --db other.db    error

  Every command creates the five tables when they are missing. A mistyped
  path therefore opens a new empty database and reports zeros. If the counts
  look wrong, check the path.

Per-command help, with its own examples:  {prog} <command> --help
"""


def _command_examples() -> dict:
    """Per-command epilogs, for commands whose flags are not self-evident.

    Attached to the subparsers in one pass at the end of build_parser, so
    adding an entry here is the whole change.
    """
    return {
        "initdb": f"""{_H}the five tables{_H}
  Three stages, five tables: Screen and Verify each keep an audit table
  beside the data.

  source_collected    SOURCE  one lead: the two source links, an optional
                              date link, a summary, and how it was found.
                              No figures yet.
  screen_extracted    SCREEN  one extracted project row: the 17 columns,
                              plus each date's resolved _dt and verbatim
                              _raw partner. Always tier P.
  screen_check        SCREEN  one checker run over one row above: FAIL,
                              PASS or CLEAN, the counts, and the report.
                              Append-only, so rechecks are all kept.
  verify_verified     VERIFY  one published row. One row per project,
                              tier V1 or V2, never P.
  verify_edits        VERIFY  one change to a published row, with the
                              reason --desc recorded.

  Full column reference: docs/schema.md

{_H}you rarely need this{_H}
  Every command creates these tables when they are missing, so the usual
  way to get them is to run any command at all. Reach for initdb when you
  want a fresh database somewhere specific:

  {ENTRY} --db /tmp/new.db initdb
""",
        "export": f"""{_H}examples{_H}
  {ENTRY} export                       writes outputs/csv_tables/
  {ENTRY} export --out-dir /tmp/csv    somewhere else
  {ENTRY} --db /tmp/other.db export    from another database

  One CSV per table. Three hold the Scoreboard: scoreboard_source.csv,
  scoreboard_screen.csv, scoreboard_verify.csv. Two hold the audit trail:
  scoreboard_screen_check.csv, scoreboard_verify_edits.csv.

  Every column of each table, in table order, sorted by id, with NULLs as
  empty cells.

  The audit trail is exported because scoreboard.db is committed and git
  cannot diff a binary. Without it a commit could add fifty check runs, or
  a correction to a published figure, and show only that the database
  changed.

  The database is opened read-only, so an export cannot alter or lock it.
  tools/export_tables.py is the same code and still runs on its own.
""",
        "coverage": f"""{_H}examples{_H}
  {ENTRY} coverage --against ref.csv
  {ENTRY} coverage --against ref.csv --min-capital 1000000000
  {ENTRY} coverage --against ref.csv --stage verify
  {ENTRY} coverage --selftest          needs no database

{_H}what it measures{_H}
  How much of a known list of projects the Scoreboard has. Nobody publishes
  the true universe of US manufacturing projects, so the denominator has to
  come from a list you can enumerate. The reference CSV needs `project` and
  `state`; add `promised_capital_usd` to use --min-capital.

  --stage screen (the default) measures what has been collected,
  --stage verify measures what has been published.

{_H}it does not compare names{_H}
  The same project appears under different names across sources. Matching
  is gated on state first, then scored on name overlap within that state,
  which is what separates two TSMC entries for one Arizona site from two
  Nucor mills in different states.

  Results come back in three buckets: covered, missing, and ambiguous.
  An ambiguous row is never counted as covered, and recall is reported as
  a range whenever any exist. --selftest checks the matcher against pairs
  whose answer is known by hand, and should stay at 10/10.
""",
        "collect": f"""{_H}examples{_H}
  {ENTRY} collect --n 5 --dry-run     the plan, spending nothing
  {ENTRY} collect --n 10              both stages, 10 rows each
  {ENTRY} collect --only source       find new projects, do not extract
  {ENTRY} collect --only screen       extract leads already collected

{_H}this one spends money{_H}
  Each iteration starts a fresh headless Claude Code worker that searches
  the web and writes through this CLI. Each stage runs one worker per row,
  so --n 10 across both stages is twenty or more worker calls. Start small,
  and use --dry-run first.

  Needs the `claude` CLI installed and logged in once: run `claude`, then
  /login. It checks before starting rather than failing partway through.

{_H}stopping and resuming{_H}
  Ctrl-C is safe. Each iteration is a separate stateless worker, so nothing
  is half-written, and re-running continues where you left off because the
  database de-duplicates.

{_H}the other three ways in{_H}
  This is the only path that needs Claude Code. source-prompt prints the
  same instructions for any assistant that can search the web, source-add
  takes a row you wrote yourself, and automate uses the Anthropic API
  directly. All four write the same rows through the same checks.

  Every other knob (SOURCE_EFFORT, SCREEN_VERBOSE, MODEL, the SOURCE_ and
  SCREEN_ prefixes) still passes through the environment: docs/collecting.md
""",
        "review": f"""{_H}examples{_H}
  {ENTRY} review                    every row waiting, largest first
  {ENTRY} review --id 42            just that row
  {ENTRY} --db /tmp/try.db review   practise against a copy

{_H}what it does{_H}
  Walks the rows that are extracted but not published, largest promised
  capital first. For each one it prints the figures and the two source
  links, then asks whether the sources support each figure, how deeply you
  checked, and what resolved it. It prints the equivalent verify-promote
  command before writing anything.

  It prints the links rather than opening them, so you decide when to look.

  y = the source supports it        n = it does not
  s = skip this row                 q = stop

{_H}what it will not do{_H}
  Answer for you. The check has already confirmed the row is well shaped
  and in range; what it cannot do is read the sources, which is the whole
  reason a person publishes rows rather than the pipeline. Answering n
  leaves the row unpublished and prints the command that corrects the cell.

  Rows whose check FAILs are excluded, because a FAIL blocks promotion.
  Fix those with screen-check --id N first.

  It needs a terminal. Under a pipe or in a script, use screen-list,
  screen-show and verify-promote instead.
""",
        "webapp": f"""{_H}examples{_H}
  {ENTRY} webapp                      http://127.0.0.1:8100
  {ENTRY} webapp --port 9000
  {ENTRY} webapp --reload             restart when the source changes
  {ENTRY} --db /tmp/try.db webapp     review a copy, not the real data

  The browser interface exists for the review workflow: a Screen row shown
  beside its two sources, so you can correct cells and promote it with the
  reason recorded. To read the data, the CLI and the CSV exports are faster
  and need nothing installed.

{_H}what it needs{_H}
  FastAPI and uvicorn, the only dependencies in this project:
  pip install -r pipeline/requirements.txt

  This runs uvicorn in process, so it works whether or not the `uvicorn`
  script is on your PATH. The equivalent by hand, from this directory:
  python3 -m uvicorn webapp.main:app --port 8100

{_H}binding to anything but localhost{_H}
  The default is 127.0.0.1, reachable only from this machine. --host
  0.0.0.0 puts it on your network, and these pages have no login and can
  edit published rows. Use it only on a network you trust.
""",
        "source-add": f"""{_H}examples{_H}
  {ENTRY} source-add --promise https://example.com/announced \\
      --status https://example.com/latest --summary "Acme fab, AZ"

  Ingest a lead an assistant handed back, from a file or from stdin:
  {ENTRY} source-add --json lead.json --via prompt1
  cat lead.json | {ENTRY} source-add --json -

  --via records where the lead came from. Entry with --promise and --status
  defaults to 'manual'.
""",
        "screen-add": f"""{_H}examples{_H}
  {ENTRY} screen-add --json row.json --source-id 12

  --source-id links the row back to its Source lead. Leave it out and the
  row carries no recorded origin.

  Adding a row does not check it. Run screen-check afterwards.
""",
        "screen-check": f"""{_H}examples{_H}
  {ENTRY} screen-check --id 57    one row
  {ENTRY} screen-check --all      every Screen row

  FAIL blocks promotion. PASS means the row is shaped correctly and its
  values are in range. CLEAN means PASS with no warnings either.

  The check never opens the source links. Reading those happens at
  verify-promote, by a person.

  Every run appends to screen_check, so the history is kept.
""",
        "screen-list": f"""{_H}examples{_H}
  {ENTRY} screen-list             insertion order
  {ENTRY} screen-list --by-capital

  --by-capital sorts by promised capital, biggest first. That is the order
  to verify in, so that partial coverage still covers the largest projects.

  A leading -> marks a row that is not yet verified.
""",
        "verify-promote": f"""{_H}examples{_H}
  {ENTRY} verify-promote --screen-id 42 --tier V1 \\
      --flag "Resolved: both sources agree on the announced date."

  Fix a cell while publishing it, rather than editing afterwards:
  {ENTRY} verify-promote --screen-id 42 --tier V2 \\
      --set promised_jobs=1200 --set state=AZ \\
      --flag "Job count corrected; the later filing supersedes."

  --tier V1  confirmed against one source
  --tier V2  confirmed against two independent sources
  --force    promote despite a FAIL. The override is recorded.

  Publishing the same project twice is refused.
""",
        "verify-edit": f"""{_H}examples{_H}
  {ENTRY} verify-edit --id 6 \\
      --set actual_first_output=2026-03 \\
      --desc "Q1 2026 earnings call confirmed first output."

  --set is repeatable. --desc is required: every change to a published row
  goes into verify_edits with its reason. Read it back with verify-show.
""",
        "filter": f"""{_H}examples{_H}
  {ENTRY} filter --capital 1000000000     rows over $1B
  {ENTRY} filter --jobs 2000              2000+ promised jobs
  {ENTRY} filter --jobs 2000 --capital 5000000000 --op OR
  {ENTRY} filter --stage screen --capital 500000000

  These flags query rows already in the database. What qualifies a project
  in the first place ($100M or 200 jobs) is set in schema.py.

  --op AND (the default) requires both thresholds; OR requires either.
  --stage screen queries rows before publication.
""",
        "automate": f"""{_H}examples{_H}
  {ENTRY} automate --n 5      five leads, Source to Screen, checked
  {ENTRY} --db /tmp/try.db automate --n 3 --auto-promote

  Needs ANTHROPIC_API_KEY. The shell loops in
  collect/ are the usual path and need no key.

  --auto-promote writes rows into Verify without anyone reading them.
  Point --db at a copy first.
""",
        "source-prompt": f"""{_H}examples{_H}
  {ENTRY} source-prompt

  Prints the operating prompt for finding a new project. Paste it into an
  assistant that can search the web, then file what comes back:
  {ENTRY} source-add --json lead.json --via prompt1

  Needs no API key. The [API] equivalent is source-collect.
""",
        "screen-prompt": f"""{_H}examples{_H}
  {ENTRY} screen-prompt --source-id 12

  Prints the extraction prompt for one Source lead, with its links filled
  in. Paste it into an assistant that can open them, then file the row:
  {ENTRY} screen-add --json row.json --source-id 12
  {ENTRY} screen-check --id 57

  Needs no API key. The [API] equivalent is screen-extract.
""",
        "sectors-add": f"""{_H}examples{_H}
  {ENTRY} sectors-add "Cement"

  Registers a sector for a manufacturing project that fits none of the ten.
  Prefer this to filing the row under Other. If Other is filling up, the
  missing sector belongs here.
""",
    }


class _HelpFormatter(argparse.RawDescriptionHelpFormatter):
    """Keeps the epilog verbatim, and stops the command list wrapping.

    argparse sizes the help column from the longest argument name, but it
    measures subcommand names *without* the extra level of indentation it then
    renders them at. The 14-character names (`verify-promote`, `screen-extract`)
    therefore overflow by exactly that indent and wrap onto a line of their own,
    which doubles the height of the list. Re-measure them with the indent
    included.
    """

    def __init__(self, prog, **kwargs):
        kwargs.setdefault("max_help_position", 28)
        super().__init__(prog, **kwargs)

    def add_argument(self, action):
        super().add_argument(action)
        choices = getattr(action, "choices", None)
        if choices and hasattr(action, "_get_subactions"):
            longest = max((len(str(c)) for c in choices), default=0)
            self._action_max_length = max(
                self._action_max_length,
                longest + self._current_indent + 2,
            )


def build_parser() -> argparse.ArgumentParser:
    prog = _prog_name()
    p = _Parser(
        prog=prog,
        description="Command line for the Promised vs. Produced Scoreboard.",
        epilog=_epilog(prog),
        formatter_class=_HelpFormatter,
    )
    p.add_argument("--db", metavar="PATH",
                   help="SQLite database to use (overrides $SCOREBOARD_DB). "
                        "Must come before the command.")
    # metavar keeps the 20-name brace list out of the usage line and the
    # positional header; the commands are still listed once, below.
    sub = p.add_subparsers(dest="command", metavar="<command>")

    sub.add_parser("initdb", help="create an empty Scoreboard database") \
        .set_defaults(fn=cmd_initdb)
    sub.add_parser("status", help="row counts per stage").set_defaults(fn=cmd_status)

    s = sub.add_parser("collect", help="[Claude Code] find new projects and extract them")
    s.add_argument("--n", type=int, default=10,
                   help="rows to add at each stage (default 10)")
    s.add_argument("--only", choices=["source", "screen", "both"], default="both",
                   help="run one stage instead of both (default both)")
    s.add_argument("--dry-run", action="store_true",
                   help="print the plan and call nothing")
    s.add_argument("--continue-on-fail", action="store_true",
                   help="keep going when an iteration fails")
    s.set_defaults(fn=cmd_collect)

    s = sub.add_parser("review", help="work through the review queue, guided")
    s.add_argument("--id", type=int,
                   help="review one screen row rather than the whole queue")
    s.set_defaults(fn=cmd_review)

    s = sub.add_parser("webapp", help="run the browser review interface")
    s.add_argument("--port", type=int, default=8100, help="port to listen on (default 8100)")
    s.add_argument("--host", default="127.0.0.1",
                   help="address to bind (default 127.0.0.1, this machine only)")
    s.add_argument("--reload", action="store_true",
                   help="restart when the source changes, for editing the app")
    s.set_defaults(fn=cmd_webapp)

    # Source
    s = sub.add_parser("source-add", help="[manual] add a Source lead (flags or --json)")
    s.add_argument("--promise", help="promise_source URL")
    s.add_argument("--status", help="status_source URL")
    s.add_argument("--date", help="promised_date_source (optional)")
    s.add_argument("--summary", help="context summary (optional)")
    s.add_argument("--json", help="ingest a JSON lead instead (path, or - for stdin)")
    s.add_argument("--via", help="provenance label for collected_via (e.g. prompt1, prompt2); "
                                 "manual --promise/--status entry defaults to 'manual'")
    s.set_defaults(fn=cmd_source_add)

    s = sub.add_parser("source-prompt",
                       help="[prompt] print the Source prompt, for any web-search assistant")
    s.set_defaults(fn=cmd_source_prompt)

    sub.add_parser("source-collect", help="[API] collect one new Source lead from the web") \
        .set_defaults(fn=cmd_source_collect)
    sub.add_parser("source-list", help="list Source leads").set_defaults(fn=cmd_source_list)

    # Screen
    s = sub.add_parser("screen-add", help="[manual] add a Screen row from JSON")
    s.add_argument("--json", required=True, help="path to a JSON file, or - for stdin")
    s.add_argument("--source-id", type=int, help="originating source lead id (lineage)")
    s.set_defaults(fn=cmd_screen_add)

    s = sub.add_parser("screen-prompt",
                       help="[prompt] print the Screen prompt for a Source lead")
    s.add_argument("--source-id", type=int, required=True,
                   help="the source_collected id to render the prompt for")
    s.set_defaults(fn=cmd_screen_prompt)

    s = sub.add_parser("screen-extract", help="[API] extract a Screen row from a Source lead")
    s.add_argument("--source-id", type=int, required=True,
                   help="the source_collected id to extract")
    s.set_defaults(fn=cmd_screen_extract)

    s = sub.add_parser("screen-check", help="run the deterministic check (screen_check)")
    s.add_argument("--id", type=int, help="a screen_extracted id")
    s.add_argument("--all", action="store_true", help="check every screen row")
    s.set_defaults(fn=cmd_screen_check)

    s = sub.add_parser("screen-list", help="list Screen rows + check status")
    s.add_argument("--by-capital", action="store_true",
                   help="sort by promised capital, biggest first")
    s.set_defaults(fn=cmd_screen_list)
    s = sub.add_parser("screen-show", help="show one Screen row")
    s.add_argument("--id", type=int, required=True,
                   help="a screen_extracted id (see screen-list)")
    s.set_defaults(fn=cmd_screen_show)

    # Verify
    s = sub.add_parser("verify-promote", help="[human gate] promote a Screen row to Verify")
    s.add_argument("--screen-id", type=int, required=True,
                   help="the screen_extracted id to publish (see screen-list)")
    s.add_argument("--tier", default="V1",
                   help="V1 = confirmed against one source, V2 = confirmed "
                        "against two independent sources (default V1)")
    s.add_argument("--flag", help="resolution record (default: auto-generated)")
    s.add_argument("--set", action="append", metavar="col=value",
                   help="override a cell at promotion time (repeatable)")
    s.add_argument("--force", action="store_true", help="promote even if the check FAILs")
    s.set_defaults(fn=cmd_verify_promote)

    s = sub.add_parser("verify-edit", help="edit a Verify row (logged in verify_edits)")
    s.add_argument("--id", type=int, required=True,
                   help="a verify_verified id (see verify-list)")
    s.add_argument("--set", action="append", metavar="col=value", required=True,
                   help="a cell to change (repeatable)")
    s.add_argument("--desc", required=True, help="edit_description (provenance)")
    s.set_defaults(fn=cmd_verify_edit)

    sub.add_parser("verify-list", help="list Verify rows").set_defaults(fn=cmd_verify_list)
    s = sub.add_parser("verify-show", help="show one Verify row + edit history")
    s.add_argument("--id", type=int, required=True,
                   help="a verify_verified id (see verify-list)")
    s.set_defaults(fn=cmd_verify_show)

    # tools/ commands
    s = sub.add_parser("export", help="write the three stages to flat CSVs")
    s.add_argument("--out-dir", help="where to write them (default outputs/csv_tables/)")
    s.set_defaults(fn=cmd_export)

    s = sub.add_parser("coverage", help="recall against a reference list of projects")
    s.add_argument("--against", metavar="CSV",
                   help="reference list; needs at least `project` and `state` columns")
    s.add_argument("--stage", choices=["screen", "verify"], default="screen",
                   help="measure what is collected (screen) or published (verify)")
    s.add_argument("--min-capital", type=int, metavar="USD",
                   help="only score reference projects at or above this, "
                        "e.g. 1000000000 for the Phase 1 line")
    s.add_argument("--show-covered", action="store_true", help="also list the matches")
    s.add_argument("--selftest", action="store_true",
                   help="check the matcher against known pairs and exit")
    s.set_defaults(fn=cmd_coverage)

    # Sectors (the extensible vocabulary)
    sub.add_parser("sectors-list", help="list the sector vocabulary (base + registered)") \
        .set_defaults(fn=cmd_sectors_list)
    s = sub.add_parser("sectors-add",
                       help="register a new sector at runtime (extends the vocabulary)")
    s.add_argument("name", help="the new manufacturing sector name, e.g. 'Cement'")
    s.set_defaults(fn=cmd_sectors_add)

    # Filter (explore thresholds beyond the fixed floor)
    s = sub.add_parser("filter",
                       help="explore capital/jobs thresholds via a SQL query (AND/OR)")
    s.add_argument("--capital", type=int, default=0,
                   help="minimum promised_capital_usd, e.g. 1000000000 for $1B")
    s.add_argument("--jobs", type=int, default=0, help="minimum promised_jobs, e.g. 2000")
    s.add_argument("--op", choices=["AND", "OR"], default="AND",
                   help="how to combine the two thresholds (default AND)")
    s.add_argument("--stage", choices=["verify", "screen"], default="verify",
                   help="which stage's table to query (default verify)")
    # Former spelling. Kept working, hidden from help, SUPPRESS so it never
    # overrides the --stage default when it is not given.
    s.add_argument("--layer", dest="stage", choices=["verify", "screen"],
                   default=argparse.SUPPRESS, help=argparse.SUPPRESS)
    s.set_defaults(fn=cmd_filter)

    # Automate
    s = sub.add_parser("automate", help="run leads Source->Screen->check [->Verify]")
    s.add_argument("--n", type=int, default=1, help="number of leads (default 1)")
    s.add_argument("--auto-promote", action="store_true",
                   help="also promote passing rows to Verify (bypasses the human gate)")
    s.add_argument("--tier", default="V1", help="tier for auto-promotion (default V1)")
    s.set_defaults(fn=cmd_automate)

    # Attach the per-command examples. Done in one pass so that adding an
    # example above is the whole change, and a typo'd command name is caught
    # here rather than silently doing nothing.
    for name, text in _command_examples().items():
        if name not in sub.choices:
            raise AssertionError(f"example for unknown command {name!r}")
        sub.choices[name].epilog = text
        sub.choices[name].formatter_class = _HelpFormatter

    return p


def main(argv=None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.db:
        os.environ["SCOREBOARD_DB"] = args.db
    conn = connect()
    # Every command except a bare initdb assumes tables exist; be forgiving.
    init_db(conn)
    _warn_if_exports_stale()
    try:
        # No command is a request for orientation, not a usage error. It runs
        # after connect() because the greeting reports where the data stands.
        if getattr(args, "fn", None) is None:
            _landing(conn, _prog_name())
            return 0
        args.fn(conn, args)
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
