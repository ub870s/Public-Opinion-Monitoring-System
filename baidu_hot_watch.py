# -*- coding: utf-8 -*-
"""每 5 分钟用无头浏览器抓取一次百度热搜榜，实时输出日志并追加写入 CSV。

- 浏览器：Playwright + 系统自带 Edge（headless）
- 抽取：优先拦截页面自身请求的 /api/board 接口响应；失败则退回 DOM 解析
- 输出：baidu_hot_log.csv（每轮追加，带抓取时间，可看历史变化）
         baidu_hot_watch.log（实时日志文件）
"""

import csv
import json
import os
import time
from datetime import datetime

from playwright.sync_api import sync_playwright

import store

URL = "https://top.baidu.com/board?platform=pc&sa=pcindex_entry"
INTERVAL = 300  # 秒，5 分钟
CSV_PATH = "baidu_hot_log.csv"
LOG_PATH = "baidu_hot_watch.log"
PLATFORM = "baidu"
FIELDS = ["抓取时间", "排名", "标题", "热度", "热度变化", "简介", "链接", "图片"]


def log(msg):
    line = "[{}] {}".format(datetime.now().strftime("%Y-%m-%d %H:%M:%S"), msg)
    print(line, flush=True)
    with open(LOG_PATH, "a", encoding="utf-8") as f:
        f.write(line + "\n")


def parse_api(data):
    items = []
    for card in data.get("data", {}).get("cards", []):
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


# 主抽取方式：在页面内用同源 fetch 调用它所依赖的接口，浏览器自带 Cookie/头部
FETCH_JS = """
async () => {
  const r = await fetch('/api/board?platform=pc&tab=realtime',
      {headers: {'Accept': 'application/json'}});
  return await r.json();
}
"""

# DOM 兜底：直接读取页面上已渲染的榜单条目
DOM_JS = """
() => {
  const titles = document.querySelectorAll('.c-single-text-ellipsis');
  const out = [];
  titles.forEach((t, i) => {
    const row = t.closest('a') ? t.closest('a') : t.parentElement.parentElement.parentElement;
    const hot = row ? row.querySelector('[class*="hot-index"]') : null;
    const desc = row ? row.querySelector('[class*="hot-desc"]') : null;
    const a = t.closest('a') || (row ? row.querySelector('a[href]') : null);
    out.push({
      排名: i + 1,
      标题: t.innerText.trim(),
      热度: hot ? hot.innerText.trim() : "",
      热度变化: "",
      简介: desc ? desc.innerText.trim() : "",
      链接: a ? a.href : "",
      图片: "",
    });
  });
  return out;
}
"""


def scrape(page):
    page.goto(URL, wait_until="domcontentloaded", timeout=60000)
    page.wait_for_timeout(3000)

    # 判断是否需要登录 / 是否被安全验证拦截
    final_url = page.url
    if "wappass.baidu.com" in final_url or "passport" in final_url:
        return None, "被重定向到安全验证/登录页：{}".format(final_url)

    items = []
    try:
        items = parse_api(page.evaluate(FETCH_JS))
    except Exception as e:
        log("  页内接口调用失败，改用 DOM 解析：{}".format(e))
    if not items:
        items = page.evaluate(DOM_JS)
    return items, final_url


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
    log("=== 启动：无头浏览器抓取百度热搜，间隔 {} 秒 ===".format(INTERVAL))
    with sync_playwright() as p:
        browser = p.chromium.launch(channel="msedge", headless=True)
        log("已启动无头浏览器：Edge (headless)")
        context = browser.new_context(
            locale="zh-CN",
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/120.0 Safari/537.36 Edge/120.0"
            ),
        )
        round_no = 0
        while True:
            round_no += 1
            ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            log("第 {} 轮开始抓取…".format(round_no))
            page = context.new_page()
            try:
                items, info = scrape(page)
                if items is None:
                    log("第 {} 轮失败：{}".format(round_no, info))
                else:
                    append_csv(items, ts)
                    added = store.insert_items(items, ts, PLATFORM)
                    log("第 {} 轮成功：{} 条，已追加到 {}（入库新增 {} 条）".format(
                        round_no, len(items), CSV_PATH, added))
                    if items:
                        top = items[0]
                        log("  当前第一：{}（热度 {}）".format(top["标题"], top["热度"]))
            except Exception as e:
                log("第 {} 轮异常：{}".format(round_no, e))
            finally:
                page.close()
            log("等待 {} 秒后进入下一轮…".format(INTERVAL))
            time.sleep(INTERVAL)


if __name__ == "__main__":
    main()
