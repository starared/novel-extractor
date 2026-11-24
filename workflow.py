"""Single-user-directed steps; no transition launches the following step."""
import re
import logging
import hashlib
import json
import sqlite3
from contextlib import closing
from itertools import islice
import secrets
import tempfile
import threading
import time

import engine
import settings


STEPS = ('source', 'extract', 'world', 'opening', 'export', 'done')
flows = {}
guard = threading.RLock()
model_slot = threading.BoundedSemaphore(1)


class Conflict(ValueError):
    pass


def create(token=None):
    with guard:
        if token is not None:
            if token in flows:
                return token
            if not re.fullmatch(r'[0-9a-f]{32}', token):
                raise KeyError()
        for old_token, old in list(flows.items()):
            if old['status'] != 'running' and time.monotonic() - old['touched'] > 86400:
                discard(old_token)
        token = token or secrets.token_urlsafe(24)
        flows[token] = dict(log_id=secrets.token_hex(6),step='source', status='waiting', message='等待创作者选择。', file=None,
                            title='未命名角色卡', entries=[], opening='', description='', encoding='',
                            history=[], excerpt='', touched=time.monotonic())
        return token


def get(token):
    f = flows.get(token)
    if f is None:
        raise KeyError()
    f['touched'] = time.monotonic()
    return f


def check(token, step=None):
    f = get(token)
    if f['status'] == 'running' or (step and step != f['step']):
        raise Conflict('节点已变更或正在运行，请刷新进度。')
    return f


def discard(token):
    with guard:
        f = check(token)
        if f['file']:
            f['file'].close()
        if f.get('archive'):
            f['archive'].close()
        del flows[token]


def release(token):
    with guard:
        f = flows.get(token)
        if f is None:
            return
        if f['status'] == 'running':
            f['release_pending'] = True
        else:
            discard(token)


def record(f, message, level=logging.INFO):
    logging.getLogger('extractor').log(level, 'task=%s stage=%s %s', f.get('log_id','-'), f.get('step','extract'), message.replace('\n',' '))
    events = f.setdefault('events', [])
    events.append({'time':int(time.time()*1000), 'message':message, 'level':logging.getLevelName(level)})
    del events[:-120]


def save_exchange(f, kind, part, value):
    with guard:
        counts = f.setdefault('cache_stats', dict(local=0, requests=0, reported=0, tokens=0))
        if kind == 'cache':
            counts['local'] += 1
        elif kind == 'request':
            counts['requests'] += 1
        elif kind == 'usage':
            hits = engine.cached_input_tokens(value)
            if hits is not None:
                counts['reported'] += 1
                counts['tokens'] += hits
        if 'archive' not in f:
            settings.SETTINGS_FILE.parent.mkdir(parents=True, exist_ok=True)
            f['archive'] = tempfile.TemporaryFile(dir=settings.SETTINGS_FILE.parent)
            f['exchange_index'] = []
        archive = f['archive']
        offset = archive.seek(0, 2)
        raw = json.dumps(value, ensure_ascii=False).encode()
        archive.write(raw)
        f['exchange_index'].append((kind, part, offset, len(raw)))


def exchange(token, index):
    with guard:
        f = get(token)
        items = f.get('exchange_index', [])
        if index is None:
            return [[kind, part] for kind, part, _, _ in items]
        if type(index) is not int or not 0 <= index < len(items):
            raise ValueError('记录序号无效')
        _, _, offset, length = items[index]
        f['archive'].seek(offset)
        return json.loads(f['archive'].read(length))


def arrange(rows, model, instructions, progress):
    revised = [dict(r) for r in rows]
    active = [i for i,r in enumerate(rows) if r['enabled']]
    if not active:
        raise ValueError('请先启用要编排的条目。')
    for start in range(0, len(active), 12):
        batch = active[start:start+12]
        progress(f'正在编排第 {start+1}–{start+len(batch)} 条，共 {len(active)} 条')
        prompt = """为 SillyTavern 编排给定条目的插入参数。仅返回 {"plans":[{"id":0,"position":1,"order":100,"depth":4,"role":0,"reason":"依据"}]}，逐个保留给定 id。
position 0/1 角色定义前/后，2/3 作者注释上/下，4 聊天深度，5/6 示例对话前/后，7 命名插槽。仅已有插槽名才选择7。
同位置 order 值大靠后。仅位置4使用深度（0最新）和身份（0系统1用户2助手）。没有当前场景依据的历史事件作为背景处理。"""
        result = model(prompt, {'编排要求':instructions,'条目':[dict(id=i,类别=rows[i]['kind'],名称=rows[i]['name'],内容=rows[i]['content'][:1200],插槽=rows[i].get('outlet','')) for i in batch]})
        plans = result.get('plans')
        if not isinstance(plans,list) or any(not isinstance(v,dict) or type(v.get('id')) is not int for v in plans) or sorted(v['id'] for v in plans)!=batch:
            raise ValueError('编排未完整返回本批条目；保留原设置。')
        for v in plans:
            r = revised[v['id']]
            for key in ('position','order','depth','role'):
                if key in ('depth','role') and v.get('position')!=4:
                    continue
                r[key] = v.get(key)
            r['arrange_note'] = engine.short(v.get('reason'),400)
        validate_rows([revised[i] for i in batch])
    return revised


