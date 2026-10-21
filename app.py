# -*- coding: utf-8 -*-
"""全网热搜 · 实时舆情监测站（Flask）

页面：
  /            实时榜：最新一轮 Top 50，前端自动刷新
  /dashboard   数据看板：热度分布、趋势、在榜时长、情感占比
  /cloud       词云监测：按热度加权、按情感着色、可只看正面/负面
  /search      筛选查询：时间范围 / 关键词 / 排名 / 情感筛选
  /detail      单条热搜历史轨迹 + AI 解读
  /ai          AI 研判：事件聚类、智能简报、问答助手
接口：
  /api/latest            最新一轮数据 JSON
  /api/cloud             词云数据（词表 + 悬浮定位框）
  /cloud.png             词云图片
  /api/ai/*              大模型相关接口

数据源：SQLite（hot.db，由爬虫每 5 分钟写入，历史 CSV 已迁移入库）
"""

import colorsys
import json
import os
import random
import secrets
import threading
from collections import defaultdict
from datetime import datetime, timedelta
from functools import wraps
from io import BytesIO
from urllib.parse import quote, urlencode

from flask import Flask, Response, g, jsonify, redirect, render_template, request, session
from PIL import ImageFont
from wordcloud import WordCloud

import analysis
import llm
import push
import push_worker
import store

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(BASE_DIR, "config.json")


def load_config():
    """读取 config.json；不存在则生成默认配置（默认管理员 admin / admin123）。"""
    cfg = {}
    if os.path.exists(CONFIG_PATH):
        try:
            with open(CONFIG_PATH, "r", encoding="utf-8") as f:
                cfg = json.load(f)
        except Exception:
            cfg = {}
    if not cfg.get("admin_password"):
        cfg.setdefault("admin_user", "admin")
        cfg.setdefault("admin_password", "admin123")
        cfg.setdefault("secret_key", secrets.token_hex(32))
        with open(CONFIG_PATH, "w", encoding="utf-8") as f:
            json.dump(cfg, f, ensure_ascii=False, indent=2)
    if not cfg.get("secret_key"):
        cfg["secret_key"] = secrets.token_hex(32)
    return cfg


CONFIG = load_config()

app = Flask(__name__)
app.secret_key = CONFIG["secret_key"]

PLATFORMS = ("baidu", "weibo", "douyin", "bilibili", "toutiao")
PLATFORM_NAMES = {
    "baidu": "百度",
    "weibo": "微博",
    "douyin": "抖音",
    "bilibili": "B站",
    "toutiao": "头条",
}
PLATFORM_TABS = (
    ("", "全部来源"),
    ("baidu", "百度"),
    ("weibo", "微博"),
    ("douyin", "抖音"),
    ("bilibili", "B站"),
    ("toutiao", "头条"),
)


def platform_name(value):
    return PLATFORM_NAMES.get(value or "baidu", value or "百度")


def norm_platform(value):
    value = (value or "").strip()
    return value if value in PLATFORMS else ""


app.jinja_env.globals["platform_name"] = platform_name


def plat_links(path, current, params):
    """生成「全部 / 百度 / 微博 / 抖音 / B站 / 头条」来源切换链接，保留当前页面的其它筛选参数。"""
    counts = store.platform_counts()
    total = sum(counts.values())
    out = []
    for key, label in PLATFORM_TABS:
        p = dict(params)
        if key:
            p["platform"] = key
        else:
            p.pop("platform", None)
        qs = urlencode([(k, v) for k, v in p.items() if v not in (None, "", False)])
        out.append(
            {
                "key": key,
                "label": label,
                "count": counts.get(key, 0) if key else total,
                "url": path + ("?" + qs if qs else ""),
                "active": current == key,
            }
        )
    return out

