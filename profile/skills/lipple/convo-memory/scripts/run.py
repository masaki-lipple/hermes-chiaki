#!/usr/bin/env python3
"""convo-memory（会話コアv2 Phase C）: 会話台帳から「決定・好み・注意点」を長期記憶に蒸留する夜間バッチ。
cron 例: 10 21 * * 1-5。判断は GPT 5.5・検証と保存は決定論。失敗時は旧 notes を温存（記憶を失わない）。
"""
from __future__ import annotations

import datetime as dt
import json
import os
import re
import sys
from pathlib import Path
sys.path.insert(0, os.environ.get("HERMES_LIB") or str(Path(__file__).resolve().parents[5]))
from lib import convo, runtime  # noqa: E402

JST = dt.timezone(dt.timedelta(hours=9))
KINDS = ("決定", "好み", "注意")


def _valid_notes(raw, fallback: list) -> list | None:
    """蒸留結果の検証（決定論ゲート）。壊れていたら None＝旧 notes 温存。"""
    if not isinstance(raw, list):
        return None
    outp = []
    today = dt.datetime.now(JST).strftime("%Y-%m-%d")
    for n in raw:
        if not isinstance(n, dict):
            continue
        note = (n.get("note") or "").strip()
        if not note:
            continue
        outp.append({"note": note[:120],
                     "kind": n.get("kind") if n.get("kind") in KINDS else "注意",
                     "since": (n.get("since") or today)[:10]})
    if not outp and fallback:
        return None  # 全消しの提案は信じない（記憶の全損防止）
    return outp[:convo.NOTES_CAP]


def distill() -> str:
    mem = convo.memory()
    ledger = mem.get("ledger") or []
    notes = mem.get("notes") or []
    last = float(mem.get("distilled_ts") or 0)
    fresh = [e for e in ledger if float(e.get("ts") or 0) > last]
    if not fresh:
        return "skip: 新しい会話なし（LLM非起動）"
    lines = "\n".join(
        f"- [{e.get('dt', '')}] 戸田さん「{(e.get('said') or '')[:120]}」→"
        f"Chiaki AI({e.get('action')}{('＝' + e.get('gist', '')) if e.get('gist') else ''}):"
        f"「{(e.get('reply') or '')[:120]}」" for e in fresh[-60:])
    cur = json.dumps(notes, ensure_ascii=False)
    try:
        from lib import llm
        out = llm.gpt(
            "あなたは Chiaki AI（Lipple の業務観測AI）。自分の長期記憶を整理します。\n"
            "以下の「今日の会話」から、今後の会話でも覚えておくべきことだけを既存の記憶に統合して返してください。\n"
            "残すもの: 恒久的な決定（表記・運用のルール等）／戸田さんの好み・方針／繰り返し注意すべきこと。\n"
            "残さないもの: 単発の作業内容・一度きりの質問・その場で完結した話。\n"
            "既存の記憶は、矛盾する新しい決定が出た場合だけ書き換え、それ以外は残す。重複は統合する。\n"
            f"各 note は120字以内・最大{convo.NOTES_CAP}件。\n\n"
            f"# 既存の記憶\n{cur}\n\n# 今日の会話\n{lines}\n\n"
            'JSON のみで返す: {"notes": [{"note": "...", "kind": "決定|好み|注意", "since": "YYYY-MM-DD"}]}',
            max_tokens=1200, timeout=120) or ""
    except Exception as e:
        return f"skip: LLM不通 {e}"
    mm = re.search(r"\{.*\}", out, re.S)
    if not mm:
        return "skip: 出力不正（旧notes温存）"
    try:
        raw = json.loads(mm.group(0)).get("notes")
    except Exception:
        return "skip: JSON不正（旧notes温存）"
    new_notes = _valid_notes(raw, notes)
    if new_notes is None:
        return "skip: 検証不合格（旧notes温存）"
    mem = convo.memory()  # 蒸留中に届いた会話を消さないよう保存直前に読み直す
    mem["notes"] = new_notes
    # 印は「処理した最後の会話」まで＝蒸留中に届いた分は翌晩に回る（取りこぼさない）
    mem["distilled_ts"] = max(float(e.get("ts") or 0) for e in fresh)
    runtime.save_json(convo.MEM_FILE, mem)
    return f"ok: 記憶{len(new_notes)}件（会話{len(fresh)}件を蒸留・{len(notes)}件→{len(new_notes)}件）"


if __name__ == "__main__":
    print(f"[convo-memory] {distill()}")
