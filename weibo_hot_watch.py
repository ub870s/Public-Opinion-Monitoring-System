# -*- coding: utf-8 -*-
"""每 5 分钟抓取一次微博热搜榜，实时输出日志并追加写入 CSV。

- 数据源：微博公开 JSON 接口 https://weibo.com/ajax/side/hotSearch（无需登录/无 Cookie）
- 输出：weibo_hot_log.csv（每轮追加，带抓取时间，可看历史变化）
         weibo_hot_watch.log（实时日志文件）
- 同时写入 SQLite（platform=weibo），与百度双源共用一张表
"""

import csv
import json
import os
import time
from datetime import datetime
from urllib.parse import quote
from urllib.request import Request, urlopen

import store

URL = "https://weibo.com/ajax/side/hotSearch"
INTERVAL = 300  # 秒，5 分钟
CSV_PATH = "weibo_hot_log.csv"
LOG_PATH = "weibo_hot_watch.log"
PLATFORM = "weibo"
LIMIT = 50
FIELDS = ["抓取时间", "排名", "标题", "热度", "热度变化", "简介", "链接", "图片"]
UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0 Safari/537.36 Edge/120.0"
)


def log(msg):
    line = "[{}] {}".format(datetime.now().strftime("%Y-%m-%d %H:%M:%S"), msg)
    print(line, flush=True)
    with open(LOG_PATH, "a", encoding="utf-8") as f:
        f.write(line + "\n")


def parse_api(data):
    """把接口返回的 realtime 数组转成统一的热搜条目格式。"""
    items = []
    for i, it in enumerate(data.get("data", {}).get("realtime", [])):
        word = (it.get("word") or "").strip()
        if not word:
            continue
        rank = it.get("realpos") or (i + 1)
        note = (it.get("note") or "").strip()
        items.append(
            {
                "排名": rank,
                "标题": word,
                "热度": it.get("num") or 0,
                # 微博不返回简介；note 与标题相同时视为无简介
                "简介": "" if note == word else note,
                "热度变化": "",
                "链接": "https://s.weibo.com/weibo?q=" + quote(word),
                "图片": "",
            }
        )
        if len(items) >= LIMIT:
            break
    return items


def fetch():
    req = Request(
        URL,
        headers={
            "User-Agent": UA,
            "Accept": "application/json, text/plain, */*",
            "Referer": "https://weibo.com/",
        },
    )
    with urlopen(req, timeout=30) as r:
        return json.loads(r.read().decode("utf-8", "ignore"))


def append_csv(items, ts):
    exists = os.path.exists(CSV_PATH)
    with open(CSV_PATH, "a", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=FIELDS)
        if not exists:
            writer.writeheader()
        for it in items:
            row = {"抓取时间": ts}
            row.update(it)
            writer.writerow(row)


def main():
    log("=== 启动：微博热搜抓取，间隔 {} 秒 ===".format(INTERVAL))
    round_no = 0
    while True:
        round_no += 1
        ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        log("第 {} 轮开始抓取…".format(round_no))
        try:
            items = parse_api(fetch())
            if not items:
                log("第 {} 轮失败：接口返回空数据".format(round_no))
            else:
                append_csv(items, ts)
                added = store.insert_items(items, ts, PLATFORM)
                log("第 {} 轮成功：{} 条，已追加到 {}（入库新增 {} 条）".format(
                    round_no, len(items), CSV_PATH, added))
                top = items[0]
                log("  当前第一：{}（热度 {}）".format(top["标题"], top["热度"]))
        except Exception as e:
            log("第 {} 轮异常：{}".format(round_no, e))
        log("等待 {} 秒后进入下一轮…".format(INTERVAL))
        time.sleep(INTERVAL)


if __name__ == "__main__":
    main()
