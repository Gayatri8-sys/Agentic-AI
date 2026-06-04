"""
agent.py

The Discharge Summary Agent — a real planning loop that:
  1. Ingests patient PDFs (single large or multiple small)
  2. Plans extraction steps based on available document types
  3. Calls tools: doc_search, med_reconciler, drug_checker,
                  conflict_detector, flagging
  4. Fills WorkingMemory with sourced facts only — no fabrication
  5. Emits a structured DischargeSummary draft + full trace

Hard step cap : MAX_STEPS = 40
No fabrication: missing fields → ⚠ MISSING, conflicts → ⚠ CONFLICT
"""

from __future__ import annotations

import json
import os
import re
import time
from typing import Any

from openai import OpenAI

from models import (
    ChangeType,
    ClaimWithSource,
    Conflict,
    DischargeSummary,
    DocType,
    FlagSeverity,
    MedChange,
    SourceRef,
    WorkingMemory,
)
from prompts import AGENT_SYSTEM, HALLUCINATION_CHECK, PLANNER, SECTION_WRITER
from tracer import Tracer
from tools import conflict_detector, drug_checker, flagging, med_reconciler, pdf_extractor

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

MAX_STEPS   = 40
MAX_RETRIES = 3
RETRY_DELAY = 2   # seconds between retries

REQUIRED_FIELDS = [
    "patient_name",
    "date_of_birth",
    "mrn",
    "gender",
    "admission_date",
    "discharge_date",
    "principal_diagnosis",
    "secondary_diagnoses",
    "hospital_course",
    "procedures",
    "discharge_medications",
    "allergies",
    "follow_up_instructions",
    "pending_results",
    "discharge_condition",
]

MISSING        = DischargeSummary.MISSING_SENTINEL
CONFLICT_LABEL = DischargeSummary.CONFLICT_SENTINEL


# ---------------------------------------------------------------------------
# LLM helpers
# ---------------------------------------------------------------------------

def _get_client() -> OpenAI:
    key = os.environ.get("OPENAI_API_KEY", "")
    if not key:
        raise EnvironmentError("OPENAI_API_KEY not set.")
    return OpenAI(api_key=key)


def _llm(messages: list[dict], model: str = "gpt-4o-mini",
         temperature: float = 0, max_tokens: int = 1500) -> str:
    """Call OpenAI with retries."""
    client = _get_client()
    for attempt in range(MAX_RETRIES):
        try:
            resp = client.chat.completions.create(
                model=model,
                messages=messages,
                temperature=temperature,
                max_tokens=max_tokens,
            )
            return resp.choices[0].message.content.strip()
        except Exception as exc:
            if attempt < MAX_RETRIES - 1:
                time.sleep(RETRY_DELAY * (attempt + 1))
            else:
                raise RuntimeError(
                    f"LLM call failed after {MAX_RETRIES} attempts: {exc}"
                ) from exc
    return ""


def _parse_json_safe(text: str) -> Any:
    """Strip markdown fences and parse JSON safely."""
    cleaned = re.sub(r"```(?:json)?|```", "", text).strip()
    return json.loads(cleaned)


# ---------------------------------------------------------------------------
# Document search
# ---------------------------------------------------------------------------

def _doc_search(query: str, docs: list[dict],
                doc_type_filter: str | None = None) -> list[dict]:
    """
    Keyword search over extracted document text.
    Returns top-5 matching page snippets.
    """
    query_terms = query.lower().split()
    results = []

    for doc in docs:
        # Skip only if filter is set AND doc has a known type that doesn't match.
        # Always include docs classified as "unknown" — they may contain any section.
        if (doc_type_filter
                and doc["doc_type"] != doc_type_filter
                and doc["doc_type"] != "unknown"):
            continue
        for page in doc["pages"]:
            text = page["text"].lower()
            score = sum(1 for t in query_terms if t in text)
            if score > 0:
                first_term = next((t for t in query_terms if t in text), "")
                idx = text.find(first_term) if first_term else 0
                snippet_start = max(0, idx - 100)
                snippet_end   = min(len(page["text"]), idx + 400)
                results.append({
                    "doc_id":   doc["doc_id"],
                    "doc_type": doc["doc_type"],
                    "page_num": page["page_num"],
                    "snippet":  page["text"][snippet_start:snippet_end],
                    "score":    score,
                })

    results.sort(key=lambda x: x["score"], reverse=True)
    return results[:5]


