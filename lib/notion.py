"""Notion 書き込みの最小ヘルパ（NOTION_INTEGRATION_TOKEN・urllib・Notion-Version 2022-06-28）。

chiaki-tuning が hard（コード対応が要る）指示を『Chiaki｜変更・バグ リクエスト』DB へ起票する用途。
トークン無し（ローカル/テスト）は DRY 出力。要 Hermes Agent インテグレーションへの DB 共有。
"""
from __future__ import annotations
import json
import os
import urllib.request

NOTION_VERSION = "2022-06-28"
# Chiaki｜変更・バグ リクエスト（Claude Code でのバグ潰しバックログ）
REQUESTS_DB = "0bccce01dd944be4901d95e950a3964c"


def _token() -> str:
    return os.environ.get("NOTION_INTEGRATION_TOKEN", "")


def create_request(summary: str, detail: str, slack_url: str = "", channel_label: str = "") -> str | None:
    """変更・バグ リクエストを1件起票。作成ページの URL を返す（DRY/失敗時 None）。"""
    props = {
        "要約": {"title": [{"text": {"content": (summary or "（無題）").strip()[:200]}}]},
        "ステータス": {"select": {"name": "未対応"}},
        "詳細": {"rich_text": [{"text": {"content": (detail or "").strip()[:1900]}}]},
    }
    if slack_url:
        props["Slackリンク"] = {"url": slack_url}
    if channel_label:
        props["チャンネル"] = {"select": {"name": channel_label}}
    token = _token()
    if not token:
        print(f"[DRY notion-request] {summary!r} ch={channel_label} url={slack_url}")
        return None
    body = {"parent": {"database_id": REQUESTS_DB}, "properties": props}
    req = urllib.request.Request(
        "https://api.notion.com/v1/pages",
        data=json.dumps(body, ensure_ascii=False).encode("utf-8"), method="POST",
        headers={"Authorization": f"Bearer {token}", "Notion-Version": NOTION_VERSION,
                 "Content-Type": "application/json; charset=utf-8"})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.loads(r.read().decode("utf-8")).get("url")
    except Exception as e:  # 未共有(404)・スキーマ差異などは握りつぶしてログのみ
        print(f"[notion-request] failed: {e}")
        return None
