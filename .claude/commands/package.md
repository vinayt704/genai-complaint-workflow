---
description: Re-check the existing live output and build the submission zip (never includes .env)
allowed-tools: Bash(.venv/Scripts/python.exe scripts/verify_submission.py:*), Bash(.venv/bin/python scripts/verify_submission.py:*), Bash(python scripts/verify_submission.py:*), Read
---
1. Pick the interpreter: `.venv/Scripts/python.exe` if it exists, else `.venv/bin/python`, else `python`.
2. Run `<python> scripts/verify_submission.py --no-rerun --package`
   (runs environment, secret scan, tests and mock run, re-checks the existing `output/` without new LLM calls,
   then builds `dist/<project>-submission.zip` only if there are no FAIL results).
3. Read `verification_report.md` and tell me: whether the zip was built, its path and size, and anything
   that blocked it. Confirm the secret scan passed and that `.env` is not in the zip.
