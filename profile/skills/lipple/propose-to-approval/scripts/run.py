#!/usr/bin/env python3
"""propose-to-approval（§6・LLM cron／決定論フロー＋Haiku文面）。
findings.jsonl の新規（notation/typo/stall）を #8902 に「提案」として出し、pending に記録。
制御は決定論、文面案だけ Haiku（自己チェック付き）。戸田さんの GO/却下/修正 で調教が回る。
cron: 0 9-19 * * 1-5（--no-agent --script propose.py）。新規が無ければ posted=0。
"""
import json
import os
import re
import sys
from pathlib import Path
sys.path.insert(0, os.environ.get("HERMES_LIB") or str(Path(__file__).resolve().parents[5]))
from lib import runtime, source, observe  # noqa: E402

TEAM = "lipple"  # Slack ワークスペース subdomain（permalink 用）
# stall は提案フローに乗せない（監査確定バグ：対象スレッド・対象者が無く GO しても実行不能＝
# pending がゾンビ化していた。停滞の控えは stall-scan 自身が #8902 に投稿済み。チームへの促し
# 機能を本配線するかは戸田さん判断＝それまで stall finding は提案せず consumed にする）。
KINDS = ("notation", "typo")
KINDJP = {"notation": "表記", "typo": "誤字", "stall": "停滞"}
# 生成文の行頭/空白直後の裸 @名前 は架空メンション（apply-ruling と同じガード。
# プレビュー＝GO時にそのまま投稿される文面なので、ここで無害化しておく）。
_FAKE_MENTION = re.compile(r"(?<!\S)@(?=[^\s<>])")
# 監視チャンネル → (user_id, 表示名)。apply-ruling は user_id で本物の @メンション、
# 提案プレビューは表示名を太字（承認前に対象者へ通知を飛ばさない）。
CH_TARGET = {"C09U4T1BBU0": ("U09T44VEZM1", "Yu Matsunaga")}


def _target(channel: str):
    return CH_TARGET.get(channel, ("", "担当者"))


# 表示名（user_profile はAPIで返らないことが多いためIDマップ・未知IDは「担当者」）
NAMES = {"U9R35H06L": "Masaki Toda", "U09T44VEZM1": "Yu Matsunaga", "U9UA8NQCB": "Risa Nemoto"}


def _context_precheck(f: dict, found: str, suggest: str):
    """柱1（2026-07-07 戸田「文脈よんでほしい」）: 提案化の直前に GPT 5.5 がスレッド全文を読み、
    ①本当に直すべきか ②文脈から見た正しい修正案 ③作者宛の依頼文 を一括判断する。
    宛先はチャンネル固定でなく**投稿の作者**（戸田さんの誤字が松永さんに飛んだ実バグの根治）。
    返り値: None=旧経路（author情報の無い旧finding・LLM不通）／{"drop": 理由}／精査結果 dict。"""
    author = f.get("author") or ""
    root = f.get("thread_root") or f.get("msg_ts") or ""
    if not author:
        return None
    if author in (runtime.CHIAKI_SELF, runtime.GCP_TASK_BOT):
        return {"drop": "bot"}
    try:
        from lib import llm
    except Exception:
        return None
    try:
        thread = source.read_thread(f.get("channel", ""), root)
    except Exception:
        thread = []
    convo = "\n".join(
        f"- {NAMES.get(x.get('user_id'), x.get('user_name') or '参加者')}: {(x.get('text') or '')[:200]}"
        for x in thread[-15:]) or "（スレッドなし）"
    name = NAMES.get(author, "担当者")
    light = author == runtime.TODA
    tone = ("相手は投稿の作者本人で上長の戸田さんなので、依頼調にせず"
            "「〜の誤字でしょうか？よければ直しておいてください！」程度の軽い指摘にする。報告のお願いは書かない。"
            if light else
            "修正が終わったらメンションで報告してほしい旨を一言添える。")
    prompt = (
        "Slackの投稿に表記の検知がありました。スレッドの文脈で精査してください。\n"
        f"スレッドのやりとり（古い順）:\n{convo}\n\n"
        f"検知対象: {name}さんの投稿「{(f.get('excerpt') or '')[:120]}」\n"
        f"検知: 「{found}」→「{suggest}」\n\n"
        "JSON のみで返す: {\"real\": true/false, \"suggest\": \"\", \"request\": \"\"}\n"
        "- real: 本当に修正すべき誤り・表記ずれか。固有名詞・意図的な表現・引用・すでに解決済みの話なら false。\n"
        "- suggest: 文脈から見た正しい修正。検知の修正案が文脈に合わない場合は直す"
        "（例:「正体しました」は招待の話の流れなら「招待しました」が正）。\n"
        f"- request: {name}さんに送る指摘文（です・ます調・感嘆符は全角！・@メンションや太字は書かない・1〜3文）。{tone}"
    )
    out = llm.gpt(prompt, max_tokens=400) or ""
    mm = re.search(r"\{.*\}", out, re.S)
    if not mm:
        return None
    try:
        d = json.loads(mm.group(0))
    except Exception:
        return None
    if d.get("real") is False:
        return {"drop": "context"}
    if not (d.get("request") or "").strip():
        return None
    return {"author": author, "name": name,
            "suggest": (d.get("suggest") or suggest).strip(), "request": d["request"].strip()}