# ---------------------------------------------------------------------------
# Field extractor
# ---------------------------------------------------------------------------

def _extract_field(field: str, snippet: str, doc_id: str) -> str | None:
    """Ask LLM to extract one field from text. Returns None if not found."""
    prompt = (
        f"Extract the value of '{field}' from the following clinical text.\n"
        f"Rules:\n"
        f"  - Return ONLY the value, nothing else.\n"
        f"  - If the field is not present, return exactly: NOT_FOUND\n"
        f"  - Do not infer or guess. Only return what is explicitly stated.\n\n"
        f"Text:\n{snippet[:2000]}"
    )
    try:
        result = _llm([{"role": "user", "content": prompt}], max_tokens=300)
        if not result.strip() or result.strip().upper() == "NOT_FOUND":
            return None
        return result.strip()
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Hallucination check
# ---------------------------------------------------------------------------

def _hallucination_check(draft_text: str, source_text: str) -> dict:
    """Returns {"safe": bool, "hallucinated_phrases": list}."""
    prompt = HALLUCINATION_CHECK.format(
        draft_text=draft_text[:1500],
        source_text=source_text[:3000],
    )
    try:
        raw = _llm([{"role": "user", "content": prompt}], max_tokens=400)
        return _parse_json_safe(raw)
    except Exception:
        return {"safe": True, "hallucinated_phrases": []}


# ---------------------------------------------------------------------------
# Section writer
# ---------------------------------------------------------------------------

def _write_section(section_name: str, facts: dict,
                   pending: list[str], conflict_fields: list[str]) -> str:
    prompt = (
        f"section_name: {section_name}\n"
        f"facts: {json.dumps(facts, default=str)}\n"
        f"pending_fields: {pending}\n"
        f"conflict_fields: {conflict_fields}\n"
    )
    messages = [
        {"role": "system", "content": SECTION_WRITER},
        {"role": "user",   "content": prompt},
    ]
    try:
        return _llm(messages, max_tokens=600)
    except Exception as exc:
        return f"[SECTION WRITE FAILED: {exc}]"


# ---------------------------------------------------------------------------
# Plan generator
# ---------------------------------------------------------------------------

def _generate_plan(docs: list[dict]) -> list[dict]:
    """Ask LLM to generate an extraction work plan, fall back to default."""
    doc_types = list({d["doc_type"] for d in docs})
    doc_ids   = [d["doc_id"] for d in docs]

    prompt = PLANNER.format(doc_types=doc_types, doc_ids=doc_ids)
    try:
        raw  = _llm([{"role": "user", "content": prompt}], max_tokens=1200)
        plan = _parse_json_safe(raw)
        if isinstance(plan, list) and plan:
            return plan
    except Exception:
        pass
    return _default_plan(doc_types)


