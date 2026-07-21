#!/usr/bin/env python3
"""event-listener動的化＋self-healthのテスト。"""
import json
import os
import sys
import time
from pathlib import Path

SCRATCH = Path(__file__).parent / "state_h"
import shutil; shutil.rmtree(SCRATCH, ignore_errors=True)  # 冪等性=前回の状態を残さない
(SCRATCH / "state").mkdir(parents=True, exist_ok=True)
REPO = "/Users/malus_bot/Claude/Hermes"
if not (SCRATCH / "skills").exists():
    os.symlink(f"{REPO}/profile/skills", SCRATCH / "skills")
os.environ["HERMES_PROFILE_DIR"] = str(SCRATCH)
os.environ["HERMES_LIB"] = REPO
sys.path.insert(0, REPO)

from lib import runtime  # noqa: E402

ok = 0
def check(name, cond):
    global ok
    print(("PASS" if cond else "FAIL"), name)
    if not cond:
        sys.exit(1)
    ok += 1

now = runtime.now_ts()

# ── event-listener: _is_relevant のチャンネル決め打ち撤廃 ──
L = f"{REPO}/profile/skills/lipple/event-listener/scripts/run.py"
gl = {"__file__": L, "__name__": "listener_mod"}
exec(compile(open(L).read(), L, "exec"), gl)
runtime.save_json("pending_approvals.json", {"items": {
    "800.0": {"source_ts": "50.0", "source_channel": "C0BAF91KM5K", "status": "awaiting_completion"}}})
check("relevant: mgmt ruling thread", gl["_is_relevant"](runtime.CH_CHIAKI_MGMT, "800.0"))
check("relevant: a0xx source thread", gl["_is_relevant"]("C0BAF91KM5K", "50.0"))
check("relevant: wrong channel", not gl["_is_relevant"]("C_OTHER", "50.0"))
check("relevant: no thread", not gl["_is_relevant"]("C0BAF91KM5K", ""))

# ── self-health ──
H = f"{REPO}/profile/skills/lipple/self-health/scripts/run.py"
gh = {"__file__": H, "__name__": "health_mod"}
exec(compile(open(H).read(), H, "exec"), gh)

# _covers_full_workday: 点検窓の妥当性
import datetime as dtm
JST = dtm.timezone(dtm.timedelta(hours=9))
def T(y, m, d, hh, mm):
    return dtm.datetime(y, m, d, hh, mm, tzinfo=JST).timestamp()
cov = gh["_covers_full_workday"]
check("window Fri->Mon covers Friday", cov(T(2026, 7, 10, 8, 40), T(2026, 7, 13, 8, 40)))
check("window Sun->Mon covers nothing", not cov(T(2026, 7, 12, 12, 52), T(2026, 7, 13, 8, 40)))
check("window Mon->Tue covers Monday", cov(T(2026, 7, 13, 8, 40), T(2026, 7, 14, 8, 40)))
check("window Sat->Tue skips holiday Mon 7/20", not cov(T(2026, 7, 18, 0, 0), T(2026, 7, 21, 8, 40)))

# _log_missing: 初回=基準づくりのみ
log = SCRATCH / "state" / "cron.log"
log.write_text("old garbage\n")
st = {}
check("log first run no warns", gh["_log_missing"](st, now) == [] and st["log_offset"] == log.stat().st_size)
st["log_checked_ts"] = now - 10 * 86400  # 前回=10日前＝連休(週末+祝日)を挟んでも窓に営業日が丸ごと入る
# 差分に全タグ → 警告なし
with open(log, "a") as f:
    for tag in gh["LOG_EXPECT"].values():
        f.write(f"{tag} something\n")
check("log all present", gh["_log_missing"](st, now) == [])
# 差分に task-follow が無い → 警告
st["log_checked_ts"] = now - 10 * 86400
with open(log, "a") as f:
    for name, tag in gh["LOG_EXPECT"].items():
        if name != "task-follow":
            f.write(f"{tag} something\n")
miss = gh["_log_missing"](st, now)
check("log missing task-follow", len(miss) == 1 and "task-follow" in miss[0])
# 営業日を含まない窓（日曜昼→翌朝）＝痕跡ゼロでも警告しない
st["log_checked_ts"] = T(2026, 7, 12, 12, 52)
with open(log, "a") as f:
    f.write("weekend noise\n")
