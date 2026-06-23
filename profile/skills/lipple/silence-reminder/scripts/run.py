#!/usr/bin/env python3
"""silence-reminder（§3.6）: 65分無音なら最終投稿にスレッド返信で1回。機械的＝承認不要。
cron 例: */5 * * * * （--no-agent / --script）
終業後は鳴らさない・連打しない（state の already_reminded_after_ts で担保）。LLM 非起動。
"""
import os
import sys
from pathlib import Path
sys.path.insert(0, os.environ.get("HERMES_LIB") or str(Path(__file__).resolve().parents[5]))
from lib import observe, runtime, source  # noqa: E402

TEAM = "lipple"  # Slack ワークスペース subdomain（permalink 用）
# 固定フォールバック文（Haiku 生成が失敗した時に必ず出す）
FALLBACK_BODY = "最後のご報告から{gap}分経過しています。進捗報告お願いします！"


def _compose(gap_min, now_ts) -> str:
    """本文を作る。Haiku で毎回少しゆらがせ、失敗時は固定文。@メンションは呼び側で付与。"""
    import datetime as _dt
    gap = int(gap_min)
    fb = FALLBACK_BODY.format(gap=gap)
    try:
        from lib import llm
        hour = _dt.datetime.fromtimestamp(now_ts, _dt.timezone(_dt.timedelta(hours=9))).hour
        prompt = (f"松永さんへの進捗リマインドを1〜2文で書いてください。"
                  f"経過は『{gap}分』とだけ書く（分単位。週・日・時間などの他単位や『先週』『今週』『昨日』に言い換えない）。"
                  f"現在は{hour}時台。進捗報告を依頼。宛名(@)は付けず本文だけ。")
        body = llm.haiku(prompt) or fb
        # 事実崩れガード: {gap}分が無い / 週・日に化けたら固定文へ
        if f"{gap}分" not in body or any(w in body for w in ("週", "日前", "時間前", "昨日", "先週", "今週")):
            body = fb
        return _regulate(body)
    except Exception:
        return fb


def _regulate(text: str) -> str:
    """生成文をレギュレーション（live同期の notation_rules）に通して自動補正＝自分も規約を守る。"""
    try:
        import json as _json
        rp = runtime.STATE_DIR / "notation_rules.json"
        if rp.exists():
            rules = _json.loads(rp.read_text(encoding="utf-8"))
            fixed, _ = observe.apply_notation_fixes(text, rules)
            return fixed
    except Exception:
        pass
    return text


def main():
    ch = runtime.CH_YU_PDCA
    now = runtime.now_ts()
    timers = runtime.load_json("channel_timers.json", {})
    t = timers.get(ch, {})

    recent = source.read_recent(ch, limit=50)
    if not recent:
        print("[SILENT] no messages")
        return
    today = recent[-1]["datetime"][:10]
    today_msgs = [m for m in recent if m["datetime"][:10] == today]

    dec = observe.silence_decision(
        today_msgs, now,
        already_reminded_after_ts=t.get("already_reminded_after_ts"))
    if not dec["fire"]:
        print(f"[SILENT] {dec['reason']} ({dec.get('gap_min','-')}min)")
        return

    last = today_msgs[-1]
    body = _compose(dec["gap_min"], now)
    text = f"<@{last['user_id']}>\n{body}"
    res = source.post_thread_reply(ch, dec["target_ts"], text)
    nudge_ts = res.get("ts") if isinstance(res, dict) else None
    # 連打防止フラグ
    t["already_reminded_after_ts"] = last["ts_float"]
    timers[ch] = t
    runtime.save_json("channel_timers.json", timers)
    # 控えを #8902 へ（提案と同体裁・対象=チャンネルURL・末尾に促した投稿への deep link）
    ch_url = f"https://{TEAM}.slack.com/archives/{ch}"
    # リマインド控え＝処理/独り言なのでセルフメンション（@Chiaki AI）。戸田さんはpingしない
    notice = (f"<@{runtime.CHIAKI_SELF}>\n"
              f"報告：リマインド控え\n"
              f"対象：{ch_url}\n\n"
              f"最終投稿（{last['datetime']}）から{int(dec['gap_min'])}分無音 → スレッドで1回促しました。")
    if nudge_ts:
        link = (f"https://{TEAM}.slack.com/archives/{ch}/p{nudge_ts.replace('.', '')}"
                f"?thread_ts={dec['target_ts']}&cid={ch}")
        notice += f"\n\n{link}"
    source.post_message(runtime.CH_CHIAKI_MGMT, notice)
    print(f"[silence] fired: gap={dec['gap_min']}min target={dec['target_dt']}")


if __name__ == "__main__":
    main()
