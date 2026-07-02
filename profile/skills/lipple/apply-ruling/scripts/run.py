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
GO_EXACT = {"go", "ok", "おk", "おけ", "ｏｋ", "ゴー", "承認", "いいね", "了解", "りょうかい", "よし"}
# 却下＝却下語が文の主旨のときだけ（編集指示の中の部分一致で無音却下しない＝無音失敗禁止）。
REJECT_EXACT = {"却下", "却下で", "却下します", "ng", "見送り", "見送る", "見送ります", "見送りで",
                "ボツ", "ボツで", "なしで", "不要", "スルー", "スルーで", "やめて", "流して"}
_REJECT_SUFFIX = ("却下で", "なしで", "見送りで", "見送りです", "スルーで", "ボツで",
                  "は却下", "は見送り", "は不要")
_EDIT_VERB_RE = re.compile(r"足して|書い|直して|削って|消して|短く|長く|丁寧|強め|弱め|"
                           r"変えて|加えて|入れて|にして|ください|して欲し|してほし|まとめ|やわらか|柔らか")
# 質問・困惑（裁定ではない）＝ #5035 へ誤爆させず skip する手掛かり
QUESTION_WORDS = ("どういうこと", "どういう意味", "意味がわからない", "意味が分から",
                  "理解できない", "なぜ", "どうして", "意味不明", "よくわからない", "よくわから")
# 対象者の完了報告とみなすキーワード（chiaki へのメンションが無い場合の保険）。
# 完了形に限定＝「これから直します」等の未来形・宣言を完了と誤認しない（監査確定バグ）。
COMPLETE_WORDS = ("直しました", "なおしました", "直しといた", "修正しました", "修正済", "修正完了",
                  "対応しました", "対応済", "できました", "やりました", "反映しました", "完了しました")
# 未来形・着手宣言＝完了報告ではない（「すぐ直します！」に「まだ反映されていない」と返さない）
_FUTURE_RE = re.compile(r"(?:直|なお|修正|対応|反映)し(?:ます|ときます|ておきます)|これから|あとで|後で|のちほど|後ほど|予定です")
REMIND_EVERY_MIN = 120  # 完了報告が来ない時の再リマインド間隔（分）。最初のリマインドも nudge から この間隔後
MAX_REMINDS = 2         # 再リマインドの最大回数（これ以上は止める＝spam防止）
STALE_AFTER_MIN = 240   # 最後の促しから この分 無反応なら stale 終端＝未対応カウント(chiaki-pdca)から外す


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
    core = re.sub(r"[\s。、!！.．…~〜ｗw笑👍🙆\U0001F300-\U0001FAFF]+", "", t)
    if core in GO_EXACT:
        return ("go", "")
    # 却下は『却下語が文の主旨（=core が却下語そのもの/末尾が却下語）かつ編集動詞を含まない』ときだけ。
    # 『一文足して』『やめ時の表現を直して』等の編集指示を部分一致で無音却下しない。
    if (core in REJECT_EXACT or core.endswith(_REJECT_SUFFIX)) and not _EDIT_VERB_RE.search(t):
        return ("reject", "")
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
        return observe.enforce_regulations(_strip_fake_mentions((llm.haiku(prompt, max_tokens=220) or "").strip()))
    except Exception:
        return ""


# 実メンションは呼び側が <@U…> で付与する。生成文中の裸の @名前 は架空メンション（Haiku が
# 「@修正担当者さん」等をでっち上げた＝2026-07-01 戸田さん指摘）なので無害化。
# 行頭/空白直後の @ だけ対象＝メールアドレス(toda@lipple.co.jp)や <@U…>(直前が<) は温存。
# 「@」1文字だけ落とし後続テキストは残す＝「@修正担当者さん、本文…」型で本文まで削らない（監査確定）。
_FAKE_MENTION = re.compile(r"(?<!\S)@(?=[^\s<>])")


def _strip_fake_mentions(s: str) -> str:
    return _FAKE_MENTION.sub("", s).strip()


# 催促・お礼・再確認は routine な機械リマインド＝決定論の固定文にする（Haiku 生成だと架空メンション
# 「@修正担当者さん」や誤った時期「先週」を作る＝2026-07-01 戸田さん指摘。silence と同じ方針）。
def _thanks() -> str:
    return "修正ありがとうございます！助かりました。"


def _remind_text() -> str:
    return ("お願いした表記修正について、まだ完了のご報告をいただけていません。"
            "ご対応いただけましたら、メンションで教えてください。")


def _recheck_text() -> str:
    return ("ご報告ありがとうございます。確認したところ、まだ反映されていないようです。"
            "お手数ですが、もう一度ご確認をお願いします。")


