"""
llm.py -- the AI half of the pipeline, in two flavours.

The Source (search) and Screen pt-1 (extract) steps are "primarily meant for AI"
but can be driven two ways, neither of which is mandatory:

  A. CLAUDE CODE (no API key needed).  render_source_prompt() /
     render_screen_prompt() return the exact operating prompt to paste into a
     web-search-capable assistant like Claude Code. You run it there, it does the
     scraping, and hands back one JSON object which you ingest with the ordinary
     manual insert (source-add --json / screen-add --json, or the web textareas).
     This is the recommended path when you don't have an ANTHROPIC_API_KEY.

  B. DIRECT ANTHROPIC API (needs a key).  collect_source_lead() /
     extract_screen_row() call the Anthropic Messages API with the **web search**
     + **web fetch** server tools, then a second call with `output_config.format`
     (JSON Schema) to return a *schema-validated* object -- the "structured
     output option" the prompt asked for.

Both flavours share the SAME prompt builders, so the instructions are identical
whether a human, Claude Code, or the API executes them. The prompt-rendering
functions have no dependency on `anthropic` and work with no key; only the
flavour-B functions import the SDK (lazily).
"""

from __future__ import annotations

import json
import os
from pathlib import Path

from pipeline.schema_check import all_sectors, register_sector

# Models newer than Opus 4.6 support the _20260209 web tools with dynamic
# filtering; Opus 4.8 is the default for flavour B.
MODEL = os.getenv("PIPELINE_MODEL", "claude-opus-4-8")
EFFORT = os.getenv("PIPELINE_EFFORT", "high")

_PROMPT_DIR = Path(__file__).resolve().parent / "prompts"

WEB_TOOLS = [
    {"type": "web_search_20260209", "name": "web_search", "max_uses": 8},
    {"type": "web_fetch_20260209", "name": "web_fetch", "max_uses": 8},
]

# The keys each step returns -- documented here so the Claude Code path knows the
# exact shape to hand back.
SOURCE_KEYS = ["promise_source", "status_source", "promised_date_source", "summary"]
# The keys the extractor returns. For each date it returns BOTH the normalized
# token (announced / promised_first_output / actual_first_output) AND the verbatim
# *_raw source text it was copied from. lag_years / slip_years and the *_dt columns
# are NOT here: the pipeline computes them deterministically from the tokens
# (pipeline/dates.py), so two models that agree on the tokens agree on lag/slip.
SCREEN_KEYS = [
    "project", "sector", "state",
    "announced", "announced_raw",
    "promised_capital_usd", "promised_jobs",
    "promised_first_output", "promised_first_output_raw",
    "actual_first_output", "actual_first_output_raw",
    "current_status", "notes", "promise_source",
    "status_source", "flag", "promised_date_source",
]


class LLMUnavailable(RuntimeError):
    """Raised when the direct-API flavour is requested but not configured."""


# --------------------------------------------------------------------------- #
# Prompt builders (no API, no key -- for the Claude Code path AND flavour B)   #
# --------------------------------------------------------------------------- #

def _operating_prompt(filename: str) -> str:
    """Load a *_prompt.md and return the operating prompt (text below the first
    horizontal rule; the preamble above it is guidance for us, not the model)."""
    text = (_PROMPT_DIR / filename).read_text(encoding="utf-8")
    marker = "\n---\n"
    if marker in text:
        return text.split(marker, 1)[1].strip()
    return text.strip()