DT_FORMATS = ("%Y-%m-%dT%H:%M", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M")


def parse_dt(value):
    """解析前端 datetime-local 的输入。"""
    if not value:
        return None
    for fmt in DT_FORMATS:
        try:
            return datetime.strptime(value.strip(), fmt)
        except ValueError:
            continue
    return None


def to_int(value, default=0):
    try:
        return int(str(value).strip())
    except (ValueError, TypeError):
        return default


def load_rows(platform=None):
    return store.load_rows(platform or None)


def latest_batch(rows):
    """最新一轮。多来源时取每个来源各自的最新一轮再合并。"""
    if not rows:
        return [], None
    by_plat = defaultdict(list)
    for r in rows:
        by_plat[r.get("平台") or "baidu"].append(r)
    batch, latest = [], None
    for items in by_plat.values():
        times = [x["_time"] for x in items if x["_time"]]
        if not times:
            continue
        t = max(times)
        if latest is None or t > latest:
            latest = t
        batch.extend([x for x in items if x["_time"] == t])
    batch.sort(key=lambda x: (x.get("平台") or "", x["_rank"]))
    return batch, latest


def unique_rows(rows):
    """按标题去重，保留首次出现的那条。"""
    seen, out = set(), []
    for r in rows:
        t = r.get("标题", "")
        if not t or t in seen:
            continue
        seen.add(t)
        out.append(r)
    return out


def select_rows(rng, platform=""):
    """按范围取数：latest=最新一轮 / hour=最近 1 小时 / all=全部。"""
    rows = store.load_rows(platform or None)
    times = sorted({r["_time"] for r in rows if r["_time"]})
    if rng == "all":
        return rows, "全部数据"
    if rng == "hour" and times:
        cutoff = times[-1] - timedelta(minutes=60)
        return [r for r in rows if r["_time"] and r["_time"] >= cutoff], "最近 1 小时"
    if times:
        batch, _latest = latest_batch(rows)
        return batch, "最新一轮"
    return rows, "暂无数据"


def previous_batch(rows):
    """上一轮：多来源时取每个来源各自的上一轮再合并。"""
    by_plat = defaultdict(list)
    for r in rows:
        by_plat[r.get("平台") or "baidu"].append(r)
    out = []
    for items in by_plat.values():
        times = sorted({x["_time"] for x in items if x["_time"]})
        if len(times) >= 2:
            out.extend([x for x in items if x["_time"] == times[-2]])
    return out


def site_stats(rows):
    times = sorted({r["_time"] for r in rows if r["_time"]})
    return {
        "rounds": len(times),
        "records": len(rows),
        "first": times[0].strftime("%Y-%m-%d %H:%M:%S") if times else "—",
        "last": times[-1].strftime("%Y-%m-%d %H:%M:%S") if times else "—",
    }


def apply_filters(rows, start, end, q, rank_min, rank_max, dedupe, sort_by, mood=None):
    q_lower = q.lower().strip()
    result = []
    for r in rows:
        t = r["_time"]
        if start and (t is None or t < start):
            continue
        if end and (t is None or t > end):
            continue
        if q_lower:
            text = "{} {}".format(r.get("标题", ""), r.get("简介", "")).lower()
            if q_lower not in text:
                continue
        if mood and r.get("情感") != mood:
            continue
        if rank_min is not None and r["_rank"] < rank_min:
            continue
        if rank_max is not None and r["_rank"] > rank_max:
            continue
        result.append(r)

    if sort_by == "hot":
        result.sort(key=lambda x: x["_hot"], reverse=True)
    elif sort_by == "rank":
        result.sort(key=lambda x: (x["_rank"], x["_time"] or datetime.min))
    else:
        result.sort(key=lambda x: x["_time"] or datetime.min, reverse=True)

    if dedupe:
        seen, unique = set(), []
        for r in result:
            key = r.get("标题", "")
            if key in seen:
                continue
            seen.add(key)
            unique.append(r)
        result = unique

    return result


def row_to_dict(r):
    return {
        "抓取时间": r.get("抓取时间", ""),
        "排名": r["_rank"],
        "标题": r.get("标题", ""),
        "热度": r["_hot"],
        "简介": r.get("简介", ""),
        "链接": r.get("链接", ""),
        "图片": r.get("图片", ""),
        "情感": r.get("情感", "中性"),
        "情感分": r.get("情感分", 0.5),
        "平台": r.get("平台", "baidu"),
    }


# ---------------- 用户权限（登录 + 角色） ----------------

@app.before_request
def load_current_user():
    g.user = session.get("user")
    g.role = session.get("role")
    g.is_admin = g.role == "admin"


@app.context_processor
def inject_current_user():
    return {
        "current_user": getattr(g, "user", None),
        "current_role": getattr(g, "role", None),
        "is_admin": getattr(g, "is_admin", False),
        "PLATFORM_NAMES": PLATFORM_NAMES,
    }


def admin_required(fn):
    @wraps(fn)
    def wrapper(*args, **kwargs):
        if not getattr(g, "is_admin", False):
            return jsonify({"ok": False, "error": "需要管理员权限，请先登录"}), 403
        return fn(*args, **kwargs)
    return wrapper


@app.route("/login", methods=["GET", "POST"])
def login():
    error = ""
    if request.method == "POST":
        u = (request.form.get("username") or "").strip()
        p = request.form.get("password") or ""
        if u == CONFIG.get("admin_user", "admin") and p == CONFIG.get("admin_password", ""):
            session["user"] = u
            session["role"] = "admin"
            return redirect(request.args.get("next") or "/")
        error = "用户名或密码不对，请重试"
    return render_template("login.html", error=error)


@app.route("/logout")
def logout():
    session.clear()
    return redirect("/")


# ---------------- 页面路由 ----------------

@app.route("/")
def index():
    platform = norm_platform(request.args.get("platform"))
    rows = load_rows(platform)
    batch, latest = latest_batch(rows)
    return render_template(
        "index.html",
        rows=batch,
        latest=latest.strftime("%Y-%m-%d %H:%M:%S") if latest else "暂无数据",
        stats=site_stats(rows),
        mood=store.sentiment_counts(batch),
        platform=platform,
        plat_links=plat_links("/", platform, {}),
    )


@app.route("/search")
def search():
    raw_start = request.args.get("start", "").strip()
    raw_end = request.args.get("end", "").strip()
    q = request.args.get("q", "").strip()
    mood = request.args.get("mood", "").strip()
    platform = norm_platform(request.args.get("platform"))
    rank_min = request.args.get("rank_min", "").strip()
    rank_max = request.args.get("rank_max", "").strip()
    dedupe = request.args.get("dedupe", "1") == "1"
    sort_by = request.args.get("sort", "time")

    rows = apply_filters(
        load_rows(platform),
        start=parse_dt(raw_start),
        end=parse_dt(raw_end),
        q=q,
        rank_min=to_int(rank_min, None) if rank_min else None,
        rank_max=to_int(rank_max, None) if rank_max else None,
        dedupe=dedupe,
        sort_by=sort_by,
        mood=mood,
    )
    return render_template(
        "search.html",
        rows=rows,
        total=len(rows),
        start=raw_start,
        end=raw_end,
        q=q,
        mood=mood,
        platform=platform,
        plat_links=plat_links(
            "/search",
            platform,
            {
                "start": raw_start,
                "end": raw_end,
                "q": q,
                "mood": mood,
                "rank_min": rank_min,
                "rank_max": rank_max,
                "dedupe": "1" if dedupe else "",
                "sort": sort_by,
            },
        ),
        rank_min=rank_min,
        rank_max=rank_max,
        dedupe=dedupe,
        sort_by=sort_by,
    )


@app.route("/detail")
def detail():
    title = request.args.get("title", "").strip()
    rows = [r for r in load_rows() if r.get("标题", "") == title]
    rows.sort(key=lambda x: x["_time"] or datetime.min)

    timeline = []
    prev_hot = None
    for r in rows:
        delta = None if prev_hot is None else r["_hot"] - prev_hot
        timeline.append(
            {
                "时间": r.get("抓取时间", ""),
                "排名": r["_rank"],
                "热度": r["_hot"],
                "变化": delta,
                "简介": r.get("简介", ""),
                "链接": r.get("链接", ""),
                "情感": r.get("情感", "中性"),
                "情感分": r.get("情感分", 0.5),
                "平台": r.get("平台", "baidu"),
            }
        )
        prev_hot = r["_hot"]

    best = min([r["_rank"] for r in rows], default=None)
    peak = max([r["_hot"] for r in rows], default=0)
    max_hot = peak or 1

    # 传播溯源：分平台统计首现/最近/轮次/名次轨迹
    by_plat = defaultdict(list)
    for r in rows:
        by_plat[r.get("平台") or "baidu"].append(r)
    trace = []
    for plat, items in by_plat.items():
        items.sort(key=lambda x: x["_time"] or datetime.min)
        ranks = [x["_rank"] for x in items if x["_rank"]]
        trace.append(
            {
                "平台": plat,
                "首现": items[0].get("抓取时间", ""),
                "最近": items[-1].get("抓取时间", ""),
                "轮次": len(items),
                "最好名次": min(ranks) if ranks else None,
                "当前排名": items[-1]["_rank"],
                "当前热度": items[-1]["_hot"],
                "情感": items[-1].get("情感", "中性"),
            }
        )
    trace.sort(key=lambda x: x["首现"])
    first_plat = trace[0]["平台"] if trace else None
    cross_gap = None
    if len(trace) >= 2:
        t0 = store.parse_dt(trace[0]["首现"])
        t1 = store.parse_dt(trace[1]["首现"])
        if t0 and t1 and t1 > t0:
            cross_gap = {
                "from": trace[0]["平台"],
                "to": trace[1]["平台"],
                "minutes": int((t1 - t0).total_seconds() // 60),
            }

    return render_template(
        "detail.html",
        title=title,
        timeline=timeline,
        latest=timeline[-1] if timeline else None,
        best_rank=best,
        peak=peak,
        max_hot=max_hot,
        appears=len(rows),
        ai_ready=llm.is_configured(),
        trace=trace,
        first_plat=first_plat,
        cross_gap=cross_gap,
    )


@app.route("/api/comments")
def api_comments():
    """某话题最新一轮的微博热评 + 情感计数。"""
    title = request.args.get("title", "").strip()
    if not title:
        return jsonify({"ok": False, "error": "缺少 title 参数"})
    items, mood, ts = store.comments_for_topic(title)
    return jsonify({"ok": True, "count": len(items), "mood": mood, "batch": ts, "comments": items})


@app.route("/api/trace")
def api_trace():
    """某标题的跨平台溯源 JSON（供前端或其他工具调用）。"""
    title = request.args.get("title", "").strip()
    if not title:
        return jsonify({"ok": False, "error": "缺少 title 参数"})
    rows = [r for r in load_rows() if r.get("标题", "") == title]
    by_plat = defaultdict(list)
    for r in rows:
        by_plat[r.get("平台") or "baidu"].append(r)
    trace = []
    for plat, items in by_plat.items():
        items.sort(key=lambda x: x["_time"] or datetime.min)
        ranks = [x["_rank"] for x in items if x["_rank"]]
        trace.append(
            {
                "平台": plat,
                "首现": items[0].get("抓取时间", ""),
                "最近": items[-1].get("抓取时间", ""),
                "轮次": len(items),
                "最好名次": min(ranks) if ranks else None,
                "当前排名": items[-1]["_rank"],
                "当前热度": items[-1]["_hot"],
            }
        )
    trace.sort(key=lambda x: x["首现"])
    return jsonify({"ok": True, "title": title, "trace": trace})


# ---------------- 两词对比 ----------------

def keyword_series(rows, kw):
    """对一个关键词（标题精确或包含匹配）按时间聚合：每轮取最高热度与最好名次。"""
    rs = [r for r in rows if r.get("标题") and kw in r["标题"]]
    if not rs:
        return None
    hot_map, rank_map = {}, {}
    for r in rs:
        t = r["_time"]
        if not t:
            continue
        hot_map[t] = max(hot_map.get(t, 0), r["_hot"] or 0)
        rk = r["_rank"] or 0
        if rk and (t not in rank_map or rk < rank_map[t]):
            rank_map[t] = rk

    times = sorted(hot_map)
    points = [{"time": t.strftime("%m-%d %H:%M"), "hot": hot_map[t], "rank": rank_map.get(t)} for t in times]

    latest_rows = [r for r in rs if r["_time"] == times[-1]] if times else []
    latest_rows.sort(key=lambda x: x["_rank"])
    return {
        "keyword": kw,
        "points": points,
        "stats": {
            "轮次": len(times),
            "首现": times[0].strftime("%Y-%m-%d %H:%M") if times else "—",
            "当前热度": hot_map[times[-1]] if times else 0,
            "峰值热度": max(hot_map.values()) if hot_map else 0,
            "最好名次": min(rank_map.values()) if rank_map else None,
            "最新排名": rank_map.get(times[-1]) if times else None,
            "情感": (latest_rows[0].get("情感", "中性") if latest_rows else "中性"),
            "匹配条数": len(rs),
        },
    }


@app.route("/compare")
def compare():
    w1 = request.args.get("w1", "").strip()
    w2 = request.args.get("w2", "").strip()
    rows = load_rows()

    # 候选词：按最近出现顺序给输入框做提示
    seen, candidates = set(), []
    for r in sorted(rows, key=lambda x: x["_time"] or datetime.min, reverse=True):
        t = r.get("标题", "")
        if t and t not in seen:
            seen.add(t)
            candidates.append(t)

    s1 = keyword_series(rows, w1) if w1 else None
    s2 = keyword_series(rows, w2) if w2 else None
    return render_template(
        "compare.html",
        w1=w1,
        w2=w2,
        s1=s1,
        s2=s2,
        titles=candidates[:200],
    )


@app.route("/api/latest")
def api_latest():
    rows = load_rows(norm_platform(request.args.get("platform")))
    batch, latest = latest_batch(rows)
    return jsonify(
        {
            "抓取时间": latest.strftime("%Y-%m-%d %H:%M:%S") if latest else "",
            "条数": len(batch),
            "stats": site_stats(rows),
            "mood": store.sentiment_counts(batch),
            "items": [row_to_dict(r) for r in batch],
        }
    )


@app.route("/dashboard")
def dashboard():
    platform = norm_platform(request.args.get("platform"))
    rows = load_rows(platform)
    times = sorted({r["_time"] for r in rows if r["_time"]})
    span_min = int((times[-1] - times[0]).total_seconds() // 60) if len(times) >= 2 else 0
    overview = {
        "rounds": len(times),
        "records": len(rows),
        "unique": len({r.get("标题", "") for r in rows}),
        "span": span_min,
    }

    batch, latest = latest_batch(rows)
    prev_batch = sorted(previous_batch(rows), key=lambda x: x["_rank"])
    cur = {r["标题"]: r for r in batch}
    prev = {r["标题"]: r for r in prev_batch}

    top_hot = [{"标题": r["标题"], "热度": r["_hot"], "排名": r["_rank"]} for r in batch[:10]]
    max_hot = max([r["_hot"] for r in batch], default=1) or 1

    # 情感占比：最新一轮 / 全部
    mood_cur = store.sentiment_counts(batch)
    mood_all = store.sentiment_counts(rows)

    def mood_stat(m):
        total = sum(m.values()) or 1
        return {
            "正面": m["正面"],
            "中性": m["中性"],
            "负面": m["负面"],
            "正面pct": round(m["正面"] / total * 100),
            "中性pct": round(m["中性"] / total * 100),
            "负面pct": round(m["负面"] / total * 100),
        }

    mood_cur_stat = mood_stat(mood_cur)
    mood_all_stat = mood_stat(mood_all)

    # 负面热搜 Top（最新一轮里负面且热度最高的）
    negative_top = sorted(
        [r for r in batch if r.get("情感") == "负面"], key=lambda x: -x["_hot"]
    )[:6]

    # 在榜统计
    stat = defaultdict(lambda: {"count": 0, "best": 99, "sum": 0})
    for r in rows:
        s = stat[r.get("标题", "")]
        s["count"] += 1
        s["best"] = min(s["best"], r["_rank"])
        s["sum"] += r["_rank"]
    stick = sorted(
        [
            {
                "标题": t,
                "出现次数": v["count"],
                "最好名次": v["best"],
                "平均名次": round(v["sum"] / v["count"], 1),
            }
            for t, v in stat.items()
        ],
        key=lambda x: (-x["出现次数"], x["平均名次"]),
    )[:10]

    # 排名变化
    rising = []
    for title, r in cur.items():
        if title in prev:
            diff = prev[title]["_rank"] - r["_rank"]
            if diff > 0:
                rising.append(
                    {"标题": title, "上升": diff, "上轮": prev[title]["_rank"], "本轮": r["_rank"]}
                )
    rising.sort(key=lambda x: -x["上升"])
    rising = rising[:5]
    new_entries = sorted(
        [r for t, r in cur.items() if prev_batch and t not in prev], key=lambda x: x["_rank"]
    )[:6]
    dropped = sorted(
        [r for t, r in prev.items() if t not in cur], key=lambda x: x["_rank"]
    )[:6]

    # 趋势
    top_titles = [x["标题"] for x in stick[:5]]
    max_all = max([r["_hot"] for r in rows], default=1) or 1
    palette = ["#f59e0b", "#2563eb", "#dc2626", "#059669", "#7c3aed"]
    trend = []
    for i, t in enumerate(top_titles):
        pts = sorted(
            [r for r in rows if r.get("标题") == t and r["_time"]], key=lambda x: x["_time"]
        )
        n = len(pts)
        coords = []
        for j, r in enumerate(pts):
            x = 0 if n <= 1 else round(j / (n - 1) * 100, 2)
            y = round(40 - r["_hot"] / max_all * 38, 2)
            coords.append("{} {}".format(x, y))
        trend.append(
            {
                "title": t,
                "color": palette[i % len(palette)],
                "points": " ".join(coords),
                "条数": n,
                "首": pts[0]["抓取时间"] if pts else "",
                "末": pts[-1]["抓取时间"] if pts else "",
            }
        )

    return render_template(
        "dashboard.html",
        overview=overview,
        latest=latest.strftime("%Y-%m-%d %H:%M:%S") if latest else "暂无数据",
        top_hot=top_hot,
        max_hot=max_hot,
        trend=trend,
        stick=stick,
        rising=rising,
        new_entries=new_entries,
        dropped=dropped,
        mood_cur=mood_cur_stat,
        mood_all=mood_all_stat,
        negative_top=negative_top,
        platform=platform,
        plat_links=plat_links("/dashboard", platform, {}),
    )


# ---------------- 词云监测 ----------------

def compute_words(rng, mood=None, platform=""):
    """按范围统计热词：权重=出现次数×搜索热度，标题权重高于简介。"""
    selected, label = select_rows(rng, platform)
    words = analysis.word_stats(selected)
    if mood in ("正面", "中性", "负面"):
        words = [w for w in words if w["tone"] == mood]
        label = "{} · 仅{}".format(label, mood)
    return words, label, len(selected)


def fmt_weight(value):
    if value >= 1e8:
        return "{:.1f} 亿".format(value / 1e8)
    return "{:.1f} 万".format(value / 1e4)


def size_words(words):
    """按热度权重映射字号 14~54。"""
    if not words:
        return []
    lo, hi = words[-1]["weight"], words[0]["weight"]
    out = []
    for w in words:
        ratio = 0.5 if hi == lo else (w["weight"] - lo) / (hi - lo)
        item = dict(w)
        item["size"] = round(14 + ratio * 40)
        item["weight_text"] = fmt_weight(w["weight"])
        out.append(item)
    return out


FONT_CANDIDATES = ("simhei.ttf", "msyhbd.ttc", "msyh.ttc", "simsun.ttc")
_CLOUD_CACHE = {}
_CLOUD_LOCK = threading.Lock()


def find_cn_font():
    for name in FONT_CANDIDATES:
        path = os.path.join(r"C:\Windows\Fonts", name)
        if os.path.exists(path):
            return path
    return None


def random_color(word=None, font_size=None, position=None, orientation=None,
                 font_path=None, random_state=None, **kwargs):
    """高饱和度随机色，做出彩色词云效果。"""
    rnd = random_state if random_state is not None else random
    h = rnd.random()
    s = 0.55 + rnd.random() * 0.35
    v = 0.60 + rnd.random() * 0.35
    r, g, b = colorsys.hsv_to_rgb(h, s, v)
    return "#{:02x}{:02x}{:02x}".format(int(r * 255), int(g * 255), int(b * 255))


def cloud_cache_key(rng, mood, color_mode, platform=""):
    return (rng, mood or "", color_mode, platform or "")


def build_cloud(rng, mood=None, color_mode="mood", platform="", width=1100, height=640):
    """生成词云图片 + 每个词的定位框（供前端做悬浮提示）。

    返回 (png_bytes, boxes)，按“数据是否更新 + 参数”缓存。
    """
    version = store.data_version()
    key = cloud_cache_key(rng, mood, color_mode, platform)
    hit = _CLOUD_CACHE.get(key)
    if hit and hit[0] == version:
        return hit[1], hit[2]

    with _CLOUD_LOCK:
        hit = _CLOUD_CACHE.get(key)
        if hit and hit[0] == version:
            return hit[1], hit[2]

        words, _, _ = compute_words(rng, mood, platform)
        if not words:
            return None, []

        font_path = find_cn_font()
        freq = {w["word"]: w["count"] for w in words if w["count"] > 0}
        if not freq:
            return None, []
        tone_of = {w["word"]: w["tone"] for w in words}
        count_of = {w["word"]: w["count"] for w in words}
        color_of = {w["word"]: w["color"] for w in words}

        def color_func(word=None, **kw):
            if color_mode == "mood":
                return color_of.get(word, "#64748b")
            return random_color(word=word, **kw)

        wc = WordCloud(
            font_path=font_path,
            width=width,
            height=height,
            background_color="white",
            max_words=220,
            prefer_horizontal=0.85,
            relative_scaling=0.75,
            margin=3,
            color_func=color_func,
            random_state=42,
        )
        wc.generate_from_frequencies(freq)
        buf = BytesIO()
        wc.to_image().save(buf, format="PNG")
        png = buf.getvalue()

        boxes = []
        for (word, _norm), font_size, position, orientation, _c in wc.layout_:
            # position = (上, 左)；用字体实际包围盒算出词在图片中的精确位置
            try:
                font = ImageFont.truetype(font_path, int(font_size))
                tf = ImageFont.TransposedFont(font, orientation=orientation)
                l, t, r, b = tf.getbbox(word)
            except Exception:
                font = ImageFont.truetype(font_path, int(font_size))
                l, t, r, b = 0, 0, font.getlength(word), font_size
            x0 = float(position[1]) + l
            y0 = float(position[0]) + t
            x1 = float(position[1]) + r
            y1 = float(position[0]) + b
            boxes.append(
                {
                    "word": word,
                    "x": round(x0 / width * 100, 3),
                    "y": round(y0 / height * 100, 3),
                    "w": round((x1 - x0) / width * 100, 3),
                    "h": round((y1 - y0) / height * 100, 3),
                    "count": count_of.get(word, 0),
                    "tone": tone_of.get(word, "中性"),
                }
            )

        _CLOUD_CACHE[key] = (version, png, boxes)
        return png, boxes


@app.route("/cloud")
def cloud():
    rng = request.args.get("range", "latest")
    mood = request.args.get("mood", "")
    color_mode = request.args.get("color", "mood")
    platform = norm_platform(request.args.get("platform"))
    words, label, n = compute_words(rng, mood, platform)
    return render_template(
        "cloud.html",
        words=size_words(words),
        label=label,
        rounds=n,
        range_key=rng,
        mood=mood,
        color_mode=color_mode,
        platform=platform,
        plat_links=plat_links(
            "/cloud", platform, {"range": rng, "mood": mood, "color": color_mode}
        ),
    )


@app.route("/api/cloud")
def api_cloud():
    rng = request.args.get("range", "latest")
    mood = request.args.get("mood", "")
    color_mode = request.args.get("color", "mood")
    platform = norm_platform(request.args.get("platform"))
    words, label, n = compute_words(rng, mood, platform)
    _png, boxes = build_cloud(rng, mood, color_mode, platform)
    return jsonify(
        {"label": label, "rounds": n, "words": size_words(words), "boxes": boxes}
    )


@app.route("/cloud.png")
def cloud_png():
    rng = request.args.get("range", "latest")
    mood = request.args.get("mood", "")
    color_mode = request.args.get("color", "mood")
    platform = norm_platform(request.args.get("platform"))
    png, _boxes = build_cloud(rng, mood, color_mode, platform)
    if png is None:
        return Response(status=204)
    resp = Response(png, mimetype="image/png")
    resp.headers["Cache-Control"] = "no-store, max-age=0"
    return resp


# ---------------- AI 研判（大模型） ----------------

@app.route("/ai")
def ai_page():
    rows = load_rows()
    batch, latest = latest_batch(rows)
    rng = request.args.get("range", "latest")
    platform = norm_platform(request.args.get("platform"))
    selected, label = select_rows(rng, platform)
    events = analysis.cluster_events(unique_rows(selected), threshold=0.3)[:8]
    top_words = analysis.word_stats(selected)[:12]
    return render_template(
        "ai.html",
        events=events,
        latest=latest.strftime("%Y-%m-%d %H:%M:%S") if latest else "暂无数据",
        rounds=len(selected),
        rng=rng,
        label=label,
        status=llm.status(),
        top_words=top_words,
        mood=store.sentiment_counts(selected),
        platform=platform,
        plat_links=plat_links("/ai", platform, {"range": rng}),
    )


def ai_reply(text, error):
    if error:
        return jsonify({"ok": False, "error": error})
    return jsonify({"ok": True, "text": text or ""})


@app.route("/api/ai/status")
def api_ai_status():
    return jsonify(llm.status())


def _payload():
    return request.get_json(silent=True) or {}


@app.route("/api/ai/interpret", methods=["POST"])
def api_ai_interpret():
    data = _payload()
    title = (data.get("title") or "").strip()
    if not title:
        return ai_reply(None, "缺少 title 参数")
    rows = load_rows()
    text, error = llm.interpret(rows, title)
    return ai_reply(text, error)


@app.route("/api/ai/event", methods=["POST"])
def api_ai_event():
    data = _payload()
    titles = data.get("titles") or []
    if not titles:
        return ai_reply(None, "缺少 titles 参数")
    text, error = llm.summarize_event({"标题": titles})
    return ai_reply(text, error)


@app.route("/api/ai/briefing", methods=["POST"])
def api_ai_briefing():
    data = _payload()
    rng = (data.get("range") or "latest").strip()
    platform = norm_platform(data.get("platform"))
    selected, label = select_rows(rng, platform)
    if not selected:
        return ai_reply(None, "所选范围内没有数据")
    text, error = llm.briefing(selected, label)
    return ai_reply(text, error)


@app.route("/api/ai/chat", methods=["POST"])
def api_ai_chat():
    data = _payload()
    question = (data.get("question") or "").strip()
    if not question:
        return ai_reply(None, "请输入问题")
    history = data.get("history") or []
    platform = norm_platform(data.get("platform"))
    rows = load_rows(platform)
    batch, _latest = latest_batch(rows)
    text, error = llm.assistant(question, batch, history=history)
    return ai_reply(text, error)


def platform_label(p):
    return platform_name(p)


def _csv_cell(value):
    s = "" if value is None else str(value)
    s = s.replace("\r", " ").replace("\n", " ")
    if "," in s or '"' in s:
        s = '"' + s.replace('"', '""') + '"'
    return s


@app.route("/export")
def export_csv():
    """导出当前筛选结果为 CSV（Excel 可直接打开，带 BOM 防乱码）。"""
    rng = request.args.get("range", "all").strip()
    platform = norm_platform(request.args.get("platform"))
    mood = request.args.get("mood", "").strip()
    q = request.args.get("q", "").strip()
    rows, label = select_rows(rng, platform)
    if q or mood:
        rows = apply_filters(
            rows, start=None, end=None, q=q, rank_min=None, rank_max=None,
            dedupe=False, sort_by="time", mood=mood,
        )
        label = "筛选结果"

    header = ["抓取时间", "来源", "排名", "标题", "热度", "情感", "情感分", "简介", "链接"]
    lines = [",".join(header)]
    for r in rows:
        lines.append(
            ",".join(
                _csv_cell(v)
                for v in [
                    r.get("抓取时间", ""),
                    platform_label(r.get("平台")),
                    r.get("_rank") or 0,
                    r.get("标题", ""),
                    r.get("_hot") or 0,
                    r.get("情感", ""),
                    "{:.2f}".format(r.get("情感分") or 0.5),
                    r.get("简介", ""),
                    r.get("链接", ""),
                ]
            )
        )
    text = "\ufeff" + "\r\n".join(lines) + "\r\n"
    fname = "热搜_{}_{}.csv".format(label, datetime.now().strftime("%Y%m%d_%H%M"))
    resp = Response(text, mimetype="text/csv")
    resp.headers["Content-Disposition"] = (
        "attachment; filename=hot_export.csv; filename*=UTF-8''" + quote(fname)
    )
    return resp


@app.route("/alerts")
def alerts_page():
    platform = norm_platform(request.args.get("platform"))
    rows = load_rows(platform)
    batch, latest = latest_batch(rows)
    keywords = store.watch_keywords()
    alerts = analysis.detect_alerts(batch, previous_batch(rows), keywords)
    counts = {"危险": 0, "关注": 0, "提示": 0}
    for a in alerts:
        counts[a["级别"]] = counts.get(a["级别"], 0) + 1
    return render_template(
        "alerts.html",
        alerts=alerts,
        keywords=keywords,
        counts=counts,
        batch_count=len(batch),
        latest=latest.strftime("%Y-%m-%d %H:%M:%S") if latest else "暂无数据",
        platform=platform,
        plat_links=plat_links("/alerts", platform, {}),
        push_cfg=store.get_settings(),
        push_channels=push.CHANNELS,
        pushes=store.recent_pushes(12),
    )


@app.route("/api/watch", methods=["POST"])
@admin_required
def api_watch():
    data = _payload()
    action = (data.get("action") or "add").strip()
    kw = (data.get("keyword") or "").strip()
    if action == "delete":
        store.del_watch(kw)
    elif not kw:
        return jsonify({"ok": False, "error": "请输入监控词"})
    else:
        store.add_watch(kw)
    return jsonify({"ok": True, "keywords": store.watch_keywords()})


# ---------------- 告警推送 ----------------

@app.route("/api/push/settings", methods=["POST"])
@admin_required
def api_push_settings():
    data = _payload()
    values = {}
    if "enabled" in data:
        values["push_enabled"] = "1" if data.get("enabled") else "0"
    if "channel" in data:
        ch = (data.get("channel") or "generic").strip()
        values["push_channel"] = ch if ch in push.CHANNEL_NAMES else "generic"
    if "webhook" in data:
        values["push_webhook"] = (data.get("webhook") or "").strip()
    if "levels" in data:
        levels = [x for x in (data.get("levels") or []) if x in ("危险", "关注", "提示")]
        values["push_levels"] = ",".join(levels) or "危险"
    if "interval" in data:
        try:
            values["push_interval"] = str(max(30, min(3600, int(data.get("interval") or 60))))
        except ValueError:
            values["push_interval"] = "60"
    store.set_settings(values)
    return jsonify({"ok": True, "settings": store.get_settings()})


@app.route("/api/push/test", methods=["POST"])
@admin_required
def api_push_test():
    """发一条测试消息，验证 Webhook 配置是否可用。"""
    data = _payload()
    cfg = store.get_settings()
    channel = (data.get("channel") or cfg.get("push_channel") or "generic").strip()
    webhook = (data.get("webhook") or cfg.get("push_webhook") or "").strip()
    if not webhook:
        return jsonify({"ok": False, "error": "请先填写 Webhook 地址"})
    ok, msg = push.send(
        channel,
        webhook,
        "热搜实时站 · 推送测试",
        "这是一条测试消息，收到说明 Webhook 配置成功。",
    )
    store.log_push(
        push.CHANNEL_NAMES.get(channel, channel),
        "测试",
        "推送测试",
        "手动测试",
        ok,
        msg,
    )
    return jsonify({"ok": ok, "message": msg})


@app.route("/api/push/scan", methods=["POST"])
@admin_required
def api_push_scan():
    """手动触发一次扫描（便于验证调度逻辑，不必等后台线程）。"""
    try:
        n = push_worker.scan_once()
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)})
    return jsonify({"ok": True, "pushed": n, "pushes": store.recent_pushes(12)})


