# 🏥 Discharge Summary Agent

An AI-powered agentic pipeline that automatically generates structured hospital discharge summaries from patient PDF documents. The agent extracts clinical facts, reconciles medications, detects conflicts between documents, flags missing fields, and produces a draft summary ready for clinician review — with a full reasoning trace at every step.

> ⚠️ **This tool generates DRAFT summaries only. All output must be reviewed and approved by a licensed clinician before use.**

---

## 📋 Table of Contents

- [Overview](#overview)
- [Features](#features)
- [Architecture](#architecture)
- [Project Structure](#project-structure)
- [Prerequisites](#prerequisites)
- [Installation](#installation)
- [Configuration](#configuration)
- [Usage](#usage)
- [Output Files](#output-files)
- [Troubleshooting](#troubleshooting)

---

## Overview

The Discharge Summary Agent processes a folder of patient PDFs and produces:

- A structured **discharge summary** (Markdown) covering demographics, diagnoses, medications, follow-up, and more
- A step-by-step **agent trace** (Markdown + JSONL) showing every reasoning step, tool call, and memory update
- **Clinician flags** for missing fields, medication changes without documented reasons, drug-drug interactions, and conflicting values across documents

The agent uses a plan-then-execute loop: it inspects the available documents, generates a custom extraction plan, then works through each clinical section — only writing what is explicitly found in the source text.

---

## Features

- **Multi-PDF ingestion** — handles single combined records or multiple separate documents
- **Semantic doc search** — keyword-scored retrieval with doc-type filtering and automatic fallback
- **Medication reconciliation** — compares admission vs. discharge meds, flags changes without reasons
- **Drug interaction checking** — screens discharge medications for clinically significant DDIs
- **Conflict detection** — cross-document sweep to surface contradictory clinical values
- **No hallucination policy** — missing fields are marked `[MISSING]`, never invented
- **Full reasoning trace** — every agent step logged with inputs, outputs, and memory delta
- **Source attribution** — every clinical fact carries `doc_id` + `page_num`

---

## Architecture

```
patient PDFs
     │
     ▼
pdf_extractor          ← OCR + text extraction (PyMuPDF + Gemini fallback for scans)
     │
     ▼
planner (LLM)          ← generates extraction work plan based on doc types
     │
     ▼
 agent loop
  ├── doc_search        ← keyword retrieval over extracted pages
  ├── field_extractor   ← LLM extracts one field from a snippet
  ├── med_reconciler    ← LLM reconciles admission vs discharge meds
  ├── drug_checker      ← DDI screening
  ├── conflict_detector ← cross-document fact comparison
  └── flagging          ← raises clinician flags
     │
     ▼
WorkingMemory           ← single source of truth (facts + conflicts + flags)
     │
     ▼
DischargeSummary        ← structured Pydantic output model
     │
     ▼
summary_{id}.md  +  trace_{id}.md  +  trace_{id}.jsonl
```

---

## Project Structure

```
├── agent.py            # Main agent loop — planning, extraction, reconciliation
├── main.py             # CLI entrypoint
├── models.py           # Pydantic models (WorkingMemory, DischargeSummary, etc.)
├── prompts.py          # LLM prompt templates
├── tracer.py           # Step-by-step trace logger (Markdown + JSONL)
├── tools/
│   ├── pdf_extractor.py    # PDF loading, OCR, doc-type classification
│   ├── med_reconciler.py   # Medication reconciliation logic
│   ├── drug_checker.py     # Drug-drug interaction screening
│   ├── conflict_detector.py# Cross-document conflict detection
│   └── flagging.py         # Clinician flag creation
├── data/
│   └── patient_1/          # Example: one folder per patient
│       ├── admission_note.pdf
│       ├── progress_notes.pdf
│       └── discharge_meds.pdf
├── output/                 # Generated summaries and traces (git-ignored)
├── .env                    # API keys (never commit this)
├── requirements.txt
└── README.md
```

---

## Prerequisites

- Python **3.10+**
- An **OpenAI API key** with sufficient quota (`gpt-4o-mini` by default)
- *(Optional)* A **Google AI API key** for Gemini OCR fallback on scanned/image PDFs

---

## Installation

**1. Clone the repository**

```bash
git clone  https://github.com/Gayatri8-sys/Agentic-AI.git
cd discharge-summary-agent
```

**2. Create and activate a virtual environment**

```bash
python -m venv venv

# Windows
venv\Scripts\activate

# macOS / Linux
source venv/bin/activate
```

**3. Install dependencies**

```bash
pip install -r requirements.txt
```

---

## Configuration

Create a `.env` file in the project root:

```env
# Required — OpenAI for LLM extraction, reconciliation, and section writing
OPENAI_API_KEY=sk-...

# Optional — Google Gemini for OCR on scanned/image-only PDF pages
# If not set, scanned pages will be skipped with a warning
GOOGLE_API_KEY=AIza...
```

> 💡 **Google API key note:** Use the [Google AI Studio](https://aistudio.google.com/) key, not a GCP service account key. Make sure to enable the **Generative Language API** in your project and use model `gemini-2.0-flash` (not the deprecated `gemini-1.5-flash`).

---

## Usage

### Process a single patient

```bash
python main.py --patient_folder data/patient_1
```

### Process all patients in a directory

```bash
python main.py --patients_dir data/
```

### Specify a custom output directory

```bash
python main.py --patient_folder data/patient_1 --output_dir results/
```

### Dry run — list PDFs without calling any LLMs

```bash
python main.py --patient_folder data/patient_1 --dry_run
```

### All options

```
usage: main.py [-h] (--patient_folder PATH | --patients_dir PATH)
               [--output_dir PATH] [--dry_run] [--skip_env_check]

options:
  --patient_folder PATH   Path to a single patient PDF folder
  --patients_dir PATH     Path to a directory of patient sub-folders
  --output_dir PATH       Output directory (default: output/)
  --dry_run               List PDFs without calling LLMs
  --skip_env_check        Skip API key validation at startup
```

### Patient folder structure

Each patient folder should contain one or more PDFs. The agent works with a single combined PDF or multiple separate documents:

```
data/
└── patient_1/
    ├── patient_1_record.pdf        # single combined record, OR
    ├── admission_note.pdf          # separate documents
    ├── progress_notes.pdf
    └── discharge_medications.pdf
```

---

## Output Files

For each patient, three files are written to the output directory:

| File | Description |
|---|---|
| `summary_{patient_id}.md` | Structured discharge summary draft for clinician review |
| `trace_{patient_id}.md` | Human-readable step-by-step agent reasoning trace |
| `trace_{patient_id}.jsonl` | Machine-readable trace (one JSON object per step) |
| `error_{patient_id}.txt` | Error report (only created if the agent fails) |

## Troubleshooting

### All fields showing `[MISSING]`

The most common cause is that your PDF was classified as `"unknown"` doc type, so the doc-type filter was blocking all searches. Make sure you are using the fixed `agent.py` from this repo — earlier versions did not include `"unknown"` docs in filtered searches.

Also check that your PDFs contain selectable text. If the trace shows `OCR_FAILED`, you need a valid `GOOGLE_API_KEY`.

### `OCR_FAILED: 404 models/gemini-1.5-flash not found`

The Gemini model name has changed. Update `tools/pdf_extractor.py`:

```python
# Change this
model = "models/gemini-1.5-flash"
url = "...googleapis.com/v1beta/..."

# To this
model = "gemini-2.0-flash"
url = "...googleapis.com/v1/..."
```

### `429 insufficient_quota` from OpenAI

Your OpenAI account is out of credits. Add credits at [platform.openai.com/settings/billing](https://platform.openai.com/settings/billing).

### `UnicodeEncodeError: 'charmap' codec can't encode character`

This happens on Windows when `tracer.py` writes files without specifying UTF-8 encoding. Open `tracer.py` and add `encoding="utf-8"` to every `open()` call:

```python
# Before
with open(path, "w") as f:

# After
with open(path, "w", encoding="utf-8") as f:
```

### Processing is very slow

The agent makes multiple LLM calls per section. For a 15-section plan with a large PDF, expect 5–15 minutes per patient. The elapsed time is shown in the trace run summary. To reduce cost and time, the default model is `gpt-4o-mini` — do not switch to `gpt-4o` unless accuracy is insufficient.

---

## Disclaimer

This software is intended as a clinical documentation **assistance tool** only. It does not provide medical advice, diagnosis, or treatment. All generated summaries are clearly marked as drafts and must be reviewed, edited, and approved by a qualified clinician before inclusion in any medical record or patient communication. The authors accept no liability for clinical decisions made on the basis of this tool's output.
