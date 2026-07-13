"""Notion 書き込みの最小ヘルパ（NOTION_INTEGRATION_TOKEN・urllib・Notion-Version 2022-06-28）。

chiaki-intake が issue（コード対応＝Issue_DB）と rule（言葉のルール＝Rule Registry）を起票する用途。
トークン無し（ローカル/テスト）は DRY 出力。要 Hermes Agent インテグレーションへの DB 共有。
"""
from __future__ import annotations
import json
import os
import re
import time
import urllib.error
import urllib.request

NOTION_VERSION = "2022-06-28"
# Issue_Chiaki_AI_DB（不具合・要望＝Claude Code でのバグ潰しバックログ）
REQUESTS_DB = "0bccce01dd944be4901d95e950a3964c"
# Rule Registry_Hermes Agent__DB（言葉のルール＝用語/レギュレーション/スタイル）。
# ※ parent は database_id（URLスラッグ）。data source id 5f9b0b18… を渡すと404。
RULE_REGISTRY_DB = "e10777d5a7a04ac294273b9e077e1a38"


def _token() -> str:
    return os.environ.get("NOTION_INTEGRATION_TOKEN", "")


# 適正工数_DB（種別ごとの実測レンジ・中央値・回数。compute-baselines が毎晩 実測列を反映）
KOUSU_DB = "5b02889e5af24bd09fcd3b206d43fab6"
# 社内レギュレーション_DB（コンテンツマーケの正本・2026-07-08 戸田「社内のレギュレーションも調整したい」）
COMPANY_REG_DB = "2a1b88bf93264ffcaaf5e6608871b5e0"
# select は既存オプション名のみ送る（未知値は Notion が自動再作成する＝Rule Registry の chiaki 再発事故の教訓）
COMPANY_REG_CATEGORIES = ("用字・表記", "数字・英字", "記号・約物", "文末・語尾", "表現・NG", "体裁・構成")
COMPANY_REG_SCENES = ("社内コミュニケーション", "記事・コンテンツ")


_RETRY_WAITS = (8, 20)  # 一時障害のリトライ間隔（DNS不通・タイムアウト・429・5xx。恒久エラー4xxは即諦める）