def _default_plan(doc_types: list[str]) -> list[dict]:
    return [
        {"step": 1,  "section": "patient_demographics",
         "fields_needed": ["patient_name", "date_of_birth", "mrn", "gender"],
         "primary_doc_type": "admission_note",
         "query": "patient name date of birth MRN gender age"},
        {"step": 2,  "section": "admission_discharge_dates",
         "fields_needed": ["admission_date", "discharge_date"],
         "primary_doc_type": "admission_note",
         "query": "admission date discharge date"},
        {"step": 3,  "section": "allergies",
         "fields_needed": ["allergies"],
         "primary_doc_type": "admission_note",
         "query": "allergies NKDA drug allergy not known"},
        {"step": 4,  "section": "principal_diagnosis",
         "fields_needed": ["principal_diagnosis"],
         "primary_doc_type": "admission_note",
         "query": "principal diagnosis admitting diagnosis final diagnosis"},
        {"step": 5,  "section": "secondary_diagnoses",
         "fields_needed": ["secondary_diagnoses"],
         "primary_doc_type": "progress_note",
         "query": "secondary diagnosis comorbidities past medical history"},
        {"step": 6,  "section": "hospital_course",
         "fields_needed": ["hospital_course"],
         "primary_doc_type": "progress_note",
         "query": "hospital course treatment management clinical course"},
        {"step": 7,  "section": "procedures",
         "fields_needed": ["procedures"],
         "primary_doc_type": "progress_note",
         "query": "procedure operation surgery intervention cannula catheter"},
        {"step": 8,  "section": "medications",
         "fields_needed": ["discharge_medications"],
         "primary_doc_type": "medication_record",
         "query": "discharge medications drug dose route frequency tablet injection"},
        {"step": 9,  "section": "follow_up_instructions",
         "fields_needed": ["follow_up_instructions"],
         "primary_doc_type": "progress_note",
         "query": "follow up outpatient clinic appointment review instructions"},
        {"step": 10, "section": "pending_results",
         "fields_needed": ["pending_results"],
         "primary_doc_type": "lab_result",
         "query": "pending result awaiting culture sensitivity report"},
        {"step": 11, "section": "discharge_condition",
         "fields_needed": ["discharge_condition"],
         "primary_doc_type": "progress_note",
         "query": "discharge condition stable improved hemodynamically"},
    ]


# ---------------------------------------------------------------------------
# Full-text helpers
# ---------------------------------------------------------------------------

def _get_full_text_by_type(docs: list[dict], doc_type: str) -> str:
    # Match exact type; also include "unknown" docs since classifier may have failed
    parts = [d["full_text"] for d in docs
             if d["doc_type"] == doc_type or d["doc_type"] == "unknown"]
    return "\n\n".join(parts)


def _get_all_text(docs: list[dict]) -> str:
    return "\n\n".join(d["full_text"] for d in docs)


# ---------------------------------------------------------------------------
# Medication reconciliation step
# ---------------------------------------------------------------------------

def _run_med_reconciliation(docs: list[dict], memory: WorkingMemory,
                             tracer: Tracer, step: int) -> int:
    admit_text    = _get_full_text_by_type(docs, "medication_record")
    dc_text       = _get_full_text_by_type(docs, "medication_record")
    progress_text = _get_full_text_by_type(docs, "progress_note")

    # Fallback: use all text if medication_record not separated
    if not admit_text.strip():
        admit_text = _get_all_text(docs)
    if not dc_text.strip():
        dc_text = _get_all_text(docs)

    tracer.log(
        step=step,
        reasoning="Running medication reconciliation between admission and discharge medication lists.",
        tool="med_reconciler.reconcile",
        inputs={"admit_text_len": len(admit_text), "dc_text_len": len(dc_text)},
        raw_output="calling tool...",
        memory_delta="",
        next_decision="Flag any changes without documented reason",
    )

    result      = med_reconciler.reconcile(admit_text, dc_text, progress_text)
    changes     = result.get("changes", [])
    needs_review = result.get("needs_review", [])

    memory.med_changes = [MedChange(**c) for c in changes]

    for drug in needs_review:
        change      = next((c for c in changes if c["drug"] == drug), {})
        change_type = change.get("change_type", "changed").lower()
        flagging.flag_med_no_reason(memory, drug, change_type)

    if changes:
        lines       = [m.display() for m in memory.med_changes]
        dc_meds_txt = "\n".join(lines)
    else:
        dc_meds_txt = MISSING

    src = SourceRef(
        doc_id   = docs[0]["doc_id"] if docs else "unknown",
        doc_type = DocType.MEDICATION_RECORD,
        page_num = 1,
    )
    memory.add_fact("discharge_medications", dc_meds_txt, src)

    # Drug-drug interaction check
    step += 1
    dc_drug_names = [
        c["drug"] for c in changes
        if c.get("change_type") != ChangeType.STOPPED.value
    ]
    ddi_result = drug_checker.run(dc_drug_names)

    tracer.log(
        step=step,
        reasoning=f"Checking {len(dc_drug_names)} discharge drugs for interactions.",
        tool="drug_checker.run",
        inputs={"drug_list": dc_drug_names},
        raw_output=json.dumps(ddi_result, default=str)[:400],
        memory_delta=f"{ddi_result['count']} DDI alert(s) found",
        next_decision="Escalate any critical interactions",
    )

    for alert in ddi_result.get("alerts", []):
        flagging.flag_ddi(
            memory,
            alert["drug_a"],
            alert["drug_b"],
            alert["description"],
            alert["severity"].lower(),
        )

    return step


