"""
tools/conflict_detector.py

Detects contradictions between clinical documents.
Called by the agent critic whenever the same field is found in two sources.
Uses an LLM to judge if values are genuinely contradictory vs complementary.
"""

from __future__ import annotations
import json
import os

from models import Conflict, SourceRef, DocType
from prompts import CONFLICT_PROMPT


# ---------------------------------------------------------------------------
# LLM-based contradiction judge
# ---------------------------------------------------------------------------

def _llm_judge(field: str, val_a: str, src_a: str, val_b: str, src_b: str) -> dict:
    """
    Ask the LLM whether two values for the same field are genuinely contradictory.
    Falls back to conservative (contradictory=True) on any failure.
    """
    try:
        from openai import OpenAI
        client = OpenAI(api_key=os.environ["OPENAI_API_KEY"])

        prompt = CONFLICT_PROMPT.format(
            field=field,
            source_a=src_a, val_a=val_a,
            source_b=src_b, val_b=val_b,
        )
        resp = client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[{"role": "user", "content": prompt}],
            temperature=0,
            max_tokens=200,
        )
        raw = resp.choices[0].message.content.strip()
        return json.loads(raw)

    except Exception as exc:
        # Safe default: treat as contradictory so clinician sees it
        return {
            "contradictory": True,
            "explanation": f"LLM judge unavailable ({exc}) — flagging conservatively.",
        }


# ---------------------------------------------------------------------------
# Rule-based fast path (no LLM call for obvious cases)
# ---------------------------------------------------------------------------

def _quick_rules(val_a: str, val_b: str) -> bool | None:
    """
    Return True/False for clear-cut cases, None if the LLM should decide.
    """
    a, b = val_a.strip().lower(), val_b.strip().lower()
    # Identical → no conflict
    if a == b:
        return False
    # One is a substring of the other (e.g., "hypertension" vs "hypertension, uncontrolled")
    # This is NOT automatically "not a conflict" in clinical context → let LLM decide
    # Empty vs non-empty → no conflict (one doc just didn't mention it)
    if not a or not b:
        return False
    return None  # LLM decides


# ---------------------------------------------------------------------------
# Main tool interface called by the agent
# ---------------------------------------------------------------------------

def detect(
    field:    str,
    val_a:    str,
    source_a: SourceRef,
    val_b:    str,
    source_b: SourceRef,
    use_llm:  bool = True,
) -> dict:
    """
    Agent-facing tool: check if two values for the same field conflict.

    Returns:
      {
        "is_conflict":   bool,
        "explanation":   str,
        "conflict_obj":  dict | None,   # Conflict model if is_conflict=True
      }
    """
    quick = _quick_rules(val_a, val_b)

    if quick is False:
        return {
            "is_conflict":  False,
            "explanation":  "Values identical or one is empty — no conflict.",
            "conflict_obj": None,
        }

    if use_llm and quick is None:
        result = _llm_judge(
            field, val_a, source_a.doc_id, val_b, source_b.doc_id
        )
    else:
        # Conservative fallback
        result = {"contradictory": True, "explanation": "Values differ — flagged for clinician review."}

    if result.get("contradictory", True):
        conflict = Conflict(
            field=field,
            value_a=val_a,
            source_a=source_a,
            value_b=val_b,
            source_b=source_b,
        )
        return {
            "is_conflict":  True,
            "explanation":  result.get("explanation", ""),
            "conflict_obj": conflict.model_dump(),
        }
    else:
        return {
            "is_conflict":  False,
            "explanation":  result.get("explanation", ""),
            "conflict_obj": None,
        }


# ---------------------------------------------------------------------------
# Bulk scan — check extracted facts dict for any cross-source conflicts
# ---------------------------------------------------------------------------

def scan_facts_for_conflicts(
    field_candidates: dict[str, list[tuple[str, SourceRef]]]
) -> list[Conflict]:
    """
    Given a dict of {field: [(value, source), (value, source), ...]}
    find all pairs that conflict.

    Used by the agent at the end of extraction to do a sweep.
    """
    conflicts: list[Conflict] = []
    for field, candidates in field_candidates.items():
        if len(candidates) < 2:
            continue
        # Compare all pairs
        for i in range(len(candidates)):
            for j in range(i + 1, len(candidates)):
                val_a, src_a = candidates[i]
                val_b, src_b = candidates[j]
                result = detect(field, val_a, src_a, val_b, src_b)
                if result["is_conflict"] and result["conflict_obj"]:
                    # Rebuild typed object
                    c = result["conflict_obj"]
                    conflicts.append(Conflict(
                        field=c["field"],
                        value_a=c["value_a"],
                        source_a=SourceRef(**c["source_a"]),
                        value_b=c["value_b"],
                        source_b=SourceRef(**c["source_b"]),
                    ))
    return conflicts