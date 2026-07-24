#!/usr/bin/env python3
"""事実接地の再発防止テスト（2026-07-23 戸田「結果的に嘘をつかれている。再発防止をしたい」）。
実バグ: GO実行で対象スレッドへ投稿済みなのに「実際に投稿されていないよね？」に同調し、
正しい完了報告を取り消し・「投稿はしておらず」という虚偽の訂正を出した。
①convo.thread_facts=投稿の実在を時刻・リンク付きで明示 ②_handle_retract=事実照合ゲート
（矛盾する指摘では記録を書き換えない・判定不能も書き換えない側に倒す）。"""
import json
import os
import sys
import types
from pathlib import Path

SCRATCH = Path(__file__).parent / "state_fg"
import shutil; shutil.rmtree(SCRATCH, ignore_errors=True)  # 冪等性=前回の状態を残さない
(SCRATCH / "state").mkdir(parents=True, exist_ok=True)
REPO = "/Users/malus_bot/Claude/Hermes"
os.environ["HERMES_PROFILE_DIR"] = str(SCRATCH)
os.environ["HERMES_LIB"] = REPO
sys.path.insert(0, REPO)

fake_llm = types.ModuleType("lib.llm")
fake_llm.gpt = lambda *a, **k: ""
fake_llm.reset_used = lambda: None
fake_llm.last_used = lambda: "GPT 5.5"
sys.modules["lib.llm"] = fake_llm

from lib import convo, runtime, source  # noqa: E402

ok = 0
def check(name, cond):
    global ok
    print(("PASS" if cond else "FAIL"), name)
    if not cond:
        sys.exit(1)
    ok += 1

MGMT = runtime.CH_CHIAKI_MGMT

# ── ① thread_facts: 投稿の実在が時刻・リンク付きで事実に出る ──
runtime.save_json("pending_approvals.json", {"items": {"900.0": {
    "finding_kind": "typo", "status": "awaiting_completion",
    "source_channel": "CSRC", "source_ts": "5.0", "target_user_id": "U09T44VEZM1",
    "target_name": "Yu Matsunaga", "draft": "修正をお願いします！",
    "final_text": "修正をお願いします！", "nudge_ts": "1784776952.484839"}}})
facts = "\n".join(convo.thread_facts(MGMT, "900.0"))
check("① posted fact with link",
      "実際に投稿済み" in facts and "archives/CSRC/p1784776952484839" in facts)
check("① posted fact forbids agreeing with false premise",
      "事実に反する" in facts and "同調せず" in facts)

runtime.save_json("pending_approvals.json", {"items": {"901.0": {
    "finding_kind": "typo", "status": "pending", "source_channel": "CSRC",
    "source_ts": "5.0", "target_user_id": "U09T44VEZM1", "draft": "x"}}})
facts = "\n".join(convo.thread_facts(MGMT, "901.0"))
check("① pending -> not yet posted fact", "まだ何も投稿していない" in facts)

runtime.save_json("pending_approvals.json", {"items": {"902.0": {
    "finding_kind": "typo", "status": "already_fixed", "source_channel": "CSRC",
    "source_ts": "5.0", "target_user_id": "U09T44VEZM1", "draft": "x"}}})
facts = "\n".join(convo.thread_facts(MGMT, "902.0"))
check("① closed without posting fact", "投稿はしていない" in facts)

# 対象投稿の「現在の実物」の接地（2026-07-24 実バグ:「以」誤検知に「切れてる？」と聞かれ、
# 実物を見ずに検知結果を反復＋実行主体の無い「後で確認します」を約束した）
source.read_thread = lambda ch, root: [{"ts": root, "user_id": "U09T44VEZM1",
                                        "text": "次回の出勤日は以下です。\n2026年07月27日（月）09:00-18:00 出勤"}]
runtime.save_json("pending_approvals.json", {"items": {"903.0": {
    "finding_kind": "typo", "status": "pending", "source_channel": "CSRC", "source_ts": "5.0",
    "target_user_id": "U09T44VEZM1", "draft": "x", "verify_found": "以"}}})
facts = "\n".join(convo.thread_facts(MGMT, "903.0"))
check("① target live text grounded", "次回の出勤日は以下です" in facts and "実物だけを根拠" in facts)
check("① found still present noted", "「以」" in facts and "まだ残っている" in facts)
runtime.save_json("pending_approvals.json", {"items": {"904.0": {
    "finding_kind": "typo", "status": "pending", "source_channel": "CSRC", "source_ts": "5.0",
    "target_user_id": "U09T44VEZM1", "draft": "x", "verify_found": "ですです"}}})
facts = "\n".join(convo.thread_facts(MGMT, "904.0"))
check("① found gone noted", "もう存在しない" in facts)