def _permalink(channel: str, ts: str) -> str:
    if not (channel and ts):
        return ""
    return f"https://{TEAM}.slack.com/archives/{channel}/p{ts.replace('.', '')}"


def _rules():
    p = runtime.STATE_DIR / "notation_rules.json"
    return json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}


def _corrections(kind: str, n: int = 3) -> list:
    """過去に戸田さんが文面修正した例（同種・直近n件）。apply-ruling が記録。"""
    rows = [r for r in runtime.read_jsonl("style_corrections.jsonl")
            if r.get("kind") == kind and r.get("corrected")]
    return rows[-n:]


def _draft(f: dict, rules: dict) -> str:
    """松永さんへ送る想定の指摘文面案を Haiku で。失敗時はテンプレ。
    戸田さんの過去の文面修正を few-shot で渡し、言い回しを学習させる（§5 調教）。"""
    iss = f.get("issue", {}) or {}
    found, suggest = iss.get("found", ""), iss.get("suggest", "")
    if f["kind"] == "stall":
        base = f"停滞: {f.get('task', '')}（{'/'.join(f.get('signals', []))}）の確認・対応のお願い。"
    else:
        base = f"{KINDJP[f['kind']]}: 「{found}」→「{suggest}」の修正のお願い。"
    try:
        from lib import llm
        shot = ""
        ex = _corrections(f["kind"])
        if ex:
            lines = "\n".join(f"- 案『{r['original']}』→ 戸田さん採用『{r['corrected']}』" for r in ex)
            shot = ("\n以下は戸田さんが過去に直した例。言い回し・長さ・温度感をこの傾向に寄せる"
                    "（内容は今回の指摘に合わせる。例文の固有名や語はそのまま流用しない）:\n" + lines)
        tn = runtime.load_tuning("propose")  # 戸田さんの口頭調整を反映
        if tn:
            shot += "\n戸田さんの指示（必ず守る）: " + "; ".join(tn)
        prompt = (f"松永さんへ送る指摘の文面案を書いてください。内容: {base} "
                  f"対象報告の抜粋: {f.get('excerpt', '')[:60]}。理由を一言添える。"
                  f"最後に『修正したらメンションで報告ください。』の主旨を必ず一文添える。"
                  f"宛名(@)は付けず本文だけ・全体2〜3文。" + shot)
        # 注: 提案文は誤例「sns」等を説明上わざと含むので、ここでは自己チェック(apply_notation_fixes)を掛けない。
        # 架空の @名前 だけ無害化（GO 時にこの draft がそのまま #5035 へ投稿されるため）。
        return _FAKE_MENTION.sub("", (llm.haiku(prompt) or base).strip()) or base
    except Exception:
        return base


