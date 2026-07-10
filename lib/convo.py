"""会話コア v2（Phase A+B+C・2026-07-10 戸田「根本として会話が自然じゃない」への抜本対応）。

どの入口（intake の初回応対・確認ターン・codex-runner のスレッド対話）でも、同じ文脈パッケージ＝
①スレッド全文（自分の発言を「あなた」と明示） ②このスレッドでの自分の状態・行動の事実
（裁定・起票・Codex作業・出した通知の内容） ③修正報告の知識 ④システム状態（Codex利用上限等）
⑤長期記憶（決定・好み・注意点）＋直近の別スレッドのやりとり（Phase C）
を読んでから、統一されたアクション集合で「返事」と「対応」を一括判断する。

このモジュールは判断のみ＝実行は各スキルの決定論ハンドラ（口はGPT・手は決定論）。
出力不正・LLM不通は None を返し、呼び側が従来ロジックへフォールバックする（無反応にしない）。

Phase C（スレッドを跨ぐ記憶）:
- 会話台帳: decide() の判断が実際に実行へ採用された時だけ、呼び側が commit() で convo_memory.json に
  記録する（採用されずフォールバックした判断は記録しない＝記憶と事実を食い違わせない）。
- 長期記憶: convo-memory（夜間cron）が台帳から「決定・好み・注意点」を短文に蒸留して同ファイルの
  notes に保持。decide() は毎回この2つを注入＝スレッドを跨いでも「さっきの件」「昨日の話」が通じる。
"""
from __future__ import annotations

import datetime as dt
import json
import re

from lib import runtime, source

NAMES = {"U9R35H06L": "Masaki Toda", "U09T44VEZM1": "Yu Matsunaga", "U9UA8NQCB": "Risa Nemoto"}

# 統一アクション集合（説明はそのままプロンプトに載る）
ACTIONS = {
    "propose": "新しい指摘・依頼を起票の確認にかける。proposals に案（type=issue|rule・issue_kind=バグ|変更|新機能|その他・"
               "rule_kind=用語|レギュレーション|スタイル・要約・詳細・routine）。起票は必ず確認後＝この場で登録はしない。"
               "reply で案の中身を自然に示して確認を求める（「登録してもいいですか？」の紋切りにしない）。",
    "file": "提示済みの案の登録を承認された（OK/はい/Issueに追加で 等。他の話が同居していても承認が含まれていれば file）。"
            "reply には登録した旨＋同居していた話への応答（URLはシステムが付ける）。"
            'issueの場合 "codex": true/false＝コードの修正・変更・機能追加なら true（そのままCodexが実装・既定）、'
            "起票だけの希望やコード外の作業は false。返信に新しい指摘が同居していれば proposals に次の案。",
    "revise": "案の内容・振り分けの変更指示（「それRuleね」等）。proposals に修正版の全件。reply で新しい案を示して確認。",
    "cancel": "却下・見送り。",
    "edit_post": "特定の投稿そのものを直す依頼。instruction に直し方を具体的に。",
    "company_rule": "このルールを社内レギュレーション_DB（正本）にも登録する依頼。company に "
                    '{"rule","content","category"(用字・表記|数字・英字|記号・約物|文末・語尾|表現・NG|体裁・構成),"wrong","right"}。',
    "codex_continue": "進行中のCodex作業への追加指示・やり直し。instruction に Codex への指示を具体的に。reply は着手の一言。",
    "deploy_request": "本番反映の依頼（「反映して」等）。反映はClaude Codeのレビュー後＝その旨を reply で自然に。",
    "retract": "あなたの直前のアクション（修正依頼・通知・リマインド等）が誤り・宛先違い・不要と指摘された。"
               "reply で率直に謝り正しい内容を言い直す（システムが該当投稿に取り消し注記を入れ、関連する裁定を閉じる）。",
    "answer": "回答・雑談・承知＝アクションなしの自然な返事。「なぜ？」は修正報告の記録に基づき「こういうバグでした・"
              "こう直しました」と答える。記録に無いことは正直に分からないと言う。",
    "silent": "応答不要（FYI・独り言など、返事がかえって邪魔な場合）。reply は空。",
}

