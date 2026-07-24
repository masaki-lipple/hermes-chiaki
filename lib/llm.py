"""軽量 LLM ヘルパ（決定論スクリプトから「文面のゆらぎ」だけを Haiku で生成する用）。
標準ライブラリのみ。ANTHROPIC_API_KEY は launcher が profile .env からロード。
判断は呼ばない——短い文面生成だけ。失敗時は呼び側が固定文へフォールバックする前提。
"""
from __future__ import annotations
import json
import os
import shutil
import subprocess
import time
import urllib.request
from pathlib import Path

MODEL_HAIKU = "claude-haiku-4-5"
MODEL_OPUS = "claude-opus-4-8"  # 判断系（同期時のルール分類など低頻度）
# 中枢（会話・振り分け）用＝ChatGPT サブスク経由の GPT（非公式 llm-openai-via-codex・追加課金なし）。
# 2026-07-02 アカウント切替(m-toda@null.inc・Plus)で gpt-5.5 が開放された（旧 Team アカウントは 5.4 まで）。
# auth は ~/.codex/auth.json。モデル一覧はアカウント/プランで変わる＝不通時は gpt() が Haiku へ自動退避。
MODEL_GPT = "openai-codex/gpt-5.5"
GPT_LABEL = "GPT 5.5"  # 発言末尾のモデル表記用

# 「この処理で実際に使ったモデル」の記録（発言末尾の（GPT 5.4）表記用・戸田要望 2026-07-02）。
# listener はワーカー並列なので threading.local。各ハンドラの処理前に reset_used() すること。
import threading as _threading  # noqa: E402
_last_used = _threading.local()


def reset_used() -> None:
    _last_used.model = ""


def last_used() -> str:
    """直近の呼び出しで実際に文面/判断を作ったモデルの表示名（未使用なら空）。"""
    return getattr(_last_used, "model", "")


def _mark(model_label: str) -> None:
    _last_used.model = model_label


def _caller() -> str:
    """呼び出し元スキル名（R5コスト計測・2026-07-24）。スタックから skills/lipple/<name>/ を探す＝
    cron launcher 経由・listener のスレッド内実行・lib 経由（convo.decide 等）のどれでも特定できる。"""
    try:
        import inspect
        for fr in inspect.stack()[2:]:
            parts = Path(fr.filename).parts
            if "skills" in parts:
                i = parts.index("skills")
                if len(parts) > i + 2:
                    return parts[i + 2] if parts[i + 1] == "lipple" else parts[i + 1]
        import sys
        return Path(sys.argv[0]).stem or "unknown"
    except Exception:
        return "unknown"


def _track(fn: str, model: str, t0: float, ok: bool, n_in: int, n_out: int, note: str = "") -> None:
    """LLM呼び出しの計測（R5コスト計測＝Issue「4. R5 コスト計測」・2026-07-24 戸田GO）。
    llm_usage.jsonl に1呼び出し1行。集計・可視化は skills/lipple/llm-usage。
    計測の失敗で本処理（文面生成・判断）は絶対に止めない。"""
    try:
        from lib import runtime
        runtime.append_jsonl("llm_usage.jsonl", {
            "ts": runtime.now_ts(), "caller": _caller(), "fn": fn, "model": model, "ok": ok,
            "ms": int((time.time() - t0) * 1000), "in": n_in, "out": n_out,
            **({"note": note} if note else {})})
    except Exception:
        pass

# chiaki のトンマナ（SOUL のトンマナ規約に対応。戸田さんの調整時はここと SOUL を更新）
CHIAKI_TONE = (
    "あなたは株式会社Lippleのタスク管理担当『Chiaki AI』。社内Slack向けの短い日本語メッセージを書く。"
    "規約: 絵文字なし。簡潔に短く。冗長な丁寧表現（〜いただけますでしょうか/恐れ入りますが等）は避け、"
    "時々はっきり言い切る（例『進捗報告お願いします！』）。あいさつは基本省いて用件から入る。"
    "付ける場合は必ずひらがな——『おはようございます！』は朝(9〜10時台)のみ、『おつかれさま』も可。"
    "『お疲れ様』『お早う』など漢字のあいさつは使わない。です・ます基調・煽らない・温かく。"
    "毎回すこし言い回しを変える（ゆらぎ）。出力は本文のみ。前置き・引用符・宛名(@)は付けない。1〜2文・短め。"
)

_STYLE_CACHE = None  # None=未ロード / ""=無し（毎プロセス1回だけ読む）


def _load_style() -> str:
    """Style_Hermes Agent_総論 を焼いた style_hermes.md（無ければ空）。env→profile state→repo fixtures。"""
    global _STYLE_CACHE
    if _STYLE_CACHE is not None:
        return _STYLE_CACHE
    cands = []
    if os.environ.get("HERMES_STYLE"):
        cands.append(Path(os.environ["HERMES_STYLE"]))
    if os.environ.get("HERMES_PROFILE_DIR"):
        cands.append(Path(os.environ["HERMES_PROFILE_DIR"]) / "state" / "style_hermes.md")
    root = Path(__file__).resolve().parents[1]
    cands += [root / "profile" / "state" / "style_hermes.md",
              root / "fixtures" / "notion" / "style_hermes.md"]
    _STYLE_CACHE = ""
    for c in cands:
        try:
            if c.exists():
                _STYLE_CACHE = c.read_text(encoding="utf-8").strip()
                break
        except Exception:
            pass
    return _STYLE_CACHE


