import io
import threading
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

import engine
import settings
import workflow


class ReviewTests(unittest.TestCase):
    def test_resume_after_model_failure(self):
        directory = self.enterContext(tempfile.TemporaryDirectory())
        self.enterContext(patch.object(settings, 'SETTINGS_FILE', Path(directory)/'model.json'))
        seen = []
        def model(prompt, payload):
            seen.append(payload['段号'])
            if seen == [1, 2]:
                raise ValueError('temporary')
            return {'entries':[dict(kind='角色',name='阿舟',content=payload['正文'],quote=payload['正文'])]}
        def run():
            state = {'digest':'sample'}
            self.addCleanup(lambda: state.get('archive') and state['archive'].close())
            db, reader = workflow.checkpoint_reader(state, '书', ['角色'], '', 500, model)
            with closing(db):
                return engine.extract(['两人尚未相认','两人已经相认'], '书', ['角色'], '', lambda _: None,model, read_segment=reader)
        with self.assertRaises(ValueError):
            run()
        rows, _, _ = run()
        self.assertEqual(seen, [1,2,2])
        self.assertEqual([r['enabled'] for r in rows], [True,False])
        self.assertEqual(rows[1]['evidence'][0]['part'],2)
        rows.append(dict(rows[0], kind='手动',name='开场约束',content='不要相认'))
        self.assertEqual([c[2] for c in engine.opening_context(rows,'','')],['两人尚未相认','不要相认'])

    def test_global_ranking_uses_multiple_segments(self):
        def model(prompt, payload):
            names = list('甲乙丙丁戊') if payload['段号']==1 else ['戊']
            return {'entries':[dict(kind='文风',name=n,content=n,quote='这是一段原文',scope='全局') for n in names]}
        rows, _, _ = engine.extract(['这是一段原文','这是一段原文'],'书',['文风'],'',lambda _:None,model)
        self.assertTrue(rows[-1]['constant'])
        self.assertEqual(sum(r['constant'] for r in rows),4)
        self.assertTrue(all(r['global_note'] for r in rows))

class ArrangementTests(unittest.TestCase):
    def test_arrangement_preserves_content_and_disabled_rows(self):
        src = dict(kind='角色',name='甲',content='值夜人',position=1,role=0,depth=4,order=100,
                   constant=False,enabled=True,keys=['甲'],evidence=[])
        rows = [src, dict(src, enabled=False)]
        def model(prompt, payload):
            self.assertEqual([r['id'] for r in payload['条目']], [0])
            return {'plans':[dict(id=0,position=4,depth=2,role=0,order=105,reason='当前场景提醒')]}
        result = workflow.arrange(rows,model,'',lambda _:None)
        self.assertEqual(result[0]['content'],src['content'])
        self.assertEqual(result[0]['arrange_note'],'当前场景提醒')
        self.assertEqual(src['position'],1)
        self.assertEqual(result[1],rows[1])
        with self.assertRaises(ValueError):
            workflow.arrange(rows,lambda *_:{'plans':[]},'',lambda _:None)


class RegressionTests(unittest.TestCase):
    def test_release_stops_following_segments_and_frees_slot(self):
        directory = self.enterContext(tempfile.TemporaryDirectory())
        self.enterContext(patch.object(settings, 'SETTINGS_FILE', Path(directory)/'model.json'))
        token = workflow.create()
        raw = ('甲'*1200).encode()
        workflow.upload(token, io.BytesIO(raw), len(raw), 'book.txt')
        entered, resume = threading.Event(), threading.Event()
        calls = []
        def model(prompt, payload):
            calls.append(payload['段号'])
            entered.set()
            if not resume.wait(3):
                raise ValueError('test timeout')
            return {'entries':[dict(kind='角色',name='甲',content='设定',quote='甲甲甲甲甲')]}
        try:
            with patch.object(engine, 'configured_model', return_value=model):
                workflow.action(token, dict(step='extract',mode='generate',kinds=['角色'],limit=500))
                self.assertTrue(entered.wait(3))
                workflow.release(token)
                resume.set()
                acquired = workflow.model_slot.acquire(timeout=3)
                self.assertTrue(acquired)
                if acquired:
                    workflow.model_slot.release()
            self.assertEqual(calls, [1])
            self.assertNotIn(token, workflow.flows)
        finally:
            resume.set()
            workflow.release(token)

    def test_evidence_requires_short_meaningful_verbatim_quote(self):
        part = '甲走进房间。' + '乙'*101 + '。。。。。'
        for quote, expected in [('。',0),('甲走进房',0),('。。。。。',0),('乙'*101,0),
                                ('甲走进房间',1),('乙'*100,1),('不存在的句子',0)]:
            with self.subTest(quote=quote):
                items, _ = engine.segment_candidates(part, {'段号':1}, ['角色'],
                    lambda *_: {'entries':[dict(kind='角色',name='甲',content='设定',quote=quote)]})
                self.assertEqual(len(items),expected)

    def test_cancellation_blocks_http_retry(self):
        from urllib.error import URLError
        from unittest.mock import Mock
        opener = Mock()
        opener.open.side_effect = URLError('offline')
        cancelled = False
        def check():
            if cancelled:
                raise engine.Cancelled('stopped')
        def stop(_):
            nonlocal cancelled
            cancelled = True
        token = engine.check_cancelled.set(check)
        try:
            with patch.object(engine.request, 'build_opener', return_value=opener), patch.object(engine.time, 'sleep', side_effect=stop):
                with self.assertRaises(engine.Cancelled):
                    engine.chat('test', {}, config=dict(model='fake',base_url='http://example.invalid',api_key=''))
            self.assertEqual(opener.open.call_count,1)
        finally:
            engine.check_cancelled.reset(token)
