#!/usr/bin/env python3
"""codex-runner: 戸田さん承認済みの修正依頼キュー（codex_queue.jsonl）を Codex（GPT）に
実装させ、ローカルブランチ＋#8902へのレビュー待ち報告まで行う。デプロイはしない
（採用可否・本番反映は Claude Code のレビュー後＝2026-07-03 戸田合意の枠）。

さらに報告スレッドでの対話を受け付ける（段階1・2026-07-03 戸田指示「細かくSlack上で
やりとりして進めたい」）: 戸田さんの返信を GPT で読み、追加指示なら同じブランチで
Codex が続きを実装、質問なら答え、反映依頼なら記録して案内、雑談には自然に応じる。

cron 例: */10 * * * *（listener がスレッド返信で即時起動・cron はバックストップ）。
flock で同時実行1・1起動で Codex 実行は1件だけ。

セキュリティ:
- キュー投入は chiaki-intake の確認ターン（戸田さんの GO）とこの報告スレッド（戸田さんの
  返信のみ読む）経由＋本スキルでも requested_by を再検証（二重ゲート）。
- Codex は作業クローン内の workspace-write サンドボックスで実行。git push はしない
  （VPS に GitHub 書き込み権限を持たせない）。
"""
from __future__ import annotations

import datetime as dt
import fcntl
import json
import os
import re
import subprocess
import sys
from pathlib import Path
sys.path.insert(0, os.environ.get("HERMES_LIB") or str(Path(__file__).resolve().parents[5]))
from lib import runtime, source, observe, notion, convo  # noqa: E402

REPO = os.environ.get("HERMES_CODEX_REPO") or os.path.expanduser("~/src/hermes-chiaki")
WORK = os.environ.get("HERMES_CODEX_WORK") or os.path.expanduser("~/src/hermes-chiaki-codex")
CODEX = os.environ.get("HERMES_CODEX_BIN") or os.path.expanduser("~/.local/bin/codex")
CH = runtime.CH_CHIAKI_MGMT
TIMEOUT_SEC = 1800
DAILY_CAP = 5          # Codex 実行の日次上限（暴走とレビュー渋滞の防止）
THREAD_IDLE_CLOSE = 7 * 86400   # 無活動7日でスレッドを閉じる
THREAD_PURGE = 30 * 86400       # 閉じて30日で台帳から削除


def _git(repo: str, *args: str, check: bool = True) -> str:
    r = subprocess.run(["git", "-C", repo, *args], capture_output=True, text=True, timeout=120)
    if check and r.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)}: {r.stderr.strip()[:300]}")
    return r.stdout.strip()


def _tail(text: str, limit: int = 700) -> str:
    t = (text or "").strip()
    return t if len(t) <= limit else "…" + t[-limit:]


def _brief(item: dict, prev_output: str = "") -> str:
    if item.get("continue_branch"):
        return (
            "あなたは Hermes/chiaki リポジトリの修正役です。リポジトリ直下の AGENTS.md の役割分担に従ってください。\n\n"
            f"## これは進行中の作業の続きです（ブランチ {item['continue_branch']}）\n"
            f"元の依頼: {item.get('summary') or ''}\n"
            + (f"\n## 前回のあなたの報告\n{_tail(prev_output, 900)}\n" if prev_output else "")
            + f"\n## 戸田さんからの追加指示\n{item.get('detail') or ''}\n"
            "\n## 制約\n"
            "- 現在のブランチの変更を活かしたまま、追加指示を反映する。\n"
            "- コード修正と検証（python3 -m py_compile ＋依頼内容に応じたテスト）だけを行う。\n"
            "- git commit / push、デプロイ、Slack・Notion の操作はしない（ランナー側の工程）。\n"
            "\n## 完了時\n"
            "- 最後に「今回の変更点の要約」と「実施した検証と結果」を簡潔に出力する。\n"
            "- 出力は Slack にそのまま貼られる。ファイルへの言及は相対パスの平文で書く"
            "（[表示名](パス) のリンク形式や絶対パスは使わない＝Slack では壊れて表示される）。\n"
        )
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
        "- 出力は Slack にそのまま貼られる。ファイルへの言及は相対パスの平文で書く"
        "（[表示名](パス) のリンク形式や絶対パスは使わない＝Slack では壊れて表示される）。\n"
    )


