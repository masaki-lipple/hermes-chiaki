#!/usr/bin/env python3
"""メインタスク設定機能（2026-08-07 戸田GO・「Chiaki AIはGPTがあるので自然言語を理解できます」）のテスト。
設計: 書き込みは「メインタスク」relation1列のみ・確認ターンHITL・解決できなければ推測せずURLを
聞き返す・PATCH成立を検証してから宣言・戸田のみ（確認ターンの構造で担保）。"""
import json
import os
import sys
import types
from pathlib import Path

SCRATCH = Path(__file__).parent / "state_mt"
import shutil; shutil.rmtree(SCRATCH, ignore_errors=True)  # 冪等性=前回の状態を残さない
(SCRATCH / "state").mkdir(parents=True, exist_ok=True)
REPO = "/Users/malus_bot/Claude/Hermes"
os.environ["HERMES_PROFILE_DIR"] = str(SCRATCH)
os.environ["HERMES_LIB"] = REPO
sys.path.insert(0, REPO)

fake_llm = types.ModuleType("lib.llm")
fake_llm.gpt = lambda *a, **k: ""
fake_llm.haiku = lambda *a, **k: ""
fake_llm.reset_used = lambda: None
fake_llm.last_used = lambda: ""
sys.modules["lib.llm"] = fake_llm

from lib import convo, notion, runtime, source  # noqa: E402

ok = 0
def check(name, cond):
    global ok
    print(("PASS" if cond else "FAIL"), name)
    if not cond:
        sys.exit(1)
    ok += 1

now = runtime.now_ts()
TASK_ID = "3b3980d4f84081efbafbc8bcd712ba52"
MAIN_ID = "3b6980d4f84081aaaaaaaaaaaaaaaaaa"

# ── notion.set_main_task: 1列限定・成立検証 ──
patched = []
notion.update_page_props = lambda pid, props: patched.append((pid, props)) or True
check("notion writes only メインタスク relation",
      notion.set_main_task(TASK_ID, MAIN_ID) is True
      and patched[-1] == (TASK_ID, {"メインタスク": {"relation": [{"id": MAIN_ID}]}}))
check("notion rejects empty ids", notion.set_main_task("", MAIN_ID) is False and len(patched) == 1)

# ── intake: 初回=確認ターン化（実行しない） ──
R = f"{REPO}/profile/skills/lipple/chiaki-intake/scripts/run.py"
g = {"__file__": R, "__name__": "intake_mod"}
exec(compile(open(R).read(), R, "exec"), g)
posted = []
g["_reply"] = lambda ch, root, text, url="", gate=True: posted.append(text)
runtime.save_json("pending_approvals.json", {"items": {}})
MT_PROP = {"type": "main_task", "要約": "メインタスク設定: 精査→運用支援",
           "task_url": "", "main_url": "", "main_hint": "求人の更新・追加・Indeedの運用支援（2026年08月）"}
convo.decide = lambda ch, root, m, mode=None, extra_facts=None: {
    "action": "set_main_task", "reply": "精査タスクのメインタスクを運用支援に設定します。OKですか？",
    "proposals": [dict(MT_PROP)]}
convo.commit = lambda: None
convo.already_replied = lambda ch, ts: False
items = {}
m1 = {"ts": "20.0", "ts_float": now - 60, "user_id": runtime.TODA,
      "text": "<@U0BCCMPKD54> これのメンタスク設定できる？"}
r = g["_handle_propose"](m1, "CA", "10.0", items)
check("initial -> awaiting confirm (not executed)",
      r == 1 and items["20.0"]["status"] == "awaiting_confirm"
      and items["20.0"]["proposals"][0]["type"] == "main_task"
      and "OKですか？" in posted[-1] and not patched[2:])

# ── confirm OK: スレッドのタスクAI返信からtask解決＋タイトルでmain解決→実行 ──
THREAD = [
    {"ts": "10.0", "user_id": "U09T44VEZM1", "text": "業務内容：Indeedの掲載求人の精査"},
    {"ts": "11.0", "user_id": runtime.GCP_TASK_BOT,
     "text": f"Notionにタスクを追加しました。\n<https://app.notion.com/p/Indeed-{TASK_ID}>"},
]
source.read_thread = lambda ch, root: THREAD
notion.query_database_titles = lambda db: {
    "求人の更新・追加・Indeedの運用支援（2026年08月）": {"id": MAIN_ID, "props": {}},
    "別のタスク": {"id": "x" * 32, "props": {}}}
