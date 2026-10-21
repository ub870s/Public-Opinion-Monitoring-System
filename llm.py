# -*- coding: utf-8 -*-
"""通用大模型客户端（OpenAI 兼容 /chat/completions）+ 舆情提示词。

配置（优先级从高到低）：
  环境变量  LLM_BASE_URL / LLM_MODEL / LLM_API_KEY
  config.json（项目根目录）{"base_url": "...", "model": "...", "api_key": "..."}

未配置 api_key 时，is_configured() 返回 False，页面优雅降级为提示文案。
DeepSeek / 通义 / 智谱 / Kimi / 火山方舟 / OpenAI / 本地 Ollama 都走同一套接口。
"""

import json
import os
import urllib.error
import urllib.request

import analysis

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(BASE_DIR, "config.json")

DEFAULT_CONFIG = {"base_url": "https://api.deepseek.com/v1", "model": "deepseek-chat", "api_key": ""}

PLATFORM_SHORT = {
    "baidu": "百度",
    "weibo": "微博",
    "douyin": "抖音",
    "bilibili": "B站",
    "toutiao": "头条",
}

SYSTEM_PROMPT = (
    "你是资深舆情分析师，服务于一个全网热搜实时监测系统。"
    "你会收到系统从百度、微博、抖音、B站、头条等多平台抓取的热搜数据"
    "（标题、来源平台、热度、简介、情感标签）。"
    "请基于给定数据作答，观点明确、条理清晰、说人话；"
    "不要编造数据里没有的事实，数据不足时直接说明。"
)


def load_config():
    cfg = dict(DEFAULT_CONFIG)
    if os.path.exists(CONFIG_PATH):
        try:
            with open(CONFIG_PATH, "r", encoding="utf-8") as f:
                cfg.update(json.load(f) or {})
        except Exception:
            pass
    for key, env in (
        ("base_url", "LLM_BASE_URL"),
        ("model", "LLM_MODEL"),
        ("api_key", "LLM_API_KEY"),
    ):
        val = os.environ.get(env)
        if val:
            cfg[key] = val.strip()
    cfg["base_url"] = (cfg.get("base_url") or "").strip()
    cfg["model"] = (cfg.get("model") or "").strip()
    cfg["api_key"] = (cfg.get("api_key") or "").strip()
    return cfg


def is_configured():
    cfg = load_config()
    return bool(cfg["base_url"] and cfg["model"] and cfg["api_key"])


def status():
    cfg = load_config()
    return {
        "configured": is_configured(),
        "base_url": cfg["base_url"],
        "model": cfg["model"] if is_configured() else "",
        "has_key": bool(cfg["api_key"]),
    }


def chat(messages, temperature=0.4, timeout=90):
    """调用大模型，返回 (文本, 错误信息)。"""
    cfg = load_config()
    if not (cfg["base_url"] and cfg["model"] and cfg["api_key"]):
        return None, "未配置大模型：请在项目根目录新建 config.json 填入 base_url / model / api_key，或设置同名环境变量。"
    url = cfg["base_url"].rstrip("/") + "/chat/completions"
    payload = {
        "model": cfg["model"],
        "messages": messages,
        "temperature": temperature,
        "stream": False,
    }
    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "Authorization": "Bearer " + cfg["api_key"],
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        return data["choices"][0]["message"]["content"], None
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", "ignore")[:300]
        return None, "模型接口返回 {}：{}".format(e.code, detail)
    except Exception as e:
        return None, "请求模型失败：{}".format(e)


# ---------------- 数据摘要 → 提示词 ----------------

def data_digest(rows, label="", max_items=25):
    """把热搜数据压缩成一段紧凑文本，供大模型阅读。"""
    lines = []
    if label:
        lines.append("数据范围：{}（共 {} 条记录）".format(label, len(rows)))
    counts = {}
    for r in rows:
        counts[r.get("情感") or "中性"] = counts.get(r.get("情感") or "中性", 0) + 1
    lines.append(
        "情感分布：正面 {} / 中性 {} / 负面 {}".format(
            counts.get("正面", 0), counts.get("中性", 0), counts.get("负面", 0)
        )
    )

    titles = []
    seen = set()
    for r in rows:
        t = r.get("标题", "")
        if t and t not in seen:
            seen.add(t)
            titles.append(r)
    lines.append("热搜条目（按热度）：")
    for r in sorted(titles, key=lambda x: -x["_hot"])[:max_items]:
        desc = (r.get("简介") or "").replace("\n", " ")[:60]
        lines.append(
            "- {}｜来源 {}｜热度 {}｜排名 {}｜情感 {}｜简介：{}".format(
                r.get("标题"),
                PLATFORM_SHORT.get(r.get("平台") or "baidu", r.get("平台") or "百度"),
                r["_hot"], r["_rank"], r.get("情感"), desc or "无"
            )
        )
    return "\n".join(lines)


