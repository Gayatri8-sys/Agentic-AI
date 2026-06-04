"""
tools/drug_checker.py

Mock drug-drug interaction (DDI) checker.
Uses a hardcoded table of clinically relevant interactions.
In production: swap _MOCK_DDI_TABLE for a call to RxNorm or DrugBank API.

The agent calls this whenever a discharge medication list is compiled.
"""

from __future__ import annotations
from models import DDIAlert, FlagSeverity


# ---------------------------------------------------------------------------
# Mock DDI table — (drug_a_lower, drug_b_lower) → (severity, description)
# Pairs are order-independent. All drug names lowercase.
# ---------------------------------------------------------------------------

_MOCK_DDI_TABLE: dict[tuple[str, str], tuple[FlagSeverity, str]] = {
    ("warfarin",      "aspirin"):         (FlagSeverity.CRITICAL, "Increased bleeding risk — both inhibit coagulation via different mechanisms."),
    ("warfarin",      "ibuprofen"):       (FlagSeverity.CRITICAL, "Serious bleeding risk — NSAIDs displace warfarin from protein binding."),
    ("warfarin",      "amoxicillin"):     (FlagSeverity.WARNING,  "Antibiotics may alter gut flora, affecting vitamin K absorption and INR."),
    ("metformin",     "contrast dye"):    (FlagSeverity.WARNING,  "Hold metformin 48h before/after iodinated contrast to prevent lactic acidosis."),
    ("lisinopril",    "potassium"):       (FlagSeverity.WARNING,  "ACE inhibitors raise serum potassium — monitor electrolytes."),
    ("lisinopril",    "spironolactone"):  (FlagSeverity.CRITICAL, "High risk of hyperkalemia — dual potassium-sparing agents."),
    ("simvastatin",   "amiodarone"):      (FlagSeverity.CRITICAL, "Increased risk of myopathy/rhabdomyolysis — CYP3A4 inhibition raises simvastatin levels."),
    ("clopidogrel",   "omeprazole"):      (FlagSeverity.WARNING,  "CYP2C19 inhibition reduces clopidogrel antiplatelet effect."),
    ("ssri",          "tramadol"):        (FlagSeverity.CRITICAL, "Risk of serotonin syndrome."),
    ("fluoxetine",    "tramadol"):        (FlagSeverity.CRITICAL, "Risk of serotonin syndrome — CYP2D6 inhibition also raises tramadol levels."),
    ("metoprolol",    "verapamil"):       (FlagSeverity.CRITICAL, "Additive bradycardia and heart block risk."),
    ("digoxin",       "amiodarone"):      (FlagSeverity.CRITICAL, "Amiodarone raises digoxin levels — reduce digoxin dose and monitor closely."),
    ("ciprofloxacin", "antacids"):        (FlagSeverity.WARNING,  "Divalent cations (Mg, Ca, Al) chelate ciprofloxacin, reducing absorption."),
    ("heparin",       "warfarin"):        (FlagSeverity.INFO,     "Concurrent use expected during bridging — monitor INR."),
    ("nsaid",         "antihypertensive"):(FlagSeverity.WARNING,  "NSAIDs can blunt antihypertensive effect and worsen renal function."),
    ("insulin",       "beta-blocker"):    (FlagSeverity.WARNING,  "Beta-blockers may mask hypoglycaemia symptoms."),
}


def _normalise(name: str) -> str:
    """Strip common dose/route suffixes so 'warfarin 5mg' matches 'warfarin'."""
    import re
    name = name.lower().strip()
    name = re.sub(r"\s+\d+[\d.]*\s*(mg|mcg|g|ml|iu|units?|%)?", "", name)
    name = re.sub(r"\s+(tablet|capsule|injection|infusion|solution|oral|iv|sc|im|patch|cream|drops?)s?$", "", name)
    return name.strip()


def _partial_match(drug: str, key_part: str) -> bool:
    """True if key_part appears anywhere in drug name (for family matching)."""
    return key_part in drug


def check_interactions(drug_list: list[str]) -> list[DDIAlert]:
    """
    Check a list of drug names for known interactions.

    Returns a (possibly empty) list of DDIAlert.
    This is a mock — real implementation would call RxNorm:
      POST https://rxnav.nlm.nih.gov/REST/interaction/list.json
    """
    if not drug_list:
        return []

    normalised = [_normalise(d) for d in drug_list]
    alerts: list[DDIAlert] = []
    seen: set[tuple[str, str]] = set()

    for i in range(len(normalised)):
        for j in range(i + 1, len(normalised)):
            a, b = normalised[i], normalised[j]
            # Try exact pair
            pair = tuple(sorted([a, b]))
            if pair in seen:
                continue
            seen.add(pair)

            # Lookup in both orders
            hit = _MOCK_DDI_TABLE.get((a, b)) or _MOCK_DDI_TABLE.get((b, a))

            # Partial / family match if no exact hit
            if hit is None:
                for (ka, kb), v in _MOCK_DDI_TABLE.items():
                    if (_partial_match(a, ka) and _partial_match(b, kb)) or \
                       (_partial_match(a, kb) and _partial_match(b, ka)):
                        hit = v
                        break

            if hit:
                severity, description = hit
                alerts.append(DDIAlert(
                    drug_a=drug_list[i],
                    drug_b=drug_list[j],
                    severity=severity,
                    description=description,
                ))

    return alerts


# ---------------------------------------------------------------------------
# Tool wrapper called by the agent
# ---------------------------------------------------------------------------

def run(drug_list: list[str]) -> dict:
    """
    Agent-facing tool interface.
    Returns {"alerts": [...], "count": int, "status": "ok" | "empty"}
    """
    if not drug_list:
        return {"alerts": [], "count": 0, "status": "empty"}

    try:
        alerts = check_interactions(drug_list)
        return {
            "alerts": [a.model_dump() for a in alerts],
            "count":  len(alerts),
            "status": "ok",
        }
    except Exception as exc:
        return {"alerts": [], "count": 0, "status": "error", "error": str(exc)}