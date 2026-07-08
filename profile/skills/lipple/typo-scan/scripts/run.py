#!/usr/bin/env python3
"""typo-scan（§3.5 Layer2・自由文の誤字/表記ミス検知／LLM=Haiku・保守的）。

辞書(notation_check)で拾えない一般的な誤字・誤変換・スペルミス・明らかな助詞誤りを、
1回の Haiku 呼び出しで当日新着メッセージからまとめて高精度に抽出する。
固有名詞・製品名・人名・社内既知用語・意図的な英字大小は誤字としない（誤検知を強く避ける）。
検知は findings(kind=typo) に積み、propose が #8902 へ承認提案（以降は notation と同じループ）。
対象: #5035 の当日新着（投稿元＋スレッド返信。bot/自分は除外）。cron: 50 17 / 30 18（平日）。
辞書層と重複する found はスキップ（二重提案防止）。cursor は typo_cursor.json（obs-batch と別ファイル）。
"""
import json
import os
import re
import sys
from pathlib import Path
sys.path.insert(0, os.environ.get("HERMES_LIB") or str(Path(__file__).resolve().parents[5]))
from lib import observe, runtime, source  # noqa: E402

CHANNELS = [runtime.CH_YU_PDCA]  # 必要なら #a027 を追加
MAX_MSGS = 40                    # 1回の Haiku に渡す最大件数


def _rules():
    p = runtime.STATE_DIR / "notation_rules.json"
    if p.exists():
        return json.loads(p.read_text(encoding="utf-8"))
    return {"terms": [], "acronyms": [], "style_rules": []}


def _known(rules):
    out = [t.get("official") for t in rules.get("terms", []) if t.get("official")]
    out += list(rules.get("acronyms", []))
    return [x for x in out if x][:60]


def _gather(ch, since, bots):
    """当日・since 以降の新着（投稿元＋スレッド返信、bot/空文除外）。(messages, new_cursor)。"""
    import datetime as dt
    recent = source.read_recent(ch, oldest_ts=since or None, limit=200)
    if not recent:
        return [], since
    # 「当日」は実際の現在JST日付（チャンネル最終投稿の日付だと、週末明けに過去日の投稿を再検査する＝監査確定）
    today = dt.datetime.now(dt.timezone(dt.timedelta(hours=9))).strftime("%Y-%m-%d")
    msgs, maxts, seen = [], (since or 0.0), set()
    for m in recent:
        if m["datetime"][:10] != today:
            continue
        if m["ts_float"] > (since or 0) and m["user_id"] not in bots and m["text"].strip():
            msgs.append({**m, "_root": m["ts"]})  # _root=スレッド根（文脈精査・作者宛先の柱1用）
            seen.add(m["ts"])
        maxts = max(maxts, m["ts_float"])
        if m.get("thread_replies"):
            for r in source.read_thread(ch, m["ts"]):
                # thread_broadcast は履歴とスレッドの両方に現れる＝ts で二重取り込みを防ぐ（監査確定）
                if r["ts"] in seen or r["ts"] == m["ts"] or r["ts_float"] <= (since or 0) or r["user_id"] in bots:
                    continue
                if r["text"].strip():
                    msgs.append({**r, "_root": m["ts"]})
                    seen.add(r["ts"])
                maxts = max(maxts, r["ts_float"])
    return msgs, maxts


def _detect(messages, known):
    """1回の Haiku で複数メッセージの誤字を JSON 抽出。失敗時は []。"""
    try:
        from lib import llm
    except Exception:
        return []
    numbered = "\n".join(f'[{i}] {m["text"][:200]}' for i, m in enumerate(messages))
    sysp = ("あなたは日本語ビジネス文の校正者。明確な誤字・誤変換・スペルミス・明らかな助詞の誤りだけを指摘する。"
            "固有名詞・製品名・人名・社内用語・意図的な英字大小・口語やスタイルの好みは指摘しない。"
            "確信が持てないものは出さない（誤検知を強く避ける）。出力は JSON のみ・前置きなし。")
    user = ("次の各行『[i] 本文』から、明確な誤字だけを抽出してください。"
            f"次は正しい既知用語なので誤字にしない: {', '.join(known)}。"
            'JSON配列で各要素 {"i": 行番号, "found": "誤った表記", "suggest": "正しい表記"} を返す。'
            "誤字が無ければ [] だけ返す。\n" + numbered)
    out = llm.haiku(user, system=sysp, max_tokens=700) or ""
    mm = re.search(r"\[.*\]", out, re.S)
    if not mm:
        return []
    try:
        return json.loads(mm.group(0))
    except Exception:
        return []


def main():
    bots = {runtime.GCP_TASK_BOT, runtime.CHIAKI_SELF}
    rules = _rules()
    known = _known(rules)
    cur = runtime.load_json("typo_cursor.json", {})
    total = 0
    for ch in CHANNELS:
        since = cur.get(ch, 0.0)
        msgs, maxts = _gather(ch, since, bots)
        if msgs:
            for h in _detect(msgs[:MAX_MSGS], known):
                try:
                    i = int(h.get("i"))
                except (TypeError, ValueError):
                    continue
                found, suggest = h.get("found", ""), h.get("suggest", "")
                if not (0 <= i < len(msgs)) or not found or not suggest or found == suggest:
                    continue
                msg = msgs[i]
                # Haiku の found が対象投稿に実在しない＝幻覚 or 行番号取り違え → 捨てる（監査確定 high：
                # 実在しない検知は #8902 提案が事実と不一致になり、GO 後に「本文に無い＝修正済み」と
                # 誤判定されて催促直後に偽の完了お礼・完了通知まで連鎖する）。
                if found not in msg["text"]:
                    continue
                # 辞書層(notation_check)と重複する found はスキップ（二重提案防止）
                if any(iss.get("found") == found for iss in observe.notation_check(msg["text"], rules)):
                    continue
                runtime.record_finding("typo", {
                    "channel": ch, "msg_ts": msg["ts"], "msg_dt": msg["datetime"],
                    "issue": {"found": found, "suggest": suggest}, "excerpt": msg["text"][:80],
                    # 柱1（2026-07-07 戸田「文脈よんでほしい」）: 作者＝指摘先・スレッド根＝文脈精査用
                    "author": msg["user_id"], "thread_root": msg.get("_root") or msg["ts"]})
                total += 1
        # MAX_MSGS で切り捨てた未スキャン分(最新側)を飛ばさない＝落とした最小 ts の手前までしか進めない
        dropped = msgs[MAX_MSGS:]
        cur[ch] = max(since, min(m["ts_float"] for m in dropped) - 1e-6) if dropped else maxts
    runtime.save_json("typo_cursor.json", cur)
    print(f"[typo-scan] findings={total}")


if __name__ == "__main__":
    main()
