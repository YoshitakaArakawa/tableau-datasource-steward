---
purpose: datasource-augmenter の入力 change-set spec（JSON）の全フィールド仕様
note: describer / prospector が生成し augmenter が消費する契約。spec の網羅はここに集約する
---

# change-set spec フォーマット

`augment_datasource.py --spec <path>` に渡す JSON の全フィールド。

## 1 サイクルの単位

(source 1 PDS) + (datasource.description 任意) + (descriptions N 件) + (calcs M 件) → (target PDS 1 個 publish)。
descriptions / calcs / datasource.description のいずれかが非空なら実行する意味がある。すべて空なら no-op。

## トップレベル

| フィールド | 必須 | 説明 |
|---|---|---|
| `source_luid` | yes | 編集元 PDS の LUID |
| `mode` | no | `CreateNew`（既定）/ `Overwrite`（明示時のみ、破壊的） |
| `target.new_name` | CreateNew 時必須 | 出力 PDS 名。Overwrite では無視（元の名前を維持） |
| `target.project_id` | no | 出力先 project LUID。省略時は source PDS の project を継承 |
| `datasource.description` | no | datasource レベルの説明（grain ステートメント等）。下記参照 |
| `descriptions` | no | field description の配列（下記） |
| `calcs` | no | calculated field の配列（下記） |

## `datasource.description`（grain）

PDS オブジェクト自体の説明文。dbt の model description（「このテーブルは何の 1 行か」= grain）に相当する。列の `<desc>` と違い `.tds` XML ではなく **Tableau カタログ側の属性**で、REST では **publish 時のみ設定可能**（`Update Data Source` は description 非対応）。本 Skill は publish リクエストの description 属性として送り、publish 後に再 query して一致を検証する。

```json
{ "datasource": { "description": "受注明細データソース。粒度は注文明細行（1 行 = 1 商品 × 1 注文）。" } }
```

Overwrite でも publish 経路で送られるが、既定は CreateNew。空文字列は送らない（未設定扱い）。

## `descriptions[]`

| フィールド | 必須 | 説明 |
|---|---|---|
| `field_caption` | yes | 対象 field の display 名。`<column caption='X'>` または `name='[X]'` を特定する |
| `text` | yes | 設定する説明文。既存 description があれば置換する。XML escape はスクリプトが行う |

## `calcs[]`

| フィールド | 必須 | 説明 |
|---|---|---|
| `caption` | yes | calc の display 名 |
| `formula` | yes | Tableau Calc 構文の式（caller 提供必須）。XML escape はスクリプトが行う |
| `datatype` | yes | `real` / `integer` / `string` / `boolean` / `date` / `datetime` |
| `role` | no | `measure` / `dimension`。省略時は datatype から導出（数値→measure） |
| `type` | no | `quantitative` / `nominal` / `ordinal`。省略時は role から導出 |
| `description` | no | calc 自体の説明文。注入する `<column>` に `<desc>` として付与 |

## 出力

- Tableau に新規（または上書き）PDS が publish される
- `<out-dir>/`: `original.tdsx`（revert 用）/ `original.tds` / `edited.tds` / `edited.tdsx` / `verified.tdsx` / `verified.tds` / `result.json`
- `result.json` / stdout `RESULT_JSON`: `published_luid` / `published_name` / `mode` / `roundtrip_checks`（desc・calc の survive）/ `calc_registered_graphql`（GraphQL での calc 登録確認）/ `datasource_description_check`（grain の再 query 一致）/ `verified`

## hoist 由来 calc の注意

`workbook-calc-prospector` が出した hoist 候補をそのまま calcs に流す場合、**table calc（WINDOW_*, INDEX, LOOKUP, RUNNING_* 等）は PDS 側 calc としては意味が変わりうる**ため、prospector 側で除外・警告済みであることを前提とする。operand が source PDS に存在しない calc は publish 時に formula エラーになる。

`datasource-column-describer` が付ける provenance フィールド（`source` = `extracted` | `inferred`、`conflict`、`variants`）が calcs / descriptions の要素に含まれることがある。これらは orchestrator 報告用の注記で、**augmenter は上記の定義済みキーのみ参照しスキップする**ため、change-set にそのまま残してよい。
