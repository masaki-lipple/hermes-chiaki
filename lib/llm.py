"""軽量 LLM ヘルパ（決定論スクリプトから「文面のゆらぎ」だけを Haiku で生成する用）。
標準ライブラリのみ。ANTHROPIC_API_KEY は launcher が profile .env からロード。
判断は呼ばない——短い文面生成だけ。失敗時は呼び側が固定文へフォールバックする前提。
"""
from __future__ import annotations
import json
import os
import urllib.request
from pathlib import Path

MODEL_HAIKU = "claude-haiku-4-5"
MODEL_OPUS = "claude-opus-4-8"  # 判断系（同期時のルール分類など低頻度）

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
    return _call(MODEL_HAIKU, user, system if system is not None else _chiaki_system(), max_tokens)


def opus(user: str, system: str = "", max_tokens: int = 1024) -> str:
    """判断系（同期時のレギュレーション分類など低頻度）。強モデルで信頼性重視。"""
    return _call(MODEL_OPUS, user,
                 system or "あなたは正確な日本語校正・分類アシスタントです。指示に厳密に従う。",
                 max_tokens, timeout=90)
