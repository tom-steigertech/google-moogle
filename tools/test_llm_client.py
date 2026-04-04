#!/usr/bin/env python3
"""Test harness for the MoogleLLMClient - test LLM in isolation.

Usage:
    export OPENAI_API_KEY='your-key-here'
    python test_llm_client.py "What is Final Fantasy VII about?"
    
    # Or with custom personality
    python test_llm_client.py "Tell me about chocobos" --personality "you_are_grumpy.txt"
"""

import sys
import os
import argparse

# Add parent directory to path for imports
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..', 'lambda_functions'))

from processing.llm_client import MoogleLLMClient


def main():
    parser = argparse.ArgumentParser(description='Test the Moogle LLM Client')
    parser.add_argument('question', help='The question to ask')
    parser.add_argument('--model', default='gpt-4o-mini', help='OpenAI model to use')
    parser.add_argument('--temperature', type=float, default=0.7, help='Response temperature')
    parser.add_argument('--max-tokens', type=int, default=1000, help='Max tokens in response')
    parser.add_argument('--personality', help='Path to custom personality file')
    parser.add_argument('--verbose', '-v', action='store_true', help='Enable debug logging')
    
    args = parser.parse_args()
    
    # Get API key
    api_key = os.environ.get('OPENAI_API_KEY')
    if not api_key:
        print("Error: OPENAI_API_KEY environment variable is required")
        sys.exit(1)
    
    # Load custom personality if provided
    system_prompt = None
    if args.personality:
        with open(args.personality, 'r') as f:
            system_prompt = f.read()
        print(f"Loaded custom personality from {args.personality}")
    
    # Initialize client
    log_level = 'DEBUG' if args.verbose else 'INFO'
    client = MoogleLLMClient(
        api_key=api_key,
        model=args.model,
        log_level=log_level,
        system_prompt=system_prompt
    )
    
    # Validate
    if not client.validate_configuration():
        print("Error: Client configuration is invalid")
        sys.exit(1)
    
    # Generate response
    print(f"\nQuestion: {args.question}")
    print(f"Model: {args.model}")
    print("-" * 50)
    
    try:
        response = client.generate_response(
            question=args.question,
            max_tokens=args.max_tokens,
            temperature=args.temperature
        )
        print(f"\nResponse:\n{response}")
        print("-" * 50)
        print(f"Response length: {len(response)} characters")
    except Exception as e:
        print(f"Error: {e}")
        sys.exit(1)


if __name__ == '__main__':
    main()
