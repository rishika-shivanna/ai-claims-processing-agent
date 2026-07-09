#!/usr/bin/env python3
"""
Usage:
    python3 main.py batch [claims_dir]          # process every claim, print + save JSON
    python3 main.py chat <claim_id> [claims_dir] # interactive customer chat for one claim

batch mode is the take-home's required "process the folder" behavior.
chat mode is the "highly encouraged" interactive mode: you play the customer,
the agent responds live, and on `done` it re-evaluates the claim with
whatever the conversation resolved -- same code path as the customer_reply.txt
handling, just fed from a live transcript instead of a file.
"""
from __future__ import annotations
import json
import os
import sys

from agent.agent import ClaimsAgent
from agent.prioritize import prioritize
from agent import llm_client


def run_batch(claims_dir: str, out_dir: str = "sample_output"):
    os.makedirs(out_dir, exist_ok=True)
    results = []
    for name in sorted(os.listdir(claims_dir)):
        path = os.path.join(claims_dir, name)
        if not os.path.isdir(path):
            continue
        print(f"Processing {name}...")
        result = ClaimsAgent(path).process()
        results.append(result)
        with open(os.path.join(out_dir, f"{name}.json"), "w") as f:
            json.dump(result, f, indent=2)

    ranking = prioritize(results)
    with open(os.path.join(out_dir, "prioritization.json"), "w") as f:
        json.dump(ranking, f, indent=2)

    print("\n=== Summary ===")
    for r in results:
        print(f"  {r['claim_id']}: {r['status']} -> {r['next_action']['type']}")
    print("\n=== Recommended processing order ===")
    for item in ranking["processing_order"]:
        print(f"  {item['claim_id']}: {item['reason']}")
    return results, ranking


def run_chat(claim_id: str, claims_dir: str):
    path = os.path.join(claims_dir, claim_id)
    if not os.path.isdir(path):
        print(f"No such claim: {claim_id}")
        return

    print(f"Processing {claim_id} from its documents first...\n")
    initial = ClaimsAgent(path).process()
    print(json.dumps({k: v for k, v in initial.items() if k != "tools_used"}, indent=2))

    if initial["status"] == "complete":
        print("\nNothing outstanding -- no customer input needed. Exiting chat.")
        return

    claim_context = {
        "claim_id": claim_id,
        "missing_docs": initial["documents"]["missing"],
        "missing_fields": [f for f, v in initial["extracted_fields"].items() if not v["value"]],
    }
    print("\n--- Interactive customer chat (type as the customer; 'done' to finish) ---\n")

    conversation = []
    opening, _ = llm_client.chat_reply(claim_context, [{"role": "assistant", "content": "(open the conversation)"}])
    print(f"Agent: {opening}\n")
    conversation.append({"role": "assistant", "content": opening})

    transcript_lines = []
    while True:
        try:
            user_msg = input("You (customer): ")
        except EOFError:
            break
        if user_msg.strip().lower() == "done":
            break
        conversation.append({"role": "user", "content": user_msg})
        transcript_lines.append(user_msg)
        reply, _ = llm_client.chat_reply(claim_context, conversation)
        print(f"Agent: {reply}\n")
        conversation.append({"role": "assistant", "content": reply})

    if transcript_lines:
        # Feed the transcript through the same reconciliation path as a
        # customer_reply.txt file, then re-run the claim.
        tmp_reply_path = os.path.join(path, "customer_reply.txt")
        already_had_reply = os.path.exists(tmp_reply_path)
        if already_had_reply:
            print("\n--- NOTE: this claim already has a customer_reply.txt on disk. "
                  "To avoid overwriting it, this chat transcript was NOT applied -- "
                  "re-evaluating with the EXISTING reply file only. ---\n")
        else:
            with open(tmp_reply_path, "w") as f:
                f.write("\n".join(transcript_lines))
            print("\n--- Re-evaluating claim with chat transcript applied ---\n")
        try:
            updated = ClaimsAgent(path).process()
            print(json.dumps({k: v for k, v in updated.items() if k != "tools_used"}, indent=2))
        finally:
            if not already_had_reply:
                os.remove(tmp_reply_path)


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(1)

    mode = sys.argv[1]
    if mode == "batch":
        claims_dir = sys.argv[2] if len(sys.argv) > 2 else "claims"
        run_batch(claims_dir)
    elif mode == "chat":
        if len(sys.argv) < 3:
            print(__doc__)
            sys.exit(1)
        claim_id = sys.argv[2]
        claims_dir = sys.argv[3] if len(sys.argv) > 3 else "claims"
        run_chat(claim_id, claims_dir)
    else:
        print(__doc__)
