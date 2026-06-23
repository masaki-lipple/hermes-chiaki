#!/usr/bin/env python3
"""apply-ruling（§6 裁定の実行＋学習／決定論 polling cron・LLM 非起動）。

#8902 の pending 提案スレッドを巡回し、戸田さんの返信を分類して実行する:
  GO/OK/承認        → 対象スレッド（#5035 等）へ @対象者メンション付きで文面を投稿
  却下/流して/NG     → 投稿しない
  上記以外の実文     → 文面修正＝戸田さんの文面で投稿し、案→採用 を学習(style_corrections.jsonl)
裁定は rulings.jsonl に記録し、#8902 の提案スレッドへ短い事後報告を返す。
学習ファイルは propose の下書き(Haiku)に few-shot で渡り、文面が戸田さんの言い回しに寄る。
cron: */5 9-19 * * 1-5（--no-agent --script apply_ruling.py）。
"""
import os
import sys
from pathlib import Path
sys.path.insert(0, os.environ.get("HERMES_LIB") or str(Path(__file__).resolve().parents[5]))
from lib import runtime, source  # noqa: E402

# 分類キーワード（全角/半角・大小は _norm で吸収）。却下を GO より優先判定する。
GO = ("go", "ok", "おk", "おけ", "オッケ", "オーケー", "承認", "いいね", "良いね",
      "よし", "ゴー", "👍", "🙆", "賛成", "それで", "出して")
REJECT = ("却下", "ng", "見送", "流して", "流す", "スルー", "ボツ", "なしで", "却下で", "やめ", "不要")
MIN_EDIT = 4  # これ未満の非GO/非却下返信は曖昧として保留（誤爆防止）


def _norm(s: str) -> str:
    z2h = str.maketrans("ＧＯＫＮ０１２３４５６７８９", "GOKN0123456789")
    return s.translate(z2h).replace("　", " ").strip().lower()


def _classify(text: str):
    """戸田さんの返信を (verdict, edit_text) に。verdict ∈ go|reject|edit|skip。"""
    t = _norm(text)
    if not t:
        return ("skip", "")
    if any(w in t for w in REJECT):        # 「却下」を最優先（GO語と混在しても却下）
        return ("reject", "")
    if any(w in t for w in GO):
        return ("go", "")
    if len(text.strip()) >= MIN_EDIT:      # GO でも却下でもない実文＝文面修正とみなす
        return ("edit", text.strip())
    return ("skip", "")


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
            continue                        # 戸田さんの裁定待ち
        ruling_text = toda[-1].get("text", "")   # 最新の戸田さん返信を採用
        verdict, edit_text = _classify(ruling_text)
        if verdict == "skip":
            continue                        # 曖昧 → 保留（次回再評価）

        src_ch, src_ts = it.get("source_channel"), it.get("source_ts")
        tgt, draft = it.get("target_user_id") or "", it.get("draft", "")
        final, report = "", ""
        if verdict in ("go", "edit"):
            if not (src_ch and src_ts and tgt):
                print(f"[apply-ruling] {tts}: missing target/source, skip post")
                continue
            final = edit_text if verdict == "edit" else draft
            source.post_thread_reply(src_ch, src_ts, f"<@{tgt}>\n{final}")
            if verdict == "edit":
                runtime.append_jsonl("style_corrections.jsonl", {
                    "ts": runtime.now_ts(), "kind": it.get("finding_kind", ""),
                    "original": draft, "corrected": final})
                report = "文面修正で反映しました。学習に取り込みます。"
            else:
                report = "GO 了解です。対象スレッドへ投稿しました。"
        else:  # reject
            report = "了解です。今回は出しません。"

        it["status"], it["final_text"], it["ruling_text"] = verdict, final, ruling_text
        runtime.append_jsonl("rulings.jsonl", {
            "ts": runtime.now_ts(), "thread_ts": tts, "verdict": verdict,
            "kind": it.get("finding_kind", ""), "original": draft,
            "final_text": final, "ruling_text": ruling_text})
        source.post_thread_reply(runtime.CH_CHIAKI_MGMT, tts, report)  # #8902 へ事後報告
        acted += 1
    if acted:
        runtime.save_json("pending_approvals.json", pend)
    print(f"[apply-ruling] acted={acted}")


if __name__ == "__main__":
    main()
