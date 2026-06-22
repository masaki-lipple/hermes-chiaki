"""Slack ソース抽象。

- ローカル検証: 環境変数 HERMES_FIXTURES=<dir> で fixtures JSON を読む（投稿は dry-run でログ）。
- 本番(box): SLACK_BOT_TOKEN で Slack Web API を urllib 直叩き（cron --no-agent から LLM 非経由）。
  ※ MCP が cron セッションで使えるならそちらでも可。実機検証で確定（plan のリスク参照）。

返すメッセージは slacklib と同じ正規化 dict。
"""
from __future__ import annotations
import json
import os
import time
import urllib.parse
import urllib.request
from pathlib import Path

FIXTURES = os.environ.get("HERMES_FIXTURES")
_TOKEN = os.environ.get("SLACK_BOT_TOKEN", "")
_API = "https://slack.com/api/"

_CH_FIXTURE = {
    "C09U4T1BBU0": "yu-pdca.json",
    "C045C1ZBX26": "nichiji-jidoor.json",
}


def _jst(ts: float) -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S JST", time.gmtime(ts + 9 * 3600))


def _norm_api(m: dict) -> dict:
    ts = m.get("ts", "0")
    return {
        "ts": ts, "ts_float": float(ts), "datetime": _jst(float(ts)),
        "user_id": m.get("user") or m.get("bot_id") or "",
        "user_name": (m.get("user_profile") or {}).get("real_name", ""),
        "text": m.get("text", ""),
        "thread_replies": (m.get("reply_count") if m.get("reply_count") else None),
        "thread_latest": None,
        "has_files": bool(m.get("files")),
    }


def _api_get(method: str, params: dict) -> dict:
    url = _API + method + "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {_TOKEN}"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read().decode())


def _api_post(method: str, payload: dict) -> dict:
    data = json.dumps(payload).encode()
    req = urllib.request.Request(_API + method, data=data, headers={
        "Authorization": f"Bearer {_TOKEN}", "Content-Type": "application/json; charset=utf-8"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read().decode())


def read_recent(channel_id: str, oldest_ts: float | None = None, limit: int = 200) -> list[dict]:
    if FIXTURES:
        fn = _CH_FIXTURE.get(channel_id)
        if not fn:
            return []
        msgs = json.loads((Path(FIXTURES) / fn).read_text(encoding="utf-8"))
        if oldest_ts is not None:
            msgs = [m for m in msgs if m["ts_float"] > oldest_ts]
        return sorted(msgs, key=lambda x: x["ts_float"])[-limit:]
    params = {"channel": channel_id, "limit": min(limit, 200)}
    if oldest_ts is not None:
        params["oldest"] = f"{oldest_ts:.6f}"
    res = _api_get("conversations.history", params)
    msgs = [_norm_api(m) for m in res.get("messages", []) if m.get("subtype") is None or m.get("text")]
    return sorted(msgs, key=lambda x: x["ts_float"])


def read_thread(channel_id: str, thread_ts: str) -> list[dict]:
    """スレッド返信（根を含む）。stall の human_replies 算出に使う。"""
    if FIXTURES:
        return []  # fixture にスレッド本文は無い（root の thread_replies で代用）
    res = _api_get("conversations.replies", {"channel": channel_id, "ts": thread_ts, "limit": 200})
    return [_norm_api(m) for m in res.get("messages", [])]


def human_replies(channel_id: str, root: dict, bot_user_ids: set[str]) -> int | None:
    """§3.8: bot を除いた人間の返信数。fixtures では算出不能 → None（呼び側が thread_replies 代用）。"""
    if FIXTURES:
        return None
    thread_ts = root["ts"]
    replies = read_thread(channel_id, thread_ts)
    # 先頭(root)を除き、bot 著者を除外
    return sum(1 for m in replies[1:] if m["user_id"] not in bot_user_ids)


def post_thread_reply(channel_id: str, thread_ts: str, text: str) -> dict:
    if FIXTURES or not _TOKEN:
        print(f"[DRY post] ch={channel_id} thread={thread_ts}\n  {text}")
        return {"ok": True, "dry": True}
    return _api_post("chat.postMessage", {"channel": channel_id, "thread_ts": thread_ts,
                                          "text": text})


def post_message(channel_id: str, text: str) -> dict:
    if FIXTURES or not _TOKEN:
        print(f"[DRY post] ch={channel_id}\n  {text}")
        return {"ok": True, "dry": True}
    return _api_post("chat.postMessage", {"channel": channel_id, "text": text})