def render_source_prompt(
    avoid_projects: list[str] | None = None,
    avoid_inflight: list[str] | None = None,
) -> str:
    """The full Source operating prompt to run in Claude Code (web search).

    Paste the returned text into a web-search-capable assistant; it will find one
    new qualifying project and end with a single JSON object you can ingest with
    `source-add --json` (CLI) or the Source "Add from JSON" box (web).

    `avoid_projects` is the verify_verified exclusion list; `avoid_inflight` is the
    already-collected-but-unverified Source/Screen set (see
    `pipeline.inflight_project_hints`). Both are rendered as distinct sections so
    the collector steers away from everything already in the pipeline -- not just
    verify, which stays empty until a human promotes.
    """
    prompt = _operating_prompt("prompt_source_collected.md")
    # Always emitted, under the exact title the prompt body points at, so the
    # Claude Code and direct-API paths both see the live verify_verified state in
    # the expected place. Emitted even when the table is EMPTY: an absent section
    # would leave the collector guessing what is already covered, and on a
    # rebuild-from-empty the honest answer is "nothing is".
    prompt += "\n\n## The verify table already holds these — do not collect them\n"
    if avoid_projects:
        prompt += (
            "These `project` names are the current contents of the `verify_verified` "
            "table (the authoritative, verified scoreboard). Do **not** collect any of "
            "them, nor a mere expansion / re-announcement of one:\n> "
            + " · ".join(sorted(set(avoid_projects)))
        )
    else:
        prompt += (
            "The `verify_verified` table is currently **empty** — nothing has been "
            "published yet, so no project is excluded on this basis. Collect the best "
            "qualifying project you can find, including the largest and most obvious."
        )
    if avoid_inflight:
        # In-flight Source/Screen leads: already collected this run (or a prior
        # one) but not yet verified into verify. Excluding these is what stops the
        # collector from re-finding the same top project every iteration while
        # verify sits empty. Shown as Screen `project` names and Source summaries.
        prompt += (
            "\n\n## Already in flight — collected but not yet verified; do not re-file these\n"
            "These leads are already sitting in the pipeline's earlier stages "
            "(Source/Screen), shown as Screen `project` names and one-line Source "
            "summaries. Do **not** collect any project they describe, nor a mere "
            "expansion / re-announcement of one — pick something new:\n> "
            + " · ".join(sorted(set(avoid_inflight)))
        )
    prompt += (
        "\n\n## How to hand the result back\n"
        "Do the web search/scraping yourself, then end your reply with **only** "
        "the single JSON object from the Output format section above (keys: "
        f"{', '.join(SOURCE_KEYS)}), on its own, so it can be pasted straight "
        "back into the pipeline. No commentary after the JSON."
    )
    return prompt


def render_screen_prompt(lead: dict) -> str:
    """The full Screen pt-1 operating prompt for a specific Source lead.

    Paste into Claude Code; it opens the lead's links, extracts the 17-column
    row, and ends with a single JSON object you ingest with `screen-add --json`
    (CLI) or the Screen "Add from JSON" box (web).
    """
    prompt = _operating_prompt("prompt_screen_extracted.md")
    prompt += (
        "\n\n## Sector vocabulary — classify into ONE of these\n"
        "> " + " · ".join(sorted(all_sectors())) + "\n\n"
        "These are the defined manufacturing sectors (the pipeline is **not** "
        "sector-agnostic). Pick the one that fits. Only if the project is "
        "**clearly** a different *manufacturing* sector, use a new short sector "
        "name and say so in `flag` (e.g. \"new sector: Cement\"). If you are "
        "running this in Claude Code, also add that new sector to the `SECTORS` "
        "set in `schema.py` so the vocabulary grows with the "
        "data; otherwise a sector outside this list is rejected by the checker."
    )
    prompt += (
        "\n\n## The lead to extract from\n\n"
        f"- promise_source: {lead.get('promise_source', '') or '(none)'}\n"
        f"- status_source: {lead.get('status_source', '') or '(none)'}\n"
        f"- promised_date_source: {lead.get('promised_date_source', '') or '(none)'}\n"
        f"- summary: {lead.get('summary', '') or '(none)'}\n"
    )
    if lead.get("source_collected_id"):
        prompt += f"- source_collected_id: {lead['source_collected_id']}\n"
    prompt += (
        "\nOpen these links, read them, and extract the row.\n\n"
        "## How to hand the result back\n"
        "End your reply with **only** the single JSON object from the Output "
        f"format section above (the {len(SCREEN_KEYS)} keys, including each date's "
        "*_raw verbatim partner), on its own, so "
        "it can be pasted straight back into the pipeline. Use digits only for "
        "promised_capital_usd and promised_jobs. Do not include "
        "verification_tier -- it is always P at this stage. No commentary after "
        "the JSON."
    )
    return prompt


