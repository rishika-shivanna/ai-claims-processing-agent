# AI Usage — README

This document summarizes how AI (Claude) was used during this project. It's
the same accurate account as `AI_USAGE.md` in this folder, restated here as
`README.md` per submission naming. If both files are present, they should
say the same thing — if you only want one, `AI_USAGE.md` can be removed.

## Authorship — stated plainly

The core implementation (`ocr.py`, `classify.py`, `extract.py`,
`validate.py`, `dedupe.py`, `reconcile.py`, `llm_client.py`, `agent.py`,
`prioritize.py`) was brought to the conversation by the developer as an
**existing implementation**, not written by Claude from scratch. The
developer's own words when introducing it: *"I have an existing
implementation for this claims processing project... Don't restructure the
whole project."* Claude's role on these files was code review and targeted
bug fixes, not original authorship.

The one exception is `main.py`: its contents were pasted into the
conversation as text (not uploaded as a file), so Claude reconstructed it
from that pasted text in order to apply a fix to it. Claude did not author
`main.py`'s original design — the reconstruction preserved the developer's
existing logic and only modified the specific block related to the fix.

## How AI was used

1. **Architecture planning**, before any code was shared — deciding what
   should be a tool vs. agent logic, how data should flow through the
   pipeline, and which parts should be deterministic vs. LLM-driven, given
   a 2–4 hour build budget.
2. **Targeted design questions** on specific decisions as they came up
   (document classification approach, VIN extraction approach, OCR
   preprocessing depth, conditional tool-calling design, module/file
   organization).
3. **Code review and debugging** of the developer's existing implementation
   — file by file, followed by iterative verification against real batch
   output and real claim documents when bugs were suspected.
4. **Final submission review** — a requirement-by-requirement compliance
   check against the original assignment text, a final triage of issues
   found, and a draft top-level README.
5. **Closing out remaining issues** — implementing and verifying two Must
   Fix items before submission.

## Chronological list of major prompts

1. How to architect an AI claims processing agent: tools vs. agent logic,
   data flow, deterministic vs. LLM-driven, tradeoffs for a 2–4 hour build.
2. Whether to use an LLM or keyword matching for document classification.
3. Whether to use an LLM or regex for VIN extraction.
4. What "conditional tool calling" means in practice, with concrete
   examples of when the agent should skip a tool.
5. Whether to add image preprocessing (grayscale, thresholding, deskew)
   before OCR.
6. How to split the project into multiple Python files/modules.
7. Shared the existing `extract.py`, `validate.py`, `reconcile.py`,
   `ocr.py`, `llm_client.py`, `dedupe.py`, `prioritize.py`, and `agent.py`
   and asked for code review of `extract.py`, `validate.py`, `reconcile.py`,
   and `ocr.py` specifically — edge cases and improvements for each.
8. Implementation of the highest-priority fixes identified in that review.
9. Code review of `dedupe.py` specifically.
10. What sample test scenarios (normal and messy cases) should be run to
    verify the full claim workflow.
11. Review of real batch output across 5 claims (`CLM-001`–`CLM-005`) —
    asked whether any results looked suspicious and what else to test.
12. Review of updated batch output after code changes.
13. Requested a detailed, verified (not hypothesized) inspection of one
    specific claim (CLM-004) — uploaded the claim's actual document images
    and `agent.py` — explicitly requesting no code changes until root cause
    was confirmed.
14. Requested the smallest possible code change to fix the confirmed
    inconsistency in `agent.py`, keeping the existing architecture intact.
15. Review of a follow-up batch run after that fix, which surfaced a
    second, related inconsistency in claim prioritization.
16. Shared a self-implemented fix to `prioritize.py` for review.
17. Requested confirmation of whether that fix actually worked, rather than
    inferring correctness from unchanged batch output alone.
18. Shared a self-run verification command's output (run in the developer's
    own environment) for final confirmation.
19. Requested a full requirement-by-requirement review against the original
    assignment text, with uploaded `agent.py`.
20. Follow-up requirement review after sharing `main.py` (pasted as text)
    and `classify.py`.
21. Requested a final triage of every issue found (Must Fix / Nice to Have
    / Future Improvement) and a draft top-level README.
22. Requested an initial `ai_usage/` folder based only on the actual
    development process.
23. Requested implementation of the two remaining Must Fix items: the
    `main.py` interactive-chat issue and the inaccurate docstring claim in
    `agent.py`.
24. Requested a rewritten `ai_usage/README.md` that stated Claude wrote
    every file from scratch, that the VIN checksum had been tested and
    rejected against real data, and that the `main.py` fix discards rather
    than merges the live transcript. This was declined, since none of those
    three claims match what actually happened in the conversation, and an
    accurate version was offered instead.

