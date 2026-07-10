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
import threading
import time
from collections import Counter
from pathlib import Path
sys.path.insert(0, os.environ.get("HERMES_LIB") or str(Path(__file__).resolve().parents[5]))
from lib import observe, runtime, source, notion  # noqa: E402

JST = dt.timezone(dt.timedelta(hours=9))
MENTION = f"<@{runtime.CHIAKI_SELF}>"
WATCH_EXTRA = (runtime.CH_YU_PDCA, runtime.CH_NICHIJI)  # #5035/#a027（@メンションはどこでも受ける）
ISSUE_KINDS = {"バグ", "変更", "新機能", "その他"}
RULE_KINDS = {"用語", "レギュレーション", "スタイル"}
INTAKE_TIMEOUT_SEC = 7 * 24 * 3600  # 確認が来ない案は7日で失効（24hだと金曜起票→月曜返信が無音で死ぬ＝監査確定）
_PROPOSE_CAP = 4               # 再提示の上限（堂々巡り防止）
_GO = {"go", "ok", "okです", "おk", "おけ", "ｏｋ", "了解", "了解です", "りょうかい", "承認", "承知",
       "承知しました", "いいね", "いいよ", "はい", "おねがいします", "お願いします", "それで",
       "それでお願いします", "登録して", "ok!"}
# トークン一致では拾えない承認の言い回し（部分一致・小文字化して判定・長い順＝除去の食べ残し防止）
_GO_PHRASES = ("issueに追加で", "issueに追加", "イシューに追加で", "イシューに追加",
               "登録しておいて", "起票して", "issueで", "登録で")
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
def _norm_item(c: dict) -> dict:
    if c.get("type") == "issue" and c.get("issue_kind") not in ISSUE_KINDS:
        c["issue_kind"] = "その他"
    if c.get("type") == "rule" and c.get("rule_kind") not in RULE_KINDS:
        c["rule_kind"] = "スタイル"
    try:  # 低確信は unclear に倒す（誤起票より確認）
        if c.get("type") in ("issue", "rule") and float(c.get("確信度", 1)) < 0.45:
            c["type"] = "unclear"
    except Exception:
        pass
    return c


def _classify_intake(text: str, context: str = "") -> list:
    """戸田さんの指摘を分類して list で返す（各 {type,issue_kind,rule_kind,要約,詳細,誤例,正例,確信度}）。
    本当に独立した複数の指摘は要素を分ける（分割起票）。失敗時 []。"""
    try:
        from lib import llm
    except Exception:
        return []
    body = re.sub(rf"{re.escape(MENTION)}|<@U[A-Z0-9]+>", "", text or "").strip()
    prompt = (
        "次は戸田さんが Chiaki AI（社内タスク管理AI）に送った指摘/依頼です。種別を判定:\n"
        "- issue: Chiaki AI の不具合・要望（動作の問題/機能追加/変更/バグ）。issue_kind=バグ|変更|新機能|その他。\n"
        "- rule: 言葉のルールを“今後のルール”として覚えるべき指摘。rule_kind="
        "スタイル(声/トーン/温度・例『もっとラフに』『！多すぎ』)|用語(固有名詞や語の統一)|レギュレーション(表記/約物/語尾)。\n"
        "- retract: Chiaki AI の直前のアクション（修正依頼・リマインド・指摘の投稿）が間違っている・宛先違い・"
        "不要という指摘（『これは僕あてですね』『この依頼おかしい』『それ違うよ』『いらなかったよ』）。\n"
        "- edit: いま出ている“この投稿そのもの”を今すぐ直す依頼（『この一文消して』『今回直して』『ここ柔らかく』）。\n"
        "  ※Chiaki AI の投稿の表記・整形の指摘（改行・空行・記号・スペース・箇条書きの体裁・重複行など）も edit＝まずその投稿を直す"
        "（2026-07-03 戸田「直してと言っているのに Issue 提案になる」への抜本対応。整形はコード側で出口一括適用済みのため、"
        "見えている問題は過去の投稿の直しで解消する）。issue(issue_kind=バグ)にするのはコードの動作そのものの不具合"
        "（起票されない・二重送信・反応しない・文字化けが毎回出る等の再現する症状）だけ。\n"
        "- question: 質問・依頼（『まとめて』『教えて』『何件？』）。\n"
        "  ※『なぜ』『どうして』『何で』等の質問を含むメッセージは、不満や不具合への言及が同居していても"
        " question を第一要素にする＝まず答える（2026-07-03 戸田「なぜってきいたらこういうバグでした、"
        "というのが適切では」。質問に Issue 提案で返すのは会話として不適切）。\n"
        "- unclear: 指摘だが種別が曖昧（『なんか違う』等）。確信が低い時もここ。\n"
        "- none: 指摘・依頼・質問のいずれでもない（雑談・お礼・相づち・FYI・了承だけ 等）。\n"
        "**本当に独立した別々の指摘だけ要素を分ける**（例『スペース』と『全角』は別件＝2要素／"
        "『！多すぎ・もっとラフに』は同じトーンの話なので1要素）。edit/question/unclear/none は必ず1要素。\n"
        + (f"直前のやりとり(古い順):\n{context}\n" if context else "")
        + f"メッセージ: {body}\n"
        "各要素に 要約(title用・一言[:200])・詳細(背景や直し方)・(rule時のみ)誤例/正例・確信度(0〜1) を付ける。\n"
        "依頼が『毎回・毎日・定期的に・自動で・定型化して』など**繰り返し実行する仕組み**を求めるものなら"
        ' "routine": true を付ける（単発の修正依頼は false）。\n'
        'JSON 配列のみ: [{"type":"issue|rule|edit|question|unclear|none","issue_kind":"","rule_kind":"",'
        '"要約":"","詳細":"","誤例":"","正例":"","確信度":0.0,"routine":false}]'
    )
    out = llm.gpt(prompt, max_tokens=700) or ""  # 中枢の振り分け＝GPT(失敗時Haiku自動フォールバック)
    arr = None
    mm = re.search(r"\[.*\]", out, re.S)
    if mm:
        try:
            arr = json.loads(mm.group(0))
        except Exception:
            arr = None
    if arr is None:  # 配列で来なければ単体 {...} を1要素に
        m1 = re.search(r"\{.*\}", out, re.S)
        try:
            arr = [json.loads(m1.group(0))] if m1 else []
        except Exception:
            return []
    if not isinstance(arr, list):
        arr = [arr]
    return [_norm_item(c) for c in arr if isinstance(c, dict) and c.get("type")]


_APPEND_RE = re.compile(r"記載|追記|加えて|付け加え|盛り込|書いとい|書いてお|入れとい|入れてお|も書いて|も入れて|も記")
# 承認に同梱された「訂正・振り分け変更・修正指示」の手掛かり。
# ※単独の種別語(バグ/Rule/スタイル等)は質問文(『なぜこのバグが？』)にも出るので入れない＝
#   再分類は「動詞・言い換え（〜にして/〜じゃなくて/〜の方で/短く 等）」が在るときだけに限定。
_CORRECT_RE = re.compile(r"じゃなくて|じゃなく|ではなく|でなく|の方で|の方が|"
                         r"に直して|に変えて|に変更|にして|変更して|訂正|"
                         r"短く|長く|足して|減らして|消して|追加して|修正して")


