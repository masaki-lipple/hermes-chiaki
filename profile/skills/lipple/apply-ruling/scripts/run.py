#!/usr/bin/env python3
"""apply-ruling（§6 裁定の実行＋完了追跡＋学習／決定論 polling・判定時のみ Haiku）。

Phase1 裁定: #8902 の pending スレッドの戸田さん最新返信を解釈し実行
  GO単独 → 下書きそのまま投稿 ／ 却下 → 出さない ／ それ以外 → Haiku解釈で最終文面を投稿
  投稿したら status=awaiting_completion（nudge_ts 記録）。却下は status=reject。
  事後報告は @Chiaki AI セルフメンション＋実投稿リンク（#8902）。
Phase2 完了追跡: awaiting_completion の対象スレッド(#5035等)で対象者が chiaki にメンション返信
  （＝修正完了の報告）したら → 対象者へお礼（対象スレッド）＋戸田さんへ完了通知（#8902・該当リンク）。
  status=completed。
裁定/完了は rulings.jsonl、文面変更は style_corrections.jsonl に学習。
cron: */1 9-19 * * 1-5（--no-agent --script apply_ruling.py）。
"""
import os
import re
import sys
from pathlib import Path
sys.path.insert(0, os.environ.get("HERMES_LIB") or str(Path(__file__).resolve().parents[5]))
from lib import observe, runtime, source  # noqa: E402

TEAM = "lipple"  # Slack ワークスペース subdomain（permalink 用）
REJECT = ("却下", "ng", "見送", "流して", "流す", "スルー", "ボツ", "なしで", "却下で", "やめ", "不要")
GO_EXACT = {"go", "ok", "おk", "おけ", "ｏｋ", "ゴー", "承認", "いいね", "了解", "りょうかい", "よし"}
# 質問・困惑（裁定ではない）＝ #5035 へ誤爆させず skip する手掛かり
QUESTION_WORDS = ("どういうこと", "どういう意味", "意味がわからない", "意味が分から",
                  "理解できない", "なぜ", "どうして", "意味不明", "よくわからない", "よくわから")
# 対象者の完了報告とみなすキーワード（chiaki へのメンションが無い場合の保険）
COMPLETE_WORDS = ("直し", "なおし", "修正しました", "修正済", "修正完了", "完了", "対応しました",
                  "対応済", "できました", "やりました", "反映しました", "なおしました")
REMIND_EVERY_MIN = 120  # 完了報告が来ない時の再リマインド間隔（分）。最初のリマインドも nudge から この間隔後
MAX_REMINDS = 2         # 再リマインドの最大回数（これ以上は止める＝spam防止）


def _norm(s: str) -> str:
    z2h = str.maketrans("ＧＯＫＮ０１２３４５６７８９", "GOKN0123456789")
    return s.translate(z2h).replace("　", " ").strip().lower()


def _permalink(ch: str, ts: str, parent: str) -> str:
    return (f"https://{TEAM}.slack.com/archives/{ch}/p{ts.replace('.', '')}"
            f"?thread_ts={parent}&cid={ch}")


def _classify(text: str):
    """(verdict, payload)。verdict ∈ reject | go | interpret | skip。"""
    t = _norm(text)
    if not t:
        return ("skip", "")
    if any(w in t for w in REJECT):
        return ("reject", "")
    core = re.sub(r"[\s。、!！.．…~〜ｗw笑👍🙆\U0001F300-\U0001FAFF]+", "", t)
    if core in GO_EXACT:
        return ("go", "")
    # 質問・困惑（？で終わる or 疑問語）は裁定指示ではない → 投稿しない（#5035 への誤爆防止）
    if t.endswith(("?", "？")) or any(w in t for w in QUESTION_WORDS):
        return ("skip", "")
    return ("interpret", text.strip())


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
        return observe.enforce_regulations((llm.haiku(prompt, max_tokens=220) or "").strip())
    except Exception:
        return ""