check("log weekend window skipped", gh["_log_missing"](st, T(2026, 7, 13, 8, 40)) == [])
# ログ縮小（ローテーション）→ 先頭から読み直して落ちない
log.write_text("[intake] tiny\n")
st["log_checked_ts"] = now - 10 * 86400
check("log shrink safe", isinstance(gh["_log_missing"](st, now), list))

# _ledger_stale
runtime.save_json("task_ledger.json", {"updated_at": now, "tasks": {}})
check("ledger fresh", gh["_ledger_stale"](now) == [])
runtime.save_json("task_ledger.json", {"updated_at": now - 10 * 86400, "tasks": {}})
check("ledger stale", len(gh["_ledger_stale"](now)) == 1)

# _swallowed（再設計R1: 突き合わせ先=実行台帳）
disp = SCRATCH / "state" / "exec_ledger.jsonl"
rows = [
    {"id": "CA:10.0", "at": now - 3600, "source": "listener", "ch": "CA", "ts": "10.0",
     "thread_root": "", "owner": "intake", "status": "received"},   # items にある → OK
    {"id": "CB:20.0", "at": now - 3600, "source": "listener", "ch": "CB", "ts": "20.0",
     "thread_root": "", "owner": "intake", "status": "received"},   # cursor が跨ぐ → OK
    {"id": "CC:30.0", "at": now - 3600, "source": "listener", "ch": "CC", "ts": "30.0",
     "thread_root": "", "owner": "intake", "status": "received"},   # 痕跡なし → 警告
    {"id": "CD:40.0", "at": now - 3600, "source": "listener", "ch": "CD", "ts": "40.0",
     "thread_root": "800.0", "owner": "intake", "status": "received"},  # 裁定スレッド → 対象外
    {"id": "CE:60.0", "at": now - 3600, "source": "listener", "ch": "CE", "ts": "60.0",
     "thread_root": "700.0", "owner": "codex", "status": "received"},   # 既読済み → OK
    {"id": "CE:80.0", "at": now - 3600, "source": "listener", "ch": "CE", "ts": "80.0",
     "thread_root": "700.0", "owner": "codex", "status": "received"},   # 未読 → 警告
    {"id": "CF:90.0", "at": now - 30, "source": "listener", "ch": "CF", "ts": "90.0",
     "thread_root": "", "owner": "intake", "status": "received"},     # 直近10分 → 次回へ
    {"id": "CG:95.0", "at": now - 3600, "source": "listener", "ch": "CG", "ts": "95.0",
     "thread_root": "", "owner": "intake", "status": "received"},
    {"id": "CG:95.0", "at": now - 3500, "ch": "CG", "ts": "95.0",
     "owner": "intake", "status": "handled"},  # 台帳に処理記録 → OK（一次判定）
]
disp.write_text("\n".join(json.dumps(r) for r in rows) + "\n")
runtime.save_json("chiaki_intake.json", {"items": {"10.0": {"status": "filed"}}})
runtime.save_json("tuning_cursor.json", {"CB": 25.0, "CC": 5.0})
runtime.save_json("codex_threads.json", {"items": {"700.0": {"last_seen_ts": 70.0}}})
runtime.save_json("pending_approvals.json", {"items": {
    "800.0": {"source_ts": "50.0", "source_channel": "C0BAF91KM5K"}}})
st2 = {}
warns = gh["_swallowed"](st2, now)
check("swallowed: exactly 2 warns", len(warns) == 2)
check("swallowed: CC warned", any("ch=CC" in w for w in warns))
check("swallowed: codex unread warned", any("ts=80.0" in w for w in warns))
check("swallowed: grace deferred", not any("ch=CF" in w for w in warns))
check("swallowed: ledger-handled ok", not any("ch=CG" in w for w in warns))
check("swallowed: cursor saved", abs(st2["dispatch_ts"] - (now - 600)) < 5)
# 2回目＝同じ行を再警告しない
check("swallowed: no rewarns", gh["_swallowed"](st2, now + 60) == [])

print(f"\n{ok} checks passed")
