#!/usr/bin/env python3
"""行程文本 -> 逐日驾车路线 + 景点图片。

用法:
    export AMAP_KEY=xxx            # 高德 Web服务 key
    python3 build_route.py itinerary.txt -o data.json

输入格式(每行一天):
    1:成都—天全服务区—折多山——住新都桥
    2:住宿地—雅江天路十八弯——住理塘

"住X" 标记当日宿地(自动作为当天终点和次日起点)。"住宿地" 表示沿用前一晚宿地。
括号内用逗号分隔的内容会展开成途经点。
"""
import argparse
import json
import math
import os
import re
import sys
import time
import urllib.parse
import urllib.request

import cache

AMAP = "https://restapi.amap.com/v3"

# 语义噪音: 出现在地名里但不该进地理编码的描述性词
NOISE = re.compile(
    r"(网红打卡|网红公路|打卡点?|反穿|正穿|海拔\s*\d+米?|自驾|沿线|方向|附近|一日游|观光|游览)"
)

# 地名修正表: 原文写法 -> (高德搜索词, 城市限定)
# 城市限定至关重要 —— 缺了它高德会跨市模糊匹配到同名 POI(实测偏差可达 95km)
ALIAS = {}   # 由 load_aliases() 从 aliases.json 载入; 见下

def load_aliases(path="aliases.json"):
    """地名修正表: 原文写法 -> (高德搜索词, 城市限定)。

    每条线路的地名不同, 所以外置成 aliases.json, 换线路只改数据不改代码。
    格式: {"原文名": {"query": "高德搜索词", "city": "城市限定"}}
           query 为 null 表示这是路线名(如"格聂南线"), 不是点位, 直接丢弃。

    城市限定极其重要 —— 缺了它高德会跨市模糊匹配到同名 POI, 实测偏差可达 95km,
    而且不会报错, 只会安静地给出一条绕远的路线。
    """
    if not os.path.exists(path):
        return {}
    with open(path, encoding="utf-8") as f:
        raw = json.load(f)
    return {k: (v.get("query"), v.get("city", "")) for k, v in raw.items()}

OUTLIER_KM = 120  # 单点离当日重心超过此距离 -> 警告(可能定位到了同名的另一个地方)

# 特色景观标签: 关键词 -> 标签。只从已有证据里提取(POI 名称/类型 + 小红书笔记标题),
# 不凭空给点位贴标签 —— 贴错了会让人白跑一趟。
SCENERY = [
    ("雪山", ["雪山", "日照金山", "金山", "贡嘎", "神山", "冰川", "雪顶"]),
    ("花海", ["花海", "花开", "野花", "格桑花", "杜鹃", "花期", "成海"]),
    ("草甸", ["草原", "草甸", "牧场", "牦牛"]),
    ("湖泊", ["湖", "海子", "湖泊"]),
    ("星空", ["星空", "银河", "星轨"]),
    ("寺庙", ["寺", "佛学院", "白塔", "经幡", "喇嘛"]),
    ("古镇藏寨", ["古镇", "藏寨", "千户", "民居"]),
    ("垭口观景", ["垭口", "观景台", "机位", "观景点"]),
    ("温泉", ["温泉"]),
    ("峡谷", ["峡谷", "溪谷", "瀑布"]),
    ("地质奇观", ["石林", "丹霞", "墨石", "石头城", "地质"]),
    ("云海", ["云海", "云雾", "佛光"]),
    ("日落", ["日落", "晚霞", "夕阳", "日暮", "黄昏"]),
    ("日出", ["日出", "早起"]),
    ("森林", ["森林", "红叶", "原始林"]),
]


def derive_scenery(spot):
    """从点位名称/类型和小红书笔记标题里提取特色景观标签。

    返回 [{"tag":..., "from": "名称"|"小红书"}]; 名称命中优先(更可靠)。
    """
    name_text = " ".join([spot.get("matched", ""), spot.get("input", ""),
                          spot.get("type", ""), spot.get("address", "")])
    xhs_text = " ".join(p.get("title", "") for p in spot.get("photos", [])
                        if p.get("src") == "xhs")
    out, seen = [], set()
    for tag, kws in SCENERY:
        if tag in seen:
            continue
        if any(k in name_text for k in kws):
            out.append({"tag": tag, "from": "名称"})
            seen.add(tag)
        elif any(k in xhs_text for k in kws):
            out.append({"tag": tag, "from": "小红书"})
            seen.add(tag)
    return out


