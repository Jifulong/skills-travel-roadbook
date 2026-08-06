#!/usr/bin/env python3
"""本地缓存。高德和小红书都有限流(高德 10021 并发超限, 小红书 461),
反复重跑同一条行程没必要每次都打接口。

结构:
    .cache/index.json      键 -> JSON 值(地理编码/路线/小红书搜图结果)
    .cache/blob/<sha1>.<ext>  二进制(景点照片/静态底图), 按 URL 哈希存

键的写法由调用方决定, 约定加前缀便于人工排查:
    geo:<搜索词>|<城市>      drive:<坐标串>      xhs:<搜索词>|<张数>
"""
import hashlib
import json
import os
import threading

_HERE = os.path.dirname(os.path.abspath(__file__))
DIR = os.path.join(_HERE, ".cache")
BLOB = os.path.join(DIR, "blob")
_INDEX = os.path.join(DIR, "index.json")
_lock = threading.Lock()
_mem = None
_dirty = False


def _load():
    global _mem
    if _mem is None:
        os.makedirs(BLOB, exist_ok=True)
        if os.path.exists(_INDEX):
            try:
                with open(_INDEX, encoding="utf-8") as f:
                    _mem = json.load(f)
            except (json.JSONDecodeError, OSError):
                print("⚠ 缓存索引损坏, 已重建")
                _mem = {}
        else:
            _mem = {}
    return _mem


def get(key, default=None):
    return _load().get(key, default)


def has(key):
    return key in _load()


def put(key, value):
    global _dirty
    with _lock:
        _load()[key] = value
        _dirty = True


def flush():
    """写盘。批量跑完调一次即可, 避免每次 put 都做全量序列化。"""
    global _dirty
    if not _dirty:
        return
    with _lock:
        os.makedirs(DIR, exist_ok=True)
        tmp = _INDEX + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(_mem, f, ensure_ascii=False, separators=(",", ":"))
        os.replace(tmp, _INDEX)     # 原子替换, 中途被打断不会留半个文件
        _dirty = False


def cached(key, produce, refresh=False):
    """有缓存直接返回; 否则调 produce() 并存。produce 抛异常时不写缓存。"""
    if not refresh and has(key):
        return get(key)
    val = produce()
    put(key, val)
    return val


# ---------------------------------------------------------------- 二进制

def _blob_path(url, ext):
    h = hashlib.sha1(url.encode("utf-8")).hexdigest()
    return os.path.join(BLOB, f"{h}.{ext}")


def blob_get(url, ext="bin"):
    p = _blob_path(url, ext)
    if os.path.exists(p):
        return open(p, "rb").read()
    return None


def blob_put(url, data, ext="bin"):
    os.makedirs(BLOB, exist_ok=True)
    p = _blob_path(url, ext)
    tmp = p + ".tmp"
    with open(tmp, "wb") as f:
        f.write(data)
    os.replace(tmp, p)
    return p


def blob_cached(url, produce, ext="bin", refresh=False):
    if not refresh:
        got = blob_get(url, ext)
        if got is not None:
            return got
    data = produce()
    if data:
        blob_put(url, data, ext)
    return data


def stats():
    idx = _load()
    kinds = {}
    for k in idx:
        kinds[k.split(":", 1)[0]] = kinds.get(k.split(":", 1)[0], 0) + 1
    nblob = len(os.listdir(BLOB)) if os.path.isdir(BLOB) else 0
    size = sum(os.path.getsize(os.path.join(BLOB, f)) for f in os.listdir(BLOB)) if nblob else 0
    return {"entries": len(idx), "kinds": kinds, "blobs": nblob, "blob_mb": round(size / 1048576, 2)}


if __name__ == "__main__":
    st = stats()
    print(f"缓存条目 {st['entries']} 个 {st['kinds']}")
    print(f"二进制 {st['blobs']} 个 / {st['blob_mb']}MB")
    print(f"位置 {DIR}")
