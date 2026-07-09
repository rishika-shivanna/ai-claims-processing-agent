"""
LLM client used for the parts of the pipeline that need actual judgment
rather than deterministic parsing:
  - reading a free-text customer reply email and figuring out what it does
    and doesn't answer
  - drafting the customer-facing message
  - interactive chat mode (playing claims agent in a live conversation)

Three backends, auto-selected in this order:
  1. Anthropic API, if ANTHROPIC_API_KEY is set (model via CLAUDE_MODEL,
     default "claude-sonnet-5").
  2. Local Ollama, if OLLAMA_HOST/OLLAMA_MODEL are set or a local Ollama
     server responds on localhost:11434. Set OLLAMA_MODEL (e.g. "llama3.1").
  3. Rule-based fallback -- keeps the whole system runnable with zero
     external dependencies/keys, at the cost of shallower language
     understanding on the free-text bits. Used automatically in CI/sandboxes
     with no model access.

This is intentionally the ONLY place in the codebase that talks to an LLM.
Every other tool (extraction, validation, reconciliation, dedupe) is plain
deterministic Python, which keeps the audit trail explainable and cheap.
"""
from __future__ import annotations
import json
import os
import re
import urllib.request


def _anthropic_available() -> bool:
    return bool(os.environ.get("ANTHROPIC_API_KEY"))


def _ollama_available() -> bool:
    host = os.environ.get("OLLAMA_HOST", "http://localhost:11434")
    try:
        urllib.request.urlopen(f"{host}/api/tags", timeout=1)
        return True
    except Exception:
        return False


def _call_anthropic(system: str, user: str) -> str:
    import anthropic
    client = anthropic.Anthropic()
    model = os.environ.get("CLAUDE_MODEL", "claude-sonnet-5")
    resp = client.messages.create(
        model=model,
        max_tokens=1000,
        system=system,
        messages=[{"role": "user", "content": user}],
    )
    return "".join(b.text for b in resp.content if b.type == "text")


def _call_ollama(system: str, user: str) -> str:
    host = os.environ.get("OLLAMA_HOST", "http://localhost:11434")
    model = os.environ.get("OLLAMA_MODEL", "llama3.1")
    payload = json.dumps({
        "model": model,
        "prompt": f"{system}\n\n{user}",
        "stream": False,
    }).encode()
    req = urllib.request.Request(f"{host}/api/generate", data=payload,
                                  headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=60) as resp:
        return json.loads(resp.read())["response"]


def call_llm(system: str, user: str) -> tuple[str, str]:
    """Returns (response_text, backend_used)."""
    if _anthropic_available():
        return _call_anthropic(system, user), "anthropic"
    if _ollama_available():
        return _call_ollama(system, user), "ollama"
    return None, "none"


# ---------------------------------------------------------------------------
# Task-specific helpers. Each has a rule-based fallback so the pipeline is
# fully runnable without any model configured.
# ---------------------------------------------------------------------------

def draft_customer_message(claim_id: str, missing_docs: list[str],
                            missing_fields: list[str], issues: list[dict]) -> tuple[str, str]:
    system = ("You are a courteous auto-insurance claims assistant. Write a short, "
               "plain-language email to the customer explaining what's still needed to "
               "process their total-loss claim. Be specific, empathetic, and never invent "
               "information not given to you.")
    user = (f"Claim: {claim_id}\nMissing documents: {missing_docs}\n"
            f"Missing/unresolved fields: {missing_fields}\n"
            f"Issues: {json.dumps(issues)}\nWrite the email now.")
    text, backend = call_llm(system, user)
    if text:
        return text.strip(), backend

    # Rule-based fallback template.
    lines = [f"Subject: Additional information needed for your claim {claim_id}", "",
              "Hi,", "", "Thanks for your patience while we process your total-loss claim. "
              "To keep things moving, we still need the following:"]
    for d in missing_docs:
        lines.append(f"  - {d.replace('_', ' ').title()}")
    for f in missing_fields:
        lines.append(f"  - Confirmation of your {f.replace('_', ' ')}")
    for issue in issues:
        if issue["type"] == "inconsistency":
            lines.append(f"  - A discrepancy we need help resolving: {issue['description']}")
    lines += ["", "Please reply to this email with the above at your earliest convenience.",
              "", "Thank you,", "Claims Department"]
    return "\n".join(lines), "rule_based"


