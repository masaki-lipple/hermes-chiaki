#!/usr/bin/env python3
"""convo-review（Phase D・2026-07-24 戸田GO＝Issue「3. Phase D 会話の週次自己レビュー」）:
会話台帳の直近7日分を週1でレビューし、会話品質の問題を自分で検出して#8902へ報告する。
改善のIssue化は戸田さんのスレッド返信（「1番をIssueに」等）→ intakeの会話フローが確認へ乗せる。
判断はGPT 5.5・投稿は決定論ガード（LLM不通・出力不正＝投稿しない・誤報を出さない）。
cron: 20 18 * * 5。"""
from __future__ import annotations

import datetime as dt
import json
import os
import re
import sys
from pathlib import Path
sys.path.insert(0, os.environ.get("HERMES_LIB") or str(Path(__file__).resolve().parents[5]))
from lib import convo, observe, runtime, source  # noqa: E402

JST = dt.timezone(dt.timedelta(hours=9))
STATE = "convo_review.json"
WINDOW_SEC = 7 * 86400
MIN_GAP_SEC = 3 * 86400  # cron二重発火・手動実行との重複ガード
MIN_ENTRIES = 3          # 会話がほぼ無い週はレビュー自体をスキップ（無理に投稿しない）
MAX_ISSUES = 5


def main() -> None:
    st = runtime.load_json(STATE, {})
    now = runtime.now_ts()
    if now - float(st.get("last_run_ts") or 0) < MIN_GAP_SEC:
        print("[convo-review] skip: 前回から3日未満")
        return
    entries = [e for e in (convo.memory().get("ledger") or [])
               if float(e.get("ts") or 0) > now - WINDOW_SEC][-80:]
    if len(entries) < MIN_ENTRIES:
        print(f"[convo-review] skip: 今週の会話が{len(entries)}件")
        return
    lines = "\n".join(
        f"{i + 1}. [{e.get('dt', '')}] 戸田さん「{(e.get('said') or '')[:140]}」→"
        f"あなた({e.get('action')}):「{(e.get('reply') or '')[:140]}」"
        for i, e in enumerate(entries))
    try:
        from lib import llm
        llm.reset_used()
        out = llm.gpt(
            "あなたは Chiaki AI。自分の1週間の会話を自己レビューします。\n"
            "観点: ①相手の意図の取り違え ②ぎこちない定型・同じ言い回しの繰り返し "
            "③根拠のない断定・記録と矛盾する説明 ④「あとで確認して返します」等の実行されない約束 "
            "⑤質問への答え漏れ。\n"
            "本当に問題のあるものだけを最大5件。無ければ空配列でよい（無理に出さない）。\n\n"
            f"# 今週の会話（番号つき）\n{lines}\n\n"
            'JSON のみで返す: {"summary": "全体の一言（1〜2文）", "issues": '
            '[{"no": 会話番号, "problem": "何が問題か", "suggestion": "どう直すか"}]}',
            max_tokens=900, timeout=120) or ""
        d = json.loads(re.search(r"\{.*\}", out, re.S).group(0))
    except Exception as e:
        print(f"[convo-review] skip: LLM不通/出力不正 {type(e).__name__}: {e}")
        return  # 状態は進めない＝次回cronで再試行（誤報より無投稿）
    summary = (d.get("summary") or "").strip()
    issues = [x for x in (d.get("issues") or []) if isinstance(x, dict)
              and (x.get("problem") or "").strip()][:MAX_ISSUES]
    body = [f"今週の会話セルフレビューです（対象=直近7日の{len(entries)}件）。"]
    if summary:
        body.append(summary)
    body.append("")
    if issues:
        for i, x in enumerate(issues, 1):
            src = ""
            try:
                e = entries[int(x.get("no")) - 1]
                src = f"（{e.get('dt', '')}「{(e.get('said') or '')[:40]}」への応答）"
            except Exception:
                pass
            line = f"• {i}. {(x.get('problem') or '').strip()[:140]}{src}"
            sug = (x.get("suggestion") or "").strip()
            if sug:
                line += f" 改善案: {sug[:120]}"
            body.append(line)
        body += ["", "直したいものがあれば、このスレッドに「1番をIssueに」のように返信してください。"]
    else:
        body.append("大きな問題は見つかりませんでした。引き続き見ていきます。")
    text = runtime.ensure_punct(observe.enforce_regulations("\n".join(body)))
    try:
        from lib import llm
        tag = llm.last_used()
        if tag:
            text = runtime.append_model_tag(text, tag)
    except Exception:
        pass
    posted = source.post_message(runtime.CH_CHIAKI_MGMT, f"<@{runtime.TODA}>\n{text}")
    if isinstance(posted, dict) and posted.get("ts"):
        st["last_run_ts"] = now
        runtime.save_json(STATE, st)
        print(f"[convo-review] posted issues={len(issues)}")
    else:
        print("[convo-review] post failed -> 状態を進めない（次回再試行）")


if __name__ == "__main__":
    main()
