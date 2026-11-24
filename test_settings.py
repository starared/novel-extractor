import json
import io
import os
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import engine
import server
import settings
from test_extractor import PASSAGE, fake


class SettingsTests(unittest.TestCase):
    def setUp(self):
        directory = self.enterContext(tempfile.TemporaryDirectory())
        self.enterContext(patch.object(settings, 'SETTINGS_FILE', Path(directory) / 'data' / 'model.json'))
        self.enterContext(patch.dict(os.environ, MODEL_BASE_URL='https://initial.example/v1', MODEL_NAME='initial', MODEL_API_KEY='initial-secret'))

    def serve(self, handler):
        http = ThreadingHTTPServer(('127.0.0.1', 0), handler)
        threading.Thread(target=http.serve_forever, daemon=True).start()
        self.addCleanup(http.server_close)
        self.addCleanup(http.shutdown)
        return f'http://127.0.0.1:{http.server_port}'

    def test_settings_http(self):
        base = self.serve(server.Handler)
        def call(fields=None, **headers):
            body = json.dumps(fields).encode() if fields is not None else None
            with urlopen(Request(base + '/settings', body, {'Content-Type': 'application/json', **headers}), timeout=4) as res:
                return json.load(res)

        initial = call()
        self.assertEqual(initial, dict(base_url='https://initial.example/v1', model='initial', has_key=True, stream=False))
        fields = dict(base_url='http://host.docker.internal:11434/v1/', model='local-model')
        self.assertFalse(call(fields)['has_key'])
        changed = call({**fields, 'api_key': 'test-secret'})
        self.assertEqual(changed, call())
        self.assertEqual(changed, dict(base_url=fields['base_url'].rstrip('/'), model='local-model', has_key=True, stream=False))
        self.assertTrue(call({**fields, 'stream': True})['stream'])
        call({**fields, 'model': 'different', 'api_key': ''})
        self.assertTrue(call()['stream'])
        self.assertEqual(settings.load()['api_key'], 'test-secret')
        for invalid in ({'base_url': 'file:///something'}, {'model': ' '}, {'base_url': 'https://key@site.test/v1'}, {'base_url': 'https://site.test/v1?key=test'}):
            with self.assertRaises(HTTPError) as err:
                call({**fields, **invalid})
            self.assertEqual(err.exception.code, 400)
            err.exception.close()
        self.assertEqual(settings.load()['model'], 'different')
        with self.assertRaises(HTTPError) as err:
            call(fields, Origin='https://unrelated.example')
        self.assertEqual(err.exception.code, 403)
        err.exception.close()
        self.assertFalse(call({**fields, 'clear_key': True})['has_key'])
        self.assertTrue(settings.SETTINGS_FILE.exists())
        with patch.dict(os.environ, MODEL_API_KEY='not-used', MODEL_NAME='not-used'):
            self.assertEqual(settings.load()['api_key'], '')
            self.assertEqual(settings.load()['model'], 'local-model')

    def test_real_client_keeps_one_job_on_one_config(self):
        seen = []
        class LocalModel(BaseHTTPRequestHandler):
            def do_POST(self):
                req = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
                seen.append((self.path, req['model'], self.headers.get('Authorization')))
                if len(seen) == 1:
                    settings.save(dict(base_url='http://127.0.0.1:1/v1', model='next-job'))
                response = fake('', json.loads(req['messages'][1]['content']))
                self.send_response(200)
                self.end_headers()
                self.wfile.write(json.dumps({'choices': [{'message': {'content': json.dumps(response)}}]}).encode())

        settings.save(dict(base_url=self.serve(LocalModel) + '/v1', model='active', api_key='active-key'))
        rows,n,_ = engine.extract([PASSAGE]*2, '测试', engine.KINDS,'',lambda msg: None, engine.configured_model())
        self.assertTrue(rows);self.assertEqual(n,2)
        self.assertEqual(seen, [('/v1/chat/completions', 'active', 'Bearer active-key')] * 2)
        self.assertEqual(settings.load()['model'], 'next-job')

    def test_discover_without_saving_or_forwarding_old_key(self):
        seen=[]
        class Models(BaseHTTPRequestHandler):
            def do_GET(self):
                seen.append((self.path,self.headers.get('Authorization')))
                self.send_response(200);self.end_headers()
                self.wfile.write(b'{"data":[{"id":"model-b"},{"id":"model-a"},{"id":"model-b"}]}')
        url=self.serve(Models)+'/v1'
        api=self.serve(server.Handler)
        def fetch(fields):
            with urlopen(Request(api+'/settings/models',json.dumps(fields).encode(),{'Content-Type':'application/json'})) as r:return json.load(r)
        self.assertEqual(fetch({'base_url':url}),{'models':['model-a','model-b']})
        self.assertEqual(settings.load()['model'],'initial')
        settings.save({'base_url':url,'model':'model-a','api_key':'different-key'})
        fetch({'base_url':url}); fetch({'base_url':url,'clear_key':True})
        self.assertEqual(seen,[('/v1/models',None),('/v1/models','Bearer different-key'),('/v1/models',None)])