MODES = {
    "initial": ("propose", "edit_post", "retract", "company_rule", "answer", "silent"),
    "confirm": ("file", "revise", "cancel", "edit_post", "company_rule", "retract", "answer"),
    "filed": ("propose", "company_rule", "retract", "edit_post", "answer"),
    "codex_thread": ("codex_continue", "deploy_request", "retract", "company_rule", "answer"),
}


def display_name(uid: str) -> str:
    return NAMES.get(uid) or source.user_display_name(uid) or "参加者"


# ── Phase C: スレッドを跨ぐ記憶 ─────────────────────────
MEM_FILE = "convo_memory.json"
LEDGER_CAP = 120   # 会話台帳の保持件数（リングバッファ）
NOTES_CAP = 25     # 長期記憶（決定・好み・注意点）の上限
_last: dict | None = None  # decide() の直近の判断（commit() されるまでの仮置き）

_CH_LABEL = {runtime.CH_CHIAKI_MGMT: "#8902", runtime.CH_CHIAKI_PDCA: "#5902"}


def memory() -> dict:
    return runtime.load_json(MEM_FILE, {"ledger": [], "notes": []})


def commit() -> None:
    """直近の decide() の判断を会話台帳へ記録する。呼ぶのは判断を実行に採用した呼び側だけ＝
    フォールバックに落ちた判断は残らない（台帳は「実際にあった会話」の記録）。"""
    global _last
    if not _last:
        return
    try:
        mem = memory()
        led = mem.setdefault("ledger", [])
        led.append(_last)
        mem["ledger"] = led[-LEDGER_CAP:]
        runtime.save_json(MEM_FILE, mem)
    except Exception:
        pass
    _last = None


def _notes_text(mem: dict) -> str:
    out = []
    for n in (mem.get("notes") or [])[:NOTES_CAP]:
        note = (n.get("note") or "").strip()
        if note:
            kind = n.get("kind") or ""
            out.append(f"- {('[' + kind + '] ') if kind else ''}{note[:120]}")
    return "\n".join(out) or "- （まだ無し）"


def _cross_thread_text(mem: dict, root: str, n: int = 6) -> str:
    """直近の別スレッドでのやりとり（新しい順）。今のスレッドの分は本文で読めるので除外。"""
    out = []
    for e in reversed(mem.get("ledger") or []):
        if e.get("root") == root:
            continue
        ch_label = _CH_LABEL.get(e.get("ch") or "", "")
        act = e.get("action") or ""
        if e.get("gist"):
            act += f"＝{e['gist'][:60]}"
        out.append(f"- [{e.get('dt', '')}{('・' + ch_label) if ch_label else ''}] "
                   f"戸田さん「{(e.get('said') or '')[:80]}」→あなた({act}):"
                   f"「{(e.get('reply') or '')[:80]}」")
        if len(out) >= n:
            break
    return "\n".join(out) or "- （直近の別スレッドの会話なし）"


def fix_reports(n: int = 6) -> str:
    """#8902 の直近の修正報告＝自分がどう直されてきたかの知識源。"""
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


