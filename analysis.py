# -*- coding: utf-8 -*-
"""文本分析：中文分词、词权重统计、事件聚类。

与情感分析（sentiment.py）配合：统计词频时可同时记录每个词的正/负倾向。
"""

import re
from collections import Counter

import jieba
import jieba.posseg as pseg

STOPWORDS = set(
    """
    的 了 在 是 我 有 和 就 不 人 都 一 一个 一些 上 也 很 到 说 要 去 你 会 着 没有 看 好 自己
    这 那 他 她 它 他们 我们 你们 与 及 并 但 而 或 被 把 让 从 为 对 中 后 前 时 等 更 还 又 已
    将 可 能 因 于 之 其 该 此 些 个 多 少 新 年 月 日 天 内 外 名 次 表示 回应 官方 男子 女子
    消息 报道 记者 网友 视频 中国 全国 目前 截至 已经 一名 两位 引发 事件 曝光 进展 情况
    问题 工作 出现 进行 发生 认为 需要 开始 继续 相关 有关 什么 怎么 为什么 可以 不能 不是
    就是 由于 通过 根据 这些 那些 之后 以后 现在 今天 昨天 明天 突然 竟然 而且 但是 因为
    所以 其中 同时 表示 称 据 一 二 三 四 五 六 七 八 九 十 上下 左右 第一 第二 第三
    近日 此前 日前 昨日 今日 当天 当日 现场 一度 随后 最终 整体 部分 多名 少数 大量
    不少 若干 各类 各种 等等 以来 为止 之间 之后 之前 以上 以下 另外 此外 据悉 鉴于
    针对 关于 连日 持续 不断 纷纷 各自 相应 有关 上述 该 相关 一系列 一方面 另一方面
    今年 明年 去年 往年 未来 如今 近来 一直 一时 仍然 尚 亦 均 皆 部分 全部 所有
    """.split()
)

SENT_COLORS = {"正面": "#059669", "负面": "#dc2626", "中性": "#64748b"}


def tokenize(text):
    """中文分词，过滤停用词、单字与纯数字。"""
    out = []
    for w in jieba.cut(text or ""):
        w = w.strip()
        if len(w) < 2 or w in STOPWORDS or re.fullmatch(r"[0-9.%]+", w):
            continue
        out.append(w)
    return out


def word_stats(rows, title_factor=1.0, summary_factor=0.5):
    """统计词的出现次数、热度权重与情感倾向。

    权重 = 出现次数 × 所在热搜的搜索热度；标题权重高于简介。
    情感倾向按该词所属热搜的情感标签累计。
    """
    weight = Counter()
    count = Counter()
    mood = {}  # word -> Counter(正面/中性/负面)

    def add(text, r, factor):
        for w in tokenize(text):
            weight[w] += r["_hot"] * factor
            count[w] += 1
            mood.setdefault(w, Counter())[r.get("情感") or "中性"] += 1

    for r in rows:
        add(r.get("标题", ""), r, title_factor)
        add(r.get("简介", ""), r, summary_factor)

    words = []
    for w, _ in weight.most_common(120):
        m = mood.get(w, Counter())
        total = sum(m.values()) or 1
        if m.get("负面", 0) > m.get("正面", 0):
            tone = "负面"
        elif m.get("正面", 0) > m.get("负面", 0):
            tone = "正面"
        else:
            tone = "中性"
        words.append(
            {
                "word": w,
                "weight": weight[w],
                "count": count[w],
                "pos": m.get("正面", 0),
                "neu": m.get("中性", 0),
                "neg": m.get("负面", 0),
                "pos_pct": round(m.get("正面", 0) / total * 100),
                "neu_pct": round(m.get("中性", 0) / total * 100),
                "neg_pct": round(m.get("负面", 0) / total * 100),
                "tone": tone,
                "color": SENT_COLORS[tone],
            }
        )
    return words


def jaccard(a, b):
    if not a or not b:
        return 0.0
    inter = len(a & b)
    return inter / float(len(a | b))