def _verdict(text: str) -> str:
    """確認ターンの戸田さん返信 → go / go_plus / reject / reclassify。@メンション除去後に判定。
    go 語＋追記/訂正は go_plus＝**提示済みの案は承認として起票し、残りの依頼は別途処理する**
    （2026-07-03 まで reclassify＝再提示していたが、「OK、あと〜してもらっていい？」
    「文言は修正してください。Issueに追加で。」の承認が消費されず同じ提案を繰り返す
    ナンセンスなループになった＝戸田指摘・Issue起票済み）。"""
    t = re.sub(rf"{re.escape(MENTION)}|<@U[A-Z0-9]+>", "", text or "").strip()
    core = t.strip(" 　。、！!.?？\n\r\t")
    if any(w in t for w in _REJECT):
        return "reject"
    tokens = [x for x in re.split(r"[、。！!\?？\s　]+", core) if x]
    has_go = (core.lower() in _GO or any(tok.lower() in _GO for tok in tokens)
              or any(p in t.lower() for p in _GO_PHRASES))
    if has_go:
        res = t
        for p in _GO_PHRASES:
            res = re.sub(re.escape(p), "", res, flags=re.I)
        res = "".join(tok for tok in re.split(r"[、。！!\?？\s　]+", res) if tok and tok.lower() not in _GO)
        # 承認語を除いて実質的な文が残る＝追記/訂正/別依頼が同梱 → 起票してから残りを処理。
        # （go_plus は起票＋残りの再分類なので広めに倒しても安全。短い残り＝社交辞令は go のまま）
        if "「" in t or _APPEND_RE.search(res) or _CORRECT_RE.search(res) or len(res) >= 6:
            return "go_plus"
        return "go"
    return "reclassify"  # 文面修正・振り分け変更・補足はすべて再分類して再提示


def _strip_go(text: str) -> str:
    """返信から承認語・承認フレーズを除いた「残りの依頼」部分を返す。"""
    t = re.sub(rf"{re.escape(MENTION)}|<@U[A-Z0-9]+>", "", text or "").strip()
    for p in _GO_PHRASES:
        t = re.sub(re.escape(p), "", t, flags=re.I)
    tokens = [x for x in re.split(r"([、。！!\?？\s　]+)", t)]
    return "".join(x for x in tokens if x.strip(" 　。、！!.?？\n\r\t").lower() not in _GO).strip(" 　、。\n")


def _one_line(p: dict) -> str:
    s = (p.get("要約") or "").strip()
    if p.get("type") == "issue":
        return f"Issue「{s}／{p.get('issue_kind') or 'その他'}」"
    return f"{p.get('rule_kind') or 'スタイル'}「{s}」"


def _propose_text(proposals: list) -> str:
    """案提示の文面（柔らかく・詰めない）。複数の issue/rule は番号付きで一括確認。"""
    bills = [p for p in proposals if p.get("type") in ("issue", "rule")]
    if len(bills) >= 2:
        lines = [f"{i}) {_one_line(p)}" for i, p in enumerate(bills, 1)]
        return f"以下の{len(bills)}件を登録してもいいですか？\n" + "\n".join(lines)
    p = bills[0] if bills else proposals[0]
    t, s = p.get("type"), (p.get("要約") or "").strip()
    if t == "issue":
        return f"Issueに「{s}／種別={p.get('issue_kind') or 'その他'}」で登録してもいいですか？"
    if t == "rule":
        return f"{p.get('rule_kind') or 'スタイル'}に「{s}」で登録してもいいですか？"
    return ("これは不具合の話ですか？それとも言葉のルール（トーンや表記）の話ですか？"
            "どう直すのがいいか、もう少し具体的に教えてもらえますか？")


# 本文中のメンションは中和する＝宛先pingはヘッダの <@戸田> だけ（監査確定：_answer がスレッドから
# 実IDをコピーして第三者へ意図しないping／Haikuが架空の@名前を作る）。既知IDは呼び名に置換。
_KNOWN_NAMES = {runtime.TODA: "戸田さん", "U09T44VEZM1": "松永さん", runtime.CHIAKI_SELF: "Chiaki AI"}


def _neutralize_mentions(s: str) -> str:
    s = re.sub(r"<@([A-Z0-9]+)(?:\|[^>]*)?>", lambda mm: _KNOWN_NAMES.get(mm.group(1), ""), s)
    s = re.sub(r"<!(?:channel|here|everyone)>", "", s)  # broadcast の巻き添えpingも防ぐ
    s = re.sub(r"(?<!\S)@(?=[^\s<>])", "", s)  # 裸の@名前は@だけ落とす（本文は残す）
    return s


def _reply(ch: str, root: str, body: str, url: str = "") -> None:
    b = runtime.ensure_punct(observe.enforce_regulations(_neutralize_mentions(body)))
    if url:
        b += f"\n{url}"
    # この処理で LLM が文面/判断を作った場合はモデル名を末尾に表記（戸田要望 2026-07-02）。
    # タグ無し＝固定文/決定論。main が各メッセージ処理前に reset_used() する。
    try:
        from lib import llm
        tag = llm.last_used()
        if tag:
            b += f"\n（{tag}）"
    except Exception:
        pass
    source.post_thread_reply(ch, root, f"<@{runtime.TODA}>\n{b}")


def _file_issue(p: dict, permalink: str, ch: str):
    summary = p.get("要約", "")
    # 繰り返しの仕組み化を求める依頼＝定型業務化の候補としてマーク（Claude Code が Issue_DB の
    # このプレフィクスと intake_log.jsonl を読み、定型業務に昇格させるか判定して実装する運用）。
    if p.get("routine") and not summary.startswith("定型業務化"):
        summary = f"定型業務化: {summary}"
    return notion.create_request(summary, p.get("詳細", ""), slack_url=permalink,
                                 channel_label=_ch_label(ch), kind=p.get("issue_kind") or "その他")


def _file_rule(p: dict, permalink: str):
    # 機械ルール（用語/レギュレーション＝決定論で直せる）は即「承認」、スタイル（判断）は「未承認」
    status = "承認" if p.get("rule_kind") in ("用語", "レギュレーション") else "未承認"
    return notion.create_rule_registry(p.get("要約", ""), p.get("詳細", ""),
                                       p.get("rule_kind") or "スタイル", slack_url=permalink,
                                       wrong=p.get("誤例", ""), right=p.get("正例", ""), status=status)


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


