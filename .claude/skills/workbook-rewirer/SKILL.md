---
name: workbook-rewirer
description: calc hoist 後の下流 workbook を書き換えて PDS 側の calculated field を使わせる write エンジン。workbook の published datasource 接続を augmented PDS に付け替え（repoint）、ローカル calc の定義を削除して参照を PDS 側 calc に差し替え、新規 workbook として publish（既定 CreateNew）した上で全 view の CSV エクスポートでエラーが無いか検証する。寄せた calc を workbook で実際に使わせたい、workbook のデータソースを付け替えたい、hoist 後の workbook 掃除と動作確認をしたいときに使う。prospector → augmenter の hoist ワークフローの後段。
---

# workbook-rewirer

PDS への calc hoist だけでは下流 workbook は何も変わらない。ローカル calc を持ち続け、PDS 側 calc は使われない。本 Skill は workbook を書き換えてこのループを閉じる: `download → .twb XML 編集（repoint + calc 参照差し替え）→ publish → view 描画検証`。

入力は prospector の hoist 候補と augmenter の publish 結果（augmented PDS）。これらを受けて動く最終段。

## publish ポリシー（重要）

- **既定は `mode: CreateNew`**（別名で新規 workbook を作る）。元 workbook は変更しない。命名は draft と分かる形（`…__rewired`）にする。
- `Overwrite` は元 workbook を破壊的に置換する。**明示要求 + ユーザー承認**がそろったときのみ。
- 自走ワークフロー内では CreateNew publish まで承認プロンプトなしで進めてよい。代わりに事後の orchestrator 報告で publish 物（name / luid / project）と破棄手順を必ず提示する。

## 2 つの使い方（repoint の有無）

| 場面 | `pds.luid` に渡すもの | repoint |
|---|---|---|
| **draft 検証**（augmented PDS で動作確認） | augmenter が publish した augmented PDS | する（スクリプトが接続先の差分を見て自動判断） |
| **promote 後の掃除**（本番 PDS に calc が入った後） | workbook が既に使っている本番 PDS | しない（calc 差し替えのみ） |

## スコープ

含む:
- published datasource 接続の付け替え（repository-location / caption / dbname。同一 site 内のみ）
- workbook ローカル calc の定義削除と、全参照（shelf / column-instance / 修飾参照）の PDS 側 calc への置換
- CreateNew publish（既定）/ Overwrite publish（明示時のみ）
- 検証: round-trip（再 DL で編集が survive）+ **全 view の CSV エクスポート**（サーバー側でクエリ実行させ、参照切れを露見させる）+ GraphQL 補助チェック

含まない:
- view レイアウト・filter・parameter の編集
- PDS 側の変更
- hoist 候補の検出・formula の意味的等価判定（本 Skill は spec で受けた formula の字句正規化一致でローカル calc を特定するだけ）
- embedded（非 published）datasource しか使わない workbook（付け替え先の published 接続が前提）

## 入力（rewire spec）

`rewire_workbook.py --spec spec.json --out-dir <dir>` で実行する。1 spec = 1 workbook。全フィールドは [references/rewire-spec-format.md](references/rewire-spec-format.md) を参照。最小例:

```json
{
  "workbook_luid": "WB_LUID_PLACEHOLDER",
  "pds": { "luid": "AUGMENTED_PDS_LUID_PLACEHOLDER" },
  "swaps": [
    { "formula": "SUM([Profit])/SUM([Sales])", "pds_calc_caption": "Profit Ratio" }
  ],
  "mode": "CreateNew",
  "target": { "new_name": "my_workbook__rewired" }
}
```

`swaps[].formula` は prospector の `candidates[].formula`、`pds_calc_caption` は augmenter change-set の `calcs[].caption` をそのまま使う。

## ワークフロー

進捗:
- [ ] prospector の候補（対象 workbook / formula）と augmenter の result（augmented PDS の luid / calc caption）から spec を作る
- [ ] `Overwrite` 指定時はユーザー承認を取得（CreateNew は自走可）
- [ ] `rewire_workbook.py` を実行（download → 編集 → publish → 検証）
- [ ] `result.json` の `verified` と `view_checks` / `roundtrip_checks` / `repoint` を確認
- [ ] `view_checks` にエラーがあれば `out-dir` の `original.twb` / `edited.twb` を diff し、参照切れ（token 置換漏れ）か repoint 不備（dbname / content_url）かを切り分け
- [ ] 対象 workbook が複数あるときは workbook ごとに spec を作って繰り返し、報告にまとめる

スクリプトは編集前の `original.twb(x)` を必ず保存する（revert 用）。

## XML 編集の要点

詳細は [references/twb-edit-format.md](references/twb-edit-format.md)。実装上の必須事項:

- **改行非依存で編集する**（.twb も CRLF のことがある）。
- ローカル calc の特定は caption でなく**正規化 formula の一致**（caption は workbook ごとに揺れる）。
- token 置換は区切り文字（`[` `]` `:`）に挟まれた token だけを対象にする。qualified な instance 名（`[none:token:qk]`）もこれでカバーされる。
- 定義削除とセットで、worksheet 側 dependency キャッシュの `<calculation>` 写しも削除する。

## 検証の注意

- **XML が正しくても field が解決するかは分からない**。view の CSV エクスポート（`view_checks`）が実行時テストの本体で、`verified` の合否に入る。
- GraphQL チェック（upstream / embedded calc）は Metadata API のインデックス遅延があるため補助扱い。`_note` が出たら後で再読して確認する。
- 検証の失敗は握り潰さず result に出す。

## 認証 / 依存

OAuth（`scripts/tableau_auth.py` の `signed_in_server()`）。GraphQL は `scripts/metadata_api.py`。依存: `tableauserverclient` / `python-dotenv` / `requests`。

## Scripts

| スクリプト | 役割 |
|---|---|
| `scripts/rewire_workbook.py` | spec を読み download → .twb 編集（repoint + calc 差し替え）→ publish → view 描画検証を一気通貫実行。終了時に `RESULT_JSON` を emit し `result.json` を書く |