def cluster_events(rows, threshold=0.3, max_titles=60):
    """按关键词重叠度把同一件事的多条热搜聚成一个事件。

    返回事件列表：{标题列表, 条数, 合计热度, 最高排名, 关键词, 情感, 负面占比}。
    """
    picked = [r for r in rows if r.get("标题")][:max_titles]
    keys = {}
    for i, r in enumerate(picked):
        kws = set(tokenize(r.get("标题", "")))
        keys[i] = kws

    parent = list(range(len(picked)))

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(x, y):
        rx, ry = find(x), find(y)
        if rx != ry:
            parent[ry] = rx

    for i in range(len(picked)):
        for j in range(i + 1, len(picked)):
            if jaccard(keys[i], keys[j]) >= threshold:
                union(i, j)

    groups = {}
    for i in range(len(picked)):
        groups.setdefault(find(i), []).append(i)

    events = []
    for _, idxs in groups.items():
        items = [picked[i] for i in idxs]
        kws = Counter()
        for i in idxs:
            kws.update(keys[i])
        hot = sum(it["_hot"] for it in items)
        neg = sum(1 for it in items if it.get("情感") == "负面")
        pos = sum(1 for it in items if it.get("情感") == "正面")
        tone = "负面" if neg > pos else ("正面" if pos > neg else "中性")
        events.append(
            {
                "标题": [it["标题"] for it in items],
                "条数": len(items),
                "合计热度": hot,
                "最高排名": min(it["_rank"] for it in items if it["_rank"]),
                "关键词": [w for w, _ in kws.most_common(5)],
                "情感": tone,
                "负面占比": round(neg / len(items) * 100),
                "样本": items,
            }
        )
    events.sort(key=lambda e: (-e["条数"], e["最高排名"]))
    return events


ALERT_LEVELS = ("危险", "关注", "提示")
ALERT_COLORS = {"危险": "#dc2626", "关注": "#d97706", "提示": "#2563eb"}


def detect_alerts(batch, prev, keywords, rank_jump=10, mood_jump=5):
    """从最新一轮里挑出需要盯的异常条目。

    告警类型：
      - 负面突增：负面话题排名大幅上升（高危）
      - 监控词命中：命中用户自定义监控词（负面时升级为高危）
      - 排名飙升：较上一轮名次上升超过 rank_jump（关注）
      - 负面高位：负面话题进入前 5（提示）
    统一返回 [{标题, 平台, 排名, 类型, 级别, 说明, 情感, 热度, 时间}]。
    """
    prev_map = {(r.get("平台") or "baidu", r.get("标题")): r for r in prev}
    words = [k for k in (keywords or []) if k]
    out = []
    for r in batch:
        title = r.get("标题") or ""
        p = prev_map.get((r.get("平台") or "baidu", title))
        cur = r.get("_rank") or 0
        pre = (p.get("_rank") if p else 0) or 0
        rise = (pre - cur) if (pre and cur) else 0
        hit = next((k for k in words if k.lower() in title.lower()), None)
        neg = (r.get("情感") == "负面")

        if neg and rise >= mood_jump:
            level, atype = "危险", "负面突增"
            why = "负面话题升温：第 {} 名 → 第 {} 名".format(pre, cur)
        elif hit and neg:
            level, atype = "危险", "监控词命中"
            why = "命中监控词「{}」且判定为负面".format(hit)
        elif rise >= rank_jump:
            level, atype = "关注", "排名飙升"
            why = "第 {} 名 → 第 {} 名（上升 {} 位）".format(pre, cur, rise)
        elif hit:
            level, atype = "关注", "监控词命中"
            why = "命中监控词「{}」".format(hit)
        elif neg and cur and cur <= 5:
            level, atype = "提示", "负面高位"
            why = "负面话题进入前 5（第 {} 名）".format(cur)
        else:
            continue

        out.append(
            {
                "标题": title,
                "平台": r.get("平台") or "baidu",
                "排名": cur,
                "类型": atype,
                "级别": level,
                "级别色": ALERT_COLORS[level],
                "说明": why,
                "情感": r.get("情感") or "中性",
                "热度": r.get("_hot") or 0,
                "时间": r.get("抓取时间") or "",
                "监控词": hit or "",
            }
        )
    order = {lv: i for i, lv in enumerate(ALERT_LEVELS)}
    out.sort(key=lambda a: (order[a["级别"]], a["排名"]))
    return out


ENTITY_TYPES = {"nr": "人物", "ns": "地点", "nt": "机构", "nz": "其他专名"}


def extract_entities(rows, top=15):
    """基于 jieba 词性标注抽取人名/地点/机构等实体，按累计热度排序。

    这是规则版 NER：无模型依赖，快但会有少量误判。
    """
    count = {v: Counter() for v in ENTITY_TYPES.values()}
    heat = {v: Counter() for v in ENTITY_TYPES.values()}
    for r in rows:
        text = "{} {}".format(r.get("标题") or "", r.get("简介") or "")
        seen = set()
        for word, flag in pseg.cut(text):
            kind = ENTITY_TYPES.get(flag)
            word = word.strip()
            if not kind or len(word) < 2 or word in STOPWORDS or word in seen:
                continue
            seen.add(word)
            count[kind][word] += 1
            heat[kind][word] += r.get("_hot") or 0

    result = {}
    for kind, counter in count.items():
        items = [
            {"name": w, "count": counter[w], "heat": heat[kind][w]}
            for w, _ in counter.most_common(top)
        ]
        items.sort(key=lambda x: (-x["heat"], -x["count"]))
        result[kind] = items
    return result
