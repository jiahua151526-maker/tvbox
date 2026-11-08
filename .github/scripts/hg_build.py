#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""黄果短剧 → 苹果CMS v10 JSON（静态化）

GitHub Actions 每日运行：抓黄果 App 的全部剧集，转成苹果CMS 格式的静态 JSON。
生成后的文件可直接作为影视仓/影视仓类 App 的 CMS 源。

用法：python3 hg_build.py [输出路径]
"""
import json
import os
import sys
import time
import uuid
import urllib.parse
import urllib.request
import urllib.error
from concurrent.futures import ThreadPoolExecutor

BASES = [
    "https://api.huangguaap025isda.com",
    "https://api.huangguaap024isda.com",
    "https://api.aiflj6662232.com",
    "https://api.aiflj2112312.com",
    "https://api.huangguaap021isda.com",
    "https://api.huangguaap022isda.com",
    "https://api.huangguaap023isda.com",
    "https://api.hgua.io",
]
UA = "okhttp/4.12.0"
DEV = "hg-gh-" + uuid.uuid4().hex[:12]
GOOD = [None]
TOKEN = [None, 0.0]

# 并发度：太高容易被服务端限流，6 是实测比较稳的值
WORKERS = 6
# 每部剧最多取多少集（防止个别超长剧拖死整轮）
MAX_EPS = 60


def _call(base, method, path, body=None, token=None, params=None, timeout=20):
    url = base + path
    if params:
        qs = "&".join(
            "%s=%s" % (k, urllib.parse.quote(str(v), safe=""))
            for k, v in params.items()
        )
        if qs:
            url += ("&" if "?" in path else "?") + qs
    data = json.dumps(body, ensure_ascii=False).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Accept", "application/json")
    req.add_header("User-Agent", UA)
    req.add_header("X-Device-Id", DEV)
    req.add_header("X-Request-Id", uuid.uuid4().hex)
    if data:
        req.add_header("Content-Type", "application/json")
    if token:
        req.add_header("Authorization", "Bearer " + token)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        raw = resp.read().decode("utf-8", "ignore")
    return json.loads(raw)


def req(method, path, body=None, token=None, params=None, retries=2):
    """按域名池顺序请求，成功即记住该域名"""
    order = ([GOOD[0]] + [b for b in BASES if b != GOOD[0]]) if GOOD[0] else BASES[:]
    last = None
    for attempt in range(retries):
        for base in order:
            try:
                out = _call(base, method, path, body, token, params)
                GOOD[0] = base
                return out
            except Exception as exc:
                last = exc
                if GOOD[0] == base:
                    GOOD[0] = None
    raise last or RuntimeError("所有域名均不可用")


def get_token(force=False):
    now = time.time()
    if not force and TOKEN[0] and now < TOKEN[1]:
        return TOKEN[0]
    ch = req("GET", "/api/v1/user/auto-register/challenge")["data"]["challengeId"]
    req("POST", "/api/v1/user/auto-register/interaction",
        {"challengeId": ch, "interactionType": "touch"})
    tok = req("POST", "/api/v1/user/auto-register",
              {"challengeId": ch, "interactionType": "touch"})["data"]["token"]
    TOKEN[0] = tok["accessToken"]
    TOKEN[1] = now + int(tok.get("expiresIn") or 43200) - 600
    return TOKEN[0]


def api(path, params=None, retries=3):
    err = None
    for i in range(retries):
        try:
            return req("GET", path, None, get_token(force=(i > 0)), params)
        except urllib.error.HTTPError as exc:
            err = exc
            if exc.code in (401, 403):
                TOKEN[0] = None
                continue
            time.sleep(0.5 * (i + 1))
        except Exception as exc:
            err = exc
            time.sleep(0.5 * (i + 1))
    raise err or RuntimeError("api 请求失败: %s" % path)


def to_vod(d, play=""):
    """把黄果的 drama 对象转成苹果CMS 的 vod 结构"""
    return {
        "vod_id": d.get("id"),
        "type_id": d.get("moduleId") or 0,
        "type_name": d.get("moduleName") or "",
        "vod_name": d.get("dramaName") or "",
        "vod_sub": d.get("dramaNo") or "",
        "vod_pic": d.get("coverUrl") or "",
        "vod_blurb": (d.get("description") or "")[:120],
        "vod_content": d.get("description") or "",
        "vod_remarks": ("全%s集" % d["episodeCount"]) if d.get("episodeCount") else "",
        "vod_serial": str(d.get("episodeCount") or 0),
        "vod_year": "2026",
        "vod_score": str(d.get("rating") or ""),
        "vod_play_from": "huangguo",
        "vod_play_url": play,
    }


def fetch_episodes(drama_id):
    """取一部剧的所有集播放地址，返回 '第1集$url#第2集$url'"""
    try:
        detail = api("/api/v1/drama/%s" % drama_id).get("data") or {}
    except Exception as exc:
        print("    详情失败 %s: %s" % (drama_id, exc))
        return "", detail_of(drama_id)

    eps = (detail.get("episodes") or [])[:MAX_EPS]
    if not eps:
        return "", detail.get("drama") or {}

    def one(ep):
        try:
            url = (api("/api/v1/drama/episode/%s" % ep["id"]).get("data") or {}).get("playUrl") or ""
            return (ep.get("episodeNo"), url)
        except Exception:
            return (ep.get("episodeNo"), "")

    # 集数内部再并发，4 路足够
    with ThreadPoolExecutor(max_workers=4) as pool:
        pairs = list(pool.map(one, eps))

    parts = ["第%s集$%s" % (no, url) for no, url in pairs if url]
    return "#".join(parts), detail.get("drama") or {}


def detail_of(drama_id):
    """失败时至少拿回基础信息"""
    try:
        return (api("/api/v1/drama/%s" % drama_id).get("data") or {}).get("drama") or {}
    except Exception:
        return {}


def collect_classes():
    out = []
    try:
        mods = api("/api/v1/drama/modules").get("data") or []
        for m in mods:
            if isinstance(m, dict) and str(m.get("id")) != "0":
                out.append({"type_id": m["id"], "type_name": m.get("moduleName") or ""})
    except Exception as exc:
        print("  分类获取失败:", exc)
    return out


def collect_items():
    """拉全部剧集。list 接口实测经常返回空，优先 home 栏目，再补 list 分页。"""
    items, seen = [], set()

    def push(it):
        if not isinstance(it, dict) or not it.get("id"):
            return 0
        if it["id"] in seen:
            return 0
        seen.add(it["id"])
        items.append(it)
        return 1

    try:
        home = api("/api/v1/drama/home").get("data") or {}
        cnt = 0
        for col in (home.get("columns") or []):
            for it in (col.get("items") or []):
                cnt += push(it)
        print("  home 栏目 -> %d 部" % cnt)
    except Exception as exc:
        print("  home 失败:", exc)

    page = 1
    while page <= 30:
        try:
            d = api("/api/v1/drama/list", {"page": page, "pageSize": 30}).get("data") or {}
            got = d.get("items") or []
        except Exception as exc:
            print("  list 第%d页失败: %s" % (page, exc))
            break
        if not got:
            break
        new = sum(push(it) for it in got)
        print("  list 第%d页 -> 新增 %d，累计 %d" % (page, new, len(items)))
        if new == 0:
            break
        page += 1

    return items


def main():
    out_path = sys.argv[1] if len(sys.argv) > 1 else os.path.join(
        os.path.dirname(os.path.abspath(__file__)), "..", "huangguo_cms.json")

    t0 = time.time()

    print("[1/4] 获取分类")
    classes = collect_classes()
    print("  分类:", [c["type_name"] for c in classes])

    print("[2/4] 获取剧集列表")
    items = collect_items()
    print("  共 %d 部" % len(items))
    if not items:
        print("[!] 一部都没取到，退出")
        return 1

    print("[3/4] 抓取播放地址（%d 部，并发 %d）" % (len(items), WORKERS))
    done = [0]
    lock_buf = []

    def work(it):
        play, drama = fetch_episodes(it["id"])
        done[0] += 1
        if done[0] % 5 == 0 or done[0] == len(items):
            print("  %d/%d（%.0fs）" % (done[0], len(items), time.time() - t0))
        return to_vod(drama if drama else it, play)

    with ThreadPoolExecutor(max_workers=WORKERS) as pool:
        vods = list(pool.map(work, items))

    ok = sum(1 for v in vods if v["vod_play_url"])
    print("[4/4] 有播放地址 %d/%d 部，用时 %.0f 秒" % (ok, len(vods), time.time() - t0))

    data = {
        "code": 1,
        "msg": "数据列表",
        "page": 1,
        "pagecount": 1,
        "limit": len(vods),
        "total": len(vods),
        "class": classes,
        "list": vods,
        "_meta": {
            "built_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "count": len(vods),
            "playable": ok,
            "source": "huangguo",
        },
    }

    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=1)
    size = os.path.getsize(out_path)
    print("已写入 %s（%.1f KB）" % (out_path, size / 1024.0))
    return 0


if __name__ == "__main__":
    sys.exit(main())