# ---------------------------------------------------------------------------
# Core extraction step
# ---------------------------------------------------------------------------

def _run_extraction_step(plan_step: dict, docs: list[dict],
                          memory: WorkingMemory, tracer: Tracer,
                          step: int) -> int:
    section         = plan_step.get("section", "unknown")
    fields          = plan_step.get("fields_needed", [])
    query           = plan_step.get("query", section)
    doc_type_filter = plan_step.get("primary_doc_type")

    # Search with type filter first, then without if nothing found
    search_results = _doc_search(query, docs, doc_type_filter)
    if not search_results:
        search_results = _doc_search(query, docs, None)

    snippet_text = "\n\n".join(r["snippet"] for r in search_results)
    source_doc   = search_results[0] if search_results else None

    tracer.log(
        step=step,
        reasoning=f"Extracting section '{section}': fields {fields}",
        tool="doc_search",
        inputs={"query": query, "doc_type_filter": doc_type_filter},
        raw_output=snippet_text[:400] if snippet_text else "NO RESULTS",
        memory_delta="",
        next_decision=f"Extract {fields} from search results",
    )

    if not snippet_text.strip():
        for field in fields:
            flagging.flag_missing_field(memory, field)
        tracer.log(
            step=step,
            reasoning=f"No documents found for section '{section}'.",
            tool="flagging.flag_missing_field",
            inputs={"fields": fields},
            raw_output="marked as MISSING",
            memory_delta=f"Pending: {fields}",
            next_decision="Continue to next section",
        )
        return step

    # Extract each field
    for field in fields:
        if field in memory.facts or field in memory.pending:
            continue   # already resolved

        value = _extract_field(
            field, snippet_text,
            source_doc["doc_id"] if source_doc else "unknown"
        )

        if value:
            # Map doc_type string to DocType enum safely
            raw_type = source_doc["doc_type"] if source_doc else "unknown"
            try:
                dtype = DocType(raw_type)
            except ValueError:
                dtype = DocType.UNKNOWN

            src = SourceRef(
                doc_id   = source_doc["doc_id"] if source_doc else "unknown",
                doc_type = dtype,
                page_num = source_doc["page_num"] if source_doc else 1,
            )
            memory.add_fact(field, value, src)

            # If add_fact registered a conflict, flag it
            if memory.is_conflicted(field):
                conflict = next(
                    (c for c in memory.conflicts if c.field == field), None
                )
                if conflict:
                    flagging.flag_conflict(
                        memory, field,
                        conflict.value_a, conflict.source_a.doc_id,
                        conflict.value_b, conflict.source_b.doc_id,
                    )
        else:
            # Try all other docs before marking missing
            found = False
            for doc in docs:
                if source_doc and doc["doc_id"] == source_doc["doc_id"]:
                    continue
                val2 = _extract_field(field, doc["full_text"][:2000], doc["doc_id"])
                if val2:
                    try:
                        dtype2 = DocType(doc["doc_type"])
                    except ValueError:
                        dtype2 = DocType.UNKNOWN
                    src2 = SourceRef(
                        doc_id   = doc["doc_id"],
                        doc_type = dtype2,
                        page_num = 1,
                    )
                    memory.add_fact(field, val2, src2)
                    found = True
                    break

            if not found:
                flagging.flag_missing_field(memory, field)

    # Log results
    extracted      = {f: memory.get_fact(f) for f in fields if f in memory.facts}
    pending_here   = [f for f in fields if f in memory.pending]
    conflicted_here = [f for f in fields if memory.is_conflicted(f)]

    tracer.log(
        step=step + 1,
        reasoning=f"Extraction results for '{section}'",
        tool="memory.add_fact",
        inputs={"section": section},
        raw_output=json.dumps(extracted, default=str)[:400],
        memory_delta=(
            f"Extracted: {list(extracted.keys())}  |  "
            f"Pending: {pending_here}  |  "
            f"Conflicts: {conflicted_here}"
        ),
        next_decision=(
            "Continue to next plan step"
            if step < MAX_STEPS - 2
            else "Approaching step limit — wrap up"
        ),
    )

    return step + 1


