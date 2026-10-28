import copy
import os
import unittest
from types import SimpleNamespace
from unittest.mock import patch
from wiki_retrieval import summarize_wikipedia, wikipedia_search, wikipedia_article


class SummaryTests(unittest.TestCase):
    def setUp(self):
        self.result = {'results': [{'title': 'Казань', 'text': 'Текст источника',
                       'revision_url': 'https://ru.wikipedia.org/w/index.php?oldid=1',
                       'snapshot': '20261001', 'text_license': 'CC BY-SA'}],
                       'limitations': ['snapshot'], 'snapshot': '20261001'}

    def test_default_and_empty_results_do_not_call_llm(self):
        with patch('openai.OpenAI') as client:
            self.assertIs(summarize_wikipedia(self.result, None), self.result)
            self.assertEqual(summarize_wikipedia({'results': []}, 'Ищу историю'), {'results': []})
            client.assert_not_called()

    @patch.dict(os.environ, {'LLM_API_KEY': 'test', 'WIKIPEDIA_SUMMARY_MODEL': 'summary-model'})
    def test_summary_omits_text_preserves_sources_and_pagination(self):
        original = copy.deepcopy(self.result)
        with patch('openai.OpenAI') as client:
            create = client.return_value.__enter__.return_value.chat.completions.create
            create.return_value = SimpleNamespace(model='summary-model', choices=[SimpleNamespace(message=SimpleNamespace(content='История [1]'))])
            result = summarize_wikipedia(self.result, 'Нужна история')
            self.assertNotIn('results', result)
            self.assertNotIn('text', result['sources'][0])
            self.assertEqual(result['sources'][0]['revision_url'], original['results'][0]['revision_url'])
            self.assertEqual(result['sources'][0]['source_id'], 1)
            self.assertEqual(self.result, original)
            self.assertFalse(create.call_args.kwargs['stream'])
            article = {'found': True, **original['results'][0], 'next_offset': 6000, 'images': []}
            compact = summarize_wikipedia(article, 'История')
            self.assertEqual(compact['next_offset'], 6000)
            self.assertNotIn('text', compact)

    @patch.dict(os.environ, {'LLM_API_KEY': 'test'})
    def test_failure_does_not_leak_provider_error_or_raw_text(self):
        with patch('openai.OpenAI', side_effect=RuntimeError('secret-provider-error')):
            with self.assertRaises(ValueError) as error:
                summarize_wikipedia(self.result, 'История')
            self.assertNotIn('secret-provider-error', str(error.exception))
            self.assertNotIn('Текст источника', str(error.exception))

    def test_invalid_purpose_fails_before_retrieval(self):
        with patch('wiki_retrieval.search_wikipedia') as search:
            for purpose in ['', ' ', 'x' * 2001]:
                with self.assertRaises(ValueError):
                    wikipedia_search.invoke({'query': 'Казань', 'purpose': purpose})
            search.assert_not_called()
        with patch('wiki_retrieval.corpus_path') as corpus:
            with self.assertRaises(ValueError):
                wikipedia_article.invoke({'title': 'Казань', 'purpose': ''})
            corpus.assert_not_called()
