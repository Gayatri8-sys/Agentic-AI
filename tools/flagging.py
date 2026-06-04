"""
tools/flagging.py

Escalation tool — the agent calls this whenever it finds something
requiring explicit clinician attention:
  - Drug interactions
  - Medication changes with no documented reason
  - Conflicting clinical information between documents
  - Missing required fields after exhaustive search
  - Any safety concern the agent cannot resolve autonomously

Flags are stored in WorkingMemory and surface in the final summary.
"""

from __future__ import annotations
from models import ClinicalFlag, FlagSeverity, WorkingMemory


# ---------------------------------------------------------------------------
# Severity routing
# ---------------------------------------------------------------------------

_SEVERITY_MAP = {
    "critical": FlagSeverity.CRITICAL,
    "warning":  FlagSeverity.WARNING,
    "info":     FlagSeverity.INFO,
}


def escalate(
    memory:   WorkingMemory,
    reason:   str,
    context:  str,
    severity: str = "warning",
) -> dict:
    """
    Agent-facing tool: add a clinician flag to working memory.

    Parameters
    ----------
    memory   : the shared WorkingMemory object
    reason   : short description of the issue (shown in summary header)
    context  : longer supporting context (e.g. the conflicting values)
    severity : "critical" | "warning" | "info"

    Returns
    -------
    dict with the created flag
    """
    sev = _severity_map_lookup(severity)
    memory.add_flag(reason=reason, context=context, severity=sev)
    flag = memory.flags[-1]
    return {"flag_id": flag.flag_id, "severity": sev.value, "reason": reason}


def _severity_map_lookup(s: str) -> FlagSeverity:
    return _SEVERITY_MAP.get(s.lower(), FlagSeverity.WARNING)


# ---------------------------------------------------------------------------
# Convenience wrappers — called internally by the agent for common patterns
# ---------------------------------------------------------------------------

def flag_missing_field(memory: WorkingMemory, field: str) -> None:
    """Mark a required field as missing and add a clinician flag."""
    memory.mark_pending(field)
    memory.add_flag(
        reason=f"Required field not found: '{field}'",
        context=f"All source documents were searched. '{field}' could not be located. "
                f"Please populate this field before finalising.",
        severity=FlagSeverity.WARNING,
    )


def flag_conflict(
    memory:   WorkingMemory,
    field:    str,
    val_a:    str,
    src_a:    str,
    val_b:    str,
    src_b:    str,
) -> None:
    """Flag a contradiction between two source documents."""
    memory.add_flag(
        reason=f"Conflicting values for '{field}'",
        context=(
            f"  Document A ({src_a}): {val_a}\n"
            f"  Document B ({src_b}): {val_b}\n"
            f"  Please reconcile before finalising."
        ),
        severity=FlagSeverity.WARNING,
    )


def flag_ddi(memory: WorkingMemory, drug_a: str, drug_b: str,
             description: str, severity: str = "warning") -> None:
    """Flag a drug–drug interaction."""
    memory.add_flag(
        reason=f"Drug interaction: {drug_a} ↔ {drug_b}",
        context=description,
        severity=_severity_map_lookup(severity),
    )


def flag_med_no_reason(memory: WorkingMemory, drug: str, change_type: str) -> None:
    """Flag a medication change with no documented reason."""
    memory.add_flag(
        reason=f"Medication {change_type} without documented reason: {drug}",
        context=(
            f"'{drug}' was {change_type.lower()} but no reason was found in "
            f"progress notes. Please document the clinical rationale."
        ),
        severity=FlagSeverity.WARNING,
    )


def flag_tool_failure(memory: WorkingMemory, tool_name: str, error: str) -> None:
    """Flag a tool failure that may have left data incomplete."""
    memory.add_flag(
        reason=f"Tool failure — results may be incomplete: {tool_name}",
        context=f"Error: {error}. Manual review of relevant data is advised.",
        severity=FlagSeverity.INFO,
    )