def _chiaki_system() -> str:
    style = _load_style()
    return CHIAKI_TONE + ("\n\n# スタイル(prose)\n" + style if style else "")


def _call(model: str, user: str, system: str, max_tokens: int, timeout: int = 30) -> str:
    key = os.environ.get("ANTHROPIC_API_KEY")
    if not key:
        raise RuntimeError("ANTHROPIC_API_KEY not set")
    # 大きな system（スタイル焼き込み時）はプロンプトキャッシュで安く繰り返す
    sys_field = ([{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}]
                 if len(system) >= 4000 else system)
    body = {"model": model, "max_tokens": max_tokens, "system": sys_field,
            "messages": [{"role": "user", "content": user}]}
    req = urllib.request.Request(
        "https://api.anthropic.com/v1/messages",
        data=json.dumps(body).encode(),
        headers={"x-api-key": key, "anthropic-version": "2023-06-01",
                 "content-type": "application/json"})
    res = json.load(urllib.request.urlopen(req, timeout=timeout))
    parts = [b.get("text", "") for b in res.get("content", []) if b.get("type") == "text"]
    return "".join(parts).strip()


def haiku(user: str, system: str | None = None, max_tokens: int = 150) -> str:
    """短い文面生成（既定 system＝CHIAKI_TONE＋焼いたスタイル）。判断は呼ばない。"""
    sys_prompt = system if system is not None else _chiaki_system()
    t0 = time.time()
    try:
        out = _call(MODEL_HAIKU, user, sys_prompt, max_tokens)
    except Exception as e:
        _track("haiku", "Haiku 4.5", t0, False, len(user) + len(sys_prompt), 0, type(e).__name__)
        raise
    _mark("Haiku 4.5")
    _track("haiku", "Haiku 4.5", t0, True, len(user) + len(sys_prompt), len(out))
    return out


def opus(user: str, system: str = "", max_tokens: int = 1024) -> str:
    """判断系（同期時のレギュレーション分類など低頻度）。強モデルで信頼性重視。"""
    sys_prompt = system or "あなたは正確な日本語校正・分類アシスタントです。指示に厳密に従う。"
    t0 = time.time()
    try:
        out = _call(MODEL_OPUS, user, sys_prompt, max_tokens, timeout=90)
    except Exception as e:
        _track("opus", "Opus 4.8", t0, False, len(user) + len(sys_prompt), 0, type(e).__name__)
        raise
    _mark("Opus 4.8")
    _track("opus", "Opus 4.8", t0, True, len(user) + len(sys_prompt), len(out))
    return out


def _llm_bin() -> str:
    """`llm` CLI の場所（systemd/cron の PATH に ~/.local/bin が無くても解決）。"""
    return (os.environ.get("HERMES_LLM_BIN")
            or shutil.which("llm")
            or str(Path.home() / ".local/bin/llm"))


def _gpt_raw(user: str, system: str, timeout: int) -> str:
    cmd = [_llm_bin(), "-m", MODEL_GPT, user]
    if system:
        cmd += ["-s", system]
    p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    if p.returncode != 0:
        raise RuntimeError(f"gpt rc={p.returncode}: {(p.stderr or '').strip()[:200]}")
    return (p.stdout or "").strip()


def _note_gpt_fallback(err: str) -> None:
    """GPT 経路が落ちたら日1回だけ #8902 に控え（サイレント劣化にしない・handoff_C の故障検知方針）。"""
    try:
        import datetime as _dt
        from lib import runtime, source
        today = _dt.datetime.now(_dt.timezone(_dt.timedelta(hours=9))).strftime("%Y-%m-%d")
        st = runtime.load_json("gpt_fallback_notice.json", {})
        if st.get("date") == today:
            return
        runtime.save_json("gpt_fallback_notice.json", {"date": today, "err": err[:200]})
        source.post_message(runtime.CH_CHIAKI_MGMT,
                            f"<@{runtime.CHIAKI_SELF}>\n報告：GPTルートの不調\n\n"
                            "ChatGPTサブスク経由のGPT呼び出しが失敗したため、本日はHaikuで代替しています。"
                            "認証(codex login)の期限切れの可能性があります。")
    except Exception:
        pass


def gpt(user: str, system: str | None = None, max_tokens: int = 450, timeout: int = 90) -> str:
    """中枢（会話・振り分け）用。ChatGPT サブスク経由の GPT-5.4 を叩き、失敗時は Haiku へ自動フォールバック
    （高額側でなく安価側への退避＝コスト方針と整合）＋日1回 #8902 に控え。呼び側の扱いは haiku() と同じ。"""
    sys_prompt = system if system is not None else _chiaki_system()
    t0 = time.time()
    try:
        out = _gpt_raw(user, sys_prompt, timeout)
        if out:
            _mark(GPT_LABEL)
            _track("gpt", GPT_LABEL, t0, True, len(user) + len(sys_prompt), len(out))
            return out
        raise RuntimeError("gpt empty response")
    except Exception as e:
        print(f"[llm] gpt failed -> haiku fallback: {type(e).__name__}: {e}")
        _track("gpt", GPT_LABEL, t0, False, len(user) + len(sys_prompt), 0, type(e).__name__)
        _note_gpt_fallback(str(e))
        t1 = time.time()
        out = _call(MODEL_HAIKU, user, sys_prompt, max_tokens)
        _mark("Haiku 4.5・代替")  # GPT不通の退避＝表記で見分けられるように
        _track("haiku", "Haiku 4.5", t1, True, len(user) + len(sys_prompt), len(out), "代替")
        return out
