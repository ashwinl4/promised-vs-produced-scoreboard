# Part 2 — collecting new projects

The ongoing harvest. Two shell loops that start fresh headless Claude Code
workers: one finds new projects and files them into Source, the other extracts
Source leads into Screen rows. Neither touches Verify, which is a human gate.

**Contents**

- [Run it](#run-it)
- [What is here](#what-is-here)
- [How the loops behave](#how-the-loops-behave)

## Run it

Run from the parent directory (`scoreboard/`). Needs the `claude` CLI logged in
once (`claude`, then `/login`); the loop checks that before it starts.

```bash
python3 scoreboard.py collect --n 5 --dry-run   # show the plan
python3 scoreboard.py collect --n 10            # do it
```

The scripts still run directly, and that is the form to use when you want a knob
the flags do not expose (see [`../docs/collecting.md`](../docs/collecting.md)):

```bash
N=5 DRY_RUN=1 bash collect/all.sh   # identical to the first command above
N=10 bash collect/all.sh            # identical to the second
```

## What is here

| Path | What it does |
|---|---|
| `collect.sh` | runs both stages back to back. The usual entry point. |
| `source.sh` | stage A on its own: web discovery into Source |
| `screen.sh` | stage B on its own: Source into Screen |
| `prompts/` | the prompt handed to each worker, and how the workers are configured |

## How the loops behave

Each iteration is a separate stateless worker, so nothing carries between passes.
Ctrl-C is safe, and re-running continues where you left off because the database
de-duplicates.

Every knob: [`../docs/collecting.md`](../docs/collecting.md).