def http_json(url, tries=5):
    """带并发限流重试的 GET。高德免费额度并发很低, 10021 是常态。"""
    last = None
    for i in range(tries):
        try:
            with urllib.request.urlopen(url, timeout=25) as f:
                r = json.load(f)
            if r.get("infocode") == "10021":  # CUQPS_HAS_EXCEEDED_THE_LIMIT
                time.sleep(1.5 + 2 * i)
                last = "并发超限"
                continue
            return r
        except Exception as e:  # noqa: BLE001
            last = str(e)
            time.sleep(1.5 + 2 * i)
    raise RuntimeError(f"高德请求失败: {last}\n  {url[:120]}")


def s(v):
    """高德对空值返回 [] 而不是 "" —— 直接当字符串用会 TypeError。"""
    return v if isinstance(v, str) else ""


def haversine_km(a, b):
    (lon1, lat1), (lon2, lat2) = a, b
    dx = (lon2 - lon1) * math.cos(math.radians((lat1 + lat2) / 2)) * 111.32
    dy = (lat2 - lat1) * 110.57
    return math.hypot(dx, dy)


# ---------------------------------------------------------------- 文本解析

def parse_itinerary(text):
    """把行程文本拆成 [{"day": 1, "raw": [...], "stay": "新都桥"}]。"""
    days = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        m = re.match(r"^(\d+)\s*[:：]\s*(.+)$", line)
        if not m:
            print(f"  跳过无法识别的行: {line[:40]}", file=sys.stderr)
            continue
        day, body = int(m.group(1)), m.group(2)

        # 括号内的逗号分隔项展开为并列途经点
        def expand(mo):
            inner = re.split(r"[,，、]", mo.group(1))
            return "—" + "—".join(x.strip() for x in inner if x.strip())

        body = re.sub(r"[（(]([^）)]*)[）)]", expand, body)

        stay = None
        parts = []
        for tok in re.split(r"[—\-–~～/]+", body):
            tok = NOISE.sub("", tok).strip(" 、,，")
            if not tok:
                continue
            # "住宿地" 是"沿用前一晚宿地"的占位符, 不是"住X"的宿地声明
            if tok.startswith("住") and tok != "住宿地":
                stay = tok[1:].strip()
                continue
            parts.append(tok)
        days.append({"day": day, "raw": parts, "stay": stay})

    prev_stay = None
    for d in days:
        d["raw"] = [prev_stay if x == "住宿地" and prev_stay else x for x in d["raw"]]
        # 宿地即当日终点; 缺了它 D1 会停在最后一个景点而非酒店所在地
        if d["stay"] and (not d["raw"] or d["raw"][-1] != d["stay"]):
            d["raw"].append(d["stay"])
        if d["stay"]:
            prev_stay = d["stay"]
    return days


def resolve_name(name):
    """原文地名 -> (高德搜索词, 城市限定)。未登记的原样搜索、无城市限定。"""
    if name in ALIAS:
        return ALIAS[name]
    for k, v in ALIAS.items():           # 子串兜底: "雅江天路十八弯" 命中 "天路十八弯"
        if k in name or name in k:
            return v
    return (name, "")


# ---------------------------------------------------------------- 高德调用

def pick_best(pois, kw):
    """高德首条命中不一定最优 —— 实测"毛垭大草原"首条是"大毛垭坝养护站"。

    按 名称吻合度 > 是否景点类 > 有图 打分, 保证选到真正的目的地。
    """
    def score(p):
        name = s(p.get("name"))
        typ = s(p.get("type"))
        sc = 0
        if name == kw:
            sc += 100
        elif kw in name:
            sc += 60
        elif name and name in kw:
            sc += 30
        # 排斥养护站/收费站/村委会这类行政或设施 POI
        if re.search(r"(养护站|收费站|服务区|村民委员会|管理处|派出所)", name) and kw not in name:
            sc -= 40
        if "风景名胜" in typ or "旅游景点" in typ:
            sc += 20
        if [x for x in (p.get("photos") or []) if s(x.get("url"))]:
            sc += 10
        return sc

    return max(pois, key=score)