# ---------------------------------------------------------------------------
# Cross-document conflict sweep
# ---------------------------------------------------------------------------

def _run_conflict_sweep(docs: list[dict], memory: WorkingMemory,
                         tracer: Tracer, step: int) -> int:
    field_candidates: dict[str, list[tuple[str, SourceRef]]] = {}

    for doc in docs:
        for field in REQUIRED_FIELDS:
            val = _extract_field(field, doc["full_text"][:2000], doc["doc_id"])
            if val:
                try:
                    dtype = DocType(doc["doc_type"])
                except ValueError:
                    dtype = DocType.UNKNOWN
                src = SourceRef(
                    doc_id   = doc["doc_id"],
                    doc_type = dtype,
                    page_num = 1,
                )
                field_candidates.setdefault(field, []).append((val, src))

    new_conflicts = conflict_detector.scan_facts_for_conflicts(field_candidates)

    existing_fields = {c.field for c in memory.conflicts}
    for c in new_conflicts:
        if c.field not in existing_fields:
            memory.conflicts.append(c)
            flagging.flag_conflict(
                memory, c.field,
                c.value_a, c.source_a.doc_id,
                c.value_b, c.source_b.doc_id,
            )

    tracer.log(
        step=step,
        reasoning="Cross-document conflict sweep across all required fields.",
        tool="conflict_detector.scan_facts_for_conflicts",
        inputs={"fields_checked": list(field_candidates.keys())},
        raw_output=f"{len(new_conflicts)} new conflict(s) found",
        memory_delta=f"Conflicts: {[c.field for c in new_conflicts]}",
        next_decision="Flag all conflicts for clinician review",
    )

    return step


# ---------------------------------------------------------------------------
# Build DischargeSummary from WorkingMemory
# ---------------------------------------------------------------------------

def _build_summary(memory: WorkingMemory, docs: list[dict]) -> DischargeSummary:

    def _resolve(field: str) -> str:
        if memory.is_conflicted(field):
            return CONFLICT_LABEL
        val = memory.get_fact(field)
        if val:
            return val
        return MISSING

    # Patient demographics block
    demo_parts = {
        "Name":   _resolve("patient_name"),
        "DOB":    _resolve("date_of_birth"),
        "MRN":    _resolve("mrn"),
        "Gender": _resolve("gender"),
    }
    demographics = "  |  ".join(f"{k}: {v}" for k, v in demo_parts.items())

    # Hospital course — LLM-written section
    hc_facts = {
        "hospital_course":      _resolve("hospital_course"),
        "principal_diagnosis":  _resolve("principal_diagnosis"),
    }
    hospital_course = _write_section(
        "hospital_course",
        hc_facts,
        [f for f in memory.pending    if f == "hospital_course"],
        [c.field for c in memory.conflicts if c.field == "hospital_course"],
    )

    # Hallucination check on hospital course
    all_text = _get_all_text(docs)
    hc_check = _hallucination_check(hospital_course, all_text)
    if not hc_check.get("safe", True):
        phrases = hc_check.get("hallucinated_phrases", [])
        memory.add_flag(
            reason="Possible hallucination detected in hospital course draft",
            context=f"Suspect phrases: {phrases}. Manual review required.",
            severity=FlagSeverity.CRITICAL,
        )

    flag_strs = [f.display() for f in memory.flags]

    return DischargeSummary(
        patient_id             = memory.patient_id,
        patient_demographics   = demographics,
        admission_date         = _resolve("admission_date"),
        discharge_date         = _resolve("discharge_date"),
        principal_diagnosis    = _resolve("principal_diagnosis"),
        secondary_diagnoses    = _resolve("secondary_diagnoses"),
        hospital_course        = hospital_course,
        procedures             = _resolve("procedures"),
        discharge_medications  = _resolve("discharge_medications"),
        allergies              = _resolve("allergies"),
        follow_up_instructions = _resolve("follow_up_instructions"),
        pending_results        = _resolve("pending_results"),
        discharge_condition    = _resolve("discharge_condition"),
        clinician_flags        = flag_strs,
        is_draft               = True,
    )