def view(token):
    with guard:
        f = get(token)
        return dict(cache_stats=dict(f.get('cache_stats', {})),direct_download=True,download_after_finish=True,exchange_count=len(f.get('exchange_index',[])), log_id=f.get('log_id'),failed=f.get('failed', False),source=f.get('source'), events=list(f.get('events', [])), revision=f.get('revision',0), part_count=f.get('part_count', 0), preview=f.get('preview', '') if f['status']=='running' else '', progress=f.get('progress'), can_retry=f['file'] is not None, **{k: f[k] for k in ('step', 'status', 'message', 'title', 'entries', 'opening', 'description', 'encoding', 'history')})


def advance(f, decision, message):
    if (f['step'] == 'world' or (f['step'] == 'extract' and decision == '跳过')) and f['file'] is not None:
        f['file'].close()
        f['file'] = None
    record(f, message)
    f['history'] = f['history'] + [dict(step=f['step'], decision=decision)]
    f.update(step=STEPS[STEPS.index(f['step']) + 1], status='waiting', message=message, touched=time.monotonic())


def upload(token, stream, size, filename):
    if not filename.lower().endswith('.txt') or size <= 0:
        raise ValueError('请选择非空的 TXT 文件。')
    with guard:
        create(token)
        f = check(token, 'source')
        f.update(status='running', message='正在接收并检查编码。')
    file = None
    try:
        directory = settings.SETTINGS_FILE.parent
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        file = tempfile.TemporaryFile(dir=directory)
        digest = hashlib.sha256()
        remaining = size
        while remaining:
            block = stream.read(min(remaining, 256 * 1024))
            if not block:
                raise ValueError('上传中断，请重试。')
            digest.update(block)
            file.write(block)
            remaining -= len(block)
        actual_size = file.tell()
        encoding = engine.book_encoding(file)
        excerpt = file.read(8192).decode(encoding, errors='ignore')[:1800]
        with guard:
            f.update(source=dict(name=filename, size=actual_size, head=excerpt[:240]), file=file, digest=digest.hexdigest(), encoding=encoding, title=filename[:-4][:100], excerpt=excerpt)
            file = None
            advance(f, '上传 TXT', f'已读取 {size:,} 字节，编码 {encoding}。尚未调用模型。')
    except Exception:
        with guard:
            f.update(status='waiting', message='上传失败，可重新选择文件。')
        raise
    finally:
        if file is not None:
            file.close()
        with guard:
            if f.get('release_pending'):
                release(token)


def validate_rows(rows):
    if not isinstance(rows, list):
        raise ValueError('条目列表无效。')
    for r in rows:
        if not isinstance(r, dict) or not all(isinstance(r.get(k), str) and r[k].strip() for k in ('name', 'content','kind')):
            raise ValueError('条目名称、类别和内容不能为空。')
        if not all(type(r.get(k)) is bool for k in ('enabled','constant')) or not isinstance(r.get('keys'), list) or not all(isinstance(k,str) and k.strip() for k in r['keys']):
            raise ValueError('灯色或关键词无效。')
        if r['enabled'] and not r['constant'] and not r['keys']:
            raise ValueError('绿灯条目至少需要一个关键词。')
        for k, high in [('position',7),('role',2),('order',9999),('depth',999)]:
            if type(r.get(k)) is not int or not 0 <= r[k] <= high:
                raise ValueError('插入位置、身份、顺序或深度无效。')
        outlet = r.get('outlet', '')
        if not isinstance(outlet, str) or len(outlet) > 120 or (r['position'] == 7 and not outlet.strip()):
            raise ValueError('命名插槽需要填写插槽名。')
        if not isinstance(r.get('evidence'),list) or not all(isinstance(e,dict) and isinstance(e.get('quote'),str) and type(e.get('part')) is int for e in r['evidence']):
            raise ValueError('原文依据格式无效。')


