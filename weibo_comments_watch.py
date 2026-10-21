# -*- coding: utf-8 -*-
"""每 5 分钟抓取一次微博热搜话题的热门评论（游客模式，无需登录）。

- 链路：Playwright 无头 Edge → m.weibo.cn（访问首页自动种游客 Cookie）
    1) /api/container/getIndex?containerid=100103type=1&q=<词>   话题下的微博列表
    2) 选评论数最高的一条微博
    3) /comments/hotflow?id=<mid>&mid=<mid>                      热门评论
- 入库：SQLite comments 表（platform=weibo，topic=热搜词），每条评论带情感标签
- 日志：weibo_comments_watch.log
"""

import json
import re
import time
from datetime import datetime
from urllib.parse import quote

from playwright.sync_api import sync_playwright

import store

INTERVAL = 300  # 秒，5 分钟
TOP_N = 10           # 每轮覆盖微博热搜前 N 个话题
COMMENTS_PER_TOPIC = 15
LOG_PATH = "weibo_comments_watch.log"
UA_M = (
    "Mozilla/5.0 (iPhone; CPU iPhone OS 16_0 like Mac OS X) AppleWebKit/605.1.15 "
    "(KHTML, like Gecko) Version/16.0 Mobile/15E148 Safari/604.1"
)

TAG_RE = re.compile(r"<[^>]+>")


def log(msg):
    line = "[{}] {}".format(datetime.now().strftime("%Y-%m-%d %H:%M:%S"), msg)
    print(line, flush=True)
    with open(LOG_PATH, "a", encoding="utf-8") as f:
        f.write(line + "\n")


def clean_text(s):
    """去掉评论里的表情图标/链接等 HTML 标签。"""
    s = TAG_RE.sub("", s or "")
    return re.sub(r"\s+", " ", s).strip()


def latest_topics(platform="weibo", limit=TOP_N):
    """从库里取某平台最新一轮的热搜词。"""
    rows = store.load_rows(platform)
    times = sorted({r["抓取时间"] for r in rows if r["抓取时间"]})
    if not times:
        return []
    latest = times[-1]
    out, seen = [], set()
    for r in rows:
        if r["抓取时间"] != latest:
            continue
        t = r["标题"]
        if not t or t in seen:
            continue
        seen.add(t)
        out.append(t)
        if len(out) >= limit:
            break
    return out


def get_json(page, url, wait=2, retries=2):
    """访问接口页并解析 JSON；偶发非 JSON（游客 Cookie 未就绪/风控）时重试。"""
    last = ""
    for i in range(retries + 1):
        page.goto(url, wait_until="domcontentloaded", timeout=30000)
        time.sleep(wait)
        try:
            return json.loads(page.evaluate("() => document.body.innerText"))
        except Exception:
            last = (page.evaluate("() => document.body.innerText") or "")[:80]
            time.sleep(3)
    raise RuntimeError("接口返回非 JSON：{}".format(last))


def search_topic(page, word):
    """搜话题 → 返回 [{mid, comments_count}]，按评论数热排。"""
    url = "https://m.weibo.cn/api/container/getIndex?containerid=" + quote(
        "100103type=1&q=" + word
    )
    d = get_json(page, url)
    posts = []
    for card in (d.get("data") or {}).get("cards") or []:
        for g in (card.get("card_group") or [card]):
            mb = g.get("mblog") or {}
            if mb.get("id"):
                posts.append(
                    {"mid": mb["id"], "comments_count": mb.get("comments_count") or 0}
                )
    return posts


def hot_comments(page, mid):
    """取一条微博的热门评论。"""
    url = "https://m.weibo.cn/comments/hotflow?id={}&mid={}&max_id_type=0".format(mid, mid)
    d = get_json(page, url)
    out = []
    for c in ((d.get("data") or {}).get("data") or [])[:COMMENTS_PER_TOPIC]:
        text = clean_text(c.get("text"))
        if not text:
            continue
        out.append(
            {
                "mid": mid,
                "user": (c.get("user") or {}).get("screen_name", "") or "匿名",
                "text": text,
                "likes": c.get("like_count") or 0,
            }
        )
    return out


def main():
    log("=== 启动：微博热评抓取，间隔 {} 秒，每轮覆盖 Top {} 话题 ===".format(INTERVAL, TOP_N))
    round_no = 0
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True, channel="msedge")
        ctx = browser.new_context(
            user_agent=UA_M, viewport={"width": 390, "height": 844}
        )
        page = ctx.new_page()

        # 先访问首页种游客 Cookie
        try:
            page.goto("https://m.weibo.cn/", wait_until="domcontentloaded", timeout=30000)
            time.sleep(5)
            log("游客 Cookie 就绪（{} 条）".format(len(ctx.cookies("https://m.weibo.cn"))))
        except Exception as e:
            log("首页种 Cookie 异常（继续）：{}".format(e))

        while True:
            round_no += 1
            log("第 {} 轮开始抓取…".format(round_no))
            added_total = 0
            try:
                topics = latest_topics()
                if not topics:
                    log("第 {} 轮跳过：库里还没有微博热搜数据".format(round_no))
                for word in topics:
                    try:
                        posts = search_topic(page, word)
                        if not posts:
                            log("  「{}」未搜到微博".format(word))
                            continue
                        best = max(posts, key=lambda x: x["comments_count"] or 0)
                        cs = hot_comments(page, best["mid"])
                        if cs:
                            ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                            added = store.insert_comments(cs, ts, word, "weibo")
                            added_total += added
                            log("  「{}」取到 {} 条热评（入库新增 {}，来源微博评论数 {}）".format(
                                word, len(cs), added, best["comments_count"]))
                        else:
                            log("  「{}」该微博暂无热评".format(word))
                        time.sleep(2)  # 温和抓取
                    except Exception as e:
                        log("  「{}」抓取异常：{}".format(word, e))
                        time.sleep(2)
                log("第 {} 轮完成：共入库新增 {} 条评论".format(round_no, added_total))
            except Exception as e:
                log("第 {} 轮异常：{}".format(round_no, e))
            log("等待 {} 秒后进入下一轮…".format(INTERVAL))
            time.sleep(INTERVAL)


if __name__ == "__main__":
    main()
