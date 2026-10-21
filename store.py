# -*- coding: utf-8 -*-
"""SQLite 存储层：热搜历史数据的统一读写。

设计：
  - 表 hot_items 保存每一轮抓取的每一条热搜，带来源平台与情感标签
  - 唯一约束 (batch_time, platform, rank, title) 保证重复写入自动忽略（幂等）
  - 支持从 baidu_hot_log.csv 一次性迁移 + 增量同步，历史数据不丢
"""

import csv
import os
import sqlite3
import threading
from datetime import datetime

import sentiment

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(BASE_DIR, "hot.db")
CSV_PATH = os.path.join(BASE_DIR, "baidu_hot_log.csv")

DT_FORMATS = ("%Y-%m-%dT%H:%M", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M")

SCHEMA = """
CREATE TABLE IF NOT EXISTS hot_items (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    batch_time TEXT NOT NULL,
    platform TEXT NOT NULL DEFAULT 'baidu',
    rank INTEGER DEFAULT 0,
    title TEXT DEFAULT '',
    hot INTEGER DEFAULT 0,
    hot_change TEXT DEFAULT '',
    summary TEXT DEFAULT '',
    url TEXT DEFAULT '',
    img TEXT DEFAULT '',
    sentiment TEXT DEFAULT '中性',
    score REAL DEFAULT 0.5,
    UNIQUE (batch_time, platform, rank, title)
);
CREATE INDEX IF NOT EXISTS idx_items_batch ON hot_items(batch_time);
CREATE INDEX IF NOT EXISTS idx_items_title ON hot_items(title);
CREATE INDEX IF NOT EXISTS idx_items_platform ON hot_items(platform);

CREATE TABLE IF NOT EXISTS watch_keywords (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    keyword TEXT NOT NULL UNIQUE,
    created_at TEXT DEFAULT ''
);

CREATE TABLE IF NOT EXISTS comments (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    batch_time TEXT NOT NULL,
    platform TEXT NOT NULL DEFAULT 'weibo',
    topic TEXT NOT NULL,
    mid TEXT DEFAULT '',
    user TEXT DEFAULT '',
    text TEXT DEFAULT '',
    likes INTEGER DEFAULT 0,
    sentiment TEXT DEFAULT '中性',
    score REAL DEFAULT 0.5,
    UNIQUE (batch_time, topic, mid, text)
);
CREATE INDEX IF NOT EXISTS idx_comments_topic ON comments(topic);

CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT DEFAULT ''
);

CREATE TABLE IF NOT EXISTS push_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at TEXT NOT NULL,
    channel TEXT DEFAULT '',
    level TEXT DEFAULT '',
    title TEXT DEFAULT '',
    detail TEXT DEFAULT '',
    ok INTEGER DEFAULT 0,
    message TEXT DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_push_time ON push_log(created_at);
"""

_write_lock = threading.Lock()
_synced = False


def parse_dt(value):
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


def connect():
    conn = sqlite3.connect(DB_PATH, timeout=15)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    with connect() as conn:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.executescript(SCHEMA)


def data_version():
    """返回一个随数据变化而变化的版本串，用于缓存失效判断。"""
    with connect() as conn:
        row = conn.execute(
            "SELECT COUNT(*) AS c, COALESCE(MAX(batch_time), '') AS m FROM hot_items"
        ).fetchone()
    return "{}#{}".format(row["m"], row["c"])


def platform_counts():
    """各来源平台已入库的条数，例如 {'baidu': 650, 'weibo': 100}。"""
    with connect() as conn:
        rows = conn.execute(
            "SELECT platform, COUNT(*) AS c FROM hot_items GROUP BY platform"
        ).fetchall()
    return {r["platform"] or "baidu": r["c"] for r in rows}


def insert_items(items, batch_time, platform="baidu", with_sentiment=True):
    """写入一批热搜。返回实际新增条数（重复的会被唯一约束忽略）。"""
    if not items:
        return 0
    rows = []
    for it in items:
        title = (it.get("标题") or "").strip()
        summary = (it.get("简介") or "").strip()
        if with_sentiment:
            label, score = sentiment.analyze(title, summary)
        else:
            label, score = "中性", 0.5
        rows.append(
            (
                batch_time,
                platform,
                to_int(it.get("排名"), 0),
                title,
                to_int(it.get("热度"), 0),
                str(it.get("热度变化") or ""),
                summary,
                str(it.get("链接") or ""),
                str(it.get("图片") or ""),
                label,
                score,
            )
        )
    with _write_lock, connect() as conn:
        cur = conn.executemany(
            """INSERT OR IGNORE INTO hot_items
               (batch_time, platform, rank, title, hot, hot_change, summary, url, img, sentiment, score)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            rows,
        )
        return cur.rowcount if cur.rowcount and cur.rowcount > 0 else 0


def _max_batch_time(platform="baidu"):
    with connect() as conn:
        row = conn.execute(
            "SELECT MAX(batch_time) AS m FROM hot_items WHERE platform = ?", (platform,)
        ).fetchone()
    return row["m"] if row and row["m"] else ""


def sync_from_csv():
    """把 CSV 中尚未入库的批次迁移进库。幂等，可重复调用。"""
    global _synced
    if not os.path.exists(CSV_PATH):
        _synced = True
        return 0

    with open(CSV_PATH, "r", encoding="utf-8-sig", newline="") as f:
        raw = list(csv.DictReader(f))
    if not raw:
        _synced = True
        return 0

    latest_in_db = _max_batch_time("baidu")
    buckets = {}
    for r in raw:
        ts = (r.get("抓取时间") or "").strip()
        if not ts or (latest_in_db and ts <= latest_in_db):
            continue
        item = {
            "排名": r.get("排名"),
            "标题": r.get("标题"),
            "热度": r.get("热度"),
            "热度变化": r.get("热度变化"),
            "简介": r.get("简介"),
            "链接": r.get("链接"),
            "图片": r.get("图片"),
        }
        buckets.setdefault(ts, []).append(item)

    total = 0
    for ts in sorted(buckets):
        total += insert_items(buckets[ts], ts, "baidu")
    _synced = True
    return total


def ensure_synced():
    """首次调用时做一次 CSV→DB 同步，之后不再重复。"""
    if not _synced:
        sync_from_csv()


def load_rows(platform=None):
    """读取全部历史记录，字段与旧 CSV 版本保持一致。

    额外补充 _time / _hot / _rank 内部字段，以及 平台 / 情感 / 情感分。
    """
    ensure_synced()
    sql = "SELECT * FROM hot_items"
    args = ()
    if platform:
        sql += " WHERE platform = ?"
        args = (platform,)
    sql += " ORDER BY batch_time, rank"
    with connect() as conn:
        raw = conn.execute(sql, args).fetchall()

    rows = []
    for r in raw:
        rank = r["rank"] or 0
        hot = r["hot"] or 0
        rows.append(
            {
                "抓取时间": r["batch_time"],
                "排名": rank,
                "标题": r["title"] or "",
                "热度": hot,
                "热度变化": r["hot_change"] or "",
                "简介": r["summary"] or "",
                "链接": r["url"] or "",
                "图片": r["img"] or "",
                "平台": r["platform"] or "baidu",
                "情感": r["sentiment"] or "中性",
                "情感分": r["score"] if r["score"] is not None else 0.5,
                "_time": parse_dt(r["batch_time"]),
                "_hot": hot,
                "_rank": rank,
            }
        )
    return rows


def distinct_titles(rows):
    return {r["标题"] for r in rows if r.get("标题")}


def watch_keywords():
    """读取全部监控词。"""
    with connect() as conn:
        rows = conn.execute("SELECT keyword FROM watch_keywords ORDER BY id").fetchall()
    return [r["keyword"] for r in rows]


def add_watch(keyword):
    kw = (keyword or "").strip()
    if not kw:
        return False
    with _write_lock, connect() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO watch_keywords (keyword, created_at) VALUES (?, ?)",
            (kw, datetime.now().strftime("%Y-%m-%d %H:%M:%S")),
        )
    return True


def del_watch(keyword):
    with _write_lock, connect() as conn:
        conn.execute(
            "DELETE FROM watch_keywords WHERE keyword = ?", ((keyword or "").strip(),)
        )


# ---------------- 评论（微博热评） ----------------

def insert_comments(items, batch_time, topic, platform="weibo"):
    """写入一批评论，每条评论入库前统一打情感标签。返回实际新增条数。"""
    rows = []
    for it in items:
        text = (it.get("text") or "").strip()
        if not text:
            continue
        label, score = sentiment.analyze(text, "")
        rows.append(
            (
                batch_time,
                platform,
                (topic or "").strip(),
                str(it.get("mid") or ""),
                (it.get("user") or "").strip(),
                text,
                to_int(it.get("likes"), 0),
                label,
                score,
            )
        )
    if not rows:
        return 0
    with _write_lock, connect() as conn:
        cur = conn.executemany(
            """INSERT OR IGNORE INTO comments
               (batch_time, platform, topic, mid, user, text, likes, sentiment, score)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            rows,
        )
        return cur.rowcount if cur.rowcount and cur.rowcount > 0 else 0


def comments_for_topic(topic, limit=80):
    """取某话题最新一轮的热门评论 + 该轮情感计数。"""
    topic = (topic or "").strip()
    if not topic:
        return [], {"正面": 0, "中性": 0, "负面": 0}, ""
    with connect() as conn:
        row = conn.execute(
            "SELECT MAX(batch_time) AS m FROM comments WHERE topic = ?", (topic,)
        ).fetchone()
        ts = row["m"] if row and row["m"] else ""
        if not ts:
            return [], {"正面": 0, "中性": 0, "负面": 0}, ""
        raw = conn.execute(
            """SELECT * FROM comments WHERE topic = ? AND batch_time = ?
               ORDER BY likes DESC, id ASC LIMIT ?""",
            (topic, ts, limit),
        ).fetchall()

    items = []
    mood = {"正面": 0, "中性": 0, "负面": 0}
    for r in raw:
        label = r["sentiment"] or "中性"
        mood[label] = mood.get(label, 0) + 1
        items.append(
            {
                "user": r["user"] or "匿名",
                "text": r["text"] or "",
                "likes": r["likes"] or 0,
                "情感": label,
                "情感分": r["score"] if r["score"] is not None else 0.5,
                "时间": r["batch_time"],
            }
        )
    return items, mood, ts


def topics_with_comments():
    """已有评论数据的话题列表（按最新一轮时间倒序）。"""
    with connect() as conn:
        rows = conn.execute(
            """SELECT topic, MAX(batch_time) AS latest, COUNT(*) AS c
               FROM comments GROUP BY topic ORDER BY latest DESC"""
        ).fetchall()
    return [
        {"话题": r["topic"], "最近一轮": r["latest"], "条数": r["c"]} for r in rows
    ]


# ---------------- 设置与推送记录 ----------------

SETTING_KEYS = (
    "push_enabled",
    "push_channel",
    "push_webhook",
    "push_levels",
    "push_interval",
)


def get_settings():
    """读取全部设置，缺失项返回空串。"""
    out = {k: "" for k in SETTING_KEYS}
    with connect() as conn:
        rows = conn.execute("SELECT key, value FROM settings").fetchall()
    for r in rows:
        if r["key"] in out:
            out[r["key"]] = r["value"] or ""
    return out


def set_settings(values):
    """批量写入设置（只接受 SETTING_KEYS 内的键）。"""
    items = [(k, str(v)) for k, v in (values or {}).items() if k in SETTING_KEYS]
    if not items:
        return
    with _write_lock, connect() as conn:
        conn.executemany(
            "INSERT INTO settings (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            items,
        )


def log_push(channel, level, title, detail, ok, message):
    with _write_lock, connect() as conn:
        conn.execute(
            """INSERT INTO push_log
               (created_at, channel, level, title, detail, ok, message)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (
                datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                channel or "",
                level or "",
                title or "",
                detail or "",
                1 if ok else 0,
                (message or "")[:300],
            ),
        )


def recent_pushes(limit=20):
    with connect() as conn:
        rows = conn.execute(
            "SELECT * FROM push_log ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()
    return [
        {
            "时间": r["created_at"],
            "渠道": r["channel"],
            "级别": r["level"],
            "标题": r["title"],
            "说明": r["detail"],
            "成功": bool(r["ok"]),
            "返回": r["message"],
        }
        for r in rows
    ]


def sentiment_counts(rows):
    counts = {"正面": 0, "中性": 0, "负面": 0}
    for r in rows:
        label = r.get("情感") or "中性"
        counts[label] = counts.get(label, 0) + 1
    return counts


init_db()
