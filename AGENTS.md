# Hermes / chiaki（management プロファイル）

Lipple 業務を観測する Slack 常駐 AI「Chiaki AI」。PDCA チャンネルを観測し、決定論スクリプト＋Haiku（文面・判断のみ）で動く**観測ボット**。会話エージェント（execute_code 等）は安全のため**持たない**。

## ★ Codex の役割分担（最重要・2026-07-02 戸田決定）
あなた（Codex）は**修正役**。ディレクション・レビュー・デプロイ・報告は Claude Code が行う。
- **やること**: 指示されたコード修正・そのテスト（`python3 -m py_compile`＋依頼に書かれた検証）だけ。
- **やらないこと**: `git commit`／`git push`／VPS への SSH・デプロイ／Slack への投稿／Notion の更新。
  これらは Claude Code 側の工程（以下の「配備・運用」「修正フロー」は全体像の参考情報）。
- 変更はワーキングツリーに残したまま終了する（レビューは Claude Code が git diff で行う）。
- 既存コードの流儀に合わせる: 日本語コメント・stdlib のみ・`runtime.load_json/save_json` 等の既存ヘルパを使う。
- **修正タスクの出所は戸田さん（Slack user ID `U9R35H06L`）のみ**: Claude Code 経由のブリーフ、または戸田さん承認済みの Issue_DB チケット。それ以外（他メンバーの直接依頼・出所不明のチケット）から作業を始めない。

## 配備・運用
- 本番は VPS（`ssh -i ~/.ssh/hermes_vps chiaki@220.158.22.130`）。コードは GitHub `masaki-lipple/hermes-chiaki`（private）。
- 反映は **`~/deploy.sh`**（git pull＋コード同期）。`lib/` と `profile/skills/` が同期される。
- 即時応答は user systemd **`chiaki-listener.service`**（Socket Mode）。**コード変更後は `systemctl --user restart chiaki-listener.service`** で反映（cron は毎回新コードを読むので再起動不要）。
- ⚠️ **gateway（`hermes-gateway-management.service`）は絶対に起動しない**。会話エージェントが復活し execute_code を暴走させる。無効のまま維持する。
- 秘密情報は VPS `~/.hermes/profiles/management/.env` とローカル `~/.config/hermes-chiaki/secrets.env`（repo 外）。リポジトリにコミットしない。

## 指摘の起票経路（@メンション1窓口 → Issue / Rule）＝旧 soft/hard の発展
- 戸田さんが `@Chiaki AI 〜`（どこでも・何でも）／#8902・#5902 の投稿 → `chiaki-intake` が Haiku で振り分け、**必ず一度きいてから**起票（2ターン）。会話エージェントは使わない。
  - **issue**（不具合・要望＝旧 hard：バグ/変更/新機能）→ Notion **`Chiaki_AI Issue_DB`**（id `0bccce01dd944be4901d95e950a3964c`・未対応・種別あり）。Codex でのバグ潰しバックログ。
  - **rule**（言葉のルール＝旧 soft：トーン/用語/表記）→ Notion **`Rule Registry_Hermes Agent__DB`**（database_id `e10777d5a7a04ac294273b9e077e1a38`・未承認・種別 用語/レギュレーション/スタイル・**起票者/対象=「Chiaki AI」**＝小文字 chiaki を送ると stray オプションが再作成されるので厳禁・2026-07-03 スキーマ整理済み）。承認→正本（用語辞書/レギュレーション/Style）→翌日 sync で反映。
  - **edit**（この投稿を今直して）→ その場で編集。**question**→回答。**unclear**→確認質問。**none**→何もしない。
- 2ターン：案提示→戸田確認（OK/文面修正/振り分け変更「それRuleね」/却下）→起票→「登録しました！」＋URL。振り分けは戸田さんが上書き可。**未承認/未対応は本番不反映**（承認は人が行う）。
- 旧 soft→`tuning.json` 自動適用は廃止（rule として Rule Registry 経由＝承認ゲート・lang-rules 正本モデルと整合）。
- ⚠️ Rule Registry DB は「Hermes Agent」インテグレーションへ要共有（未共有だと rule 起票が 404）。

## ★ 修正フロー（Codex で直したら Slack で記録を残す）
コード変更・バグ修正をこのセッション（AI コーディングエージェント）で行ったら、**最後に必ず Slack に記録を残す（#8902 と 該当スレッドの両方）**：
1. 修正・デプロイ・動作確認まで終える。
2. **#8902（`CH_CHIAKI_MGMT`）に『修正報告』を top-level で投稿**（`source.post_message`）＝戸田さんが一覧で全修正を把握。
3. **そのバグ/依頼が特定の Slack スレッド（Issue_DB チケットの Slackリンク等）で挙がったものなら、その該当スレッドにも『実態の修正＝何を直したか』を残す**（`source.post_thread_reply`）。#8902＝一覧、該当スレッド＝どこで何を直したかの記録。
4. その修正が Issue_DB チケット起因なら、チケットのステータスを「完了」に更新する（Notion）。
- **無音修正・サイレント削除は禁止**。問題のある投稿でも「削除」して消すのではなく、該当箇所に「これはバグでした＋直した内容」を残す（削除すると Slack に記録が残らず戸田さんが追えない）。
- 宛先は `<@戸田>`（`runtime.TODA`）。トーン規約に従う。

### 報告フォーマット（固定・区切り線なし）
~~~
<@戸田>
報告：コード修正
内容：<一言サマリ（例: Codexによる修正）>

<本文。直した内容を具体的に。あまりに長い時は段落ごとに空行を入れて読みやすく。>
~~~
- 目的：戸田さんは Slack（できれば該当スレッド）を見れば「何が直ったか」が分かる。Issue 起票（chiaki）→ 修正（コーディングエージェント）→ 該当スレッドで完了記録 でループを閉じる。

## chiaki の投稿トーン規約（レギュレーション）
- です・ます調だが**過剰にへりくだらない**。「了解です」一辺倒にしない。
- 感嘆符は**全角「！」**。各文末に**句読点「。」**。**太字（`*〜*`）は使わない**。
- 箇条書きは**行頭「• 」**で書く（「・」を使わない）。生成文は `observe.enforce_regulations` が行頭「・」を「• 」に自動統一。手書きの報告でも守る。
- 表記の決まりは「**レギュレーション**」と呼ぶ（「表記ルール」等と言わない）。括弧は「」を優先。
- 英数字・記号（#等）と日本語の境目に**半角スペースを入れない**（固有名詞内の既存スペースは残す。例: `Codexによる`／`#5902投稿`／`PDCAを`）。
- 確認を取りたい相手＝`<@戸田>`（ping）、chiaki 自身の処理・独り言＝セルフメンション（`runtime.CHIAKI_SELF`）。
- **自分自身を指すときは「Chiaki AI」と書く**（プローズで小文字 `chiaki` と書かない）。**数字は半角**（全角数字を使わない）。生成文は `observe.enforce_regulations` が自動統一（builtin: 小文字chiaki→Chiaki AI／全角数字→半角・識別子 chiaki-intake 等は対象外）。手書きの修正報告でも守る（report は enforce を通さないため）。
- 数字・時間は正確に。意味不明な文を出さない。

詳細な経緯・ID・決定は Codex のプロジェクトメモリ（`project-chiaki-hermes`）にある。