def _fix_reports(n: int = 6) -> str:
    """#8902 の直近の修正報告（Chiaki AI 自身の不具合と直した内容の記録）を知識として返す。
    「なぜこうなる？」に「こういうバグでした」と答えるための情報源（2026-07-03 戸田指摘）。"""
    try:
        out = []
        for m in source.read_recent(runtime.CH_CHIAKI_MGMT, limit=40):
            t = m.get("text") or ""
            if m.get("user_id") == runtime.CHIAKI_SELF and "報告：" in t[:40]:
                out.append(f"[{(m.get('datetime') or '')[:16]}] {t[:400]}")
            if len(out) >= n:
                break
        return "\n---\n".join(out) or "（まだ無し）"
    except Exception:
        return "（取得失敗）"


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
              "分からないことは推測せず正直に言う。"
              "自分の不具合の「なぜ」を聞かれたら、下の「最近の修正報告」に該当する説明があれば"
              "それに基づいて「こういうバグでした・こう直しました」と答える。該当が無ければ推測せず"
              "『記録に見当たらないので、原因調査はClaude Codeに依頼してください』と正直に言う。"
              "太字や * による強調は使わない。\n"
              f"依頼: {question}\nこのスレッドのやりとり:\n{convo}\n"
              f"\n最近の修正報告（Chiaki AI自身の不具合と直した内容の記録・新しい順）:\n{_fix_reports()}\n")
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
    return (llm.gpt(prompt, max_tokens=450) or "").strip()  # 会話＝GPT(失敗時Haiku)


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
    # 数字の捏造・改変ガード：原文にも指示にも無い数字が出力に現れたら不採用（監査確定：
    # 行数/長さのガードだけでは件数・時刻の書き換えが chat.update で既存投稿に焼き込まれる）。
    if set(re.findall(r"\d+", out)) - set(re.findall(r"\d+", text + instruction)):
        return ""
    if reflow:  # 行数は変わってよいが、本文（空白以外）の欠落・水増し（前置き混入）がないこと
        a, b = re.sub(r"\s", "", out), re.sub(r"\s", "", text)
        return out if (a and len(b) * 0.8 <= len(a) <= len(b) * 1.15) else ""
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
    if not msg:
        return "notfound"
    if msg.get("user_id") != runtime.CHIAKI_SELF:
        return "notself"  # 投稿は在るが Chiaki AI のものでない（戸田自身の投稿リンク等）＝編集対象外
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
    "notself": "そのリンクは Chiaki AI の投稿ではないようです。直すのは Chiaki AI のどの投稿でしょうか。",
}


# ── 候補収集（propose/confirm 両ターン） ───────────────────
def _candidates(cur: dict, items: dict):
    """戸田さんの新規メッセージ候補 [(msg, thread_root, channel, hint)]。"""
    mgmt, pdca = runtime.CH_CHIAKI_MGMT, runtime.CH_CHIAKI_PDCA
    pend = runtime.load_json("pending_approvals.json", {"items": {}}).get("items", {})
    # 裁定（apply-ruling）スレッドは**完了後も**通常の拾いから除外＝明示的な @メンションだけ受ける。
    # 進行中だけ除外すると、完了した瞬間にスレッド内の過去の「OK」（裁定として処理済み）を
    # intake が新規発話として拾い直し、後追いの雑談返信を投げる（2026-07-03 実バグ＝二重処理）。
    ruling_threads = set(pend)
    # Codex 報告スレッドの返信は codex-runner の対話（継続実装/質問/反映依頼）が引き受ける＝intake は触らない
    codex_threads = set(runtime.load_json("codex_threads.json", {"items": {}}).get("items", {}))
    awaiting = {(it.get("channel"), it.get("thread_root")) for it in items.values()
                if it.get("status") == "awaiting_confirm"}
    # 戸田さん以外からの @Chiaki AI ＝エスカレーション対象（権限は戸田さんのみ・2026-07-02）。
    # ①トップレベルのみ＝スレッド内の返信（chiaki のリマインドへの「OK」等）は依頼でなく応答なので対象外
    # ②直近1時間の新規のみ＝カーソル未設定/リセット時に過去分へ一括送信しない
    # （どちらも 2026-07-02 の誤爆5件＝過去の「OK」への後追いエスカレーションの再発防止）。
    _now = runtime.now_ts()

    def _other(uid):
        return uid and uid not in (runtime.TODA, runtime.CHIAKI_SELF, runtime.GCP_TASK_BOT)

    def _esc_ok(m):
        return (_other(m.get("user_id")) and MENTION in (m.get("text") or "")
                and m["ts_float"] > _now - 3600)
    cand = []
    # #8902/#5902：戸田さんの全投稿（top-level＋提案以外スレッド返信）
    since_m = float(cur.get(mgmt, 0.0))
    for m in source.read_recent(mgmt, limit=50):
        if m["user_id"] == runtime.TODA and m["ts_float"] > since_m:
            cand.append((m, m["ts"], mgmt, ""))
        elif m["ts_float"] > since_m and _esc_ok(m):
            cand.append((m, m["ts"], mgmt, "escalate"))
        if m.get("thread_replies") and m["ts"] not in codex_threads:
            need_mention = m["ts"] in ruling_threads  # 裁定スレッド内は @メンション明示のみ
            for r in source.read_thread(mgmt, m["ts"]):
                if (r["ts"] != m["ts"] and r["user_id"] == runtime.TODA and r["ts_float"] > since_m
                        and (not need_mention or MENTION in (r.get("text") or ""))):
                    # 裁定スレッドでメンション付きでも、裸の裁定語（GO/OK/却下等）は apply-ruling の領分
                    # ＝intake が「了解です！動きます」等を重ねない（2026-07-10 実バグ: GO の二重応答）
                    if need_mention and _is_bare_ruling(r.get("text") or ""):
                        continue
                    cand.append((r, m["ts"], mgmt, ""))
    since_p = float(cur.get(pdca, 0.0))
    for m in source.read_recent(pdca, limit=50):
        # #5902 の戸田さん top-level も窓口（監査確定：スレッド返信しか見ておらず@メンションが無視されていた）
        if m["user_id"] == runtime.TODA and m["ts_float"] > since_p:
            cand.append((m, m["ts"], pdca, ""))
        elif m["ts_float"] > since_p and _esc_ok(m):
            cand.append((m, m["ts"], pdca, "escalate"))
        if m.get("thread_replies"):
            for r in source.read_thread(pdca, m["ts"]):
                if r["ts"] != m["ts"] and r["user_id"] == runtime.TODA and r["ts_float"] > since_p:
                    cand.append((r, m["ts"], pdca, ""))
    # #5035/#a027：@メンション（どこでも）＋ 確認待ちスレッドの戸田返信（負荷を抑えて拾う）。
    # 新着返信のあるスレッド(thread_latest>since)も走査＝根が松永さん/botのスレッド内@メンションを
    # 黙殺しない（監査確定：「どこでも1窓口」の破れ）。拾う返信は従来どおり戸田さん＋MENTION限定。
    for ch in WATCH_EXTRA:
        since = float(cur.get(ch, 0.0))
        for m in source.read_recent(ch, limit=50):
            if (m["user_id"] == runtime.TODA and m["ts_float"] > since and MENTION in (m.get("text") or "")):
                cand.append((m, m["ts"], ch, ""))
            elif m["ts_float"] > since and _esc_ok(m):
                # トップレベルの新規メンションのみ。スレッド返信（リマインドへの「OK」等）は対象外。
                cand.append((m, m["ts"], ch, "escalate"))
            scan = (m.get("thread_replies") and
                    (m.get("user_id") == runtime.CHIAKI_SELF or (ch, m["ts"]) in awaiting
                     or float(m.get("thread_latest") or 0) > since))
            if scan:
                for r in source.read_thread(ch, m["ts"]):
                    if (r["ts"] != m["ts"] and r["user_id"] == runtime.TODA and r["ts_float"] > since
                            and (MENTION in (r.get("text") or "") or (ch, m["ts"]) in awaiting)):
                        cand.append((r, m["ts"], ch, ""))
    return cand


_FILED_FOLLOWUP_SEC = 24 * 3600  # 起票完了後もこの間はスレッドの続きを会話エージェントで受ける


