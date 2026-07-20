---
name: datasource-augmenter
description: Published Data Source のメタデータを書き込む write エンジン。書き込み経路は 2 つ — (1) source 列の Catalog description を REST Update Column で in-place 更新（PDS を作らず全下流に継承）、(2) .tds XML 編集 + republish で field description・grain（datasource 説明）・calculated field を注入。desc のみの Overwrite は preflight + XML diff ゲート付きの準非破壊モードとして自走でき、calc 注入は CreateNew で draft を作る。列の説明を埋めたい、grain を書きたい、calc を PDS に追加・集約したい、draft PDS を掃除したい、書き込みを巻き戻したいときに使う。describer / prospector が出した change-set を反映する最終段。
---

# datasource-augmenter

Published Data Source (PDS) のメタデータを書き込む write エンジン。describer / prospector（読取・提案）が出した change-set を受け取り反映する最終段。

## 書き込み経路の使い分け（最重要）

| 書きたいもの | 経路 | スクリプト | PDS への影響 |
|---|---|---|---|
| source 列の説明（PDS フィールドと 1:1 の実テーブル列） | Catalog in-place（REST Update Column） | `update_source_column_descs.py` | **PDS を作らない・触らない**。全下流 PDS に `descriptionInherited` として継承 |
| PDS フィールドの description・grain | .tds republish（**desc-only Overwrite**） | `augment_datasource.py` | LUID 維持の準非破壊（ゲート付き、下記） |
| calculated field の注入 | .tds republish（**CreateNew** → 承認 → promote） | `augment_datasource.py` | 別名 draft PDS を新規作成 |

経路選択の根拠となる制約:
- PDS フィールドの description スロット（`.tds` の `<desc>`）を直接書く API は無い。republish が唯一の経路で、extract を参照する PDS は **.tdsx（extract 込み）往復が必須**（定義のみの publish はサーバーが拒否する）。
- source 列の Catalog description は別スロット。REST が認識する実テーブル（live / cloudfile 接続元）にのみ書け、hyper extract の内部テーブルは対象外。継承は Prep フローを跨いで下流 PDS に届き、`get-datasource-metadata` では `descriptionInherited` 属性として露出する。
- 1 フィールドが複数の upstream 列を持つ派生フィールドに source 列経路は使わない（`resolve` が ineligible に落とす）。それらは republish 経路で書く。

## publish ポリシー

- **desc-only Overwrite（spec に `calcs` が無い Overwrite）は準非破壊**。スクリプトが自動で次の安全装置を適用し、すべて通過したときだけ publish する。per-PDS の人間承認は不要（承認は**バッチ単位で 1 回**、事後報告に集約）。
  - preflight: `embedPassword=true` の接続があれば中止（republish は connection を作り直すため）。例外は spec の `connection_credentials.oauth_username` で OAuth 再 embed を明示した場合で、接続の userName 一致を検証して続行する。実行中の upstream flow run があれば中止（データ巻き戻り防止）。次回スケジュール実行が近ければ warning。
  - XML diff ゲート: 編集差分が `<desc>`（と desc を載せるための合成 `<column>` 殻）に限られることを canonical XML 比較で publish 前に機械証明。それ以外の差分が 1 つでもあれば publish せず中止。
  - LUID 検証: Overwrite は「名前 + プロジェクト」一致で LUID を保持する。別 LUID になったら失敗として扱う（`luid_preserved`）。
- **CreateNew は calc 注入の既定**。別名 draft（例 `…__augmented`）を作り、元を壊さない。本番への反映（promote / calc 込み Overwrite）は**明示要求 + 下流影響の提示 + ユーザー承認**がそろったときのみ。
- **grain は publish 時にしか設定できない**。Overwrite で spec に `datasource.description` が無ければ既存 grain を読んで引き継ぐ（消さない）。
- **OAuth コネクタ（BigQuery / Google Drive 等）の republish は embed 済み資格情報を失う**。spec の `connection_credentials.oauth_username` を指定すると、publish 時に実行ユーザーの Saved Credential を embed し直し（生トークンは API に流れない）、publish 後に `embed_check` で維持を検証して `verified` の合否に含める。前提・制約は [references/change-set-format.md](references/change-set-format.md) を参照。
- **巻き戻し**: republish 経路は `--rollback`（保全済み `original.tdsx` を Overwrite 再 publish）、source 列経路は `rollback` サブコマンド（記録済みの元値へ逆適用）、CreateNew draft は `cleanup_drafts.py`（ガード付き削除）。

## スコープ

含む:
- source 列 Catalog description の更新（resolve / apply / rollback）
- field description の注入・更新（通常列・calc 列とも。republish 経路）
- datasource レベルの description（grain）の設定
- calculated field の注入（caption / formula / datatype / role、任意で description）
- 編集後の round-trip 検証（再 DL・GraphQL 登録確認・desc カバレッジ・grain 一致・LUID 維持）
- draft PDS のガード付き削除（dry-run 既定）