def _thanks() -> str:
    """完了報告への短いお礼。Haiku でゆらがせ、失敗時は固定文。"""
    try:
        from lib import llm
        return observe.enforce_regulations(llm.haiku("松永さんが指摘どおりに表記を修正してくれたことへの短いお礼を1文。"
                         "絵文字なし・明るく簡潔・です/ます。") or "修正ありがとうございます！")
    except Exception:
        return "修正ありがとうございます！"


def _remind_text() -> str:
    """完了報告が来ない時の再リマインド。Haiku でゆらがせ、失敗時は固定文。"""
    try:
        from lib import llm
        return llm.haiku("先に依頼した表記修正の完了報告がまだ来ていません。"
                         "確認と対応をうながす1〜2文。絵文字なし・きつくしすぎず・です/ます。"
                         "完了済みならメンションで教えてほしい旨も。") \
            or "まだ修正ができていません。もう一度確認してください。"
    except Exception:
        return "まだ修正ができていません。もう一度確認してください。"


def _recheck_text() -> str:
    """完了報告は来たが まだ直っていない時の指摘。Haiku、失敗時は固定文。"""
    try:
        from lib import llm
        return llm.haiku("修正完了の報告をもらったが、確認するとまだ直っていなかった。"
                         "お礼を一言添えつつ、まだ反映されていないようなので再確認をお願いする1〜2文。"
                         "絵文字なし・責めない・です/ます。") \
            or "まだ修正ができていません。もう一度確認してください。"
    except Exception:
        return "まだ修正ができていません。もう一度確認してください。"


def _verify_fixed(it: dict, replies: list):
    """修正後の対象メッセージを検証。True=直っている / False=まだ未修正 / None=検証不能。
    検知語(verify_found 例「sns」)が編集後の対象メッセージから消えていれば修正済とみなす。"""
    vf = it.get("verify_found")
    if not vf:
        return None
    src_ts = it.get("source_ts")
    root = next((m for m in replies if m.get("ts") == src_ts), None)
    if root is None:
        return None
    return vf not in root.get("text", "")


def _phase_ruling(items: dict) -> int:
    """Phase1: 戸田さんの裁定を処理。"""
    acted = 0
    for tts, it in items.items():
        if it.get("status") != "pending":
            continue
        replies = source.read_thread(runtime.CH_CHIAKI_MGMT, tts)
        toda = [m for m in replies if m.get("user_id") == runtime.TODA and m.get("ts") != tts]
        if not toda:
            continue
        ruling_text = toda[-1].get("text", "")
        verdict, payload = _classify(ruling_text)
        if verdict == "skip":
            continue

        src_ch, src_ts = it.get("source_channel"), it.get("source_ts")
        tgt, draft = it.get("target_user_id") or "", it.get("draft", "")
        final, report = "", ""

        if verdict == "reject":
            it["status"], it["ruling_text"] = "reject", ruling_text
            report = "了解です。今回は出しません。"
        else:
            if not (src_ch and src_ts and tgt):
                print(f"[apply-ruling] {tts}: missing target/source, skip")
                continue
            if verdict == "go":
                final = draft
            else:
                final = _interpret(draft, payload)
                if not final:
                    print(f"[apply-ruling] {tts}: interpret failed -> leave pending")
                    continue
            posted = source.post_thread_reply(src_ch, src_ts, f"<@{tgt}>\n{final}")
            nudge_ts = posted.get("ts") if isinstance(posted, dict) else None
            link = _permalink(src_ch, nudge_ts, src_ts) if nudge_ts else ""
            if verdict == "interpret" and final.strip() != draft.strip():
                runtime.append_jsonl("style_corrections.jsonl", {
                    "ts": runtime.now_ts(), "kind": it.get("finding_kind", ""),
                    "original": draft, "corrected": final})
                report = "ご指示を反映して投稿しました。学習に取り込みます。"
            else:
                report = "GO 了解です。対象スレッドへ投稿しました。"
            if link:
                report += "\n" + link
            it["status"], it["final_text"] = "awaiting_completion", final
            it["nudge_ts"], it["ruling_text"] = nudge_ts, ruling_text
        runtime.append_jsonl("rulings.jsonl", {
            "ts": runtime.now_ts(), "thread_ts": tts, "verdict": verdict,
            "kind": it.get("finding_kind", ""), "original": draft,
            "final_text": final, "ruling_text": ruling_text})
        # 事後報告＝処理/独り言＝@Chiaki AI セルフメンション（戸田さんはpingしない）
        source.post_thread_reply(runtime.CH_CHIAKI_MGMT, tts, f"<@{runtime.CHIAKI_SELF}>\n{report}")
        acted += 1
    return acted


