#!/usr/bin/env python3
"""chiaki-intake（指摘の起票経路・1窓口 / 決定論＋Haiku・ツール無し＝安全）。

戸田さんの指摘を Haiku で種別判定し、**必ず一度きいてから**該当 Notion DB に未対応/未承認で起票する（着手C）。
  - issue : 不具合・要望（既存 hard 相当）→ 案提示→確認→ Issue_Chiaki_AI_DB（未対応・種別 バグ/変更/新機能/その他）
  - rule  : 言葉のルール（既存 soft 相当・トーン/用語/表記）→ 案提示→確認→ Rule Registry（未承認・種別 用語/レギュレーション/スタイル）
  - edit  : この投稿そのものを今すぐ直す依頼 → その場で chat.update（起票しない）
  - question: 質問・依頼 → Haiku がテキスト回答
  - unclear : 曖昧 → まず確認質問（案を出さない）
2ターン（案提示→戸田確認 OK/修正/振り分け変更→起票→「登録しました！」＋URL）。振り分けは戸田さんが上書き可。
承認→正本反映（§4.4）は別フロー（ここは起票まで）。
対象: #8902/#5902（全 戸田 投稿）＋ #5035/#a027（@メンション・確認待ちスレッドの返信）。listener 即時＋ cron */2 9-19 backstop。
"""
import datetime as dt
import json
import os
import re
import sys
from collections import Counter
from pathlib import Path
sys.path.insert(0, os.environ.get("HERMES_LIB") or str(Path(__file__).resolve().parents[5]))
from lib import observe, runtime, source, notion  # noqa: E402

JST = dt.timezone(dt.timedelta(hours=9))
MENTION = f"<@{runtime.CHIAKI_SELF}>"
WATCH_EXTRA = (runtime.CH_YU_PDCA, runtime.CH_NICHIJI)  # #5035/#a027（@メンションはどこでも受ける）
ISSUE_KINDS = {"バグ", "変更", "新機能", "その他"}
RULE_KINDS = {"用語", "レギュレーション", "スタイル"}
INTAKE_TIMEOUT_SEC = 24 * 3600  # 確認が来ない案は24hで失効
_PROPOSE_CAP = 4               # 再提示の上限（堂々巡り防止）
_GO = {"go", "ok", "okです", "おk", "おけ", "ｏｋ", "了解", "りょうかい", "承認", "いいね", "いいよ",
       "はい", "おねがいします", "お願いします", "それで", "それでお願いします", "登録して", "ok!"}
_REJECT = ("却下", "やめ", "なしで", "見送", "いらない", "ボツ", "流して", "今回はいい", "結構です", "やめて")


def _tsd(ts) -> str:
    try:
        return dt.datetime.fromtimestamp(float(ts), JST).strftime("%Y-%m-%d")
    except Exception:
        return ""


def _ch_label(ch: str) -> str:
    return ("#8902" if ch == runtime.CH_CHIAKI_MGMT
            else "#5902" if ch == runtime.CH_CHIAKI_PDCA else "")  # Issue DB のチャンネルは#8902/#5902のみ


def _permalink(ch: str, ts: str, parent: str) -> str:
    return f"https://lipple.slack.com/archives/{ch}/p{ts.replace('.', '')}?thread_ts={parent}&cid={ch}"


