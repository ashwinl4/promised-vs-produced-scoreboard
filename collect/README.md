# Part 2 — collecting new projects

The ongoing harvest. Two shell loops that start fresh headless Claude Code
workers: one finds new projects and files them into Source, the other extracts
Source leads into Screen rows. Neither touches Verify, which is a human gate.

**Contents**

- [Run it](#run-it)
- [What is here](#what-is-here)
- [How the loops behave](#how-the-loops-behave)

## Run it

Run from the parent directory (`scoreboard/`). Needs the `claude` CLI logged in once
(`claude`, then `/login`).

```bash
N=5 DRY_RUN=1 bash collect/all.sh   # show the plan
N=10 bash collect/all.sh            # do it
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
