#!/usr/bin/env bash
#
# source.sh -- drive PROMPT 1 (or PROMPT 2) as repeated, independent
# Claude Code calls until ADD new rows have been added THIS RUN.
#
# HOW IT WORKS (the "outer loop"):
#   It notes the starting row count for COUNT_TABLE, then each iteration...
#     1. reads the current count from `pipeline.cli status`,
#     2. stops once (current - starting) >= ADD, i.e. ADD rows were added this run,
#     3. launches ONE `claude -p` process that actually
#        does the job -- web-searches, collects, and writes to scoreboard.db using
#        the real pipeline CLI. It is a brand-new process every time, so no
#        context bleeds between runs (see prompts/README.md, "no chat history").
#   This script is JUST the loop; the `claude` CLI does the work each turn.
#
# WHY IT DOESN'T DOUBLE-COLLECT:
#   Every `source-add` commits immediately, and each iteration re-runs `source-prompt`
#   first -- which re-reads the DB (published + already collected) -- so each run
#   automatically steers around everything collected so far.
#
# RUN IT (from anywhere; it cd's to scoreboard/ itself. bash on macOS/Linux/WSL):
#   bash collect/source.sh                 # add ADD (default 10) source leads
#   ADD=3 bash collect/source.sh           # just add 3 this run
#   PROMPT_FILE=collect/prompts/prompt2_extract_screen.md \
#     COUNT_TABLE=screen_extracted ADD=10 bash collect/source.sh   # PROMPT 2
#
set -euo pipefail

# Stop cleanly on Ctrl-C / terminal close instead of rolling into the next
# iteration. (Without this, the `|| echo` below swallows the interrupt.)
trap 'echo; echo "interrupted -- stopping the loop."; exit 130' INT TERM

# --- Config (override any of these via env) -------------------------------- #
PROMPT_FILE="${PROMPT_FILE:-collect/prompts/prompt1_collect_recent.md}"
COUNT_TABLE="${COUNT_TABLE:-source_collected}"   # which stage's rows we're adding to
ADD="${ADD:-10}"                                  # how many NEW rows to add to COUNT_TABLE this run
# The cap bounds COST, not liveness -- MAX_STALL below is the liveness check.
# It exists for the case MAX_STALL cannot see: steady but slow progress. `stall`
# resets on every success, so a run adding one row every four turns never trips
# it while paying four calls per row. Only this cap bounds that.
#
# It scales with ADD rather than sitting at a flat number, because "too many
# turns" depends on how many rows were asked for. A flat 200 silently truncated
# any run wanting more than ~66. Non-numeric or tiny values fall back to a floor
# so the loop can never be capped at zero -- BSD seq counts DOWN for `seq 1 0`,
# which would run two turns with i=1 then i=0.
case "$ADD" in
  ''|*[!0-9]*) _iters_default=30 ;;
  *) _iters_default=$(( ADD * 3 ))
     [ "$_iters_default" -lt 30 ] && _iters_default=30 ;;
esac
MAX_ITERS="${MAX_ITERS:-$_iters_default}"         # cost cap on loop turns
MAX_STALL="${MAX_STALL:-3}"                       # stop after this many no-progress iterations in a row
MODEL="${MODEL:-claude-opus-4-8}"
# NOTE: the print-mode `--effort` flag only accepts low|medium|high -- there is no
# "extra high" from the CLI. `high` is the ceiling here.
EFFORT="${EFFORT:-high}"
VERBOSE="${VERBOSE:-0}"   # 1 = stream tool calls/text live (JSON firehose)

# --- Locate scoreboard/ and the tools --------------------------------- #
cd "$(dirname "$0")/.."                            # collect/ -> scoreboard/
PY="$(command -v python3 || command -v python)"
CLAUDE="$(command -v claude || echo "$HOME/.local/bin/claude")"
[ -x "$CLAUDE" ] || { echo "ERROR: claude CLI not found ($CLAUDE)"; exit 1; }

# --- Preflight: the claude CLI must be authenticated ----------------------- #
# One tiny call detects the "Not logged in" state so we fail fast with guidance
# instead of spinning through failed iterations. Skip with PREFLIGHT=0.
if [ "${PREFLIGHT:-1}" = "1" ]; then
  # `timeout` is GNU coreutils and is absent on stock macOS; fall back to
  # `gtimeout` if present, else run the probe unbounded.
  if command -v timeout >/dev/null 2>&1; then TIMEOUT="timeout 120"
  elif command -v gtimeout >/dev/null 2>&1; then TIMEOUT="gtimeout 120"
  else TIMEOUT=""; fi
  probe="$($TIMEOUT "$CLAUDE" -p "Reply with exactly: READY" \
             --model "$MODEL" --permission-mode default 2>&1 || true)"
  if ! printf '%s' "$probe" | grep -q "READY"; then
    echo "ERROR: claude preflight failed. First response was:"
    printf '  %s\n' "$probe" | head -5
    echo "If it says 'Not logged in', run this once:  claude   (then  /login)"
    echo "Then re-run. (Set PREFLIGHT=0 to skip this check.)"
    exit 1
  fi
  echo "preflight ok (claude authenticated)."
fi

PROMPT="$(cat "$PROMPT_FILE")"
count() { "$PY" -m pipeline.cli status | awk -v t="$COUNT_TABLE" '$0 ~ t {print $NF}'; }

