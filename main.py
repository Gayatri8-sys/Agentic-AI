"""
main.py

CLI entrypoint for the Discharge Summary Agent.

Usage:
  python main.py --patient_folder data/patient_2
  python main.py --patients_dir data/
  python main.py --patient_folder data/patient_2 --output_dir results/
  python main.py --patient_folder data/patient_2 --dry_run
"""

from __future__ import annotations

# Load .env file (OPENAI_API_KEY, GOOGLE_API_KEY) before anything else
from dotenv import load_dotenv
load_dotenv()

import argparse
import os
import sys
import traceback
from pathlib import Path


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _check_env() -> None:
    missing = []
    if not os.environ.get("OPENAI_API_KEY"):
        missing.append("OPENAI_API_KEY")
    if not os.environ.get("GOOGLE_API_KEY"):
        print("  [warn] GOOGLE_API_KEY not set — OCR fallback for scanned pages disabled.")
    if missing:
        print(f"\n  [error] Missing required environment variables: {missing}")
        print("  Set them in your .env file:")
        for var in missing:
            print(f"    {var}=<your-key>")
        sys.exit(1)


def _find_patient_folders(patients_dir: str) -> list[Path]:
    root = Path(patients_dir)
    folders = []
    for candidate in sorted(root.iterdir()):
        if candidate.is_dir() and list(candidate.glob("*.pdf")):
            folders.append(candidate)
    if not folders:
        print(f"  [warn] No patient folders with PDFs found in: {patients_dir}")
    return folders


def _print_summary_table(summary) -> None:
    print(f"\n{'─'*60}")
    print(f"  DISCHARGE SUMMARY — {summary.patient_id}  (DRAFT)")
    print(f"{'─'*60}")
    fields = [
        ("Demographics",        summary.patient_demographics),
        ("Admission date",      summary.admission_date),
        ("Discharge date",      summary.discharge_date),
        ("Principal diagnosis", summary.principal_diagnosis),
        ("Secondary diagnoses", summary.secondary_diagnoses[:120]),
        ("Discharge condition", summary.discharge_condition),
        ("Allergies",           summary.allergies),
        ("Follow-up",           summary.follow_up_instructions[:100]),
        ("Pending results",     summary.pending_results[:100]),
    ]
    for label, value in fields:
        print(f"  {label:<26}: {value}")

    print(f"\n  Medications:")
    for line in summary.discharge_medications.splitlines()[:10]:
        print(f"    {line}")

    if summary.clinician_flags:
        print(f"\n  ⚠  {len(summary.clinician_flags)} Clinician Flag(s):")
        for flag in summary.clinician_flags[:5]:
            first_line = flag.splitlines()[0] if flag else ""
            print(f"    • {first_line}")
        if len(summary.clinician_flags) > 5:
            print(f"    … and {len(summary.clinician_flags)-5} more (see trace)")

    print(f"\n  is_draft = {summary.is_draft}")
    print()


def _run_patient(patient_folder: str, output_dir: str, dry_run: bool) -> bool:
    patient_id = Path(patient_folder).name
    print(f"\n{'━'*60}")
    print(f"  Processing patient: {patient_id}")
    print(f"{'━'*60}")

    if dry_run:
        print(f"  [dry_run] Would process: {patient_folder}")
        pdfs = list(Path(patient_folder).glob("*.pdf"))
        print(f"  [dry_run] PDFs found: {[p.name for p in pdfs]}")
        return True

    try:
        from agent import run_agent
        summary, tracer = run_agent(
            patient_folder=patient_folder,
            patient_id=patient_id,
            output_dir=output_dir,
        )
        _print_summary_table(summary)
        return True

    except KeyboardInterrupt:
        print("\n  [interrupted] User cancelled.")
        return False

    except Exception as exc:
        print(f"\n  [error] Patient {patient_id} failed: {exc}")
        traceback.print_exc()
        Path(output_dir).mkdir(parents=True, exist_ok=True)
        error_path = Path(output_dir) / f"error_{patient_id}.txt"
        with open(error_path, "w", encoding="utf-8") as f:
            f.write(f"Patient: {patient_id}\nError: {exc}\n\n")
            traceback.print_exc(file=f)
        print(f"  Error report → {error_path}")
        return False


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="Discharge Summary Agent",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    group = p.add_mutually_exclusive_group(required=True)
    group.add_argument("--patient_folder", type=str,
                       help="Path to a single patient PDF folder.")
    group.add_argument("--patients_dir",   type=str,
                       help="Path to directory of patient sub-folders.")
    p.add_argument("--output_dir",    type=str, default="output",
                   help="Output directory (default: output/).")
    p.add_argument("--dry_run",       action="store_true",
                   help="List PDFs without calling LLMs.")
    p.add_argument("--skip_env_check", action="store_true",
                   help="Skip API key validation.")
    return p


def main() -> int:
    parser = _build_parser()
    args   = parser.parse_args()

    if not args.skip_env_check and not args.dry_run:
        _check_env()

    output_dir = args.output_dir
    Path(output_dir).mkdir(parents=True, exist_ok=True)

    if args.patient_folder:
        folders = [Path(args.patient_folder)]
    else:
        folders = _find_patient_folders(args.patients_dir)

    if not folders:
        print("No patients to process.")
        return 1

    print(f"\n  Patients to process : {len(folders)}")
    print(f"  Output directory    : {output_dir}")
    print(f"  Dry run             : {args.dry_run}")

    successes, failures = 0, 0
    for folder in folders:
        ok = _run_patient(str(folder), output_dir, args.dry_run)
        if ok:
            successes += 1
        else:
            failures += 1

    print(f"\n{'═'*60}")
    print(f"  Done. Success: {successes}  |  Failed: {failures}")
    print(f"  Output in: {output_dir}/")
    print(f"{'═'*60}\n")

    return 0 if failures == 0 else 1


if __name__ == "__main__":
    sys.exit(main())