def main():
    if not runtime.is_jp_workday():
        print("[SILENT] holiday/weekend")  # 祝日に戸田さんへ提案pingしない（cronは曜日しか知らない）
        return
    findings = runtime.read_jsonl("findings.jsonl")
    if not findings:
        print("[propose] no findings")
        return
    rules = _rules()
    pending = runtime.load_json("pending_approvals.json", {"items": {}})
    posted = 0
    for f in findings:
        if f.get("status") != "new":
            continue
        if f.get("kind") == "stall":
            f["status"] = "noted"  # 停滞は stall-scan の #8902 控えで可視化済み＝提案フローに乗せない
            continue
        if f.get("kind") not in KINDS:
            continue
        iss = f.get("issue", {}) or {}
        link = _permalink(f.get("channel", ""), f.get("msg_ts", ""))
        found = iss.get("found", f.get("task", ""))
        suggest = iss.get("suggest", "")
        pc = _context_precheck(f, found, suggest)
        if pc and pc.get("drop"):
            f["status"] = "rejected_context"  # 文脈精査で棄却（bot投稿・誤検知）＝提案しない
            continue
        if pc:
            tgt_id, tgt_name = pc["author"], pc["name"]
            draft, suggest = pc["request"], pc["suggest"]
        else:  # 旧finding・LLM不通＝従来経路（チャンネル固定宛先+Haiku文面）
            tgt_id, tgt_name = _target(f.get("channel", ""))
            draft = _draft(f, rules)
        kenchi = f"{found} → {suggest}" if suggest else found
        author_note = f"（{tgt_name}さんの投稿）" if pc else ""
        proposal = (
            f"<@{runtime.TODA}>\n"
            f"提案：{KINDJP[f['kind']]}\n"
            f"対象：{link or f.get('channel', '')}{author_note}\n"
            f"検知：{kenchi}\n\n"
            f"```\n"               # 文面プレビューはコードブロック（メンション/装飾が発火しない＝松永さんに通知も飛ばない）
            f"{tgt_name}\n"
            f"{draft}\n"
            f"```\n"
            f"このスレッドに GO・却下・文面修正 の指示をお願いします。"
        )
        res = source.post_message(runtime.CH_CHIAKI_MGMT, proposal)
        ts = res.get("ts") if isinstance(res, dict) else None
        if ts:
            pending.setdefault("items", {})[ts] = {
                "finding_kind": f["kind"], "source_channel": f.get("channel"),
                "source_ts": f.get("msg_ts"), "draft": draft,
                "target_user_id": tgt_id, "target_name": tgt_name,
                "verify_found": found,  # 完了検証用：修正後メッセージにこの語が残っていたら未修正
                "status": "pending"}
            f["status"] = "proposed"  # 投稿成功時のみ proposed。失敗は new のまま次回再試行（無音失敗禁止）
            posted += 1
        else:
            print(f"[propose] post failed, leave status=new: kind={f['kind']} ch={f.get('channel')}")
    changed = posted or any(f.get("status") in ("noted", "rejected_context") for f in findings)
    if posted:
        runtime.save_json("pending_approvals.json", pending)  # pending を先に＝提案がGO不能になる黒穴を作らない
    if changed:
        # 書き戻し直前に再読込し、実行中に他スキル(obs-batch/stall-scan)が追記した行を温存（監査確定バグ：
        # 同時刻cronのappendを全書き戻しで物理消去していた）。temp+replaceでアトミックに。
        current = runtime.read_jsonl("findings.jsonl")
        seen = {(r.get("ts"), r.get("kind"), r.get("msg_ts")) for r in findings}
        extra = [r for r in current if (r.get("ts"), r.get("kind"), r.get("msg_ts")) not in seen]
        p = runtime.STATE_DIR / "findings.jsonl"
        tmp = p.with_name(p.name + f".{os.getpid()}.tmp")
        with open(tmp, "w", encoding="utf-8") as fh:
            for r in findings + extra:
                fh.write(json.dumps(r, ensure_ascii=False) + "\n")
        os.replace(tmp, p)
    print(f"[propose] posted={posted}")


if __name__ == "__main__":
    main()
