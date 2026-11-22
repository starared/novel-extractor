"""Instance-wide model settings. Only public() results belong in HTTP replies."""
import json
import os
import tempfile
import threading
from pathlib import Path
from urllib.parse import urlsplit


SETTINGS_FILE = Path(__file__).resolve().parent / 'data' / 'model.json'
_write_lock = threading.Lock()


def load():
    try:
        cfg = json.loads(SETTINGS_FILE.read_text(encoding='utf-8'))
        if not isinstance(cfg, dict) or not all(isinstance(cfg.get(k), str) for k in ('base_url', 'model', 'api_key')):
            raise ValueError()
        return cfg
    except FileNotFoundError:
        return dict(base_url=os.getenv('MODEL_BASE_URL', 'https://api.openai.com/v1').strip().rstrip('/'),
                    model=os.getenv('MODEL_NAME', '').strip(), api_key=os.getenv('MODEL_API_KEY', ''))
    except (OSError, ValueError):
        raise ValueError('无法读取模型配置文件，请检查数据卷权限和文件内容。') from None


def public(cfg):
    return dict(base_url=cfg['base_url'], model=cfg['model'], has_key=bool(cfg['api_key']))


def resolve(fields, *, require_model=True):
    if not isinstance(fields, dict):
        raise ValueError('配置必须为 JSON 对象。')
    url, model, key = (fields.get(k, '') for k in ('base_url', 'model', 'api_key'))
    clear = fields.get('clear_key', False)
    if not all(isinstance(x, str) for x in (url, model, key)) or type(clear) is not bool:
        raise ValueError('配置字段类型错误。')
    url, model, key = url.strip().rstrip('/'), model.strip(), key.strip()
    try:
        p = urlsplit(url)
        valid = (url.isascii() and not any(ord(c) <= 32 or ord(c) == 127 or c in '\\?#' for c in url)
                 and p.scheme in ('http', 'https') and p.hostname and p.username is None
                 and p.port != 0 and len(url) <= 2048 and not p.path.endswith('/chat/completions'))
    except ValueError:
        valid = False
    if not valid:
        raise ValueError('请输入 HTTP(S) API 根地址，不含凭据、查询参数或 /chat/completions。')
    if (require_model and not model) or len(model) > 256 or (model and not model.isprintable()):
        raise ValueError('请输入有效模型名（最多 256 个字符）。')
    if key and (clear or not key.isascii() or not key.isprintable() or len(key) > 4096):
        raise ValueError('密钥无效，或同时选择了清除密钥。')
    old = load()
    return dict(base_url=url, model=model, api_key=key or (old['api_key'] if url == old['base_url'] and not clear else ''))


def save(fields):
    with _write_lock:
        cfg = resolve(fields)
        temp = None
        try:
            SETTINGS_FILE.parent.mkdir(mode=0o700, exist_ok=True, parents=True)
            with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', dir=SETTINGS_FILE.parent, delete=False) as f:
                temp = Path(f.name)
                json.dump(cfg, f, ensure_ascii=False)
            os.replace(temp, SETTINGS_FILE)
        except OSError:
            raise ValueError('无法保存模型配置，请检查数据卷的写入权限。') from None
        finally:
            if temp is not None:
                temp.unlink(missing_ok=True)
        return public(cfg)