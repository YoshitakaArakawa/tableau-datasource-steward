---
name: datasource-inspector
description: Published Data Source のスキーマとメタデータを読み取り、整備状況（説明あり/なしの列、role、default aggregation、既存 calculated field）を把握する読取専用 Skill。Tableau MCP の get-datasource-metadata（列メタ）と Metadata API（既存 calc）を組み合わせ、メタデータの欠落を洗い出す。データソースの現状を棚卸ししたい、どの列に説明が無いか調べたい、calc を追加する前に既存定義を確認したいときに使う。出力は describer / augmenter の入力になる。
---

# datasource-inspector

対象 Published Data Source (PDS) の現状を読み取り、メタデータ整備のための棚卸しレポートを作る読取専用 Skill。副作用なし。

## なぜ二段か

- **列メタ（型・role・default aggregation・既存 description）** は Tableau MCP `get-datasource-metadata` が返す。
- ただし **`get-datasource-metadata` は datasource レベルの calculated field を列挙しない**（物理列のみ）。既存 calc は Metadata API（GraphQL）で別途読む。

この 2 経路を合わせて初めて「列 + calc」の全体像になる。

## ワークフロー

進捗:
- [ ] 対象 PDS の LUID を確認（無ければ `list-datasources` で名前から特定）
- [ ] `get-datasource-metadata` を LUID で呼び、列の `name` / `dataType` / `role` / `defaultAggregation` / `description`（**全文**）を取得
- [ ] `read_calcs.py --pds-luid <luid> --out calcs.json` を実行し、既存 calc（formula / description）と **datasource description（grain）** を取得
- [ ] 3 つを統合し棚卸しレポートを書く：datasource description（grain）の現在値、説明あり/なしの列（既存 desc は**全文**）、role、default-agg、既存 calc 一覧
- [ ] メタデータ欠落（description 未設定の列・calc、grain 未設定）を gap として明示

## 出力

棚卸しレポート（Markdown または JSON）に少なくとも次を含める:
- **datasource description（grain）の現在値**（有無・全文）
- 列一覧：name / dataType / role / defaultAggregation / **description（有無だけでなく既存 desc の全文）**
- 既存 calc 一覧：name / formula / **description（全文）**
- gap：description が未設定の列・calc、grain 未設定

既存 desc・grain を**全文**で出すのは、`datasource-describer` が「未設定を埋める」だけでなく「**既存 desc が現データに対して適切か検証する**」ためにも本レポートを使うため。inspector は事実（現在値）を提示するだけで、内容の妥当性判定はしない（判定は describer）。READ 層に推論を持ち込まない。

このレポートは `datasource-describer`（説明草案・既存 desc の検証）と `datasource-augmenter`（注入）の入力になる。

## 認証 / 依存

- MCP: Tableau MCP。接続は環境側で用意された MCP サーバーを前提（接続の詳細は CLAUDE.md の「認証」を正とする）。
- 既存 calc 読取: OAuth（`scripts/tableau_auth.py`）+ `scripts/metadata_api.py`。依存 `tableauserverclient` / `python-dotenv` / `requests`。

## Scripts

| スクリプト | 役割 |
|---|---|
| `scripts/read_calcs.py` | Metadata API で既存 calculated field（formula / description）と datasource description（grain）を取得し `calcs.json` に出力 |
