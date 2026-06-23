#!/usr/bin/env python3
"""Notion の 用語辞書_DB ＋ レギュレーション_DB → state/notation_rules.json を生成（§3.5 Layer1 の物差し）。
box で daily 実行＋配備時に1回。NOTION_INTEGRATION_TOKEN は profile .env or 環境変数から。
ローカルfixturesを置き換える本番の rules ソース。標準ライブラリのみ。
"""
from __future__ import annotations
import json
import os
import re
import urllib.request
from pathlib import Path

YOUGO_DB = "876b0c67-4a6f-4d09-ba83-6c9ca822c7a3"   # 用語辞書_DB（固有名詞・誤変換）
REG_DB = "2a1b88bf-9326-4ffc-aaf5-e6608871b5e0"     # レギュレーション_DB（誤例→正例）
ACRONYMS = ["SNS", "EC", "SEO", "AI", "CV", "CVR", "KPI", "URL", "HP", "DM", "FAQ", "HR"]
_SPLIT = re.compile(r"[、,／/]\s*")


def _profile_dir() -> Path:
    return Path(os.environ.get("HERMES_PROFILE_DIR")
                or Path.home() / ".hermes/profiles/management")


def _token() -> str:
    t = os.environ.get("NOTION_INTEGRATION_TOKEN")
    if t:
        return t
    env = _profile_dir() / ".env"
    if env.exists():
        for ln in env.read_text(encoding="utf-8").splitlines():
            if ln.startswith("NOTION_INTEGRATION_TOKEN="):
                return ln.split("=", 1)[1].strip()
    raise SystemExit("NOTION_INTEGRATION_TOKEN が見つかりません（環境変数 or profile .env）")


def _query(db_id: str, token: str) -> list[dict]:
    H = {"Authorization": f"Bearer {token}", "Notion-Version": "2022-06-28",
         "Content-Type": "application/json"}
    rows, cursor = [], None
    while True:
        body = {"page_size": 100}
        if cursor:
            body["start_cursor"] = cursor
        req = urllib.request.Request(f"https://api.notion.com/v1/databases/{db_id}/query",
                                     data=json.dumps(body).encode(), headers=H, method="POST")
        res = json.load(urllib.request.urlopen(req, timeout=30))
        rows += res.get("results", [])
        if not res.get("has_more"):
            break
        cursor = res.get("next_cursor")
    return rows


def _txt(props: dict, name: str) -> str:
    p = props.get(name, {})
    arr = p.get("title") or p.get("rich_text") or []
    return "".join(t.get("plain_text", "") for t in arr).strip()


def _sel(props: dict, name: str) -> str:
    return ((props.get(name, {}) or {}).get("select") or {}).get("name", "")


def _split(s: str) -> list[str]:
    return [x.strip() for x in _SPLIT.split(s) if x.strip()]


def build(token: str) -> dict:
    terms = []
    for pg in _query(YOUGO_DB, token):
        p = pg["properties"]
        official = _txt(p, "正式表記")
        if not official:
            continue
        terms.append({
            "official": official,
            "aliases": _split(_txt(p, "別称")),
            "misconversions": _split(_txt(p, "誤変換パターン")),
            "category": _sel(p, "カテゴリ"),
            "minutes": _txt(p, "議事録表記"),
        })
    style_rules = []
    for pg in _query(REG_DB, token):
        p = pg["properties"]
        rule = _txt(p, "ルール")
        wrongs = _split(_txt(p, "誤例"))
        rights = _split(_txt(p, "正例"))
        if not wrongs:
            continue
        for i, w in enumerate(wrongs):
            right = rights[i] if i < len(rights) else (rights[0] if rights else "")
            style_rules.append({"rule": rule, "wrong": w, "right": right})
    return {"_source": "Notion 用語辞書_DB + レギュレーション_DB (sync_notation.py)",
            "terms": terms, "acronyms": ACRONYMS, "style_rules": style_rules}


def main():
    token = _token()
    rules = build(token)
    out = _profile_dir() / "state" / "notation_rules.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(rules, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"terms={len(rules['terms'])} style_rules={len(rules['style_rules'])} "
          f"acronyms={len(rules['acronyms'])} -> {out}")


if __name__ == "__main__":
    main()
