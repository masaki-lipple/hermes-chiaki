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


# 台帳の対象外＝業務チャンネルでないもの（松永さんPDCA・chiaki発信ch）。それ以外の参加chは自動で対象。
_LEDGER_EXCLUDE = {runtime.CH_YU_PDCA, runtime.CH_CHIAKI_PDCA, runtime.CH_CHIAKI_MGMT}


def main():
    if not runtime.is_jp_workday():
        print("[SILENT] holiday/weekend")  # 祝日に停滞検知を出さない（cron は曜日しか知らない）
        return
    now = runtime.now_ts()
    bots = {runtime.GCP_TASK_BOT}

    # 台帳: bot が参加している業務チャンネル全部（a025/a027/a035…・新chは招待だけで対象化）。
    # 30日窓をページングで全取得し、タスク根を毎回全再生成（冪等）。チャンネル単位で失敗を隔離。
    tasks, ledger_msgs = {}, {}
    for c in source.list_bot_channels():
        ch_id = c.get("id")
        if not ch_id or ch_id in _LEDGER_EXCLUDE:
            continue
        try:
            msgs = source.read_recent(ch_id, oldest_ts=now - 30 * 86400, limit=200, paginate=True)
            # 各タスク根の human_replies を埋める（live はスレッド取得して bot 除外、fixtures は None→thread_replies代用）
            for m in msgs:
                if observe.parse_biz_task(m["text"]):
                    hr = source.human_replies(ch_id, m, bots)
                    if hr is not None:
                        m["human_replies"] = hr
            ledger_msgs[ch_id] = msgs
        except Exception as e:
            print(f"[stall-scan] ledger fetch failed ch={ch_id}: {e}")
            continue
        for m in msgs:
            parsed = observe.parse_biz_task(m["text"])
            if not parsed:
                continue
            tasks[f"{ch_id}:{m['ts']}"] = {
                "channel": ch_id, "channel_name": c.get("name", ""), "ts": m["ts"],
                "task": parsed["task"],
                "due": parsed["due"],
                "assignees": parsed["assignees"],
                "author": m["user_id"],
                "datetime": m["datetime"],
                "reactions": m.get("reactions", []),
                "human_replies": m.get("human_replies"),
            }
    runtime.save_json("task_ledger.json", {"updated_at": now, "tasks": tasks})

    # 停滞検知は従来どおり #a027 のみ（挙動不変）
    ch = runtime.CH_NICHIJI
    msgs = ledger_msgs.get(ch) or []
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

    # chiaki 自身の機械的検知の控え＝セルフメンション付き（戸田さんへ ping はしない・トーン規約）。silence-reminder と同形式。
    ch_url = f"https://lipple.slack.com/archives/{ch}"
    date = runtime.time.strftime('%Y-%m-%d', runtime.time.gmtime(now + 9 * 3600))
    lines = [f"<@{runtime.CHIAKI_SELF}>", "報告：停滞検知", f"対象：{ch_url}", "",
             f"停滞を{len(fresh)}件検知しました（{date}時点）。"]
    for s in fresh:
        lines.append(f"• {','.join(s['signals'])} | {s['task']} | 期限{s['due']} 人活動{s['human_replies']} "
                     f"経過{s['age_days']}日 root_by_bot={s['root_by_bot']}")
        # team への促し方は承認系
        runtime.record_finding("stall", {"channel": ch, **s})
    # 機械的検知の控えは #8902 へ
    source.post_message(runtime.CH_CHIAKI_MGMT, "\n".join(lines))
    print("\n".join(lines))


if __name__ == "__main__":
    main()
