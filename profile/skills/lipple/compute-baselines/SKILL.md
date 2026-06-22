---
name: compute-baselines
description: 蓄積した実測(actuals)から 種別×案件 / 種別 の相場を再構築して baselines.json に書く決定論バッチ。
metadata:
  hermes:
    tags: [observation, deterministic, cron]
---

# compute-baselines（§3.3）

`state/actuals_*.json` を全部読み、`種別×案件` と `種別` の実測統計（n/平均/中央/最小/最大）を `state/baselines.json` に再構築。`scripts/run.py` を cron `--no-agent` で。LLM 非起動。

- サンプルが増えるほど精度向上。後の「工数レベル推定」「§8 評価（業務量）」の土台。
- 種別ラベルのゆれ（例 `SNS` / `SNS投稿`）は相場を割るので、quality 側で正規化を促すと相場も締まる（表裏）。

## cron
`0 21 * * 1-5`（終業後）。
