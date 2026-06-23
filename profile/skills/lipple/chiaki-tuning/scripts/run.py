#!/usr/bin/env python3
"""chiaki-tuning（#8902/#5902 での戸田さんとの対話：指示は学習／質問は回答／決定論＋Haiku・ツール無し）。

戸田さんのメッセージを Haiku で種別判定:
  - directive: Chiaki AI の振る舞い/文面への指示。
        soft(文面調整) → tuning.json に蓄積＋ackを返す（必要なら対象投稿も編集）。
        hard(コード変更が要る: リンク差替/ロジック/しきい値/時間/新機能/バグ等) → 学習せず
        「ご指摘ありがとうございます！Slackでは対応できないのでClaude Codeをお使いください。」と正直に返す。
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
from lib import runtime, source, notion  # noqa: E402

SKILLS = {"silence", "pdca", "propose", "notation", "stall", "general"}
CAP = 12  # skill ごとに保持する指示の最大数（トンマナ一括ロードに対応）
JST = dt.timezone(dt.timedelta(hours=9))


def _tsd(ts) -> str:
    try:
        return dt.datetime.fromtimestamp(float(ts), JST).strftime("%Y-%m-%d")
    except Exception:
        return ""


def _classify(text: str, hint: str = "", context: str = ""):
    """戸田さんの投稿を {type, skill, directive, ack, scope} に分類。type ∈ directive|question|none。失敗時 None。"""
    try:
        from lib import llm
    except Exception:
        return None
    prompt = (
        "次は戸田さんが Chiaki AI（社内タスク管理AI）に送ったメッセージです。"
        + (f"（文脈: {hint}）" if hint else "")
        + (f"\n直近のやりとり(古い順・terseな指示はこの文脈で解釈):\n{context}\n" if context else "")
        + "種別を判定:\n"
        "- directive: Chiaki AI の振る舞い/文面/運用への指示・フィードバック\n"
        "- question: Chiaki AI への質問・依頼（例『まとめて』『教えて』『どう？』『何を学んだ？』）\n"
        "- none: それ以外\n"
        f"メッセージ: {text}\n"
        "directive の場合のみ skill(silence/pdca/propose/notation/stall/general)・"
        "directive(今後守る指示1文)・ack(短い了解文)・scope も付ける。用語統一や全般の言い回しは general。\n"
        "scope は soft か hard。chiaki が Slack 上でできるのは『今後の文面ルールの学習』と"
        "『いま返信しているスレッドの1投稿だけの文面書き換え』のみ。それを超えるものは hard:\n"
        "- soft: 言い回し・トーン・敬語・絵文字・記号・句読点・形式・行数・呼称・レギュレーション用語・"
        "定型文の文言など『文章の調整』で、今後ルールの学習 or いま返信している1投稿の修正で済むもの。\n"
        "- hard（コード/一括処理が要る・Slackでは即対応不可）:\n"
        "  ・リンク/URL/パーマリンク/メッセージID/チャンネルID を『どれにするか・どこへ向けるか』の指示は、"
        "『〜でいいよ』『〜にして』のような柔らかい言い方でも、chiakiは正しいリンク/IDを特定・計算できないため必ず hard。\n"
        "  ・『前後の投稿も』『他の投稿も』『過去の投稿も』『全部直して』など、いま返信している1投稿を超えて"
        "複数・過去の投稿を遡って直す依頼（chiakiは1投稿しか編集できず一括修正はできない）。\n"
        "  ・検知ロジック・しきい値・時間/スケジュールの変更、新機能・チャンネル追加、バグ修正、"
        "観測対象の変更、値の計算や参照が必要な修正。\n"
        "例: 『リンクは該当箇所のリンクでいい』=hard / 『停滞は90分に変えて』=hard / "
        "『そのあとに投稿したものも直して』=hard / 『その前にもあった(＝前の投稿も直して)』=hard / "
        "『今後は引き続きの後に読点をつけて』=soft / 『もっと簡潔に・絵文字なし』=soft。\n"
        'JSON のみ: {"type":"directive|question|none","skill":"...","directive":"...","ack":"...","scope":"soft|hard"}'
    )
    out = llm.haiku(prompt, max_tokens=300) or ""
    m = re.search(r"\{.*\}", out, re.S)
    if not m:
        return None
    try:
        return json.loads(m.group(0))
    except Exception:
        return None


def _thread_context(ch: str, root: str, before_ts: str, n: int = 6) -> str:
    """terse な追従指示（『その前にもあった』等）を解釈するため、同スレッド・当該より前のやりとり（古い順）。"""
    if not root or root == before_ts:
        return ""
    try:
        msgs = [m for m in source.read_thread(ch, root) if m.get("ts_float", 0) < float(before_ts)]
    except Exception:
        return ""
    who = {runtime.TODA: "戸田", runtime.CHIAKI_SELF: "chiaki"}
    return "\n".join(f"- {who.get(m.get('user_id'), m.get('user_id') or '?')}: {(m.get('text') or '')[:120]}"
                     for m in msgs[-n:])


def _answer(question: str, ch: str, root: str) -> str:
    """状態（スレッドのやりとり＋学習済みtuning＋本日の観測）から Haiku がテキストで回答。ツール無し。"""
    try:
        from lib import llm
    except Exception:
        return ""
    thread = source.read_thread(ch, root)
    convo = "\n".join(f"- {(m.get('user_name') or m.get('user_id') or '?')}: {m.get('text', '')[:200]}"
                      for m in thread[-15:])
    # 「このやりとり/今回/ここ」と言われたら、このスレッドの会話内だけから拾う（全体tuningを持ち出さない）
    scoped = any(w in question for w in ("このやりとり", "ここ", "今回", "この件",
                                         "このスレッド", "この流れ", "この会話", "上記"))
    prompt = ("あなたは Chiaki AI。戸田さんの依頼に簡潔に答えます（テキストのみ・絵文字なし・です/ます・要点）。"
              "分からないことは推測せず正直に言う。太字や * による強調は使わない。\n"
              f"依頼: {question}\n"
              f"このスレッドのやりとり:\n{convo}\n")
    if scoped:
        prompt += ("依頼は『このやりとり/今回』に限定されています。"
                   "**上の『このスレッドのやりとり』の中で実際に出た指摘・合意・変更点だけ**を拾って答えてください。"
                   "スレッド外の一般的なレギュレーションや既存の学習済みルールは持ち出さない。"
                   "このスレッドで該当が無ければ『このやりとりでは特にありません』と答える。")
    else:
        tuning = runtime.load_json("tuning.json", {})
        learned = "; ".join(d.get("directive", "") for lst in tuning.values() for d in lst) or "（まだ無し）"
        today = dt.datetime.now(JST).strftime("%Y-%m-%d")
        fk = Counter(f.get("kind") for f in runtime.read_jsonl("findings.jsonl") if _tsd(f.get("ts")) == today)
        rv = Counter(r.get("verdict") for r in runtime.read_jsonl("rulings.jsonl") if _tsd(r.get("ts")) == today)
        prompt += (f"あなたが全体で学習済みの指示: {learned}\n"
                   f"本日の観測: 表記{fk.get('notation', 0)}・誤字{fk.get('typo', 0)}・停滞{fk.get('stall', 0)}件、"
                   f"裁定 GO/反映{rv.get('go', 0) + rv.get('interpret', 0)}・完了{rv.get('completed', 0)}件。\n"
                   "依頼に沿って上記から答える。")
    return (llm.haiku(prompt, max_tokens=450) or "").strip()


def _revise(text: str, skill: str, instruction: str = "") -> str:
    """既存投稿を、今回の指示(最優先)＋学習済み指示に従って最小限だけ修正。Haiku・ツール無し。"""
    if not (text or "").strip():
        return ""
    try:
        from lib import llm
    except Exception:
        return ""
    directives = list(dict.fromkeys(
        ([instruction.strip()] if instruction.strip() else []) + runtime.load_tuning(skill)))
    if not directives:
        return ""
    n = text.count("\n")
    prompt = ("次の Chiaki AI の投稿を、以下の指示に従って最小限だけ修正してください（先頭の指示を最優先）。"
              "**改行位置と行数は厳守（行を結合も分割もしない）**。事実・数字・構成は変えない。"
              "先頭の <@..> や <!channel> はそのまま残す。修正後の本文のみ出力（前置きなし）。\n"
              f"指示: {'; '.join(directives)}\n投稿:\n{text}")
    out = (llm.haiku(prompt, max_tokens=400) or "").strip()
    # 行数が変わった＝構成を壊したら採用しない（3行ルール等を守る）
    return out if (out and out.count("\n") == n and out != text) else ""


_PERMALINK = re.compile(r"/archives/(C[A-Z0-9]+)/p(\d{10})(\d{6})")


def _resolve_link(raw: str):
    """戸田さんのメッセージ中の Slack パーマリンク → (channel, ts, thread_root)。無ければ None。"""
    mm = _PERMALINK.search(raw or "")
    if not mm:
        return None
    tch, ts = mm.group(1), mm.group(2) + "." + mm.group(3)
    tm = re.search(r"thread_ts=([\d.]+)", raw)
    return tch, ts, (tm.group(1) if tm else ts)


def _edit_post(tch: str, tts: str, parent: str, skill: str, instruction: str) -> bool:
    """tch/tts の chiaki 投稿を、指示＋tuning で最小修正（chat.update）。編集したら True。"""
    msg = next((x for x in source.read_thread(tch, parent) if x.get("ts") == tts), None)
    if not msg or msg.get("user_id") != runtime.CHIAKI_SELF:
        return False
    edit_skill = "pdca" if tch == runtime.CH_CHIAKI_PDCA else skill
    revised = _revise(msg.get("text", ""), edit_skill, instruction)
    if revised and revised.strip() != (msg.get("text", "") or "").strip():
        source.update_message(tch, tts, revised)
        print(f"[tuning] edited post ch={tch} ts={tts}")
        return True
    return False


def _maybe_edit_root(ch: str, root: str, skill: str, instruction: str = "", raw: str = ""):
    """編集対象を決めて修正する。
    1) 戸田さんのメッセージに Slack リンクがあれば、そのリンク先の投稿（チャンネル跨ぎ可）。
    2) なければ、同スレッド内の最新の実質的 chiaki 投稿（ack＝<@CHIAKI_SELF>始まりは除く）。"""
    resolved = _resolve_link(raw)
    if resolved:
        tch, tts, parent = resolved
        return _edit_post(tch, tts, parent, skill, instruction)
    self_tag = f"<@{runtime.CHIAKI_SELF}>"
    posts = [m for m in source.read_thread(ch, root)
             if m.get("user_id") == runtime.CHIAKI_SELF
             and not (m.get("text") or "").lstrip().startswith(self_tag)]
    if not posts:
        return False
    t = posts[-1]
    return _edit_post(ch, t["ts"], root, skill, instruction)


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
        c = _classify(m["text"], hint, _thread_context(ch, root, m["ts"]))
        if not c:
            continue
        typ = c.get("type")
        if typ == "directive":
            # コード対応が要る指示は学習せず、Notionへ起票して正直に返す（PR/自動デプロイは別途・保留）
            if (c.get("scope") or "soft").strip().lower() == "hard":
                slack_url = (f"https://lipple.slack.com/archives/{ch}"
                             f"/p{m['ts'].replace('.', '')}?thread_ts={root}&cid={ch}")
                ch_label = ("#8902" if ch == runtime.CH_CHIAKI_MGMT
                            else "#5902" if ch == runtime.CH_CHIAKI_PDCA else "")
                summary = (c.get("directive") or m["text"]).strip()
                page_url = notion.create_request(summary, m["text"], slack_url, ch_label)
                runtime.append_jsonl("code_requests.jsonl", {
                    "ts": runtime.now_ts(), "channel": ch, "thread": root,
                    "text": m["text"], "directive": (c.get("directive") or "").strip(),
                    "notion_url": page_url})
                if page_url:  # 保存できた時だけ「保存しました」と言う（嘘をつかない）
                    msg = ("上記の指示はSlackのやりとりでは対応できないので、"
                           "AIコーディングエージェントをお使いください！NotionのDBに保存しました。")
                    body = f"<@{runtime.TODA}>\n{runtime.ensure_punct(msg)}\n\n{page_url}"
                else:
                    msg = ("上記の指示はSlackのやりとりでは対応できないので、"
                           "AIコーディングエージェントをお使いください！")
                    body = f"<@{runtime.TODA}>\n{runtime.ensure_punct(msg)}"
                source.post_thread_reply(ch, root, body)
                acted += 1
                continue
            skill = c.get("skill") if c.get("skill") in SKILLS else "general"
            directive = (c.get("directive") or "").strip()
            if not directive:
                continue
            tuning.setdefault(skill, []).append({"directive": directive, "ts": runtime.now_ts()})
            tuning[skill] = tuning[skill][-CAP:]
            edited = _maybe_edit_root(ch, root, skill, directive, m["text"])  # 今回の指示で対象投稿を実際に編集
            ack = "修正しました。" if edited else ((c.get("ack") or "").strip() or "承知しました。今後反映します。")
            source.post_thread_reply(ch, root, f"<@{runtime.TODA}>\n{runtime.ensure_punct(ack)}")  # 戸田さん宛て・句読点保証
            acted += 1
        elif typ == "question":
            ans = _answer(m["text"], ch, root)
            if ans:
                source.post_thread_reply(ch, root, f"<@{runtime.TODA}>\n{runtime.ensure_punct(ans)}")  # 戸田さん宛て
                acted += 1
    if tuning:
        runtime.save_json("tuning.json", tuning)
    for ch, mx in maxts.items():
        cur[ch] = mx
    runtime.save_json("tuning_cursor.json", cur)
    print(f"[tuning] acted={acted}")


if __name__ == "__main__":
    main()