# --------------------------------------------------------------------------- #
# Flavour B -- direct Anthropic API (needs a key)                             #
# --------------------------------------------------------------------------- #

def _client():
    try:
        import anthropic  # lazy: only needed for the API flavour
    except ImportError as e:  # pragma: no cover
        raise LLMUnavailable(
            "the API flavour needs the `anthropic` package: pip install anthropic. "
            "No key? Use the Claude Code path instead (render_*_prompt / the "
            "'-prompt' CLI commands)."
        ) from e
    try:
        return anthropic.Anthropic()
    except Exception as e:  # pragma: no cover
        raise LLMUnavailable(f"could not construct Anthropic client: {e}") from e


def _research(instruction: str, max_loops: int = 8) -> str:
    """Phase 1: run the model with web tools until it produces a final answer."""
    client = _client()
    import anthropic

    messages = [{"role": "user", "content": instruction}]
    last_text = ""
    for _ in range(max_loops):
        try:
            resp = client.messages.create(
                model=MODEL,
                max_tokens=8000,
                thinking={"type": "adaptive"},
                output_config={"effort": EFFORT},
                tools=WEB_TOOLS,
                messages=messages,
            )
        except anthropic.APIError as e:  # pragma: no cover
            raise LLMUnavailable(f"Anthropic API error during research: {e}") from e

        if resp.stop_reason == "refusal":
            raise LLMUnavailable(
                "model refused the request "
                f"({getattr(resp.stop_details, 'category', None)})"
            )

        last_text = "".join(b.text for b in resp.content if b.type == "text")

        if resp.stop_reason == "pause_turn":
            messages.append({"role": "assistant", "content": resp.content})
            continue
        return last_text
    return last_text


def _schema_too_complex(err: Exception) -> bool:
    """True for the API's 'Schema is too complex.' 400 on output_config.format."""
    return "schema is too complex" in str(err).lower()


def _extract_json_object(text: str) -> dict:
    """Parse the first JSON object out of a model reply.

    The strict json_schema path returns bare JSON, but the schema-less fallback
    (below) can wrap it in prose or ```json fences, so tolerate both: strip a
    leading/trailing code fence, then fall back to the outermost {...} span.
    """
    text = text.strip()
    if text.startswith("```"):
        text = text.split("\n", 1)[1] if "\n" in text else ""
        if text.rstrip().endswith("```"):
            text = text.rstrip()[:-3]
        text = text.strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        start, end = text.find("{"), text.rfind("}")
        if start != -1 and end > start:
            return json.loads(text[start:end + 1])
        raise


def _structure(research: str, schema: dict, instruction: str) -> dict:
    """Phase 2: turn the research into a JSON object.

    Prefer the strict `output_config.format` (json_schema) path. If the API
    rejects the schema as too complex, retry WITHOUT the schema and parse the
    reply ourselves -- the deterministic `screen_check` (schema_check.check_row)
    verifies the row downstream, so dropping the schema loses no validation.
    """
    client = _client()
    import anthropic

    prompt = f"{instruction}\n\n---\n{research}"

    def _create(use_schema: bool):
        kwargs = dict(
            model=MODEL,
            max_tokens=4000,
            messages=[{"role": "user", "content": prompt}],
        )
        if use_schema:
            kwargs["output_config"] = {"format": {"type": "json_schema", "schema": schema}}
        return client.messages.create(**kwargs)

    try:
        resp = _create(use_schema=True)
    except anthropic.APIError as e:
        if _schema_too_complex(e):
            try:
                resp = _create(use_schema=False)
            except anthropic.APIError as e2:  # pragma: no cover
                raise LLMUnavailable(
                    f"Anthropic API error during structuring (fallback): {e2}"
                ) from e2
        else:  # pragma: no cover
            raise LLMUnavailable(f"Anthropic API error during structuring: {e}") from e

    text = "".join(b.text for b in resp.content if b.type == "text").strip()
    try:
        return _extract_json_object(text)
    except json.JSONDecodeError as e:  # pragma: no cover
        raise LLMUnavailable(f"model did not return valid JSON: {text[:200]}") from e


