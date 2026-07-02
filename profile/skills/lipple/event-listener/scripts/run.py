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


# chiaki-intake（@メンション/指摘＝issue/rule 振り分け・質問は回答）も即時起動
_INTAKE = os.path.join(os.environ["HERMES_PROFILE_DIR"], "skills/lipple/chiaki-intake/scripts/run.py")
_gi = {"__file__": _INTAKE, "__name__": "chiaki_intake_mod"}
exec(compile(open(_INTAKE).read(), _INTAKE, "exec"), _gi)
_intake_main = _gi["main"]
INTAKE_LOCK = "/tmp/chiaki_intake.lock"


def _run_intake():
    """flock を取って chiaki-intake を実行（crontab と排他）。"""
    with open(INTAKE_LOCK, "w") as lf:
        fcntl.flock(lf, fcntl.LOCK_EX)
        try:
            _intake_main()
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


def _is_intake_thread(ch: str, thread_ts: str) -> bool:
    """その返信が chiaki-intake の確認待ち（awaiting_confirm）スレッドか＝確認ターンを即時起動。"""
    if not thread_ts:
        return False
    items = runtime.load_json("chiaki_intake.json", {"items": {}}).get("items", {})
    return any(it.get("status") == "awaiting_confirm" and it.get("channel") == ch
               and it.get("thread_root") == thread_ts for it in items.values())


_DEDUP_LOCK = threading.Lock()


def _dup(key: str) -> bool:
    """同一発話(ch:ts)の重複を弾く（直近500保持）。10並列ワーカーのため lock で read-modify-write を直列化。
    第二の冪等性ガード（intake の items/(ch,ts)・cursor、apply の status）が本筋で、これは一次フィルタ。"""
    if not key:
        return False
    with _DEDUP_LOCK:
        st = runtime.load_json("processed_events.json", {"ids": []})
        ids = st.get("ids", [])
        if key in ids:
            return True
        ids.append(key)
        st["ids"] = ids[-500:]
        runtime.save_json("processed_events.json", st)
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
        etype = ev.get("type")
        if etype not in ("message", "app_mention") or ev.get("subtype") or ev.get("bot_id"):
            return
        if ev.get("user") == runtime.CHIAKI_SELF:
            return
        ch, tts, user = ev.get("channel"), ev.get("thread_ts"), ev.get("user")
        # 何をするか先に決め、actionable な時だけ event_id で重複排除（message/app_mention の二重も吸収）。
        # 戸田さんの明示的な @メンションは intake 最優先（監査確定：促しスレッド内の @メンションが
        # _is_relevant で apply-ruling に回り黙殺されていた）。メンション無しの裁定返信は従来どおり apply。
        action = None
        if user == runtime.TODA and etype == "app_mention":
            action = "intake"
        elif _is_relevant(ch, tts):
            action = "apply"
        elif user == runtime.TODA and (ch in (runtime.CH_CHIAKI_MGMT, runtime.CH_CHIAKI_PDCA)
                                       or _is_intake_thread(ch, tts)):
            action = "intake"
        elif etype == "app_mention" and user:
            # 戸田さん以外の @Chiaki AI ＝ intake がエスカレーション（受領＋戸田さんへ引き継ぎ）で処理
            action = "intake"
        if not action:
            return
        # ch:ts で統一＝同一発話の message と app_mention は同一鍵で1回に畳む（event_id は両者で別＝素通りする）
        if _dup(f"{ch}:{ev.get('ts')}"):
            return
        try:
            if action == "apply":
                print(f"[listener] ruling event ch={ch} thread={tts} -> apply-ruling", flush=True)
                _run_apply()
            else:
                print(f"[listener] toda {etype} ch={ch} -> chiaki-intake (即時)", flush=True)
                _run_intake()
        except Exception as e:
            print(f"[listener] handler error: {e}", flush=True)

    client.socket_mode_request_listeners.append(handle)
    client.connect()
    print("[listener] connected (Socket Mode: apply-ruling裁定/完了 ＋ chiaki-intake即時)", flush=True)
    threading.Event().wait()


if __name__ == "__main__":
    main()