@app.route("/entities")
def entities_page():
    rng = request.args.get("range", "all").strip()
    platform = norm_platform(request.args.get("platform"))
    selected, label = select_rows(rng, platform)
    entities = analysis.extract_entities(unique_rows(selected))
    return render_template(
        "entities.html",
        entities=entities,
        label=label,
        rounds=len(selected),
        range_key=rng,
        platform=platform,
        plat_links=plat_links("/entities", platform, {"range": rng}),
    )


@app.route("/briefing")
def briefing_page():
    rng = request.args.get("range", "latest").strip()
    platform = norm_platform(request.args.get("platform"))
    rows = load_rows(platform)
    selected, label = select_rows(rng, platform)
    times = sorted({r["_time"] for r in rows if r["_time"]})
    mood = store.sentiment_counts(selected)
    total = sum(mood.values()) or 1
    mood_pct = {k: round(v / total * 100) for k, v in mood.items()}
    batch, latest = latest_batch(rows)
    keywords = store.watch_keywords()
    alerts = analysis.detect_alerts(batch, previous_batch(rows), keywords)[:10]
    negs = sorted(
        [r for r in selected if r.get("情感") == "负面"],
        key=lambda r: (r.get("_rank") or 99),
    )[:10]
    return render_template(
        "briefing.html",
        label=label,
        rounds=len(selected),
        records=len(rows),
        span_first=times[0].strftime("%Y-%m-%d %H:%M") if times else "—",
        span_last=times[-1].strftime("%Y-%m-%d %H:%M") if times else "—",
        generated=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        mood=mood,
        mood_pct=mood_pct,
        events=analysis.cluster_events(unique_rows(selected), threshold=0.3)[:6],
        words=analysis.word_stats(selected)[:15],
        entities=analysis.extract_entities(unique_rows(selected)),
        alerts=alerts,
        negs=negs,
        range_key=rng,
        platform=platform,
        plat_links=plat_links("/briefing", platform, {"range": rng}),
    )


if __name__ == "__main__":
    push_worker.start()
    app.run(host="0.0.0.0", port=5000, debug=False)
