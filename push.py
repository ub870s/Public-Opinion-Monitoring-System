# -*- coding: utf-8 -*-
"""告警推送：把高危预警推送到飞书 / 企业微信 / 钉钉 / 通用 Webhook。

四种渠道的报文格式不同，这里统一封装成 send(channel, url, title, text)：
  - feishu   : {"msg_type":"text","content":{"text":"..."}}
  - wecom    : {"msgtype":"text","text":{"content":"..."}}
  - dingtalk : {"msgtype":"text","text":{"content":"..."}}
  - generic  : {"title":"...","text":"..."}

不引入第三方依赖，用 urllib 直连；超时 10 秒，返回 (ok, message)。
"""

import json
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

CHANNELS = (
    ("feishu", "飞书机器人"),
    ("wecom", "企业微信机器人"),
    ("dingtalk", "钉钉机器人"),
    ("generic", "通用 Webhook"),
)
CHANNEL_NAMES = dict(CHANNELS)


def build_payload(channel, title, text):
    content = (title + "\n\n" + text).strip()
    if channel == "feishu":
        return {"msg_type": "text", "content": {"text": content}}
    if channel in ("wecom", "dingtalk"):
        return {"msgtype": "text", "text": {"content": content}}
    return {"title": title, "text": text}


def send(channel, url, title, text):
    """发送一条推送，返回 (ok, message)。"""
    channel = channel or "generic"
    url = (url or "").strip()
    if not url:
        return False, "未配置 Webhook 地址"
    if not url.startswith(("http://", "https://")):
        return False, "Webhook 地址必须以 http:// 或 https:// 开头"

    body = json.dumps(build_payload(channel, title, text), ensure_ascii=False).encode("utf-8")
    req = Request(
        url,
        data=body,
        method="POST",
        headers={"Content-Type": "application/json; charset=utf-8"},
    )
    try:
        with urlopen(req, timeout=10) as r:
            raw = r.read().decode("utf-8", "ignore")
            status = r.status
    except HTTPError as e:
        raw = e.read().decode("utf-8", "ignore")
        return False, "HTTP {}：{}".format(e.code, raw[:200])
    except URLError as e:
        return False, "网络错误：{}".format(e.reason)
    except Exception as e:
        return False, "发送失败：{}".format(e)

    try:
        data = json.loads(raw)
    except Exception:
        data = {}

    # 各渠道成功标志位不同，做一次统一判断
    code = data.get("code", data.get("errcode", data.get("StatusCode", 0)))
    if channel == "feishu":
        ok = data.get("code", 0) in (0, None) or data.get("StatusCode") == 0
    elif channel in ("wecom", "dingtalk"):
        ok = data.get("errcode", 0) == 0
    else:
        ok = 200 <= status < 300
    if not ok:
        return False, "渠道返回错误：{}".format(raw[:200])
    return True, "发送成功（HTTP {}，code={}）".format(status, code)
