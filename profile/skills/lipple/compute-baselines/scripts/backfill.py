#!/usr/bin/env python3
"""backfill（Issue「7. backfill」・2026-07-24 戸田「7やる」）: 過去N日の#5035履歴から
actuals_<date>.json を再構成し、相場（baselines）の母数を充実させる。手動実行の一回限り運用
（cron登録なし）。抽出は obs-batch と同一の決定論関数（observe.extract_task_events）＝手法の一貫性。
既存の actuals_*.json は上書きしない（ライブ観測分の温存）。当日は obs-batch の領分＝対象外。
実行後に compute-baselines を回すと適正工数_DBへ反映される。使い方: python3 backfill.py [日数=60]"""
from __future__ import annotations

import datetime as dt
import os
import sys
from pathlib import Path
sys.path.insert(0, os.environ.get("HERMES_LIB") or str(Path(__file__).resolve().parents[5]))
from lib import observe, runtime, source  # noqa: E402

JST = dt.timezone(dt.timedelta(hours=9))


def main() -> None:
    days = 60
    if len(sys.argv) > 1:
        try:
            days = int(sys.argv[1])
        except ValueError:
            pass
    now = runtime.now_ts()
    today = dt.datetime.fromtimestamp(now, JST).strftime("%Y-%m-%d")
    ch = runtime.CH_YU_PDCA
    msgs = source.read_recent(ch, oldest_ts=now - days * 86400, paginate=True, max_pages=40)
    by_date: dict[str, list] = {}
    for m in msgs:
        by_date.setdefault(m["datetime"][:10], []).append(m)
    created = skipped = n_actuals = 0
    for date, dm in sorted(by_date.items()):
        if date >= today:
            continue  # 当日は obs-batch が毎回再生成する領分（二重管理しない）
        if (runtime.STATE_DIR / f"actuals_{date}.json").exists():
            skipped += 1
            continue
        ev = observe.extract_task_events(sorted(dm, key=lambda x: x["ts_float"]))
        observe.refine_actual_kinds(ev["actuals"], dm)
        runtime.save_json(f"actuals_{date}.json",
                          {"date": date, "actuals": ev["actuals"],
                           "unmatched": ev["unmatched"], "backfilled": True})
        created += 1
        n_actuals += len(ev["actuals"])
    print(f"[backfill] 取得={len(msgs)}件/{days}日 生成={created}日分（実測{n_actuals}件） "
          f"既存スキップ={skipped}日分")


if __name__ == "__main__":
    main()
