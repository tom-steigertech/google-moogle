#!/usr/bin/env python3
"""Test harness for the MoogleLLMClient - Claude on Bedrock with tool use.

Usage:
    # Make sure your AWS credentials are configured (env, profile, or instance role)
    # and the Claude model is enabled in Bedrock for your account/region.

    python test_llm_client.py "What is Final Fantasy VII about?"
    python test_llm_client.py "Tell me about Bone Chip" -v
    python test_llm_client.py "What drops Excalibur?" --model anthropic.claude-sonnet-4-6
"""

import argparse
import os
import sys

# Add parent directory to path for imports
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'lambda_functions'))

from processing.llm_client import MoogleLLMClient, DEFAULT_MODEL_ID


def main():
    parser = argparse.ArgumentParser(description='Test the Moogle LLM Client (Claude on Bedrock)')
    parser.add_argument('question', help='The question to ask')
    parser.add_argument('--model', default=DEFAULT_MODEL_ID,
                        help=f'Bedrock model ID (default: {DEFAULT_MODEL_ID}). '
                             f'Use amazon.nova-pro-v1:0 for higher quality, '
                             f'or anthropic.claude-haiku-4-5-20251001-v1:0 once Claude access is approved.')
    parser.add_argument('--region', default=None,
                        help='AWS region (default: BEDROCK_REGION/AWS_REGION/us-east-1)')
    parser.add_argument('--temperature', type=float, default=0.7, help='Response temperature')
    parser.add_argument('--max-tokens', type=int, default=1000, help='Max tokens in response')
    parser.add_argument('--personality', help='Path to custom personality file')
    parser.add_argument('--verbose', '-v', action='store_true', help='Enable debug logging')

    args = parser.parse_args()

    system_prompt = None
    if args.personality:
        with open(args.personality, 'r') as f:
            system_prompt = f.read()
        print(f"Loaded custom personality from {args.personality}")

    log_level = 'DEBUG' if args.verbose else 'INFO'
    client = MoogleLLMClient(
        model_id=args.model,
        region_name=args.region,
        log_level=log_level,
        system_prompt=system_prompt,
    )

    if not client.validate_configuration():
        print("Error: Client configuration is invalid")
        sys.exit(1)

    print(f"\nQuestion: {args.question}")
    print(f"Model: {args.model}")
    print(f"Region: {client.region_name}")
    print("-" * 50)

    try:
        messages = [{"role": "user", "content": args.question}]
        text, item_lookups = client.generate_response(
            messages=messages,
            max_tokens=args.max_tokens,
            temperature=args.temperature,
        )
        if item_lookups:
            print(f"\nItem lookups ({len(item_lookups)}):")
            for item in item_lookups:
                if item.get("found"):
                    print(f"  [{item['name']}] vendors={len(item.get('vendors',[]))} drops={item.get('drops_total',0)}")
                else:
                    print(f"  [not found: {item.get('name','?')}]")
        print(f"\nResponse:\n{text}")
        print("-" * 50)
        print(f"Response length: {len(text)} characters")
    except Exception as e:
        print(f"Error: {e}")
        sys.exit(1)


if __name__ == '__main__':
    main()