def checkpoint_reader(f, title, kinds, wishes, limit, model):
    cfg = getattr(model, 'keywords', {}) or {}
    cfg = cfg.get('config', {})
    signature = json.dumps([2, f['digest'], title, sorted(kinds), wishes, limit,
                            cfg.get('base_url'), cfg.get('model')], ensure_ascii=False)
    job = hashlib.sha256(signature.encode()).hexdigest()
    db = sqlite3.connect(settings.SETTINGS_FILE.parent / 'segments.sqlite3', timeout=30)
    db.execute('CREATE TABLE IF NOT EXISTS segments (job TEXT, part INTEGER, result TEXT, saved REAL, PRIMARY KEY(job,part))')
    with db:
        if f.pop('regenerate', False):
            db.execute('DELETE FROM segments WHERE job=?', (job,))
        db.execute('DELETE FROM segments WHERE saved < ?', (time.time() - 7*86400,))
    def read(part, payload, requested, client):
        number = payload['段号']
        def log_candidates(result):
            items, rejected = result
            found = {r['kind'] for r in items}
            missing = '、'.join(k for k in requested if k not in found)
            titles = '、'.join(str(r['name'])[:45] for r in items[:12]) or '无'
            record(f, f'第 {number} 段返回条目：{titles}。未提取到的所选类别：{missing or "无"}。过滤 {rejected} 条。')
        cached = db.execute('SELECT result FROM segments WHERE job=? AND part=?', (job, number)).fetchone()
        if cached:
            result = json.loads(cached[0])
            save_exchange(f,'cache',number,result[0])
            with guard:
                record(f, f'第 {number} 段复用本地提取结果，未请求上游，{len(result[0])} 条有效候选。')
                log_candidates(result)
            return result
        with guard:
            record(f, f"第 {number} 段（{len(part):,} 字符）已提交给 {cfg.get('model') or '模型'}，正在等待回复。")
        try:
            result = engine.segment_candidates(part, payload, requested, client)
        except engine.ReplyFormatError as exc:
            if len(part) <= 2000:
                raise
            with guard:
                record(f, f'第 {number} 段：{exc}。按最多 2000 字符拆分重试。',logging.WARNING)
            items, dropped = [], 0
            for offset in range(0,len(part),2000):
                result_items, count = engine.segment_candidates(part[offset:offset+2000],payload,requested,client)
                items.extend(result_items)
                dropped += count
            result = items, dropped
        with guard:
            record(f, f'第 {number} 段收到 {len(result[0])} 条有效候选，过滤 {result[1]} 条。')
            log_candidates(result)
        save_exchange(f, 'accepted', number, result[0])
        with db:
            db.execute('INSERT OR REPLACE INTO segments VALUES (?,?,?,?)',
                       (job, number, json.dumps(result, ensure_ascii=False), time.time()))
        return result
    return db, read


def selected_part(f, number):
    if type(number) is not int or not 1 <= number <= f.get('part_count', 0) or not f['file']:
        raise ValueError('请选择有效的原文段号。')
    with closing(engine.segments(f['file'], f['encoding'], f['limit'])) as parts:
        return next(islice(parts, number-1, number))


def inspect_part(token, number):
    with guard:
        return selected_part(check(token, 'world'), number)


