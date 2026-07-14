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
| R1 | 実行台帳（1依頼=1行・記録と突き合わせ） | 1日 | 実装中（2026-07-14） |
| R2 | イベント駆動化（listenerのイベントが正・cronはリコンサイルに降格） | 1日 | 未着手 |
| R3 | 承認バインディング（GO=提案ID+digestへの承認）＋HITL規定表の明文化 | 半日 | 未着手 |
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

### R2以降が台帳をどう使うか（予告）

- R2: listener が正規化した依頼（text含む）を台帳に書き、各スキルは「自分がownerの未処理行」を引く。
  cron走査は「台帳に無い発話を拾って登録する」リコンサイルに降格。
- R3: 裁定は `refs.approval = {提案id, digest, 承認者, ts}` として台帳に束縛。
- R4: 日次サマリ=台帳の日次集計。自己診断=対象スレッドの台帳行+ログを証拠としてIssueに添付。
