#!/usr/bin/env python3
"""codex-runner: 戸田さん承認済みの修正依頼キュー（codex_queue.jsonl）を Codex（GPT）に
実装させ、ローカルブランチ＋#8902へのレビュー待ち報告まで行う。デプロイはしない
（採用可否・本番反映は Claude Code のレビュー後＝2026-07-03 戸田合意の枠）。
cron 例: */10 * * * *（--no-agent / --script）。flock で同時実行1・1回の起動で1件だけ処理。

セキュリティ:
- キュー投入は chiaki-intake の確認ターン経由（戸田さんの GO のみ）＋本スキルでも
  requested_by が戸田さんの user ID であることを再検証（二重ゲート）。
- Codex は作業クローン内の workspace-write サンドボックスで実行。git push はしない
  （VPS に GitHub 書き込み権限を持たせない）。
"""
from __future__ import annotations

import datetime as dt
import fcntl
import os
import re
import subprocess
import sys
from pathlib import Path
sys.path.insert(0, os.environ.get("HERMES_LIB") or str(Path(__file__).resolve().parents[5]))
from lib import runtime, source  # noqa: E402

REPO = os.environ.get("HERMES_CODEX_REPO") or os.path.expanduser("~/src/hermes-chiaki")
WORK = os.environ.get("HERMES_CODEX_WORK") or os.path.expanduser("~/src/hermes-chiaki-codex")
CODEX = os.environ.get("HERMES_CODEX_BIN") or os.path.expanduser("~/.local/bin/codex")
TIMEOUT_SEC = 1800
DAILY_CAP = 5  # 暴走防止（レビュー渋滞も防ぐ）


def _git(repo: str, *args: str, check: bool = True) -> str:
    r = subprocess.run(["git", "-C", repo, *args], capture_output=True, text=True, timeout=120)
    if check and r.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)}: {r.stderr.strip()[:300]}")
    return r.stdout.strip()


def _brief(item: dict) -> str:
    return (
        "あなたは Hermes/chiaki リポジトリの修正役です。リポジトリ直下の AGENTS.md の役割分担に従ってください。\n\n"
        f"## 依頼（戸田さん承認済み）\n"
        f"要約: {item.get('summary') or ''}\n"
        f"詳細: {item.get('detail') or ''}\n"
        + (f"Issue: {item.get('issue_url')}\n" if item.get("issue_url") else "")
        + "\n## 制約\n"
        "- コード修正と検証（python3 -m py_compile ＋依頼内容に応じたテスト）だけを行う。\n"
        "- git commit / push、デプロイ、Slack・Notion の操作はしない（ランナー側の工程）。\n"
        "- 修正が不要・不可能と判断した場合はファイルを変えず、理由を出力して終了する。\n"
        "\n## 完了時\n"
        "- 最後に「変更点の要約」と「実施した検証と結果」を簡潔に出力する。\n"
    )


def _post(text: str) -> None:
    source.post_message(runtime.CH_CHIAKI_MGMT, text)


def _run_codex(item: dict, branch: str) -> dict:
    """作業クローンで codex exec。返り値 {ok, output, changed, diffstat, base}。"""
    if not os.path.isdir(WORK):
        subprocess.run(["git", "clone", "-q", REPO, WORK], check=True, timeout=300)
    _git(WORK, "fetch", "-q", "origin")
    _git(WORK, "checkout", "-q", "-B", branch, "origin/main")
    _git(WORK, "reset", "-q", "--hard", "origin/main")
    _git(WORK, "clean", "-qfd")
    base = _git(WORK, "rev-parse", "--short", "HEAD")

    env = dict(os.environ)
    env["PATH"] = os.path.dirname(CODEX) + ":" + env.get("PATH", "")
    try:
        r = subprocess.run(
            [CODEX, "exec", "-s", "workspace-write", "-C", WORK, _brief(item)],
            capture_output=True, text=True, timeout=TIMEOUT_SEC, env=env)
        out = (r.stdout or "") + ("\n" + r.stderr if r.returncode != 0 else "")
        ok = r.returncode == 0
    except subprocess.TimeoutExpired:
        return {"ok": False, "output": f"タイムアウト（{TIMEOUT_SEC // 60}分）", "changed": False,
                "diffstat": "", "base": base}

    changed = bool(_git(WORK, "status", "--porcelain"))
    diffstat = ""
    if changed:
        _git(WORK, "add", "-A")
        _git(WORK, "-c", "user.name=codex-runner", "-c", "user.email=codex@lipple.local",
             "commit", "-q", "-m", f"codex: {(item.get('summary') or '')[:72]}")
        diffstat = _git(WORK, "diff", "--stat", "origin/main..HEAD")
    return {"ok": ok, "output": out.strip(), "changed": changed, "diffstat": diffstat, "base": base}