# ── ② _handle_retract: 事実照合ゲート ──
R = f"{REPO}/profile/skills/lipple/chiaki-intake/scripts/run.py"
g = {"__file__": R, "__name__": "intake_mod"}
exec(compile(open(R).read(), R, "exec"), g)

edits, replies = [], []
source.update_message = lambda ch, ts, text: edits.append((ch, ts, text))
g["_reply"] = lambda ch, root, text: replies.append((ch, root, text))
THREAD = [
    {"ts": "900.0", "user_id": runtime.CHIAKI_SELF, "user_name": "Chiaki AI",
     "text": "<@U9R35H06L>\n提案：誤字"},
    {"ts": "905.0", "user_id": runtime.CHIAKI_SELF, "user_name": "Chiaki AI",
     "text": "<@U9R35H06L>\n対象の投稿が修正されていることを確認しました。"},
]
source.read_thread = lambda ch, root: THREAD

def pend_item():
    return {"items": {"900.0": {
        "finding_kind": "typo", "status": "awaiting_completion",
        "source_channel": "CSRC", "source_ts": "5.0", "target_user_id": "U09T44VEZM1",
        "target_name": "Yu Matsunaga", "draft": "修正をお願いします！",
        "final_text": "修正をお願いします！", "nudge_ts": "952.0"}}}

# (a) 指摘が記録と矛盾（mistake=false）→ 何も書き換えず事実を提示
runtime.save_json("pending_approvals.json", pend_item())
fake_llm.gpt = lambda *a, **k: json.dumps(
    {"mistake": False, "reply": "実際には対象スレッドへ投稿済みです。リンクはこちらです。"},
    ensure_ascii=False)
r = g["_handle_retract"]({"text": "実際に投稿されていないよね？"}, MGMT, "900.0")
pend = runtime.load_json("pending_approvals.json", {})
check("② contradiction -> no annotation", r == 1 and not edits)
check("② contradiction -> ruling not retracted",
      pend["items"]["900.0"]["status"] == "awaiting_completion")
check("② contradiction -> facts presented", "投稿済み" in replies[-1][2])

# (b) プロンプトに記録（投稿済みリンク）が入っている
seen_prompt = {}
def capture_gpt(prompt, **k):
    seen_prompt["p"] = prompt
    return json.dumps({"mistake": False, "reply": "事実を伝えます。"}, ensure_ascii=False)
fake_llm.gpt = capture_gpt
g["_handle_retract"]({"text": "投稿されていないよね？"}, MGMT, "900.0")
check("② prompt grounded with posted-link fact",
      "実際に投稿済み" in seen_prompt["p"] and "システム記録" in seen_prompt["p"])

# (c) 本当に誤り（mistake=true・対象スレッドでの指摘）→ 従来どおり注記＋retracted＋謝罪
edits.clear(); replies.clear()
runtime.save_json("pending_approvals.json", pend_item())
TGT_THREAD = [
    {"ts": "5.0", "user_id": "U09T44VEZM1", "user_name": "Yu Matsunaga", "text": "本文"},
    {"ts": "952.0", "user_id": runtime.CHIAKI_SELF, "user_name": "Chiaki AI",
     "text": "<@U09T44VEZM1>\n修正をお願いします！"},
]
source.read_thread = lambda ch, root: TGT_THREAD if root == "5.0" else THREAD
fake_llm.gpt = lambda *a, **k: json.dumps(
    {"mistake": True, "reply": "失礼しました！宛先を間違えていました。"}, ensure_ascii=False)
r = g["_handle_retract"]({"text": "これ宛先違うよ"}, "CSRC", "5.0")
pend = runtime.load_json("pending_approvals.json", {})
check("② real mistake -> annotated", r == 1 and len(edits) == 1
      and "※この投稿は誤りでした" in edits[0][2] and edits[0][1] == "952.0")
check("② real mistake -> ruling retracted", pend["items"]["900.0"]["status"] == "retracted")
check("② real mistake -> apology", "失礼しました" in replies[-1][2])

# (d) 判定不能（LLM不通）→ 書き換えない側に倒して正直に確認
edits.clear(); replies.clear()
runtime.save_json("pending_approvals.json", pend_item())
def gpt_down(*a, **k):
    raise RuntimeError("529")
fake_llm.gpt = gpt_down
r = g["_handle_retract"]({"text": "さっきの誤りだよ"}, MGMT, "900.0")
pend = runtime.load_json("pending_approvals.json", {})
check("② LLM down -> nothing rewritten", r == 1 and not edits
      and pend["items"]["900.0"]["status"] == "awaiting_completion")
check("② LLM down -> honest holdback reply", "保留" in replies[-1][2])

print(f"\n{ok} checks passed")
