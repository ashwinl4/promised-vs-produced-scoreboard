# webapp

The browser interface. It is the friendliest way to do the **review workflow**:
open a Screen row beside its two sources, correct cells, and promote it to Verify
with the reason recorded.

If you only want to *read* the data, you do not need this. Two other routes need
nothing installed at all — see [the three ways to see the
Scoreboard](../README.md#see-the-scoreboard) in the main README.

**Contents**

- [Run it](#run-it)
- [What each file holds](#what-each-file-holds)
- [How the pieces fit](#how-the-pieces-fit)

## Run it

The rest of the Scoreboard runs on the standard library. This is the one part
that needs packages installed: FastAPI, uvicorn and python-multipart.

From the parent directory (`scoreboard/`), not from here:

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r pipeline/requirements.txt
python3 scoreboard.py webapp
```

Then open <http://localhost:8100>. Add `--reload` while editing, `--port` to
move it, and `--db PATH` to review a copy rather than the real database. Leave
the environment later with `deactivate`; `.venv/` is gitignored.

### On the virtual environment

It is a recommendation, not a requirement. Installing into your user site works
too, and `scoreboard.py webapp` finds the packages either way, because it calls
uvicorn in process rather than shelling out to the `uvicorn` script. That script
is the usual source of `command not found: uvicorn`: `pip install --user` puts it
somewhere like `~/Library/Python/3.9/bin`, which is often not on `PATH` even
though the library imported perfectly well.

What the environment buys you is keeping three packages out of the system
interpreter, which on macOS is Apple's and worth leaving alone.

By hand, without the command, still from the parent directory:

```bash
python3 -m uvicorn webapp.main:app --port 8100
```

Server-rendered HTML and plain form posts. No build step and no JavaScript
framework, so it works with the browser alone.

## What each file holds

| File | Holds |
|---|---|
| `main.py` | creates the app, mounts the three stage modules, and serves the dashboard plus the database picker |
| `shared.py` | the stylesheet, the page skeleton, the database picker bar, and the small formatting helpers every page uses |
| `source.py` | the Source pages: the lead list, the rendered collection prompt, and the three ways to add a lead |
| `screen.py` | the Screen pages: the row list, the extraction prompt, the checker, and the inspect view where a row is read against its sources |
| `verify.py` | the Verify pages: the published rows, the capital/jobs filter, the sector vocabulary, and the edit form |

## How the pieces fit

Only `main.py` creates a server. The three stage modules each collect their pages
on a `router`, which `main.py` mounts. So none of them runs on its own, and the
dependency runs one way with no cycles:

```
shared.py  <-  source.py / screen.py / verify.py  <-  main.py
```

The pipeline itself lives in
[`../pipeline/`](../pipeline/).
This directory only renders it; every write goes through the same functions the
CLI calls, so the two interfaces cannot drift.

Promotion to Verify is a human gate here as everywhere else, and every edit is
written to `verify_edits` with a required reason.
