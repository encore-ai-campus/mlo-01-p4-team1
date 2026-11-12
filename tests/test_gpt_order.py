import json
import os
import unittest
from unittest.mock import patch

from app.backend.recommendation import gpt_order


class GptOrderTests(unittest.TestCase):
    def setUp(self):
        self.enterContext(patch.dict(os.environ, {
            'OPENAI_API_KEY': 'test-only-key',
            'OPENAI_PLAYLIST_ORDER_MODEL': 'test-order-model',
        }, clear=True))
        self.connect = self.enterContext(patch.object(gpt_order, 'HTTPSConnection'))
        self.connection = self.connect.return_value
        self.response = self.connection.getresponse.return_value.__enter__.return_value
        self.response.status = 200
        self.blocks = [
            {'weather': 'RAIN', 'tracks': [
                {'track_id': 'a', 'track_name': '곡 A', 'genre_group_codes': ['JAZZ'], 'theme_codes': ['CALM'],
                 'speed': 'fast', 'play_url': 'https://example.invalid/', 'score_cache': {'private': True}},
                {'track_id': 'b', 'artists_name': '아티스트 B'},
            ]},
            {'weather': None, 'tracks': [{'track_id': 'c'}]},
        ]

    def provide(self, text, *, content_type='output_text', status='completed'):
        self.response.read.return_value = json.dumps({
            'status': status,
            'output': [
                {'type': 'reasoning', 'summary': []},
                {'type': 'message', 'role': 'assistant', 'status': 'completed',
                 'content': [{'type': content_type, 'text': text}]},
            ],
        }).encode('utf-8')

    def test_one_request_returns_the_order_without_exposing_unrelated_fields(self):
        long_id = 'tagged:' + 'a' * 64
        self.blocks[0]['tracks'][0]['track_id'] = long_id
        self.provide('{"blocks":[[2,1],[3]]}')
        order = gpt_order.propose_order(
            self.blocks, timeout_seconds=2, anchor={'track_id': 'last', 'theme_codes': ['PEACEFUL'], 'private': 'omit'},
        )
        self.assertEqual(order, [['b', long_id], ['c']])
        self.connect.assert_called_once()
        self.assertEqual(self.connect.call_args.args, ('api.openai.com',))
        self.assertGreater(self.connect.call_args.kwargs['timeout'], 0)
        self.assertLessEqual(self.connect.call_args.kwargs['timeout'], 2)
        self.connection.request.assert_called_once()
        args, kwargs = self.connection.request.call_args
        self.assertEqual(args, ('POST', '/v1/responses'))
        self.assertEqual(kwargs['headers']['Authorization'], 'Bearer test-only-key')
        request = json.loads(kwargs['body'])
        self.assertEqual(request['model'], 'test-order-model')
        self.assertNotIn('reasoning', request)
        self.assertNotIn('service_tier', request)
        self.assertFalse(request['store'])
        self.assertEqual(request['text']['format']['type'], 'json_schema')
        self.assertTrue(request['text']['format']['strict'])
        self.assertFalse(request['text']['format']['schema']['additionalProperties'])
        supplied = json.loads(request['input'])
        self.assertEqual(supplied['anchor'], {'track_id': 0, 'theme_codes': ['PEACEFUL']})
        self.assertEqual(supplied['blocks'][0], {'weather': 'RAIN', 'tracks': [
            {'track_id': 1, 'track_name': '곡 A', 'genre_group_codes': ['JAZZ'], 'theme_codes': ['CALM']},
            {'track_id': 2, 'artists_name': '아티스트 B'},
        ]})
        self.assertNotIn(long_id, request['input'])
        self.connection.close.assert_called_once()

    def test_rejects_missing_duplicate_invented_and_cross_block_ids(self):
        for blocks in ([[1], [3]], [[1, 1], [3]], [[1, 9], [3]], [[1, 0], [3]],
                       [[3, 2], [1]], [[1, 2]], [[True, 2], [3]], [['1', 2], [3]]):
            with self.subTest(blocks=blocks):
                self.provide(json.dumps({'blocks': blocks}))
                with self.assertRaises(gpt_order.GptOrderError):
                    gpt_order.propose_order(self.blocks, timeout_seconds=2)

    def test_rejects_extra_keys_duplicate_keys_and_malformed_json(self):
        for text in ('{"blocks":[[2,1],[3]],"extra":1}',
                     '{"blocks":[],"blocks":[[2,1],[3]]}', '{"blocks":'):
            with self.subTest(text=text):
                self.provide(text)
                with self.assertRaises(gpt_order.GptOrderError):
                    gpt_order.propose_order(self.blocks, timeout_seconds=2)

    def test_rejects_refusal_and_incomplete_response(self):
        for content_type, status in (('refusal', 'completed'), ('output_text', 'incomplete')):
            with self.subTest(content_type=content_type, status=status):
                self.provide('{"blocks":[[2,1],[3]]}', content_type=content_type, status=status)
                with self.assertRaises(gpt_order.GptOrderError):
                    gpt_order.propose_order(self.blocks, timeout_seconds=2)

    def test_defaults_to_luna_fast_with_low_reasoning(self):
        self.provide('{"blocks":[[2,1],[3]]}')
        for model in ('', 'gpt-6-luna'):
            with self.subTest(model=model), patch.dict(os.environ, {'OPENAI_PLAYLIST_ORDER_MODEL': model}):
                gpt_order.propose_order(self.blocks, timeout_seconds=2)
                body = json.loads(self.connection.request.call_args.kwargs['body'])
                self.assertEqual(body['model'], 'gpt-6-luna')
                self.assertEqual(body['reasoning'], {'effort': 'low'})
                self.assertEqual(body['service_tier'], 'fast')

    def test_api_key_and_timeout_are_required_before_contacting_openai(self):
        with patch.dict(os.environ, {'OPENAI_API_KEY': ''}):
            with self.assertRaises(gpt_order.GptOrderError):
                gpt_order.propose_order(self.blocks, timeout_seconds=2)
        for timeout in (0, -1, float('inf'), float('nan'), True):
            with self.subTest(timeout=timeout):
                with self.assertRaises(gpt_order.GptOrderError):
                    gpt_order.propose_order(self.blocks, timeout_seconds=timeout)
        self.connect.assert_not_called()

    def test_rejects_ambiguous_input_before_contacting_openai(self):
        with self.assertRaises(gpt_order.GptOrderError):
            gpt_order.propose_order(self.blocks, timeout_seconds=2, anchor={'track_id': 'a'})
        self.connect.assert_not_called()

    def test_timeout_is_wrapped_without_retry_or_exposing_transport_details(self):
        self.connection.getresponse.side_effect = TimeoutError('transport debug: test-only-key')
        with self.assertRaisesRegex(gpt_order.GptOrderError, 'timed out') as error:
            gpt_order.propose_order(self.blocks, timeout_seconds=2)
        self.assertNotIn('test-only-key', str(error.exception))
        self.connection.request.assert_called_once()
        self.connection.close.assert_called_once()

    def test_rejects_late_success(self):
        self.provide('{"blocks":[[2,1],[3]]}')
        with patch.object(gpt_order, 'monotonic', side_effect=[10.0, 10.1, 12.1]):
            with self.assertRaisesRegex(gpt_order.GptOrderError, 'deadline expired'):
                gpt_order.propose_order(self.blocks, timeout_seconds=2)

    def test_rejects_redirect_without_following_it_or_reading_error_details(self):
        self.response.status = 302
        with self.assertRaises(gpt_order.GptOrderError):
            gpt_order.propose_order(self.blocks, timeout_seconds=2)
        self.connection.request.assert_called_once()
        self.response.read.assert_not_called()
