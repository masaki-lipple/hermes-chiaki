#!/usr/bin/env python3
"""apply-ruling（§6 裁定の実行＋学習／決定論 polling cron・判定時のみ Haiku）。

#8902 の pending 提案スレッドを巡回し、戸田さんの最新返信を解釈して実行する:
  GO/OK/承認(単独)  → 下書きのまま対象スレッド（#5035 等）へ <@対象者> で投稿
  却下/流して/NG     → 投稿しない
  それ以外の実文     → Haiku で解釈（別文面の指定／『一文足して』『短く』等の編集指示）し最終文面を投稿
裁定は rulings.jsonl に記録、文面が変わったら style_corrections.jsonl に学習（→propose の下書きに few-shot）。
#8902 の提案スレッドへ <@戸田> 付きで事後報告。会話エージェントは使わない（決定論＋必要時のみ Haiku）。
cron: */5 9-19 * * 1-5（--no-agent --script apply_ruling.py）。
"""
import os
import re
import sys
from pathlib import Path
sys.path.insert(0, os.environ.get("HERMES_LIB") or str(Path(__file__).resolve().parents[5]))
from lib import runtime, source  # noqa: E402

REJECT = ("却下", "ng", "見送", "流して", "流す", "スルー", "ボツ", "なしで", "却下で", "やめ", "不要")
# 単独でこれだけなら「下書きのまま承認」＝LLM 不要
GO_EXACT = {"go", "ok", "おk", "おけ", "ｏｋ", "ゴー", "承認", "いいね", "了解", "りょうかい", "よし"}


def _norm(s: str) -> str:
    z2h = str.maketrans("ＧＯＫＮ０１２３４５６７８９", "GOKN0123456789")
    return s.translate(z2h).replace("　", " ").strip().lower()


def _classify(text: str):
    """(verdict, payload)。verdict ∈ reject | go | interpret | skip。"""
    t = _norm(text)
    if not t:
        return ("skip", "")
    if any(w in t for w in REJECT):                          # 却下を最優先
        return ("reject", "")
    core = re.sub(r"[\s。、!！.．…~〜ｗw笑👍🙆\U0001F300-\U0001FAFF]+", "", t)
    if core in GO_EXACT:                                     # 単独GO＝下書きそのまま
        return ("go", "")
    return ("interpret", text.strip())                      # それ以外は Haiku 解釈


def _interpret(draft: str, reply: str) -> str:
    """戸田さんの指示を反映した『松永さんへ送る最終文面』を Haiku で生成。失敗時は空文字。"""
    try:
        from lib import llm
        prompt = (
            "松永さんへ送る指摘メッセージの下書きと、承認者(戸田さん)の指示があります。"
            "戸田さんの指示を反映した『松永さんへ送る最終メッセージ本文』だけを出力してください。\n"
            f"下書き: {draft}\n"
            f"戸田さんの指示: {reply}\n"
            "規則: 『そのままでGO』の意なら下書きをそのまま。別文面の指定ならそれを採用。"
            "『一文足して』『短く』『丁寧に』等の編集指示なら下書きに反映。"
            "出力は松永さんへ送る本文のみ。宛名(@)・前置き・引用符は付けない。"
        )
        return (llm.haiku(prompt, max_tokens=220) or "").strip()
    except Exception:
        return ""


def main():
    pend = runtime.load_json("pending_approvals.json", {"items": {}})
    items = pend.get("items", {})
    if not items:
        print("[apply-ruling] no pending")
        return
    acted = 0
    for tts, it in items.items():
        if it.get("status") != "pending":
            continue
        replies = source.read_thread(runtime.CH_CHIAKI_MGMT, tts)
        toda = [m for m in replies if m.get("user_id") == runtime.TODA and m.get("ts") != tts]
        if not toda:
            continue                                          # 戸田さんの裁定待ち
        ruling_text = toda[-1].get("text", "")
        verdict, payload = _classify(ruling_text)
        if verdict == "skip":
            continue

        src_ch, src_ts = it.get("source_channel"), it.get("source_ts")
        tgt, draft = it.get("target_user_id") or "", it.get("draft", "")
        final, report, learned = "", "", False

        if verdict == "reject":
            report = "了解です。今回は出しません。"
        else:
            if not (src_ch and src_ts and tgt):
                print(f"[apply-ruling] {tts}: missing target/source, skip post")
                continue
            if verdict == "go":
                final = draft
            else:  # interpret
                final = _interpret(draft, payload)
                if not final:
                    print(f"[apply-ruling] {tts}: interpret failed -> leave pending (retry next run)")
                    continue                                  # 保留＝次回リトライ
            source.post_thread_reply(src_ch, src_ts, f"<@{tgt}>\n{final}")
            if verdict == "interpret" and final.strip() != draft.strip():
                runtime.append_jsonl("style_corrections.jsonl", {
                    "ts": runtime.now_ts(), "kind": it.get("finding_kind", ""),
                    "original": draft, "corrected": final})
                learned = True
                report = "ご指示を反映して投稿しました。学習に取り込みます。"
            else:
                report = "GO 了解です。対象スレッドへ投稿しました。"

        it["status"] = "reject" if verdict == "reject" else "go"
        it["final_text"], it["ruling_text"], it["learned"] = final, ruling_text, learned
        runtime.append_jsonl("rulings.jsonl", {
            "ts": runtime.now_ts(), "thread_ts": tts, "verdict": verdict,
            "kind": it.get("finding_kind", ""), "original": draft,
            "final_text": final, "ruling_text": ruling_text})
        # 事後報告＝処理/独り言なのでセルフメンション（@Chiaki AI）。戸田さんは確認要件ではないのでpingしない
        source.post_thread_reply(runtime.CH_CHIAKI_MGMT, tts, f"<@{runtime.CHIAKI_SELF}>\n{report}")
        acted += 1
    if acted:
        runtime.save_json("pending_approvals.json", pend)
    print(f"[apply-ruling] acted={acted}")


if __name__ == "__main__":
    main()
