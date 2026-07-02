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
        "thread_latest": m.get("latest_reply"),  # スレッド最新返信の ts（新着返信の検知用）
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


# ── 箇条書きを Slack ネイティブの rich_text_list（リストタグ）で送る ─────────────
_INLINE_RE = __import__("re").compile(
    r"<@([A-Z0-9]+)(?:\|[^>]*)?>|<!(channel|here|everyone)>"
    r"|<(https?://[^|>]+)(?:\|([^>]*))?>|(https?://[^\s<>]+)")
_BULLET_RE = __import__("re").compile(r"^[ 　]*(?:•|・|-)[ 　]+(.*)$")


def _inline_elements(s: str) -> list[dict]:
    """1行ぶんのテキストを rich_text のインライン要素列へ（メンション/broadcast/リンク/テキスト）。"""
    out, i = [], 0
    for mm in _INLINE_RE.finditer(s):
        if mm.start() > i:
            out.append({"type": "text", "text": s[i:mm.start()]})
        if mm.group(1):
            out.append({"type": "user", "user_id": mm.group(1)})
        elif mm.group(2):
            out.append({"type": "broadcast", "range": mm.group(2)})
        elif mm.group(3):
            el = {"type": "link", "url": mm.group(3)}
            if mm.group(4):
                el["text"] = mm.group(4)
            out.append(el)
        elif mm.group(5):
            out.append({"type": "link", "url": mm.group(5)})
        i = mm.end()
    if i < len(s):
        out.append({"type": "text", "text": s[i:]})
    return out or [{"type": "text", "text": s}]


def _rich_blocks(text: str):
    """箇条書き行(•/・/-)を含むテキストを rich_text ブロック化（section と bullet list を交互に）。
    箇条書きが無ければ None（＝従来どおりプレーンテキスト送信）。失敗時も None でフォールバック。"""
    try:
        lines = (text or "").split("\n")
        if not any(_BULLET_RE.match(ln) for ln in lines):
            return None
        els, sec, lst = [], [], []

        def flush_sec():
            if sec:
                els.append({"type": "rich_text_section",
                            "elements": _inline_elements("\n".join(sec))})
                sec.clear()

        def flush_lst():
            if lst:
                els.append({"type": "rich_text_list", "style": "bullet",
                            "elements": [{"type": "rich_text_section",
                                          "elements": _inline_elements(it)} for it in lst]})
                lst.clear()

        for ln in lines:
            bm = _BULLET_RE.match(ln)
            if bm:
                flush_sec()
                lst.append(bm.group(1))
            else:
                flush_lst()
                sec.append(ln)
        flush_sec()
        flush_lst()
        return [{"type": "rich_text", "elements": els}] if els else None
    except Exception:
        return None


def read_recent(channel_id: str, oldest_ts: float | None = None, limit: int = 200,
                paginate: bool = False, max_pages: int = 10) -> list[dict]:
    """既定は1ページ(最新 limit 件)。paginate=True で next_cursor を辿り窓内を全取得（stall-scan 用＝
    #200 超の古いタスク根を見落とさない）。max_pages で暴走/レート上限を抑え、途中失敗は print して打ち切り。"""
    if FIXTURES:
        fn = _CH_FIXTURE.get(channel_id)
        if not fn:
            return []
        msgs = json.loads((Path(FIXTURES) / fn).read_text(encoding="utf-8"))
        if oldest_ts is not None:
            msgs = [m for m in msgs if m["ts_float"] > oldest_ts]
        msgs = sorted(msgs, key=lambda x: x["ts_float"])
        return msgs if paginate else msgs[-limit:]  # paginate 時は窓内全件（[-limit:] で切らない）
    params = {"channel": channel_id, "limit": min(limit, 200)}
    if oldest_ts is not None:
        params["oldest"] = f"{oldest_ts:.6f}"
    if not paginate:
        res = _api_get("conversations.history", params)
        # ファイルのみ投稿(subtype=file_share・text空)も活動として拾う＝直後の silence 誤爆を防ぐ（監査確定）
        msgs = [_norm_api(m) for m in res.get("messages", [])
                if m.get("subtype") is None or m.get("text") or m.get("files")]
        return sorted(msgs, key=lambda x: x["ts_float"])
    out, cursor, pages = [], None, 0
    while pages < max_pages:
        p = dict(params)
        if cursor:
            p["cursor"] = cursor
        try:
            res = _api_get("conversations.history", p)
        except Exception as e:
            print(f"[source] read_recent paginate stopped at page {pages}: {e}")
            break
        out += [_norm_api(m) for m in res.get("messages", [])
                if m.get("subtype") is None or m.get("text") or m.get("files")]
        cursor = (res.get("response_metadata") or {}).get("next_cursor")
        pages += 1
        if not res.get("has_more") or not cursor:
            break
    return sorted(out, key=lambda x: x["ts_float"])


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
    payload = {"channel": channel_id, "thread_ts": thread_ts, "text": text}
    blocks = _rich_blocks(text)
    if blocks:
        payload["blocks"] = blocks
    return _api_post("chat.postMessage", payload)


def post_message(channel_id: str, text: str) -> dict:
    if FIXTURES or not _TOKEN:
        print(f"[DRY post] ch={channel_id}\n  {text}")
        return {"ok": True, "dry": True}
    payload = {"channel": channel_id, "text": text}
    blocks = _rich_blocks(text)
    if blocks:
        payload["blocks"] = blocks
    return _api_post("chat.postMessage", payload)


def update_message(channel_id: str, ts: str, text: str) -> dict:
    """自分(chiaki)の既存投稿を編集（chat.update）。学習内容を投稿に反映する用。"""
    if FIXTURES or not _TOKEN:
        print(f"[DRY update] ch={channel_id} ts={ts}\n  {text}")
        return {"ok": True, "dry": True}
    payload = {"channel": channel_id, "ts": ts, "text": text}
    blocks = _rich_blocks(text)
    if blocks:
        payload["blocks"] = blocks
    return _api_post("chat.update", payload)