def action(token, data):
    step, mode = data.get('step'), data.get('mode')
    with guard:
        if step == 'source' and mode == 'skip':
            create(token)
        f = check(token, step)
        if step not in STEPS[:-1] or step != f['step']:
            raise Conflict('无效节点。')
        title = engine.short(data.get('title'),100) or f['title']
        f['title'] = title
        if mode == 'skip':
            if step == 'world':
                f['entries'] = []
            if step == 'opening':
                f.update(opening='', description='')
            advance(f, '跳过', '已跳过此节点。')
        elif step == 'world' and mode == 'retry' and f['file'] is not None:
            f.update(step='extract', regenerate=True, message='调整类别或提取重点，然后重新提取。')
        elif step == 'world' and mode == 'confirm':
            validate_rows(data.get('entries'))
            f['entries'] = data['entries']
            advance(f, '确认条目', '已确认世界书。')
        elif step == 'opening' and mode == 'confirm':
            f.update(opening=engine.short(data.get('opening'),12000), description=engine.short(data.get('description'),3000))
            advance(f, '确认文本', '已确认开场白和扮演说明。')
        elif step == 'export' and mode == 'finish':
            advance(f, '结束', '工作流已完成。')
        elif (step in ('extract','opening') and mode == 'generate') or (step == 'world' and mode in ('supplement','arrange')):
            if step == 'extract' and not f['file']:
                raise ValueError('没有小说素材，请跳过提取并手动编写条目。')
            kinds, limit = data.get('kinds',list(engine.KINDS)), data.get('limit',7000)
            if not isinstance(kinds,list) or not kinds or any(k not in engine.KINDS for k in kinds) or type(limit) is not int or not 500 <= limit <= 12000:
                raise ValueError('请选择提取类别，分段长度需为 500–12000 字符。')
            if step == 'world':
                if mode == 'supplement':
                    number = data.get('part')
                    part = selected_part(f, number)
                validate_rows(data.get('entries'))
            def preview(text):
                with guard:
                    f['preview'] = text
            client = engine.configured_model(preview)
            def check_cancelled():
                with guard:
                    if f.get('release_pending'):
                        raise engine.Cancelled('页面已关闭，任务已停止。')
            def model(*args, **kwargs):
                check_cancelled()
                result = client(*args, **kwargs)
                check_cancelled()
                return result
            model.keywords = getattr(client, 'keywords', {})
            if not model_slot.acquire(blocking=False):
                raise Conflict('其他模型节点正在运行，请稍后重试。')
            if step == 'world':
                f['entries'] = data['entries']
            f.update(status='running', failed=False, preview='', progress=None, message='正在生成开场白和角色描述…' if step == 'opening' else (('正在编排酒馆参数…' if mode=='arrange' else '正在补提所选段落…') if step == 'world' else '正在统计分段…'))
            record(f, f'已开始{dict(extract="提取",world=("编排" if mode=="arrange" else "补提"),opening="生成开场白")[step]}。')
            def worker():
                def trace(message):
                    with guard:
                        record(f,message)
                inspect_token = engine.model_inspect.set(lambda kind, part, value: save_exchange(f,kind,part,value))
                trace_token = engine.model_trace.set(trace)
                cancel_token = engine.check_cancelled.set(check_cancelled)
                def progress(msg):
                    with guard:
                        f['message'] = msg
                try:
                    wishes = engine.short(data.get('instructions'), 3000)
                    if step == 'extract':
                        def segments():
                            return engine.segments(f['file'], f['encoding'], limit)
                        total = sum(1 for _ in segments())
                        with guard:
                            f.update(part_count=total, limit=limit)
                            record(f, f'共 {total} 段；所选类别：{", ".join(kinds)}。')
                        def counted():
                            with guard:
                                f['progress'] = [0, total]
                            with closing(segments()) as source:
                                for index, part in enumerate(source, 1):
                                    check_cancelled()
                                    yield part
                                    with guard:
                                        f['progress'] = [index, total]
                        parts = counted()
                        try:
                            db, read = checkpoint_reader(f, title, kinds, wishes, limit, model)
                            with closing(db):
                                rows, count, rejected = engine.extract(parts, title, kinds, wishes, progress, model, read_segment=read)
                        finally:
                            parts.close()
                        with guard:
                            f.update(entries=rows, revision=f.get('revision',0)+1)
                            advance(f, '模型提取', f'{count} 段，{len(rows)} 条候选，忽略 {rejected} 条。等待审阅。')
                    elif mode == 'arrange':
                        rows = arrange(data['entries'], model, wishes, progress)
                        with guard:
                            f.update(entries=rows,revision=f.get('revision',0)+1,status='waiting',message='编排完成。展开条目可查看理由或修改参数。')
                    elif step == 'world':
                        extra, _, _ = engine.extract([part], title, kinds, wishes, progress, model, first_part=number)
                        rows = list(data['entries'])
                        known = {(r['kind'],r['name'],r['content']) for r in rows}
                        names = {(r['kind'],r['name']) for r in rows}
                        for row in extra:
                            key = row['kind'],row['name'],row['content']
                            if key not in known:
                                if key[:2] in names:
                                    row.update(enabled=False, phase_note='同名条目的补提版本，请选择适用状态。')
                                if row['constant'] and sum(r['enabled'] and r['constant'] for r in rows) >= 4:
                                    row.update(constant=False,global_note='已有四条常驻设定，补提候选建议绿灯。')
                                rows.append(row)
                                known.add(key)
                                names.add(key[:2])
                        with guard:
                            f.update(entries=rows, revision=f.get('revision',0)+1, status='waiting', message=f'第 {number} 段补提完成，新增 {len(rows)-len(data["entries"])} 条。')
                    else:
                        draft = engine.generate_opening(title,wishes,f['excerpt'],f['entries'],model,engine.short(data.get('role_requirements'), 3000))
                        with guard:
                            f.update(**draft, status='waiting', message='开场白已生成，可直接下载角色卡。')
                except engine.Cancelled as exc:
                    with guard:
                        f.update(status='waiting', failed=False, message=str(exc))
                except Exception as exc:
                    with guard:
                        f.update(status='waiting', failed=True, message=str(exc) if isinstance(exc, ValueError) else '节点失败，可重试或跳过。')
                finally:
                    engine.model_inspect.reset(inspect_token)
                    engine.model_trace.reset(trace_token)
                    engine.check_cancelled.reset(cancel_token)
                    with guard:
                        if not f.get('events') or f['events'][-1]['message'] != f['message']:
                            record(f, f['message'], logging.ERROR if f.get('failed') else logging.INFO)
                        if f.get('release_pending'):
                            release(token)
                    model_slot.release()
            threading.Thread(target=worker, daemon=True).start()
        else:
            raise ValueError('此节点不支持该操作。')
        return view(token)