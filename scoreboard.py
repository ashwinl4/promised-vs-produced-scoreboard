#!/usr/bin/env python3
"""scoreboard.py -- the entry point. Run this.

A thin launcher for the pipeline CLI, so the first thing anyone runs is short and
obvious:

    python3 scoreboard.py status        # row counts per stage
    python3 scoreboard.py verify-list   # the published scoreboard
    python3 scoreboard.py --help        # every command

It is exactly equivalent to `python3 -m pipeline.cli`,
which still works and is what the docs and the collection loops use internally.
Nothing lives here; the commands are defined in
pipeline/cli.py.
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from pipeline.cli import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(main())