convo.decide = lambda ch, root, m, mode=None, extra_facts=None: {
    "action": "file", "reply": "設定します！", "proposals": []}
it = items["20.0"]
m2 = {"ts": "21.0", "ts_float": now - 30, "user_id": runtime.TODA, "text": "OK！"}
r = g["_handle_confirm"](it, m2, "CA", "10.0")
check("confirm OK -> relation written (resolved from thread + title)",
      r == 1 and patched[-1] == (TASK_ID, {"メインタスク": {"relation": [{"id": MAIN_ID}]}})
      and it["status"] == "filed" and "設定しました" in posted[-1])
check("execution recorded on item", it["main_task_set"]["task"] == TASK_ID
      and it["main_task_set"]["main"] == MAIN_ID)

# ── 解決できないとき=推測せずURLを聞き返す（awaiting維持） ──
it3 = {"status": "awaiting_confirm", "channel": "CA", "thread_root": "10.0", "permalink": "http://x",
       "mention_text": "x", "propose_count": 1, "last_seen_ts": "30.0",
       "proposals": [{"type": "main_task", "要約": "設定", "main_hint": "存在しない名前"}]}
n_patch = len(patched)
m3 = {"ts": "31.0", "ts_float": now - 20, "user_id": runtime.TODA, "text": "OK"}
r = g["_handle_confirm"](it3, m3, "CA", "10.0")
check("unresolved -> asks URL, stays awaiting", r == 1 and it3["status"] == "awaiting_confirm"
      and "特定できませんでした" in posted[-1] and len(patched) == n_patch)

# 次の返信にNotion URLが貼られたら解決して実行
m4 = {"ts": "32.0", "ts_float": now - 10, "user_id": runtime.TODA,
      "text": f"これ！ https://app.notion.com/p/Task-{MAIN_ID}"}
r = g["_handle_confirm"](it3, m4, "CA", "10.0")
check("URL in follow-up resolves and executes",
      r == 1 and it3["status"] == "filed" and patched[-1][1]["メインタスク"]["relation"][0]["id"] == MAIN_ID)

# ── 書き込み失敗=正直に失敗と言い、awaiting維持（成立確認） ──
notion.update_page_props = lambda pid, props: False
it5 = {"status": "awaiting_confirm", "channel": "CA", "thread_root": "10.0", "permalink": "http://x",
       "mention_text": "x", "propose_count": 1, "last_seen_ts": "40.0",
       "proposals": [{"type": "main_task", "要約": "設定",
                      "main_url": f"https://app.notion.com/p/T-{MAIN_ID}"}]}
r = g["_handle_confirm"](it5, {"ts": "41.0", "ts_float": now - 5, "user_id": runtime.TODA,
                               "text": "OK"}, "CA", "10.0")
check("write failure -> honest + awaiting kept", r == 1 and it5["status"] == "awaiting_confirm"
      and "失敗しました" in posted[-1])

# ── legacy（GPT不通）でも「OK」で実行できる ──
notion.update_page_props = lambda pid, props: patched.append((pid, props)) or True
convo.decide = lambda ch, root, m, mode=None, extra_facts=None: None  # 不通
it6 = {"status": "awaiting_confirm", "channel": "CA", "thread_root": "10.0", "permalink": "http://x",
       "mention_text": "x", "propose_count": 1, "last_seen_ts": "50.0",
       "proposals": [{"type": "main_task", "要約": "設定",
                      "main_url": f"https://app.notion.com/p/T-{MAIN_ID}"}]}
r = g["_handle_confirm"](it6, {"ts": "51.0", "ts_float": now - 2, "user_id": runtime.TODA,
                               "text": "OK"}, "CA", "10.0")
check("legacy OK executes deterministically", r == 1 and it6["status"] == "filed")

# ── Slackスレッドリンクからの解決（メイン側） ──
def read_thread2(ch, root):
    if (ch, root) == ("CB", "1785538842.542629"):
        return [{"ts": root, "user_id": runtime.GCP_TASK_BOT,
                 "text": f"Notionにタスクを追加しました。\n<https://app.notion.com/p/M-{MAIN_ID}>"}]
    return THREAD
source.read_thread = read_thread2
pid = g["_page_id_from_ref"]("https://lipple.slack.com/archives/CB/p1785538842542629")
check("slack link resolves via task-bot notion url", pid == MAIN_ID)

print(f"\n{ok} checks passed")
