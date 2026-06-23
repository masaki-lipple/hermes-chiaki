#!/usr/bin/env python3
"""event-listener: Slack Socket Mode でスレッド返信を即時受信し、決定論 apply-ruling を起動する。

会話エージェントは一切使わない（安全）。受信イベントが pending に関係するスレッド
（#8902 の提案スレッド／#5035・#a027 の対象スレッド）への返信のときだけ apply-ruling を即実行。
crontab の apply-ruling とは flock で排他（二重処理なし）。常駐デーモン（systemd user service）。
依存: slack_sdk（builtin Socket Mode＝stdlib のみで websocket 接続）。
"""
import fcntl
import os
import sys
import threading
from pathlib import Path
sys.path.insert(0, os.environ.get("HERMES_LIB") or str(Path(__file__).resolve().parents[5]))
from lib import runtime  # noqa: E402

LOCK_PATH = "/tmp/chiaki_apply.lock"  # crontab の flock と共有
WATCH_MGMT = runtime.CH_CHIAKI_MGMT            # 戸田さんの裁定（提案スレッド）
WATCH_SRC = {runtime.CH_YU_PDCA, runtime.CH_NICHIJI}  # 対象者の完了報告（対象スレッド）

# apply-ruling の main を読み込む（このプロセス内で実行。__main__ では起動しない）
_AR = os.path.join(os.environ["HERMES_PROFILE_DIR"], "skills/lipple/apply-ruling/scripts/run.py")
_g = {"__file__": _AR, "__name__": "apply_ruling_mod"}
exec(compile(open(_AR).read(), _AR, "exec"), _g)
_apply_main = _g["main"]


def _run_apply():
    """flock を取って apply-ruling を実行（crontab と排他）。"""
    with open(LOCK_PATH, "w") as lf:
        fcntl.flock(lf, fcntl.LOCK_EX)
        try:
            _apply_main()
        finally:
            fcntl.flock(lf, fcntl.LOCK_UN)


def _is_relevant(ch: str, thread_ts: str) -> bool:
    """その返信が pending に関係する（＝apply-ruling が見るべき）スレッドか。"""
    if not thread_ts:
        return False
    items = runtime.load_json("pending_approvals.json", {"items": {}}).get("items", {})
    if not items:
        return False
    if ch == WATCH_MGMT:
        return thread_ts in items                      # #8902 提案スレッド＝戸田さんの裁定
    if ch in WATCH_SRC:
        return any(it.get("source_ts") == thread_ts for it in items.values())  # 対象スレッド＝完了報告
    return False


def main():
    from slack_sdk.socket_mode import SocketModeClient
    from slack_sdk.socket_mode.response import SocketModeResponse
    from slack_sdk.web import WebClient

    client = SocketModeClient(
        app_token=os.environ["SLACK_APP_TOKEN"],
        web_client=WebClient(token=os.environ["SLACK_BOT_TOKEN"]),
    )

    def handle(c, req):
        # まず ack（再送防止）
        try:
            c.send_socket_mode_response(SocketModeResponse(envelope_id=req.envelope_id))
        except Exception:
            pass
        if req.type != "events_api":
            return
        ev = (req.payload or {}).get("event", {}) or {}
        if ev.get("type") != "message" or ev.get("subtype") or ev.get("bot_id"):
            return
        if ev.get("user") == runtime.CHIAKI_SELF:
            return
        ch, tts = ev.get("channel"), ev.get("thread_ts")
        if _is_relevant(ch, tts):
            print(f"[listener] relevant reply ch={ch} thread={tts} -> apply-ruling", flush=True)
            try:
                _run_apply()
            except Exception as e:
                print(f"[listener] apply error: {e}", flush=True)

    client.socket_mode_request_listeners.append(handle)
    client.connect()
    print("[listener] connected (Socket Mode, deterministic apply-ruling only)", flush=True)
    threading.Event().wait()


if __name__ == "__main__":
    main()