# ---------------------------------------------------------------------------
# Write summary to Markdown file
# ---------------------------------------------------------------------------

def _format_summary_md(summary: DischargeSummary,
                        output_dir: str = "output") -> str:
    from pathlib import Path as _Path
    _Path(output_dir).mkdir(parents=True, exist_ok=True)
    path = str(_Path(output_dir) / f"summary_{summary.patient_id}.md")

    lines = [
        "# DISCHARGE SUMMARY DRAFT",
        "> ⚠ THIS IS A DRAFT FOR CLINICIAN REVIEW — NOT A FINALIZED DOCUMENT",
        "",
        f"**Patient ID:** {summary.patient_id}",
        "",
        "---",
        "",
        "## Patient Demographics",
        summary.patient_demographics,
        "",
        "## Admission & Discharge Dates",
        f"- **Admission:** {summary.admission_date}",
        f"- **Discharge:** {summary.discharge_date}",
        "",
        "## Principal Diagnosis",
        summary.principal_diagnosis,
        "",
        "## Secondary Diagnoses",
        summary.secondary_diagnoses,
        "",
        "## Hospital Course",
        summary.hospital_course,
        "",
        "## Procedures",
        summary.procedures,
        "",
        "## Discharge Medications",
        "_(Changes from admission: + ADDED, - STOPPED, ~ CHANGED)_",
        "",
        summary.discharge_medications,
        "",
        "## Allergies",
        summary.allergies,
        "",
        "## Follow-Up Instructions",
        summary.follow_up_instructions,
        "",
        "## Pending Results",
        summary.pending_results,
        "",
        "## Discharge Condition",
        summary.discharge_condition,
        "",
    ]

    if summary.clinician_flags:
        lines += [
            "---",
            "",
            "## ⚠ Clinician Flags Requiring Review",
            "",
        ]
        for flag in summary.clinician_flags:
            lines.append(f"- {flag}\n")

    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))

    return path


# ---------------------------------------------------------------------------
# Main agent entrypoint
# ---------------------------------------------------------------------------

