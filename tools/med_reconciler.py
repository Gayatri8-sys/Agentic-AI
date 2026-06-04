"""
tools/med_reconciler.py

Compares admission medications vs discharge medications.
Produces a list of MedChange records:
  - ADDED   (new at discharge)
  - STOPPED (on admission, not at discharge)
  - CHANGED (dose / frequency / route differs)
  - UNCHANGED

For every ADDED / STOPPED / CHANGED, searches progress notes for a documented reason.
If no reason found → reason=None → will be escalated by the agent.
"""

from __future__ import annotations
import json
import os
import re
from difflib import SequenceMatcher

from models import MedChange, ChangeType
from prompts import MED_PARSER


# ---------------------------------------------------------------------------
# LLM-based medication list parser
# ---------------------------------------------------------------------------

def _parse_med_list(raw_text: str) -> list[dict]:
    """
    Extract structured medication list from raw text using LLM.
    Falls back to a regex heuristic if LLM unavailable.
    """
    try:
        from openai import OpenAI
        client = OpenAI(api_key=os.environ["OPENAI_API_KEY"])
        prompt = MED_PARSER.format(text=raw_text[:3000])
        resp = client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[{"role": "user", "content": prompt}],
            temperature=0,
            max_tokens=800,
        )
        raw = resp.choices[0].message.content.strip()
        raw = re.sub(r"```(?:json)?|```", "", raw).strip()
        return json.loads(raw)
    except Exception:
        return _regex_fallback_parser(raw_text)


def _regex_fallback_parser(text: str) -> list[dict]:
    """
    Simple regex fallback — catches common medication line formats.
    E.g. "Metformin 500mg twice daily oral"
    """
    meds = []
    # Common patterns: Drug Name Dose Route Frequency
    pattern = re.compile(
        r"([A-Za-z][A-Za-z\s\-]+?)\s+"           # Drug name
        r"(\d+[\d.]*\s*(?:mg|mcg|g|ml|iu|units?))"  # Dose
        r"(?:\s*(oral|iv|sc|im|topical|inhaled))?"   # Route (optional)
        r"(?:\s*(once|twice|three times|daily|bd|tds|qds|hourly|weekly|prn|as needed))?",
        re.IGNORECASE,
    )
    for line in text.splitlines():
        m = pattern.search(line)
        if m:
            meds.append({
                "name":      m.group(1).strip(),
                "dose":      m.group(2).strip() if m.group(2) else None,
                "route":     m.group(3).strip() if m.group(3) else None,
                "frequency": m.group(4).strip() if m.group(4) else None,
            })
    return meds


# ---------------------------------------------------------------------------
# Name fuzzy matching
# ---------------------------------------------------------------------------

def _name_similarity(a: str, b: str) -> float:
    return SequenceMatcher(None, a.lower().strip(), b.lower().strip()).ratio()


def _find_match(drug_name: str, candidates: list[dict], threshold: float = 0.75) -> dict | None:
    best_score, best_match = 0.0, None
    for c in candidates:
        score = _name_similarity(drug_name, c.get("name", ""))
        if score > best_score:
            best_score, best_match = score, c
    return best_match if best_score >= threshold else None


# ---------------------------------------------------------------------------
# Reason lookup — searches progress notes for documented reason
# ---------------------------------------------------------------------------

def _find_reason(drug_name: str, change_type: str, progress_note_text: str) -> str | None:
    """
    Simple heuristic: look for the drug name near reason-indicating phrases.
    Returns the reason text if found, None otherwise.
    """
    if not progress_note_text:
        return None

    drug_lower = drug_name.lower()
    text_lower = progress_note_text.lower()

    reason_phrases = [
        "stopped because", "stopped due to", "discontinued due to", "discontinued because",
        "added for", "started for", "started due to", "initiated for", "commenced for",
        "changed to", "switched to", "dose reduced", "dose increased",
        "due to", "because of", "for management of", "for treatment of",
    ]

    # Find lines that mention the drug
    for line in progress_note_text.splitlines():
        if drug_lower in line.lower():
            for phrase in reason_phrases:
                if phrase in line.lower():
                    # Extract the reason clause (30 chars after the phrase)
                    idx = line.lower().find(phrase)
                    reason = line[idx:idx + 120].strip()
                    return reason

    return None


# ---------------------------------------------------------------------------
# Main reconciler
# ---------------------------------------------------------------------------

def reconcile(
    admit_text:    str,
    dc_text:       str,
    progress_text: str = "",
) -> dict:
    """
    Agent-facing tool: reconcile admission vs discharge medication lists.

    Returns:
      {
        "changes":        list[dict],   # MedChange objects serialised
        "admit_list":     list[dict],
        "dc_list":        list[dict],
        "status":         "ok" | "error",
        "needs_review":   list[str],    # drug names needing clinician review
      }
    """
    try:
        admit_meds = _parse_med_list(admit_text) if admit_text.strip() else []
        dc_meds    = _parse_med_list(dc_text)    if dc_text.strip()    else []

        changes: list[MedChange] = []
        needs_review: list[str] = []

        # --- Mark STOPPED or CHANGED drugs ---
        for admit_med in admit_meds:
            name = admit_med.get("name", "")
            dc_match = _find_match(name, dc_meds)
            if dc_match is None:
                # STOPPED
                reason = _find_reason(name, "stopped", progress_text)
                mc = MedChange(
                    drug=name,
                    change_type=ChangeType.STOPPED,
                    admit_dose=admit_med.get("dose"),
                    dc_dose=None,
                    reason=reason,
                )
                changes.append(mc)
                if reason is None:
                    needs_review.append(name)
            else:
                # Check if dose/route/frequency changed
                dose_same  = _name_similarity(
                    str(admit_med.get("dose") or ""),
                    str(dc_match.get("dose") or ""),
                ) > 0.85
                freq_same  = _name_similarity(
                    str(admit_med.get("frequency") or ""),
                    str(dc_match.get("frequency") or ""),
                ) > 0.85

                if not dose_same or not freq_same:
                    reason = _find_reason(name, "changed", progress_text)
                    mc = MedChange(
                        drug=name,
                        change_type=ChangeType.CHANGED,
                        admit_dose=admit_med.get("dose"),
                        dc_dose=dc_match.get("dose"),
                        reason=reason,
                    )
                    changes.append(mc)
                    if reason is None:
                        needs_review.append(name)
                else:
                    changes.append(MedChange(
                        drug=name,
                        change_type=ChangeType.UNCHANGED,
                        admit_dose=admit_med.get("dose"),
                        dc_dose=dc_match.get("dose"),
                    ))

        # --- Mark ADDED drugs ---
        for dc_med in dc_meds:
            name = dc_med.get("name", "")
            admit_match = _find_match(name, admit_meds)
            if admit_match is None:
                reason = _find_reason(name, "added", progress_text)
                mc = MedChange(
                    drug=name,
                    change_type=ChangeType.ADDED,
                    admit_dose=None,
                    dc_dose=dc_med.get("dose"),
                    reason=reason,
                )
                changes.append(mc)
                if reason is None:
                    needs_review.append(name)

        return {
            "changes":      [c.model_dump() for c in changes],
            "admit_list":   admit_meds,
            "dc_list":      dc_meds,
            "status":       "ok",
            "needs_review": needs_review,
        }

    except Exception as exc:
        return {
            "changes":      [],
            "admit_list":   [],
            "dc_list":      [],
            "status":       "error",
            "error":        str(exc),
            "needs_review": [],
        }