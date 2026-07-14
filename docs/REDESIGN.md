# Chiaki AI 再設計計画（2026-07-14 戸田GO・Hermes/aiko設計書を参照）

2026-07-14 の全体レビュー（7観点・確定51件）で判明した構造的弱点を、本家 Hermes/aiko の設計
（実行台帳・イベント駆動・承認バインディング・HITL規定表・半自動改善ループ）で解消する段階計画。
各段階は独立にレビュー・ロールバック可能。旧ロジックは非常用フォールバックとして温存する。

## 不変の原則（aikoと共通）

- 口はGPT・手は決定論。gateway（execute_code）は封印のまま。
- 権限不足の操作は迂回しない（fail-closed）。コード変更・Codex起動の受付は戸田さん（U9R35H06L）のみ。
- 親（Claude Code）は子（Codex）の自己報告を検証せず採用しない。
- Webhook・Browser操作・顧客向け送信は持たない。

## 段階

| 段階 | 内容 | 目安 | 状態 |
|---|---|---|---|
| R1 | 実行台帳（1依頼=1行・記録と突き合わせ） | 1日 | 稼働（2026-07-14） |
| R2 | イベント駆動化（listenerのイベントが正・cronはリコンサイルに降格） | 1日 | 稼働（2026-07-14） |
| R3 | 承認バインディング（GO=提案ID+digestへの承認）＋HITL規定表の明文化 | 半日 | 稼働（2026-07-14） |
| R4 | 自己補完（自己診断→Codex修正／段階2=承認付き自動反映／日次サマリ・週次自己レビュー） | 1〜1.5日 | 未着手（R1〜R3の安定後） |
| R5 | コスト最適化（文脈の遅延読込・ツール結果圧縮） | 半日 | 任意 |

## R1: 実行台帳（詳細設計）

### 目的

「このスレッド・この発話は誰の担当で、どう処理されたか」の正本を1本にする。
現状は裁定台帳（pending_approvals）・起票台帳（chiaki_intake）・Codex台帳（codex_threads）に
状態が散り、受持ち境界を各所の除外集合で表現している＝レビュー51件の黙殺・二重応答・宙吊りの温床。

### 正本とスキーマ

- 正本: VPSローカル `state/exec_ledger.jsonl`（追記専用・event-sourcing型）。
  同一 `id` の**新しい行が古い行のフィールドを上書き**する（読み手は `lib/ledger.load()` でマージ済みを得る）。
  追記は `O_APPEND` の1行書き＝プロセス間でアトミック。コンパクションはしない（追記のみ・低容量）。
- Notionへは日次で控えを写す（R4の日次サマリと同時に導入・R1ではローカルのみ）。
- `id` = `"{channel}:{ts}"`（発話単位）。スレッド単位の束ねは `thread_root` フィールドで行う。

| フィールド | 意味 |
|---|---|
| id | channel:ts（発話の一意鍵） |
| at | 記録時刻（epoch） |
| source | listener / cron / reconcile / manual |
| actor | 発話者 user_id |
| ch / thread_root / ts | 位置 |
| kind | intake / ruling / codex / escalate / system |
| owner | intake / apply / codex / none（誰の領分として処理した/するか） |
| status | received → processing → replied / filed / ruled / queued / skipped / failed |
| refs | 結果への参照（reply_ts, page_urls, branch, verdict 等の辞書） |
| note | 短い補足（エラー種別等） |

### R1で書く場所（挙動は変えない＝記録のみ）

1. event-listener: ディスパッチ時に `received`（従来の listener_dispatch.jsonl を置換・発展）。
2. chiaki-intake: 候補処理の完了時に owner=intake で結果 status。
3. apply-ruling: 裁定実行時に owner=apply（verdict・依頼投稿tsをrefsへ）。
4. codex-runner: スレッド対話・キュー実行の完了時に owner=codex。
5. self-health: 黙殺検知の突き合わせ先を台帳へ切替（received に対し処理記録 or カーソル越えが無ければ警告）。

## R2: イベント駆動化（2026-07-14 実装）

- listener が受信イベント（text含む）を台帳へ登録し、intake の主経路は
  `_ledger_candidates()`＝「owner=intake・status=received の台帳行」になった。
  処理直前に Slack の現物を読み直す（編集・削除に追随＝入力の権威は Slack）。
- 従来のフルスキャン（_candidates）は **5分毎のリコンサイル**に降格
  （listener 停止・イベント欠落・台帳導入前の残りの補完。`tuning_cursor.json` の `__scan__` で間隔管理）。
- listener の `_is_intake_thread` は awaiting_confirm に加え filed（24h）も対象＝
  起票直後の続き依頼も即時。apply-ruling / codex-runner は従来どおり（apply は R3 で承認バインディング化）。
- 防御: 台帳経路にも裁定スレッドのガード（メンション無し・裸裁定語は apply の領分）と
  エスカレーション条件（トップレベル・1h・メンション・人間）を再判定で持つ。

## R3: 承認バインディング＋HITL規定表（2026-07-14 実装）

- 裁定の実行時に `approval = {proposal, digest(実際に出した最終文面), approver, ruled_at, verdict}` を
  裁定台帳の item と実行台帳 refs の両方へ記録＝「何への承認だったか」が残る。
- 冪等化: 裁定発話（GOのts）が台帳上 ruled 済みなら再実行しない＝承認は発話単位で一度だけ消費。
  状態ファイルの巻き戻り・競合・再走査でも同じGOが二度実行されない。
- 裁定台帳（pending_approvals.json）の書き手3系統（apply-ruling / intake retract /
  propose-to-approval）を単一ロック（/tmp/chiaki_apply.lock＝runtime.approvals_lock）に統一。
  propose は「ロック下で読み直してマージ」方式＝全書き戻しで他系統の遷移を消さない。
- HITL規定表を docs/SPEC.md §7 に明文化（操作区分ごとの承認の既定・requester/approver=戸田さんのみ）。
- 未実装（R3残・必要になったら）: 承認の期限（古い提案へのGOに再確認を挟む）。

### R4が台帳をどう使うか（予告）
- R4: 日次サマリ=台帳の日次集計。自己診断=対象スレッドの台帳行+ログを証拠としてIssueに添付。