含まない:
- 既存 calc / 列の削除、列の rename / hide / cast（将来拡張）
- formula の推論・naming 規約の自動生成（caller が change-set で明示提供）
- default aggregation / folder 構成
- workbook 側の編集

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
- [ ] `python scripts/tableau_auth.py status` で cached session を確認（`no cached session` なら先にサインインを依頼。ブラウザ待ちのハングを防ぐ）
- [ ] spec を読み、経路を判定：source 列の説明だけなら in-place、それ以外は republish
- [ ] （source 列経路）`resolve` で 1:1 対応と addressable を確認 → `apply`。verify は REST 直読で即時確定
- [ ] （republish 経路）spec の `mode`・`source_luid`・`target.new_name`（CreateNew 時必須）を検証。**calc を含む Overwrite だけ**は下流影響を提示し承認を取得
- [ ] `augment_datasource.py` を実行（download → 編集 → preflight / diff ゲート（desc-only 時）→ publish → 再 DL 検証）
- [ ] `result.json` の `verified` / `luid_preserved` / `preflight` / `diff_gate` / `roundtrip_checks` / `coverage` / `datasource_description_check` を確認
- [ ] 検証 NG なら out-dir の `edited.tds` / `verified.tds` を読み、原因（caption 不一致・formula 構文・survive せず）を切り分け。必要なら `--rollback` で巻き戻す

スクリプトは編集前 `original.tdsx` を必ず保存する（rollback 用）。

## XML 編集の要点

詳細は [references/tds-edit-format.md](references/tds-edit-format.md)。実装上の必須事項:

- **改行非依存で編集する**。`.tds` は CRLF のことがあり、改行に依存した anchor は壊れる。
- field の特定は **caption='X' または name='[X]'** の両対応。caption は display 名と内部名が違うときだけ存在し、等しいと省略される。
- description は `<column>` 子の `<desc><formatted-text><run>テキスト</run></formatted-text></desc>`。既存 desc は置換する。
- calc は `<aliases .../>` 直後に `<column><calculation class='tableau' formula='...'/></column>` を sibling として注入。formula は XML escape する。
- calc 注入は冪等性ガード付き：spec の caption が既存 column の表示名と衝突したら 1 件も注入せず停止し（重複注入防止）、内部名 `[Calculation_steward_N]` は既存最大 N の続番を振る。

## 検証の注意

- 検証の失敗は握り潰さず result に出す。
- desc・calc の survive は再 DL した `.tds` の round-trip が正。GraphQL（calc 登録・coverage）はカタログのインデックス依存で 1 テンポ遅れることがあり、`verified` の合否には含めない。MCP `get-datasource-metadata` も calc を formula / description 付きで列挙するが、同じくインデックス反映後に見えるため即時検証には使わない。
- **grain（datasource.description）は `.tds` に乗らない**カタログ属性。publish 後に `datasources.get_by_id(luid).description` を再 query して一致を検証する（`datasource_description_check`）。
- **カバレッジ**: GraphQL は論理テーブル自体を ColumnField として数える（名前が upstream テーブル名と一致し upstream 列を持たない擬似列）。coverage は擬似列を分母から除外し、除外分を `pseudo_table_fields_excluded` に出す。coverage は情報提供で `verified` の合否には含めない（部分整備は正当なユースケース）。
- source 列経路の verify は REST 直読で即時確定する。Catalog / MCP（`descriptionInherited`）への露出はインデックス反映後（目安 15〜60 秒）。

## 認証

OAuth 2.0 (Authorization Code + PKCE)。リポジトリ直下の `scripts/tableau_auth.py`（`signed_in_server()`）を共通モジュールとして import する。`.env` に `SERVER` / `SITE_NAME` を置く（テンプレートは `.env.template`）。

## 依存

`tableauserverclient` / `python-dotenv` / `requests`。

## Scripts

| スクリプト | 役割 |
|---|---|
| `scripts/augment_datasource.py` | spec を読み download → XML 編集（desc / calc 注入）→ publish → 再 DL 検証を一気通貫実行。desc-only Overwrite では preflight + diff ゲートを自動適用。`--rollback` で original.tdsx を再 publish。終了時に `RESULT_JSON` を emit し `result.json` を書く |
| `scripts/update_source_column_descs.py` | source 列の Catalog description を in-place 更新。`resolve`（1:1 対応と addressable の解決）/ `apply`（元値を記録して反映・REST 直読 verify）/ `rollback`（元値へ逆適用） |
| `scripts/cleanup_drafts.py` | CreateNew draft のガード付き削除。result.json の `published_luid` または LUID 指定を入力に、name 接尾辞 ∧ project の両ガードを通過したものだけ削除。既定 dry-run、実削除は `--execute` |