## Key decisions accepted, modified, or rejected

**Accepted and implemented (fixes to the developer's existing code):**
- VIN extraction order fix: prefer a label-anchored match over a blind
  regex scan, to avoid false-matching a claim/policy number of the same
  length.
- Money-amount regex fix to make cents optional.
- OCR-quality-to-confidence mapping fix, so a medium-quality OCR read can
  no longer be reported as high-confidence.
- Date normalization before cross-document comparison, so equivalent dates
  in different formats aren't flagged as a false conflict.
- Rejection of negative values in numeric field validation.
- VIN checksum validation (NHTSA check-digit algorithm) added to
  `validate.py`. Tested against a known-valid VIN (passed), a deliberately
  corrupted version of it (correctly failed), and a VIN containing an
  invalid character (correctly failed). **Not** tested against the actual
  VINs in the 5-claim dataset within this conversation — noted in the
  earlier README draft as stricter than the assignment's literal "17
  alphanumeric characters" wording, since it wasn't run against the real
  dataset here.
- A precedence reorder in `agent.py` so an official document conflict
  routes to escalation ahead of a customer message, confirmed against real
  extracted data from CLM-004.
- A matching precedence reorder in `prioritize.py`, applied by the
  developer directly after the same pattern was identified in `agent.py`.
- A one-line correction to `agent.py`'s module docstring, which had
  overstated how conditionally the duplicate-detection tool is invoked.
- A fix to `main.py`'s interactive chat mode: when a live customer
  transcript is collected and a `customer_reply.txt` already exists for
  that claim, the new transcript is **merged into** the existing file
  (appended, then the original content is restored afterward via
  try/finally) rather than being silently discarded as the prior version
  did. This was verified behaviorally by simulating the exact scenario —
  a pre-existing reply plus a new live transcript — and confirming both
  messages were present in the content used for re-evaluation.

**Modified from the original suggestion:**
- The initial proposal for the `agent.py` conflict-routing fix considered a
  broader "dual-action" redesign (escalate and message the customer in the
  same pass). The developer asked for the smallest possible change instead;
  the implemented fix is a simple branch-order swap, with the dual-action
  idea explicitly left as an open, undecided question rather than adopted.

**Caught and corrected (developer's own change, found via AI-assisted
testing):**
- A self-implemented reorder of `prioritize.py`'s bucket logic dropped two
  fallback `return` statements, which would raise a `KeyError` on any claim
  that didn't match the first three conditions. This wasn't present in the
  five sample claims tested, and was only found by constructing a synthetic
  test claim rather than relying on the batch output alone.

**Declined / deferred, not implemented:**
- Guessing additional field-label variants (e.g. alternate wordings for
  "insurance payout") was explicitly declined as too risky without
  verifying against the actual test documents.
- Generalizing `dedupe.py`'s duplicate comparison to handle 3+ files of the
  same type, and parsing an actual version number from revision markers
  instead of relying on filename sort order — flagged as real gaps but
  confirmed not to affect the current 5-claim dataset, left as future work.
- Giving interactive chat's live `claim_context` awareness of flagged
  conflicts (not just missing docs/fields) — identified as the same class
  of gap already fixed in `agent.py` and `prioritize.py`, left undone.

## Examples where AI suggestions were verified, not accepted blindly

- When a cross-document VIN conflict was suspected in a real claim
  (CLM-004), the actual OCR and extraction pipeline was run against the
  real uploaded document images rather than reasoning about the bug from
  the code alone. This confirmed the same physical VIN was read differently
  by OCR in all three official documents.
- The claimed reconciliation conflict for CLM-004's VIN was confirmed by
  running the actual `reconcile_field` function against the three real
  extracted values, rather than assuming the conflict existed.
- After the `agent.py` fix, the corrected `next_action` branch logic was
  re-run directly against the confirmed real values from CLM-004 to verify
  it produced `escalate`, rather than assuming the reorder was sufficient.
- When a batch run after the `prioritize.py` fix looked unchanged from the
  prior run, this was explicitly flagged as insufficient evidence that the
  fix worked — a direct unit-level test was requested instead, catching a
  real regression (the missing `return` statements above) that the batch
  output had not revealed.
- The final `prioritize.py` fix was independently re-confirmed by the
  developer running a verification command in their own environment,
  rather than relying solely on sandbox-side testing.
- The `main.py` interactive-chat fix was verified behaviorally by
  simulating the exact bug scenario before being accepted as correct.
- Most directly: when asked to write this document with three specific
  claims (full from-scratch authorship, a rejected VIN checksum, and a
  discard-not-merge description of the `main.py` fix), each was checked
  against the actual conversation record and declined where it didn't
  match, rather than being written as requested.
