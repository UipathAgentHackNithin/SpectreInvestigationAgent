"""
Manual test: verify queue search and log fetch for a known transaction reference.
Run from project root: uv run python scripts/test_queue_search.py
"""
import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import requests
from dotenv import load_dotenv
from spectre.auth import get_pat

load_dotenv()

TRANSACTION_REF = "INV-98768"
PROCESS_NAME = "ICSAUTO-3201 Invoice Performer"


def main():
    pat, base_url = get_pat()
    headers = {"Authorization": f"Bearer {pat}", "Content-Type": "application/json"}

    print(f"Base URL: {base_url}")
    print(f"Searching queue for reference containing: {TRANSACTION_REF}\n")

    # --- Step 1: Queue search ---
    resp = requests.get(
        f"{base_url}/orchestrator_/odata/QueueItems",
        headers=headers,
        params={"$filter": f"contains(Reference, '{TRANSACTION_REF}')"},
        timeout=10
    )
    print(f"Queue search status: {resp.status_code}")

    if resp.status_code != 200:
        print(f"Error: {resp.text}")
        return

    items = resp.json().get("value", [])
    print(f"Queue items found: {len(items)}")

    if not items:
        print("No queue items found — check that INV-98766 was added with Reference set correctly.")
        return

    for item in items:
        print(f"  - Id: {item.get('Id')} | Reference: {item.get('Reference')} | Status: {item.get('Status')}")
        print(f"    StartProcessing: {item.get('StartProcessing')} | EndProcessing: {item.get('EndProcessing')}")

    # --- Step 2: Fetch logs via orchestrator fetch_logs ---
    print(f"\nFetching logs via 3-layer strategy for '{TRANSACTION_REF}' / '{PROCESS_NAME}'...")
    from spectre.orchestrator import fetch_logs
    logs, source = fetch_logs(pat, base_url, TRANSACTION_REF, PROCESS_NAME)
    print(f"\n--- Log source: {source} ---")
    print(logs)


if __name__ == "__main__":
    main()