def _fmt(body: str) -> str:
    # 箇条書き前の空行はじめ投稿の整形は lib/source の出口で全投稿に一律適用される
    b = runtime.ensure_punct(observe.enforce_regulations(body))
    try:
        from lib import llm
        tag = llm.last_used()
        if tag:
            b += f"\n（{tag}）"
    except Exception:
        pass
    return f"<@{runtime.TODA}>\n{b}"


def _post(text: str) -> str:
    """top-level 投稿。返り値は投稿 ts（スレッド台帳のキー）。"""
    r = source.post_message(CH, text)
    return (r or {}).get("ts") or ""


def _reply(thread_ts: str, body: str, ch: str = "") -> None:
    """スレッド返信。ch未指定は#8902（2026-07-13 監査: #8902決め打ちだと#5902等の会話スレッド発の
    依頼で、開始・完了報告が存在しないスレッドts宛てになりSlackが黙ってトップレベル化＝報告の迷子）。"""
    source.post_thread_reply(ch or CH, thread_ts, _fmt(body))


# ── 報告スレッドでの対話（段階1） ─────────────────────────
def _classify_thread_reply(t: dict, text: str) -> dict | None:
    """戸田さんのスレッド返信を分類。返り値 {action, reply, instruction} / 失敗時 None。"""
    try:
        from lib import llm
    except Exception:
        return None
    prompt = (
        "これは Codex（コード修正AI）の作業報告スレッドでの戸田さんの返信です。\n"
        f"作業の要約: {t.get('summary') or ''}\n"
        f"直前のCodexの報告：\n{_tail(t.get('last_output') or '', 500)}\n\n"
        f"戸田さんの返信: {text}\n\n"
        "返信を分類し、JSON のみで返す:\n"
        '{"action": "continue|question|deploy|chat", "reply": "", "instruction": ""}\n'
        "- continue: コードの追加修正・やり直し・改善の指示。instruction に Codex への指示を"
        "具体的に書く（戸田さんの言葉を補って明確に）。reply には着手の一言。\n"
        "- question: この作業への質問（コード変更なし）。reply に文脈を踏まえた答えを書く。\n"
        "- deploy: 本番反映の依頼（「反映して」「デプロイして」「本番に出して」等）。\n"
        "- chat: 了解・お礼・雑談。reply に短く自然な応答（定型文にしない）。\n"
        "reply は です・ます調・1〜3文・感嘆符は！・@メンションは書かない。"
    )
    from lib import llm
    out = llm.gpt(prompt, max_tokens=400) or ""
    m = re.search(r"\{.*\}", out, re.S)
    if not m:
        return None
    try:
        d = json.loads(m.group(0))
    except Exception:
        return None
    return d if d.get("action") in ("continue", "question", "deploy", "chat") else None


