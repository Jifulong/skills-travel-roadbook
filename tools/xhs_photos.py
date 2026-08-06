#!/usr/bin/env python3
"""小红书景点实拍图抓取。

签名引擎(xhs_sign/)移植自 TripStar(GPL-2.0), 底层是 Spider_XHS 的 JS 签名,
经 PyExecJS 调本地 node 执行, 依赖 node_modules 里的 crypto-js + jsdom。

Cookie 来源(按优先级): 环境变量 XHS_COOKIE -> 同目录 .xhs_cookie 文件。
单独运行做连通性自检:
    python3 xhs_photos.py 格聂之眼 措普沟
"""
import json
import os
import sys
import time
import urllib.error
import urllib.request

from xhs_sign.sign_util import generate_request_params, trans_cookies

BASE = "https://edith.xiaohongshu.com"
_HERE = os.path.dirname(os.path.abspath(__file__))


class XHSError(RuntimeError):
    """Cookie 失效 / 被风控 / 接口变更, 都归到这里由调用方决定是否降级。"""


def load_cookie():
    ck = os.environ.get("XHS_COOKIE", "").strip()
    if ck:
        return ck
    path = os.path.join(_HERE, ".xhs_cookie")
    if os.path.exists(path):
        return open(path, encoding="utf-8").read().strip()
    raise XHSError("未配置小红书 Cookie: 设置 XHS_COOKIE 或写入 .xhs_cookie 文件")


def _post(api, data, cookie, tries=4):
    """461 是小红书的限流响应, 连着打十几次必中 —— 退避重试, 签名每次重算。"""
    last = None
    for i in range(tries):
        headers, cookies, body = generate_request_params(cookie, api, data, "POST")
        headers["Cookie"] = "; ".join(f"{k}={v}" for k, v in cookies.items())
        headers["Content-Type"] = "application/json;charset=UTF-8"
        req = urllib.request.Request(BASE + api, data=body.encode("utf-8"),
                                    headers=headers, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=20) as r:
                res = json.load(r)
            break
        except urllib.error.HTTPError as e:
            last = e
            if e.code in (461, 429) and i < tries - 1:
                time.sleep(8 + 10 * i)      # 8s / 18s / 28s
                continue
            raise XHSError(f"HTTP {e.code} {api}") from e
    else:
        raise XHSError(f"HTTP {getattr(last, 'code', '?')} {api} 重试耗尽")
    if not res.get("success"):
        code, msg = res.get("code", ""), res.get("msg", "")
        raise XHSError(f"接口失败 code={code} msg={msg}"
                       + ("  ← Cookie 已失效或被风控" if code == 300011 or "异常" in str(msg) else ""))
    return res


def search_notes(keyword, cookie, sort="time_descending", page_size=20):
    """搜笔记。默认按最新排序 —— 综合排序里高赞帖多是带大字的攻略拼图, 不适合当实景照。"""
    api = "/api/sns/web/v1/search/notes"
    data = {
        "keyword": keyword, "page": 1, "page_size": page_size,
        "search_id": os.urandom(11).hex(), "sort": "general", "note_type": 0,
        "ext_flags": [],
        "filters": [
            {"tags": [sort], "type": "sort_type"},
            {"tags": ["不限"], "type": "filter_note_type"},
            {"tags": ["不限"], "type": "filter_note_time"},
            {"tags": ["不限"], "type": "filter_note_range"},
            {"tags": ["不限"], "type": "filter_pos_distance"},
        ],
        "geo": "", "image_formats": ["jpg", "webp", "avif"],
    }
    return _post(api, data, cookie).get("data", {}).get("items", []) or []


def note_images(note_id, xsec_token, cookie):
    """取单篇笔记的图片直链。"""
    api = "/api/sns/web/v1/feed"
    data = {"source_note_id": note_id, "image_formats": ["jpg", "webp", "avif"],
            "extra": {"need_body_topic": "1"},
            "xsec_source": "pc_search", "xsec_token": xsec_token}
    items = _post(api, data, cookie).get("data", {}).get("items", []) or []
    if not items:
        return []
    urls = []
    for img in (items[0].get("note_card", {}).get("image_list") or []):
        info = img.get("info_list") or []
        # info_list[1] 通常是较大尺寸; 退化到默认链接
        u = (info[1].get("url") if len(info) > 1 else
             info[0].get("url") if info else
             img.get("url_default") or img.get("url_pre") or img.get("url"))
        if u:
            urls.append(u)
    return urls


def note_link(note_id, xsec_token):
    """原帖链接。xsec_token 必须带上, 否则打开是"当前内容无法查看"。"""
    return (f"https://www.xiaohongshu.com/explore/{note_id}"
            f"?xsec_token={xsec_token}&xsec_source=pc_search")


def get_photos(keyword, cookie, want=2, max_notes=3, suffix="风景"):
    """搜"<地名> 风景"取前几篇笔记的首图 + 原帖链接。

    返回 [{url, note_id, title, link, author}]。
    图片直链带时效签名(URL 里那串 20260804xxxx), 过期就失效 ——
    所以链接才是长期有效的那部分, 图片只当预览。
    """
    q = f"{keyword} {suffix}".strip()
    notes = [n for n in search_notes(q, cookie) if n.get("model_type") == "note"]
    out = []
    for n in notes[:max_notes]:
        nid, tok = n.get("id"), n.get("xsec_token", "")
        if not nid:
            continue
        try:
            imgs = note_images(nid, tok, cookie)
        except XHSError:
            continue
        if imgs:
            card = n.get("note_card") or {}
            out.append({
                "url": imgs[0],
                "note_id": nid,
                "title": (card.get("display_title") or "").strip()[:40],
                "author": ((card.get("user") or {}).get("nickname") or "").strip()[:20],
                "link": note_link(nid, tok),
            })
        if len(out) >= want:
            break
        time.sleep(1.0)          # 搜图接口打太快会触发风控(461)
    return out


if __name__ == "__main__":
    ck = load_cookie()
    ids = trans_cookies(ck)
    print(f"Cookie 字段 {len(ids)} 个, a1={'有' if ids.get('a1') else '缺失'}, "
          f"web_session={'有' if ids.get('web_session') else '缺失'}\n")
    for kw in (sys.argv[1:] or ["格聂之眼"]):
        try:
            ph = get_photos(kw, ck)
            print(f"{kw:<10} {len(ph)} 张")
            for p in ph:
                print(f"           {p['url'][:78]}")
                if p["title"]:
                    print(f"           来自笔记「{p['title']}」")
        except XHSError as e:
            print(f"{kw:<10} 失败: {e}")
        time.sleep(1)
