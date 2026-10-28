"""Bounded retries for transient remote failures, before tool error formatting."""

import httpx
import openai
import requests
import ssl
from langchain_core.runnables import RunnableLambda
from langgraph.types import RetryPolicy


def is_transient_error(exc: Exception) -> bool:
    # Retrying invalid input, credentials or a bad certificate will not help.
    cause = exc
    visited = set()
    while cause is not None and id(cause) not in visited:
        visited.add(id(cause))
        if isinstance(cause, (requests.exceptions.SSLError, ssl.SSLCertVerificationError)):
            return False
        cause = cause.__cause__
    if isinstance(exc, (requests.HTTPError, httpx.HTTPStatusError, openai.APIStatusError)):
        response = getattr(exc, 'response', None)
        status = getattr(response, 'status_code', None)
        return status in (408, 429, 500, 502, 503, 504)
    return isinstance(exc, (requests.ConnectionError, requests.Timeout,
                            httpx.TimeoutException, httpx.NetworkError,
                            openai.APIConnectionError))


REMOTE_RETRY_POLICY = RetryPolicy(
    initial_interval=0, backoff_factor=1, max_interval=0,
    max_attempts=3, jitter=False, retry_on=is_transient_error,
)


def retry_remote_call(operation):
    """Use the same retry budget for operations also called outside LangGraph."""
    policy = REMOTE_RETRY_POLICY
    return RunnableLambda(lambda _: operation()).with_retry(
        retry_if_exception_type=is_transient_error,
        stop_after_attempt=policy.max_attempts,
        exponential_jitter_params={
            'initial': policy.initial_interval, 'max': policy.max_interval,
            'exp_base': policy.backoff_factor, 'jitter': 1 if policy.jitter else 0,
        },
    ).invoke(None)


def geo_post(*args, **kwargs):
    """Only read-only geographic POSTs belong here; never retry mutations."""
    def send():
        response = requests.post(*args, **kwargs)
        if response.status_code in (408, 429, 500, 502, 503, 504):
            response.raise_for_status()
        return response

    try:
        return retry_remote_call(send)
    except requests.HTTPError as exc:
        # Preserve callers' domain-specific final-error handling (e.g. OTP 503).
        if exc.response is not None:
            return exc.response
        raise