def _tail(text: str, limit: int = 700) -> str:
    t = (text or "").strip()
    return t if len(t) <= limit else "…" + t[-limit:]


def main():
    lock = open(runtime.STATE_DIR / "codex_runner.lock", "w")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        print("[codex-runner] already running")
        return

    st = runtime.load_json("codex_runner.json", {"done": {}, "days": {}})
    queue = [q for q in runtime.read_jsonl("codex_queue.jsonl")
             if str(q.get("ts")) not in st["done"]]
    if not queue:
        print("[codex-runner] queue empty")
        return

    today = dt.datetime.fromtimestamp(
        runtime.now_ts(), dt.timezone(dt.timedelta(hours=9))).strftime("%Y-%m-%d")
    if st["days"].get(today, 0) >= DAILY_CAP:
        print(f"[codex-runner] daily cap {DAILY_CAP} reached")
        return

    item = queue[0]
    key = str(item.get("ts"))
    # 二重ゲート: intake 側も戸田さん限定だが、キュー改ざん・別経路投入に備えここでも検証
    if item.get("requested_by") != runtime.TODA:
        st["done"][key] = {"status": "rejected", "ts": runtime.now_ts()}
        runtime.save_json("codex_runner.json", st)
        _post(f"<@{runtime.TODA}>\n報告：Codex実行の拒否\n内容：権限のない依頼をスキップしました\n\n"
              f"codex_queue.jsonl に戸田さん以外（{item.get('requested_by')}）を依頼元とする項目が"
              f"あったため、実行せずスキップしました。心当たりがない場合は確認をお願いします！")
        print("[codex-runner] rejected: not TODA")
        return

    branch = f"codex/q{int(float(item.get('ts', 0)))}"
    summary = (item.get("summary") or "（無題）").strip()
    print(f"[codex-runner] start {branch}: {summary[:60]}")
    try:
        res = _run_codex(item, branch)
    except Exception as e:
        res = {"ok": False, "output": f"ランナー内部エラー: {e}", "changed": False,
               "diffstat": "", "base": "?"}

    st["done"][key] = {"status": "ok" if res["ok"] else "failed",
                       "branch": branch, "changed": res["changed"], "ts": runtime.now_ts()}
    st["days"][today] = st["days"].get(today, 0) + 1
    st["days"] = {d: n for d, n in st["days"].items() if d >= today[:8] + "01"}  # 当月分だけ保持
    runtime.save_json("codex_runner.json", st)

    issue_line = f"\n{item['issue_url']}" if item.get("issue_url") else ""
    if res["ok"] and res["changed"]:
        n_files = len(re.findall(r"\|", res["diffstat"])) or "?"
        _post(f"<@{runtime.TODA}>\n報告：Codex実装（レビュー待ち）\n内容：{summary}\n\n"
              f"Codexが実装を終えました。VPSのブランチ{branch}（ベース{res['base']}・"
              f"変更{n_files}ファイル）に変更があります。\n\n"
              f"Codexの報告:\n{_tail(res['output'])}\n\n"
              f"この変更はまだ本番に反映されていません。Claude Codeがレビューして採用可否を判断します。{issue_line}")
    elif res["ok"]:
        _post(f"<@{runtime.TODA}>\n報告：Codex実装（変更なし）\n内容：{summary}\n\n"
              f"Codexは修正不要（または対応不可）と判断し、コードは変更されていません。\n\n"
              f"Codexの報告:\n{_tail(res['output'])}{issue_line}")
    else:
        _post(f"<@{runtime.TODA}>\n報告：Codex実装（失敗）\n内容：{summary}\n\n"
              f"Codexの実行が失敗しました。Claude Codeでの対応に切り替えてください。\n\n"
              f"{_tail(res['output'])}{issue_line}")
    print(f"[codex-runner] done ok={res['ok']} changed={res['changed']}")


if __name__ == "__main__":
    main()
