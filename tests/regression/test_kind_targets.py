#!/usr/bin/env python3
"""kind_targets（案件名→対象の振り直し・2026-07-15）のテスト。"""
import os, sys
from pathlib import Path
SCRATCH = Path(__file__).parent / "state_kt"
import shutil; shutil.rmtree(SCRATCH, ignore_errors=True)  # 冪等性=前回の状態を残さない
(SCRATCH / "state").mkdir(parents=True, exist_ok=True)
os.environ["HERMES_PROFILE_DIR"] = str(SCRATCH)
os.environ["HERMES_LIB"] = "/Users/malus_bot/Claude/Hermes"
sys.path.insert(0, "/Users/malus_bot/Claude/Hermes")
from lib import runtime  # noqa: E402

ok = 0
def check(name, cond):
    global ok
    print(("PASS" if cond else "FAIL"), name)
    if not cond: sys.exit(1)
    ok += 1

R = "/Users/malus_bot/Claude/Hermes/profile/skills/lipple/compute-baselines/scripts/run.py"
g = {"__file__": R, "__name__": "cb_mod"}
exec(compile(open(R).read(), R, "exec"), g)

runtime.save_json("kind_targets.json", {"BUZZ GOLF": "BUZZ GOLF", "白岩工業": "求人ページ"})
acts = [
    {"kind": "流し込み", "name": "BUZZ GOLF", "kind2": "流し込み（コンテンツ）"},
    {"kind": "流し込み", "name": "白岩工業①"},       # 前方一致＝枝番吸収
    {"kind": "流し込み（コンテンツ）", "name": "白岩工業"},  # 対象付きは触らない
    {"kind": "修正", "name": "白岩工業②"},           # 種別を問わず対象を付ける
    {"kind": "流し込み", "name": "未知案件"},          # 辞書外＝無印のまま
    {"kind": "", "name": "白岩工業"},
]
n = g["_apply_kind_targets"](acts)
check("remap count", n == 3)
check("buzz golf", acts[0]["kind"] == "流し込み（BUZZ GOLF）")
check("kind2 overridden", acts[0]["kind2"] == "流し込み（BUZZ GOLF）")
check("prefix match", acts[1]["kind"] == "流し込み（求人ページ）")
check("targeted untouched", acts[2]["kind"] == "流し込み（コンテンツ）")
check("other kind also mapped", acts[3]["kind"] == "修正（求人ページ）")
check("unknown untouched", acts[4]["kind"] == "流し込み")
runtime.save_json("kind_targets.json", {})
check("empty dict no-op", g["_apply_kind_targets"]([{"kind": "流し込み", "name": "X"}]) == 0)

print(f"\n{ok} checks passed")
