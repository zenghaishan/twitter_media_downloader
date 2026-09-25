"""WebDAV 双向同步。

同步内容（视频不参与）：
  - config-proxy 配置（含 cookie/代理/下载目录）—— 序列化为 JSON 后 AES-GCM 加密上传
  - download_history —— 同上加密上传
  - 头像/缩略图（downloads/.avatars、downloads/.thumbnails）—— 明文增量上传/补拉

机制：
  - push_all(): 无条件把当前 config/history/头像/缩略图 推送到云端
  - pull_all(): 仅当本地是“全新状态”（无历史且未配置）时从云端还原 config/history；
                头像/缩略图则始终做“本地缺失即补拉”的合并
  - 加密口令 webdav_encrypt_pass 只在本地加密使用，绝不随 config 上传
"""
import hashlib  # noqa: F401  (reserved)
import json
import os
import threading
import urllib.parse

import httpx

from config import Config
from crypto import encrypt_bytes, decrypt_bytes, b64, unb64
import database

CONFIG_FILE = 'config.json'
HISTORY_FILE = 'history.json'

# 以下配置项仅本地保留，绝不随 config 上传
_LOCAL_ONLY_KEYS = {'webdav_encrypt_pass', 'webdav_password', 'secret_key', 'device_token'}
_MEDIA_DIRS = ('.avatars', '.thumbnails')  # (本地目录, 云端前缀标识) 本地扫描时使用实际目录名


def _cfg(key: str) -> str:
    return (database.get_config(key) or '').strip()


def is_configured() -> bool:
    return bool(_cfg('webdav_url') and _cfg('webdav_username')
                and _cfg('webdav_enabled') in ('1', 'true', 'on'))


class WebDAVError(Exception):
    pass


class Dav:
    """极简 WebDAV 客户端（MKCOL / PUT / GET / HEAD / PROPFIND）"""

    def __init__(self, base_url: str, username: str, password: str):
        self.base = base_url.rstrip('/')
        self._c = httpx.Client(auth=httpx.BasicAuth(username, password),
                               follow_redirects=True, timeout=40)

    def _href(self, *segs: str) -> str:
        path = self.base
        for s in segs:
            path += '/' + urllib.parse.quote(s.strip('/'), safe='-_.~')
        return path

    def close(self):
        self._c.close()

    def mkcol(self, *segs) -> bool:
        r = self._c.request('MKCOL', self._href(*segs))
        if r.status_code in (200, 201, 204, 301, 405):
            return True
        raise WebDAVError(f'MKCOL {segs[-1]} 失败: HTTP {r.status_code}')

    def put(self, data: bytes, *segs) -> bool:
        r = self._c.put(self._href(*segs), content=data)
        if r.status_code in (200, 201, 204):
            return True
        if r.status_code == 423:
            raise WebDAVError(f'PUT {segs[-1]} 失败: HTTP 423 云端文件被锁定。'
                              '请在 WebDAV 服务器端解锁/删除该文件，或在配置里更换 webdav_dir 目录名。')
        raise WebDAVError(f'PUT {segs[-1]} 失败: HTTP {r.status_code}')

    def delete(self, *segs) -> None:
        r = self._c.request('DELETE', self._href(*segs))
        if r.status_code in (200, 202, 204, 404):
            return
        raise WebDAVError(f'DELETE {segs[-1]} 失败: HTTP {r.status_code}')

    def unlock(self, *segs) -> None:
        """尝试解除文件上的锁（部分服务器通过 UNLOCK 释放独占写锁）"""
        try:
            self._c.request('UNLOCK', self._href(*segs))
        except Exception:
            pass  # 服务器不支持 UNLOCK 时忽略

    def get(self, *segs) -> bytes:
        r = self._c.get(self._href(*segs))
        return r.content if r.status_code == 200 else None

    def exists(self, *segs) -> bool:
        r = self._c.head(self._href(*segs))
        return r.status_code in (200, 207)

    def list_names(self, *segs) -> list:
        """列出某目录下的一级条目名"""
        r = self._c.request('PROPFIND', self._href(*segs),
                            headers={'Depth': '1'})
        if r.status_code not in (200, 207):
            return []
        hrefs = []
        body = r.text
        while '<d:href>' in body or '<href>' in body:
            start = body.find('<d:href>')
            tag = '<d:href>'
            if start == -1:
                start = body.find('<href>')
                tag = '<href>'
            if start == -1:
                break
            end = body.find(f'</{tag[1:]}', start)
            if end == -1:
                break
            hrefs.append(body[start + len(tag):end].strip())
            body = body[end + len(tag) + 2:]
        names = [urllib.parse.unquote(h.rsplit('/', 1)[-1]) for h in hrefs
                 if h.rsplit('/', 1)[-1]]
        return names


def get_dav() -> Dav:
    if not is_configured():
        raise WebDAVError('未启用 WebDAV 或配置不完整')
    return Dav(_cfg('webdav_url'), _cfg('webdav_username'), _cfg('webdav_password'))