def _process_threads() -> None:
    """開いている報告スレッドの戸田さんの新着返信を処理する。"""
    reg = runtime.load_json("codex_threads.json", {"items": {}})
    items = reg.setdefault("items", {})
    now = runtime.now_ts()
    changed = False
    for tts, t in list(items.items()):
        last = float(t.get("last_seen_ts") or 0)
        if t.get("status") == "closed":
            if now - last > THREAD_PURGE:
                del items[tts]
                changed = True
            continue
        if now - last > THREAD_IDLE_CLOSE:
            t["status"] = "closed"
            changed = True
            continue
        tch = t.get("channel") or CH  # スレッドの実チャンネル（#8902決め打ちにしない・2026-07-13）
        try:
            replies = source.read_thread(tch, tts)
        except Exception as e:
            print(f"[codex-runner] read_thread failed {tts}: {e}")
            continue
        new = [m for m in replies
               if m.get("user_id") == runtime.TODA and float(m.get("ts_float") or 0) > last]
        if not new:
            continue
        text = "\n".join((m.get("text") or "") for m in new)
        if convo.already_replied(tch, new[-1].get("ts") or ""):
            # 既に別経路（intake等）がこの発話に返答済み＝二重発話しない（2026-07-13 16:48/16:50 二重の再発防止）
            t["last_seen_ts"] = float(new[-1].get("ts_float") or now)
            changed = True
            print(f"[codex-runner] thread {tts} -> already replied elsewhere")
            continue
        try:
            from lib import llm
            llm.reset_used()
        except Exception:
            pass
        # 判断は会話コア（Phase A+B＝統一文脈パッケージ）。不成立時は従来の分類へフォールバック
        act = None
        cd = convo.decide(tch, tts, {"text": text}, mode="codex_thread")
        if cd:
            amap = {"codex_continue": "continue", "deploy_request": "deploy", "answer": "chat"}
            act = {"action": amap.get(cd["action"], cd["action"]),
                   "reply": cd.get("reply") or "", "instruction": cd.get("instruction") or "",
                   "company": cd.get("company") or {}}
        if not act:
            act = _classify_thread_reply(t, text)
        if not act:
            # 分類不能（LLM全滅など）は last_seen を進めず次回リトライ＝黙殺しない
            print(f"[codex-runner] classify failed {tts}")
            continue
        t["last_seen_ts"] = float(new[-1].get("ts_float") or now)
        changed = True
        if cd:
            convo.commit()  # Phase C: 会話コアの判断を採用＝会話台帳へ
        action = act.get("action")
        reply = (act.get("reply") or "").strip()
        if action == "retract":
            _reply(tts, reply or "失礼しました！さきほどの投稿は誤りでした。", ch=tch)
            print(f"[codex-runner] thread {tts} -> retract")
            continue
        if action == "company_rule":
            c = act.get("company") or {}
            url = notion.create_company_regulation(
                rule=c.get("rule") or "", content=c.get("content") or "",
                category=c.get("category") or "", wrong=c.get("wrong") or "", right=c.get("right") or "")
            _reply(tts, (reply + ("\n" + url if url else "")) if url else
                   "社内レギュレーション_DBへの登録に失敗しました。共有・権限を確認してもらえますか？",
                   ch=tch)
            print(f"[codex-runner] thread {tts} -> company_rule")
            continue
        if action == "continue":
            # 内容行には「今回の指示」を出す（元の件名のままだと報告が実作業とズレて見える）
            runtime.append_jsonl("codex_queue.jsonl", {
                "ts": now, "requested_by": runtime.TODA,
                "summary": f"継続：{(act.get('instruction') or text)[:60]}",
                "detail": act.get("instruction") or text,
                "issue_url": t.get("issue_url") or "",
                "continue_branch": t.get("branch") or "", "thread": tts, "channel": tch})
            _reply(tts, reply or "追加の指示を受け取りました！Codexに続きを任せます。", ch=tch)
            print(f"[codex-runner] thread {tts} -> continue queued")
        elif action == "deploy":
            t["deploy_requested"] = True
            _reply(tts, "本番への反映はClaude Codeのレビューを通してから行う約束にしています。"
                        "レビュー依頼として記録したので、確認でき次第反映します！", ch=tch)
            print(f"[codex-runner] thread {tts} -> deploy requested")
        else:  # question / chat
            if reply:
                _reply(tts, reply, ch=tch)
            print(f"[codex-runner] thread {tts} -> {action}")
    if changed:
        runtime.save_json("codex_threads.json", reg)


