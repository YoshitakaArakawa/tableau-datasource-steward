---
name: datasource-column-describer
description: Published Data Source の列および hoist 対象 calculated field に対し、列名・型・サンプル値・formula・WB 側 description から field description（説明文）の草案を作り、datasource-augmenter 用の change-set に整える Skill。説明が未設定の列を inspector の棚卸しから拾い、prospector の hoist 候補からは WB 側 description を抽出採用するか formula から推論し、各説明に出所（extracted / inferred）と衝突フラグを付ける。列の説明を埋めたい・データ辞書を整備したい・寄せた calc に説明を付けたいときに使う。注入そのものは augmenter が行う。
---

# datasource-column-describer

説明が未設定の列、および PDS へ寄せる calculated field に対して description 草案を作る分析 Skill。読取・推論のみ（注入は `datasource-augmenter`）。各説明に**出所（provenance）**を付け、自動解決できない衝突は orchestrator の報告でユーザー確認に回す。

## 入力（2 系統）

- **列の説明**: `datasource-inspector` の棚卸しレポート（列一覧・型・gap）。無ければ先に inspector を実行する。
- **hoist calc の説明**: `workbook-calc-prospector` の `candidates.json`（`wb_descriptions[]` / コメント込み `formula`）。calc 本体（caption / formula / datatype）は prospector + augmenter が持ち、本 Skill は **`description` と provenance だけ**を足す。

## ワークフロー（列の説明）

進捗:
- [ ] inspector のレポートから description 未設定の列を抽出
- [ ] 各列について、列名・型・role から説明を起こせるか判断
- [ ] 列名だけで意味が曖昧な列は `Tableau:query-datasource` でサンプル値を確認（dimension は distinct 上位、measure は MIN/MAX/AVG 等の集計で代表値）
- [ ] 「description の品質規範」に沿って簡潔で一貫した草案を作る（業務語彙・命名規則をユーザーに確認しつつ）
- [ ] 各説明に provenance（`source` / 衝突時は `variants`）を付ける
- [ ] `datasource-augmenter` の change-set `descriptions[]` に整えて引き渡す

## ワークフロー（hoist calc の説明）

prospector の各 candidate の `wb_descriptions[]` の件数で分岐する：

- [ ] **0 件** → `formula`（コメント含む）から説明を推論。コメントは「計算の説明か / TODO・dead code 等のノイズか」を判定し、説明に資する部分だけ使う。`source: inferred`
- [ ] **1 件** → その description をほぼそのまま採用（文体だけ品質規範に合わせる）。`source: extracted`
- [ ] **2 件以上** → 内容が整合するなら 1 文に reconcile（`source: extracted`、`variants` に原文を残す）。整合しない（相反する）なら自動マージせず、reconcile 草案 + 原文を `conflict: true` で残し、**orchestrator 報告でユーザー確認に回す**
- [ ] calc 本体（caption / formula / datatype）には触れない。`description` と provenance のみ付与
- [ ] augmenter の `calcs[]` 各要素の `description` として引き渡す

## サンプル値の使い方

- 主たる手がかりは列名・型・既存メタ。サンプル値は「列名だけでは意味が不明」なときの補助。
- コスト管理: 行レベル取得は避け、distinct + limit や集計を使う（query-datasource の TOP / 集計を活用）。

## 出力（change-set 候補）

列の説明は `descriptions[]`、hoist calc の説明は `calcs[]` 各要素の `description` に載せる。各説明に provenance を添える：

```json
{
  "descriptions": [
    { "field_caption": "Segment", "text": "顧客セグメント（Consumer / Corporate / Home Office）",
      "source": "inferred" }
  ],
  "calcs": [
    { "caption": "Profit Ratio", "description": "利益率（割引後利益 / 売上）",
      "source": "extracted", "conflict": false,
      "variants": ["利益率", "Profit / Sales の比率"] }
  ]
}
```

provenance フィールド（`source` = `extracted` | `inferred`、`conflict`、`variants`）は **orchestrator がユーザー報告で「抽出 / 推論」「衝突」を区別するための注記**。augmenter は自身が定義したキー（`field_caption` / `text` / `caption` / `formula` / `datatype` / `description` 等）だけを読み、provenance は無視するので、change-set にそのまま残してよい。

## description の品質規範

dbt Semantic Layer の指針（grain / 単位 / 包含 / 取りうる値を明示し、列名の同義反復を避ける）を列レベルに適用する。

- 言い換え禁止：列名の和訳だけ（`Order Date` →「注文日」）は情報量ゼロ。列名だけでは分からないこと（基準時点・タイムゾーン・null の意味）を書く。
- measure は単位・集計・包含を必須：通貨/個数、税抜・割引後か、inspector が拾う default aggregation と整合させる。
- dimension は取りうる値の集合を示す：列名で曖昧なものだけ `Tableau:query-datasource` で distinct 上位を確認して列挙する。
- calc 列は派生元と境界条件（しきい値・case 分岐）を書く。
- 文体を統一：体言止めか「〜を表す」に揃え、主語を省き 1〜2 文。change-set 内で文体を揃えてから augmenter に渡す。

grain（テーブル全体が何の 1 行か）は datasource レベルの description であり列単位ではない。本 Skill は列に専念し、grain の記述は `datasource-augmenter` 側で扱う。

## 認証 / 依存

サンプル値取得は Tableau MCP `Tableau:query-datasource`（`.mcp.json`、実値は gitignore）。スキーマ参照は inspector 経由。

## 設計原則

- 説明は列名・型・サンプル・formula・WB 側 desc から「草案」を作る。最終文言はユーザー承認を前提に
- サンプル値は補助。重い行レベル取得をしない
- WB の構造化 desc は抽出採用（`extracted`）、無ければ formula/コメントから推論（`inferred`）。出所を必ず付ける
- 相反する複数 desc は**自動マージしない**。reconcile 草案 + 原文を `conflict: true` で残し、判断は orchestrator 報告経由でユーザーに委ねる
- 注入はしない（augmenter に委譲）。本 Skill の成果物は change-set 候補