_RULING_WORDS = _GO | {"go!", "却下", "やめ", "なしで", "見送り", "ボツ", "流して"}


def _is_bare_ruling(text: str) -> bool:
    """メンションを除いた本文が裁定語（GO/OK/却下…）だけか＝apply-ruling が処理する発話。"""
    t = re.sub(r"<@U[A-Z0-9]+>", "", text or "").strip(" 　。、！!.?？\n\r\t")
    if not t:
        return False
    tokens = [x for x in re.split(r"[、。！!\?？\s　]+", t) if x]
    return bool(tokens) and all(tok.lower() in _RULING_WORDS for tok in tokens)


def _find_awaiting(items: dict, ch: str, root: str, msg_ts: str):
    """確認待ち（awaiting_confirm）に加え、起票直後（filed・24h以内）のスレッド返信も返す＝
    「登録しました！」の後の『そのまま社内のレギュレーションも調整したい』のような続きの依頼を、
    初回分類に落とさず会話エージェント（company_rule 等のアクション持ち）で受ける（2026-07-08 戸田）。"""
    for it in items.values():
        if (it.get("status") == "awaiting_confirm" and it.get("channel") == ch
                and it.get("thread_root") == root
                and float(msg_ts) > float(it.get("last_seen_ts", 0))
                and msg_ts != it.get("mention_ts")):
            return it
    for it in items.values():  # filed の続き（awaiting が無い時だけ）
        if (it.get("status") == "filed" and it.get("channel") == ch
                and it.get("thread_root") == root
                and float(msg_ts) > float(it.get("last_seen_ts", 0))
                and msg_ts != it.get("mention_ts")
                and runtime.now_ts() - float(it.get("proposed_at", 0)) < _FILED_FOLLOWUP_SEC):
            return it
    return None


def _expire_stale(items: dict) -> None:
    now = runtime.now_ts()
    for it in items.values():
        if it.get("status") == "awaiting_confirm" and now - float(it.get("proposed_at", now)) > INTAKE_TIMEOUT_SEC:
            it["status"] = "expired"
            _log("expired", it.get("channel", ""), it.get("thread_root", ""),
                 依頼元=(it.get("mention_text") or "")[:200])
            try:  # 無音で消さない＝1回だけ知らせる（失効は片道なので二重通知しない）
                _reply(it.get("channel"), it.get("thread_root"),
                       "時間が空いたので、この確認はいったん閉じますね。必要でしたら、もう一度メンションしてください。")
            except Exception as e:
                print(f"[intake] expire notice failed: {e}")


def _log(event: str, ch: str = "", root: str = "", m: dict = None, **extra) -> None:
    """業務化トリアージログ＝「しゃべりかけ→対応」の全記録（intake_log.jsonl）。
    Claude Code が後で読み、繰り返される依頼を定型業務へ昇格するか判定する材料（戸田設計 2026-07-02）。"""
    try:
        rec = {"ts": runtime.now_ts(), "event": event, "channel": ch, "thread_root": root}
        if m is not None:
            rec["msg_ts"] = m.get("ts")
            rec["依頼"] = (m.get("text") or "")[:300]
        rec.update(extra)
        runtime.append_jsonl("intake_log.jsonl", rec)
    except Exception as e:
        print(f"[intake] log failed: {e}")


def _await(items: dict, m: dict, ch: str, root: str, proposals: list) -> int:
    items[m["ts"]] = {"status": "awaiting_confirm", "channel": ch, "thread_root": root,
                      "mention_ts": m["ts"], "mention_text": m["text"],
                      "permalink": _permalink(ch, m["ts"], root), "proposals": proposals,
                      "proposed_at": runtime.now_ts(), "last_seen_ts": m["ts"], "propose_count": 1}
    _reply(ch, root, _propose_text(proposals))
    _log("propose", ch, root, m,
         分類=[{"type": p.get("type"), "kind": p.get("issue_kind") or p.get("rule_kind") or "",
               "要約": (p.get("要約") or "")[:100], "routine": bool(p.get("routine"))} for p in proposals])
    return 1


_SYMPTOM_RE = re.compile(r"[|｜]{2,}|文字化け|区切り(?:記号|文字)|崩れて|残骸|重複して|行になっちゃ|改行され(?:て)?(?:い|な)|行が増え")


def _bug_symptom(instruction: str, ch: str, root: str, raw: str):
    """Chiaki AI 出力の“症状”指摘（|||混入・文字化け等）か。該当なら issue(バグ) proposal を返す
    ＝編集で証拠を消す前に、現状テキストを誤例として証拠付き起票へ回す（戸田: 直す前に記録）。"""
    target = ""
    try:
        resolved = _resolve_link(raw)
        if resolved:
            tch, tts, parent = resolved
            msg = next((x for x in source.read_thread(tch, parent) if x.get("ts") == tts), None)
        else:
            msg = next((x for x in source.read_thread(ch, root) if x.get("ts") == root), None)
        if msg and msg.get("user_id") == runtime.CHIAKI_SELF:
            target = msg.get("text", "") or ""
    except Exception:
        target = ""
    residue = bool(re.search(r"[|｜]{2,}", target))  # 投稿に区切り残骸が現存
    if not (residue or _SYMPTOM_RE.search(instruction or "")):
        return None
    instr = re.sub(rf"{re.escape(MENTION)}|<@U[A-Z0-9]+>", "", instruction or "").strip()
    excerpt = (target or instr).strip()[:300]
    return {"type": "issue", "issue_kind": "バグ", "rule_kind": "",
            "要約": (instr[:60] or "Chiaki AI 出力の不具合"),
            "詳細": f"Chiaki AI の出力に不具合（戸田指摘: {instr[:120]}）。該当投稿の現状（証拠）:\n{excerpt}",
            "誤例": "", "正例": "", "確信度": 0.85}


def _mark_done(items: dict, m: dict, ch: str, root: str) -> int:
    """item を作らない経路(edit/question)でも『処理済み』印を残す＝再処理時に二重投稿しない。"""
    items[m["ts"]] = {"status": "handled", "channel": ch, "thread_root": root, "mention_ts": m["ts"]}
    return 1


def _escalate(items: dict, m: dict, ch: str, root: str) -> int:
    """戸田さん以外からの @Chiaki AI への依頼＝分類・起票・実行はせず、依頼者に受領を返して
    戸田さんへメンションで引き継ぐ（権限は戸田さんのみ・2026-07-02 戸田指示）。固定文＝決定論。"""
    sender = m.get("user_id", "")
    body = (f"<@{sender}>\n"
            "ご連絡ありがとうございます！Chiaki AIへの依頼や変更は、戸田さんに確認をとる決まりになっています。\n"
            f"<@{runtime.TODA}> 上記の依頼の確認をお願いします！")
    source.post_thread_reply(ch, root, body)
    _log("escalate", ch, root, m, 依頼者=sender)
    items[m["ts"]] = {"status": "escalated", "channel": ch, "thread_root": root, "mention_ts": m["ts"]}
    return 1


