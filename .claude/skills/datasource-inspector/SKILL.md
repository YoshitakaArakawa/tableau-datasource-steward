---
name: datasource-inspector
description: Published Data Source のスキーマとメタデータを読み取り、整備状況（説明あり/なしの列、role、default aggregation、既存 calculated field）を把握する読取専用 Skill。Tableau MCP の get-datasource-metadata（列メタ）と Metadata API（既存 calc）を組み合わせ、メタデータの欠落を洗い出す。データソースの現状を棚卸ししたい、どの列に説明が無いか調べたい、calc を追加する前に既存定義を確認したいときに使う。出力は describer / augmenter の入力になる。
---

# datasource-inspector

対象 Published Data Source (PDS) の現状を読み取り、メタデータ整備のための棚卸しレポートを作る読取専用 Skill。副作用なし。

## なぜ二段か

- **列メタ（型・role・default aggregation・既存 description）** は Tableau MCP `Tableau:get-datasource-metadata` が返す。
- ただし **`get-datasource-metadata` は datasource レベルの calculated field を列挙しない**（物理列のみ）。既存 calc は Metadata API（GraphQL）で別途読む。

この 2 経路を合わせて初めて「列 + calc」の全体像になる。

## ワークフロー

進捗:
- [ ] 対象 PDS の LUID を確認（無ければ `Tableau:list-datasources` で名前から特定）
- [ ] `Tableau:get-datasource-metadata` を LUID で呼び、列の `name` / `dataType` / `role` / `defaultAggregation` / `description` を取得
- [ ] `read_calcs.py --pds-luid <luid> --out calcs.json` を実行し既存 calc（formula / description）を取得
- [ ] 2 つを統合し棚卸しレポートを書く：説明あり/なしの列、role、default-agg、既存 calc 一覧
- [ ] メタデータ欠落（description 未設定の列、説明なし calc）を gap として明示

## 出力

棚卸しレポート（Markdown または JSON）に少なくとも次を含める:
- 列一覧：name / dataType / role / defaultAggregation / description(有無)
- 既存 calc 一覧：name / formula / description(有無)
- gap：description が未設定の列・calc

このレポートは `datasource-column-describer`（説明草案づくり）と `datasource-augmenter`（注入）の入力になる。

## 認証 / 依存

- MCP: Tableau MCP（`INCLUDE_TOOLS` に datasource 系）。設定は `.mcp.json`（実値は gitignore、テンプレート `.mcp.json.template`）。
- 既存 calc 読取: OAuth（`scripts/tableau_auth.py`）+ `scripts/metadata_api.py`。依存 `tableauserverclient` / `python-dotenv` / `requests`。

## Scripts

| スクリプト | 役割 |
|---|---|
| `scripts/read_calcs.py` | Metadata API で既存 calculated field（formula / description）を取得し `calcs.json` に出力 |

## 設計原則

- 読取のみ。変更は augmenter に委譲
- 列メタは MCP、既存 calc は GraphQL（MCP が calc を列挙しない制約への対応）
- gap（未整備）を明示し、後段 Skill の作業対象を絞る
