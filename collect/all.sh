#!/usr/bin/env bash
#
# all.sh -- run the Source stage and then the Screen stage for N
# datapoints, in one command.
#
# source.sh and screen.sh are the same outer loop pointed at different prompts and
# counting different tables, so the normal session is "collect N leads, then
# extract N rows" -- two commands and a wait in between. This runs the pair
# back-to-back and prints one before/after summary.
#
# RUN IT (from anywhere; it cd's to scoreboard/ itself. bash on macOS/Linux/WSL):
#   bash collect/all.sh            # N=10 to each stage
#   N=3 bash collect/all.sh        # 3 leads, then 3 extractions
#   N=5 DRY_RUN=1 bash collect/all.sh   # show the plan, call nothing
#
#   # different sizes per stage, and only one stage:
#   SOURCE_ADD=10 SCREEN_ADD=4 bash collect/all.sh
#   ONLY=screen N=5 bash collect/all.sh
#
#   # per-stage model/effort (screen extraction is the fiddlier job):
#   SOURCE_EFFORT=medium SCREEN_EFFORT=high N=8 \
#     bash collect/all.sh
#
# CONFIG
#   Shared knobs are passed to BOTH stages; every one can be overridden for a
#   single stage with a SOURCE_ / SCREEN_ prefix, which wins over the shared
#   value:
#
#     N            how many rows to add at each stage        (default 10)
#     ONLY         source | screen | both                    (default both)
#     MODEL        Claude model for the workers              (default claude-opus-4-8)
#     EFFORT       low | medium | high                       (default high)
#     MAX_ITERS    hard cap on loop turns per stage          (default 200)
#     MAX_STALL    give up after N no-progress iterations    (default 3)
#     VERBOSE      1 = stream the worker's tool calls        (default 0)
#     PROMPT_FILE  operating prompt for the stage            (per-stage defaults)
#     COUNT_TABLE  table the stage counts                    (per-stage defaults)
#
#     e.g. SCREEN_MAX_STALL=5, SOURCE_MODEL=claude-sonnet-4-5, SCREEN_VERBOSE=1
#
#   Plus:
#     DRY_RUN=1        print the two commands and exit without calling Claude
#     PREFLIGHT=0      skip the "is claude authenticated?" probe entirely
#     CONTINUE_ON_FAIL=1  run Screen even if Source exited non-zero
#
# NOTE ON COST: each stage spawns one fresh headless Claude Code worker per
# iteration, so `N=10` on both stages is ~20+ worker calls. Start small.
#
set -euo pipefail

trap 'echo; echo "interrupted -- stopping."; exit 130' INT TERM

HERE="$(cd "$(dirname "$0")" && pwd)"
SCOREBOARD_ROOT="$(cd "$HERE/.." && pwd)"
SOURCE_LOOP="$HERE/source.sh"
SCREEN_LOOP="$HERE/screen.sh"
for f in "$SOURCE_LOOP" "$SCREEN_LOOP"; do
  [ -f "$f" ] || { echo "ERROR: missing $f"; exit 1; }
done

cd "$SCOREBOARD_ROOT"
PY="$(command -v python3 || command -v python)"

# --- Config ---------------------------------------------------------------- #
N="${N:-10}"
ONLY="${ONLY:-both}"
case "$ONLY" in source|screen|both) ;; *)
  echo "ERROR: ONLY must be source | screen | both (got '$ONLY')"; exit 1 ;;
esac

DRY_RUN="${DRY_RUN:-0}"
CONTINUE_ON_FAIL="${CONTINUE_ON_FAIL:-0}"

# stage_cfg <STAGE> <NAME> <fallback> -- the per-stage value if set, else the
# shared value if set, else the fallback. Lets SCREEN_EFFORT beat EFFORT.
stage_cfg() {
  local stage="$1" name="$2" fallback="$3" specific shared
  specific="${stage}_${name}"
  shared="$name"
  if [ -n "${!specific:-}" ]; then printf '%s' "${!specific}"
  elif [ -n "${!shared:-}" ]; then printf '%s' "${!shared}"
  else printf '%s' "$fallback"; fi
}

count() {
  "$PY" -m pipeline.cli status \
    | awk -v t="$1" '$0 ~ t {print $NF}'
}

