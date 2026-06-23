"""プロファイル状態の読み書き・パス解決・findings/proposal 補助。

state/*.json は本番(box)で skill scripts が読み書きする運用状態。
時刻は実時間でよい（cron 実行）。Workflow スクリプトではないので Date 制限なし。
"""
from __future__ import annotations
import json
import os
import time
from pathlib import Path

# profile dir: env 優先（box: ~/.hermes/profiles/management）、無ければリポジトリの profile/
PROFILE_DIR = Path(os.environ.get("HERMES_PROFILE_DIR")
                   or Path(__file__).resolve().parents[1] / "profile")
STATE_DIR = PROFILE_DIR / "state"

# 監視/発信チャンネル
CH_YU_PDCA = "C09U4T1BBU0"
CH_NICHIJI = "C045C1ZBX26"
CH_CHIAKI_PDCA = "C0BC6PPG013"
CH_CHIAKI_MGMT = "C0BCE19BN2G"
TODA = "U9R35H06L"
GCP_TASK_BOT = "U0BBZ3B3UNS"
CHIAKI_SELF = "U0BCCMPKD54"  # 新Bot「Chiaki AI」の user_id。処理・独り言はこれでセルフメンション


def now_ts() -> float:
    return time.time()


def _path(name: str) -> Path:
    return STATE_DIR / name


def load_json(name: str, default=None):
    p = _path(name)
    if not p.exists():
        return default if default is not None else {}
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return default if default is not None else {}


def save_json(name: str, data) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    _path(name).write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def append_jsonl(name: str, row: dict) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    with open(_path(name), "a", encoding="utf-8") as f:
        f.write(json.dumps(row, ensure_ascii=False) + "\n")


def read_jsonl(name: str) -> list[dict]:
    p = _path(name)
    if not p.exists():
        return []
    return [json.loads(ln) for ln in p.read_text(encoding="utf-8").splitlines() if ln.strip()]


def load_policy() -> dict:
    return load_json("policy.json", {
        "quality_nudges_require_approval": True,
        "stall_nudge_wording_require_approval": True,
        "notion_writes_require_approval": True,
    })


def record_finding(kind: str, payload: dict) -> None:
    """承認が要る判断候補を findings キューに積む（propose-to-approval が #8902 へ出す）。"""
    append_jsonl("findings.jsonl", {"ts": now_ts(), "kind": kind, "status": "new", **payload})


def load_tuning(skill: str, n: int = 6) -> list:
    """戸田さんが #8902 で与えた調整指示（chiaki-tuning が tuning.json に蓄積）。
    指定 skill＋general の直近 n 件を返す。各生成（silence/pdca/propose）が文面に反映する。"""
    t = load_json("tuning.json", {})
    items = list(t.get(skill, [])) + list(t.get("general", []))
    return [d.get("directive", d) if isinstance(d, dict) else d for d in items][-n:]


_PUNCT_END = "。！？!?、，…「」『』（）()：:〜・"


def ensure_punct(text: str) -> str:
    """各文末に句読点「。」を確実に付ける（メンション/チャンネル単独行・コードブロック内・空行は除外）。"""
    out, in_code = [], False
    for ln in (text or "").split("\n"):
        s = ln.rstrip()
        t2 = s.strip()
        if t2.startswith("```"):
            in_code = not in_code
            out.append(s)
            continue
        if in_code or not t2:
            out.append(s)
            continue
        if (t2.startswith("<@") or t2.startswith("<!")) and t2.endswith(">") and " " not in t2:
            out.append(s)  # メンション/チャンネル単独行
            continue
        if "://" in t2:
            out.append(s)  # URL 行
            continue
        if set(t2) <= set("ー—–-＝=・　 "):
            out.append(s)  # 区切り行（ーーーーー等）
            continue
        if "：" in t2 and len(t2.split("：", 1)[0]) <= 8:
            out.append(s)  # 短いラベル行（提案：/対象：/検知：等）は文末扱いしない
            continue
        if s[-1] not in _PUNCT_END:
            s = s + "。"
        out.append(s)
    return "\n".join(out)
