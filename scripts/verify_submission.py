"""Automated end-to-end verification of the project.

Runs every check needed before submission and writes verification_report.md.

    python scripts/verify_submission.py                 # everything (live run if a provider is configured)
    python scripts/verify_submission.py --skip-live     # no API calls: environment, secrets, tests, mock run
    python scripts/verify_submission.py --live-only     # only the real LLM run + acceptance checks
    python scripts/verify_submission.py --no-rerun      # check the existing output/ without calling the LLM again
    python scripts/verify_submission.py --package       # ...and build dist/<name>.zip if nothing FAILED

Steps
  1. Environment   Python version, packages, .env / provider configuration
  2. Secrets       .env is git-ignored; no API key value or key pattern in any other file
  3. Unit tests    pytest
  4. Mock run      full pipeline offline; every input file accounted for, outputs complete
  5. Live run      full pipeline with the configured LLM into output/
  6. Acceptance    output/ vs evaluation/expected_results.json -> hard/soft checks + accuracy %
                   plus email and summary quality checks for every processed document
  7. Package       (optional) submission zip, never containing .env

Exit code: 0 = no FAIL, 1 = at least one FAIL.
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import re
import subprocess
import sys
import tempfile
import time
import zipfile
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

EXPECTED_FILE = PROJECT_ROOT / "evaluation" / "expected_results.json"
REPORT_FILE = PROJECT_ROOT / "verification_report.md"
NOT_PROVIDED = "Not Provided"
SUMMARY_SECTIONS = ("Case overview", "Key issue", "Action taken", "Current status", "Recommended next action")

# Excluded from the secret scan and the submission zip
EXCLUDED_DIRS = {".venv", "venv", ".git", "__pycache__", ".pytest_cache", "dist", "logs",
                 ".vscode", ".idea", ".claude", "node_modules"}
EXCLUDED_FILES = {".env", "verification_report.md", "verification_review.md", "CLAUDE.md"}
KEY_PATTERNS = {
    "OpenAI key": re.compile(r"\bsk-(?:proj-)?[A-Za-z0-9_-]{20,}"),
    "Google API key": re.compile(r"\bAIza[0-9A-Za-z_-]{35}\b"),
}


# ---------------------------------------------------------------------- #
@dataclass
class Check:
    step: str
    name: str
    status: str  # PASS | WARN | FAIL | SKIP
    detail: str = ""


@dataclass
class Verifier:
    checks: list[Check] = field(default_factory=list)
    accuracy: dict = field(default_factory=dict)
    live_provider: str = ""

    def add(self, step, name, status, detail=""):
        self.checks.append(Check(step, name, status, detail))
        tag = {"PASS": "[PASS]", "WARN": "[WARN]", "FAIL": "[FAIL]", "SKIP": "[SKIP]"}[status]
        print(f"  {tag} {name}" + (f" - {detail}" if detail else ""))

    def count(self, status):
        return sum(1 for c in self.checks if c.status == status)


def header(title: str) -> None:
    print(f"\n=== {title} " + "=" * max(0, 70 - len(title)))


def run_subprocess(args: list[str], env: dict | None = None, capture: bool = True, timeout: int = 1800):
    merged = {**os.environ, "PYTHONUTF8": "1", **(env or {})}
    return subprocess.run(
        args, cwd=PROJECT_ROOT, env=merged, timeout=timeout,
        capture_output=capture, text=True, encoding="utf-8", errors="replace",
    )


# ---------------------------------------------------------------------- #
#  1. Environment
# ---------------------------------------------------------------------- #
def step_environment(v: Verifier, provider_override: str | None):
    header("1. Environment")
    ok = sys.version_info >= (3, 10)
    v.add("Environment", "Python version", "PASS" if ok else "FAIL", sys.version.split()[0])

    missing = []
    for module in ("pydantic", "dotenv", "tenacity", "langchain_core", "langchain_openai",
                   "langchain_google_genai", "pypdf", "docx", "reportlab", "pytest"):
        try:
            __import__(module)
        except ImportError:
            missing.append(module)
    v.add("Environment", "Required packages", "FAIL" if missing else "PASS",
          f"missing: {', '.join(missing)} - run pip install -r requirements.txt" if missing else "all importable")

    if missing:
        return None

    from complaint_processor.config import ConfigError, Settings

    env_path = PROJECT_ROOT / ".env"
    v.add("Environment", ".env file", "PASS" if env_path.exists() else "WARN",
          "found" if env_path.exists() else "not found - copy .env.example to .env for a live run")
    try:
        settings = Settings.from_env(env_file=env_path, llm_provider=provider_override)
        settings.validate()
    except ConfigError as exc:
        v.add("Environment", "LLM provider configuration", "WARN", f"{exc} (live run will be skipped)")
        return None
    if settings.llm_provider == "mock":
        v.add("Environment", "LLM provider configuration", "WARN",
              "LLM_PROVIDER=mock - set a real provider for the live run")
        return None
    v.add("Environment", "LLM provider configuration", "PASS",
          f"{settings.llm_provider} / {settings.resolved_model}")
    return settings


# ---------------------------------------------------------------------- #
#  2. Secrets
# ---------------------------------------------------------------------- #
def iter_project_files():
    for path in PROJECT_ROOT.rglob("*"):
        rel = path.relative_to(PROJECT_ROOT)
        if any(part in EXCLUDED_DIRS for part in rel.parts):
            continue
        if path.is_file() and path.name not in EXCLUDED_FILES and not path.name.endswith(".env"):
            yield path


def step_secrets(v: Verifier):
    header("2. Secrets")
    gitignore = PROJECT_ROOT / ".gitignore"
    ignored = gitignore.exists() and re.search(r"^\.env\s*$", gitignore.read_text(), re.MULTILINE)
    v.add("Secrets", ".env is git-ignored", "PASS" if ignored else "FAIL",
          "" if ignored else "add '.env' to .gitignore")

    secret_values = []
    env_path = PROJECT_ROOT / ".env"
    if env_path.exists():
        from dotenv import dotenv_values
        for key, value in dotenv_values(env_path).items():
            if value and len(value) >= 12 and any(t in key.upper() for t in ("KEY", "TOKEN", "SECRET")):
                secret_values.append((key, value))

    leaks = []
    for path in iter_project_files():
        if path.suffix.lower() in {".png", ".pdf", ".docx", ".zip", ".pyc"}:
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        rel = path.relative_to(PROJECT_ROOT).as_posix()
        for key, value in secret_values:
            if value in text:
                leaks.append(f"value of {key} in {rel}")
        for label, pattern in KEY_PATTERNS.items():
            if pattern.search(text):
                leaks.append(f"{label} pattern in {rel}")
    v.add("Secrets", "No API keys outside .env", "FAIL" if leaks else "PASS", "; ".join(leaks[:5]))


# ---------------------------------------------------------------------- #
#  3. Unit tests
# ---------------------------------------------------------------------- #
def step_unit_tests(v: Verifier):
    header("3. Unit & integration tests (pytest)")
    started = time.perf_counter()
    result = run_subprocess([sys.executable, "-m", "pytest", "-p", "no:cacheprovider"])
    summary = next((line for line in reversed(result.stdout.splitlines()) if "passed" in line or "failed" in line
                    or "error" in line), result.stdout.strip()[-200:])
    status = "PASS" if result.returncode == 0 else "FAIL"
    v.add("Tests", "pytest", status, f"{summary.strip()} ({time.perf_counter() - started:.1f}s)")
    if status == "FAIL":
        print(result.stdout[-3000:])


# ---------------------------------------------------------------------- #
#  4 / 5. Pipeline runs
# ---------------------------------------------------------------------- #
def data_files() -> list[Path]:
    from complaint_processor.config import Settings
    data_dir = PROJECT_ROOT / Settings.from_env(env_file=PROJECT_ROOT / ".env").data_dir
    return sorted(p for p in data_dir.iterdir() if p.is_file() and not p.name.startswith((".", "~$")))


def check_run_outputs(v: Verifier, step: str, output_dir: Path, expected_errors: dict):
    report_path = output_dir / "final_report.csv"
    summary_path = output_dir / "run_summary.json"
    if not report_path.exists() or not summary_path.exists():
        v.add(step, "Report files created", "FAIL", f"missing final_report.csv or run_summary.json in {output_dir}")
        return []
    v.add(step, "Report files created", "PASS", "final_report.csv, run_summary.json")

    with open(report_path, encoding="utf-8-sig") as handle:
        rows = list(csv.DictReader(handle))
    inputs = {p.name for p in data_files()}
    reported = {r["file_name"] for r in rows}
    missing = sorted(inputs - reported)
    v.add(step, "Every input file is in the report", "FAIL" if missing else "PASS",
          f"missing: {missing}" if missing else f"{len(rows)} rows for {len(inputs)} files")

    by_name = {r["file_name"]: r for r in rows}
    wrong = [f"{name}: {by_name.get(name, {}).get('processing_status', 'absent')} (expected {status})"
             for name, status in expected_errors.items()
             if name in inputs and by_name.get(name, {}).get("processing_status") != status]
    v.add(step, "Broken files handled gracefully", "FAIL" if wrong else "PASS",
          "; ".join(wrong) if wrong else "corrupt/empty -> FAILED, unsupported -> SKIPPED")

    unexpected = [f"{r['file_name']}: {r['processing_status']} - {r['errors'][:120]}" for r in rows
                  if r["file_name"] not in expected_errors and r["processing_status"] != "SUCCESS"]
    v.add(step, "All valid documents SUCCESS", "FAIL" if unexpected else "PASS",
          "; ".join(unexpected[:4]) if unexpected else "")

    incomplete = []
    for r in rows:
        if r["processing_status"] != "SUCCESS":
            continue
        for col in ("structured_data_file", "customer_email_file", "case_summary_file"):
            if not r[col] or not (output_dir / r[col]).exists():
                incomplete.append(f"{r['file_name']}:{col}")
    v.add(step, "3 output files per successful document", "FAIL" if incomplete else "PASS",
          ", ".join(incomplete[:6]))
    return rows


def step_mock_run(v: Verifier, expected_errors: dict):
    header("4. Offline pipeline run (mock provider)")
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        result = run_subprocess(
            [sys.executable, "main.py", "--provider", "mock", "--clean", "--log-level", "WARNING",
             "--output-dir", str(tmp_path / "output")],
            env={"LOG_DIR": str(tmp_path / "logs")},
        )
        v.add("Mock run", "main.py exits cleanly", "PASS" if result.returncode == 0 else "FAIL",
              f"exit code {result.returncode}")
        if result.returncode != 0:
            print(result.stderr[-2000:])
            return
        check_run_outputs(v, "Mock run", tmp_path / "output", expected_errors)


def step_live_run(v: Verifier, settings, expected_errors: dict, rerun: bool, workers: int | None):
    header(f"5. Live pipeline run ({settings.llm_provider} / {settings.resolved_model})")
    output_dir = PROJECT_ROOT / settings.output_dir
    if rerun:
        args = [sys.executable, "main.py", "--clean", "--provider", settings.llm_provider]
        if workers:
            args += ["--workers", str(workers)]
        print("  Running main.py (streaming output below)...\n")
        started = time.perf_counter()
        result = run_subprocess(args, capture=False)
        elapsed = time.perf_counter() - started
        v.add("Live run", "main.py exits cleanly", "PASS" if result.returncode == 0 else "FAIL",
              f"exit code {result.returncode}, {elapsed:.0f}s")
        if result.returncode != 0:
            return None
    else:
        v.add("Live run", "Re-run skipped", "SKIP", f"checking existing {output_dir}")

    summary_path = output_dir / "run_summary.json"
    if summary_path.exists():
        summary = json.loads(summary_path.read_text(encoding="utf-8"))
        v.live_provider = summary.get("provider", "")
        if v.live_provider == "mock":
            v.add("Live run", "Output produced by a real LLM", "FAIL",
                  "output/ was produced by the mock provider - run with a real provider")
        else:
            v.add("Live run", "Output produced by a real LLM", "PASS",
                  f"{summary.get('provider')} / {summary.get('model')}, "
                  f"{summary.get('input_tokens', 0) + summary.get('output_tokens', 0):,} tokens")
    return check_run_outputs(v, "Live run", output_dir, expected_errors)


# ---------------------------------------------------------------------- #
#  6. Acceptance checks
# ---------------------------------------------------------------------- #
def _digits(text: str) -> str:
    return re.sub(r"\D", "", text or "")


def _field_matches(field_name: str, actual: str, expected) -> bool:
    if isinstance(expected, list):
        return actual in expected
    if expected == NOT_PROVIDED:
        return actual == NOT_PROVIDED
    if field_name == "phone_number":
        a, e = _digits(actual), _digits(expected)
        return bool(a) and (a.endswith(e) or e.endswith(a)) and len(a) >= 10
    if field_name in ("customer_name", "email"):
        return actual.strip().lower() == expected.strip().lower() or (
            field_name == "customer_name" and expected.lower() in actual.lower())
    return actual == expected


def evaluate_extractions(expected_docs: dict, extractions: dict) -> tuple[list[Check], dict]:
    """Pure function (unit-tested): compare extractions to ground truth."""
    checks: list[Check] = []
    hard_total = hard_ok = soft_total = soft_ok = 0
    for doc_id, spec in expected_docs.items():
        actual = extractions.get(doc_id)
        if actual is None:
            checks.append(Check("Acceptance", f"{doc_id}: extraction present", "FAIL", "no structured_data file"))
            continue
        problems_hard, problems_soft = [], []
        for name, expected in spec.get("hard", {}).items():
            hard_total += 1
            if _field_matches(name, actual.get(name, ""), expected):
                hard_ok += 1
            else:
                problems_hard.append(f"{name}='{actual.get(name)}' expected {expected}")
        for name, allowed in spec.get("soft", {}).items():
            soft_total += 1
            if actual.get(name) in allowed:
                soft_ok += 1
            else:
                problems_soft.append(f"{name}='{actual.get(name)}' expected one of {allowed}")
        for name, banned in spec.get("forbidden", {}).items():
            if actual.get(name) in banned:
                problems_hard.append(f"{name}='{actual.get(name)}' is forbidden ({spec.get('note', '')})")

        if problems_hard:
            checks.append(Check("Acceptance", f"{doc_id}: key fields", "FAIL", "; ".join(problems_hard)))
        elif problems_soft:
            checks.append(Check("Acceptance", f"{doc_id}: key fields", "WARN",
                                "judgement fields differ: " + "; ".join(problems_soft)))
        else:
            checks.append(Check("Acceptance", f"{doc_id}: key fields", "PASS", ""))

    def pct(a, b):
        return round(100 * a / b, 1) if b else 0.0

    accuracy = {
        "hard_fields": f"{hard_ok}/{hard_total}", "hard_accuracy_pct": pct(hard_ok, hard_total),
        "judgement_fields": f"{soft_ok}/{soft_total}", "judgement_accuracy_pct": pct(soft_ok, soft_total),
        "overall_accuracy_pct": pct(hard_ok + soft_ok, hard_total + soft_total),
    }
    return checks, accuracy


def check_generated_content(v: Verifier, output_dir: Path, rows: list[dict]):
    from complaint_processor.guardrails import PLACEHOLDER_PATTERN, check_grounding
    from complaint_processor.ingestion import DocumentLoader, DocumentLoadError
    from complaint_processor.config import Settings

    settings = Settings.from_env(env_file=PROJECT_ROOT / ".env")
    loader = DocumentLoader(settings.max_file_size_mb, settings.max_document_chars)
    data_dir = PROJECT_ROOT / settings.data_dir
    allowed = f"{settings.company_name}\n{settings.email_signature}"

    for row in rows:
        if row["processing_status"] != "SUCCESS":
            continue
        name = row["file_name"]
        try:
            source = loader.load(data_dir / name).text
        except DocumentLoadError as exc:
            v.add("Content", f"{name}: source readable", "FAIL", str(exc))
            continue
        extraction = json.loads((output_dir / row["structured_data_file"]).read_text(encoding="utf-8"))["extraction"]
        email_text = (output_dir / row["customer_email_file"]).read_text(encoding="utf-8")
        summary_text = (output_dir / row["case_summary_file"]).read_text(encoding="utf-8")

        # --- email ---
        problems, warnings = [], []
        if not email_text.startswith("Subject:"):
            problems.append("no subject line")
        customer = extraction["customer_name"]
        expected_greeting = "Dear Customer" if customer == NOT_PROVIDED else customer.split()[0]
        if "Dear" not in email_text or expected_greeting.lower() not in email_text.lower()[:400]:
            problems.append(f"greeting does not address '{expected_greeting}'")
        if settings.email_signature.split(",")[0].lower() not in email_text.lower():
            warnings.append("configured signature not found")
        placeholders = PLACEHOLDER_PATTERN.findall(email_text)
        if placeholders:
            problems.append(f"placeholders {placeholders}")
        grounding = [i for i in check_grounding(email_text, source, allowed) if "placeholder" not in i]
        if grounding:
            warnings.append("possible ungrounded content: " + "; ".join(grounding))
        words = len(email_text.split())
        if not 60 <= words <= 400:
            warnings.append(f"length {words} words (target ~120-220)")
        status = "FAIL" if problems else "WARN" if warnings else "PASS"
        v.add("Content", f"{name}: customer email", status, "; ".join(problems + warnings))

        # --- summary ---
        missing = [s for s in SUMMARY_SECTIONS
                   if not re.search(rf"## {re.escape(s)}[^\n]*\n\s*\S", summary_text)]
        grounding = check_grounding(summary_text, source, allowed)
        status = "FAIL" if missing else "WARN" if grounding else "PASS"
        detail = (f"missing sections: {missing}" if missing else "") + (
            ("possible ungrounded content: " + "; ".join(grounding)) if grounding else "")
        v.add("Content", f"{name}: case summary", status, detail)


def step_acceptance(v: Verifier, output_dir: Path, rows: list[dict], expected: dict):
    header("6. Acceptance checks against labelled expected results")
    extractions = {}
    for path in (output_dir / "structured_data").glob("*.json"):
        extractions[path.stem] = json.loads(path.read_text(encoding="utf-8"))["extraction"]
    checks, v.accuracy = evaluate_extractions(expected["documents"], extractions)
    for c in checks:
        v.add(c.step, c.name, c.status, c.detail)
    print(f"\n  Extraction accuracy: hard {v.accuracy['hard_fields']} ({v.accuracy['hard_accuracy_pct']}%), "
          f"judgement {v.accuracy['judgement_fields']} ({v.accuracy['judgement_accuracy_pct']}%)\n")
    check_generated_content(v, output_dir, rows)


# ---------------------------------------------------------------------- #
#  7. Package
# ---------------------------------------------------------------------- #
def step_package(v: Verifier, allow_mock: bool) -> Path | None:
    header("7. Package submission")
    if v.count("FAIL"):
        v.add("Package", "Build zip", "SKIP", "not built because there are FAIL results")
        return None
    summary_path = PROJECT_ROOT / "output" / "run_summary.json"
    provider = json.loads(summary_path.read_text()).get("provider") if summary_path.exists() else None
    if provider in (None, "mock") and not allow_mock:
        v.add("Package", "Build zip", "SKIP",
              "output/ is missing or was produced by the mock provider; run live first (or --allow-mock-output)")
        return None

    dist = PROJECT_ROOT / "dist"
    dist.mkdir(exist_ok=True)
    zip_path = dist / f"{PROJECT_ROOT.name}-submission.zip"
    count = 0
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as archive:
        for path in iter_project_files():
            archive.write(path, f"{PROJECT_ROOT.name}/{path.relative_to(PROJECT_ROOT).as_posix()}")
            count += 1
    with zipfile.ZipFile(zip_path) as archive:
        names = archive.namelist()
    has_env = any(n.endswith("/.env") for n in names)
    v.add("Package", "Zip excludes .env", "FAIL" if has_env else "PASS")
    v.add("Package", "Zip built", "PASS", f"{zip_path.relative_to(PROJECT_ROOT)} ({count} files, "
          f"{zip_path.stat().st_size / 1024:.0f} KB)")
    return zip_path


# ---------------------------------------------------------------------- #
#  Report
# ---------------------------------------------------------------------- #
def write_report(v: Verifier, args, elapsed: float) -> None:
    verdict = "FAIL" if v.count("FAIL") else "PASS with warnings" if v.count("WARN") else "PASS"
    lines = [
        "# Verification Report", "",
        f"- **Generated:** {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
        f"- **Verdict:** {verdict}",
        f"- **Totals:** {v.count('PASS')} PASS, {v.count('WARN')} WARN, {v.count('FAIL')} FAIL, {v.count('SKIP')} SKIP",
        f"- **Live provider:** {v.live_provider or 'not run'}",
        f"- **Duration:** {elapsed:.0f}s",
        f"- **Options:** {' '.join(sys.argv[1:]) or '(defaults)'}", "",
    ]
    if v.accuracy:
        a = v.accuracy
        lines += ["## Extraction accuracy (labelled sample set)", "",
                  "| Metric | Result |", "|---|---|",
                  f"| Hard fields (name, email, phone, complaint flag, status) | {a['hard_fields']} ({a['hard_accuracy_pct']}%) |",
                  f"| Judgement fields (category, escalation, supporting doc, priority) | {a['judgement_fields']} ({a['judgement_accuracy_pct']}%) |",
                  f"| Overall | {a['overall_accuracy_pct']}% |", ""]
    for step in dict.fromkeys(c.step for c in v.checks):
        lines += [f"## {step}", "", "| Status | Check | Detail |", "|---|---|---|"]
        for c in (c for c in v.checks if c.step == step):
            detail = c.detail.replace("|", "\\|").replace("\n", " ")
            lines.append(f"| {c.status} | {c.name} | {detail} |")
        lines.append("")
    lines += ["---", "WARN = review manually (judgement calls or pattern-based grounding hints). "
              "FAIL = must be fixed before submission."]
    REPORT_FILE.write_text("\n".join(lines), encoding="utf-8")


# ---------------------------------------------------------------------- #
def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--skip-live", action="store_true", help="do not call a real LLM")
    parser.add_argument("--live-only", action="store_true", help="only live run + acceptance checks")
    parser.add_argument("--no-rerun", action="store_true", help="check existing output/ without re-running the LLM")
    parser.add_argument("--provider", choices=["openai", "azure_openai", "gemini"], help="override LLM_PROVIDER")
    parser.add_argument("--workers", type=int, help="override MAX_WORKERS for the live run")
    parser.add_argument("--package", action="store_true", help="build the submission zip if nothing FAILED")
    parser.add_argument("--allow-mock-output", action="store_true", help="allow packaging mock output (not advised)")
    args = parser.parse_args(argv)

    started = time.perf_counter()
    v = Verifier()
    expected = json.loads(EXPECTED_FILE.read_text(encoding="utf-8"))
    expected_errors = expected.get("expected_error_files", {})

    settings = step_environment(v, args.provider)
    if v.count("FAIL"):
        write_report(v, args, time.perf_counter() - started)
        print(f"\nEnvironment problems - see {REPORT_FILE.name}")
        return 1

    if not args.live_only:
        step_secrets(v)
        step_unit_tests(v)
        step_mock_run(v, expected_errors)

    if args.skip_live:
        header("5. Live pipeline run")
        v.add("Live run", "Live run", "SKIP", "--skip-live")
    elif settings is None and not args.no_rerun:
        header("5. Live pipeline run")
        v.add("Live run", "Live run", "SKIP", "no real LLM provider configured in .env")
    else:
        if settings is None:
            from complaint_processor.config import Settings
            settings = Settings.from_env(env_file=PROJECT_ROOT / ".env")
        rows = step_live_run(v, settings, expected_errors, rerun=not args.no_rerun, workers=args.workers)
        if rows:
            step_acceptance(v, PROJECT_ROOT / settings.output_dir, rows, expected)

    if args.package:
        step_package(v, args.allow_mock_output)

    elapsed = time.perf_counter() - started
    write_report(v, args, elapsed)
    print("\n" + "=" * 74)
    print(f"RESULT: {v.count('PASS')} PASS | {v.count('WARN')} WARN | {v.count('FAIL')} FAIL | "
          f"{v.count('SKIP')} SKIP   ({elapsed:.0f}s)")
    if v.accuracy:
        print(f"Extraction accuracy: {v.accuracy['overall_accuracy_pct']}% overall, "
              f"{v.accuracy['hard_accuracy_pct']}% on hard fields")
    print(f"Report: {REPORT_FILE}")
    print("=" * 74)
    return 1 if v.count("FAIL") else 0


if __name__ == "__main__":
    sys.exit(main())