def _smalltalk(text: str) -> str:
    """指摘でも依頼でもない呼びかけ（テスト・あいさつ・お礼など）への短い自然な返事。失敗時は固定文。"""
    fb = "はい、Chiaki AIです。ちゃんと届いています！指摘・依頼・質問があればどうぞ。"
    try:
        from lib import llm
        body = re.sub(rf"{re.escape(MENTION)}|<@U[A-Z0-9]+>", "", text or "").strip()
        out = (llm.gpt(f"戸田さんからの軽い呼びかけ「{body[:120]}」に1〜2文で自然に応じてください。"
                       "指摘・依頼・質問があれば受け付ける旨をさりげなく添える。宛名(@)・絵文字・引用符は付けない。",
                       max_tokens=120) or "").strip()
        return out or fb
    except Exception:
        return fb


def _handle_propose(m: dict, ch: str, root: str, items: dict) -> int:
    if m["ts"] in items:  # 既に提案/起票/処理済みの同一メンション＝再処理時に二重投稿しない（冪等）
        return 0
    cs = _classify_intake(m["text"], _thread_context(ch, root, m["ts"]))
    if not cs:
        return 0
    bills = [c for c in cs if c.get("type") in ("issue", "rule")]
    if bills:  # 1件でも複数でも awaiting（複数は分割起票）
        return _await(items, m, ch, root, bills)
    typ = cs[0].get("type")  # bills が無い＝単発の retract/edit/question/unclear/none
    if typ == "retract":
        acted = _handle_retract(m, ch, root)
        _log("retract", ch, root, m)
        return _mark_done(items, m, ch, root) if acted else 0
    if typ == "edit":
        sym = _bug_symptom(m["text"], ch, root, m["text"])  # バグ症状なら証拠付き issue 化（編集で消さない）
        if sym:
            return _await(items, m, ch, root, [sym])
        st = _maybe_edit_root(ch, root, m["text"], m["text"])  # 生指示＝改行/空白の語を保つ
        _reply(ch, root, _EDIT_MSG[st])
        _log("edit", ch, root, m, 結果=st)
        return _mark_done(items, m, ch, root)
    if typ == "question":
        ans = _answer(m["text"], ch, root)
        if ans:
            _reply(ch, root, ans)
            _log("answer", ch, root, m, 回答=(ans or "")[:200])
            return _mark_done(items, m, ch, root)
        return 0
    if typ == "unclear":
        return _await(items, m, ch, root, [cs[0]])
    # none でも明示的な @メンション付きの呼びかけには短く自然に応じる＝無視に見せない
    # （2026-07-02 戸田「テスト」への無言が故障に見えた）。メンション無しの none（お礼・FYI・相づち）は従来どおり静観。
    if MENTION in (m.get("text") or ""):
        _reply(ch, root, _smalltalk(m["text"]))
        _log("smalltalk", ch, root, m)
        return _mark_done(items, m, ch, root)
    return 0  # none


def _handle_confirm(it: dict, m: dict, ch: str, root: str):
    """確認ターン。last_seen_ts / proposed_at は正常終了時にだけ前進＝途中例外（LLM残高切れ等）で
    戸田さんの返信が恒久に握りつぶされない（監査確定バグ）。対話が続く限りタイムアウトもリセット。
    返り値 None＝filed の続きをエージェントで受けられなかった＝呼び側が初回分類（propose）へ回す。"""
    acted = _confirm_inner(it, m, ch, root)
    if acted is None:
        return None
    it["last_seen_ts"] = m["ts"]
    it["proposed_at"] = runtime.now_ts()
    return acted


_CODEX_RE = re.compile(r"codex", re.I)


def _maybe_enqueue_codex(it: dict, m: dict, ch: str, root: str, ok: list, force=None) -> str:
    """起票済み issue を codex-runner のキューへ積む。force=True は無条件（会話エージェントが
    「コード変更の依頼」と判断＝2026-07-08 戸田「その場でコードを編集していけないの？」以降の既定）、
    force=False は積まない（「起票だけ」）、None は従来の「Codex」言及トリガ（legacy経路用）。
    権限＝確認ターンに到達できるのは戸田さんのみ＋runner 側でも requested_by を再検証（二重ゲート）。
    返り値は返信に足す一言（積まなければ空文字）。"""
    if force is False:
        return ""
    if force is not True and not _CODEX_RE.search(
            (it.get("mention_text") or "") + " " + (m.get("text") or "")):
        return ""
    issues = [(p, u) for p, u in ok if p.get("type") == "issue"]
    if not issues:
        return ""
    for p, u in issues:
        runtime.append_jsonl("codex_queue.jsonl", {
            "ts": runtime.now_ts(), "requested_by": m.get("user_id"),
            "summary": p.get("要約") or "", "detail": p.get("詳細") or "",
            "issue_url": u or "", "channel": ch, "thread": root})
    try:  # ランナーを即起動（cron待ちで最大10分黙らせない）。多重起動は runner 内の flock が防ぐ
        import subprocess
        script = os.path.join(os.environ.get("HERMES_PROFILE_DIR", ""), "scripts/codex_runner.py")
        if os.path.isfile(script):
            subprocess.Popen([sys.executable, script], stdout=subprocess.DEVNULL,
                             stderr=subprocess.DEVNULL, start_new_session=True)
    except Exception:
        pass
    return "\nCodexに実装させます！進捗はこのスレッドに報告します。"


def _handle_go_extra(it: dict, m: dict, ch: str, root: str, filed_bills: list) -> None:
    """go_plus の残り（承認語を除いた追加依頼）を処理。起票直後に呼ぶ。
    起票済みと同じ問題の「修正実行して」だけなら黙って完結（重複提案ループを作らない）。
    新しい指摘なら1回だけ提案し直し、edit なら直し、質問なら答える。"""
    rest = _strip_go(m["text"])
    if len(rest) < 4:
        return
    filed = "・".join((p.get("要約") or "") for p in filed_bills)
    cs = _classify_intake(
        f"{rest}\n※注意: 次の内容はたった今起票済み:「{filed}」。"
        "残りの依頼がこの起票済みの問題の修正実行を求めているだけなら type=none にする。",
        _thread_context(ch, root, m["ts"]))
    if not cs:
        return
    bills = [c for c in cs if c.get("type") in ("issue", "rule")]
    typ = cs[0].get("type")
    if bills:
        it["proposals"], it["status"] = bills, "awaiting_confirm"
        it["propose_count"] = 1
        _reply(ch, root, "あわせて、" + _propose_text(bills))
    elif typ == "edit":
        st = _maybe_edit_root(ch, root, rest, rest)
        _reply(ch, root, _EDIT_MSG[st])
    elif typ == "question":
        ans = _answer(m["text"], ch, root)
        if ans:
            _reply(ch, root, ans)
    # none / unclear → 起票報告のみで完結


