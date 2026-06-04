"""
Pydantic models for the Discharge Summary Agent.
All facts in WorkingMemory must carry source attribution — no naked claims.
"""

from __future__ import annotations
from enum import Enum
from typing import ClassVar, Optional
from pydantic import BaseModel, field_validator


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------

class DocType(str, Enum):
    ADMISSION_NOTE    = "admission_note"
    PROGRESS_NOTE     = "progress_note"
    LAB_RESULT        = "lab_result"
    MEDICATION_RECORD = "medication_record"
    UNKNOWN           = "unknown"


class ChangeType(str, Enum):
    ADDED     = "ADDED"
    STOPPED   = "STOPPED"
    CHANGED   = "CHANGED"
    UNCHANGED = "UNCHANGED"


class FlagSeverity(str, Enum):
    INFO     = "INFO"
    WARNING  = "WARNING"
    CRITICAL = "CRITICAL"


# ---------------------------------------------------------------------------
# Source attribution — every clinical fact must have one
# ---------------------------------------------------------------------------

class SourceRef(BaseModel):
    doc_id:   str
    doc_type: DocType
    page_num: int


# ---------------------------------------------------------------------------
# A sourced clinical claim
# ---------------------------------------------------------------------------

class ClaimWithSource(BaseModel):
    value:  str
    source: SourceRef

    def __str__(self) -> str:
        return (
            f"{self.value}  "
            f"[source: {self.source.doc_id}, p.{self.source.page_num}]"
        )


# ---------------------------------------------------------------------------
# Conflicts between two notes
# ---------------------------------------------------------------------------

class Conflict(BaseModel):
    field:    str
    value_a:  str
    source_a: SourceRef
    value_b:  str
    source_b: SourceRef

    def display(self) -> str:
        return (
            f"CONFLICT on '{self.field}':\n"
            f"  * {self.value_a}  [{self.source_a.doc_id}, p.{self.source_a.page_num}]\n"
            f"  * {self.value_b}  [{self.source_b.doc_id}, p.{self.source_b.page_num}]"
        )


# ---------------------------------------------------------------------------
# Medication change record
# ---------------------------------------------------------------------------

class MedChange(BaseModel):
    drug:        str
    change_type: ChangeType
    admit_dose:  Optional[str] = None
    dc_dose:     Optional[str] = None
    reason:      Optional[str] = None

    def display(self) -> str:
        reason_str = self.reason if self.reason else "[!] REASON MISSING - clinician review required"
        if self.change_type == ChangeType.ADDED:
            return f"  + ADDED:   {self.drug} ({self.dc_dose})  |  Reason: {reason_str}"
        elif self.change_type == ChangeType.STOPPED:
            return f"  - STOPPED: {self.drug} ({self.admit_dose})  |  Reason: {reason_str}"
        elif self.change_type == ChangeType.CHANGED:
            return f"  ~ CHANGED: {self.drug}  {self.admit_dose} -> {self.dc_dose}  |  Reason: {reason_str}"
        else:
            return f"    UNCHANGED: {self.drug} ({self.admit_dose})"


# ---------------------------------------------------------------------------
# Drug interaction alert
# ---------------------------------------------------------------------------

class DDIAlert(BaseModel):
    drug_a:      str
    drug_b:      str
    severity:    FlagSeverity
    description: str


# ---------------------------------------------------------------------------
# Clinical escalation flag
# ---------------------------------------------------------------------------

class ClinicalFlag(BaseModel):
    flag_id:  int
    reason:   str
    context:  str
    severity: FlagSeverity = FlagSeverity.WARNING

    def display(self) -> str:
        return (
            f"[{self.severity.value}] Flag #{self.flag_id}: {self.reason}\n"
            f"  Context: {self.context}"
        )


# ---------------------------------------------------------------------------
# Working memory — single source of truth across the agent loop
# ---------------------------------------------------------------------------

class WorkingMemory(BaseModel):
    patient_id:    str
    facts:         dict[str, ClaimWithSource] = {}
    pending:       list[str] = []
    conflicts:     list[Conflict] = []
    flags:         list[ClinicalFlag] = []
    med_changes:   list[MedChange] = []
    step_count:    int = 0
    _flag_counter: int = 0

    class Config:
        arbitrary_types_allowed = True

    def add_fact(self, field: str, value: str, source: SourceRef) -> None:
        if field in self.facts:
            existing = self.facts[field]
            if existing.value.strip().lower() != value.strip().lower():
                conflict = Conflict(
                    field=field,
                    value_a=existing.value,
                    source_a=existing.source,
                    value_b=value,
                    source_b=source,
                )
                self.conflicts.append(conflict)
                del self.facts[field]
                return
        self.facts[field] = ClaimWithSource(value=value, source=source)

    def get_fact(self, field: str) -> Optional[str]:
        claim = self.facts.get(field)
        return claim.value if claim else None

    def mark_pending(self, field: str) -> None:
        if field not in self.pending:
            self.pending.append(field)

    def is_conflicted(self, field: str) -> bool:
        return any(c.field == field for c in self.conflicts)

    def add_flag(self, reason: str, context: str,
                 severity: FlagSeverity = FlagSeverity.WARNING) -> None:
        self._flag_counter += 1
        self.flags.append(ClinicalFlag(
            flag_id=self._flag_counter,
            reason=reason,
            context=context,
            severity=severity,
        ))

    def increment_step(self) -> int:
        self.step_count += 1
        return self.step_count


# ---------------------------------------------------------------------------
# Structured discharge summary (final output)
# ---------------------------------------------------------------------------

class DischargeSummary(BaseModel):
    # ClassVar tells Pydantic these are NOT model fields — just class constants
    MISSING_SENTINEL:  ClassVar[str] = "[MISSING] clinician review required"
    CONFLICT_SENTINEL: ClassVar[str] = "[CONFLICT] see clinician flags"

    patient_id:              str
    patient_demographics:    str
    admission_date:          str
    discharge_date:          str
    principal_diagnosis:     str
    secondary_diagnoses:     str
    hospital_course:         str
    procedures:              str
    discharge_medications:   str
    allergies:               str
    follow_up_instructions:  str
    pending_results:         str
    discharge_condition:     str
    clinician_flags:         list[str] = []
    is_draft:                bool = True


# ---------------------------------------------------------------------------
# Trace entry (one per agent step)
# ---------------------------------------------------------------------------

class TraceEntry(BaseModel):
    step:          int
    reasoning:     str
    tool:          str
    inputs:        dict
    raw_output:    str
    memory_delta:  str
    next_decision: str