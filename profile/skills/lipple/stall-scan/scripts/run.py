#!/usr/bin/env python3
"""stall-scan（§3.8）: 業務チャンネルのタスク根が生きているか。日単位。
cron 例: 0 9 * * 1-5 （任意 0 14 追加）。--no-agent / --script。
活動＝人間の反応のみ（GCP Task AI bot の投下は動きにしない）。
検知（着手なし/期限近接無動）は機械的＝#8902 に控え。team への促し文面は承認系（findings）。
"""
import os
import sys
from pathlib import Path
sys.path.insert(0, os.environ.get("HERMES_LIB") or str(Path(__file__).resolve().parents[5]))
from lib import observe, runtime, source  # noqa: E402


def main():
    ch = runtime.CH_NICHIJI
    now = runtime.now_ts()
    bots = {runtime.GCP_TASK_BOT}

    msgs = source.read_recent(ch, limit=200)
    # 各タスク根の human_replies を埋める（live はスレッド取得して bot 除外、fixtures は None→thread_replies代用）
    for m in msgs:
        if observe.parse_biz_task(m["text"]):
            hr = source.human_replies(ch, m, bots)
            if hr is not None:
                m["human_replies"] = hr

    cands = observe.stall_scan(msgs, now, bot_user_ids=bots)
    if not cands:
        print("[SILENT] no stalls")
        return

    # dedup/cooldown: 同一停滞(channel|task|due)を signals 不変なら COOLDOWN 内は再通知/再記録しない。
    # 新規 or signals 変化 or cooldown 経過のものは必ず通知＝無音失敗にしない。findings.jsonl 肥大も抑える。
    COOLDOWN = 3 * 86400
    seen = runtime.load_json("stall_seen.json", {})
    fresh = []
    for s in cands:
        key = f"{ch}|{(s.get('task') or '')[:60]}|{s.get('due')}"
        sig = sorted(s.get("signals", []))
        prev = seen.get(key)
        if prev is None or prev.get("signals") != sig or now - float(prev.get("last", 0)) >= COOLDOWN:
            fresh.append(s)
            seen[key] = {"last": now, "signals": sig}
    seen = {k: v for k, v in seen.items() if now - float(v.get("last", 0)) < 30 * 86400}  # 古い記録を掃除
    runtime.save_json("stall_seen.json", seen)
    if not fresh:
        print(f"[SILENT] {len(cands)} stalls all within cooldown")
        return

    lines = [f"[停滞検知 {len(fresh)}件] now={runtime.time.strftime('%Y-%m-%d', runtime.time.gmtime(now+9*3600))}"]
    for s in fresh:
        lines.append(f"・{','.join(s['signals'])} | {s['task']} | 期限{s['due']} 人活動{s['human_replies']} "
                     f"経過{s['age_days']}日 root_by_bot={s['root_by_bot']}")
        # team への促し方は承認系
        runtime.record_finding("stall", {"channel": ch, **s})
    # 機械的検知の控えは #8902 へ
    source.post_message(runtime.CH_CHIAKI_MGMT, "\n".join(lines))
    print("\n".join(lines))


if __name__ == "__main__":
    main()
