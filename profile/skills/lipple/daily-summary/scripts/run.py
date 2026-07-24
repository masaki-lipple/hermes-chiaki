#!/usr/bin/env python3
"""daily-summary（R4-1・2026-07-24 戸田「R4-1いこうか」＝Issue「1. R4-1 日次サマリ」）:
1日の動きを実行台帳・裁定記録・検知・LLM計測から決定論で集計し、夕方#8902へ1本で投稿する。
完全決定論（LLM非起動）＝数字の誤報ゼロ。cron: 40 18 * * 1-5。"""
from __future__ import annotations

import datetime as dt
import os
import sys
from collections import Counter
from pathlib import Path
sys.path.insert(0, os.environ.get("HERMES_LIB") or str(Path(__file__).resolve().parents[5]))
from lib import ledger, runtime, source  # noqa: E402

JST = dt.timezone(dt.timedelta(hours=9))
STATE = "daily_summary.json"
ST_JP = {"handled": "処理済み", "skipped": "スキップ", "failed": "失敗", "received": "受付中",
         "replied": "応答済み", "queued": "キュー投入", "deferred": "保留", "ruled": "裁定済み"}


def _day_start(now: float) -> float:
    d = dt.datetime.fromtimestamp(now, JST)
    return d.replace(hour=0, minute=0, second=0, microsecond=0).timestamp()


def build(now: float) -> str:
    d0 = _day_start(now)
    lines = []

    # 受付・Codex（実行台帳＝きょう初めて現れたidを、最終状態で数える）
    events: dict = {}
    for r in runtime.read_jsonl(ledger.FILE):
        if r.get("id"):
            events.setdefault(r["id"], []).append(r)
    intake_st, codex_st = Counter(), Counter()
    for es in events.values():
        first = min(float(r.get("at") or 0) for r in es)
        if first < d0:
            continue
        merged: dict = {}
        for r in sorted(es, key=lambda r: float(r.get("at") or 0)):
            merged.update({k: v for k, v in r.items() if v is not None})
        st = merged.get("status") or "received"
        if merged.get("owner") == "intake":
            intake_st[st] += 1
        elif merged.get("owner") == "codex":
            codex_st[st] += 1
    if intake_st:
        detail = "・".join(f"{ST_JP.get(s, s)}{n}" for s, n in intake_st.most_common())
        lines.append(f"• 受付: {sum(intake_st.values())}件（{detail}）")

    # 裁定（rulings.jsonl のきょうの行）
    verdicts = Counter(r.get("verdict") for r in runtime.read_jsonl("rulings.jsonl")
                       if float(r.get("ts") or 0) >= d0)
    vparts = []
    for key, label in (("go", "GO"), ("interpret", "文面修正で実行"), ("reject", "却下"),
                       ("expired", "自動失効"), ("completed", "完了確認"),
                       ("unactionable", "実行不能クローズ"), ("gone", "対象消失"),
                       ("already_fixed", "修正済みクローズ")):
        if verdicts.get(key):
            vparts.append(f"{label}{verdicts[key]}")
    if vparts:
        lines.append(f"• 裁定: {'・'.join(vparts)}")

    if codex_st:
        detail = "・".join(f"{ST_JP.get(s, s)}{n}" for s, n in codex_st.most_common())
        lines.append(f"• Codex対話: {sum(codex_st.values())}件（{detail}）")

    # 検知と提案（findings のきょうの行・提案=裁定台帳にきょう作られた項目）
    kinds = Counter(f.get("kind") for f in runtime.read_jsonl("findings.jsonl")
                    if float(f.get("ts") or 0) >= d0)
    kj = {"typo": "誤字", "notation": "表記", "stall": "停滞"}
    if kinds:
        lines.append("• 検知: " + "・".join(f"{kj.get(k, k)}{n}" for k, n in kinds.most_common()))
    pend = runtime.load_json("pending_approvals.json", {"items": {}}).get("items", {})
    new_props = sum(1 for k in pend if k.replace(".", "").isdigit() and float(k) >= d0)
    npend = sum(1 for v in pend.values() if v.get("status") == "pending")
    nwait = sum(1 for v in pend.values() if v.get("status") == "awaiting_completion")
    if new_props:
        lines.append(f"• 新しい提案: {new_props}件を#8902へ")
    if npend or nwait:
        lines.append(f"• いまの残り: 裁定待ち{npend}件・修正の完了待ち{nwait}件")

    # LLM使用（R5計測の1行版）
    usage = [r for r in runtime.read_jsonl("llm_usage.jsonl") if float(r.get("ts") or 0) >= d0]
    if usage:
        per = Counter(r.get("model") for r in usage)
        lines.append(f"• LLM呼び出し: {len(usage)}回（"
                     + "・".join(f"{m}={n}" for m, n in per.most_common()) + "）")

    day = dt.datetime.fromtimestamp(now, JST).strftime("%-m月%-d日")
    if not lines:
        return f"きょう（{day}）は受付・裁定・検知の動きはありませんでした。"
    return f"きょう（{day}）のまとめです。\n\n" + "\n".join(lines)


def main() -> None:
    now = runtime.now_ts()
    if not runtime.is_jp_workday(now):
        print("[daily-summary] SILENT holiday/weekend")
        return
    today = dt.datetime.fromtimestamp(now, JST).strftime("%Y-%m-%d")
    st = runtime.load_json(STATE, {})
    if st.get("date") == today:
        print("[daily-summary] skip: 投稿済み")
        return
    text = build(now)
    posted = source.post_message(runtime.CH_CHIAKI_MGMT, f"<@{runtime.CHIAKI_SELF}>\n{text}")
    if isinstance(posted, dict) and posted.get("ts"):
        runtime.save_json(STATE, {"date": today, "ts": posted["ts"]})
        print("[daily-summary] posted")
    else:
        print("[daily-summary] post failed（状態は進めない）")


if __name__ == "__main__":
    main()
