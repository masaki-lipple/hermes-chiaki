#!/usr/bin/env python3
"""obs-batch（§3.1/3.2/3.3/3.4/3.5-Layer1）: 監視PDCAの新着をまとめ処理。決定論・LLM非起動。
cron 例: */10 9-19 * * 1-5 （--no-agent / --script）
- 朝スケジュール → plan_<date>.json（予定工数）
- 開始↔終了 → actuals_<date>.jsonl（実測・予実差）
- channel_timers 更新（last_post_ts / end_of_work_date / last_processed_ts）
- notation_check(Layer1) で表記候補を findings へ（承認系。自動投稿しない）
判断・文面が要る所は findings に積むだけ。LLM は propose/typo 側で。
"""
import os
import sys
from pathlib import Path
sys.path.insert(0, os.environ.get("HERMES_LIB") or str(Path(__file__).resolve().parents[5]))
from lib import observe, runtime, source  # noqa: E402


def main():
    ch = runtime.CH_YU_PDCA
    now = runtime.now_ts()
    timers = runtime.load_json("channel_timers.json", {})
    t = timers.get(ch, {})
    last_processed = t.get("last_processed_ts") or 0.0
    policy = runtime.load_policy()

    recent = source.read_recent(ch, limit=200)
    if not recent:
        print("[SILENT] no messages")
        return
    today = recent[-1]["datetime"][:10]
    today_msgs = [m for m in recent if m["datetime"][:10] == today]

    # 予定工数
    sched = next((observe.parse_schedule(m["text"]) for m in today_msgs
                  if observe.is_schedule_post(m["text"])), None)
    if sched:
        runtime.save_json(f"plan_{today}.json", sched)

    # 実測・予実
    ev = observe.extract_task_events(today_msgs)
    # actuals は当日分を毎回再生成（冪等）
    runtime.save_json(f"actuals_{today}.json",
                      {"date": today, "actuals": ev["actuals"], "unmatched": ev["unmatched"]})

    # timers 更新
    last_post = today_msgs[-1]
    eow = any(observe.classify_event(m["text"]).get("type") == "eow" for m in today_msgs)
    t["last_post_ts"] = last_post["ts_float"]
    t["last_post_dt"] = last_post["datetime"]
    if eow:
        t["end_of_work_date"] = today
    timers[ch] = t

    # 突合失敗（§3.7）— 新規分だけ findings に（確認は1回・上位で）
    for u in ev["unmatched"]:
        runtime.record_finding("reconcile_fail", {"channel": ch, "detail": u})

    # 表記 Layer1（新着のみ）
    rules = _load_rules()
    n_notation = 0
    for m in today_msgs:
        if m["ts_float"] <= last_processed:
            continue
        for issue in observe.notation_check(m["text"], rules):
            n_notation += 1
            if policy.get("quality_nudges_require_approval", True):
                runtime.record_finding("notation", {
                    "channel": ch, "msg_ts": m["ts"], "msg_dt": m["datetime"],
                    "issue": issue, "excerpt": m["text"][:80]})

    t["last_processed_ts"] = max(m["ts_float"] for m in today_msgs)
    timers[ch] = t
    runtime.save_json("channel_timers.json", timers)

    print(f"[obs-batch] {today}: msgs={len(today_msgs)} 予定={'有' if sched else '無'} "
          f"実測={len(ev['actuals'])} 未突合={len(ev['unmatched'])} 表記候補={n_notation} eow={eow}")


def _load_rules():
    # 本番は用語辞書DBから同期した JSON。ローカル/暫定は fixtures。
    p = Path(os.environ.get("HERMES_NOTATION_RULES")
             or Path(__file__).resolve().parents[5] / "fixtures/notion/notation_rules.json")
    import json
    return json.loads(p.read_text(encoding="utf-8")) if p.exists() else {"terms": [], "acronyms": []}


if __name__ == "__main__":
    main()