def _confirm_agent(it: dict, m: dict, ch: str, root: str):
    """確認ターンの会話エージェント（2026-07-03 戸田「会話の主導権をGPTに渡す」）。
    GPT 5.5 にスレッド履歴・提案中の内容・修正報告の知識・アクション一覧を渡し、
    返事とアクションを一括で決めさせる＝口はGPT。実行は決定論ゲートのまま＝手は決定論
    （起票は保存済み/検証済みの提案のみ・投稿編集は _maybe_edit_root・Codex起動は
    _maybe_enqueue_codex・権限/冪等性は従来どおり）。出力が壊れていたら None＝従来ロジックへ。"""
    try:
        from lib import llm
    except Exception:
        return None
    proposals = it.get("proposals") or ([it["proposal"]] if it.get("proposal") else [])
    bills = [p for p in proposals if p.get("type") in ("issue", "rule")]
    filed = it.get("status") == "filed"
    thread = source.read_thread(ch, root)
    convo = "\n".join(f"- {(x.get('user_name') or x.get('user_id') or '?')}: {(x.get('text') or '')[:250]}"
                      for x in thread[-15:])
    count = it.get("propose_count", 1)
    plist = json.dumps([{k: p.get(k) for k in ("type", "issue_kind", "rule_kind", "要約", "詳細", "routine")}
                        for p in bills], ensure_ascii=False) if bills else "（まだ案が定まっていない）"
    state = "登録済み（このスレッドの続きの会話。file は使わない＝二重登録になる）" if filed else "確認待ち"
    prompt = (
        "あなたは Chiaki AI（Lipple の業務観測AI）。#8902 で戸田さんと会話しながら、指摘の起票を進めています。\n"
        f"案の状態: {state}\n"
        f"起票案: {plist}\n"
        f"再提示回数: {count}回目（4回を超えたら再提案せず、登録か見送りの判断を仰ぐ）\n"
        f"このスレッドのやりとり:\n{convo}\n"
        f"\n最近の修正報告（Chiaki AI 自身の不具合と直した内容の記録・新しい順）:\n{_fix_reports()}\n"
        f"\n戸田さんの新しい返信: {m.get('text') or ''}\n\n"
        "あなたが書き込めるNotion: ①Chiaki AIのRule Registry（自分の言葉のルール） ②Issue_DB（不具合バックログ） "
        "③社内レギュレーション_DB（コンテンツマーケの正本）。それ以外のDB・ページへの登録を頼まれたら、"
        "権限（共有）が無いことを正直に伝え、NotionでHermes Agentに共有してもらえれば対応できると案内する。\n"
        "返事（reply）と、取るアクション（action）を決めて JSON のみで返す:\n"
        '{"action": "file|revise|cancel|edit_post|answer_only|company_rule", "reply": "", "proposals": [], '
        '"instruction": "", "company": {"rule": "", "content": "", "category": "", "wrong": "", "right": ""}}\n'
        "- file: 提示中の案の登録を承認した（OK/はい/登録して/Issueに追加で 等。追加の依頼や雑談が同居していても"
        "承認が含まれていれば file）。reply には登録した旨＋同居していた話への応答（登録URLはシステムが後ろに付ける）。"
        'issueの場合は "codex": true/false も返す＝コードの修正・変更・機能追加なら true（そのままCodexが実装まで進める・既定）、'
        "戸田さんが起票だけを求めた場合やコード外の作業（Notionの手作業・運用の相談等）は false。"
        "返信に『新しく起票すべき別の指摘』が含まれる場合だけ proposals に次の案を入れる"
        '（各: {"type":"issue|rule","issue_kind":"バグ|変更|新機能|その他","rule_kind":"用語|レギュレーション|スタイル",'
        '"要約":"","詳細":"","routine":false}）。\n'
        "- revise: 案の内容・振り分けの変更指示（『それRuleね』『要約はこうして』）。proposals に修正版の全件を入れ、"
        "reply で新しい案の中身を具体的に示して確認を求める。\n"
        "- cancel: 却下・見送り（『やめて』『いらない』）。\n"
        "- edit_post: 特定の投稿そのものを直す依頼。instruction に直し方を具体的に。\n"
        "- answer_only: 質問・雑談・情報共有＝起票の判断はまだ。reply で普通に答える/応じる（『なぜ』への質問は"
        "上の修正報告の記録に基づいて『こういうバグでした・こう直しました』と答える。記録に無ければ正直に分からないと言う）。\n"
        "- company_rule: このルールを社内レギュレーション（正本）にも登録してほしいという依頼"
        "（『社内のレギュレーションも調整したい』『正本にも追加して』・レギュレーション_DBのURL付き等）。"
        "company に登録内容を入れる: rule=ルール名（一言）・content=ルールの説明・"
        "category=用字・表記|数字・英字|記号・約物|文末・語尾|表現・NG|体裁・構成 から選ぶ・wrong=誤例・right=正例。"
        "reply には登録した旨（URLはシステムが付ける）。\n"
        "reply の規約: です・ます調、感嘆符は全角！、太字や*は使わない、@メンションは書かない、絵文字なし、1〜5文で簡潔に。"
        "「この提案は開いたままなので〜」のような案内の定型文は書かない。同じ文面を繰り返さない。"
    )
    llm.reset_used()
    out = llm.gpt(prompt, max_tokens=800) or ""
    mm = re.search(r"\{.*\}", out, re.S)
    if not mm:
        return None
    try:
        d = json.loads(mm.group(0))
    except Exception:
        return None
    action = d.get("action")
    reply = (d.get("reply") or "").strip()
    if action not in ("file", "revise", "cancel", "edit_post", "answer_only", "company_rule") or not reply:
        return None

    if action == "company_rule":
        # 社内レギュレーション_DB（正本）への登録（2026-07-08 戸田「社内のレギュレーションも調整したい」。
        # 従来はアクションが無く、自分のRule Registryへ「社内にも反映する」というルールを登録し直す空回りをしていた）
        c = d.get("company") or {}
        url = notion.create_company_regulation(
            rule=c.get("rule") or "", content=c.get("content") or "",
            category=c.get("category") or "", wrong=c.get("wrong") or "", right=c.get("right") or "",
            basis=f"戸田さん指示（Slack・{dt.datetime.now(JST).strftime('%Y-%m-%d')}）")
        if url:
            _reply(ch, root, reply, url)
            _log("company_rule", ch, root, m, url=url)
        else:
            _reply(ch, root, "社内レギュレーション_DBへの登録に失敗しました。"
                             "NotionでHermes Agentへの共有・権限を確認してもらえますか？")
        return 1

    if action == "cancel":
        it["status"] = "cancelled"
        _reply(ch, root, reply)
        _log("cancelled", ch, root, m, 依頼元=(it.get("mention_text") or "")[:200])
        return 1

    if action == "answer_only":
        _reply(ch, root, reply)  # awaiting は維持＝引き続き承認/修正/却下を受け付ける
        _log("answer", ch, root, m, 回答=reply[:200])
        return 1

    if action == "edit_post":
        st = _maybe_edit_root(ch, root, d.get("instruction") or m["text"], m["text"])
        _reply(ch, root, reply if st == "edited" else _EDIT_MSG[st])
        _log("edit", ch, root, m, 結果=st)
        return 1

    def _valid_bills(raw) -> list:
        outp = []
        for c in raw or []:
            if isinstance(c, dict) and c.get("type") in ("issue", "rule") and (c.get("要約") or "").strip():
                outp.append(_norm_item({**c, "確信度": c.get("確信度", 0.9)}))
        return [c for c in outp if c.get("type") in ("issue", "rule")]

    if action == "revise":
        newb = _valid_bills(d.get("proposals"))
        if not newb or count >= _PROPOSE_CAP + 2:  # 案が壊れている/回りすぎ → 従来ロジックの安全弁へ
            return None
        it["proposals"] = newb
        it["propose_count"] = count + 1
        it["status"] = "awaiting_confirm"  # filed の続きから新しい案を出す場合も確認待ちに戻す
        _reply(ch, root, reply)
        return 1

    # action == "file"
    if filed:
        _reply(ch, root, reply)  # 登録済みの再承認＝二重登録しない（返事だけ）
        return 1
    if not bills:
        return None  # まだ案が無い状態の承認＝従来ロジック（再分類）へ
    results = [(p, _file_issue(p, it["permalink"], ch) if p["type"] == "issue"
                else _file_rule(p, it["permalink"])) for p in bills]
    ok = [(p, u) for p, u in results if u]
    ng = [p for p, u in results if not u]
    urls = [u for _, u in ok]
    extra = ""
    if urls and any(p["type"] == "rule" for p, _ in ok) and not it.get("root_edited"):
        if _maybe_edit_root(ch, root, "", it.get("mention_text", "")) == "edited":
            extra, it["root_edited"] = "\n指摘のあった投稿も直しました。", True
    if urls and not ng:  # 全件成功
        it["status"], it["page_urls"] = "filed", urls
        # issue のコード変更は既定でそのまま Codex 実装へ（エージェントの codex 判断・未指定は issue なら true）
        force = d.get("codex")
        if force is None:
            force = any(p.get("type") == "issue" for p, _ in ok) or None
        codex_note = _maybe_enqueue_codex(it, m, ch, root, ok, force=force)
        _reply(ch, root, reply + extra + codex_note + "\n" + "\n".join(urls))
        _log("filed", ch, root, m, urls=urls,
             routine=any(bool(p.get("routine")) for p, _ in ok),
             依頼元=(it.get("mention_text") or "")[:200])
        follow = _valid_bills(d.get("proposals"))
        if follow:  # 承認に同梱された新しい指摘＝次のラウンドへ（取りこぼさない）
            it["proposals"], it["status"] = follow, "awaiting_confirm"
            it["propose_count"] = 1
    elif urls and ng:  # 部分失敗＝失敗分だけ残し再試行可能に
        it["proposals"], it["status"] = ng, "awaiting_confirm"
        it["page_urls"], it["proposed_at"] = urls, runtime.now_ts()
        _reply(ch, root, f"{len(bills)}件中{len(urls)}件 登録しました！" + extra + "\n" + "\n".join(urls)
               + f"\n残り{len(ng)}件は起票に失敗しました。DBの共有を確認のうえ、もう一度「はい」で再試行します。")
    elif not notion._token():
        _reply(ch, root, reply + "（ローカル確認のため実際の保存はしていません）")
    else:
        _reply(ch, root, "起票に失敗しました。DBの共有を確認してもらえますか？")
    return 1


