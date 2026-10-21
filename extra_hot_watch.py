# -*- coding: utf-8 -*-
"""每 5 分钟抓取一次抖音 / B站 / 今日头条 热搜榜，写入 CSV 与 SQLite。

- 三个源均为公开接口，无需登录、无需 Cookie
- 每个源独立写入自己的 CSV（douyin_hot_log.csv / bilibili_hot_log.csv / toutiao_hot_log.csv）
- 统一入库到 hot_items，platform 字段区分来源
"""

import csv
import json
import os
import time
from datetime import datetime
from urllib.parse import quote
from urllib.request import Request, urlopen

import store

INTERVAL = 300  # 秒，5 分钟
LOG_PATH = "extra_hot_watch.log"
FIELDS = ["抓取时间", "排名", "标题", "热度", "热度变化", "简介", "链接", "图片"]
UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)


def log(msg):
    line = "[{}] {}".format(datetime.now().strftime("%Y-%m-%d %H:%M:%S"), msg)
    print(line, flush=True)
    with open(LOG_PATH, "a", encoding="utf-8") as f:
        f.write(line + "\n")


def fetch(url, referer):
    req = Request(
        url,
        headers={
            "User-Agent": UA,
            "Accept": "application/json, text/plain, */*",
            "Referer": referer,
        },
    )
    with urlopen(req, timeout=25) as r:
        return json.loads(r.read().decode("utf-8", "ignore"))


def make_item(rank, title, hot, link="", img="", summary=""):
    return {
        "排名": rank,
        "标题": str(title or "").strip(),
        "热度": hot or 0,
        "热度变化": "",
        "简介": str(summary or "").strip(),
        "链接": link,
        "图片": img,
    }


# ---------------- 各源解析 ----------------

def fetch_douyin():
    """抖音热搜榜（iesdouyin 公开榜单接口）：word / hot_value / label。"""
    d = fetch(
        "https://www.iesdouyin.com/web/api/v2/hotsearch/billboard/word/",
        "https://www.douyin.com/",
    )
    items = []
    for i, it in enumerate(d.get("word_list") or []):
        word = it.get("word") or ""
        if not word:
            continue
        label = it.get("label")
        items.append(
            make_item(
                i + 1,
                word,
                it.get("hot_value"),
                "https://www.douyin.com/search/" + quote(word),
                summary=label if isinstance(label, str) else "",
            )
        )
    return items


def fetch_bilibili():
    """B站热搜词：keyword / heat_score。"""
    d = fetch("https://s.search.bilibili.com/main/hotword", "https://www.bilibili.com/")
    items = []
    for i, it in enumerate(d.get("list") or []):
        kw = it.get("keyword") or ""
        if not kw:
            continue
        items.append(
            make_item(
                i + 1,
                kw,
                it.get("heat_score"),
                "https://search.bilibili.com/all?keyword=" + quote(kw),
            )
        )
    return items


def fetch_toutiao():
    """今日头条热榜：Title / HotValue / Url / Image。"""
    d = fetch(
        "https://www.toutiao.com/hot-event/hot-board/?origin=toutiao_pc",
        "https://www.toutiao.com/",
    )
    items = []
    for i, it in enumerate(d.get("data") or []):
        title = it.get("Title") or ""
        if not title:
            continue
        items.append(
            make_item(
                i + 1,
                title,
                it.get("HotValue"),
                it.get("Url") or "",
                it.get("Image") or "",
            )
        )
    return items


SOURCES = (
    ("douyin", "抖音", fetch_douyin, "douyin_hot_log.csv"),
    ("bilibili", "B站", fetch_bilibili, "bilibili_hot_log.csv"),
    ("toutiao", "头条", fetch_toutiao, "toutiao_hot_log.csv"),
)


def append_csv(items, ts, path):
    exists = os.path.exists(path)
    with open(path, "a", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=FIELDS)
        if not exists:
            writer.writeheader()
        for it in items:
            row = {"抓取时间": ts}
            row.update(it)
            writer.writerow(row)


def main():
    log("=== 启动：多源热搜抓取（抖音 / B站 / 头条），间隔 {} 秒 ===".format(INTERVAL))
    round_no = 0
    while True:
        round_no += 1
        ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        log("第 {} 轮开始抓取…".format(round_no))
        for platform, name, fn, csv_path in SOURCES:
            try:
                items = fn()
                if not items:
                    log("  {} 本轮无数据".format(name))
                    continue
                append_csv(items, ts, csv_path)
                added = store.insert_items(items, ts, platform)
                log("  {} 成功：{} 条（入库新增 {}），第一：{}".format(
                    name, len(items), added, items[0]["标题"]))
            except Exception as e:
                log("  {} 抓取异常：{}".format(name, e))
            time.sleep(1)
        log("第 {} 轮完成".format(round_no))
        log("等待 {} 秒后进入下一轮…".format(INTERVAL))
        time.sleep(INTERVAL)


if __name__ == "__main__":
    main()
