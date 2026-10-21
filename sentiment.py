# -*- coding: utf-8 -*-
"""轻量中文情感分析：判断一条热搜是 正面 / 中性 / 负面。

基于 snownlp 的情感打分（0~1，越大越正面），阈值划分三档。
"""

POS_THRESHOLD = 0.60
NEG_THRESHOLD = 0.40


def analyze(text, extra=""):
    """返回 (标签, 得分)。

    标签：正面 / 中性 / 负面；得分 0~1，越大越正面。
    同时看标题与简介，简介权重较低，避免长文稀释判断。
    """
    title = (text or "").strip()
    body = (extra or "").strip()
    if not title and not body:
        return "中性", 0.5

    samples = []
    try:
        from snownlp import SnowNLP

        if title:
            samples.append(float(SnowNLP(title).sentiments))
        if body:
            samples.append(float(SnowNLP(body).sentiments))
    except Exception:
        return "中性", 0.5

    if not samples:
        return "中性", 0.5
    score = round(sum(samples) / len(samples), 4)

    if score >= POS_THRESHOLD:
        return "正面", score
    if score <= NEG_THRESHOLD:
        return "负面", score
    return "中性", score