def _confirm_inner(it: dict, m: dict, ch: str, root: str):
    """確認ターン。主経路＝会話エージェント（GPT 5.5）。出力不正・LLM不通時は従来の
    決定論ロジック（_confirm_legacy）へフォールバック＝無反応にはならない。
    filed（起票済み）の続き会話でエージェント不成立の場合は None＝呼び側が初回分類へ回す
    （legacy の go は二重起票になるため通さない）。"""
    r = _confirm_agent(it, m, ch, root)
    if r is not None:
        return r
    if it.get("status") == "filed":
        print("[intake] filed follow-up: agent failed -> propose path")
        return None
    print("[intake] confirm agent fallback -> legacy")
    return _confirm_legacy(it, m, ch, root)


def _confirm_legacy(it: dict, m: dict, ch: str, root: str) -> int:
    v = _verdict(m["text"])
    proposals = it.get("proposals") or ([it["proposal"]] if it.get("proposal") else [])
    bills = [p for p in proposals if p.get("type") in ("issue", "rule")]
    if v == "reject":
        it["status"] = "cancelled"
        _reply(ch, root, "わかりました、今回は見送りますね。")
        _log("cancelled", ch, root, m, 依頼元=(it.get("mention_text") or "")[:200])
        return 1
    # unclear（bills 無し）はまだ候補が定まっていない → go でも再分類
    if v in ("go", "go_plus") and bills:
        results = [(p, _file_issue(p, it["permalink"], ch) if p["type"] == "issue"
                    else _file_rule(p, it["permalink"])) for p in bills]
        ok = [(p, u) for p, u in results if u]
        ng = [p for p, u in results if not u]
        urls = [u for _, u in ok]
        # rule があれば指摘元の投稿も決定論で1回だけ直す（再試行で二重編集しない）
        extra = ""
        if urls and any(p["type"] == "rule" for p, _ in ok) and not it.get("root_edited"):
            if _maybe_edit_root(ch, root, "", it.get("mention_text", "")) == "edited":
                extra, it["root_edited"] = "\n指摘のあった投稿も直しました。", True
        if urls and not ng:  # 全件成功
            it["status"], it["page_urls"] = "filed", urls
            head = "登録しました！" if len(urls) == 1 else f"{len(urls)}件 登録しました！"
            codex_note = _maybe_enqueue_codex(it, m, ch, root, ok)
            _reply(ch, root, head + extra + codex_note + "\n" + "\n".join(urls))
            _log("filed", ch, root, m, urls=urls,
                 routine=any(bool(p.get("routine")) for p, _ in ok),
                 依頼元=(it.get("mention_text") or "")[:200])
            if v == "go_plus":  # 承認に同梱された残りの依頼を取りこぼさない
                _handle_go_extra(it, m, ch, root, bills)
        elif urls and ng:  # 部分失敗＝失敗分だけ残し再試行可能に（成功分は除く＝重複起票しない）
            it["proposals"], it["status"] = ng, "awaiting_confirm"
            it["page_urls"], it["proposed_at"] = urls, runtime.now_ts()
            _reply(ch, root, f"{len(bills)}件中{len(urls)}件 登録しました！" + extra + "\n" + "\n".join(urls)
                   + f"\n残り{len(ng)}件は起票に失敗しました。DBの共有を確認のうえ、もう一度「はい」で再試行します。")
        elif not notion._token():
            _reply(ch, root, "登録しました！（ローカル確認のため実際の保存はしていません）")
            if v == "go_plus":
                _handle_go_extra(it, m, ch, root, bills)
        else:
            _reply(ch, root, "起票に失敗しました。DBの共有を確認してもらえますか？")
        return 1
    # reclassify（文面修正・振り分け変更・unclear の手掛かり）
    if it.get("propose_count", 1) >= _PROPOSE_CAP:
        _reply(ch, root, "うまく汲み取れていないかもしれません。「これでOK」か「却下」で教えてください。")
        return 1
    cs2 = _classify_intake(f"{it.get('mention_text', '')} / 戸田さんの指示: {m['text']}",
                           _thread_context(ch, root, m["ts"]))
    bills2 = [c for c in cs2 if c.get("type") in ("issue", "rule")]
    if bills2 or (cs2 and cs2[0].get("type") == "unclear"):
        it["proposals"] = bills2 or [cs2[0]]
        it["propose_count"] = it.get("propose_count", 1) + 1
        _reply(ch, root, _propose_text(it["proposals"]))
        return 1
    if cs2 and cs2[0].get("type") == "edit":
        st = _maybe_edit_root(ch, root, m["text"], m["text"])
        it["status"] = "cancelled"
        _reply(ch, root, _EDIT_MSG[st])
        return 1
    # 確認ターン中の質問には普通に答える＝紋切り型の「うまく汲み取れませんでした」で会話を断ち切らない
    # （2026-07-02 戸田「イシューではなくここで実装できる？」への紋切り返しが「こういう会話になっちゃう」）。
    # awaiting は維持＝回答後も GO・却下・文面修正を受け付ける。
    if cs2 and cs2[0].get("type") == "question":
        ans = _answer(m["text"], ch, root)
        if ans:
            # 案内フッターは付けない＝会話のループ感を出さない（2026-07-03 戸田「自然な会話をしたい」。
            # awaiting は維持しているので GO/却下/文面修正はそのまま受け付けられる）
            _reply(ch, root, ans)
            return 1
    if cs2 and cs2[0].get("type") == "none":
        _reply(ch, root, _smalltalk(m["text"]))
        return 1
    # どの分類にも落ちなかった（空・回答失敗）＝無返信を防ぐ。awaiting は維持して次の返信を待つ。
    it["propose_count"] = it.get("propose_count", 1) + 1
    _reply(ch, root, "うまく汲み取れませんでした。「これでOK」か「却下」、または直したい内容を具体的に教えてもらえますか？")
    return 1


