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
# 固定フォールバック文（Haiku 生成が失敗/不自然/へりくだり過ぎた時に必ず出す）。言い切り・へりくだらない
FALLBACK_BODY = "最後の報告から{gap}分が経過しているので、報告をお願いします！"


def _compose(gap_min, now_ts) -> str:
    """本文を作る。Haiku で毎回少しゆらがせ、失敗時は固定文。@メンションは呼び側で付与。"""
    import datetime as _dt
    gap = int(gap_min)
    fb = FALLBACK_BODY.format(gap=gap)
    try:
        from lib import llm
        hour = _dt.datetime.fromtimestamp(now_ts, _dt.timezone(_dt.timedelta(hours=9))).hour
        prompt = (f"松永さんへの進捗リマインドを1文で書いてください。"
                  f"必ず『{gap}分が経過』を入れ『最後の報告から{gap}分が経過しているので』と続け、明確に報告を依頼する"
                  f"（例『報告をお願いします！』）。進捗は必ずあるので、へりくだらず言い切る"
                  f"（『もし〜あれば』『〜と嬉しいです』『差し支えなければ』『幸いです』等は使わない）。現在は{hour}時台。"
                  f"絵文字なし・宛名(@)なし・本文のみ。日付や他の時間単位（週/日/時間）は使わない。")
        tn = runtime.load_tuning("silence")  # 既存 tuning.json（旧 soft 学習の凍結データ）を反映
        if tn:
            prompt += " 戸田さんの指示（必ず守る）: " + "; ".join(tn) + "。"
        body = llm.haiku(prompt) or fb
        # 事実崩れ/へりくだり過ぎガード → 固定文（言い切り）へ
        ok = (f"{gap}分が経過" in body) or (f"{gap}分経過" in body)
        bad = any(w in body for w in ("もし", "あれば", "嬉し", "幸い", "ただけれ",
                                      "週", "日前", "時間前", "昨日", "先週", "今週"))
        if not ok or bad:
            body = fb
        return _regulate(body)
    except Exception:
        return fb


def _regulate(text: str) -> str:
    """生成文をレギュレーション（regulations.json）で決定論補正＝自分も規約を守る（無ければ原文）。"""
    return observe.enforce_regulations(text)


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
        today_msgs, now, already_reminded_after_ts=t.get("already_reminded_after_ts"))
    if not dec["fire"]:
        print(f"[SILENT] {dec['reason']} ({dec.get('gap_min', '-')}min)")
        return

    last = today_msgs[-1]
    body = _compose(dec["gap_min"], now)
    res = source.post_thread_reply(ch, dec["target_ts"], f"<@{last['user_id']}>\n{body}")
    nudge_ts = res.get("ts") if isinstance(res, dict) else None
    # 自分のキー(already_reminded_after_ts)だけ最新へ書き戻す＝並行 obs-batch(*/10)のキーを巻き戻さない(二重nudge防止)
    latest = runtime.load_json("channel_timers.json", {})
    merged = latest.get(ch, {})
    merged["already_reminded_after_ts"] = last["ts_float"]
    latest[ch] = merged
    runtime.save_json("channel_timers.json", latest)
    # 控えを #8902 へ（セルフメンション＝戸田さんはping無し・対象=チャンネルURL・末尾に促した投稿リンク）
    ch_url = f"https://{TEAM}.slack.com/archives/{ch}"
    notice = (f"<@{runtime.CHIAKI_SELF}>\n報告：リマインド控え\n対象：{ch_url}\n\n"
              f"最終投稿（{last['datetime']}）から{int(dec['gap_min'])}分無音 → スレッドで1回促しました。")
    if nudge_ts:
        notice += (f"\n\nhttps://{TEAM}.slack.com/archives/{ch}/p{nudge_ts.replace('.', '')}"
                   f"?thread_ts={dec['target_ts']}&cid={ch}")
    source.post_message(runtime.CH_CHIAKI_MGMT, notice)
    print(f"[silence] fired: gap={dec['gap_min']}min target={dec['target_dt']}")


if __name__ == "__main__":
    main()
