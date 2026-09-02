# Collecting new projects

*Directory: [`../collect/`](../collect/)*

The ongoing collection: shell loops that spawn fresh headless Claude Code workers to
find new projects and extract them into rows. The short version is in the
[scoreboard README](../README.md#add-data); this is the full set of controls.

*Directory: [`collect/`](../collect/) — `all.sh`, `source.sh`, `screen.sh`, `prompts/`*

Where the bulk import was a one-off load, this is the ongoing collection: shell loops
that spawn fresh headless Claude Code workers to find new projects and extract
them. Bookkeeping is below; the full methodology is in
[`prompts/README.md`](../collect/prompts/README.md)
and each `promptN_*.md` beside it.

**Before any run:** `cd` into the `scoreboard/` directory and
make sure the `claude` CLI is logged in there (`claude` → `/login`, one-time). Each
command below spawns fresh Claude Code workers via the loop scripts.

### ⭐ START HERE — PROMPT 1 + PROMPT 2 in one command

Collecting leads then extracting them is the normal session, so it has a single
runner, reachable as a command:

```bash
python3 scoreboard.py collect --n 10
```

→ adds 10 to `source_collected` (PROMPT 1), then 10 to `screen_extracted`
(PROMPT 2), then prints one before/after summary.

The command covers the four knobs most sessions need. Everything else is set the
way the shell has always set it, by prefixing the variable to `bash collect/all.sh`:

| want | command |
|---|---|
| see the plan without calling Claude | `python3 scoreboard.py collect --n 5 --dry-run` |
| only one stage | `python3 scoreboard.py collect --only screen --n 5` |
| keep going after a failed iteration | `python3 scoreboard.py collect --n 10 --continue-on-fail` |
| different sizes per stage | `SOURCE_ADD=10 SCREEN_ADD=4 bash collect/all.sh` |
| cheaper discovery, careful extraction | `SOURCE_EFFORT=medium SCREEN_EFFORT=high N=8 bash collect/all.sh` |
| stream one stage live | `SCREEN_VERBOSE=1 N=3 bash collect/all.sh` |

`VAR=value` before a command is shell syntax for setting that variable for that
one run: it does not persist afterwards, and it must come *before* `bash`, not
after the script name. The flags above need none of that.

**Per-stage config:** any knob below can be prefixed `SOURCE_` or `SCREEN_` to
aim it at one stage; the prefixed value beats the shared one. So
`MODEL=claude-sonnet-4-5 SCREEN_MODEL=claude-opus-4-8` runs discovery cheap and
extraction strong.

Also: the `claude` auth preflight runs **once** (not per stage), and a failing
Source stage stops the run before Screen — `CONTINUE_ON_FAIL=1` to push on
anyway, which is what you want when Screen should chew through leads collected
in an earlier session.

> **Cost:** each stage spawns one fresh headless Claude Code worker per
> iteration, so `N=10` on both stages is 20+ worker calls. Start small, and use
> `DRY_RUN=1` first.

Run the stages separately (below) when you want to inspect the Source rows
before extracting them.

### `source.sh` — PROMPT 1: discover NEW projects on the web → Source

`ADD` = rows to add.

```bash
ADD=25 bash collect/source.sh
```

### `screen.sh` — PROMPT 2: extract Source → Screen

`ADD` = screen rows to add. `screen.sh` is the *same* loop as `source.sh`, pointed at the
extraction prompt and counting a different table, so this is a thin wrapper.

```bash
ADD=5 bash collect/screen.sh
```

### Knobs (all commands)

- `ADD=n` (the collect loops) = add *n* rows this run; `N=n` (`collect.sh`) = how many to add at each stage.
- Every loop understands `ADD`, `MODEL`, `EFFORT`, `MAX_ITERS`, `MAX_STALL`, `VERBOSE`, `PROMPT_FILE`, `COUNT_TABLE`, `PREFLIGHT`.
- In `collect.sh` only: `ONLY=source|screen|both`, `DRY_RUN=1`, `CONTINUE_ON_FAIL=1`, and the `SOURCE_` / `SCREEN_` prefixes.
- `VERBOSE=1` = stream the worker live (JSON firehose); omit for the clean per-iteration heartbeat.
- `Ctrl-C` stops cleanly; re-running continues where you left off (the DB dedups overlap).
- Provenance: PROMPT 1 stamps `source_collected.collected_via` via `--via prompt1`.
- Verify is a human-only gate — not driven by these loops.

---