# ── Codex 実行 ────────────────────────────────────────
def _run_codex(item: dict, branch: str, prev_output: str = "") -> dict:
    """作業クローンで codex exec。返り値 {ok, output, changed, diffstat, base}。"""
    if not os.path.isdir(WORK):
        subprocess.run(["git", "clone", "-q", REPO, WORK], check=True, timeout=300)
    _git(WORK, "fetch", "-q", "origin")
    if item.get("continue_branch"):
        _git(WORK, "checkout", "-q", branch)   # 既存ブランチの変更を保持したまま続ける
    else:
        _git(WORK, "checkout", "-q", "-B", branch, "origin/main")
        _git(WORK, "reset", "-q", "--hard", "origin/main")
        _git(WORK, "clean", "-qfd")
    base = _git(WORK, "rev-parse", "--short", "HEAD")

    env = dict(os.environ)
    env["PATH"] = os.path.dirname(CODEX) + ":" + env.get("PATH", "")
    try:
        r = subprocess.run(
            [CODEX, "exec", "-s", "workspace-write", "-C", WORK, _brief(item, prev_output)],
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
    if item.get("continue_branch") or changed:
        diffstat = _git(WORK, "diff", "--stat", "origin/main..HEAD", check=False)
    return {"ok": ok, "output": out.strip(), "changed": changed, "diffstat": diffstat, "base": base}


def _register_thread(reg_items: dict, tts: str, item: dict, branch: str, res: dict,
                     ch: str = "") -> None:
    if not tts:
        return
    reg_items[tts] = {
        "branch": branch, "summary": item.get("summary") or "",
        "issue_url": item.get("issue_url") or "",
        "channel": ch or CH,  # 対話・報告の宛先チャンネル（#8902とは限らない）
        # 既読の起点は依頼時刻＝Codex実行中に届いた返信を取りこぼさない
        "status": "open", "last_seen_ts": float(item.get("ts") or runtime.now_ts()),
        "last_output": _tail(res.get("output") or "", 900)}


def main():
    lock = open(runtime.STATE_DIR / "codex_runner.lock", "w")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        print("[codex-runner] already running")
        return

    _process_threads()  # 報告スレッドの対話（継続指示はここでキューに積まれる）

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

    reg = runtime.load_json("codex_threads.json", {"items": {}})
    reg_items = reg.setdefault("items", {})
    cont = item.get("continue_branch") or ""
    branch = cont or f"codex/q{int(float(item.get('ts', 0)))}"
    prev_output = ""
    if cont and item.get("thread") in reg_items:
        prev_output = reg_items[item["thread"]].get("last_output") or ""
    summary = (item.get("summary") or "（無題）").strip()
    # 利用上限中は空実行（30分タイムアウト）せず即案内（12時間で自動再試行・成功で解除）
    quota = runtime.load_json("codex_quota.json", {})
    if quota.get("blocked") and runtime.now_ts() - float(quota.get("detected_ts", 0)) < 12 * 3600:
        st["done"][key] = {"status": "quota", "ts": runtime.now_ts()}
        runtime.save_json("codex_runner.json", st)
        note = (f"<@{runtime.TODA}>\n報告：Codex実装（利用上限）\n内容：{summary}\n\n"
                f"ChatGPTプランのCodex利用上限に到達中のため、実装はClaude Codeが引き受けます"
                f"（Issueは残っています）。" + (f"\n{item['issue_url']}" if item.get("issue_url") else ""))
        if item.get("thread"):
            source.post_thread_reply(item.get("channel") or CH, item["thread"], note)
        else:
            _post(note)
        print("[codex-runner] quota blocked -> skip run")
        return
    # 会話スレッド発の依頼＝進捗も完了もそのスレッドへ（2026-07-03 戸田「進捗を同じスレッド内で報告させて」）。
    # 宛先はキュー項目のchannel（#5902や業務チャンネルの会話発もある＝#8902決め打ちにしない）
    origin_thread = "" if cont else (item.get("thread") or "")
    origin_ch = item.get("channel") or CH
    if origin_thread:
        try:
            from lib import llm
            llm.reset_used()
        except Exception:
            pass
        _reply(origin_thread, "Codexが作業を開始しました。終わったらこのスレッドに報告します。",
               ch=origin_ch)
    print(f"[codex-runner] start {branch}: {summary[:60]}")
    try:
        res = _run_codex(item, branch, prev_output)
    except Exception as e:
        res = {"ok": False, "output": f"ランナー内部エラー: {e}", "changed": False,
               "diffstat": "", "base": "?"}

    st["done"][key] = {"status": "ok" if res["ok"] else "failed",
                       "branch": branch, "changed": res["changed"], "ts": runtime.now_ts()}
    st["days"][today] = st["days"].get(today, 0) + 1
    st["days"] = {d: n for d, n in st["days"].items() if d >= today[:8] + "01"}  # 当月分だけ保持
    runtime.save_json("codex_runner.json", st)
    if res["ok"] and runtime.load_json("codex_quota.json", {}).get("blocked"):
        runtime.save_json("codex_quota.json", {})  # 実行が通った＝上限解除

    if res["ok"] and res["changed"] and item.get("issue_url"):
        # 履歴管理の正本＝Issue_DB（2026-07-08 戸田）: 実装完了で「レビュー待ち」+ブランチを記録。
        # Claude Code のレビューで 採用→完了／不採用→未対応に戻す。失敗しても報告は止めない。
        try:
            notion.update_issue(item["issue_url"], status="レビュー待ち", branch=branch)
        except Exception as e:
            print(f"[codex-runner] issue status update failed: {e}")

    issue_line = f"\n{item['issue_url']}" if item.get("issue_url") else ""
    if res["ok"] and res["changed"]:
        n_files = len(re.findall(r"\|", res["diffstat"])) or "?"
        body = (f"<@{runtime.TODA}>\n報告：Codex実装（レビュー待ち）\n内容：{summary}\n\n"
                f"Codexが実装を終えました。VPSのブランチ{branch}（ベース{res['base']}・"
                f"変更{n_files}ファイル）に変更があります。\n\n"
                f"Codexの報告：\n{_tail(res['output'])}\n\n"
                f"続きの指示・質問はこのスレッドでどうぞ。本番反映はClaude Codeのレビュー後です。{issue_line}")
    elif res["ok"]:
        body = (f"<@{runtime.TODA}>\n報告：Codex実装（変更なし）\n内容：{summary}\n\n"
                f"Codexは修正不要（または対応不可）と判断し、コードは変更されていません。\n\n"
                f"Codexの報告：\n{_tail(res['output'])}\n\n"
                f"続きの指示・質問はこのスレッドでどうぞ。{issue_line}")
    else:
        out_l = (res.get("output") or "").lower()
        if "usage limit" in out_l or "rate limit" in out_l:
            # 生のエラー羅列を貼らず、事象と代替手段を一言で（2026-07-10 実バグ: 上限エラーの垂れ流し）
            # 上限フラグを保存＝会話コアが「Codexは当面不可」を知って正直に話せる+次回以降の空実行を防ぐ
            runtime.save_json("codex_quota.json", {"blocked": True, "detected_ts": runtime.now_ts()})
            body = (f"<@{runtime.TODA}>\n報告：Codex実装（利用上限）\n内容：{summary}\n\n"
                    f"ChatGPTプランのCodex利用上限に達しているため、実装できませんでした。"
                    f"上限が回復するまでは、この依頼はClaude Codeが引き受けます（Issueは残っています）。{issue_line}")
        else:
            body = (f"<@{runtime.TODA}>\n報告：Codex実装（失敗）\n内容：{summary}\n\n"
                    f"Codexの実行が失敗しました。このスレッドで指示をもらえれば再試行します。\n\n"
                    f"{_tail(res['output'])}{issue_line}")

    if cont and item.get("thread"):
        t = reg_items.get(item["thread"])
        tch = item.get("channel") or (t or {}).get("channel") or CH
        source.post_thread_reply(tch, item["thread"], body)
        if t is not None:
            t["last_output"] = _tail(res.get("output") or "", 900)
            # last_seen_ts はここで進めない＝Codex実行中に届いた戸田さんの返信を
            # 既読扱いで飲み込まない（2026-07-03「同じことは起きない？」が黙殺された実バグ）
    elif origin_thread:
        source.post_thread_reply(origin_ch, origin_thread, body)
        _register_thread(reg_items, origin_thread, item, branch, res, ch=origin_ch)
        if res["ok"] and res["changed"]:
            # 完了（レビュー待ち）はトップレベル（#8902＝レビュー待ち一覧）にも改めて報告（2026-07-03 戸田
            # 「進捗はその会話の中で、完了はトップレベルに改めて報告しよう」）。
            # 対話は元スレッドで続けるため、この投稿は台帳に登録しない。
            link = f"https://lipple.slack.com/archives/{origin_ch}/p{origin_thread.replace('.', '')}"
            _post(f"<@{runtime.TODA}>\n報告：Codex実装（レビュー待ち）\n内容：{summary}\n\n"
                  f"会話スレッドで受けた依頼の実装が終わりました。ブランチは{branch}です。\n"
                  f"詳細とやりとりは以下のスレッドにあります。\n{link}")
    else:
        tts = _post(body)
        _register_thread(reg_items, tts, item, branch, res)
    runtime.save_json("codex_threads.json", reg)
    print(f"[codex-runner] done ok={res['ok']} changed={res['changed']}")


if __name__ == "__main__":
    main()
