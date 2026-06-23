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

REMINDER_TMPL = (
    "<@{user}>\n"
    "最後のご報告から{gap}分ほど空いています。進捗報告お願いします！"
)


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
    text = REMINDER_TMPL.format(user=last["user_id"], gap=int(dec["gap_min"]))
    source.post_thread_reply(ch, dec["target_ts"], text)
    # 連打防止フラグ
    t["already_reminded_after_ts"] = last["ts_float"]
    timers[ch] = t
    runtime.save_json("channel_timers.json", timers)
    # 控えを #8902 へ
    source.post_message(runtime.CH_CHIAKI_MGMT,
                        f"[リマインド控え] {ch} の最終投稿({last['datetime']})から{int(dec['gap_min'])}分無音 → スレッドで1回促しました。")
    print(f"[silence] fired: gap={dec['gap_min']}min target={dec['target_dt']}")


if __name__ == "__main__":
    main()