# Live-streaming flags (VERBOSE=1). stream-json requires --verbose in print mode.
# Expanded below as ${VERBOSE_FLAGS[@]+"${VERBOSE_FLAGS[@]}"} rather than plain
# "${VERBOSE_FLAGS[@]}": under `set -u`, bash 3.2 (which macOS still ships)
# treats an empty array expansion as an unbound variable and aborts.
VERBOSE_FLAGS=()
[ "$VERBOSE" = "1" ] && VERBOSE_FLAGS=(--verbose --output-format stream-json --include-partial-messages)


# --- Run transcript --------------------------------------------------------- #
# Every run tees its own output to logs/. The transcript is the only record of
# what a run actually did: which model and effort, how many turns, which
# iterations failed, why it stopped. The database says what was collected but
# not how, and for a corpus that will be cited, how is part of the claim.
#
# LOG=0 turns it off. LOG=<path> picks the file.
#
# LOG_ACTIVE is what stops a stage double-writing. all.sh already redirects its
# own output through tee, and a stage it calls inherits that -- so if the stage
# also opened the file, every line would be written twice: once through the
# stage's tee and once through all.sh's. The parent sets LOG_ACTIVE, the child
# sees it and skips its own redirect, and the lines still reach the file by
# flowing up through the parent. Run a stage on its own and LOG_ACTIVE is unset,
# so it opens its own transcript.
LOG="${LOG:-}"
if [ "$LOG" != "0" ] && [ -z "${LOG_ACTIVE:-}" ]; then
  [ -n "$LOG" ] || LOG="logs/$(date -u +%Y%m%dT%H%M%SZ)-${COUNT_TABLE}.log"
  mkdir -p "$(dirname "$LOG")"
  export LOG LOG_ACTIVE=1
  exec > >(tee -a "$LOG") 2>&1
fi

START="$(count)"; START="${START:-0}"
# One header line, so a transcript read on its own says when it ran and which
# database it wrote to. all.sh prints these too; a stage run directly did not.
echo "started : $(date -u +%Y-%m-%dT%H:%M:%SZ)  db=$("$PY" -c 'from pipeline.db import db_path; print(db_path())')"
echo "loop: prompt=$PROMPT_FILE  add $ADD to $COUNT_TABLE (now $START)  (model=$MODEL, effort=$EFFORT)"

stall=0
hit_cap=1        # cleared by either deliberate exit below
for i in $(seq 1 "$MAX_ITERS"); do
  now="$(count)"; now="${now:-0}"
  added=$(( now - START ))
  echo "=== iter $i - added $added/$ADD this run ($COUNT_TABLE=$now) ==="
  [ "$added" -ge "$ADD" ] && { echo "added $ADD this run; stopping."; hit_cap=0; break; }

  # One fresh process. The operating prompt goes in the system prompt (works for
  # prompt1 OR prompt2). docs/cli.md is attached as context via an @-mention
  # (like clicking a file into context in the IDE) rather than read with a tool.
  # --allowedTools pre-approves tools so nothing stalls on a permission prompt.
  #
  # docs/cli.md is the command reference and nothing else, so the attachment is
  # scoped by construction. The two guardrails below still have to be said: with
  # Bash pre-approved above, the process could otherwise re-launch the loop that
  # started it, and Verify is a human-only gate.
  "$CLAUDE" -p "@docs/cli.md is attached as reference context -- it is this pipeline's CLI reference. Never run a shell script from collect/ (that is the loop that launched you), and never promote to Verify (verify-promote / --promote-tier) -- that is a human-only gate. Now carry out your operating instructions (in the system prompt) to completion using the real pipeline CLI. Do only what those instructions say." \
    --model "$MODEL" \
    --effort "$EFFORT" \
    --append-system-prompt "$PROMPT" \
    --allowedTools "Bash Read Write WebSearch WebFetch" \
    --permission-mode acceptEdits \
    ${VERBOSE_FLAGS[@]+"${VERBOSE_FLAGS[@]}"} \
    || echo "  ! iteration $i failed; continuing."

  after="$(count)"; after="${after:-0}"
  if [ "$after" -le "$now" ]; then
    stall=$(( stall + 1 ))
    echo "  (no new rows this iteration; stall $stall/$MAX_STALL)"
    [ "$stall" -ge "$MAX_STALL" ] && { echo "no new leads in $MAX_STALL iterations; stopping."; hit_cap=0; break; }
  else
    stall=0
  fi
done

# Running out of turns used to look exactly like finishing: the `for` simply
# ended and the same "done." line printed. Say it, loudly, or a truncated run
# reads as a complete one. NOT an error exit -- the stage did real work, and
# all.sh skips Screen when Source exits non-zero, which would strand every row
# this run collected.
if [ "$hit_cap" = "1" ]; then
  final="$(count)"; final="${final:-0}"
  echo
  echo "!! ==================================================================="
  echo "!!  STOPPED AT THE ITERATION CAP -- THIS RUN IS INCOMPLETE"
  echo "!!"
  echo "!!    added $(( final - START ))/$ADD rows in $MAX_ITERS turns"
  echo "!!"
  echo "!!  Re-run with a higher MAX_ITERS. Nothing is collected twice: the"
  echo "!!  database de-duplicates, so a re-run continues where this stopped."
  echo "!! ==================================================================="
  echo
fi

echo "done. Final:"; "$PY" -m pipeline.cli status
