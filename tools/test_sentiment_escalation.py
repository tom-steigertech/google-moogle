#!/usr/bin/env python3
"""Self-checks for the sentiment-escalation + placeholder-update logic.

Covers the pure pieces that don't need AWS:
- _format_transcript / _original_goal helpers
- detect_repeated_negativity's "<2 user turns" short-circuit (no Bedrock call)

Run: python tools/test_sentiment_escalation.py
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'lambda_functions'))

from processing.llm_client import MoogleLLMClient, _format_transcript
from processing.handler import _original_goal


def test_format_transcript():
    msgs = [
        {"role": "user", "content": "where is excalibur"},
        {"role": "assistant", "content": [{"text": "In the wiki, kupo!"}]},  # block form
        {"role": "user", "content": "that's wrong"},
    ]
    out = _format_transcript(msgs)
    assert out == "User: where is excalibur\nAssistant: In the wiki, kupo!\nUser: that's wrong", out


def test_original_goal():
    prior = [
        {"role": "user", "content": "how much is a fire crystal"},
        {"role": "assistant", "content": "About 100 gil, kupo!"},
        {"role": "user", "content": "no"},
    ]
    assert _original_goal(prior, "still no") == "how much is a fire crystal"
    assert _original_goal([], "fresh question") == "fresh question"
    # no usable prior user turn -> fall back to current
    assert _original_goal([{"role": "assistant", "content": "hi"}], "cur") == "cur"


def test_negativity_short_circuit():
    # Fewer than two user turns can't be "twice" — must return False without a
    # Bedrock call (constructing the client never hits the network).
    c = MoogleLLMClient(model_id="amazon.nova-lite-v1:0", region_name="us-east-1")
    assert c.detect_repeated_negativity([
        {"role": "user", "content": "one message"},
        {"role": "assistant", "content": "reply"},
    ]) is False


if __name__ == "__main__":
    test_format_transcript()
    test_original_goal()
    test_negativity_short_circuit()
    print("All sentiment-escalation self-checks passed.")