# ── 分類（propose ターン） ─────────────────────────────
def _classify_intake(text: str, context: str = ""):
    """戸田さんの指摘を {type, issue_kind, rule_kind, 要約, 詳細, 誤例, 正例, 確信度} に分類。失敗時 None。"""
    try:
        from lib import llm
    except Exception:
        return None
    body = re.sub(rf"{re.escape(MENTION)}|<@U[A-Z0-9]+>", "", text or "").strip()
    prompt = (
        "次は戸田さんが Chiaki AI（社内タスク管理AI）に送った指摘/依頼です。種別を1つに判定:\n"
        "- issue: Chiaki AI の不具合・要望（動作の問題/機能追加/変更/バグ）。issue_kind=バグ|変更|新機能|その他。\n"
        "- rule: 言葉のルールを“今後のルール”として覚えるべき指摘。rule_kind="
        "スタイル(声/トーン/温度・例『もっとラフに』『！多すぎ』)|用語(固有名詞や語の統一)|レギュレーション(表記/約物/語尾)。\n"
        "- edit: いま出ている“この投稿そのもの”を今すぐ直す依頼（『この一文消して』『今回直して』『ここ柔らかく』）。\n"
        "- question: 質問・依頼（『まとめて』『教えて』『何件？』）。\n"
        "- unclear: 指摘だが種別が曖昧（『なんか違う』等）。確信が低い時もここ。\n"
        "- none: 指摘・依頼・質問のいずれでもない（雑談・お礼・相づち・FYI・了承だけ 等）。何もしない。\n"
        + (f"直前のやりとり(古い順):\n{context}\n" if context else "")
        + f"メッセージ: {body}\n"
        "要約(title用・一言[:200])・詳細(背景や直し方)・(rule時のみ)誤例/正例・確信度(0〜1) も付ける。"
        "指摘・依頼・質問でなければ none。種別が曖昧な“指摘”だけ unclear。\n"
        'JSON のみ: {"type":"issue|rule|edit|question|unclear|none","issue_kind":"","rule_kind":"",'
        '"要約":"","詳細":"","誤例":"","正例":"","確信度":0.0}'
    )
    out = llm.haiku(prompt, max_tokens=400) or ""
    m = re.search(r"\{.*\}", out, re.S)
    if not m:
        return None
    try:
        c = json.loads(m.group(0))
    except Exception:
        return None
    if c.get("type") == "issue" and c.get("issue_kind") not in ISSUE_KINDS:
        c["issue_kind"] = "その他"
    if c.get("type") == "rule" and c.get("rule_kind") not in RULE_KINDS:
        c["rule_kind"] = "スタイル"
    # 低確信は unclear に倒す（誤起票より確認）
    try:
        if c.get("type") in ("issue", "rule") and float(c.get("確信度", 1)) < 0.45:
            c["type"] = "unclear"
    except Exception:
        pass
    return c


def _verdict(text: str) -> str:
    """確認ターンの戸田さん返信 → go / reject / reclassify。
    @メンション（app_mention 返信で必ず付く）を除去してから判定する（除かないと『はい』が go に一致しない）。"""
    t = re.sub(rf"{re.escape(MENTION)}|<@U[A-Z0-9]+>", "", text or "").strip()
    core = t.strip(" 　。、！!.?？\n\r\t")
    if any(w in t for w in _REJECT):
        return "reject"
    tokens = [x for x in re.split(r"[、。！!\?？\s　]+", core) if x]
    if core.lower() in _GO or any(tok.lower() in _GO for tok in tokens):
        return "go"
    return "reclassify"  # 文面修正・振り分け変更・補足はすべて再分類して再提示


def _propose_text(p: dict) -> str:
    """案提示の文面（柔らかく・詰めない＝スタイル第1部E）。"""
    t = p.get("type")
    s = (p.get("要約") or "").strip()
    if t == "issue":
        return f"Issueに「{s}／種別={p.get('issue_kind') or 'その他'}」で登録してもいいですか？"
    if t == "rule":
        return f"{p.get('rule_kind') or 'スタイル'}に「{s}」で登録してもいいですか？"
    return ("これは不具合の話ですか？それとも言葉のルール（トーンや表記）の話ですか？"
            "どう直すのがいいか、もう少し具体的に教えてもらえますか？")


def _reply(ch: str, root: str, body: str, url: str = "") -> None:
    b = runtime.ensure_punct(observe.enforce_regulations(body))
    if url:
        b += f"\n{url}"
    source.post_thread_reply(ch, root, f"<@{runtime.TODA}>\n{b}")


def _file_issue(p: dict, permalink: str, ch: str):
    return notion.create_request(p.get("要約", ""), p.get("詳細", ""), slack_url=permalink,
                                 channel_label=_ch_label(ch), kind=p.get("issue_kind") or "その他")


def _file_rule(p: dict, permalink: str):
    return notion.create_rule_registry(p.get("要約", ""), p.get("詳細", ""),
                                       p.get("rule_kind") or "スタイル", slack_url=permalink,
                                       wrong=p.get("誤例", ""), right=p.get("正例", ""))