def _verify_fixed(it: dict, replies: list):
    """修正後の対象メッセージを検証。True=直っている / False=まだ未修正 / None=検証不能。
    検知語(verify_found 例「sns」)が編集後の対象メッセージから消えていれば修正済とみなす。
    検知側(notation_check)と同じく URL/リンク/コード内はマスクして照合＝URL内の残存文字で
    「未修正」と誤判定して直した人に再確認を送り続けない（監査確定バグ）。"""
    vf = it.get("verify_found")
    if not vf:
        return None
    src_ts = it.get("source_ts")
    root = next((m for m in replies if m.get("ts") == src_ts), None)
    if root is None:
        return None
    return vf not in observe._mask_noncontent(root.get("text", ""))


def _biz_hours(now: float) -> bool:
    """時間ベースの催促・stale通知を出してよい時間帯か（平日・祝日除く 9-20時 JST）。
    listener 経由で 24/7 走るため、cron 窓に頼らずコード側でガード（監査確定バグ：土曜深夜の催促）。"""
    import datetime as dt
    jst = dt.datetime.fromtimestamp(now, dt.timezone(dt.timedelta(hours=9)))
    return runtime.is_jp_workday(now) and 9 <= jst.hour < 20


def _save(pend: dict):
    """item の状態を変えた直後に都度保存＝途中クラッシュで『投稿済みなのに未保存』の窓を最小化
    （監査確定バグ：途中例外で同文 nudge の二重投稿）。save_json はアトミック・cron/listener は flock 排他。"""
    runtime.save_json("pending_approvals.json", pend)


def _phase_ruling(pend: dict) -> int:
    """Phase1: 戸田さんの裁定を処理。"""
    acted = 0
    items = pend.get("items", {})
    for tts, it in items.items():
        if it.get("status") != "pending":
            continue
        try:
            acted += _rule_one(pend, tts, it)
        except Exception as e:
            print(f"[apply-ruling] {tts}: ruling error {type(e).__name__}: {e} -> continue")
    return acted


def _rule_one(pend: dict, tts: str, it: dict) -> int:
    replies = source.read_thread(runtime.CH_CHIAKI_MGMT, tts)
    toda = [m for m in replies if m.get("user_id") == runtime.TODA and m.get("ts") != tts]
    if not toda:
        return 0
    ruling_text = toda[-1].get("text", "")
    verdict, payload = _classify(ruling_text)
    if verdict == "skip":
        return 0

    src_ch, src_ts = it.get("source_channel"), it.get("source_ts")
    tgt, draft = it.get("target_user_id") or "", it.get("draft", "")
    final, report = "", ""

    if verdict == "reject":
        it["status"], it["ruling_text"] = "reject", ruling_text
        report = "了解です。今回は出しません。"
    elif not (src_ch and src_ts and tgt):
        # 実行先が無い提案（停滞検知等）＝GO されても投稿できない。無音 skip で pending が
        # ゾンビ化していた（監査確定バグ）→ 1回だけ知らせて終端（unactionable）。
        it["status"], it["ruling_text"] = "unactionable", ruling_text
        report = ("この提案は対象スレッド・対象者を特定できないため、自動投稿ができません。"
                  "いったんクローズします（対応が必要でしたら手動でお願いします）。")
    else:
        if verdict == "go":
            final = draft
        else:
            final = _interpret(draft, payload)
            if not final:
                print(f"[apply-ruling] {tts}: interpret failed -> leave pending")
                return 0
        posted = source.post_thread_reply(src_ch, src_ts, f"<@{tgt}>\n{final}")
        nudge_ts = posted.get("ts") if isinstance(posted, dict) else None
        if not nudge_ts:  # 投稿失敗(ok:false/network/dry)＝松永さんへ届いていない → 状態を進めず pending のまま再試行
            print(f"[apply-ruling] {tts}: nudge post returned no ts -> leave pending, retry next run")
            return 0
        link = _permalink(src_ch, nudge_ts, src_ts)
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
    _save(pend)  # 投稿直後に永続化＝この後の例外でも再投稿しない
    runtime.append_jsonl("rulings.jsonl", {
        "ts": runtime.now_ts(), "thread_ts": tts, "verdict": verdict,
        "kind": it.get("finding_kind", ""), "original": draft,
        "final_text": final, "ruling_text": ruling_text})
    # 事後報告＝処理/独り言＝@Chiaki AI セルフメンション（戸田さんはpingしない）
    source.post_thread_reply(runtime.CH_CHIAKI_MGMT, tts, f"<@{runtime.CHIAKI_SELF}>\n{report}")
    return 1


