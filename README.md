# AI Claims Processing Agent

Processes total-loss auto insurance claims: reads the documents in a claim
folder, extracts and cross-checks four required fields, decides a status,
and figures out what happens next (finalize / message the customer /
escalate to a human).

## Quick start

​```bash
pip install -r requirements.txt

# tesseract must also be installed at the OS level (apt install tesseract-ocr / brew install tesseract)

python3 main.py batch claims # process all 5 claims, write sample_output/\*.json
python3 main.py chat CLM-004 claims # interactive mode: play the customer, chat live
​```

By default the system runs with **zero external dependencies or API keys** --
the LLM-dependent steps (reading free-text customer replies, drafting the
customer message, live chat) fall back to rule-based logic. Set
`ANTHROPIC_API_KEY` (and optionally `CLAUDE_MODEL`, default `claude-sonnet-5`)
to use the Claude API for those steps instead, or set `OLLAMA_MODEL` (e.g.
`llama3.1`) with a local Ollama server running to use that instead. See
`agent/llm_client.py`. **Note:** `sample_output/` includes a run with a live
Ollama backend (`llama3.1`) -- CLM-002 and CLM-005 (the two claims with a
customer reply) exercise the LLM reply-parsing and message-drafting paths;
CLM-001, CLM-003, and CLM-004 never touch the LLM layer at all, since they
route straight to `escalate` with no customer message to draft.

---

## Architecture

​`
main.py                    CLI: batch mode + interactive chat mode
agent/
  ocr.py                   Tool: document_reader   -- PDF text / image OCR with deskew+denoise
  classify.py               Tool: document_classifier -- filename + content -> doc type
  extract.py                 Tool: field_extractor   -- regex extraction of the 4 required fields
  validate.py                 Tool: field_validator    -- VIN/date/numeric format checks
  dedupe.py                    Tool: duplicate_detector -- exact dupes vs. superseding revisions
                                       vs. genuinely ambiguous conflicting versions
  reconcile.py                  Tool: consistency_checker -- cross-doc agreement + best-guess policy
  llm_client.py                   LLM boundary -- reply parsing, message drafting, live chat
  agent.py                          ClaimsAgent -- orchestrator, decides which tools run per claim
  prioritize.py                      Ranks all processed claims into a recommended order
​`

Each claim goes through the same _shape_ of pipeline, but the actual tool
calls made are conditional on what's in the folder -- see "Tool Design"
below.

---

## What each required feature does, concretely

**Document intake / classification** -- every file is read (`ocr.py`) and
classified (`classify.py`) by filename first, content keywords second.
Anything that isn't `police_report` / `finance_agreement` / `settlement_breakdown`
is still read and reported (e.g. `adjuster_note.png`, `tow_receipt.png`)
but never blocks completeness. `agent.py` imports its required-type list
directly from `classify.py` rather than maintaining a second copy, so the
two can't drift out of sync.

**Field extraction** -- VIN, date of loss, insurance payout, and loan balance
are pulled via layout-aware regex (label -> nearby value), not an LLM call.
See "Why regex, not an LLM, for extraction" below. Confidence is derived
directly from document read quality (`high`/`medium`/`low` map 1:1 to
extraction confidence -- an earlier version silently upgraded any
non-`low`-quality OCR read to `high` confidence, which was wrong and has
been fixed). VIN extraction prefers a match found next to an explicit "VIN"
label over a blind scan for any 17-character alphanumeric string, since the
latter can false-positive on a claim or policy number.

**Cross-document consistency** -- for each field, every document that could
plausibly report it is compared (`reconcile.py`). Disagreements are flagged
with the exact source values, and a best guess is offered per this policy:
prefer a superseding revision over the doc it superseded, else prefer higher
extraction confidence, else majority vote, else give up and say so. Dates
are normalized before comparison (`02/14/2026` vs `2026-02-14` won't
false-flag as a conflict).

**Duplicates** -- `dedupe.py` distinguishes three patterns, comparing every
pair of files in a group (not just the first two, so 3+ files of the same
type are handled correctly):