def _api(method: str, path: str, body: dict | None = None):
    """Notion API 呼び出し（GET/POST/PATCH）。トークン無・失敗時は None（握りつぶしてログ）。
    一時障害はリトライ（2026-07-09 VPSのDNS一時エラーで夜間の適正工数反映が1晩スキップした実例。
    起票・Issue更新・社内レギュ登録など全Notion経路の底上げ）。"""
    token = _token()
    if not token:
        return None
    data = json.dumps(body, ensure_ascii=False).encode("utf-8") if body is not None else None
    for attempt in range(len(_RETRY_WAITS) + 1):
        req = urllib.request.Request(
            f"https://api.notion.com/v1/{path}", data=data, method=method,
            headers={"Authorization": f"Bearer {token}", "Notion-Version": NOTION_VERSION,
                     "Content-Type": "application/json; charset=utf-8"})
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                return json.loads(r.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            transient = e.code in (429, 500, 502, 503, 504)
            if not transient or attempt >= len(_RETRY_WAITS):
                print(f"[notion _api] {method} {path} failed: {e}")
                return None
            print(f"[notion _api] {method} {path} {e.code} -> {_RETRY_WAITS[attempt]}s後に再試行")
            time.sleep(_RETRY_WAITS[attempt])
        except Exception as e:  # URLError(DNS)・タイムアウト等の一時障害
            if attempt >= len(_RETRY_WAITS):
                print(f"[notion _api] {method} {path} failed: {e}")
                return None
            print(f"[notion _api] {method} {path} {type(e).__name__} -> {_RETRY_WAITS[attempt]}s後に再試行")
            time.sleep(_RETRY_WAITS[attempt])
    return None


def query_database_titles(db_id: str) -> dict:
    """DB の全ページを {title文字列: {"id":..., "props":生プロパティ}} で返す（ページング対応）。
    title プロパティ名は問わず type=='title' の列を採用。失敗/未共有時は {}。"""
    out, cursor = {}, None
    while True:
        body = {"page_size": 100}
        if cursor:
            body["start_cursor"] = cursor
        res = _api("POST", f"databases/{db_id}/query", body)
        if not res:
            break
        for pg in res.get("results", []):
            props = pg.get("properties", {})
            title = ""
            for v in props.values():
                if v.get("type") == "title":
                    title = "".join(t.get("plain_text", "") for t in v.get("title", []))
                    break
            if title:
                out[title] = {"id": pg.get("id"), "props": props}
        if not res.get("has_more"):
            break
        cursor = res.get("next_cursor")
    return out


def create_company_regulation(rule: str, content: str, category: str = "用字・表記",
                              wrong: str = "", right: str = "", basis: str = "",
                              scenes: list | None = None) -> str | None:
    """社内レギュレーション_DB へ1件登録（種別=Lipple・ステータス=有効固定）。URL を返す。
    category/scenes は既存オプションのみ許可（未知値は既定へ丸める＝strayオプション防止）。"""
    if category not in COMPANY_REG_CATEGORIES:
        category = "用字・表記"
    scenes = [s for s in (scenes or []) if s in COMPANY_REG_SCENES] or list(COMPANY_REG_SCENES)
    props = {
        "ルール": {"title": [{"text": {"content": (rule or "（無題）").strip()[:200]}}]},
        "ルール内容": _rt(content),
        "カテゴリ": {"select": {"name": category}},
        "種別": {"select": {"name": "Lipple"}},
        "ステータス": {"select": {"name": "有効"}},
        "適用シーン": {"multi_select": [{"name": s} for s in scenes]},
    }
    if wrong:
        props["誤例"] = _rt(wrong)
    if right:
        props["正例"] = _rt(right)
    if basis:
        props["根拠"] = _rt(basis)
    return _create_page(COMPANY_REG_DB, props, "notion-company-reg")


ISSUE_STATUSES = ("未対応", "対応中", "レビュー待ち", "完了")


def _page_id_from_url(url: str) -> str:
    m = re.search(r"([0-9a-f]{32})", (url or "").replace("-", ""))
    return m.group(1) if m else ""


def update_issue(url: str, status: str = "", branch: str = "") -> bool:
    """Issue_DB のページを URL 指定で更新（ステータス/ブランチ）。履歴管理の正本＝Issue_DB
    （2026-07-08 戸田「何を実装したかの履歴管理・二重チェック」）。status は既存オプションのみ。"""
    pid = _page_id_from_url(url)
    if not pid:
        return False
    props: dict = {}
    if status and status in ISSUE_STATUSES:
        props["ステータス"] = {"select": {"name": status}}
    if branch:
        props["ブランチ"] = _rt(branch)
    return update_page_props(pid, props) if props else False


def update_page_props(page_id: str, props: dict) -> bool:
    """既存ページのプロパティを更新（PATCH）。props は Notion API 形式。成功で True。"""
    if not _token():
        print(f"[DRY notion-update] page={page_id[:8]} props={list(props)}")
        return False
    res = _api("PATCH", f"pages/{page_id}", {"properties": props})
    return bool(res)


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
                         target_agent: str = "Chiaki AI", reporter: str = "Chiaki AI",
                         status: str = "未承認") -> str | None:
    """Rule（言葉のルール）を1件起票。作成ページ URL を返す（DRY/失敗時 None）。
    rule_kind ∈ 用語|レギュレーション|スタイル ／ status ∈ 承認(機械ルール)|未承認(判断ルール)|却下
    ／ target_agent・reporter は「Chiaki AI」（2026-07-03 スキーマ整理済み。小文字 chiaki を送ると
    Notion が stray オプションを自動再作成してしまうので使わない）。"""
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