def geocode(name, key, refresh=False):
    kw, city = resolve_name(name)
    if kw is None:
        return None
    ck = f"geo:{kw}|{city}"
    if not refresh and cache.has(ck):
        hit = cache.get(ck)
        if hit:
            # 必须复制 photos 列表: 浅拷贝会让多天共用同一个 list, 后面 += 就地扩展
            # 会把缓存里的那份也撑大(新都桥出现 3 次 -> 图片累积到 8 张)
            hit = dict(hit, input=name, photos=list(hit.get("photos") or []))
        return hit
    base = f"{AMAP}/place/text?keywords={urllib.parse.quote(kw)}&extensions=all&offset=10&key={key}"

    # 先严格限定城市; 失败才放开 —— 放开时必须标记, 这是错定位的主要来源
    attempts = []
    if city:
        attempts.append((f"{base}&city={urllib.parse.quote(city)}&citylimit=true", False))
    attempts.append((f"{base}&city={urllib.parse.quote(city)}&citylimit=false" if city else base, True))

    for url, loose in attempts:
        r = http_json(url)
        if r.get("status") != "1":
            continue
        pois = r.get("pois") or []
        if not pois:
            continue
        p = pick_best(pois, kw)
        try:
            lon, lat = map(float, s(p.get("location")).split(","))
        except ValueError:
            continue
        photos = [{"url": s(x.get("url")), "src": "amap"}
                  for x in (p.get("photos") or []) if s(x.get("url"))]
        # 最准的 POI 常常是没图的行政点位("勒通古镇"vs"勒通古镇·千户藏寨旅游景区")。
        # 保留它的坐标, 但从同名候选里借图。
        if not photos:
            for alt in pois:
                if alt is p:
                    continue
                an = s(alt.get("name"))
                if kw not in an and an not in kw:
                    continue
                try:
                    alon, alat = map(float, s(alt.get("location")).split(","))
                except ValueError:
                    continue
                if haversine_km((lon, lat), (alon, alat)) > 8:   # 同名但相距过远, 不是同一处
                    continue
                borrowed = [{"url": s(x.get("url")), "src": "amap"}
                            for x in (alt.get("photos") or []) if s(x.get("url"))]
                if borrowed:
                    photos = borrowed
                    break
        result = {
            "input": name,
            "query": kw,
            "matched": s(p.get("name")),
            "adname": s(p.get("adname")),
            "address": s(p.get("address")),
            "lon": lon,
            "lat": lat,
            "type": s(p.get("type")).split(";")[0],
            "photos": photos[:3],
            "fuzzy": loose,   # True = 跨市模糊匹配, 需人工确认
        }
        cache.put(ck, result)
        return dict(result, input=name)
    cache.put(ck, None)
    return None


def drive(points, key, refresh=False):
    """points: [(lon,lat)] 首尾为起终点, 中间为途经点(高德上限 16)。"""
    fmt = lambda p: f"{p[0]:.6f},{p[1]:.6f}"
    ck = "drive:" + ";".join(fmt(p) for p in points)
    if not refresh and cache.has(ck):
        return cache.get(ck)
    if len(points) - 2 > 16:
        print(f"  途经点 {len(points)-2} 个超过高德上限 16, 已截断", file=sys.stderr)
        points = [points[0]] + points[1:-1][:16] + [points[-1]]
    url = (f"{AMAP}/direction/driving?origin={fmt(points[0])}&destination={fmt(points[-1])}"
           f"&extensions=base&strategy=0&key={key}")
    if len(points) > 2:
        way = ";".join(fmt(p) for p in points[1:-1])
        url += "&waypoints=" + urllib.parse.quote(way, safe=";,")
    r = http_json(url)
    if r.get("status") != "1":
        cache.put(ck, None)
        return None
    paths = (r.get("route") or {}).get("paths") or []
    if not paths:
        cache.put(ck, None)
        return None
    p = paths[0]
    poly = []
    for st in p.get("steps", []):
        for seg in s(st.get("polyline")).split(";"):
            if "," in seg:
                lon, lat = seg.split(",")
                poly.append([round(float(lon), 6), round(float(lat), 6)])
    res = {
        "distance_km": round(int(p.get("distance", 0)) / 1000, 1),
        "duration_min": int(p.get("duration", 0)) // 60,
        "polyline": poly,
    }
    cache.put(ck, res)
    return res


