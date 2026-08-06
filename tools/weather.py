#!/usr/bin/env python3
"""按坐标取天气与海拔(Open-Meteo, 免 key)。

为什么不用高德天气: 高德按区县返回, 而这条线一个县内海拔能差 2000m
(理塘县城 3960m vs 格聂之眼 4100m+), 按区县给的温度没有参考价值。
Open-Meteo 按坐标取, 且一次请求能带多个点, 顺带返回海拔。

海拔说明: 来自地形模型格点(约 1km 分辨率), 与实际路面可能差几百米 ——
折多山返回 4774m, 而垭口路面约 4298m。当量级参考, 别当精确值。
"""
import json
import time
import urllib.parse
import urllib.request

import cache

API = "https://api.open-meteo.com/v1/forecast"
BATCH = 40          # 一次请求带多少个点

# WMO 天气代码 -> 中文
WMO = {
    0: "晴", 1: "晴间多云", 2: "多云", 3: "阴",
    45: "雾", 48: "冻雾",
    51: "毛毛雨", 53: "小雨", 55: "中雨",
    56: "冻雨", 57: "强冻雨",
    61: "小雨", 63: "中雨", 65: "大雨",
    66: "冻雨", 67: "强冻雨",
    71: "小雪", 73: "中雪", 75: "大雪", 77: "米雪",
    80: "阵雨", 81: "强阵雨", 82: "暴雨",
    85: "阵雪", 86: "强阵雪",
    95: "雷阵雨", 96: "雷暴伴冰雹", 99: "强雷暴伴冰雹",
}


def desc(code):
    return WMO.get(code, f"代码{code}")


def _fetch(points, days):
    """points: [(name, lon, lat)]。返回 {name: 天气 dict}。"""
    lats = ",".join(f"{lat:.4f}" for _, _, lat in points)
    lons = ",".join(f"{lon:.4f}" for _, lon, _ in points)
    url = (f"{API}?latitude={lats}&longitude={lons}"
           "&daily=weather_code,temperature_2m_max,temperature_2m_min,"
           "precipitation_probability_max"
           "&current=temperature_2m,weather_code"
           f"&timezone=Asia%2FShanghai&forecast_days={days}")
    with urllib.request.urlopen(url, timeout=30) as f:
        raw = json.load(f)
    items = raw if isinstance(raw, list) else [raw]     # 单点时不是数组
    out = {}
    for (name, _, _), it in zip(points, items):
        cur = it.get("current") or {}
        dly = it.get("daily") or {}
        out[name] = {
            "elevation": round(it.get("elevation") or 0),
            "now": {"temp": cur.get("temperature_2m"),
                    "code": cur.get("weather_code"),
                    "desc": desc(cur.get("weather_code"))},
            "daily": [
                {"date": d, "tmax": mx, "tmin": mn,
                 "code": c, "desc": desc(c), "pop": pop}
                for d, c, mx, mn, pop in zip(
                    dly.get("time", []), dly.get("weather_code", []),
                    dly.get("temperature_2m_max", []),
                    dly.get("temperature_2m_min", []),
                    dly.get("precipitation_probability_max", []))
            ],
        }
    return out


def get(points, days=7, refresh=False, today=None):
    """points: [(name, lon, lat)]。按天缓存 —— 预报每天变, 但同一天内够用。"""
    today = today or time.strftime("%Y-%m-%d")
    result, todo = {}, []
    for name, lon, lat in points:
        ck = f"wx:{lat:.3f},{lon:.3f}:{days}:{today}"
        if not refresh and cache.has(ck):
            result[name] = cache.get(ck)
        else:
            todo.append((name, lon, lat, ck))

    for i in range(0, len(todo), BATCH):
        chunk = todo[i:i + BATCH]
        got = _fetch([(n, lo, la) for n, lo, la, _ in chunk], days)
        for name, _, _, ck in chunk:
            if name in got:
                cache.put(ck, got[name])
                result[name] = got[name]
        if i + BATCH < len(todo):
            time.sleep(0.5)
    cache.flush()
    return result


ARCHIVE = "https://archive-api.open-meteo.com/v1/archive"


