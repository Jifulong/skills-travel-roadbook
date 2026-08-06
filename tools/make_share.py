#!/usr/bin/env python3
"""把 data.json 打包成完全自包含的分享页(所有底图/照片内嵌为 data URI)。

托管页面禁止任何外部请求, 所以:
  · 底图 -> 高德静态地图 API 出 PNG, 内嵌
  · 照片 -> 下载后用 sips 压缩到 560px/q60, 内嵌
用法: AMAP_KEY=xxx python3 make_share.py
"""
import base64
import json
import os
import subprocess
import sys
import tempfile
import urllib.parse
import urllib.request

import cache

KEY = os.environ.get("AMAP_KEY", "")
if not KEY:
    sys.exit("缺少 AMAP_KEY")

# 经幡五色: 蓝(天) 白->赭 红(火) 绿(水) 黄(土); 第 6 天回到起点, 复用第 1 天的蓝
DAY_HEX = ["0x2F6FB5", "0xB5723A", "0xC8452F", "0x3F8F5F", "0xD9A32C", "0x2F6FB5"]

UA = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36"}


def fetch(url, timeout=30):
    return urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=timeout).read()


def static_map(day, color, max_pts=88):
    """出一张带路线折线和编号标记的静态底图。URL 有长度上限, 折线必须抽稀。"""
    poly = day["route"]["polyline"] if day["route"] else [[s["lon"], s["lat"]] for s in day["spots"]]
    step = max(1, len(poly) // max_pts)
    thin = poly[::step]
    if thin[-1] != poly[-1]:
        thin.append(poly[-1])
    path = f"6,{color},1,,:" + ";".join(f"{x:.5f},{y:.5f}" for x, y in thin)
    marks = "|".join(
        f"mid,{color},{i+1}:{s['lon']:.5f},{s['lat']:.5f}" for i, s in enumerate(day["spots"][:9]))
    url = ("https://restapi.amap.com/v3/staticmap?size=1000*620&scale=2"
           f"&paths={urllib.parse.quote(path, safe=':,;')}"
           f"&markers={urllib.parse.quote(marks, safe=':,;|')}&key={KEY}")
    if len(url) > 8000:
        return static_map(day, color, max_pts=max_pts // 2)
    return fetch(url)


def shrink(raw, px=560, q=58):
    """用 macOS 自带 sips 压缩, 避免额外依赖。返回 jpeg bytes。"""
    with tempfile.TemporaryDirectory() as td:
        src, dst = f"{td}/i", f"{td}/o.jpg"
        open(src, "wb").write(raw)
        r = subprocess.run(["sips", "-Z", str(px), "-s", "format", "jpeg",
                            "-s", "formatOptions", str(q), src, "--out", dst],
                           capture_output=True)
        if r.returncode != 0 or not os.path.exists(dst):
            return None
        return open(dst, "rb").read()


def durl(raw, mime):
    return f"data:{mime};base64," + base64.b64encode(raw).decode()


def main():
    data = json.load(open("data.json", encoding="utf-8"))
    photo_cache = {}          # 原始 URL -> data URI, 跨天去重
    total = 0

    for i, day in enumerate(data["days"]):
        color = DAY_HEX[i % len(DAY_HEX)]
        try:
            ckey = f"staticmap:d{day['day']}:{color}"
            png = cache.blob_cached(ckey, lambda: static_map(day, color), ext="png")
            day["map"] = durl(png, "image/png")
            total += len(png)
            print(f"D{day['day']} 底图 {len(png)/1024:>5.0f}KB")
        except Exception as e:
            day["map"] = ""
            print(f"D{day['day']} 底图失败: {e}")

        for s in day["spots"]:
            embedded, links = [], []
            for p in s["photos"]:
                # 只内嵌高德图: POI 挂钩、链接长期有效、体积可控。
                # 小红书图直链带时效签名(过期即失效), 全量内嵌会让包体翻倍还留不住 ——
                # 只保留标题和原帖链接, 让人自己点过去看。
                if p["src"] == "xhs":
                    if p.get("link"):
                        links.append({"title": p.get("title", ""),
                                      "author": p.get("author", ""), "link": p["link"]})
                    continue
                if len(embedded) >= 2:
                    continue
                u = p["url"]
                if u in photo_cache:
                    embedded.append(photo_cache[u])
                    continue
                try:
                    raw = cache.blob_cached(u, lambda: fetch(u), ext="orig")
                    small = shrink(raw) if raw else None
                    if not small:
                        continue
                    uri = durl(small, "image/jpeg")
                    photo_cache[u] = uri
                    embedded.append(uri)
                    total += len(small)
                except Exception as e:                      # noqa: BLE001
                    print(f"   照片失败 {s['matched']}: {e}")
            s["img"] = embedded
            s["xhs"] = links[:3]
            s.pop("photos", None)
        n = sum(len(s["img"]) for s in day["spots"])
        nl = sum(len(s["xhs"]) for s in day["spots"])
        print(f"      内嵌 {n} 张 · 小红书链接 {nl} 条 / {len(day['spots'])} 点位")

    # 折线抽稀后再进页面(前端只用来画示意线, 不需要 6000 点)
    for day in data["days"]:
        if day["route"]:
            p = day["route"]["polyline"]
            st = max(1, len(p) // 400)
            day["route"]["polyline"] = [[round(x, 5), round(y, 5)] for x, y in p[::st]]

    payload = json.dumps(data, ensure_ascii=False, separators=(",", ":"))
    tpl = open("share_template.html", encoding="utf-8").read()
    body = tpl.replace("/*__DATA__*/null", payload)

    # 1) 托管版: 只有正文, 外层 head 由平台补齐
    open("share.html", "w", encoding="utf-8").write(body)

    # 2) 独立版: 补完整文档外壳。charset 必须有 ——
    #    单独打开时浏览器会按 locale 猜编码, 中文直接乱码。
    shell = ('<!doctype html>\n<html lang="zh-CN">\n<head>\n'
             '<meta charset="utf-8">\n'
             '<meta name="viewport" content="width=device-width,initial-scale=1">\n'
             '<meta name="color-scheme" content="light dark">\n'
             f'{body}\n</body>\n</html>\n')
    # body 里已含 <title> 和 <style>, 放进 head 后用 </head><body> 收口
    shell = shell.replace("<div class=\"wrap wrap-head\">",
                          "</head>\n<body>\n<div class=\"wrap wrap-head\">", 1)
    standalone = "川西大环线6天路书.html"
    open(standalone, "w", encoding="utf-8").write(shell)

    cache.flush()
    mb = len(body.encode()) / 1024 / 1024
    print(f"\n内嵌资源 {total/1024/1024:.2f}MB · 图片 {len(photo_cache)} 张(已跨天去重)")
    print(f"托管版 share.html            {mb:.2f}MB")
    print(f"独立版 {standalone}  {len(shell.encode())/1024/1024:.2f}MB  ← 直接发这个")
    if mb > 15:
        print("⚠ 超过 16MB 上限, 需要降低 shrink() 的 px/q")


if __name__ == "__main__":
    main()
