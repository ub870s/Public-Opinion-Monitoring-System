# -*- coding: utf-8 -*-
"""后台告警调度：定时扫描最新一轮，命中指定级别的新告警就推送一次。

启动方式：app.py 启动时调用 start()，起一个守护线程，随主进程一起退出。
去重：以「标题 + 级别 + 说明」为键，同一告警只推一次；说明变化（排名继续上升）会再推。
失败重试：推送失败时最多重试 3 次，仍失败则放弃。"""

import threading
import time
from collections import Counter, defaultdict
from datetime import datetime

import analysis
import push
import store

DEFAULT_INTERVAL = 60  # 秒
_pushed = set()        # 已成功推送或已放弃的告警键 (标题, 级别, 说明)
_failed = Counter()    # 推送失败计数，同一告警最多重试 3 次
_lock = threading.Lock()
MAX_RETRY = 3
_started = False


def split_batch(rows):
    """取每个来源各自的最新一轮（合并）与上一轮。与 app.latest_batch 等价。"""
    by_plat = defaultdict(list)
    for r in rows:
        by_plat[r.get("平台") or "baidu"].append(r)

    batch, prev, latest = [], [], None
    for items in by_plat.values():
        times = sorted({x["_time"] for x in items if x["_time"]})
        if not times:
            continue
        t = times[-1]
        if latest is None or t > latest:
            latest = t
        batch.extend([x for x in items if x["_time"] == t])
        if len(times) >= 2:
            prev.extend([x for x in items if x["_time"] == times[-2]])
    return batch, prev, latest


def format_message(alerts):
    """把多条告警拼成一条推送正文。"""
    lines = []
    for a in alerts:
        lines.append(
            "【{}】{}（{}｜第 {} 名）\n{}".format(
                a["级别"],
                a["标题"],
                {"baidu": "百度", "weibo": "微博", "douyin": "抖音",
                 "bilibili": "B站", "toutiao": "头条"}.get(a["平台"], a["平台"]),
                a["排名"] or "—",
                a["说明"],
            )
        )
    return "\n\n".join(lines)


def scan_once(cfg=None):
    """扫描一次。返回本次实际推送的告警条数。"""
    cfg = cfg or store.get_settings()
    if cfg.get("push_enabled") != "1":
        return 0
    webhook = (cfg.get("push_webhook") or "").strip()
    if not webhook:
        return 0

    levels = {x.strip() for x in (cfg.get("push_levels") or "危险").split(",") if x.strip()}
    if not levels:
        levels = {"危险"}

    rows = store.load_rows()
    batch, prev, latest = split_batch(rows)
    if not batch or latest is None:
        return 0

    batch_key = latest.strftime("%Y-%m-%d %H:%M:%S")
    alerts = analysis.detect_alerts(batch, prev, store.watch_keywords())
    hits = [a for a in alerts if a["级别"] in levels]
    hits = [a for a in hits if a["标题"]]
    if not hits:
        return 0

    with _lock:
        # 去重键不含批次：同一话题持续在榜时，只有「说明」变化（如排名继续上升）才会再推
        fresh = [a for a in hits if (a["标题"], a["级别"], a["说明"]) not in _pushed]
        if not fresh:
            return 0

        title = "热搜预警：{} 条{}级告警（{}）".format(len(fresh), "/".join(sorted(levels)), batch_key)
        text = format_message(fresh)
        ok, msg = push.send(cfg.get("push_channel") or "generic", webhook, title, text)

        channel = push.CHANNEL_NAMES.get(cfg.get("push_channel") or "generic", "通用 Webhook")
        for a in fresh:
            key = (a["标题"], a["级别"], a["说明"])
            store.log_push(channel, a["级别"], a["标题"], a["说明"], ok, msg)
            if ok:
                _pushed.add(key)
            else:
                _failed[key] += 1
                if _failed[key] >= MAX_RETRY:
                    _pushed.add(key)  # 重试多次仍失败，放弃该告警，避免持续轰炸
        # 防止长期运行内存膨胀
        if len(_pushed) > 2000:
            _pushed.clear()
            _failed.clear()
        return len(fresh) if ok else 0


def _loop():
    while True:
        interval = DEFAULT_INTERVAL
        try:
            cfg = store.get_settings()
            try:
                interval = max(30, int(cfg.get("push_interval") or DEFAULT_INTERVAL))
            except ValueError:
                interval = DEFAULT_INTERVAL
            scan_once(cfg)
        except Exception as e:
            print("[push-worker] 扫描异常：{}".format(e), flush=True)
        time.sleep(interval)


def start():
    """启动守护线程（重复调用只启动一次）。"""
    global _started
    if _started:
        return
    _started = True
    t = threading.Thread(target=_loop, name="push-worker", daemon=True)
    t.start()
    print("[push-worker] 已启动，间隔 {} 秒".format(DEFAULT_INTERVAL), flush=True)
