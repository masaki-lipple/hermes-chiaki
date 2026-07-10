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
        # リアクション名のリスト（例 ["memo","kanryo"]）＝#a027 の起票/完了スタンプ検知用（Phase0-1）
        "reactions": [r.get("name", "") for r in (m.get("reactions") or [])],
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


# ※旧 _rich_blocks（箇条書きを rich_text_list ブロックへ変換）は 2026-07-03 に廃止。
#   section と list の間の空行が表示上消える＝生テキストは正しいのに見た目が詰まる不具合の真因で、
#   テキスト層の修正（空行の自動挿入）では直らなかった（戸田「抜本的解消をしたい」）。
#   以後は常にプレーンテキスト送信＝書いたとおりに表示される（チームの投稿と同じ・規約の「行頭•」表記）。


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


def list_bot_channels() -> list[dict]:
    """bot が参加しているチャンネル一覧 [{id,name}]（users.conversations・ページング対応）。
    新しいクライアントチャンネルに bot を招待するだけで台帳等の観測対象に入る（ゼロコンフィグ）。"""
    if FIXTURES:
        return [{"id": cid, "name": fn.split(".")[0]} for cid, fn in _CH_FIXTURE.items()]
    out, cursor = [], None
    while True:
        params = {"types": "public_channel,private_channel", "exclude_archived": "true", "limit": 200}
        if cursor:
            params["cursor"] = cursor
        res = _api_get("users.conversations", params)
        out += [{"id": c.get("id"), "name": c.get("name", "")} for c in res.get("channels", [])]
        cursor = (res.get("response_metadata") or {}).get("next_cursor")
        if not cursor:
            break
    return out


_USER_NAME_CACHE: dict = {}


def user_display_name(user_id: str) -> str:
    """users.info の real_name（プロセス内キャッシュ・失敗時は空文字）。
    新しいワーカーch対応＝IDマップに無い人を「担当者」呼ばわりしない（2026-07-10 戸田指摘）。"""
    if not user_id or FIXTURES or not _TOKEN:
        return ""
    if user_id in _USER_NAME_CACHE:
        return _USER_NAME_CACHE[user_id]
    res = _api_get("users.info", {"user": user_id})
    u = res.get("user") or {}
    name = (u.get("profile") or {}).get("real_name") or u.get("real_name") or ""
    _USER_NAME_CACHE[user_id] = name
    return name


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


def _ensure_mention(channel_id: str, text: str) -> str:
    """#8902（管理ch）宛の投稿が冒頭メンションで始まらない場合、規約（chiaki の自発投稿は
    必ず冒頭に <@戸田> か セルフメンション）に沿ってセルフメンションを自動付与する。
    手書き報告・続報の付け忘れを人の注意でなく仕組みで防ぐ（2026-07-03 戸田指摘＝
    続報2件でメンション漏れ。コード生成の投稿はテンプレートで担保済みのため実質無影響）。"""
    if not (text or "").lstrip().startswith("<@"):
        try:
            from lib import runtime
            if channel_id == runtime.CH_CHIAKI_MGMT:
                return f"<@{runtime.CHIAKI_SELF}>\n{text}"
        except Exception:
            pass
    return text


def _blank_before_bullets(text: str) -> str:
    """本文行の直後に箇条書きが続く場合、Slack上で詰まらないよう空行を1つ入れる。
    投稿の出口（post_message/post_thread_reply/update_message）で全投稿に適用＝
    手書き・どのスキル経由でも同じ整形が掛かる（2026-07-03 戸田「抜本的解消をしたい」。
    スキル個別のテンプレ修正では手書き投稿で再発した）。コードフェンス内は触らない。"""
    import re as _re
    out: list[str] = []
    in_fence = False
    for line in (text or "").split("\n"):
        if line.strip().startswith("```"):
            in_fence = not in_fence
        is_bullet = bool(_re.match(r"^[ \t　]*(?:•|・|-)[ \t　]+\S", line))
        prev_is_bullet = bool(out and _re.match(r"^[ \t　]*(?:•|・|-)[ \t　]+\S", out[-1]))
        if is_bullet and not in_fence and out and out[-1].strip() and not prev_is_bullet:
            out.append("")
        out.append(line)
    return "\n".join(out)


def post_thread_reply(channel_id: str, thread_ts: str, text: str) -> dict:
    text = _blank_before_bullets(_ensure_mention(channel_id, text))
    if FIXTURES or not _TOKEN:
        print(f"[DRY post] ch={channel_id} thread={thread_ts}\n  {text}")
        return {"ok": True, "dry": True}
    return _api_post("chat.postMessage",
                     {"channel": channel_id, "thread_ts": thread_ts, "text": text})


def post_message(channel_id: str, text: str) -> dict:
    text = _blank_before_bullets(_ensure_mention(channel_id, text))
    if FIXTURES or not _TOKEN:
        print(f"[DRY post] ch={channel_id}\n  {text}")
        return {"ok": True, "dry": True}
    return _api_post("chat.postMessage", {"channel": channel_id, "text": text})


def update_message(channel_id: str, ts: str, text: str) -> dict:
    """自分(chiaki)の既存投稿を編集（chat.update）。学習内容を投稿に反映する用。
    blocks は常に空で送る＝旧 rich_text ブロック付き投稿もプレーンテキスト表示へ揃える。"""
    text = _blank_before_bullets(_ensure_mention(channel_id, text))
    if FIXTURES or not _TOKEN:
        print(f"[DRY update] ch={channel_id} ts={ts}\n  {text}")
        return {"ok": True, "dry": True}
    return _api_post("chat.update", {"channel": channel_id, "ts": ts, "text": text, "blocks": []})
