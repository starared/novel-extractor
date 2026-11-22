"""Single-user-directed steps; no transition launches the following step."""
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


def create():
    with guard:
        for token, old in list(flows.items()):
            if old['status'] != 'running' and time.monotonic() - old['touched'] > 86400:
                discard(token)
        token = secrets.token_urlsafe(24)
        flows[token] = dict(step='source', status='waiting', message='等待创作者选择。', file=None,
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
        del flows[token]


def view(token):
    with guard:
        f = get(token)
        return {k: f[k] for k in ('step', 'status', 'message', 'title', 'entries', 'opening', 'description', 'encoding', 'history')}


def advance(f, decision, message):
    if f['step'] == 'extract' and f['file'] is not None:
        f['file'].close()
        f['file'] = None
    f['history'] = f['history'] + [dict(step=f['step'], decision=decision)]
    f.update(step=STEPS[STEPS.index(f['step']) + 1], status='waiting', message=message, touched=time.monotonic())


def upload(token, stream, size, filename):
    if not filename.lower().endswith('.txt') or size <= 0:
        raise ValueError('请选择非空的 TXT 文件。')
    with guard:
        f = check(token, 'source')
        f.update(status='running', message='正在接收并检查编码。')
    file = None
    try:
        directory = settings.SETTINGS_FILE.parent
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        file = tempfile.TemporaryFile(dir=directory)
        remaining = size
        while remaining:
            block = stream.read(min(remaining, 256 * 1024))
            if not block:
                raise ValueError('上传中断，请重试。')
            file.write(block)
            remaining -= len(block)
        encoding = engine.book_encoding(file)
        excerpt = file.read(8192).decode(encoding, errors='ignore')[:1800]
        with guard:
            f.update(file=file, encoding=encoding, title=filename[:-4][:100], excerpt=excerpt)
            file = None
            advance(f, '上传 TXT', f'已读取 {size:,} 字节，编码 {encoding}。尚未调用模型。')
    except Exception:
        with guard:
            f.update(status='waiting', message='上传失败，可重新选择文件。')
        raise
    finally:
        if file is not None:
            file.close()


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
        for k, high in [('position',4),('role',2),('order',9999),('depth',999)]:
            if type(r.get(k)) is not int or not 0 <= r[k] <= high:
                raise ValueError('插入位置、身份、顺序或深度无效。')
        if not isinstance(r.get('evidence'),list) or not all(isinstance(e,dict) and isinstance(e.get('quote'),str) and type(e.get('part')) is int for e in r['evidence']):
            raise ValueError('原文依据格式无效。')


def action(token, data):
    step, mode = data.get('step'), data.get('mode')
    with guard:
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
        elif step == 'world' and mode == 'confirm':
            validate_rows(data.get('entries'))
            f['entries'] = data['entries']
            advance(f, '确认条目', '已确认世界书。')
        elif step == 'opening' and mode == 'confirm':
            f.update(opening=engine.short(data.get('opening'),12000), description=engine.short(data.get('description'),3000))
            advance(f, '确认文本', '已确认开场白和扮演说明。')
        elif step == 'export' and mode == 'finish':
            advance(f, '结束', '工作流已完成。')
        elif step in ('extract','opening') and mode == 'generate':
            if step == 'extract' and not f['file']:
                raise ValueError('没有小说素材，请跳过提取并手动编写条目。')
            kinds, limit = data.get('kinds',list(engine.KINDS)), data.get('limit',7000)
            if not isinstance(kinds,list) or not kinds or any(k not in engine.KINDS for k in kinds) or type(limit) is not int or not 500 <= limit <= 12000:
                raise ValueError('请选择提取类别，分段长度需为 500–12000 字符。')
            model = engine.configured_model()
            if not model_slot.acquire(blocking=False):
                raise Conflict('其他模型节点正在运行，请稍后重试。')
            f.update(status='running', message='模型节点执行中…')
            def worker():
                def progress(msg):
                    with guard:
                        f['message'] = msg
                try:
                    wishes = engine.short(data.get('instructions'), 3000)
                    if step == 'extract':
                        parts = engine.segments(f['file'], f['encoding'], limit)
                        try:
                            rows, count, rejected = engine.extract(parts, title, kinds, wishes, progress, model)
                        finally:
                            parts.close()
                        with guard:
                            f['entries'] = rows
                            advance(f, '模型提取', f'{count} 段，{len(rows)} 条候选，忽略 {rejected} 条。等待审阅。')
                    else:
                        draft = engine.generate_opening(title,wishes,f['excerpt'],f['entries'],model)
                        with guard:
                            f.update(**draft, status='waiting', message='草稿已生成；请修改、确认，或重新生成。')
                except Exception as exc:
                    with guard:
                        f.update(status='waiting', message=str(exc) if isinstance(exc, ValueError) else '节点失败，可重试或跳过。')
                finally:
                    model_slot.release()
            threading.Thread(target=worker, daemon=True).start()
        else:
            raise ValueError('此节点不支持该操作。')
        return view(token)