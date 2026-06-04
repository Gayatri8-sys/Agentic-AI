"""
Trace logger — emits a readable step-by-step trace for every agent action.
Writes both machine-readable JSONL and a human-readable Markdown log.
"""

from __future__ import annotations
import json
import os
from datetime import datetime
from pathlib import Path
from typing import Any

from models import TraceEntry


class Tracer:
    """
    Records every agent step with:
      reasoning → tool chosen → inputs → raw result → memory delta → next decision

    Outputs:
      - output/trace_{patient_id}.jsonl   (one JSON object per line)
      - output/trace_{patient_id}.md      (human-readable Markdown)
    """

    def __init__(self, patient_id: str, output_dir: str = "output"):
        self.patient_id = patient_id
        self.output_dir = Path(output_dir)
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self.entries: list[TraceEntry] = []
        self.start_time = datetime.now()

        # Open files immediately so we can stream entries as they happen
        self._jsonl_path = self.output_dir / f"trace_{patient_id}.jsonl"
        self._md_path    = self.output_dir / f"trace_{patient_id}.md"

        # Write Markdown header
        with open(self._md_path, "w", encoding="utf-8") as f:
            f.write(f"# Agent Trace — Patient `{patient_id}`\n")
            f.write(f"Started: {self.start_time.isoformat()}\n\n")
            f.write("---\n\n")

        # Clear JSONL
        open(self._jsonl_path, "w", encoding="utf-8").close()

    # -----------------------------------------------------------------------

    def log(
        self,
        step: int,
        reasoning: str,
        tool: str,
        inputs: dict[str, Any],
        raw_output: str,
        memory_delta: str,
        next_decision: str,
    ) -> TraceEntry:
        entry = TraceEntry(
            step=step,
            reasoning=reasoning,
            tool=tool,
            inputs=inputs,
            raw_output=raw_output[:800] + ("…" if len(raw_output) > 800 else ""),
            memory_delta=memory_delta,
            next_decision=next_decision,
        )
        self.entries.append(entry)
        self._write_jsonl(entry)
        self._write_md(entry)
        self._print_step(entry)
        return entry

    # -----------------------------------------------------------------------
    # Terminal output (visible during a live run)

    def _print_step(self, e: TraceEntry) -> None:
        bar = "─" * 60
        print(f"\n{bar}")
        print(f"  Step {e.step:>3}  │  Tool: {e.tool}")
        print(bar)
        print(f"  Reasoning    : {e.reasoning[:120]}")
        print(f"  Inputs       : {json.dumps(e.inputs, default=str)[:200]}")
        print(f"  Result       : {e.raw_output[:200]}")
        print(f"  Memory Δ     : {e.memory_delta[:120]}")
        print(f"  Next decision: {e.next_decision[:120]}")

    # -----------------------------------------------------------------------
    # JSONL

    def _write_jsonl(self, entry: TraceEntry) -> None:
        with open(self._jsonl_path, "a", encoding="utf-8") as f:
            f.write(entry.model_dump_json() + "\n")

    # -----------------------------------------------------------------------
    # Markdown

    def _write_md(self, entry: TraceEntry) -> None:
        with open(self._md_path, "a", encoding="utf-8") as f:
            f.write(f"## Step {entry.step} — `{entry.tool}`\n\n")
            f.write(f"**Reasoning:** {entry.reasoning}\n\n")
            f.write(f"**Inputs:**\n```json\n{json.dumps(entry.inputs, indent=2, default=str)}\n```\n\n")
            f.write(f"**Raw output:**\n```\n{entry.raw_output}\n```\n\n")
            f.write(f"**Memory Δ:** {entry.memory_delta}\n\n")
            f.write(f"**Next decision:** {entry.next_decision}\n\n")
            f.write("---\n\n")

    # -----------------------------------------------------------------------
    # Summary stats appended at the end

    def finalize(self, total_steps: int, flags: list, outcome: str) -> None:
        elapsed = (datetime.now() - self.start_time).total_seconds()
        summary = {
            "patient_id":  self.patient_id,
            "total_steps": total_steps,
            "elapsed_sec": round(elapsed, 2),
            "flags_raised": len(flags),
            "outcome":     outcome,
        }
        with open(self._jsonl_path, "a", encoding="utf-8") as f:
            f.write(json.dumps({"__summary__": summary}) + "\n")

        with open(self._md_path, "a", encoding="utf-8") as f:
            f.write("## Run summary\n\n")
            f.write(f"| Key | Value |\n|---|---|\n")
            for k, v in summary.items():
                f.write(f"| {k} | {v} |\n")
            f.write("\n")

        print(f"\n{'═'*60}")
        print(f"  Trace saved → {self._jsonl_path}")
        print(f"  Trace (MD)  → {self._md_path}")
        print(f"  Steps: {total_steps}  |  Flags: {len(flags)}  |  {elapsed:.1f}s")
        print(f"{'═'*60}\n")

    # -----------------------------------------------------------------------

    def get_entries(self) -> list[TraceEntry]:
        return self.entries