# ── 既存の再利用ヘルパ（編集・回答・リンク解決） ───────────────
def _thread_context(ch: str, root: str, before_ts: str, n: int = 6) -> str:
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
    try:
        from lib import llm
    except Exception:
        return ""
    thread = source.read_thread(ch, root)
    convo = "\n".join(f"- {(m.get('user_name') or m.get('user_id') or '?')}: {m.get('text', '')[:200]}"
                      for m in thread[-15:])
    scoped = any(w in question for w in ("このやりとり", "ここ", "今回", "この件",
                                         "このスレッド", "この流れ", "この会話", "上記"))
    prompt = ("あなたは Chiaki AI。戸田さんの依頼に簡潔に答えます（テキストのみ・絵文字なし・です/ます・要点）。"
              "分からないことは推測せず正直に言う。太字や * による強調は使わない。\n"
              f"依頼: {question}\nこのスレッドのやりとり:\n{convo}\n")
    if scoped:
        prompt += ("依頼は『このやりとり/今回』に限定されています。"
                   "**上の『このスレッドのやりとり』の中で実際に出た指摘・合意・変更点だけ**を拾って答えてください。"
                   "スレッド外の一般論は持ち出さない。該当が無ければ『このやりとりでは特にありません』と答える。")
    else:
        tuning = runtime.load_json("tuning.json", {})
        learned = "; ".join(d.get("directive", "") for lst in tuning.values() for d in lst) or "（まだ無し）"
        today = dt.datetime.now(JST).strftime("%Y-%m-%d")
        fk = Counter(f.get("kind") for f in runtime.read_jsonl("findings.jsonl") if _tsd(f.get("ts")) == today)
        rv = Counter(r.get("verdict") for r in runtime.read_jsonl("rulings.jsonl") if _tsd(r.get("ts")) == today)
        prompt += (f"学習済みの指示: {learned}\n"
                   f"本日の観測: 表記{fk.get('notation', 0)}・誤字{fk.get('typo', 0)}・停滞{fk.get('stall', 0)}件、"
                   f"裁定 GO/反映{rv.get('go', 0) + rv.get('interpret', 0)}・完了{rv.get('completed', 0)}件。\n"
                   "依頼に沿って上記から答える。")
    return (llm.haiku(prompt, max_tokens=450) or "").strip()


_REFLOW_RE = re.compile(r"改行|空行|行間|レイアウト|間隔|スペース|詰め|空け|あけ|開け")


def _revise(text: str, instruction: str = "") -> str:
    if not (text or "").strip() or not instruction.strip():
        return ""
    try:
        from lib import llm
    except Exception:
        return ""
    n = text.count("\n")
    # 改行・空白そのものを変える指示（『URLの上は改行して』等）は行数固定を解除する。
    reflow = bool(_REFLOW_RE.search(instruction))
    layout = ("指示どおりに改行・空白だけ調整してよい（本文の文言・数字・順序・絵文字は変えない）"
              if reflow else "改行位置と行数は厳守（行を結合も分割もしない）")
    prompt = ("次の Chiaki AI の投稿を、以下の指示に従って最小限だけ修正してください。"
              f"**{layout}**。事実・数字・構成は変えない。"
              "先頭の <@..> や <!channel> はそのまま残す。修正後の本文のみ出力（前置きなし）。\n"
              f"指示: {instruction}\n投稿:\n{text}")
    out = (llm.haiku(prompt, max_tokens=400) or "").strip()
    if not out or out == text:
        return ""
    if reflow:  # 行数は変わってよいが、本文（空白以外）が大きく欠落していないこと
        a, b = re.sub(r"\s", "", out), re.sub(r"\s", "", text)
        return out if (a and len(a) >= len(b) * 0.8) else ""
    return out if out.count("\n") == n else ""


_PERMALINK = re.compile(r"/archives/(C[A-Z0-9]+)/p(\d{10})(\d{6})")


def _resolve_link(raw: str):
    mm = _PERMALINK.search(raw or "")
    if not mm:
        return None
    tch, ts = mm.group(1), mm.group(2) + "." + mm.group(3)
    tm = re.search(r"thread_ts=([\d.]+)", raw)
    return tch, ts, (tm.group(1) if tm else ts)


def _edit_post(tch: str, tts: str, parent: str, instruction: str) -> str:
    """edited / notfound / norevise を返す（特定できたか・直せたかを区別）。
    機械的な修正（レギュレーション/スペース/全角/URL空行）は決定論を優先し、Haiku には頼らない。"""
    msg = next((x for x in source.read_thread(tch, parent) if x.get("ts") == tts), None)
    if not msg or msg.get("user_id") != runtime.CHIAKI_SELF:
        return "notfound"
    text = msg.get("text", "")
    # 1) まず決定論で直す＝確実（曖昧な指示でも表記違反は必ず直る）
    enforced = observe.enforce_regulations(text)
    if enforced != text:
        source.update_message(tch, tts, enforced)
        print(f"[intake] edited(enforce) ch={tch} ts={tts}")
        return "edited"
    # 2) 決定論で変化なし → 具体指示があれば Haiku で（文面の言い換え等）
    revised = _revise(text, instruction)
    if revised and revised.strip() != (text or "").strip():
        source.update_message(tch, tts, observe.enforce_regulations(revised))
        print(f"[intake] edited(revise) ch={tch} ts={tts}")
        return "edited"
    return "norevise"


