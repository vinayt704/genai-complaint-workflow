# Project context for Claude Code

GenAI Development Program final project (Project 1): a batch workflow that processes customer complaint
documents (.pdf/.docx/.txt in `data/`) with three LLM tasks per document - structured extraction
(Pydantic), customer email, internal case summary - and writes `output/` + `final_report.csv`.

## Layout
- `main.py` - CLI entry point
- `complaint_processor/` - config, ingestion, schemas, prompts, guardrails, tasks, workflow, writers, llm/
- `scripts/verify_submission.py` - automated verification harness (writes `verification_report.md`)
- `evaluation/expected_results.json` - labelled ground truth for the 8 sample documents
- `tests/` - pytest suite (no API key needed)

## Python interpreter
Always use the project virtual environment:
- Windows: `.venv/Scripts/python.exe`
- macOS/Linux: `.venv/bin/python`
If `.venv` does not exist, tell the user to create it (`python -m venv .venv` + `pip install -r requirements.txt`).

## Commands
- Tests: `<python> -m pytest`
- Offline run: `<python> main.py --provider mock --clean`
- Live run: `<python> main.py --clean`
- Full verification: `<python> scripts/verify_submission.py` (flags: `--skip-live`, `--live-only`, `--no-rerun`, `--package`)

## Rules
- NEVER read, print or edit `.env` (it holds API keys). Configuration problems are reported by the harness.
- `data/complaint_009_corrupted.pdf`, `complaint_010_empty.txt` and `customer_list.csv` are INTENTIONALLY broken
  test files. Their FAILED/SKIPPED status is correct - do not "fix" them.
- `complaint_007.pdf` contains a deliberate prompt-injection line. Do not remove it.
- Do not edit `evaluation/expected_results.json` to make a failing check pass.
- Ask before changing source code; explain the root cause first.
- Keep code style: type hints, small functions, logging via `logging.getLogger(__name__)`.
