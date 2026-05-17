"""Runaway protection: sets reserved concurrency to 0 on the initial Lambda.

Triggered by a CloudWatch alarm via SNS when the invocation rate is too high.
To re-enable the bot after a throttle, run:
  aws lambda delete-function-concurrency --function-name <initial-function-name>
"""

import logging
import os

import boto3

logger = logging.getLogger()
logger.setLevel(logging.INFO)


def handler(event, context):
    fn_name = os.environ["TARGET_FUNCTION_NAME"]
    client = boto3.client("lambda")

    client.put_function_concurrency(
        FunctionName=fn_name,
        ReservedConcurrentExecutions=0,
    )

    logger.info(
        "RUNAWAY PROTECTION TRIGGERED: set reserved_concurrent_executions=0 "
        f"on {fn_name}. Bot is now throttled. API Gateway will return 429."
    )
    return {"status": "throttled", "function": fn_name}
