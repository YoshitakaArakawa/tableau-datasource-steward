---
name: datasource-inspector
description: Published Data Source のスキーマとメタデータを読み取り、整備状況（説明あり/なしの列、role、既存 calculated field、grain）を把握する読取専用 Skill。Metadata API (GraphQL) 1 クエリで列・calc・grain を一括棚卸しし、論理テーブル由来の擬似列の除外と説明対象外（hidden・定数 calc 等）の skip 候補分類まで行う。データソースの現状を棚卸ししたい、どの列に説明が無いか調べたい、calc を追加する前に既存定義を確認したいときに使う。出力は describer / augmenter の入力になる。
---

# datasource-inspector

対象 Published Data Source (PDS) の現状を読み取り、メタデータ整備のための棚卸しレポートを作る読取専用 Skill。副作用なし。

## 読取経路

- **主経路は Metadata API (GraphQL)**。`read_schema.py` が列（name / dataType / role / isHidden / description / upstream 1:1）・既存 calc（formula / description）・grain（datasource description）を 1 クエリで返す。バッチで安定し、per-PDS の往復が無い。
- **MCP `get-datasource-metadata` は補完**。defaultAggregation 等 GraphQL に無い属性が必要なときだけ呼ぶ。並列呼び出しで断続的に 401 を返すことがあるため、バッチの主経路にしない。
- GraphQL は論理テーブル自体を ColumnField として数える（名前が upstream テーブル名と一致し upstream 列を持たない擬似列）。`read_schema.py` が除外して `pseudo_table_fields_excluded` に記録する。

## ワークフロー

進捗:
- [ ] `python scripts/tableau_auth.py status` で cached session を確認（`no cached session` なら先にサインインを依頼）
- [ ] 対象 PDS の LUID を確認（無ければ `list-datasources` で名前から特定）
- [ ] `read_schema.py --pds-luid <luid> --out schema.json` を実行
- [ ] defaultAggregation が必要な場合のみ `get-datasource-metadata` で補完
- [ ] 棚卸しレポートを書く：grain の現在値、説明あり/なしの列（既存 desc は**全文**）、role、既存 calc 一覧、skip 候補
- [ ] メタデータ欠落（`gap`: description 未設定の列・calc、grain 未設定）を明示

## 出力

`read_schema.py` の `schema.json` に以下が含まれる:

- **`datasource_description`（grain）の現在値**（None/空 = 未設定）
- `columns[]`：name / dataType / role / isHidden / **description（全文）** / upstream_1to1（source 列経路が使えるかの目印）
- `calcs[]`：name / formula / **description（全文）**
- `pseudo_table_fields_excluded[]`：分母から除外した論理テーブル擬似列
- `skip_candidates[]`：説明対象外の**候補**（hidden フィールド・定数 calc・単純エイリアス calc）。理由付き。採否は describer / ユーザーが判断する
- `gap`：description 未設定の列・calc（skip 候補は除く）、grain 未設定

既存 desc・grain を**全文**で出すのは、`datasource-describer` が「未設定を埋める」だけでなく「**既存 desc が現データに対して適切か検証する**」ためにも本レポートを使うため。inspector は事実（現在値）を提示するだけで、内容の妥当性判定はしない（判定は describer）。READ 層に推論を持ち込まない。

このレポートは `datasource-describer`（説明草案・既存 desc の検証）と `datasource-augmenter`（注入）の入力になる。

## 認証 / 依存

- 主経路: OAuth（`scripts/tableau_auth.py`）+ `scripts/metadata_api.py`。依存 `tableauserverclient` / `python-dotenv` / `requests`。
- 補完: Tableau MCP。接続は環境側で用意された MCP サーバーを前提（接続の詳細は CLAUDE.md の「認証」を正とする）。

## Scripts

| スクリプト | 役割 |
|---|---|
| `scripts/read_schema.py` | Metadata API 1 クエリで列・既存 calc・grain を棚卸しし、擬似列の除外と skip 候補の分類まで行って `schema.json` に出力 |
