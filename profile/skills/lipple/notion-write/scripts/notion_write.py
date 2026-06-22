#!/usr/bin/env python3
"""Notion タスクページの補完メタだけを書くガード付き writer（§4）。
status/sync_source は機械的に拒否。許可は カテゴリー/工数/優先度 のみ。
  notion_write.py <page_id> '<json props>' [--no-overwrite-category]
トークン無し/ドライ時は検証＋ドライ出力（404ブロッカー中もガードを検証できる）。
"""
import argparse
import json
import os
import re
import sys
import urllib.request

# 絶対禁止（別名・大小無視）
DENY = re.compile(r"^(status|sync[_ ]?source|ステータス|sync)$", re.IGNORECASE)
# 許可（別名）
ALLOW = {"category", "カテゴリー", "カテゴリ", "effort", "工数", "priority", "優先度"}
CATEGORY_KEYS = {"category", "カテゴリー", "カテゴリ"}


def validate(props: dict):
    bad = [k for k in props if DENY.match(k.strip())]
    if bad:
        raise SystemExit(f"REJECTED: 禁止プロパティへの書き込み {bad}（status/sync_source は不可侵）")
    notallow = [k for k in props if k not in ALLOW]
    if notallow:
        raise SystemExit(f"REJECTED: 許可外プロパティ {notallow}（許可は カテゴリー/工数/優先度 のみ）")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("page_id")
    ap.add_argument("props")
    ap.add_argument("--no-overwrite-category", action="store_true")
    args = ap.parse_args()
    props = json.loads(args.props)
    validate(props)  # ここで禁止/許可外なら即終了（API 呼ぶ前）

    token = os.environ.get("NOTION_INTEGRATION_TOKEN", "")
    if not token:
        print(f"[DRY notion-write] page={args.page_id} props={props} "
              f"(no-overwrite-category={args.no_overwrite_category})  ✓ガード通過")
        return
    # 実書き込み（Notion API）。プロパティ型のマッピングは配備時にタスクDBスキーマで確定。
    # ここでは「ガードを通った props だけが API に届く」ことを保証するのが主目的。
    body = {"properties": _to_notion_props(props)}
    req = urllib.request.Request(
        f"https://api.notion.com/v1/pages/{args.page_id}",
        data=json.dumps(body).encode(), method="PATCH",
        headers={"Authorization": f"Bearer {token}", "Notion-Version": "2022-06-28",
                 "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=30) as r:
        print("notion patch ok:", r.status)


def _to_notion_props(props: dict) -> dict:
    """簡易マッピング（配備時にタスクDBの実プロパティ名/型へ合わせる）。"""
    out = {}
    for k, v in props.items():
        if k in CATEGORY_KEYS:
            out[k] = {"select": {"name": str(v)}}
        elif k in ("effort", "工数"):
            out[k] = {"number": float(v)}
        elif k in ("priority", "優先度"):
            out[k] = {"select": {"name": str(v)}}
    return out


if __name__ == "__main__":
    main()
