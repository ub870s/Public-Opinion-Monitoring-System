# -*- coding: utf-8 -*-
"""抓取百度热搜榜（https://top.baidu.com/board）并保存为 JSON/CSV。"""

import csv
import json
import urllib.request

API = "https://top.baidu.com/api/board?platform=pc&tab=realtime"
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0 Safari/537.36"
    ),
    "Referer": "https://top.baidu.com/board?platform=pc&sa=pcindex_entry",
}


def fetch():
    req = urllib.request.Request(API, headers=HEADERS)
    with urllib.request.urlopen(req, timeout=20) as resp:
        return json.loads(resp.read().decode("utf-8"))


def parse(data):
    items = []
    for card in data["data"]["cards"]:
        for it in card.get("content", []):
            items.append(
                {
                    "排名": it.get("index", 0) + 1,
                    "标题": it.get("query", ""),
                    "热度": it.get("hotScore", ""),
                    "热度变化": it.get("hotChange", ""),
                    "简介": it.get("desc", ""),
                    "链接": it.get("url", ""),
                    "图片": it.get("img", ""),
                }
            )
    return items


def save_json(items, path="baidu_hot.json"):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(items, f, ensure_ascii=False, indent=2)


def save_csv(items, path="baidu_hot.csv"):
    fields = ["排名", "标题", "热度", "热度变化", "简介", "链接", "图片"]
    with open(path, "w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fields)
        writer.writeheader()
        writer.writerows(items)


if __name__ == "__main__":
    data = fetch()
    hot = parse(data)
    save_json(hot)
    save_csv(hot)
    for it in hot:
        print(f"{it['排名']:>2}. {it['标题']}  (热度 {it['热度']})")
    print(f"\n共 {len(hot)} 条，已保存到 baidu_hot.json / baidu_hot.csv")