1. **True duplicates** -- near-identical text, collapsed to one document.
2. **Superseding revisions** -- multiple versions with a "REVISED"/"supersedes"/
   version-number marker (e.g. CLM-003's `_v2` settlement breakdown);
   authoritative version chosen by parsed version _number_, not filename
   string sort (so `v10` doesn't lose to `v2`).
3. **Genuinely ambiguous conflicts** -- same doc type, differing content, no
   revision marker. Earlier versions of this code left a comment saying this
   case was "flagged for review" but nothing downstream actually read that
   flag, so a claim could silently reach `complete` despite an unresolved
   document conflict. Fixed: this case now sets `needs_review=True` on the
   document group, which `agent.py` turns into a real `issues` entry.

**Status decision** -- `incomplete` beats `needs_review` beats `complete`
when more than one applies (a claim with both a missing doc _and_ a VIN
conflict is `incomplete` -- get the doc first, the conflict might resolve
itself once it's in). See `agent.py::process` for the exact precedence.

**Multi-turn (customer reply)** -- the agent first processes the static
documents, computes what's missing/conflicted, _then_ reads
`customer_reply.txt` against that specific list of open questions
(`llm_client.parse_customer_reply`). Two different outcomes are handled
distinctly, which matters a lot in practice:

- the reply **fills in a truly missing field** -> accepted at medium
  confidence, tagged as self-reported, not silently treated as
  equivalent to an official document.
- the reply **disputes a value the system already had** (e.g. CLM-005:
  the settlement breakdown says the loss was 3/28, the customer's email
  says 3/22) -> **not** auto-corrected. It's logged as a flagged
  correction request for a human to confirm, because overwriting an
  official record from a paraphrased email is exactly the kind of thing
  that should have a person in the loop.

A partial/hedged answer ("balance is around $35,000, maybe a little more")
is detected and kept at reduced confidence rather than treated as a clean
resolution -- the spec explicitly warns against assuming a reply "fixes
everything," and this is where that shows up concretely. **Known limitation,
confirmed by direct testing, not just theorized:** if a customer replies in
two _separate_ rounds (a second `customer_reply.txt`, or a second chat
session), information resolved in round 1 is lost unless repeated in round
2 -- each run only considers whatever is in the reply file _at that moment_,
with no accumulation across rounds. Reproduced directly: resolved a VIN via
one simulated reply, then watched it revert to "unresolved conflict" after a
second, unrelated reply overwrote the file. See "What I'd do with more time."

**Interactive mode** -- `main.py chat <claim_id>` runs the same document
processing, then opens a live loop where you play the customer and the
agent (LLM-backed if configured, template-backed otherwise) asks about
whatever's actually missing for _that_ claim. On `done`, the transcript is
fed through the identical reconciliation path used for `customer_reply.txt`
and the claim is re-evaluated, so there's exactly one code path for
"new information arrived from the customer," whether it came from a file
or a live chat -- **except** when a `customer_reply.txt` already exists for
that claim (true for CLM-002 and CLM-005 in this dataset): to avoid silently
overwriting the real reply, the chat transcript is discarded and the tool
now says so explicitly, rather than the earlier behavior of silently
discarding it while still printing "chat transcript applied." Two other
known gaps, both the same class of issue as the status/prioritization
precedence bugs below: the live chat's context only carries missing
docs/fields, not conflict information, so it can ask the customer to
"confirm" a field that's actually stuck in a genuine document conflict
rather than a real gap; and interactive mode doesn't persist across
sessions any more than file-based replies do (same root cause as the
multi-round limitation above).

**Prioritization** -- `prioritize.py` buckets, in this exact order:
complete (finalize now) > official-document conflict (needs an adjuster,
checked _before_ both "reply in progress" and "incomplete", since neither
a customer reply nor simply missing paperwork can resolve two internal
documents disagreeing) > incomplete-with-reply-in-progress > incomplete-no-reply

> needs*review from low-confidence scans alone. This ordering went through
> two rounds of a real bug during development: an earlier version checked
> `status == "incomplete"` (and separately, `has_reply_progress`) \_before*
> checking for an official conflict, which meant a claim that was both
> missing a document/had a reply in progress AND had a genuine unresolved
> conflict got bucketed as "one more request/exchange closes this" -- hiding
> that an adjuster was actually needed regardless. Confirmed on real data
> (CLM-004 and CLM-005 both hit this) and fixed by reordering the conflict
> check first. `agent.py`'s `next_action` logic has the matching version of
> this fix: an official conflict always escalates, but if a _separate_,
> non-conflicted field or document is also missing, that's preserved in the
> escalation message rather than silently dropped (verified this doesn't
> happen with a naive "conflict always wins" reorder -- confirmed via CLM-004,
> which has both a conflicted VIN and a plainly-missing, non-conflicted
> insurance payout simultaneously).

---

## Tool Design (the core requirement)

**Tools built:** `document_reader`, `document_classifier`, `field_extractor`,
`field_validator`, `duplicate_detector`, `consistency_checker`, plus three
LLM-backed operations (`reply_parser`, `message_drafter`, `chat_reply`).

**How the agent decides when to use them** -- conditionally, per claim, not
as a fixed sequence:

- `document_reader` only OCRs image files / image-only PDFs; a clean
  text-layer PDF skips OCR entirely (cheaper, and higher-fidelity than
  round-tripping through image recognition unnecessarily).
- `duplicate_detector` only runs when a doc_type actually has more than one
  file behind it.
- `consistency_checker` only compares a field across documents that could
  plausibly contain it (VIN is checked across all three required types;
  insurance payout only exists on the settlement breakdown, so there's
  nothing to reconcile there in isolation).
- `reply_parser` only runs if a `customer_reply.txt` exists, and only asks
  about the specific fields/docs still open for _that_ claim -- including
  fields that are stuck in an unresolved conflict, not just plainly missing
  ones, since a customer reply can legitimately weigh in on either.
- `message_drafter` only runs when `next_action` actually needs a customer
  message; a complete claim never triggers it, and a conflicted field is
  never phrased as a question in that message.
- `escalate` vs `message_customer`: an official document conflict always
  escalates -- a customer can't referee two of the company's own documents
  disagreeing with each other. If there's _also_ a separate, non-conflicted
  gap the customer could help with, that's noted in the escalation message
  rather than silently dropped or wrongly asked of the customer. Only when
  there's no conflict at all does a genuinely customer-fixable gap route to
  `message_customer`.

**Why regex, not an LLM, for field extraction:** these documents have a
small, consistent set of layouts (label -> value, in a fixed handful of
phrasings). Regex extraction is deterministic, auditable (you can see
exactly which pattern matched and why), free, and doesn't hallucinate a
VIN that "looks about right." The LLM is reserved for the three places
that genuinely need language understanding: interpreting a free-text email,
writing a message a human will read, and holding a live conversation.
Keeping that boundary sharp also means the whole batch pipeline is
runnable and testable with zero API key/network dependency.

**What I considered but didn't build:**

- _VIN check-digit validation_ -- real VINs have a check-digit scheme
  (position 9, weighted-sum-mod-11). I actually implemented and tested this
  against the real VINs in this dataset before deciding against it: 4 of 5
  cleanly-extracted VINs failed the real checksum, since this dataset's
  VINs are synthetic and weren't generated with valid check digits. Shipping
  checksum validation would have made the system falsely reject most of its
  own correct extractions. Kept the simple 17-alphanumeric-character check
  the spec actually asks for.
- _A real conversational memory / ticket system for interactive mode_ -- the
  current chat mode is a single-session loop that writes one flat reply file
  at the end, and doesn't accumulate state across multiple sessions (see
  "Known limitations"). A production version would persist resolved fields
  incrementally and let the agent decide _mid-conversation_ when it has
  enough to close a gap.
- _An LLM fallback for classifying genuinely unknown documents_ -- right now
  "unknown" documents get a text snippet as a label and are otherwise
  ignored. An LLM call could take a real guess (e.g. "this is probably a
  repair estimate"), which would help the human reviewing the claim.
- _OCR confidence per-character (not per-document)_ -- Tesseract can report
  per-word confidence; using that instead of a coarse quality heuristic would
  make the "low confidence" flag more precise, at the cost of extra
  complexity for a 2-4 hour scope.

---

## Key decisions and tradeoffs

- **Deterministic tools + a thin LLM boundary**, rather than "ask the LLM to
  read the whole document and extract JSON." Slower to build than one big
  prompt, but every extraction and every disagreement is explainable, and
  the pipeline degrades gracefully (still fully functional) with no LLM
  configured at all.
- **OCR preprocessing (deskew + denoise + adaptive threshold) matters a lot**
  on this dataset -- verified empirically that raw Tesseract on the scanned
  images in `claims/CLM-004` recovers almost nothing (labels only, no data),
  while the preprocessed version recovers full narratives.
- **Never silently overwrite an official document's value from an email.**
  Confirmed correct via CLM-005: the settlement breakdown and the
  customer's email disagree on the date of loss, and the system correctly
  refuses to pick a winner automatically.
- **Status precedence (incomplete > needs_review > complete)** and
  **prioritization precedence (conflict beats both "reply in progress" and
  "incomplete")** are judgment calls, not given in the spec, and both went
  through real bugs during development that were caught by testing against
  actual claim data rather than assumed correct from reading the code.
- **A conflict is never phrased as a question to the customer.** This
  wasn't true in an earlier version -- CLM-004's drafted customer message
  used to literally ask the customer to help resolve a 3-way VIN
  disagreement between the company's own scanned documents. Fixed and
  verified: no claim's customer-facing message mentions "disagree,"
  "discrepancy," or "conflict" unless routed to `escalate` instead.

## Known limitations (confirmed via testing, not guessed)

- **Multi-round customer input doesn't accumulate.** A second reply file or
  chat session doesn't remember what an earlier one resolved -- reproduced
  directly by resolving a field in one simulated round and watching it
  revert to unresolved in the next.
- **Interactive chat's context doesn't distinguish "plainly missing" from
  "stuck in a document conflict."** It can ask the customer to confirm a
  field that no amount of customer input could actually fix.
- **Interactive mode on a claim that already has a `customer_reply.txt`**
  (CLM-002, CLM-005 in this dataset) discards the live chat transcript
  rather than merging it, since overwriting the real reply file would lose
  data. The tool now says so explicitly instead of silently discarding it.
- **LLM backend was exercised, but only on the reply-parsing/message-drafting
  path.** CLM-002 and CLM-005 ran through a live Ollama (`llama3.1`) backend
  for `reply_parser` and `message_drafter`; the other three claims never
  reach the LLM layer at all (they route to `escalate` before any customer
  message is drafted), and the live interactive `chat_reply` path
  specifically hasn't been run against a live model, only rule-based.
- **Near-duplicate detection uses a text-similarity threshold**, which
  could theoretically misclassify two genuinely different, boilerplate-heavy
  documents as duplicates. Mitigated in practice: `reconcile_field` still
  checks every file's extracted fields regardless of duplicate
  classification, so a real field-level mismatch surfaces even if the
  duplicate call was wrong.

## What I'd do with more time

- Persist resolved fields across multiple reply/chat rounds instead of
  re-deriving everything from scratch on each run.
- Pass conflict information into the interactive chat context so it stops
  asking the customer about fields no reply could actually fix.
- Actually run the full pipeline against a live LLM backend (Anthropic or
  Ollama) and diff the output against the rule-based baseline.
- Swap the coarse OCR quality heuristic for Tesseract's real per-word
  confidence scores.
- Add a small eval set (expected field values per claim) and a script that
  scores extraction accuracy automatically.
- Try the Jaseci/byLLM path for the extraction+reconciliation logic as a
  comparison against the current hand-rolled tool pipeline.

---

## Example output

See `sample_output/` for all 5 claims plus `prioritization.json`. Three
worth reading closely:

- `CLM-003.json` -- duplicate/revision handling (a `_v2` settlement
  breakdown that supersedes the original with different figures) _and_ an
  official VIN mismatch between the finance agreement and the other two
  documents.
- `CLM-004.json` -- a claim with _both_ a conflicted field (3-way VIN
  disagreement across noisy scans) and a separately, plainly missing field
  (insurance payout) at the same time -- exercises the dual-path fix where
  the conflict escalates without silently dropping the other gap.
- `CLM-005.json` -- the multi-turn path: a missing finance agreement, a
  date-of-loss conflict between two official documents, and a customer
  reply that disputes the system's best guess -- correctly logged as a
  flagged correction request rather than auto-applied.

Testing methodology, briefly: every fix in this codebase was verified
against the actual 5 sample claims (not just read for correctness), with a
few additionally verified via constructed synthetic cases where the real
dataset didn't naturally exercise a code path (e.g. the two priority
buckets, out of five, that none of the 5 sample claims land in).

---

## `ai_usage/`

See `ai_usage/README.md`.
