#!/usr/bin/env python3
"""chiaki-tuning（#8902/#5902 での戸田さんとの対話：指示は学習／質問は回答／決定論＋Haiku・ツール無し）。

戸田さんのメッセージを Haiku で種別判定:
  - directive: Chiaki AI の振る舞い/文面への指示 → tuning.json に蓄積、ackを返す。
  - question : Chiaki AI への質問・依頼（「まとめて」「教えて」等）→ 状態(tuning/観測/スレッド)から
               Haiku がテキストで回答（**コード実行ツールは一切持たない＝安全**）。
  - none     : 何もしない。
silence/pdca/propose の生成は runtime.load_tuning() で directive を必ず反映する。
対象: #8902(トップレベル＋提案以外のスレッド返信) と #5902(PDCA投稿への返信)。提案スレ=裁定はapply-ruling。
event-listener から即時起動＋ cron */2 9-19 バックストップ。
"""
import datetime as dt
import json
import os
import re
import sys
from collections import Counter
from pathlib import Path
sys.path.insert(0, os.environ.get("HERMES_LIB") or str(Path(__file__).resolve().parents[5]))
from lib import runtime, source  # noqa: E402

SKILLS = {"silence", "pdca", "propose", "notation", "stall", "general"}
CAP = 8
JST = dt.timezone(dt.timedelta(hours=9))


def _tsd(ts) -> str:
    try:
        return dt.datetime.fromtimestamp(float(ts), JST).strftime("%Y-%m-%d")
    except Exception:
        return ""


def _classify(text: str, hint: str = ""):
    """戸田さんの投稿を {type, skill, directive, ack} に分類。type ∈ directive|question|none。失敗時 None。"""
    try:
        from lib import llm
    except Exception:
        return None
    prompt = (
        "次は戸田さんが Chiaki AI（社内タスク管理AI）に送ったメッセージです。"
        + (f"（文脈: {hint}）" if hint else "")
        + "種別を判定:\n"
        "- directive: Chiaki AI の振る舞い/文面/運用への指示・フィードバック\n"
        "- question: Chiaki AI への質問・依頼（例『まとめて』『教えて』『どう？』『何を学んだ？』）\n"
        "- none: それ以外\n"
        f"メッセージ: {text}\n"
        "directive の場合のみ skill(silence/pdca/propose/notation/stall/general)・"
        "directive(今後守る指示1文)・ack(短い了解文)も付ける。用語統一や全般の言い回しは general。\n"
        'JSON のみ: {"type":"directive|question|none","skill":"...","directive":"...","ack":"..."}'
    )
    out = llm.haiku(prompt, max_tokens=300) or ""
    m = re.search(r"\{.*\}", out, re.S)
    if not m:
        return None
    try:
        return json.loads(m.group(0))
    except Exception:
        return None


def _answer(question: str, ch: str, root: str) -> str:
    """状態（スレッドのやりとり＋学習済みtuning＋本日の観測）から Haiku がテキストで回答。ツール無し。"""
    try:
        from lib import llm
    except Exception:
        return ""
    thread = source.read_thread(ch, root)
    convo = "\n".join(f"- {(m.get('user_name') or m.get('user_id') or '?')}: {m.get('text', '')[:160]}"
                      for m in thread[-12:])
    tuning = runtime.load_json("tuning.json", {})
    learned = "; ".join(d.get("directive", "") for lst in tuning.values() for d in lst) or "（まだ無し）"
    today = dt.datetime.now(JST).strftime("%Y-%m-%d")
    fk = Counter(f.get("kind") for f in runtime.read_jsonl("findings.jsonl") if _tsd(f.get("ts")) == today)
    rv = Counter(r.get("verdict") for r in runtime.read_jsonl("rulings.jsonl") if _tsd(r.get("ts")) == today)
    prompt = (
        "あなたは Chiaki AI。戸田さんの依頼に簡潔に答えます（テキストのみ・絵文字なし・です/ます・要点）。"
        "分からないことは推測せず正直に言う。\n"
        f"依頼: {question}\n"
        f"このスレッドのやりとり:\n{convo}\n"
        f"あなたが学習済みの指示(tuning): {learned}\n"
        f"本日の観測: 表記{fk.get('notation', 0)}・誤字{fk.get('typo', 0)}・停滞{fk.get('stall', 0)}件、"
        f"裁定 GO/反映{rv.get('go', 0) + rv.get('interpret', 0)}・完了{rv.get('completed', 0)}件。"
    )
    return (llm.haiku(prompt, max_tokens=450) or "").strip()