_SOURCE_SCHEMA = {
    "type": "object",
    "properties": {
        "promise_source": {"type": "string"},
        "status_source": {"type": "string"},
        "promised_date_source": {"type": "string"},
        "summary": {"type": "string"},
    },
    "required": ["promise_source", "status_source"],
    "additionalProperties": False,
}

_SCREEN_SCHEMA = {
    "type": "object",
    "properties": {
        "project": {"type": "string"},
        "sector": {"type": "string"},
        "state": {"type": "string"},
        # Each date is a pair: the normalized token the parser consumes, plus the
        # verbatim source text (*_raw) it was copied off the page.
        "announced": {"type": "string"},
        "announced_raw": {"type": "string"},
        "promised_capital_usd": {"type": "integer"},
        "promised_jobs": {"type": "integer"},
        "promised_first_output": {"type": "string"},
        "promised_first_output_raw": {"type": "string"},
        "actual_first_output": {"type": "string"},
        "actual_first_output_raw": {"type": "string"},
        "current_status": {"type": "string"},
        "notes": {"type": "string"},
        "promise_source": {"type": "string"},
        "status_source": {"type": "string"},
        "flag": {"type": "string"},
        "promised_date_source": {"type": "string"},
    },
    "required": ["project", "sector", "state", "announced", "current_status"],
    "additionalProperties": False,
}


def collect_source_lead(
    avoid_projects: list[str] | None = None,
    avoid_inflight: list[str] | None = None,
) -> dict:
    """[API flavour] Find ONE new qualifying project and return its lead dict."""
    research = _research(render_source_prompt(avoid_projects, avoid_inflight))
    if _looks_like_no_result(research):
        raise LLMUnavailable(
            "the model reported it could not find a qualifying new project"
        )
    lead = _structure(
        research,
        _SOURCE_SCHEMA,
        "From the research below, output the single source_collected lead as JSON "
        f"with keys {', '.join(SOURCE_KEYS)} (promised_date_source and summary "
        "optional). If no qualifying project was found, return empty "
        "promise_source and status_source.",
    )
    if not (lead.get("promise_source") and lead.get("status_source")):
        raise LLMUnavailable("no qualifying project found (empty sources)")
    return lead


def extract_screen_row(lead: dict) -> dict:
    """[API flavour] Extract one 17-column row from a Source lead."""
    research = _research(render_screen_prompt(lead))
    row = _structure(
        research,
        _SCREEN_SCHEMA,
        "From the extraction below, output the single screen_extracted row as JSON. "
        "Digits only for promised_capital_usd and promised_jobs. For EACH date give "
        "TWO cells: the normalized token (announced, promised_first_output, "
        "actual_first_output) that the pipeline can parse, AND its *_raw partner "
        "(announced_raw, promised_first_output_raw, actual_first_output_raw) holding "
        "the EXACT source text you copied it from, verbatim -- no cleanup. Do NOT "
        "include lag_years, slip_years, the *_dt columns, or verification_tier: the "
        "pipeline computes those deterministically. Extraction problems go in `flag` "
        "('None' if clean).",
    )
    row.setdefault("promise_source", lead.get("promise_source", ""))
    row.setdefault("status_source", lead.get("status_source", ""))
    if lead.get("promised_date_source"):
        row.setdefault("promised_date_source", lead["promised_date_source"])

    # Sector standardization (API path): if the extracted sector isn't in the
    # live vocabulary, FLAG it and ADD it onto the schema via the function --
    # register_sector() persists it to the registry, distinct from the Claude
    # Code path which edits the SECTORS set in code.
    sector = (row.get("sector") or "").strip()
    if sector and sector not in all_sectors():
        if register_sector(sector):
            note = (f"new sector {sector!r} was not in the vocabulary -- "
                    "registered via register_sector()")
            prior = (row.get("flag") or "").strip()
            row["flag"] = (
                note if not prior or prior.lower() == "none" else f"{prior}; {note}"
            )
    return row


def _looks_like_no_result(text: str) -> bool:
    low = text.lower()
    return (
        "could not find" in low
        or "no qualifying" in low
        or "unable to find" in low
    ) and "http" not in low