def _phase_completion(items: dict) -> int:
    """Phase2: 完了報告の検知＋修正の検証＋（未修正/未報告の）再リマインド。
      ・検証OK（直っている）  → 対象者へお礼＋戸田さんへ完了通知 → completed
      ・報告ありだが未修正    → 対象者へ「まだ直っていない」（1報告につき1回）
      ・未報告かつ未修正      → 時間ベースで再リマインド（最大 MAX_REMINDS 回）
    """
    acted = 0
    for tts, it in items.items():
        if it.get("status") != "awaiting_completion":
            continue
        src_ch, src_ts = it.get("source_channel"), it.get("source_ts")
        tgt, nudge_ts = it.get("target_user_id") or "", it.get("nudge_ts") or "0"
        if not (src_ch and src_ts and tgt):
            continue
        replies = source.read_thread(src_ch, src_ts)
        # 対象者の最新の完了報告（chiaki へのメンション or 完了語）
        report = None
        for m in replies:
            if m.get("user_id") != tgt or float(m.get("ts", "0")) <= float(nudge_ts):
                continue
            txt = m.get("text", "")
            if f"<@{runtime.CHIAKI_SELF}>" in txt or any(w in txt for w in COMPLETE_WORDS):
                report = m
        fixed = _verify_fixed(it, replies)  # True=直った / False=未修正 / None=検証不能

        if fixed is True or (fixed is None and report):
            # 完了（検証OK、または検証不能だが報告あり）→ お礼＋戸田さんへ完了通知
            source.post_thread_reply(src_ch, src_ts, f"<@{tgt}>\n{_thanks()}")
            done_ts = report["ts"] if report else src_ts
            link = _permalink(src_ch, src_ts, src_ts)  # 該当箇所（修正された元メッセージ）への直リンク
            source.post_thread_reply(
                runtime.CH_CHIAKI_MGMT, tts,
                f"<@{runtime.TODA}>\n松永さんが修正を完了しました。\n\n{link}\n\nーーーーー")
            it["status"], it["completion_ts"] = "completed", done_ts
            runtime.append_jsonl("rulings.jsonl", {
                "ts": runtime.now_ts(), "thread_ts": tts, "verdict": "completed",
                "kind": it.get("finding_kind", ""), "completion_ts": done_ts})
            acted += 1
        elif report and fixed is False:
            # 報告は来たが まだ直っていない → 同じ報告には1回だけ指摘（spam防止）
            if it.get("last_checked_report_ts") != report["ts"]:
                source.post_thread_reply(src_ch, src_ts, f"<@{tgt}>\n{_recheck_text()}")
                it["last_checked_report_ts"] = report["ts"]
                acted += 1
        else:
            # 未報告かつ未修正 → 時間ベースの再リマインド
            now = runtime.now_ts()
            base = float(it.get("last_remind_ts") or it.get("nudge_ts") or 0)
            rc = it.get("remind_count", 0)
            if base and rc < MAX_REMINDS and (now - base) >= REMIND_EVERY_MIN * 60:
                source.post_thread_reply(src_ch, src_ts, f"<@{tgt}>\n{_remind_text()}")
                it["last_remind_ts"], it["remind_count"] = now, rc + 1
                acted += 1
    return acted


def main():
    pend = runtime.load_json("pending_approvals.json", {"items": {}})
    items = pend.get("items", {})
    if not items:
        print("[apply-ruling] no pending")
        return
    ruled = _phase_ruling(items)
    completed = _phase_completion(items)
    if ruled or completed:
        runtime.save_json("pending_approvals.json", pend)
    print(f"[apply-ruling] ruled={ruled} completed={completed}")


if __name__ == "__main__":
    main()
