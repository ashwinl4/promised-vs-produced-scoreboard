"""
main.py -- the web interface to the pipeline: a small server-rendered app.

Python (FastAPI) + plain HTML forms. No build step, no framework JS -- every
action is a normal form POST that redirects back, so it works with the browser
alone.

What it is FOR is the review workflow: opening a Screen row beside its sources,
correcting cells, and promoting it to Verify with the reason recorded in
`verify_edits`. Reading the data does not need it -- the CSV exports and
`scoreboard.py verify-list` both do that with nothing installed.

This module creates the app and mounts the three stage modules onto it. The
pages themselves live in source.py, screen.py and verify.py; the stylesheet,
page skeleton and formatting helpers in shared.py.

Run it from the scoreboard directory:

    pip install -r pipeline/requirements.txt
    python3 scoreboard.py webapp --reload
    # then open http://localhost:8100

The `webapp` command runs uvicorn in process, so it does not depend on the `uvicorn` script
being on PATH. By hand it is `python3 -m uvicorn webapp.main:app --port 8100`.
"""

from __future__ import annotations

from typing import Optional

import html
import json
import os
import sys

# webapp/ -> scoreboard/, so `pipeline` imports resolve.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fastapi import FastAPI, Request  # noqa: E402
from fastapi.responses import HTMLResponse, RedirectResponse  # noqa: E402

from pipeline import orchestrate as orch  # noqa: E402
from pipeline.db import (  # noqa: E402
    connect, init_db, is_read_only, set_active_db, table_counts,
)

from webapp import screen as screen_pages, source as source_pages, verify as verify_pages  # noqa: E402
from webapp.shared import _conn, _page, esc  # noqa: E402

app = FastAPI(title="Promised vs. Produced — Source → Verify Pipeline")


@app.on_event("startup")
def _startup():
    conn = connect()
    init_db(conn)          # no-ops on a legacy-vocabulary database
    conn.close()


@app.middleware("http")
async def _guard_read_only(request: Request, call_next):
    """A legacy database is browsable but not writable, so refuse every POST
    except the one that switches database."""
    if request.method == "POST" and request.url.path != "/db" and is_read_only():
        return _page(
            "Read-only",
            '<div class="card"><p>This database uses the old '
            "<b>Bronze/Silver/Gold</b> vocabulary, so it is open for reading "
            "only — writing to it would modify a file kept as the pre-rename "
            "original.</p><p>Switch to a <b>Source/Screen/Verify</b> database "
            "above to make changes.</p></div>",
            "Read-only database — nothing was written",
        )
    return await call_next(request)


def _conn():
    return connect()


@app.post("/db")
async def switch_db(request: Request):
    """Point the running app at another database. Process-local: nothing on disk
    is touched, and the choice lasts until the server restarts."""
    form = await request.form()
    path = form.get("path", "")
    set_active_db(path)
    conn = connect()
    try:
        init_db(conn)      # brings a writable database up to schema; no-op on legacy
    finally:
        conn.close()
    return RedirectResponse(f"/?msg={html.escape(f'Switched to {path}')}", status_code=303)



# --------------------------------------------------------------------------- #
# Dashboard                                                                    #
# --------------------------------------------------------------------------- #

@app.get("/", response_class=HTMLResponse)
def dashboard(request: Request, msg: Optional[str] = None):
    conn = _conn()
    try:
        c = table_counts(conn)
    finally:
        conn.close()
    body = f"""
<div class="stages">
  <div><div class="n">{c['source_collected']}</div>Source<br><small>collected</small></div>
  <div><div class="n">{c['screen_extracted']}</div>Screen<br><small>extracted</small></div>
  <div><div class="n">{c['screen_check']}</div>Screen<br><small>checks</small></div>
  <div><div class="n">{c['verify_verified']}</div>Verify<br><small>verified</small></div>
  <div><div class="n">{c['verify_edits']}</div>Verify<br><small>edits</small></div>
</div>

<h2>Automate all the way to Verify (direct API)</h2>
<div class="card">
  <p>Runs each lead <code>Source → Screen → check</code> via the direct Anthropic
  API (needs <code>ANTHROPIC_API_KEY</code>). Tick auto-promote to also push
  passing rows into Verify — this <b>bypasses the human gate</b> and is meant for
  the convenience/demo path only. No key? Use the per-step Claude Code prompts on
  the Source and Screen pages instead.</p>
  <form method="post" action="/automate">
    <div class="grid2">
      <div><label>How many leads</label><input type="text" name="n" value="1"></div>
      <div><label>Auto-promote tier</label>
        <select name="tier">
          <option value="V1">V1 — checked against one source</option>
          <option value="V2">V2 — two independent sources</option>
        </select></div>
    </div>
    <label><input type="checkbox" name="auto_promote" value="1"> auto-promote passing rows to Verify</label>
    <p><button class="primary" type="submit">Run automation</button></p>
  </form>
</div>
"""
    return _page("Dashboard", body, msg)


@app.post("/automate")
async def do_automate(request: Request):
    form = await request.form()
    try:
        n = max(1, int(form.get("n") or 1))
    except ValueError:
        n = 1
    auto = form.get("auto_promote") == "1"
    tier = form.get("tier") or "V1"
    conn = _conn()
    try:
        results = orch.automate_all(conn, n=n, auto_promote=auto, promote_tier=tier)
    finally:
        conn.close()
    ok = sum(1 for r in results if "error" not in r)
    errs = [r["error"] for r in results if "error" in r]
    msg = f"Automated {ok}/{len(results)} leads."
    if errs:
        msg += " Errors: " + "; ".join(errs[:3])
    return RedirectResponse(f"/?msg={html.escape(msg)}", status_code=303)



# --------------------------------------------------------------------------- #
# The stage pages, defined in their own modules                               #
# --------------------------------------------------------------------------- #

app.include_router(source_pages.router)
app.include_router(screen_pages.router)
app.include_router(verify_pages.router)
