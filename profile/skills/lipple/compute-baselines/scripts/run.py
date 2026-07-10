#!/usr/bin/env python3
"""compute-baselines（§3.3）: 蓄積した actuals から 種別×案件 / 種別 の相場を再構築し、
適正工数_DB の実測列（下限/上限/中央値/回数/最終更新/乖離）を毎晩反映する。
cron 例: 0 21 * * 1-5 （--no-agent / --script）。決定論・LLM 非起動。
"""
import datetime as dt
import glob
import json
import os
import sys
import time
from pathlib import Path
sys.path.insert(0, os.environ.get("HERMES_LIB") or str(Path(__file__).resolve().parents[5]))
from lib import observe, runtime, notion  # noqa: E402


def _m(h):
    """時間→分の整数（DBは分の整数。丸め）。"""
    return int(round((h or 0) * 60))


def _query_rows_with_retry(db_id: str, waits=(30, 60)) -> dict:
    """query が空なら間隔を空けて再試行して返す（2026-07-09 Notion API 一時エラーで
    実測反映が1晩丸ごとスキップした実例＝翌日の照会は正常）。notion._api は失敗時 None
    ＝「本当に空のDB」と「一時障害」を呼び側で区別できないため、夜間バッチはここで粘る。
    恒常的な未共有/権限切れでも余計に待つのは最大90秒（cron なので許容）。"""
    rows = notion.query_database_titles(db_id)
    for i, w in enumerate(waits):
        if rows:
            break
        print(f"[compute-baselines] DB query empty -> {w}s待って再試行({i + 1}/{len(waits)})")
        time.sleep(w)
        rows = notion.query_database_titles(db_id)
    return rows


def _push_to_notion(bl: dict) -> None:
    """適正工数_DB の既存行に実測を反映（2026-07-07 戸田「全然更新されていない」＝反映ジョブが未実装だった）。
    行の新規作成はしない（ページ新規作成は戸田さんの許可制）。DBに無い種別はログのみ。
    乖離は 適正工数（下限/上限）が両方入っている行だけ計算（未設定なら触らない）。"""
    if not notion._token():
        print("[compute-baselines] no notion token -> skip DB push")
        return
    rows = _query_rows_with_retry(notion.KOUSU_DB)
    if not rows:
        print("[compute-baselines] DB query empty -> skip (再試行後も空。共有/権限またはNotion障害を確認)")
        return
    today = dt.datetime.now(dt.timezone(dt.timedelta(hours=9))).strftime("%Y-%m-%d")
    by_kind = bl.get("by_kind", {})
    updated, unmatched = 0, []
    for kind, v in by_kind.items():
        if not v:
            continue
        row = rows.get(kind)
        if not row:
            # 「（対象）」付きの分類済み種別で件数が溜まっているものは行追加の候補（作成は許可制）
            if "（" in kind and v.get("n", 0) >= 2:
                unmatched.append(f"{kind}(n={v['n']})")
            continue
        props = {
            "実測（下限）": {"number": _m(v["min_h"])},
            "実測（上限）": {"number": _m(v["max_h"])},
            "実測中央値": {"number": _m(v["median_h"])},
            "実測回数": {"number": v["n"]},
            "最終更新": {"date": {"start": today}},
        }
        cur = row["props"]
        lo = (cur.get("適正工数（下限）") or {}).get("number")
        hi = (cur.get("適正工数（上限）") or {}).get("number")
        if lo is not None and hi is not None:  # 適正工数が両方あるときだけ乖離を判定
            med = _m(v["median_h"])
            props["乖離"] = {"checkbox": med < lo or med > hi}
        if notion.update_page_props(row["id"], props):
            updated += 1
    print(f"[compute-baselines] DB更新={updated}行"
          + (f" / 行未作成の種別={unmatched}" if unmatched else ""))


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
    try:
        _push_to_notion(bl)  # Notion 反映の失敗で baseline 計算を巻き込まない
    except Exception as e:
        print(f"[compute-baselines] notion push failed: {e}")


if __name__ == "__main__":
    main()