def thread_facts(ch: str, root: str) -> list[str]:
    """このスレッドに関する自分の状態・行動の事実（決定論で組み立て＝GPTに推測させない）。
    「自分が何をしたか」を知らないまま辻褄合わせの返答をする問題（松永/松下誤通知の生返事）の根治。"""
    facts: list[str] = []
    try:
        pend = runtime.load_json("pending_approvals.json", {"items": {}}).get("items", {})
        it = pend.get(root)
        if it:  # このスレッド＝裁定（提案）スレッド
            tgt = it.get("target_name") or display_name(it.get("target_user_id") or "")
            facts.append(f"このスレッドはあなたの{it.get('finding_kind', '')}提案。状態={it.get('status')}・"
                         f"対象={tgt}さんの投稿。")
            if it.get("final_text"):
                facts.append(f"あなたが対象スレッドへ出した依頼文:「{it['final_text'][:150]}」")
            if it.get("status") == "completed":
                facts.append(f"あなたは完了通知を出し済み（修正した人={tgt}さん）。")
        for k, v in pend.items():
            if v.get("source_ts") == root and v is not it:
                facts.append(f"このスレッドの投稿への裁定（提案{k}）: 状態={v.get('status')}・"
                             f"あなたの依頼文:「{(v.get('final_text') or v.get('draft') or '')[:120]}」")
    except Exception:
        pass
    try:
        intake = runtime.load_json("chiaki_intake.json", {"items": {}}).get("items", {})
        for v in intake.values():
            if v.get("thread_root") == root:
                urls = v.get("page_urls") or ([v["page_url"]] if v.get("page_url") else [])
                line = f"起票の状態: {v.get('status')}"
                if urls:
                    line += f"・登録済み: {' '.join(urls)}"
                props = v.get("proposals") or []
                if props and v.get("status") == "awaiting_confirm":
                    line += "・提示中の案: " + "／".join((p.get("要約") or "")[:60] for p in props)
                facts.append(line)
    except Exception:
        pass
    try:
        codex = runtime.load_json("codex_threads.json", {"items": {}}).get("items", {}).get(root)
        if codex:
            facts.append(f"このスレッドのCodex作業: {codex.get('summary', '')}・ブランチ{codex.get('branch', '')}"
                         + ("・反映依頼を記録済み" if codex.get("deploy_requested") else "")
                         + "。本番反映はClaude Codeのレビュー後。")
            if codex.get("last_output"):
                facts.append(f"Codexの直前の報告: {codex['last_output'][:200]}")
    except Exception:
        pass
    try:
        # task-follow のリマインドA送信記録（キー=A:ch:root:report_ts）＝「これどういうロジックで
        # リマインドした？」に事実で答えるため（2026-07-10 a040 スレッドの実質問）
        for k, v in runtime.load_json("task_follow.json", {}).items():
            parts = k.split(":")
            if len(parts) == 4 and parts[0] == "A" and parts[1] == ch and parts[2] == root:
                sent_ts = float(v.get("ts") if isinstance(v, dict) else v or 0)
                when = dt.datetime.fromtimestamp(sent_ts, dt.timezone(dt.timedelta(hours=9))
                                                 ).strftime("%m-%d %H:%M") if sent_ts else "?"
                facts.append(f"このスレッドの「報告の確認をお願いします！」（{when}）はあなたが出したリマインドA"
                             "（task-followスキル・平日8:50の定時実行）。ロジック=スレッド内の最新の完了報告で"
                             "メンションされた責任者が、翌営業日になっても返信していない場合に確認を依頼"
                             "（1つの報告につき1回だけ・kanryoスタンプ済みやスレッドURL付きの進捗共有は対象外）。")
    except Exception:
        pass
    try:
        quota = runtime.load_json("codex_quota.json", {})
        if quota.get("blocked"):
            facts.append("システム状態: ChatGPTプランのCodex利用上限に到達中＝新規のCodex実装は当面不可"
                         "（コード修正はClaude Codeが代行する運用）。")
    except Exception:
        pass
    return facts


def build_convo(ch: str, root: str, limit: int = 20) -> str:
    try:
        thread = source.read_thread(ch, root)
    except Exception:
        thread = []
    lines = []
    for x in thread[-limit:]:
        uid = x.get("user_id") or ""
        who = "Chiaki AI（あなた）" if uid == runtime.CHIAKI_SELF else display_name(uid)
        lines.append(f"- {who}: {(x.get('text') or '')[:250]}")
    return "\n".join(lines) or "（スレッドなし）"


