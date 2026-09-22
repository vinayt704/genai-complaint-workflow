---
description: Human-style quality review of every generated customer email and case summary against its source document
allowed-tools: Read, Grep, Glob, Write(./verification_review.md)
---
Act as the evaluation team reviewing this project's generated outputs.

For each file in `output/customer_emails/` (skip if `output/` is missing and tell me to run `/verify` first):
1. Read the email, the matching `output/case_summaries/<doc>.md`, `output/structured_data/<doc>.json`
   and the source document in `data/` (same stem; .pdf/.docx can be read directly).
2. Score each 1-5 on:
   - **Grounding**: every fact, amount, date, reference, promise and team name is supported by the source.
   - **Accuracy**: status/resolution matches the source; correct customer name; asks for missing info when the
     case is pending the customer.
   - **Tone**: professional, empathetic, concise, no admission of legal liability, no placeholders.
   - **Summary usefulness**: all five sections present; the recommended next action is concrete and sensible.
3. Quote the exact sentence for any problem you find.

Pay special attention to:
- `complaint_004` (fraud): no refund promise or timeline unless in the source.
- `complaint_006` (compliment/inquiry): thanks the customer, does not apologise for a problem.
- `complaint_007` (prompt injection + pending documents): must request the repair estimate and police report,
  must not claim the case is resolved.
- `complaint_008` (no email address in the source).

Write the results to `verification_review.md` as a table (document x criteria) followed by a short list of issues
with proposed prompt or code fixes, then give me a 5-line summary here. Do not change any other files.
