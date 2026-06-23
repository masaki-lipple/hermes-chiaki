#!/usr/bin/env python3
"""propose-to-approval（§6・LLM cron／決定論フロー＋Haiku文面）。
findings.jsonl の新規（notation/typo/stall）を #8902 に「提案」として出し、pending に記録。
制御は決定論、文面案だけ Haiku（自己チェック付き）。戸田さんの GO/却下/修正 で調教が回る。
cron: 0 9-19 * * 1-5（--no-agent --script propose.py）。新規が無ければ posted=0。
"""
import json
import os
import sys
from pathlib import Path
sys.path.insert(0, os.environ.get("HERMES_LIB") or str(Path(__file__).resolve().parents[5]))
from lib import runtime, source, observe  # noqa: E402

TEAM = "lipple"  # Slack ワークスペース subdomain（permalink 用）
KINDS = ("notation", "typo", "stall")
KINDJP = {"notation": "表記", "typo": "誤字", "stall": "停滞"}


def _permalink(channel: str, ts: str) -> str:
    if not (channel and ts):
        return ""
    return f"https://{TEAM}.slack.com/archives/{channel}/p{ts.replace('.', '')}"


def _rules():
    p = runtime.STATE_DIR / "notation_rules.json"
    return json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}


def _draft(f: dict, rules: dict) -> str:
    """松永さんへ送る想定の指摘文面案を Haiku で。失敗時はテンプレ。自己チェックで規約準拠。"""
    iss = f.get("issue", {}) or {}
    found, suggest = iss.get("found", ""), iss.get("suggest", "")
    if f["kind"] == "stall":
        base = f"停滞: {f.get('task', '')}（{'/'.join(f.get('signals', []))}）の確認・対応のお願い。"
    else:
        base = f"{KINDJP[f['kind']]}: 「{found}」→「{suggest}」の修正のお願い。"
    try:
        from lib import llm
        prompt = (f"松永さんへ送る指摘の文面案を1〜2文で書いてください。内容: {base} "
                  f"対象報告の抜粋: {f.get('excerpt', '')[:60]}。理由を一言添える。宛名(@)は付けず本文だけ。")
        body = llm.haiku(prompt) or base
        body, _ = observe.apply_notation_fixes(body, rules)
        return body
    except Exception:
        return base


def main():
    findings = runtime.read_jsonl("findings.jsonl")
    if not findings:
        print("[propose] no findings")
        return
    rules = _rules()
    pending = runtime.load_json("pending_approvals.json", {"items": {}})
    posted = 0
    for f in findings:
        if f.get("status") != "new" or f.get("kind") not in KINDS:
            continue
        draft = _draft(f, rules)
        iss = f.get("issue", {}) or {}
        link = _permalink(f.get("channel", ""), f.get("msg_ts", ""))
        proposal = (
            f"<@{runtime.TODA}> 【提案 / {KINDJP[f['kind']]}】\n"
            f"対象: {link or f.get('channel', '')}　{f.get('msg_dt', '')}\n"
            f"検知: {iss.get('found', f.get('task', ''))}"
            + (f" → {iss.get('suggest', '')}" if iss.get('suggest') else "") + "\n"
            f"文面案（松永さんへ）: {draft}\n"
            f"→ このスレッドに GO / 却下 / 文面修正 でお願いします。"
        )
        res = source.post_message(runtime.CH_CHIAKI_MGMT, proposal)
        ts = res.get("ts") if isinstance(res, dict) else None
        if ts:
            pending.setdefault("items", {})[ts] = {
                "finding_kind": f["kind"], "source_channel": f.get("channel"),
                "source_ts": f.get("msg_ts"), "draft": draft, "status": "pending"}
        f["status"] = "proposed"
        posted += 1
    if posted:
        with open(runtime.STATE_DIR / "findings.jsonl", "w", encoding="utf-8") as fh:
            for r in findings:
                fh.write(json.dumps(r, ensure_ascii=False) + "\n")
        runtime.save_json("pending_approvals.json", pending)
    print(f"[propose] posted={posted}")


if __name__ == "__main__":
    main()
