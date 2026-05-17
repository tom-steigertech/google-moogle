#!/usr/bin/env python3
"""
Test AgentCore Memory directly — no Slack or Lambda required.

Saves conversation turns and reads them back to verify the full round-trip.
Run from the project root after sourcing env.sh:

    source env.sh
    python tools/test_memory.py

Requires AWS credentials with bedrock-agentcore permissions.
AGENTCORE_MEMORY_ID is read from the environment or fetched from the Lambda config.
"""

import json
import os
import sys
import uuid

import boto3

# ── Resolve paths so we can import processing modules directly ────────────────
REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(REPO_ROOT, "lambda_functions", "processing"))
sys.path.insert(0, os.path.join(REPO_ROOT, "lambda_functions"))

# ── Colours ───────────────────────────────────────────────────────────────────
GREEN = "\033[0;32m"
RED   = "\033[0;31m"
YELLOW = "\033[1;33m"
BLUE  = "\033[0;34m"
NC    = "\033[0m"

def ok(msg):  print(f"  {GREEN}✓{NC} {msg}")
def fail(msg): print(f"  {RED}✗{NC} {msg}")
def info(msg): print(f"  {YELLOW}→{NC} {msg}")


def get_memory_id() -> str:
    memory_id = os.environ.get("AGENTCORE_MEMORY_ID", "")
    if memory_id:
        return memory_id
    # Fall back to fetching from the Lambda function config
    info("AGENTCORE_MEMORY_ID not in env — fetching from Lambda config...")
    region = os.environ.get("AWS_DEFAULT_REGION", "us-east-1")
    client = boto3.client("lambda", region_name=region)
    config = client.get_function_configuration(FunctionName="ff-moogle-bot-processing")
    memory_id = config.get("Environment", {}).get("Variables", {}).get("AGENTCORE_MEMORY_ID", "")
    if not memory_id:
        print(f"{RED}ERROR: Could not determine AGENTCORE_MEMORY_ID.{NC}")
        print("Source env.sh or ensure the Lambda is deployed.")
        sys.exit(1)
    ok(f"Got memory ID from Lambda: {memory_id}")
    return memory_id


def run_test(memory_id: str):
    # Set env vars so memory_client's boto3 client uses the right region
    os.environ.setdefault("BEDROCK_REGION", "us-east-1")
    os.environ["AGENTCORE_MEMORY_ID"] = memory_id

    from memory_client import save_turn, load_recent_turns, _list_all_events

    test_id = uuid.uuid4().hex[:8]
    actor_id  = f"testactor{test_id}"
    session_id = f"testsession{test_id}"

    print()
    print(f"{BLUE}═══ AgentCore Memory Test ═══{NC}")
    print(f"  Memory ID : {memory_id}")
    print(f"  Actor ID  : {actor_id}")
    print(f"  Session ID: {session_id}")
    print()

    passes = 0
    failures = 0

    # ── 1. Save turn 1 ────────────────────────────────────────────────────────
    print(f"{BLUE}[1] Save user + assistant turn{NC}")
    try:
        save_turn(memory_id, actor_id, session_id, "user", "Where can I get bone chips?")
        ok("Saved user turn")
        passes += 1
    except Exception as e:
        fail(f"Save user turn failed: {e}")
        failures += 1

    try:
        save_turn(memory_id, actor_id, session_id, "assistant",
                  "Bone chips drop from skeletons in several zones, kupo! Try Gusgen Mines or Jugner Forest.")
        ok("Saved assistant turn")
        passes += 1
    except Exception as e:
        fail(f"Save assistant turn failed: {e}")
        failures += 1

    print()

    # ── 2. List raw events ────────────────────────────────────────────────────
    print(f"{BLUE}[2] List raw events in session{NC}")
    try:
        events = _list_all_events(memory_id, actor_id, session_id)
        ok(f"Found {len(events)} raw event(s)")
        for ev in events:
            info(f"  raw event keys: {list(ev.keys())}")
        passes += 1
    except Exception as e:
        fail(f"List events failed: {e}")
        failures += 1

    print()

    # ── 3. Load turns and verify ──────────────────────────────────────────────
    print(f"{BLUE}[3] Load turns back (expect 2){NC}")
    try:
        turns = load_recent_turns(memory_id, actor_id, session_id, idle_minutes=30)
        ok(f"Loaded {len(turns)} turn(s)")
        for t in turns:
            info(f"  [{t['role']}] {t['content'][:80]}")
        if len(turns) == 2:
            passes += 1
        else:
            fail(f"Expected 2 turns, got {len(turns)}")
            failures += 1
    except Exception as e:
        fail(f"Load turns failed: {e}")
        failures += 1

    print()

    # ── 4. Save follow-up turn ────────────────────────────────────────────────
    print(f"{BLUE}[4] Save follow-up turn and reload (expect 4){NC}")
    try:
        save_turn(memory_id, actor_id, session_id, "user", "Can I get them in Jugner Forest specifically?")
        save_turn(memory_id, actor_id, session_id, "assistant",
                  "Yes! Jugner Forest skeletons drop bone chips. Check G-7 and H-8, kupo!")
        ok("Saved follow-up turns")
        turns = load_recent_turns(memory_id, actor_id, session_id, idle_minutes=30)
        ok(f"Loaded {len(turns)} turn(s) total")
        for t in turns:
            info(f"  [{t['role']}] {t['content'][:80]}")
        if len(turns) == 4:
            passes += 1
        else:
            fail(f"Expected 4 turns, got {len(turns)}")
            failures += 1
    except Exception as e:
        fail(f"Follow-up test failed: {e}")
        failures += 1

    print()

    # ── 5. Clean up test data ─────────────────────────────────────────────────
    print(f"{BLUE}[5] Clean up test session{NC}")
    try:
        from memory_client import clear_session
        deleted = clear_session(memory_id, actor_id, session_id)
        ok(f"Deleted {deleted} test event(s)")
        passes += 1
    except Exception as e:
        fail(f"Cleanup failed (not critical): {e}")
        # Don't count as failure — cleanup is best-effort

    # ── Summary ───────────────────────────────────────────────────────────────
    print()
    print(f"{BLUE}═══ Results ═══{NC}")
    if failures == 0:
        print(f"  {GREEN}All {passes} checks passed — memory is working correctly!{NC}")
    else:
        print(f"  {RED}{failures} failure(s), {passes} pass(es){NC}")
        sys.exit(1)


if __name__ == "__main__":
    memory_id = get_memory_id()
    run_test(memory_id)
