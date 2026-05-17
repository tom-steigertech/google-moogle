#!/usr/bin/env python3
"""
End-to-end multi-turn conversation test.

Two modes:
  --local   (default) Import processing modules directly. Fast, no Lambda needed.
  --lambda  Invoke the deployed Lambda twice with the same test session, then
            read AgentCore Memory directly to verify turns were saved and reloaded.

Run from the project root after sourcing env.sh:

    source env.sh
    AWS_PROFILE=steigertech-tsteiger python3 tools/test_conversation.py
    AWS_PROFILE=steigertech-tsteiger python3 tools/test_conversation.py --lambda
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import uuid

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(REPO_ROOT, "lambda_functions", "processing"))
sys.path.insert(0, os.path.join(REPO_ROOT, "lambda_functions"))

GREEN  = "\033[0;32m"
RED    = "\033[0;31m"
YELLOW = "\033[1;33m"
BLUE   = "\033[0;34m"
NC     = "\033[0m"

def ok(msg):   print(f"  {GREEN}✓{NC} {msg}")
def fail(msg): print(f"  {RED}✗{NC} {msg}")
def info(msg): print(f"  {YELLOW}→{NC} {msg}")
def head(msg): print(f"\n{BLUE}{msg}{NC}")


# ── conversation script ────────────────────────────────────────────────────────

TURNS = [
    "Where can I get Bone Chips in FFXI?",
    "What about getting them in Jugner Forest specifically?",
    "Are there any other good zones nearby?",
]

# Keywords that should appear when the model has context about the prior turns
CONTEXT_CHECKS = {
    1: lambda t: any(k in t.lower() for k in ("jugner", "bone chip", "bone", "skeleton")),
    2: lambda t: any(k in t.lower() for k in ("jugner", "bone", "zone", "forest", "area")),
}


# ══════════════════════════════════════════════════════════════════════════════
# LOCAL MODE — import modules directly
# ══════════════════════════════════════════════════════════════════════════════

def run_local(memory_id: str, idle_minutes: int = 30) -> bool:
    os.environ.setdefault("BEDROCK_REGION", "us-east-1")
    os.environ["AGENTCORE_MEMORY_ID"] = memory_id

    from memory_client import save_turn, load_recent_turns, clear_session
    from processing.llm_client import MoogleLLMClient

    test_id    = uuid.uuid4().hex[:8]
    actor_id   = f"testconv_{test_id}"
    session_id = f"testsess_{test_id}"
    llm        = MoogleLLMClient(log_level="WARNING")

    print()
    print(f"{BLUE}═══ Multi-Turn Conversation Test (local){NC}")
    print(f"  Memory ID : {memory_id}")
    print(f"  Actor     : {actor_id}")
    print(f"  Session   : {session_id}")

    passes = failures = 0

    for i, user_msg in enumerate(TURNS):
        head(f"[Turn {i+1}] \"{user_msg}\"")

        prior = load_recent_turns(memory_id, actor_id, session_id, idle_minutes)
        info(f"Loaded {len(prior)} prior turn(s)")
        expected_prior = i * 2
        if len(prior) == expected_prior:
            ok(f"Prior turn count correct ({len(prior)})")
            passes += 1
        else:
            fail(f"Expected {expected_prior} prior turns, got {len(prior)}")
            failures += 1

        messages = prior + [{"role": "user", "content": user_msg}]
        try:
            answer, _ = llm.generate_response(messages)
            ok(f"LLM responded ({len(answer)} chars)")
            info(f"  {answer[:200]}{'...' if len(answer) > 200 else ''}")
            passes += 1
        except Exception as e:
            fail(f"LLM error: {e}")
            failures += 1
            answer = "[error]"

        if i in CONTEXT_CHECKS:
            if CONTEXT_CHECKS[i](answer):
                ok("Context check: response uses prior conversation")
                passes += 1
            else:
                fail("Context check: response does NOT reference prior context")
                fail(f"  Full: {answer}")
                failures += 1

        try:
            save_turn(memory_id, actor_id, session_id, "user", user_msg)
            save_turn(memory_id, actor_id, session_id, "assistant", answer)
            ok("Turns saved")
            passes += 1
        except Exception as e:
            fail(f"save_turn failed: {e}")
            failures += 1

    head("[Final] Verify full history")
    all_turns = load_recent_turns(memory_id, actor_id, session_id, idle_minutes)
    expected = len(TURNS) * 2
    if len(all_turns) == expected:
        ok(f"Full history: {len(all_turns)} turns (expected {expected})")
        passes += 1
    else:
        fail(f"Full history: got {len(all_turns)}, expected {expected}")
        failures += 1

    head("[Cleanup]")
    try:
        deleted = clear_session(memory_id, actor_id, session_id)
        ok(f"Deleted {deleted} events")
    except Exception as e:
        fail(f"Cleanup failed (non-critical): {e}")

    _print_summary(passes, failures)
    return failures == 0


# ══════════════════════════════════════════════════════════════════════════════
# LAMBDA MODE — invoke deployed Lambda, verify via memory API
# ══════════════════════════════════════════════════════════════════════════════

def _make_sqs_event(request_id: str, actor_id: str, session_id: str,
                    user_msg: str) -> dict:
    msg = {
        "request_id": request_id,
        "payload": {
            "command": "/moogle",
            "text": user_msg,
            "user_id": "UTEST001",
            "channel_id": "CTEST001",
        },
        "timestamp": time.time(),
        "channel_id": "CTEST001",
        "thread_ts": None,
        "is_mention": False,
        "is_slash_command": True,
        "actor_id": actor_id,
        "session_id": session_id,
    }
    return {
        "Records": [{
            "messageId": f"test-{uuid.uuid4().hex[:8]}",
            "receiptHandle": f"test-receipt-{uuid.uuid4().hex[:8]}",
            "body": json.dumps(msg),
            "attributes": {},
            "messageAttributes": {},
            "md5OfBody": "abc",
            "eventSource": "aws:sqs",
            "eventSourceARN": "arn:aws:sqs:us-east-1:123:test",
            "awsRegion": "us-east-1",
        }]
    }


def _invoke_lambda(boto3_lambda, fn_name: str, payload: dict) -> dict:
    import tempfile
    with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
        json.dump(payload, f)
        path = f.name
    with open(path, "rb") as f:
        resp = boto3_lambda.invoke(
            FunctionName=fn_name,
            Payload=f.read(),
        )
    result = json.loads(resp["Payload"].read())
    return result


def run_lambda(memory_id: str, idle_minutes: int = 30) -> bool:
    import boto3

    region     = os.environ.get("BEDROCK_REGION", "us-east-1")
    lambda_cl  = boto3.client("lambda", region_name=region)
    agentcore  = boto3.client("bedrock-agentcore", region_name=region)
    fn_name    = "ff-moogle-bot-processing"

    test_id    = uuid.uuid4().hex[:8]
    actor_id   = f"testlambda_{test_id}"
    session_id = f"testlambdasess_{test_id}"

    print()
    print(f"{BLUE}═══ Multi-Turn Conversation Test (Lambda){NC}")
    print(f"  Memory ID : {memory_id}")
    print(f"  Actor     : {actor_id}")
    print(f"  Session   : {session_id}")
    print(f"  Note: Slack post will fail (test channel); memory save proceeds anyway.")

    passes = failures = 0

    for i, user_msg in enumerate(TURNS):
        head(f"[Turn {i+1}] \"{user_msg}\"")

        # Check memory BEFORE invoking (to verify prior turn was saved)
        if i > 0:
            info("Checking AgentCore Memory for prior turns before Lambda call...")
            try:
                resp = agentcore.list_events(
                    memoryId=memory_id,
                    actorId=actor_id,
                    sessionId=session_id,
                )
                events = resp.get("events") or resp.get("memoryEvents") or []
                expected = i * 2
                if len(events) == expected:
                    ok(f"Memory has {len(events)} events before this turn (expected {expected})")
                    passes += 1
                else:
                    fail(f"Memory has {len(events)} events, expected {expected}")
                    failures += 1
            except Exception as e:
                fail(f"list_events failed: {e}")
                failures += 1

        # Invoke Lambda
        request_id = f"test_{test_id}_{i}"
        payload    = _make_sqs_event(request_id, actor_id, session_id, user_msg)
        info(f"Invoking Lambda (request_id={request_id})...")
        try:
            result = _invoke_lambda(lambda_cl, fn_name, payload)
            ok(f"Lambda returned: {result}")
            passes += 1
        except Exception as e:
            fail(f"Lambda invocation failed: {e}")
            failures += 1
            continue

        # Short wait for AgentCore write to propagate
        time.sleep(2)

        # Check memory AFTER invoking (to verify this turn was saved)
        info("Checking AgentCore Memory for saved turns after Lambda call...")
        try:
            resp = agentcore.list_events(
                memoryId=memory_id,
                actorId=actor_id,
                sessionId=session_id,
            )
            events = resp.get("events") or resp.get("memoryEvents") or []
            expected = (i + 1) * 2
            if len(events) == expected:
                ok(f"Memory now has {len(events)} events (expected {expected}) — turns saved!")
                passes += 1
            else:
                fail(f"Memory has {len(events)} events after turn {i+1}, expected {expected}")
                info("  Turns were NOT saved by the Lambda.")
                failures += 1
        except Exception as e:
            fail(f"list_events failed: {e}")
            failures += 1

    # Cleanup
    head("[Cleanup]")
    try:
        from memory_client import clear_session
        os.environ.setdefault("BEDROCK_REGION", region)
        deleted = clear_session(memory_id, actor_id, session_id)
        ok(f"Deleted {deleted} events")
    except Exception as e:
        fail(f"Cleanup failed (non-critical): {e}")

    _print_summary(passes, failures)
    return failures == 0


# ── helpers ───────────────────────────────────────────────────────────────────

def _print_summary(passes: int, failures: int):
    print()
    print(f"{BLUE}═══ Results ═══{NC}")
    if failures == 0:
        print(f"  {GREEN}All {passes} checks passed!{NC}")
    else:
        print(f"  {RED}{failures} failure(s), {passes} pass(es){NC}")


def get_memory_id() -> str:
    memory_id = os.environ.get("AGENTCORE_MEMORY_ID", "")
    if memory_id:
        return memory_id
    info("AGENTCORE_MEMORY_ID not in env — fetching from Lambda config...")
    import boto3
    region = os.environ.get("AWS_DEFAULT_REGION", "us-east-1")
    cfg = boto3.client("lambda", region_name=region).get_function_configuration(
        FunctionName="ff-moogle-bot-processing"
    )
    memory_id = cfg.get("Environment", {}).get("Variables", {}).get("AGENTCORE_MEMORY_ID", "")
    if not memory_id:
        print(f"{RED}ERROR: Could not determine AGENTCORE_MEMORY_ID.{NC}")
        sys.exit(1)
    ok(f"Got memory ID from Lambda: {memory_id}")
    return memory_id


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--lambda", dest="use_lambda", action="store_true",
                        help="Invoke the deployed Lambda instead of running locally")
    args = parser.parse_args()

    memory_id = get_memory_id()
    if args.use_lambda:
        ok_result = run_lambda(memory_id)
    else:
        ok_result = run_local(memory_id)
    sys.exit(0 if ok_result else 1)