def _revise(text: str, skill: str) -> str:
    """既存投稿を、学習済み指示に従って最小限だけ修正（事実・構成は変えない）。Haiku・ツール無し。"""
    if not (text or "").strip():
        return ""
    try:
        from lib import llm
    except Exception:
        return ""
    directives = list(dict.fromkeys(runtime.load_tuning(skill)))  # skill＋general・重複排除
    if not directives:
        return ""
    prompt = ("次の Chiaki AI の投稿を、以下の指示に従って最小限だけ修正してください。"
              "事実・数字・構成・改行・行数は変えない。文体/表記/句読点/記号だけ直す。"
              "先頭の <!channel> はそのまま残す。修正後の本文のみ出力（前置きなし）。\n"
              f"指示: {'; '.join(directives)}\n投稿:\n{text}")
    return (llm.haiku(prompt, max_tokens=400) or "").strip()


def _maybe_edit_root(ch: str, root: str, skill: str):
    """指摘対象の投稿(root)が chiaki の投稿なら、学習内容を反映して編集（chat.update）。"""
    rmsg = next((x for x in source.read_thread(ch, root) if x.get("ts") == root), None)
    if not rmsg or rmsg.get("user_id") != runtime.CHIAKI_SELF:
        return
    edit_skill = "pdca" if ch == runtime.CH_CHIAKI_PDCA else skill
    revised = _revise(rmsg.get("text", ""), edit_skill)
    if revised and revised.strip() != (rmsg.get("text", "") or "").strip():
        source.update_message(ch, root, revised)
        print(f"[tuning] edited root post ch={ch} ts={root}")


def _candidates(cur: dict):
    """戸田さんの新規メッセージ候補 [(msg, ack_root_ts, channel, hint)] を集める。"""
    mgmt, pdca = runtime.CH_CHIAKI_MGMT, runtime.CH_CHIAKI_PDCA
    pend = runtime.load_json("pending_approvals.json", {"items": {}}).get("items", {})
    open_threads = {ts for ts, it in pend.items()
                    if it.get("status") in ("pending", "awaiting_completion")}
    pdca_hint = "#5902＝Chiaki AIの自己PDCAチャンネル。PDCA文面の調整が中心だが、用語統一や全般の言い回しなら general"
    cand = []
    since_m = float(cur.get(mgmt, 0.0))
    for m in source.read_recent(mgmt, limit=50):
        if m["user_id"] == runtime.TODA and m["ts_float"] > since_m:
            cand.append((m, m["ts"], mgmt, ""))
        if m.get("thread_replies") and m["ts"] not in open_threads:
            for r in source.read_thread(mgmt, m["ts"]):
                if r["ts"] != m["ts"] and r["user_id"] == runtime.TODA and r["ts_float"] > since_m:
                    cand.append((r, m["ts"], mgmt, ""))
    since_p = float(cur.get(pdca, 0.0))
    for m in source.read_recent(pdca, limit=50):
        if m.get("thread_replies"):
            for r in source.read_thread(pdca, m["ts"]):
                if r["ts"] != m["ts"] and r["user_id"] == runtime.TODA and r["ts_float"] > since_p:
                    cand.append((r, m["ts"], pdca, pdca_hint))
    return cand


def main():
    cur = runtime.load_json("tuning_cursor.json", {})
    cand = _candidates(cur)
    if not cand:
        print("[tuning] nothing new")
        return
    tuning = runtime.load_json("tuning.json", {})
    maxts, acted = {}, 0
    for m, root, ch, hint in sorted(cand, key=lambda x: x[0]["ts_float"]):
        maxts[ch] = max(maxts.get(ch, float(cur.get(ch, 0.0))), m["ts_float"])
        c = _classify(m["text"], hint)
        if not c:
            continue
        typ = c.get("type")
        if typ == "directive":
            skill = c.get("skill") if c.get("skill") in SKILLS else "general"
            directive = (c.get("directive") or "").strip()
            if not directive:
                continue
            tuning.setdefault(skill, []).append({"directive": directive, "ts": runtime.now_ts()})
            tuning[skill] = tuning[skill][-CAP:]
            ack = (c.get("ack") or "").strip() or f"承知しました。{skill} に反映します。"
            source.post_thread_reply(ch, root, f"<@{runtime.CHIAKI_SELF}>\n{ack}")
            _maybe_edit_root(ch, root, skill)  # 学習内容を指摘対象の投稿に反映して編集
            acted += 1
        elif typ == "question":
            ans = _answer(m["text"], ch, root)
            if ans:
                source.post_thread_reply(ch, root, f"<@{runtime.CHIAKI_SELF}>\n{ans}")
                acted += 1
    if tuning:
        runtime.save_json("tuning.json", tuning)
    for ch, mx in maxts.items():
        cur[ch] = mx
    runtime.save_json("tuning_cursor.json", cur)
    print(f"[tuning] acted={acted}")


if __name__ == "__main__":
    main()