def _maybe_edit_root(ch: str, root: str, instruction: str = "", raw: str = "") -> str:
    """『この投稿を今直して』系。優先順＝① raw 内 permalink、② スレッド先頭（戸田が指摘している“この投稿”）、
    ③ 同スレッド最新の実質 chiaki 投稿（ack除く）。edited / notfound / norevise を返す。"""
    resolved = _resolve_link(raw)
    if resolved:
        return _edit_post(*resolved, instruction)
    thread = source.read_thread(ch, root)
    rootmsg = next((m for m in thread if m.get("ts") == root), None)
    if rootmsg and rootmsg.get("user_id") == runtime.CHIAKI_SELF:
        return _edit_post(ch, root, root, instruction)  # スレッドで指摘される“この投稿”＝先頭
    self_tag = f"<@{runtime.CHIAKI_SELF}>"
    posts = [m for m in thread
             if m.get("user_id") == runtime.CHIAKI_SELF
             and not (m.get("text") or "").lstrip().startswith(self_tag)]
    return _edit_post(ch, posts[-1]["ts"], root, instruction) if posts else "notfound"


_EDIT_MSG = {
    "edited": "直しました！",
    "norevise": ("うまく汲み取れませんでした。Slack で直せる表記の話なら、どこをどう直すか具体的に教えてください。"
                 "ロジックや機能などコード対応が要る内容なら、AIコーディングエージェントをお使いください。"),
    "notfound": "該当の投稿が特定できませんでした。どの投稿か教えてもらえますか？",
}


# ── 候補収集（propose/confirm 両ターン） ───────────────────
def _candidates(cur: dict, items: dict):
    """戸田さんの新規メッセージ候補 [(msg, thread_root, channel, hint)]。"""
    mgmt, pdca = runtime.CH_CHIAKI_MGMT, runtime.CH_CHIAKI_PDCA
    pend = runtime.load_json("pending_approvals.json", {"items": {}}).get("items", {})
    open_threads = {ts for ts, it in pend.items() if it.get("status") in ("pending", "awaiting_completion")}
    awaiting = {(it.get("channel"), it.get("thread_root")) for it in items.values()
                if it.get("status") == "awaiting_confirm"}
    cand = []
    # #8902/#5902：戸田さんの全投稿（top-level＋提案以外スレッド返信）
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
                    cand.append((r, m["ts"], pdca, ""))
    # #5035/#a027：@メンション（どこでも）＋ 確認待ちスレッドの戸田返信のみ（負荷を抑えて拾う）
    for ch in WATCH_EXTRA:
        since = float(cur.get(ch, 0.0))
        for m in source.read_recent(ch, limit=50):
            if (m["user_id"] == runtime.TODA and m["ts_float"] > since and MENTION in (m.get("text") or "")):
                cand.append((m, m["ts"], ch, ""))
            scan = (m.get("thread_replies") and
                    (m.get("user_id") == runtime.CHIAKI_SELF or (ch, m["ts"]) in awaiting))
            if scan:
                for r in source.read_thread(ch, m["ts"]):
                    if (r["ts"] != m["ts"] and r["user_id"] == runtime.TODA and r["ts_float"] > since
                            and (MENTION in (r.get("text") or "") or (ch, m["ts"]) in awaiting)):
                        cand.append((r, m["ts"], ch, ""))
    return cand


def _find_awaiting(items: dict, ch: str, root: str, msg_ts: str):
    for it in items.values():
        if (it.get("status") == "awaiting_confirm" and it.get("channel") == ch
                and it.get("thread_root") == root
                and float(msg_ts) > float(it.get("last_seen_ts", 0))
                and msg_ts != it.get("mention_ts")):
            return it
    return None


def _expire_stale(items: dict) -> None:
    now = runtime.now_ts()
    for it in items.values():
        if it.get("status") == "awaiting_confirm" and now - float(it.get("proposed_at", now)) > INTAKE_TIMEOUT_SEC:
            it["status"] = "expired"


