---
description: Run the full automated verification (tests, offline run, live LLM run, acceptance checks) and explain the results
argument-hint: "[--skip-live | --live-only | --no-rerun | --package]"
allowed-tools: Bash(.venv/Scripts/python.exe scripts/verify_submission.py:*), Bash(.venv/bin/python scripts/verify_submission.py:*), Bash(python scripts/verify_submission.py:*), Read, Grep, Glob
---
Verify this project end to end.

1. Pick the interpreter: `.venv/Scripts/python.exe` if it exists (Windows), else `.venv/bin/python`, else `python`.
2. Run: `<python> scripts/verify_submission.py $ARGUMENTS`
   The live run calls the real LLM and can take a few minutes; let it finish.
3. Read `verification_report.md`.
4. Report back concisely:
   - The verdict, PASS/WARN/FAIL totals and the extraction accuracy.
   - For every FAIL: what failed, the root cause (read the relevant file in `output/`, the source document in
     `data/`, `logs/app.log` and the code as needed), and a specific proposed fix. Do NOT apply fixes without asking.
   - For WARNs: group them, say which are genuine concerns and which are acceptable judgement calls.
5. If everything passes, say which next step remains (e.g. `/review-outputs`, then `/package`).

Remember: the corrupted/empty/unsupported files in `data/` are supposed to fail, and never read `.env`.