def climate(points, start_md, end_md, years=5, this_year=None, refresh=False):
    """历史同期。出发日超出预报窗口(>16 天)时, 这才是唯一有参考价值的依据。

    start_md/end_md: "08-17" / "08-22"。取过去 years 年同一区间, 逐点汇总。
    返回 {name: {"tmax":均值, "tmin":均值, "rain_days_pct":有雨天占比, "years":[...]}}
    """
    this_year = this_year or int(time.strftime("%Y"))
    yrs = [this_year - i for i in range(1, years + 1)]
    acc = {n: {"tmax": [], "tmin": [], "wet": 0, "n": 0} for n, _, _ in points}

    for y in yrs:
        ck = f"clim:{y}:{start_md}:{end_md}:" + ",".join(
            f"{la:.2f}/{lo:.2f}" for _, lo, la in points)
        if not refresh and cache.has(ck):
            per = cache.get(ck)
        else:
            lats = ",".join(f"{la:.4f}" for _, _, la in points)
            lons = ",".join(f"{lo:.4f}" for _, lo, _ in points)
            url = (f"{ARCHIVE}?latitude={lats}&longitude={lons}"
                   f"&start_date={y}-{start_md}&end_date={y}-{end_md}"
                   "&daily=temperature_2m_max,temperature_2m_min,precipitation_sum"
                   "&timezone=Asia%2FShanghai")
            try:
                with urllib.request.urlopen(url, timeout=40) as f:
                    raw = json.load(f)
            except Exception as e:                      # noqa: BLE001
                print(f"  ⚠ {y} 年历史数据获取失败: {e}")
                continue
            items = raw if isinstance(raw, list) else [raw]
            per = {}
            for (name, _, _), it in zip(points, items):
                d = it.get("daily") or {}
                per[name] = {
                    "tmax": [x for x in d.get("temperature_2m_max", []) if x is not None],
                    "tmin": [x for x in d.get("temperature_2m_min", []) if x is not None],
                    "prcp": [x for x in d.get("precipitation_sum", []) if x is not None],
                }
            cache.put(ck, per)
            time.sleep(0.4)

        for name, v in (per or {}).items():
            a = acc.get(name)
            if not a:
                continue
            a["tmax"] += v["tmax"]
            a["tmin"] += v["tmin"]
            a["wet"] += sum(1 for x in v["prcp"] if x >= 1.0)   # ≥1mm 记一个雨天
            a["n"] += len(v["prcp"])

    cache.flush()
    out = {}
    for name, a in acc.items():
        if not a["tmax"]:
            continue
        out[name] = {
            "tmax": round(sum(a["tmax"]) / len(a["tmax"]), 1),
            "tmin": round(sum(a["tmin"]) / len(a["tmin"]), 1),
            "rain_days_pct": round(100 * a["wet"] / a["n"]) if a["n"] else None,
            "years": sorted(yrs),
            "range": f"{start_md}~{end_md}",
        }
    return out


if __name__ == "__main__":
    d = json.load(open("data.json", encoding="utf-8"))
    pts, seen = [], set()
    for day in d["days"]:
        for s in day["spots"]:
            if s["query"] not in seen:
                seen.add(s["query"])
                pts.append((s["query"], s["lon"], s["lat"]))
    wx = get(pts)
    print(f"{'点位':<16}{'海拔':>8}  {'实况':<14}{'今日':<18}明日")
    print("-" * 78)
    for n, _, _ in pts:
        w = wx.get(n)
        if not w:
            print(f"{n:<16}  (无数据)")
            continue
        d0 = w["daily"][0] if w["daily"] else {}
        d1 = w["daily"][1] if len(w["daily"]) > 1 else {}
        print(f"{n:<16}{w['elevation']:>6}m  "
              f"{str(w['now']['temp'])+'°C '+w['now']['desc']:<14}"
              f"{d0.get('desc','')} {d0.get('tmin')}~{d0.get('tmax')}°C  {d0.get('pop')}%   "
              f"{d1.get('desc','')} {d1.get('tmin')}~{d1.get('tmax')}°C")