def parse_customer_reply(claim_id: str, reply_text: str, open_questions: list[str]) -> tuple[dict, str]:
    """Figure out what the reply resolves. Returns a dict:
    {"resolved": {field: value}, "still_open": [...], "notes": "..."}"""
    system = ("You are extracting structured updates from a customer's reply email "
              "about their insurance claim. Given the open questions, return ONLY a JSON "
              "object: {\"resolved\": {<field>: <value or note>}, \"still_open\": [<field>...], "
              "\"notes\": <short string>}. Do not assume a question is resolved unless the "
              "customer actually gave that information -- partial or vague answers "
              "(e.g. 'around $35,000') should be noted but NOT marked fully resolved unless "
              "that's the best available figure.")
    user = f"Open questions: {open_questions}\n\nCustomer reply:\n{reply_text}"
    text, backend = call_llm(system, user)
    if text:
        try:
            cleaned = re.sub(r"^```json|```$", "", text.strip(), flags=re.MULTILINE).strip()
            return json.loads(cleaned), backend
        except Exception:
            pass

    # Rule-based fallback: look for dollar amounts, dates, and VIN-like tokens
    # in the reply, plus hedge words that signal an uncertain/partial answer.
    # Email header lines (From/To/Date/Subject) are stripped first so the
    # "Date:" timestamp of the email itself doesn't get mistaken for a date
    # the customer is actually discussing (e.g. a disputed date of loss).
    body_lines = [ln for ln in reply_text.splitlines()
                  if not re.match(r"^\s*(From|To|Date|Subject)\s*:", ln, re.IGNORECASE)]
    body = "\n".join(body_lines)

    resolved, still_open, notes = {}, [], []
    hedge_words = ("around", "about", "approximately", "roughly", "i believe", "maybe", "i think")
    is_hedged = any(h in body.lower() for h in hedge_words)

    money = re.search(r"\$?\s?[\d,]+(?:\.\d{2})?", body)
    date = re.search(r"\b(?:January|February|March|April|May|June|July|August|September|"
                      r"October|November|December)\s+\d{1,2}(?:st|nd|rd|th)?|"
                      r"\d{1,2}/\d{1,2}/\d{2,4}", body, re.IGNORECASE)
    vin = re.search(r"\b[A-HJ-NPR-Z0-9]{17}\b", body.upper())

    for q in open_questions:
        if "loan_balance" in q or "balance" in q or "insurance_payout" in q or "payout" in q:
            if money:
                val = f"~{money.group(0)}" if is_hedged else money.group(0)
                resolved[q] = val
                if is_hedged:
                    notes.append(f"{q}: customer gave an approximate figure, not exact -- verify with lender.")
                else:
                    still_open.append(q) if False else None
            else:
                still_open.append(q)
        elif "date_of_loss" in q or "date" in q:
            if date:
                resolved[q] = date.group(0)
                notes.append(f"{q}: customer disputes/clarifies the date -- verify against official record.")
            else:
                still_open.append(q)
        elif "vin" in q:
            if vin:
                resolved[q] = vin.group(0)
            else:
                still_open.append(q)
        elif "police_report" in q or "document" in q:
            still_open.append(q)
            notes.append(f"{q}: customer acknowledged the request but has not yet provided the document.")
        else:
            still_open.append(q)

    return {"resolved": resolved, "still_open": still_open, "notes": " ".join(notes)}, "rule_based"


def chat_reply(claim_context: dict, conversation: list[dict]) -> tuple[str, str]:
    system = ("You are a claims assistant chatting live with a customer about their total-loss "
              "auto insurance claim. Be warm, concise, and specific about what you still need. "
              "Claim context (internal, don't dump verbatim): " + json.dumps(claim_context))
    user = "\n".join(f"{m['role']}: {m['content']}" for m in conversation)
    text, backend = call_llm(system, user)
    if text:
        return text.strip(), backend
    missing = claim_context.get("missing_fields", []) + claim_context.get("missing_docs", [])
    if missing:
        return (f"Thanks for reaching out about claim {claim_context.get('claim_id')}. "
                f"We're still missing: {', '.join(missing)}. Could you help with those?"), "rule_based"
    return "Thanks -- your claim looks complete on our end. We'll follow up shortly.", "rule_based"
