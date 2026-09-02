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

From the parent directory (`scoreboard/`), not from here:

```bash
pip install -r pipeline/requirements.txt
python3 scoreboard.py webapp
```

Then open <http://localhost:8100>. Add `--reload` while editing, `--port` to
move it, and `--db PATH` to review a copy rather than the real database.

The `webapp` command runs uvicorn in process. That matters because `pip install` puts the
`uvicorn` script somewhere that is often not on `PATH`, which is where
`command not found: uvicorn` comes from. The equivalent by hand, still from the
parent directory:

```bash
python3 -m uvicorn webapp.main:app --port 8100
```

Server-rendered HTML and plain form posts. No build step and no JavaScript
framework, so it works with the browser alone.

## What each file holds

| File | Holds |
|---|---|
| `main.py` | creates the app, mounts the three stage modules, and serves the dashboard plus the database picker and automate action |
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
