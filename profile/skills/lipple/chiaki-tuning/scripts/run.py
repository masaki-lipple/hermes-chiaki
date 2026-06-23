#!/usr/bin/env python3
"""chiaki-tuning（戸田さんの口頭フィードバックを学習／決定論収集＋Haiku分類・会話エージェント不使用）。

#8902 の戸田さんの**トップレベル投稿**＝Chiaki AI への振る舞い/文面フィードバックとして拾い、
Haiku で {対象skill, 守るべき指示} に分類して tuning.json に蓄積する。
silence/pdca/propose の文面生成が runtime.load_tuning() でこれを読み必ず反映する
＝Slack の口頭調整がコードを触らず効く（誤字脱字の #8902 裁定ループと同じ発想）。
拾ったら #8902 のその投稿スレッドに「承知しました…」と自己メンションで返答。
※ 提案スレッドへの返信(=裁定)は apply-ruling の領分。ここは戸田さんのトップレベル投稿だけ。
cron: */2 9-19 平日（event-listener からも起動）。
"""
import json
import os
import re
import sys
from pathlib import Path
sys.path.insert(0, os.environ.get("HERMES_LIB") or str(Path(__file__).resolve().parents[5]))
from lib import runtime, source  # noqa: E402

SKILLS = {"silence", "pdca", "propose", "notation", "stall", "general"}
CAP = 8  # skill ごとに保持する指示の最大数


def _classify(text: str):
    """戸田さんの投稿を {is_feedback, skill, directive, ack} に分類。失敗時 None。"""
    try:
        from lib import llm
    except Exception:
        return None
    prompt = (
        "次は戸田さんが Chiaki AI（社内タスク管理AI）に送ったメッセージです。"
        "これは Chiaki AI の振る舞い・文面・運用に対する指示/フィードバックですか？\n"
        f"メッセージ: {text}\n"
        "そうなら、対象機能 skill を "
        "silence(無音リマインド)/pdca(自己PDCA)/propose(指摘の提案文面)/notation(表記ルール)/"
        "stall(停滞)/general(全般) から1つ選び、今後 Chiaki AI が守るべき指示を簡潔に1文(directive)、"
        "戸田さんへの短い了解文(ack)を作ってください。指示・フィードバックでなければ is_feedback を false に。"
        'JSON のみ出力: {"is_feedback": true/false, "skill": "silence|pdca|propose|notation|stall|general", '
        '"directive": "今後守る指示", "ack": "了解文"}'
    )
    out = llm.haiku(prompt, max_tokens=300) or ""
    m = re.search(r"\{.*\}", out, re.S)
    if not m:
        return None
    try:
        return json.loads(m.group(0))
    except Exception:
        return None


def main():
    ch = runtime.CH_CHIAKI_MGMT
    recent = source.read_recent(ch, limit=50)
    if not recent:
        print("[tuning] no messages")
        return
    cur = runtime.load_json("tuning_cursor.json", {})
    since = float(cur.get(ch, 0.0))
    # 戸田さんのトップレベル新規投稿のみ（提案=chiaki投稿／裁定=スレッド返信 は対象外）
    new = [m for m in recent if m["user_id"] == runtime.TODA and m["ts_float"] > since]
    if not new:
        print("[tuning] no new feedback")
        return
    tuning = runtime.load_json("tuning.json", {})
    maxts, learned = since, 0
    for m in sorted(new, key=lambda x: x["ts_float"]):
        maxts = max(maxts, m["ts_float"])
        c = _classify(m["text"])
        if not c or not c.get("is_feedback"):
            continue
        skill = c.get("skill") if c.get("skill") in SKILLS else "general"
        directive = (c.get("directive") or "").strip()
        if not directive:
            continue
        tuning.setdefault(skill, []).append({"directive": directive, "ts": runtime.now_ts()})
        tuning[skill] = tuning[skill][-CAP:]
        ack = (c.get("ack") or "").strip() or f"承知しました。{skill} に反映します。"
        source.post_thread_reply(ch, m["ts"], f"<@{runtime.CHIAKI_SELF}>\n{ack}")
        learned += 1
    runtime.save_json("tuning.json", tuning)
    cur[ch] = maxts
    runtime.save_json("tuning_cursor.json", cur)
    print(f"[tuning] learned={learned}")


if __name__ == "__main__":
    main()
