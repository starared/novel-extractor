import base64
import io
import json
import struct
import threading
import time
import unittest
import zlib
from unittest.mock import patch
from urllib.request import Request, urlopen

import engine
import server
import workflow
import settings
import tempfile
from pathlib import Path
from urllib.error import HTTPError


PASSAGE = '雾港钟声传来，沈舟登上渡船。'


def fake(prompt, payload):
    if '创作者要求' in payload:
        return dict(opening=payload['创作者要求'] + '：钟声刚落，船还未开。', description='扮演雾港的摆渡人。')
    return {'entries': [dict(kind=kind, name=name, content=name + '的设定', keys=[name], quote=quote, **{'global': True})
                        for kind, name, quote in [('角色', '沈舟', '沈舟登上渡船'), ('文风', '听觉起笔', '雾港钟声传来'), ('时间', '午夜', '不存在的引文')]]}


class ExtractorTests(unittest.TestCase):
    def test_input(self):
        for enc in ('utf-8-sig', 'utf-16', 'gbk'):
            stream = io.BytesIO(PASSAGE.encode(enc))
            self.assertEqual(''.join(engine.segments(stream,engine.book_encoding(stream))), PASSAGE)
        for raw in (b'', b' \n', b'\0abc'):
            with self.assertRaises(ValueError):
                engine.book_encoding(io.BytesIO(raw))
        text = PASSAGE * 1400 + '\n' + 'x' * 17000
        parts = list(engine.segments(io.BytesIO(text.encode()),'utf-8'))
        self.assertEqual(''.join(parts), text)
        self.assertTrue(all(0 < len(p) <= 7000 for p in parts))

    def test_merge_and_activation(self):
        rows, count, skipped = engine.extract(engine.segments(io.BytesIO((PASSAGE*1000).encode()),'utf-8'),'雾港',engine.KINDS,'',lambda _: None,fake)
        self.assertGreater(count, 1)
        self.assertEqual(count, skipped)
        actor, style = rows
        self.assertEqual(len(actor['evidence']), count)
        self.assertEqual(actor['keys'], ['沈舟'])
        self.assertFalse(actor['constant'])
        self.assertTrue(style['constant'])
        self.assertNotIn('\n', actor['content'])
        self.assertIn('渡口相逢', engine.generate_opening('雾港','渡口相逢',PASSAGE,rows,fake)['opening'])
        actor['keys'] = []
        with self.assertRaises(ValueError):
            workflow.validate_rows(rows)

    def test_style_only_retries_selected_facts(self):
        calls = []
        def model(prompt, payload):
            calls.append(payload['仅提取类别'])
            common = dict(content='设定', quote='沈舟登上渡船', keys=['沈舟'])
            if len(calls) == 1:
                return {'entries': [dict(common, kind='文风', name='叙述视角', scope='全局')]}
            return {'entries': [dict(common, kind='事件', name='登船', placement=dict(position=4, depth=2, role=0, order=150))]}
        rows, count, rejected = engine.extract([PASSAGE], '雾港', ['文风','事件'], '', lambda _: None, model)
        self.assertEqual(calls, [['文风','事件'], ['事件']])
        self.assertEqual((count, rejected), (1, 0))
        style, event = rows
        self.assertTrue(style['constant'])
        self.assertFalse(event['constant'])
        self.assertEqual([event[k] for k in ('position','depth','role','order')], [4,2,0,150])

    def test_all_insertion_positions_round_trip(self):
        rows, _, _ = engine.extract([PASSAGE], '雾港', ['角色'], '', lambda _: None, fake)
        entries = [dict(rows[0], position=pos, outlet='dock' if pos == 7 else '') for pos in range(8)]
        workflow.validate_rows(entries)
        card, world = engine.artifacts(dict(entries=entries, title='港', opening='谁在岸上？', description='摆渡人'))
        for i, embedded in enumerate(card['data']['character_book']['entries']):
            self.assertEqual(embedded['extensions']['position'], i)
            self.assertEqual(world['entries'][str(i)]['position'], i)
        self.assertEqual(embedded['extensions']['outlet_name'], 'dock')
        self.assertEqual(world['entries']['7']['outletName'], 'dock')
        entries[-1]['outlet'] = ''
        with self.assertRaises(ValueError):
            workflow.validate_rows(entries)

    def test_http_round_trip(self):
        http = server.ThreadingHTTPServer(('127.0.0.1', 0), server.Handler)
        threading.Thread(target=http.serve_forever, daemon=True).start()
        self.addCleanup(http.server_close)
        self.addCleanup(http.shutdown)
        url = f'http://127.0.0.1:{http.server_port}'
        def send(path, data, content_type):
            with urlopen(Request(url + path, data, {'Content-Type': content_type}), timeout=5) as response:
                return response.read()
        directory = self.enterContext(tempfile.TemporaryDirectory())
        self.enterContext(patch.object(settings,'SETTINGS_FILE',Path(directory)/'model.json'))
        def post(path,data):
            return send(path,json.dumps(data).encode(),'application/json')
        def create():
            token=json.loads(post('/flows',{}))['id']; self.addCleanup(workflow.discard,token); return '/flows/'+token
        flow=create()
        def upload(flow,raw):
            with urlopen(Request(url+flow+'/upload',raw,{'Content-Type':'application/octet-stream','X-Filename':'book.txt'})) as r:
                return json.load(r)
        skipped_token = 'a' * 32
        skipped = json.loads(post('/flows/'+skipped_token+'/action', {'step':'source','mode':'skip'}))
        self.addCleanup(workflow.discard, skipped_token)
        self.assertEqual(skipped['step'], 'extract')
        large='/flows/'+'b'*32
        self.assertEqual(upload(large,b'x'*(13*1024*1024))['step'],'extract')
        self.addCleanup(workflow.discard, 'b'*32)
        with self.assertRaises(HTTPError) as repeated:
            upload(large,b'new')
        self.assertEqual(repeated.exception.code,409)
        repeated.exception.close()
        self.assertEqual(json.loads(post(large+'/action',{'step':'extract','mode':'skip'}))['step'],'world')
        def act(step,mode,**kwargs):
            return json.loads(post(flow+'/action',dict(step=step,mode=mode,**kwargs)))
        def wait():
            for _ in range(60):
                with urlopen(url+flow) as res: p=json.load(res)
                if p['status']!='running':return p
                time.sleep(.05)
            self.fail('node did not finish')
        with patch.object(engine,'configured_model',return_value=fake) as config:
            self.assertEqual(upload(flow,PASSAGE.encode())['step'],'extract')
            self.assertEqual(config.call_count,0)
            with self.assertRaises(HTTPError) as err:act('opening','generate')
            self.assertEqual(err.exception.code,409);err.exception.close()
            act('extract','generate',title='雾港',kinds=['角色','文风'])
            p=wait();self.assertEqual(p['step'],'world');self.assertEqual(config.call_count,1)
        p['entries'][0].update(position=4, depth=2, role=1, order=123)
        p['entries'][1]['enabled'] = False
        act('world','confirm',entries=p['entries'])
        with patch.object(engine,'configured_model',return_value=fake):
            act('opening','generate',instructions='重逢');draft=wait();self.assertEqual(draft['step'],'opening');self.assertIn('重逢',draft['opening'])
        p=act('opening','confirm',opening='手动修改',description='引路人')
        world = json.loads(post(flow+'/world',{}))['entries']
        self.assertEqual(world['0']['key'], ['沈舟'])
        self.assertTrue(world['1']['disable'])
        png = post(flow+'/png',{})
        self.assertEqual(png[:8], b'\x89PNG\r\n\x1a\n')
        at, chunks = 8, {}
        while at < len(png):
            length, kind = struct.unpack_from('>I4s', png, at)
            data = png[at+8:at+8+length]
            self.assertEqual(zlib.crc32(kind + data), struct.unpack_from('>I', png, at+8+length)[0])
            chunks[kind] = data
            at += length + 12
        self.assertEqual(len(zlib.decompress(chunks[b'IDAT'])), 384*(256*3+1))
        header, value = chunks[b'tEXt'].split(b'\0', 1)
        self.assertEqual(header, b'chara')
        card = json.loads(base64.b64decode(value))
        self.assertEqual(card['spec'], 'chara_card_v2')
        self.assertEqual(card['data']['first_mes'], p['opening'])
        actor, style = card['data']['character_book']['entries']
        self.assertFalse(style['enabled'])
        self.assertTrue(style['constant'])
        for k in ('position', 'depth', 'role'):
            self.assertEqual(actor['extensions'][k], world['0'][k])
        self.assertEqual(actor['insertion_order'], 123)


if __name__ == '__main__':
    unittest.main()