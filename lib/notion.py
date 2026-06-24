"""Notion 書き込みの最小ヘルパ（NOTION_INTEGRATION_TOKEN・urllib・Notion-Version 2022-06-28）。

chiaki-intake が issue（コード対応＝Issue_DB）と rule（言葉のルール＝Rule Registry）を起票する用途。
トークン無し（ローカル/テスト）は DRY 出力。要 Hermes Agent インテグレーションへの DB 共有。
"""
from __future__ import annotations
import json
import os
import urllib.request

NOTION_VERSION = "2022-06-28"
# Issue_Chiaki_AI_DB（不具合・要望＝Claude Code でのバグ潰しバックログ）
REQUESTS_DB = "0bccce01dd944be4901d95e950a3964c"
# Rule Registry_Hermes Agent__DB（言葉のルール＝用語/レギュレーション/スタイル）。
# ※ parent は database_id（URLスラッグ）。data source id 5f9b0b18… を渡すと404。
RULE_REGISTRY_DB = "e10777d5a7a04ac294273b9e077e1a38"


def _token() -> str:
    return os.environ.get("NOTION_INTEGRATION_TOKEN", "")


def _prop_summary(v: dict):
    if "title" in v:
        return v["title"][0]["text"]["content"]
    if "rich_text" in v:
        return v["rich_text"][0]["text"]["content"][:50]
    if "select" in v:
        return v["select"]["name"]
    if "url" in v:
        return v["url"]
    return "?"


def _create_page(db_id: str, props: dict, dry_label: str) -> str | None:
    token = _token()
    if not token:
        print(f"[DRY {dry_label}] db={db_id[:8]} | "
              + " | ".join(f"{k}={_prop_summary(val)}" for k, val in props.items()))
        return None
    body = {"parent": {"database_id": db_id}, "properties": props}
    req = urllib.request.Request(
        "https://api.notion.com/v1/pages",
        data=json.dumps(body, ensure_ascii=False).encode("utf-8"), method="POST",
        headers={"Authorization": f"Bearer {token}", "Notion-Version": NOTION_VERSION,
                 "Content-Type": "application/json; charset=utf-8"})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.loads(r.read().decode("utf-8")).get("url")
    except Exception as e:  # 未共有(404)・スキーマ差異などは握りつぶしてログのみ
        print(f"[{dry_label}] failed: {e}")
        return None


def _rt(s: str) -> dict:
    return {"rich_text": [{"text": {"content": (s or "").strip()[:1900]}}]}


def create_request(summary: str, detail: str, slack_url: str = "",
                   channel_label: str = "", kind: str = "") -> str | None:
    """Issue（不具合・要望）を1件 未対応 で起票。作成ページ URL を返す（DRY/失敗時 None）。
    kind ∈ バグ|変更|新機能|その他（任意・既定は未設定）。"""
    props = {
        "要約": {"title": [{"text": {"content": (summary or "（無題）").strip()[:200]}}]},
        "ステータス": {"select": {"name": "未対応"}},
        "詳細": _rt(detail),
    }
    if slack_url:
        props["Slackリンク"] = {"url": slack_url}
    if channel_label:
        props["チャンネル"] = {"select": {"name": channel_label}}
    if kind:
        props["種別"] = {"select": {"name": kind}}
    return _create_page(REQUESTS_DB, props, "notion-issue")


def create_rule_registry(summary: str, detail: str, rule_kind: str, slack_url: str = "",
                         wrong: str = "", right: str = "", basis: str = "",
                         target_agent: str = "chiaki", reporter: str = "chiaki",
                         status: str = "未承認") -> str | None:
    """Rule（言葉のルール）を1件起票。作成ページ URL を返す（DRY/失敗時 None）。
    rule_kind ∈ 用語|レギュレーション|スタイル ／ status ∈ 承認(機械ルール)|未承認(判断ルール)|却下
    ／ target_agent・reporter は live スキーマ準拠で小文字 chiaki。"""
    props = {
        "要約": {"title": [{"text": {"content": (summary or "（無題）").strip()[:200]}}]},
        "種別": {"select": {"name": rule_kind}},
        "詳細": _rt(detail),
        "ステータス": {"select": {"name": status}},
        "起票者": {"select": {"name": reporter}},
        "対象エージェント": {"select": {"name": target_agent}},
    }
    if wrong:
        props["誤例"] = _rt(wrong)
    if right:
        props["正例"] = _rt(right)
    if basis:
        props["根拠"] = _rt(basis)
    if slack_url:
        props["起票元"] = {"url": slack_url}
    return _create_page(RULE_REGISTRY_DB, props, "notion-rule")
