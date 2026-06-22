#!/usr/bin/env python3
"""chiaki-pdca の下書き素材を当日 state から集約（3行整形は agent）。
出力: JSON {date, plan, n_actuals, kinds, n_findings_new, baselines_kinds}。
"""
import glob
import json
import os
import sys
import time
from pathlib import Path
sys.path.insert(0, os.environ.get("HERMES_LIB") or str(Path(__file__).resolve().parents[5]))
from lib import runtime  # noqa: E402


def main():
    today = time.strftime("%Y-%m-%d", time.gmtime(runtime.now_ts() + 9 * 3600))
    plan = runtime.load_json(f"plan_{today}.json", None)
    actuals = []
    for p in sorted(glob.glob(str(runtime.STATE_DIR / f"actuals_{today}.json"))):
        try:
            actuals = json.loads(Path(p).read_text(encoding="utf-8")).get("actuals", [])
        except (json.JSONDecodeError, OSError):
            pass
    findings = [r for r in runtime.read_jsonl("findings.jsonl") if r.get("status") == "new"]
    bl = runtime.load_json("baselines.json", {})
    out = {
        "date": today,
        "plan_hours": (plan or {}).get("planned_hours_total"),
        "n_actuals_today": len(actuals),
        "kinds_today": sorted({a.get("kind") or "?" for a in actuals}),
        "n_findings_new": len(findings),
        "findings_kinds": sorted({f.get("kind") for f in findings}),
        "baselines_kinds": list((bl.get("by_kind") or {}).keys()),
    }
    print(json.dumps(out, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