def interpret(rows, title):
    """逐条解读：针对一条热搜给出为什么上榜、舆情走向、风险点。"""
    target = [r for r in rows if r.get("标题") == title]
    others = [r for r in rows if r.get("标题") != title][:15]
    prompt = (
        "请解读下面这条热搜。\n\n"
        "【目标热搜】\n标题：{}\n当前热度：{}\n当前排名：{}\n情感标签：{}\n简介：{}\n\n"
        "【同期其他热搜（供参考）】\n{}\n\n"
        "请输出：1) 这条热搜在说什么（一句话）；2) 为什么现在会热；"
        "3) 舆情情绪判断与依据；4) 可能的舆论风险点；5) 给运营/品牌方的 2 条建议。"
        "分段小标题，每段不超过 3 句。"
    ).format(
        title,
        target[-1]["_hot"] if target else "-",
        target[-1]["_rank"] if target else "-",
        target[-1].get("情感") if target else "中性",
        (target[-1].get("简介") if target else "") or "无",
        "\n".join("- {}".format(r.get("标题")) for r in others) or "无",
    )
    return chat(
        [{"role": "system", "content": SYSTEM_PROMPT}, {"role": "user", "content": prompt}]
    )


def briefing(rows, label):
    """智能简报：时间段、核心事件、高频词、情绪分布、风险提示。"""
    events = analysis.cluster_events(rows, threshold=0.3, max_titles=60)[:8]
    words = analysis.word_stats(rows)[:15]
    event_txt = "\n".join(
        "{}. {}（{} 条，最高热度 {}，情感 {}，关键词：{}）".format(
            i + 1, " / ".join(e["标题"][:3]), e["条数"], e["合计热度"], e["情感"],
            "、".join(e["关键词"]),
        )
        for i, e in enumerate(events)
    ) or "无"
    prompt = (
        "请基于以下数据生成一份舆情简报。\n\n{}\n\n"
        "【聚合出的事件】\n{}\n\n"
        "【高频词】{}\n\n"
        "输出格式：\n"
        "一、总体舆情态势（2-3 句）\n"
        "二、核心热点事件（逐个列出，标注风险等级 高/中/低）\n"
        "三、情绪与高频词解读\n"
        "四、风险提示与建议（3 条以内）\n"
        "要求：只依据给定数据，具体、可读，不要空话。"
    ).format(
        data_digest(rows, label),
        event_txt,
        "、".join("{}（{}）".format(w["word"], w["count"]) for w in words) or "无",
    )
    return chat(
        [{"role": "system", "content": SYSTEM_PROMPT}, {"role": "user", "content": prompt}]
    )


def summarize_event(event):
    """给一个事件聚类结果生成摘要。"""
    prompt = (
        "下面是一组被系统判定为同一事件的热搜条目，请合并成一个事件摘要。\n\n"
        "【相关热搜】\n{}\n\n"
        "输出：1) 事件概述（一句）；2) 来龙去脉（3 句内）；"
        "3) 舆情情绪与风险等级（高/中/低）；4) 关注建议（1-2 条）。"
    ).format("\n".join("- {}".format(t) for t in event.get("标题", [])))
    return chat(
        [{"role": "system", "content": SYSTEM_PROMPT}, {"role": "user", "content": prompt}]
    )


def assistant(question, rows, history=None):
    """问答助手：结合当前热搜数据回答用户提问。"""
    messages = [{"role": "system", "content": SYSTEM_PROMPT}]
    messages.append(
        {
            "role": "system",
            "content": "以下是系统当前抓取到的最新热搜数据，回答时请优先参考：\n\n"
            + data_digest(rows, "最新一轮", max_items=30),
        }
    )
    for role, content in (history or [])[-6:]:
        if role in ("user", "assistant") and content:
            messages.append({"role": role, "content": content})
    messages.append({"role": "user", "content": question})
    return chat(messages, temperature=0.5)