def _phase_completion(pend: dict) -> int:
    """Phase2: 完了報告の検知＋修正の検証＋（未修正/未報告の）再リマインド。
      ・検証OK（直っている）  → 対象者へお礼＋戸田さんへ完了通知 → completed
      ・報告ありだが未修正    → 対象者へ「まだ直っていない」（1報告につき1回）
      ・未報告かつ未修正      → 時間ベースで再リマインド（最大 MAX_REMINDS 回・営業時間内のみ）
    """
    acted = 0
    items = pend.get("items", {})
    for tts, it in items.items():
        if it.get("status") != "awaiting_completion":
            continue
        try:
            acted += _complete_one(pend, tts, it)
        except Exception as e:
            print(f"[apply-ruling] {tts}: completion error {type(e).__name__}: {e} -> continue")
    return acted


def _complete_one(pend: dict, tts: str, it: dict) -> int:
    src_ch, src_ts = it.get("source_channel"), it.get("source_ts")
    tgt, nudge_ts = it.get("target_user_id") or "", it.get("nudge_ts") or "0"
    if not (src_ch and src_ts and tgt):
        return 0
    replies = source.read_thread(src_ch, src_ts)
    # 対象者の最新の完了報告（chiaki へのメンション or 完了語）。未来形・着手宣言は報告扱いしない。
    report = None
    for m in replies:
        if m.get("user_id") != tgt or float(m.get("ts", "0")) <= float(nudge_ts):
            continue
        txt = m.get("text", "")
        if _FUTURE_RE.search(txt) and not any(w in txt for w in COMPLETE_WORDS):
            continue  # 「すぐ直します！」等＝着手宣言。完了報告ではない（誤って「まだ反映されていない」と返さない）
        if f"<@{runtime.CHIAKI_SELF}>" in txt or any(w in txt for w in COMPLETE_WORDS):
            report = m
    fixed = _verify_fixed(it, replies)  # True=直った / False=未修正 / None=検証不能

    if fixed is True or (fixed is None and report):
        # 完了（検証OK、または検証不能だが報告あり）→ お礼＋戸田さんへ完了通知
        source.post_thread_reply(src_ch, src_ts, f"<@{tgt}>\n{_thanks()}")
        done_ts = report["ts"] if report else src_ts
        it["status"], it["completion_ts"] = "completed", done_ts
        _save(pend)  # お礼投稿直後に永続化＝以降の例外で二重お礼しない
        link = _permalink(src_ch, src_ts, src_ts)  # 該当箇所（修正された元メッセージ）への直リンク
        source.post_thread_reply(
            runtime.CH_CHIAKI_MGMT, tts,
            f"<@{runtime.TODA}>\n松永さんが修正を完了しました。\n\n{link}\n\nーーーーー")
        runtime.append_jsonl("rulings.jsonl", {
            "ts": runtime.now_ts(), "thread_ts": tts, "verdict": "completed",
            "kind": it.get("finding_kind", ""), "completion_ts": done_ts})
        return 1
    if report and fixed is False:
        # 報告は来たが まだ直っていない → 同じ報告には1回だけ指摘（spam防止）
        if it.get("last_checked_report_ts") != report["ts"]:
            source.post_thread_reply(src_ch, src_ts, f"<@{tgt}>\n{_recheck_text()}")
            it["last_checked_report_ts"] = report["ts"]
            _save(pend)
            return 1
        return 0
    # 未報告かつ未修正 → 時間ベースの再リマインド（営業時間外は出さない＝listener が24/7でも安全）
    now = runtime.now_ts()
    if not _biz_hours(now):
        return 0
    base = float(it.get("last_remind_ts") or it.get("nudge_ts") or 0)
    rc = it.get("remind_count", 0)
    if base and rc < MAX_REMINDS and (now - base) >= REMIND_EVERY_MIN * 60:
        source.post_thread_reply(src_ch, src_ts, f"<@{tgt}>\n{_remind_text()}")
        it["last_remind_ts"], it["remind_count"] = now, rc + 1
        _save(pend)
        return 1
    if base and rc >= MAX_REMINDS and (now - base) >= STALE_AFTER_MIN * 60:
        # 促しを使い切り、最後の促しから十分経っても無反応 → stale 終端。
        # chiaki-pdca の未対応カウントは pending/awaiting_completion のみ集計＝stale は自動で外れる。戸田へ1回だけ通知。
        it["status"], it["stale_ts"] = "stale", now
        _save(pend)
        link = _permalink(src_ch, src_ts, src_ts)
        source.post_thread_reply(
            runtime.CH_CHIAKI_MGMT, tts,
            f"<@{runtime.TODA}>\n促しを{MAX_REMINDS}回送っても反応がないため、いったん見送り扱い（stale）にします。\n{link}")
        return 1
    return 0


def main():
    pend = runtime.load_json("pending_approvals.json", {"items": {}})
    if not pend.get("items"):
        print("[apply-ruling] no pending")
        return
    ruled = _phase_ruling(pend)
    completed = _phase_completion(pend)
    if ruled or completed:
        runtime.save_json("pending_approvals.json", pend)
    print(f"[apply-ruling] ruled={ruled} completed={completed}")


if __name__ == "__main__":
    main()