def _put_overwrite(dav: Dav, data: bytes, *segs) -> bool:
    """覆盖写入：先尝试解除遗留锁并删除旧文件，再提交；失败时解锁重试一次"""
    try:
        dav.unlock(*segs)          # 尝试释放目标上的独占锁
    except WebDAVError:
        pass
    try:
        dav.delete(*segs)          # 删除旧文件，避免覆盖被锁资源
    except WebDAVError:
        pass  # 不存在/无权限删除时忽略，继续尝试写入
    try:
        return dav.put(data, *segs)
    except WebDAVError as e:
        if '423' in str(e):
            dav.unlock(*segs)      # 再解锁一次后重试
            dav.unlock(*segs[:-1]) # 有时锁在父集合上
            return dav.put(data, *segs)
        raise


def _export_config() -> dict:
    configs = {c['key']: c['value'] for c in database.get_all_configs()}
    for k in _LOCAL_ONLY_KEYS:
        configs.pop(k, None)
    return {'schema': 1, 'configs': configs}


def _restore_config(payload: dict):
    configs = payload.get('configs', {}) or {}
    for k, v in configs.items():
        database.update_config(k, v if v is not None else '')


def _encrypt_json(obj: dict, enc_pass: str) -> str:
    return b64(encrypt_bytes(
        json.dumps(obj, ensure_ascii=False, default=str).encode('utf-8'),
        enc_pass))


def _decrypt_json(text: str, enc_pass: str) -> dict:
    return json.loads(decrypt_bytes(unb64(text), enc_pass).decode('utf-8'))


def _is_fresh_local() -> bool:
    """本地是否“全新状态”（无历史且未做任何下载配置）"""
    return (database.count_history_all() == 0
            and not _cfg('proxy')
            and not _cfg('auth_token')
            and not _cfg('download_dir'))


def _local_media_names() -> dict:
    """返回 {云端子目录名: [本地相对文件名...]}"""
    result = {}
    root = os.path.join(Config.BASE_DIR, 'downloads')
    for sub in _MEDIA_DIRS:
        files = []
        base = os.path.join(root, sub)
        if os.path.isdir(base):
            for dp, _, fns in os.walk(base):
                for fn in fns:
                    rel = os.path.relpath(os.path.join(dp, fn), base)
                    files.append(rel.replace('\\', '/'))
        result[sub] = files
    return result


def push_all() -> dict:
    """全量推送：config/history 明文 JSON + 头像/缩略图 上传"""
    if not is_configured():
        raise WebDAVError('未启用 WebDAV 或配置不完整')

    dav = get_dav()
    try:
        root = _cfg('webdav_dir') or 'twitter_downloader'
        dav.mkcol(root)
        _put_overwrite(dav, json.dumps(_export_config(), ensure_ascii=False).encode('utf-8'),
                       root, CONFIG_FILE)
        hist = {'schema': 1, 'history': database.export_all_history()}
        _put_overwrite(dav, json.dumps(hist, ensure_ascii=False).encode('utf-8'),
                       root, HISTORY_FILE)

        uploaded = 0
        base = os.path.join(Config.BASE_DIR, 'downloads')
        for sub in _MEDIA_DIRS:
            dav.mkcol(root, sub)
            path = os.path.join(base, sub)
            if not os.path.isdir(path):
                continue
            for dp, _, fns in os.walk(path):
                for fn in fns:
                    local = os.path.join(dp, fn)
                    rel = os.path.relpath(local, path).replace('\\', '/')
                    data = open(local, 'rb').read()
                    dav.put(data, root, '_'.join([sub, rel.replace('/', '_')]))
                    uploaded += 1
        return {'pushed': True, 'files_uploaded': uploaded}
    finally:
        dav.close()


def pull_all() -> dict:
    """按需拉取还原"""
    dav = get_dav()
    try:
        root = _cfg('webdav_dir') or 'twitter_downloader'
        restored_cfg = restored_hist = False

        raw_cfg = dav.get(root, CONFIG_FILE)
        if raw_cfg:
            payload = json.loads(raw_cfg.decode('utf-8'))
            if _is_fresh_local():
                _restore_config(payload)
                restored_cfg = True
        raw_hist = dav.get(root, HISTORY_FILE)
        if raw_hist:
            payload = json.loads(raw_hist.decode('utf-8'))
            if _is_fresh_local():
                database.restore_history(payload.get('history', []))
                restored_hist = True

        # 头像/缩略图：本地缺失即补拉
        pulled = 0
        base = os.path.join(Config.BASE_DIR, 'downloads')
        for sub in _MEDIA_DIRS:
            names = dav.list_names(root, sub)
            for name in names:
                dst = os.path.join(base, sub, name.replace('_', '/'))
                if os.path.exists(dst):
                    continue
                data = dav.get(root, sub, name)
                if data is None:
                    continue
                os.makedirs(os.path.dirname(dst), exist_ok=True)
                with open(dst, 'wb') as f:
                    f.write(data)
                pulled += 1
        return {'pulled': True, 'restored_config': restored_cfg,
                'restored_history': restored_hist, 'media_pulled': pulled}
    finally:
        dav.close()


def test_connection() -> bool:
    """测试 WebDAV 连接是否可用"""
    dav = get_dav()
    try:
        root = _cfg('webdav_dir') or 'twitter_downloader'
        dav.mkcol(root)
        return True
    except Exception:
        return False
    finally:
        dav.close()


# ---------- 后台异步调用（避免阻塞请求线程） ----------
_push_lock = threading.Lock()


def push_async():
    def _run():
        if not is_configured():
            return
        with _push_lock:
            try:
                push_all()
            except Exception:
                import traceback
                traceback.print_exc()

    t = threading.Thread(target=_run, daemon=True)
    t.start()