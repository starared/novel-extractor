import logging
import re
import json
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote, urlsplit

import engine
import settings
import workflow

ROOT = Path(__file__).resolve().parent


class Handler(BaseHTTPRequestHandler):
    def log_request(self, code='-', size='-'):
        if self.command == 'GET' and int(code) == 200 and (self.path == '/health' or self.path.startswith('/flows/')):
            return
        path = re.sub(r'/flows/[^/?]+', '/flows/<task>', self.path.split('?')[0])
        logging.getLogger('http').info('%s %s %s', self.command, path, code)

    def reply(self, data, code=200, mime='application/json; charset=utf-8'):
        body = data if isinstance(data, bytes) else json.dumps(data, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header('Content-Type', mime)
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Cache-Control', 'no-store')
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        try:
            if self.path == '/':
                return self.reply((ROOT/'index.html').read_bytes(), mime='text/html; charset=utf-8')
            if self.path == '/workflow.js':
                return self.reply((ROOT/'workflow.js').read_bytes(), mime='text/javascript; charset=utf-8')
            if self.path in ('/health','/settings'):
                cfg = settings.public(settings.load())
                return self.reply(cfg if self.path == '/settings' else {'ok':True,'model_configured':bool(cfg['model'])})
            if self.path.startswith('/flows/'):
                return self.reply(workflow.view(self.path[7:]))
            raise KeyError()
        except KeyError:
            self.reply({'error':'本次制作记录或页面不存在。'},404)
        except ValueError as exc:
            self.reply({'error':str(exc)},500)

    def do_POST(self):
        try:
            origin = self.headers.get('Origin')
            if origin and urlsplit(origin).netloc != self.headers.get('Host'):
                return self.reply({'error':'不接受跨站提交。'},403)
            path = self.path.strip('/').split('/')
            size = int(self.headers.get('Content-Length','0'))
            if len(path)==3 and path[0]=='flows' and path[2]=='upload':
                if self.headers.get_content_type() != 'application/octet-stream':
                    raise ValueError('需要 TXT 原始文件流。')
                declared = self.headers.get('X-File-Size')
                if declared is not None and int(declared) != size:
                    raise ValueError(f'上传大小不符：浏览器选中的文件为 {int(declared):,} 字节，请求正文为 {size:,} 字节。请重新选择文件。')
                workflow.upload(path[1], self.rfile, size, unquote(self.headers.get('X-Filename','')))
                return self.reply(workflow.view(path[1]))
            if self.headers.get_content_type() != 'application/json':
                return self.reply({'error':'需要 application/json。'},415)
            # Only control messages are bounded. Novel files never enter this path.
            if not 0 < size <= (16384 if self.path.startswith('/settings') else 48*1024*1024):
                return self.reply({'error':'控制请求为空或过大。'},413)
            data = json.loads(self.rfile.read(size))
            if not isinstance(data,dict):
                raise ValueError('请求必须为 JSON 对象。')
            if self.path == '/settings':
                return self.reply(settings.save(data))
            if self.path == '/settings/models':
                return self.reply({'models':engine.list_models(data)})
            if self.path == '/flows':
                return self.reply({'id':workflow.create()},201)
            if len(path)!=3 or path[0]!='flows':
                raise KeyError()
            _, token, op = path
            if op == 'exchange':
                return self.reply(workflow.exchange(token, data.get('index')))
            if op == 'part':
                return self.reply({'text':workflow.inspect_part(token,data.get('part'))})
            if op == 'context':
                with workflow.guard:
                    f = workflow.check(token,'opening')
                    return self.reply(engine.opening_context(f['entries'],engine.short(data.get('instructions'),3000),engine.short(data.get('role_requirements'),3000)))
            if op == 'release':
                workflow.release(token)
                return self.reply({'ok':True})
            if op == 'discard':
                workflow.discard(token)
                return self.reply({'ok':True})
            if op == 'action':
                return self.reply(workflow.action(token,data))
            if op in ('png','world'):
                with workflow.guard:
                    f = workflow.check(token)
                    if f['step'] not in ('opening', 'export', 'done'):
                        raise workflow.Conflict('请先完成角色卡编辑。')
                    for key, limit in [('opening',12000), ('description',3000)]:
                        if key in data:
                            if not isinstance(data[key], str) or len(data[key]) > limit:
                                raise ValueError('文本长度或格式无效。')
                    for key in ('opening','description'):
                        if key in data:
                            f[key] = data[key]
                    project = {k:f[k] for k in ('opening','description','entries','title')}
                project['title'] = engine.short(data.get('title'),100) or project['title']
                card, world = engine.artifacts(project)
                return self.reply(engine.encode_png(card),mime='image/png') if op=='png' else self.reply(world)
            raise KeyError()
        except KeyError:
            self.reply({'error':'步骤或制作记录不存在。'},404)
        except workflow.Conflict as exc:
            self.reply({'error':str(exc)},409)
        except ValueError as exc:
            self.reply({'error':str(exc)},400)
        except OSError:
            self.reply({'error':'读写失败，请检查磁盘剩余空间和连接。'},507)


if __name__ == '__main__':
    logging.basicConfig(level=logging.INFO,format='%(asctime)s %(levelname)s %(name)s %(message)s')
    ThreadingHTTPServer((os.getenv('BIND_HOST','127.0.0.1'),int(os.getenv('PORT','49152'))),Handler).serve_forever()