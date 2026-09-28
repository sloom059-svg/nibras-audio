"""Exercise the Archive endpoint without importing/uploading to the audio service."""
import ast
from pathlib import Path
import re
import unittest
from urllib.parse import quote


class Response:
    def __init__(self, payload, status=200):
        self.payload, self.status_code = payload, status

    def json(self):
        return self.payload


class Requests:
    response = Response({})

    def get(self, url, **kwargs):
        self.url = url
        return self.response


class ArchiveEndpointTests(unittest.TestCase):
    def setUp(self):
        tree = ast.parse((Path(__file__).resolve().parents[1] / 'bot.py').read_text())
        function = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == 'archive_item')
        function.decorator_list = []
        self.requests = Requests()
        env = dict(re=re, quote=quote, requests=self.requests, jsonify=lambda **kw: kw)
        exec(compile(ast.Module(body=[function], type_ignores=[]), 'archive_item', 'exec'), env)
        self.endpoint = env['archive_item']

    def test_missing_item_is_not_successful_empty_list(self):
        self.requests.response = Response({})
        payload, status = self.endpoint('MANSOUR-1-5')
        self.assertEqual(status, 404)
        self.assertFalse(payload['ok'])

    def test_upstream_404(self):
        self.requests.response = Response({}, 404)
        self.assertEqual(self.endpoint('missing')[1], 404)

    def test_originals_do_not_hide_other_episodes(self):
        self.requests.response = Response({'metadata': {'title': 'Show'}, 'files': [
            {'name': 'Episode 1.mp4', 'source': 'original', 'length': '20'},
            {'name': 'Episode 2.mp4', 'source': 'derivative', 'original': 'Episode 2.avi'},
            {'name': 'cover.jpg'},
            {'name': 'private.mp4', 'private': True},
        ]})
        payload = self.endpoint('different-show')
        self.assertTrue(payload['ok'])
        self.assertEqual(payload['count'], 2)
        self.assertEqual(payload['files'][1]['original'], 'Episode 2.avi')
        self.assertIn('/different-show/', payload['files'][1]['url'])

    def test_invalid_identifier(self):
        self.assertEqual(self.endpoint('../bad')[1], 400)

    def test_upstream_error(self):
        self.requests.response = Response({}, 503)
        self.assertEqual(self.endpoint('show')[1], 502)


if __name__ == '__main__':
    unittest.main()