def _handle_propose(m: dict, ch: str, root: str, items: dict) -> int:
    c = _classify_intake(m["text"], _thread_context(ch, root, m["ts"]))
    if not c:
        return 0
    typ = c.get("type")
    if typ == "edit":
        st = _maybe_edit_root(ch, root, m["text"], m["text"])  # 生指示＝改行/空白の語を保つ
        _reply(ch, root, _EDIT_MSG[st])
        return 1
    if typ == "question":
        ans = _answer(m["text"], ch, root)
        if ans:
            _reply(ch, root, ans)
            return 1
        return 0
    if typ in ("issue", "rule", "unclear"):
        items[m["ts"]] = {"status": "awaiting_confirm", "channel": ch, "thread_root": root,
                          "mention_ts": m["ts"], "mention_text": m["text"],
                          "permalink": _permalink(ch, m["ts"], root), "proposal": c,
                          "proposed_at": runtime.now_ts(), "last_seen_ts": m["ts"], "propose_count": 1}
        _reply(ch, root, _propose_text(c))
        return 1
    return 0


def _handle_confirm(it: dict, m: dict, ch: str, root: str) -> int:
    it["last_seen_ts"] = m["ts"]
    v = _verdict(m["text"])
    p = it.get("proposal", {})
    if v == "reject":
        it["status"] = "cancelled"
        _reply(ch, root, "わかりました、今回は見送りますね。")
        return 1
    # unclear はまだ起票候補が定まっていない → 返信を手掛かりに再分類（go でも reclassify）
    if v == "go" and p.get("type") in ("issue", "rule"):
        url = _file_issue(p, it["permalink"], ch) if p["type"] == "issue" else _file_rule(p, it["permalink"])
        it["status"], it["page_url"] = "filed", url
        # rule は登録するだけでなく、指摘元の投稿も決定論で直す（戸田: 登録＋編集して修正）
        extra = ""
        if p["type"] == "rule" and url:
            if _maybe_edit_root(ch, root, "", it.get("mention_text", "")) == "edited":
                extra = "\n指摘のあった投稿も直しました。"
        if url:
            _reply(ch, root, "登録しました！" + extra, url=url)
        elif not notion._token():
            _reply(ch, root, "登録しました！（ローカル確認のため実際の保存はしていません）")
        else:
            _reply(ch, root, "起票に失敗しました。DBの共有を確認してもらえますか？")
        return 1
    # reclassify（文面修正・振り分け変更・unclear の手掛かり）
    if it.get("propose_count", 1) >= _PROPOSE_CAP:
        _reply(ch, root, "うまく汲み取れていないかもしれません。「これでOK」か「却下」で教えてください。")
        return 1
    c2 = _classify_intake(f"{it.get('mention_text', '')} / 戸田さんの指示: {m['text']}",
                          _thread_context(ch, root, m["ts"]))
    if c2 and c2.get("type") in ("issue", "rule", "unclear"):
        it["proposal"] = c2
        it["propose_count"] = it.get("propose_count", 1) + 1
        _reply(ch, root, _propose_text(c2))
        return 1
    if c2 and c2.get("type") == "edit":
        st = _maybe_edit_root(ch, root, m["text"], m["text"])
        it["status"] = "cancelled"
        _reply(ch, root, _EDIT_MSG[st])
        return 1
    return 0


def main():
    cur = runtime.load_json("tuning_cursor.json", {})
    intake = runtime.load_json("chiaki_intake.json", {"items": {}})
    items = intake.setdefault("items", {})
    _expire_stale(items)
    cand = _candidates(cur, items)
    if not cand:
        runtime.save_json("chiaki_intake.json", intake)  # 失効状態は保存
        print("[intake] nothing new")
        return
    seen, uniq = set(), []
    for c in sorted(cand, key=lambda x: x[0]["ts_float"]):
        key = (c[2], c[0]["ts"])
        if key in seen:
            continue
        seen.add(key)
        uniq.append(c)
    maxts, acted = {}, 0
    for m, root, ch, _hint in uniq:
        maxts[ch] = max(maxts.get(ch, float(cur.get(ch, 0.0))), m["ts_float"])
        it = _find_awaiting(items, ch, root, m["ts"])
        try:
            acted += _handle_confirm(it, m, ch, root) if it else _handle_propose(m, ch, root, items)
        except Exception as e:
            print(f"[intake] error ch={ch} ts={m['ts']}: {e}")
    runtime.save_json("chiaki_intake.json", intake)
    for ch, mx in maxts.items():
        cur[ch] = mx
    runtime.save_json("tuning_cursor.json", cur)
    print(f"[intake] acted={acted}")


if __name__ == "__main__":
    main()