# --- Run one stage --------------------------------------------------------- #
# Each stage is launched with an explicit, self-contained env (env -u strips any
# PROMPT_FILE/COUNT_TABLE inherited from the caller) so the Source stage's
# settings can never leak into the Screen stage.
run_stage() {
  local stage="$1" script="$2" default_prompt="$3" default_table="$4" preflight="$5"
  local add prompt table model effort iters stall verbose

  add="$(stage_cfg   "$stage" ADD         "$N")"
  prompt="$(stage_cfg "$stage" PROMPT_FILE "$default_prompt")"
  table="$(stage_cfg "$stage" COUNT_TABLE "$default_table")"
  model="$(stage_cfg "$stage" MODEL       "claude-opus-4-8")"
  effort="$(stage_cfg "$stage" EFFORT     "high")"
  iters="$(stage_cfg "$stage" MAX_ITERS   "200")"
  stall="$(stage_cfg "$stage" MAX_STALL   "3")"
  verbose="$(stage_cfg "$stage" VERBOSE   "0")"

  echo
  echo "==================================================================="
  echo "  ${stage}: add $add to $table"
  echo "  prompt=$prompt"
  echo "  model=$model effort=$effort max_iters=$iters max_stall=$stall verbose=$verbose"
  echo "==================================================================="

  local cmd=(env -u PROMPT_FILE -u COUNT_TABLE
    "ADD=$add" "PROMPT_FILE=$prompt" "COUNT_TABLE=$table"
    "MODEL=$model" "EFFORT=$effort" "MAX_ITERS=$iters"
    "MAX_STALL=$stall" "VERBOSE=$verbose" "PREFLIGHT=$preflight"
    bash "$script")

  if [ "$DRY_RUN" = "1" ]; then
    printf '  DRY RUN, would execute:\n    '
    printf '%q ' "${cmd[@]}"; printf '\n'
    return 0
  fi
  "${cmd[@]}"
}

# --- Plan ------------------------------------------------------------------ #
src_before="$(count source_collected)"; src_before="${src_before:-0}"
scr_before="$(count screen_extracted)"; scr_before="${scr_before:-0}"

echo "database : $("$PY" -c 'from pipeline.db import db_path; print(db_path())')"
echo "start    : source_collected=$src_before  screen_extracted=$scr_before"
echo "plan     : ONLY=$ONLY  N=$N"

# The authentication probe costs one Claude call, so run it in the FIRST stage
# only and skip it in the second -- by then we already know the CLI works.
first_preflight="${PREFLIGHT:-1}"

rc=0
if [ "$ONLY" = "both" ] || [ "$ONLY" = "source" ]; then
  run_stage SOURCE "$SOURCE_LOOP" \
    "collect/prompts/prompt1_collect_recent.md" \
    "source_collected" "$first_preflight" || rc=$?
  if [ "$rc" -ne 0 ]; then
    echo
    echo "!! SOURCE stage exited non-zero ($rc)."
    if [ "$CONTINUE_ON_FAIL" != "1" ]; then
      echo "   Stopping before Screen. Set CONTINUE_ON_FAIL=1 to run it anyway"
      echo "   (Screen can still extract from Source leads collected earlier)."
      exit "$rc"
    fi
    echo "   CONTINUE_ON_FAIL=1 -- carrying on to Screen."
  fi
  first_preflight=0     # authentication already proven
fi

if [ "$ONLY" = "both" ] || [ "$ONLY" = "screen" ]; then
  run_stage SCREEN "$SCREEN_LOOP" \
    "collect/prompts/prompt2_extract_screen.md" \
    "screen_extracted" "$first_preflight"
fi

# --- Summary --------------------------------------------------------------- #
if [ "$DRY_RUN" = "1" ]; then
  echo; echo "dry run -- nothing was called."; exit 0
fi

src_after="$(count source_collected)"; src_after="${src_after:-0}"
scr_after="$(count screen_extracted)"; scr_after="${scr_after:-0}"

echo
echo "==================================================================="
echo "  done"
echo "    source_collected  $src_before -> $src_after  (+$(( src_after - src_before )))"
echo "    screen_extracted  $scr_before -> $scr_after  (+$(( scr_after - scr_before )))"
echo "==================================================================="
"$PY" -m pipeline.cli status
