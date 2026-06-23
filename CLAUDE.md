# Hermes / chiaki（management プロファイル）

Lipple 業務を観測する Slack 常駐 AI「Chiaki AI」。PDCA チャンネルを観測し、決定論スクリプト＋Haiku（文面・判断のみ）で動く**観測ボット**。会話エージェント（execute_code 等）は安全のため**持たない**。

## 配備・運用
- 本番は VPS（`ssh -i ~/.ssh/hermes_vps chiaki@220.158.22.130`）。コードは GitHub `masaki-lipple/hermes-chiaki`（private）。
- 反映は **`~/deploy.sh`**（git pull＋コード同期）。`lib/` と `profile/skills/` が同期される。
- 即時応答は user systemd **`chiaki-listener.service`**（Socket Mode）。**コード変更後は `systemctl --user restart chiaki-listener.service`** で反映（cron は毎回新コードを読むので再起動不要）。
- ⚠️ **gateway（`hermes-gateway-management.service`）は絶対に起動しない**。会話エージェントが復活し execute_code を暴走させる。無効のまま維持する。
- 秘密情報は VPS `~/.hermes/profiles/management/.env` とローカル `~/.config/hermes-chiaki/secrets.env`（repo 外）。リポジトリにコミットしない。

## soft / hard と Issue_DB（Slack 上での「学習」）
- 戸田さんが #8902 / #5902 で chiaki に指示すると `chiaki-tuning` が **soft / hard** を判定する。
  - **soft**（言い回し・トーン・形式・句読点・レギュレーション用語など、文面の調整）→ `tuning.json` に学習し、生成・1投稿の編集に反映。
  - **hard**（リンク/ID 差替・ロジック・しきい値・時間・新機能・バグ・複数/過去投稿の一括修正など、コード対応が要るもの）→ 学習せず、Notion **`Chiaki_AI Issue_DB`**（id `0bccce01dd944be4901d95e950a3964c`）に自動起票（ステータス＝未対応）し、Slack に「Slackのやりとりでは対応できないので、AIコーディングエージェントをお使いください！」と正直に返す。

## ★ 修正フロー（Claude Code で直したら Slack で報告する）
コード変更・バグ修正をこのセッション（AI コーディングエージェント）で行ったら、**最後に必ず Slack に報告する**：
1. 修正・デプロイ・動作確認まで終える。
2. **chiaki として #8902（`CH_CHIAKI_MGMT`）へ「修正報告」を投稿**（`lib.source.post_message`、宛先は `<@戸田>`＝`runtime.TODA`）。書式：1行目で報告する旨、続けて「内容（何を直したか）」、必要なら原因や副次対応、最後に「デプロイと動作確認まで完了」。トーン規約に従う。
3. その修正が **Issue_DB のチケット起因なら、そのチケットのステータスを「完了」に**更新する（Notion）。
- 目的：戸田さんは Slack だけ見ていれば「何を直したか」が分かる。Issue 起票（chiaki）→ 修正（コーディングエージェント）→ 完了報告（Slack）でループを閉じる。

## chiaki の投稿トーン規約（レギュレーション）
- です・ます調だが**過剰にへりくだらない**。「了解です」一辺倒にしない。
- 感嘆符は**全角「！」**。各文末に**句読点「。」**。**太字（`*〜*`）は使わない**。
- 表記の決まりは「**レギュレーション**」と呼ぶ（「表記ルール」等と言わない）。括弧は「」を優先。
- 確認を取りたい相手＝`<@戸田>`（ping）、chiaki 自身の処理・独り言＝セルフメンション（`runtime.CHIAKI_SELF`）。
- 数字・時間は正確に。意味不明な文を出さない。

詳細な経緯・ID・決定は Claude のプロジェクトメモリ（`project-chiaki-hermes`）にある。