def decide(ch: str, root: str, m: dict, mode: str, extra_facts: list[str] | None = None):
    """統一判断。返り値＝{"action", "reply", "proposals", "instruction", "company", "codex"} / None。
    判断を実行に採用した呼び側は commit() を呼んで会話台帳に残す（Phase C）。"""
    global _last
    _last = None  # 前回の未採用の判断が誤って記録されないように毎回リセット
    try:
        from lib import llm
    except Exception:
        return None
    allowed = MODES.get(mode)
    if not allowed:
        return None
    facts = thread_facts(ch, root) + (extra_facts or [])
    facts_txt = "\n".join(f"- {f}" for f in facts) or "- （特になし）"
    actions_txt = "\n".join(f"- {a}: {ACTIONS[a]}" for a in allowed)
    mem = memory()
    prompt = (
        "あなたは Chiaki AI（Lipple の業務観測AI）。Slackで戸田さんと自然に会話しながら業務を進めます。\n"
        "規約: です・ます調／感嘆符は全角！／太字や*は使わない／@メンションは書かない／絵文字なし／"
        "1〜5文で簡潔に／定型の案内文・同じ言い回しを繰り返さない／知らないことは推測せず正直に言う。\n"
        "あなたが書き込めるNotion: Rule Registry（自分の言葉のルール）・Issue_DB（不具合バックログ）・"
        "社内レギュレーション_DB。それ以外のDB・ページは権限（共有）が無い＝頼まれたら共有してもらえれば"
        "対応できると正直に案内する。\n"
        "「さっきの件」「昨日の話」のような指示語は、長期記憶と別スレッドのやりとりも踏まえて解釈する。\n\n"
        f"# あなたの長期記憶（これまでの決定・戸田さんの好み・注意点）\n{_notes_text(mem)}\n\n"
        f"# このスレッドでのあなたの状態・行動（事実。これと矛盾する返答をしない）\n{facts_txt}\n\n"
        f"# 直近の別スレッドでのやりとり（新しい順）\n{_cross_thread_text(mem, root)}\n\n"
        f"# スレッドのやりとり（古い順）\n{build_convo(ch, root)}\n\n"
        f"# 最近の修正報告（あなた自身の不具合と直した内容の記録・新しい順）\n{fix_reports()}\n\n"
        f"# 新しい発話（戸田さん）\n{(m.get('text') or '')[:600]}\n\n"
        f"# 取れるアクション（この中から1つ）\n{actions_txt}\n\n"
        "JSON のみで返す: {\"action\": \"...\", \"reply\": \"...\", \"proposals\": [], "
        "\"instruction\": \"\", \"company\": {}, \"codex\": true}"
    )
    llm.reset_used()
    out = llm.gpt(prompt, max_tokens=900) or ""
    mm = re.search(r"\{.*\}", out, re.S)
    if not mm:
        return None
    try:
        d = json.loads(mm.group(0))
    except Exception:
        return None
    action = d.get("action")
    if action not in allowed:
        return None
    if action != "silent" and not (d.get("reply") or "").strip():
        return None
    d["reply"] = (d.get("reply") or "").strip()
    jst = dt.datetime.fromtimestamp(runtime.now_ts(), dt.timezone(dt.timedelta(hours=9)))
    gist = ""
    if action in ("propose", "revise", "file"):
        gist = "／".join((p.get("要約") or "")[:40] for p in (d.get("proposals") or [])
                        if isinstance(p, dict))[:120]
    elif action in ("edit_post", "codex_continue"):
        gist = (d.get("instruction") or "")[:120]
    _last = {"ts": runtime.now_ts(), "dt": jst.strftime("%m-%d %H:%M"), "ch": ch, "root": root,
             "mode": mode, "said": (m.get("text") or "")[:160], "action": action,
             "reply": d["reply"][:160], **({"gist": gist} if gist else {})}
    return d
