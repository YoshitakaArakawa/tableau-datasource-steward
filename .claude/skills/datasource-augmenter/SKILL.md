---
name: datasource-augmenter
description: Published Data Source に field description・datasource レベルの説明（grain）・calculated field を注入し、新規 PDS として publish して round-trip 検証する write エンジン。change-set spec を入力に download → .tds XML 編集 → publish（既定 CreateNew）→ 再 DL 検証を一気通貫で実行する。列の説明を埋めたい、データソース全体の粒度（grain）を記述したい、計算フィールド（指標定義）を PDS に追加・集約したい、workbook から共通 calc を寄せたいときに使う。describer / prospector が出した change-set を反映する最終段。
---

# datasource-augmenter

Published Data Source (PDS) に **field description** と **calculated field** を注入する write エンジン。Tableau には field description / calc を直接更新する API が無いため、`download → .tds XML 編集 → publish → 再 DL 検証` の経路で行う。

破壊的副作用（publish）を持つのは steward の中でこの Skill だけ。describer / prospector（読取・提案）が出した change-set spec を受け取り反映する最終段。

## publish ポリシー（重要）

- **既定は `mode: CreateNew`**（別名で新規 PDS を作る）。元 PDS は変更しない。
- `Overwrite` は元 PDS を破壊的に更新し、下流 workbook を巻き込みうる。**明示指定必須**で、呼ぶ前に下流影響（`workbook-calc-prospector` または lineage 確認）をユーザーに提示し承認を取る。
- steward は「更新案 PDS を作る」までを既定スコープとする。本番 PDS の置換（promote / swap）はユーザーの意図的な操作。

## スコープ

含む:
- field description の注入・更新（通常列・calc 列の両方）
- datasource レベルの description（grain ステートメント）の設定（publish 時のみ）
- calculated field の注入（caption / formula / datatype / role、任意で description）
- CreateNew publish（既定）/ Overwrite publish（明示時のみ）
- 編集後の round-trip 検証（再 DL して desc / calc が survive したか機械チェック、calc は GraphQL でも登録確認）

含まない:
- 既存 calc / 列の **削除**、列の rename / hide / cast（将来拡張）
- formula の推論・naming 規約の自動生成（caller が change-set で明示提供）
- default aggregation / folder 構成（現行スコープ外）
- workbook 側の編集（steward は PDS へ寄せるまで。workbook 掃除はしない）

## 入力（change-set spec）

`augment_datasource.py --spec spec.json --out-dir <dir>` で実行する。spec の全フィールドは [references/change-set-format.md](references/change-set-format.md) を参照。最小例:

```json
{
  "source_luid": "LUID_PLACEHOLDER",
  "target": { "new_name": "my_datasource__augmented" },
  "mode": "CreateNew",
  "datasource": { "description": "受注明細データソース。粒度は注文明細行。" },
  "descriptions": [
    { "field_caption": "Sales", "text": "注文明細の売上金額" }
  ],
  "calcs": [
    { "caption": "Profit Ratio", "formula": "SUM([Profit])/SUM([Sales])",
      "datatype": "real", "description": "利益率" }
  ]
}
```

## ワークフロー

進捗:
- [ ] spec を読み、`mode`・`source_luid`・`target.new_name`（CreateNew 時必須）を検証
- [ ] `Overwrite` 指定時は下流影響を提示し承認を取得
- [ ] `augment_datasource.py` を実行（download → 編集 → publish → 再 DL 検証）
- [ ] `result.json` の `verified` と `roundtrip_checks` / `calc_registered_graphql` / `datasource_description_check` を確認
- [ ] 検証 NG なら `out-dir` の `edited.tds` / `verified.tds` を読み、原因（caption 不一致・formula 構文・survive せず）を切り分け

スクリプトは編集前 `original.tdsx` を必ず保存する（revert 用）。

## XML 編集の要点

詳細は [references/tds-edit-format.md](references/tds-edit-format.md)。実装上の必須事項:

- **改行非依存で編集する**。`.tds` は CRLF のことがあり、改行に依存した anchor は壊れる。
- field の特定は **caption='X' または name='[X]'** の両対応。caption は display 名と内部名が違うときだけ存在し、等しいと省略される。
- description は `<column>` 子の `<desc><formatted-text><run>テキスト</run></formatted-text></desc>`。既存 desc は置換する。
- calc は `<aliases .../>` 直後に `<column><calculation class='tableau' formula='...'/></column>` を sibling として注入。formula は XML escape する。

## 検証の注意

- **MCP `get-datasource-metadata` は datasource レベルの calc を列挙しない**（物理列のみ）。calc 注入の確認は再 DL した `.tds` か GraphQL Metadata API（`fields{ ... on CalculatedField }`）で行う。スクリプトは両方を実施する。
- description は再 DL の `.tds` で本文一致を確認する。通常列 desc は `get-datasource-metadata` の `description` にも露出する。
- **grain（datasource.description）は `.tds` に乗らない**カタログ属性。publish 後に `datasources.get_by_id(luid).description` を再 query して一致を検証する（`datasource_description_check`）。REST `Update Data Source` は description 非対応なので、grain は publish 時に設定するしかない。

## 認証

OAuth 2.0 (Authorization Code + PKCE)。リポジトリ直下の `scripts/tableau_auth.py`（`signed_in_server()`）を共通モジュールとして import する。`.env` に `SERVER` / `SITE_NAME` を置く（テンプレートは `.env.template`）。

## 依存

`tableauserverclient` / `python-dotenv` / `requests`。

## Scripts

| スクリプト | 役割 |
|---|---|
| `scripts/augment_datasource.py` | spec を読み download → XML 編集（desc / calc 注入）→ publish → 再 DL 検証を一気通貫実行。終了時に `RESULT_JSON` を emit し `result.json` を書く |

## 設計原則

- CreateNew が既定、Overwrite は明示指定＋承認必須（破壊的副作用の隔離）
- caller が change-set を明示提供する前提。formula / 説明文は推論しない
- 編集前 `original.tdsx` を必ず保管（revert 可能に）
- 編集後は必ず round-trip 検証。失敗は握り潰さず result に出す
- 注入のみ（削除・rename はスコープ外）
