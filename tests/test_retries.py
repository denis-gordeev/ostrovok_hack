"""Retries recover transient failures without repeating successful graph steps."""

import unittest
import ssl
from unittest.mock import Mock, patch

import httpx
import openai
import requests
from langchain_core.messages import AIMessage

from retry_utils import REMOTE_RETRY_POLICY, geo_post, is_transient_error


def http_error(status):
    response = requests.Response()
    response.status_code = status
    response.url = 'https://example.test/route'
    return requests.HTTPError(response=response)


FAST_POLICY = REMOTE_RETRY_POLICY._replace(initial_interval=0, max_interval=0, jitter=False)


class RetryTests(unittest.TestCase):
    def test_only_transient_failures_are_retried(self):
        for status in (408, 429, 500, 502, 503, 504):
            self.assertTrue(is_transient_error(http_error(status)), status)
        for status in (400, 401, 403, 404, 422, 501):
            self.assertFalse(is_transient_error(http_error(status)), status)
        for error in (ValueError('invalid input'), requests.exceptions.SSLError('certificate')):
            self.assertFalse(is_transient_error(error))
        self.assertTrue(is_transient_error(requests.Timeout()))
        self.assertTrue(is_transient_error(requests.ConnectionError()))
        self.assertTrue(is_transient_error(openai.APIConnectionError(
            request=httpx.Request('POST', 'https://example.test'))))
        certificate_error = openai.APIConnectionError(request=httpx.Request('POST', 'https://example.test'))
        certificate_error.__cause__ = ssl.SSLCertVerificationError('certificate')
        self.assertFalse(is_transient_error(certificate_error))

    def test_geo_request_recovers_before_tool_error_conversion(self):
        unavailable = http_error(503).response
        success = Mock(status_code=200)
        with patch('retry_utils.REMOTE_RETRY_POLICY', FAST_POLICY), patch(
            'retry_utils.requests.post', side_effect=[requests.Timeout(), unavailable, success]
        ) as post:
            self.assertIs(geo_post('https://example.test/route', json={}), success)
        self.assertEqual(post.call_count, 3)

    def test_exhausted_http_failure_preserves_original_response(self):
        unavailable = http_error(503).response
        with patch('retry_utils.REMOTE_RETRY_POLICY', FAST_POLICY), patch(
            'retry_utils.requests.post', return_value=unavailable
        ) as post:
            self.assertIs(geo_post('https://example.test/route'), unavailable)
        self.assertEqual(post.call_count, 3)

    def test_network_failure_stops_after_budget_and_invalid_requests_are_not_retried(self):
        with patch('retry_utils.REMOTE_RETRY_POLICY', FAST_POLICY):
            with patch('retry_utils.requests.post', side_effect=requests.Timeout()) as post:
                with self.assertRaises(requests.Timeout):
                    geo_post('https://example.test/route')
                self.assertEqual(post.call_count, 3)
            for status in (401, 422):
                response = http_error(status).response
                with patch('retry_utils.requests.post', return_value=response) as post:
                    self.assertIs(geo_post('https://example.test/route'), response)
                    self.assertEqual(post.call_count, 1)

    def test_real_graph_retries_model_without_adding_failed_messages(self):
        from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel
        from pydantic import PrivateAttr
        from agent import build_agent

        class FlakyModel(FakeMessagesListChatModel):
            _attempts: int = PrivateAttr(default=0)

            def bind_tools(self, tools, **kwargs):
                return self

            def _generate(self, *args, **kwargs):
                self._attempts += 1
                if self._attempts < 3:
                    raise openai.APIConnectionError(request=httpx.Request('POST', 'https://example.test'))
                return super()._generate(*args, **kwargs)

        model = FlakyModel(responses=[AIMessage(content='Готово')])
        with patch('agent.REMOTE_RETRY_POLICY', FAST_POLICY):
            result = build_agent(model=model).invoke({'messages': [('user', 'Привет')]})
        self.assertEqual(model._attempts, 3)
        self.assertEqual(result['call_usage']['model_calls'], 1)
        self.assertEqual(len(result['messages']), 2)
        self.assertEqual(result['messages'][-1].content, 'Готово')


if __name__ == '__main__':
    unittest.main()
