---
name: notion-write
description: Notion タスクページの補完メタ（カテゴリー/工数/優先度）だけを書く。status/sync_source には絶対に触れない。書き込みは notion_write.py 経由で機械的に強制。
metadata:
  hermes:
    tags: [notion, write-guard, approval]
---

# notion-write（§4 Notion 書き戻し・ガード必須）

ワークフロー状態を壊さないための唯一の書き込み口。**必ず `scripts/notion_write.py` 経由**で書く（agent が直接 Notion MCP の patch を叩かない）。

## 許可/禁止（機械的に強制）
- **書いてよい**: カテゴリー（`category`/`カテゴリー`）・工数（`effort`/`工数`・実測）・優先度（`priority`/`優先度`・提案）。
- **絶対禁止**: `status`／`sync_source`（および別名 `ステータス`）。スクリプトが拒否する。
- カテゴリーは**上書きしない自動補完**（既に値があれば触らない）。優先度は提案のみ。

## 承認・前提
- サイレント期は `policy.json: notion_writes_require_approval:true`＝書き込みも propose→戸田承認後。
- タスクDB（**🎯 タスク_DB**, ID `331980d4-f840-800b-8bde-f6669422aeb1`）は統合に**共有済み＝404解消**。書き込めるのは status/sync_source を除くメタ（カテゴリー/工数レベル/優先度）のみ・承認後。観測本体は Slack+local で完結。

## 使い方
`notion_write.py <page_id> '{"カテゴリー":"...","工数":1.6}'` → 禁止プロパティが含まれていれば**実行前に拒否**。`--no-overwrite-category` で既存カテゴリーを保護。