# ---------------------------------------------------------------- 主流程

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("input", help="行程文本文件")
    ap.add_argument("-o", "--output", default="data.json")
    ap.add_argument("--key", default=os.environ.get("AMAP_KEY", ""))
    ap.add_argument("--refresh", action="store_true", help="忽略本地缓存, 强制重新抓取")
    ap.add_argument("--refresh-xhs", action="store_true",
                    help="只重抓小红书(高德缓存保留), 换 cookie 后补链接用")
    ap.add_argument("--aliases", default="aliases.json",
                    help="地名修正表, 默认同目录 aliases.json")
    ap.add_argument("--start", metavar="YYYY-MM-DD",
                    help="第一天的日期; 给了就把每天绑定到具体日期, 并取该日预报+历史同期")
    ap.add_argument("--weather", action="store_true",
                    help="取各点位天气与海拔(Open-Meteo, 免 key)")
    ap.add_argument("--weather-days", type=int, default=7, metavar="N",
                    help="预报天数, 默认 7")
    ap.add_argument("--xhs", type=int, default=0, metavar="N",
                    help="每个点位额外抓 N 张小红书实拍图(需 .xhs_cookie), 0=关闭")
    args = ap.parse_args()
    if not args.key:
        sys.exit("缺少高德 Web服务 key: 设置 AMAP_KEY 环境变量或传 --key")

    global ALIAS
    ALIAS = load_aliases(args.aliases)
    print(f"地名修正表: {args.aliases} ({len(ALIAS)} 条)"
          if ALIAS else f"未找到 {args.aliases}, 全部按原文直接检索(容易跨市误匹配)")

    days = parse_itinerary(open(args.input, encoding="utf-8").read())
    print(f"解析出 {len(days)} 天\n")

    warnings = []
    out = []
    for d in days:
        spots = []
        for name in d["raw"]:
            if resolve_name(name)[0] is None:      # 路线名/线路描述, 非点位
                print(f"  D{d['day']} · {name:<18} 路线名, 按非点位忽略")
                continue
            g = geocode(name, args.key, args.refresh)
            if not g:
                warnings.append(f"D{d['day']} 「{name}」定位失败, 已从路线中剔除")
                print(f"  D{d['day']} ✗ {name:<18} 定位失败")
                continue
            spots.append(g)
            flag = " ⚠跨市模糊匹配" if g["fuzzy"] else ""
            note = "" if g["input"] == g["matched"] else f" (原文「{g['input']}」)"
            print(f"  D{d['day']} ✓ {g['matched'][:18]:<20}{g['lon']:.4f},{g['lat']:.4f}"
                  f"  {len(g['photos'])}图{note}{flag}")
            if g["fuzzy"]:
                warnings.append(f"D{d['day']} 「{name}」→「{g['matched']}」({g['adname']}) 跨市模糊匹配, 请核对")

        # 离群检测: 定位错误不会报错, 只会安静地给出一条绕远的路线。
        # 只查中间途经点 —— 首尾是当日起终点, 长途转场日本来就离得远(成都↔康定 300km)。
        if len(spots) >= 4:
            mids = spots[1:-1]
            cx = sum(x["lon"] for x in mids) / len(mids)
            cy = sum(x["lat"] for x in mids) / len(mids)
            for x in mids:
                dist = haversine_km((cx, cy), (x["lon"], x["lat"]))
                if dist > OUTLIER_KM:
                    msg = (f"D{d['day']} 「{x['input']}」→「{x['matched']}」({x['adname']}) "
                           f"距当日途经点重心 {dist:.0f}km, 疑似定位错误")
                    warnings.append(msg)
                    print(f"      ⚠ {msg}")

        route = drive([(x["lon"], x["lat"]) for x in spots], args.key, args.refresh) if len(spots) >= 2 else None
        if route:
            # 绕行比: 实际驾车里程 / 各段直线距离之和。折返定位错误会把这个值顶起来
            straight = sum(haversine_km((spots[i]["lon"], spots[i]["lat"]),
                                        (spots[i + 1]["lon"], spots[i + 1]["lat"]))
                           for i in range(len(spots) - 1))
            ratio = route["distance_km"] / straight if straight > 1 else 0
            route["detour_ratio"] = round(ratio, 2)
            print(f"      路线: {route['distance_km']} km / "
                  f"{route['duration_min']//60}h{route['duration_min']%60:02d}m "
                  f"/ 绕行比 {ratio:.2f} / {len(route['polyline'])} 折线点")
            # 逐段绕行比: 找出"高德没有这条路"的路段。实测格聂南线
            # 格聂神山→扎瓦拉 直线 33km 却给出 110km —— 非铺装路段无路网数据,
            # 高德绕回国道, 画出来的折线不是你实际要走的轨迹。
            for i in range(len(spots) - 1):
                a, b = spots[i], spots[i + 1]
                sl = haversine_km((a["lon"], a["lat"]), (b["lon"], b["lat"]))
                leg = drive([(a["lon"], a["lat"]), (b["lon"], b["lat"])], args.key, args.refresh)
                if not leg:
                    continue
                # 每段都记里程和耗时, 直接标在路线图和点位列表上
                lr = leg["distance_km"] / sl if sl >= 5 else None
                spots[i]["leg_to_next"] = {
                    "km": leg["distance_km"],
                    "min": leg["duration_min"],
                    "ratio": round(lr, 2) if lr else None,
                }
                if lr and lr > 2.5:
                    msg = (f"D{d['day']} 「{a['matched']}」→「{b['matched']}」直线 {sl:.0f}km "
                           f"但驾车 {leg['distance_km']:.0f}km ({lr:.1f}x), "
                           f"高德可能无此路段路网(非铺装), 折线不代表实际轨迹")
                    warnings.append(msg)
                    print(f"      ⚠ {msg}")
        elif len(spots) >= 2:
            warnings.append(f"D{d['day']} 驾车路线计算失败(可能是非铺装路段无路网数据)")
            print("      ⚠ 路线计算失败")
        out.append({"day": d["day"], "stay": d["stay"], "spots": spots, "route": route})
        print()

    # ---- 小红书补图(可选) ----
    # 高德 POI 图是挂在点位上的, 准但偏官方; 小红书是游客实拍, 更有现场感但可能跑偏
    # (搜到自拍/宠物/攻略拼图), 所以只做补充、排在高德之后, 并标注来源。
    if args.xhs > 0:
        try:
            import xhs_photos
            cookie = xhs_photos.load_cookie()
        except Exception as e:                                  # noqa: BLE001
            print(f"⚠ 小红书补图跳过: {e}")
            cookie = None
        if cookie:
            print(f"{'='*60}\n小红书补图(每点位 {args.xhs} 张)\n")
            failed = hits = 0
            aborted = False
            for d in out:
                for sp in d["spots"]:
                    key = sp["query"]
                    ck = f"xhs:{key}|{args.xhs}"
                    if not (args.refresh or args.refresh_xhs) and cache.has(ck):
                        add = cache.get(ck) or []
                        sp["photos"] = sp["photos"] + list(add)   # 不用 += , 避免就地改到缓存
                        hits += 1
                        continue
                    try:
                        got = xhs_photos.get_photos(key, cookie, want=args.xhs)
                    except xhs_photos.XHSError as e:
                        failed += 1
                        print(f"  ✗ {key}: {e}")
                        # 461/登录过期都是账号级的, 继续试下一个点位只是白等退避时间。
                        # 成功的已增量落盘, 稍后重跑会自动跳过、从这里续上。
                        if "461" in str(e) or "登录" in str(e) or "300011" in str(e):
                            warnings.append(
                                f"小红书补图中止于「{key}」({e}); 已抓到的已缓存, "
                                f"稍后重跑 --xhs 会从此处续上")
                            print("  ⏸ 账号级限流/失效, 中止本轮补图(已抓部分已缓存)")
                            aborted = True
                            break
                        continue
                    add = [{"url": g["url"], "src": "xhs", "title": g["title"],
                            "author": g.get("author", ""), "link": g["link"]} for g in got]
                    cache.put(ck, add)
                    # 每抓一个就落盘: 小红书随时可能 461/登录过期, 中途中断不能白抓
                    cache.flush()
                    sp["photos"] = sp["photos"] + list(add)
                    print(f"  {sp['matched'][:16]:<18}+{len(add)} 张"
                          + (f"  「{got[0]['title']}」" if got and got[0]["title"] else ""))
                    time.sleep(1.2)   # 小红书限流敏感, 放慢基础节奏
                if aborted:
                    break
            print(f"  (缓存命中 {hits} 次)\n")

    # ---- 天气与海拔(Open-Meteo, 免 key, 一次请求带全部点位) ----
    if args.weather:
        import weather
        pts, seen = [], set()
        for d in out:
            for sp in d["spots"]:
                if sp["query"] not in seen:
                    seen.add(sp["query"])
                    pts.append((sp["query"], sp["lon"], sp["lat"]))
        # 有出发日期时: 每天绑定到具体日期, 预报窗口尽量拉长以覆盖行程
        dates = {}
        if args.start:
            import datetime
            d0 = datetime.date.fromisoformat(args.start)
            for i, d in enumerate(out):
                dates[d["day"]] = (d0 + datetime.timedelta(days=i)).isoformat()
                d["date"] = dates[d["day"]]
            span = (datetime.date.fromisoformat(dates[out[-1]["day"]]) -
                    datetime.date.today()).days + 1
            fdays = min(16, max(args.weather_days, span))   # Open-Meteo 上限 16 天
            print(f"出发 {args.start}, 行程末日 {dates[out[-1]['day']]}, "
                  f"拉取 {fdays} 天预报")
        else:
            fdays = args.weather_days

        try:
            wx = weather.get(pts, days=fdays, refresh=args.refresh)
            for d in out:
                for sp in d["spots"]:
                    w = wx.get(sp["query"])
                    if w:
                        sp["wx"] = w
                        # 绑定到当天日期的那条预报(超出 16 天窗口就没有)
                        if d.get("date"):
                            sp["wx_day"] = next(
                                (x for x in w["daily"] if x["date"] == d["date"]), None)

            # 出发日超出预报窗口时, 历史同期是唯一有参考价值的依据
            if args.start:
                md = lambda iso: iso[5:]
                clim = weather.climate(pts, md(dates[out[0]["day"]]),
                                       md(dates[out[-1]["day"]]),
                                       years=5, refresh=args.refresh)
                for d in out:
                    for sp in d["spots"]:
                        c = clim.get(sp["query"])
                        if c:
                            sp["clim"] = c
                ncov = sum(1 for d in out for sp in d["spots"] if sp.get("wx_day"))
                ntot = sum(len(d["spots"]) for d in out)
                print(f"日期绑定: {ncov}/{ntot} 个点位有当日预报, "
                      f"其余只有历史同期(近 5 年)")
            data_wx_at = time.strftime("%Y-%m-%d %H:%M")
            hi = max((sp.get("wx", {}).get("elevation", 0)
                      for d in out for sp in d["spots"]), default=0)
            print(f"天气/海拔: {len(wx)}/{len(pts)} 个点位取到 · 最高点 {hi}m\n")
        except Exception as e:                                  # noqa: BLE001
            print(f"⚠ 天气获取失败: {e}")
            warnings.append(f"天气获取失败: {e}")
            data_wx_at = ""
    else:
        data_wx_at = ""

    # ---- 特色景观标签(在小红书补图之后跑, 才能用上笔记标题) ----
    for d in out:
        for sp in d["spots"]:
            sp["scenery"] = derive_scenery(sp)
    ntag = sum(1 for d in out for sp in d["spots"] if sp["scenery"])
    print(f"特色标签: {ntag}/{sum(len(d['spots']) for d in out)} 个点位有标注\n")

    data = {"days": out, "warnings": warnings,
            "weather_at": data_wx_at,
            "total_km": round(sum(x["route"]["distance_km"] for x in out if x["route"]), 1)}
    with open(args.output, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=1)
    cache.flush()

    n_spots = sum(len(x["spots"]) for x in out)
    n_photo = sum(1 for x in out for sp in x["spots"] if sp["photos"])
    n_amap = sum(1 for x in out for sp in x["spots"] for p in sp["photos"] if p["src"] == "amap")
    n_xhs = sum(1 for x in out for sp in x["spots"] for p in sp["photos"] if p["src"] == "xhs")
    print(f"{'='*60}\n写入 {args.output}")
    print(f"点位 {n_spots} 个, 其中 {n_photo} 个有图 | 全程 {data['total_km']} km")
    print(f"图片 {n_amap + n_xhs} 张: 高德 {n_amap} · 小红书 {n_xhs}")
    if warnings:
        print(f"\n⚠ {len(warnings)} 条待核对:")
        for w in warnings:
            print(f"  - {w}")


if __name__ == "__main__":
    main()
