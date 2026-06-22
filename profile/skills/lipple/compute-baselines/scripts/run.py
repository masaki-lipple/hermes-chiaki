#!/usr/bin/env python3
"""compute-baselines（§3.3）: 蓄積した actuals から 種別×案件 / 種別 の相場を再構築。
cron 例: 0 21 * * 1-5 （--no-agent / --script）。決定論・LLM 非起動。
"""
import glob
import json
import os
import sys
from pathlib import Path
sys.path.insert(0, os.environ.get("HERMES_LIB") or str(Path(__file__).resolve().parents[5]))
from lib import observe, runtime  # noqa: E402


def main():
    actuals = []
    for p in sorted(glob.glob(str(runtime.STATE_DIR / "actuals_*.json"))):
        try:
            actuals += json.loads(Path(p).read_text(encoding="utf-8")).get("actuals", [])
        except (json.JSONDecodeError, OSError):
            continue
    bl = observe.compute_baselines(actuals)
    seed = {"_note": "compute-baselines が actuals_*.json から再構築",
            "n_actuals": len(actuals), **bl}
    runtime.save_json("baselines.json", seed)
    kinds = [k for k, v in bl["by_kind"].items() if v]
    print(f"[compute-baselines] n_actuals={len(actuals)} kinds={kinds}")


if __name__ == "__main__":
    main()