def _handle_retract(m: dict, ch: str, root: str) -> int:
    """柱2（2026-07-07 戸田「もっと会話をスマートに」）: Chiaki AI の直前のアクションが誤り
    （宛先違い・内容違い・不要）という指摘への自己訂正。①スレッド内の直近の実質投稿に
    取り消し注記を編集で追記（削除しない＝記録を残す） ②紐づく裁定を retracted でクローズ
    ③率直な謝罪と言い直しの返事（GPT・文脈込み）。従来は Issue 提案に逃げて会話が重かった。"""
    thread = source.read_thread(ch, root)
    self_tag = f"<@{runtime.CHIAKI_SELF}>"
    posts = [x for x in thread if x.get("user_id") == runtime.CHIAKI_SELF
             and not (x.get("text") or "").lstrip().startswith(self_tag)]
    target = posts[-1] if posts else None
    if target:
        source.update_message(ch, target["ts"],
                              (target.get("text") or "") + "\n\n※この投稿は誤りでした。取り消します。")
    pend = runtime.load_json("pending_approvals.json", {"items": {}})
    post_ts = {p.get("ts") for p in posts}
    closed = 0
    for it in pend.get("items", {}).values():
        if it.get("status") in ("pending", "awaiting_completion") and (
                it.get("source_ts") == root or it.get("nudge_ts") in post_ts):
            it["status"] = "retracted"
            closed += 1
    if closed:
        runtime.save_json("pending_approvals.json", pend)
    ans = ""
    try:
        from lib import llm
        convo = "\n".join(f"- {(x.get('user_name') or x.get('user_id') or '?')}: {(x.get('text') or '')[:200]}"
                          for x in thread[-10:])
        ans = (llm.gpt(
            "あなたは Chiaki AI。あなたの直前のアクション（修正依頼など）が誤りだったと指摘されました。\n"
            f"スレッドのやりとり:\n{convo}\n指摘: {m.get('text') or ''}\n"
            "1〜3文で率直に謝り、何が正しかったのかを言い直す（です・ます調・感嘆符は全角！・"
            "@メンションや太字は書かない・言い訳をしない）。テキストのみを返す。",
            max_tokens=250) or "").strip()
    except Exception:
        pass
    _reply(ch, root, ans or "失礼しました！さきほどの投稿は誤りだったので取り消しました。")
    return 1


_PROGRESS_FIRST_SEC = 30   # ここまでに本応答が出なければ方向性を出す（2026-07-07 戸田「30秒待っても全然いい」）
_PROGRESS_EVERY_SEC = 60   # 以降の経過共有の間隔
_PROGRESS_NOTES = ("まだ考えをまとめています。もう少しだけお待ちください！",
                   "引き続きまとめています。時間がかかっていてすみません！")


def _progress_watch(ch: str, root: str, m: dict):
    """本応答に時間がかかるときの経過共有（2026-07-07 戸田要望「考え中のときは考えをメンションして」）。
    受信直後に方向性の一言（GPT 5.5）を並行生成し、30秒たっても本応答が出ていなければ投稿。
    以降60秒ごとに経過を上限2回＝2分超の沈黙を作らない。速く返せた時は何も出さない。
    見張りは別スレッド＝llm の使用記録は threading.local なので本応答のモデルタグと混ざらない。
    返り値＝本応答の完了時に呼ぶ cancel。"""
    done = threading.Event()

    def _run():
        t0 = time.time()
        direction = ""
        try:
            from lib import llm
            llm.reset_used()
            direction = (llm.gpt(
                "戸田さんから次のメッセージを受け取り、いま返事を考えています。\n"
                f"メッセージ: {(m.get('text') or '')[:400]}\n"
                "どう受け取ったか・どう動くつもりかの方向性だけを1〜2文で先に伝える"
                "（です・ます調・感嘆符は全角！・太字や*や@メンションは書かない・"
                "「考え中です」だけの中身のない文にしない）。テキストのみを返す。",
                max_tokens=150) or "").strip()
        except Exception:
            direction = ""
        if done.wait(max(0.0, _PROGRESS_FIRST_SEC - (time.time() - t0))):
            return
        if direction:
            _reply(ch, root, direction)
        for note in _PROGRESS_NOTES:
            if done.wait(_PROGRESS_EVERY_SEC):
                return
            try:
                from lib import llm
                llm.reset_used()  # 固定文＝モデルタグなし
            except Exception:
                pass
            _reply(ch, root, note)

    threading.Thread(target=_run, daemon=True).start()
    return done.set


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
    # uniq は ts 昇順。失敗が出たチャンネルはそれ以降カーソルを進めない＝失敗メッセージを取りこぼさず次回再処理する。
    maxts, failed, acted = {}, set(), 0
    for m, root, ch, _hint in uniq:
        try:  # モデル表記（GPT 5.4等）の取り違え防止＝メッセージごとに使用記録をリセット
            from lib import llm as _llm
            _llm.reset_used()
        except Exception:
            pass
        it = _find_awaiting(items, ch, root, m["ts"])
        try:
            if _hint == "escalate" or m.get("user_id") != runtime.TODA:
                # 戸田さん以外＝分類・実行せず戸田さんへ引き継ぎ（冪等：処理済みtsは再送しない）
                acted += 0 if m["ts"] in items else _escalate(items, m, ch, root)
            else:
                # 経過共有の見張り＝必ず返事が来る経路（確認ターン/明示メンション）だけ。
                # メンション無しのトップレベルは分類結果が「静観」の可能性があるため付けない。
                watch = it is not None or MENTION in (m.get("text") or "")
                cancel = _progress_watch(ch, root, m) if watch else (lambda: None)
                try:
                    if it:
                        res = _handle_confirm(it, m, ch, root)
                        acted += res if res is not None else _handle_propose(m, ch, root, items)
                    else:
                        acted += _handle_propose(m, ch, root, items)
                finally:
                    cancel()
            if ch not in failed:
                maxts[ch] = m["ts_float"]  # 連続成功プレフィックスの高水位だけ前進
        except Exception as e:
            failed.add(ch)
            print(f"[intake] error ch={ch} ts={m['ts']}: {e}")
    runtime.save_json("chiaki_intake.json", intake)
    for ch, mx in maxts.items():
        cur[ch] = max(float(cur.get(ch, 0.0)), mx)
    runtime.save_json("tuning_cursor.json", cur)
    print(f"[intake] acted={acted} failed_ch={len(failed)}")


if __name__ == "__main__":
    main()