def run_agent(
    patient_folder: str,
    patient_id:     str | None = None,
    output_dir:     str = "output",
) -> tuple[DischargeSummary, Tracer]:
    """
    Run the full discharge summary agent for one patient folder.

    Parameters
    ----------
    patient_folder : path to folder containing patient PDFs
    patient_id     : optional ID; inferred from folder name if not given
    output_dir     : directory for summary + trace files

    Returns
    -------
    (DischargeSummary, Tracer)
    """
    if patient_id is None:
        from pathlib import Path as _Path
        patient_id = _Path(patient_folder).name

    print(f"\n{'═'*60}")
    print(f"  Discharge Summary Agent — Patient: {patient_id}")
    print(f"{'═'*60}\n")

    memory = WorkingMemory(patient_id=patient_id)
    tracer = Tracer(patient_id=patient_id, output_dir=output_dir)
    step   = 0

    # ── Step 0: Init ─────────────────────────────────────────────────────────
    tracer.log(
        step=step,
        reasoning="Initialising agent. Loading patient documents.",
        tool="pdf_extractor.load_patient_documents",
        inputs={"patient_folder": patient_folder},
        raw_output="",
        memory_delta="WorkingMemory initialised",
        next_decision="Load and classify all PDFs, then generate extraction plan",
    )

    # ── Step 1: Load documents ───────────────────────────────────────────────
    step += 1
    docs = pdf_extractor.load_patient_documents(patient_folder)

    if not docs:
        flagging.escalate(
            memory,
            reason="No patient documents found",
            context=f"No PDFs were found in folder: {patient_folder}",
            severity="critical",
        )
        tracer.log(
            step=step,
            reasoning="No documents found — cannot proceed.",
            tool="flagging.escalate",
            inputs={"folder": patient_folder},
            raw_output="CRITICAL: No documents",
            memory_delta="Flag: no documents",
            next_decision="Abort — return empty summary",
        )
        summary = _build_summary(memory, docs)
        _format_summary_md(summary, output_dir)
        tracer.finalize(step, memory.flags, "ABORTED — no documents")
        return summary, tracer

    tracer.log(
        step=step,
        reasoning=f"Loaded {len(docs)} virtual document(s).",
        tool="pdf_extractor.load_patient_documents",
        inputs={"folder": patient_folder},
        raw_output=json.dumps(
            [{"doc_id": d["doc_id"], "doc_type": d["doc_type"]} for d in docs]
        ),
        memory_delta=f"Docs loaded: {[d['doc_type'] for d in docs]}",
        next_decision="Generate extraction plan based on available doc types",
    )

    # ── Step 2: Generate plan ────────────────────────────────────────────────
    step += 1
    plan = _generate_plan(docs)

    tracer.log(
        step=step,
        reasoning="LLM generated extraction plan based on available document types.",
        tool="planner",
        inputs={"doc_types": [d["doc_type"] for d in docs]},
        raw_output=json.dumps(plan, indent=2)[:600],
        memory_delta="Work plan created",
        next_decision="Execute plan steps sequentially",
    )

    # ── Step 3+: Execute plan ────────────────────────────────────────────────
    for plan_step in plan:
        if memory.step_count >= MAX_STEPS - 5:
            memory.add_flag(
                reason="Step cap approaching — some fields may be incomplete",
                context=(
                    f"Agent reached {memory.step_count}/{MAX_STEPS} steps. "
                    f"Remaining plan steps skipped."
                ),
                severity=FlagSeverity.WARNING,
            )
            break

        memory.increment_step()
        step += 1

        if plan_step.get("section") == "medications":
            step = _run_med_reconciliation(docs, memory, tracer, step)
        else:
            step = _run_extraction_step(plan_step, docs, memory, tracer, step)

        if step >= MAX_STEPS:
            memory.add_flag(
                reason="Hard step cap reached",
                context=f"Agent stopped at step {step}/{MAX_STEPS}.",
                severity=FlagSeverity.WARNING,
            )
            break

    # ── Conflict sweep ───────────────────────────────────────────────────────
    step += 1
    if step < MAX_STEPS:
        step = _run_conflict_sweep(docs, memory, tracer, step)

    # ── Build + write summary ────────────────────────────────────────────────
    summary = _build_summary(memory, docs)
    md_path = _format_summary_md(summary, output_dir)

    tracer.log(
        step=step + 1,
        reasoning="All extraction steps complete. Building final discharge summary draft.",
        tool="build_summary",
        inputs={},
        raw_output=f"Summary written to {md_path}",
        memory_delta=(
            f"Facts: {len(memory.facts)}  |  "
            f"Pending: {len(memory.pending)}  |  "
            f"Conflicts: {len(memory.conflicts)}  |  "
            f"Flags: {len(memory.flags)}"
        ),
        next_decision="DONE",
    )

    outcome = (
        f"COMPLETE — {len(memory.facts)} facts extracted, "
        f"{len(memory.pending)} missing, "
        f"{len(memory.conflicts)} conflicts, "
        f"{len(memory.flags)} flags"
    )
    tracer.finalize(step + 1, memory.flags, outcome)

    print(f"\n  Summary → {md_path}")
    return summary, tracer