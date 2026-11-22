---
purpose: rewire_workbook.py の入出力契約（spec / result.json / rollback.json）の全フィールド定義
note: 例の値はすべてダミー。1 spec = 1 workbook。複数 workbook を書き換えるときは workbook ごとに spec を作って個別に実行する
---

# rewire spec フォーマット

```json
{
  "workbook_luid": "WB_LUID_PLACEHOLDER",
  "pds": { "luid": "AUGMENTED_PDS_LUID_PLACEHOLDER" },
  "source_pds_luid": "ORIGINAL_PDS_LUID_PLACEHOLDER",
  "swaps": [
    {
      "formula": "SUM([Profit])/SUM([Sales])",
      "pds_calc_caption": "Profit Ratio"
    }
  ],
  "mode": "CreateNew",
  "target": { "new_name": "my_workbook__rewired", "project_id": "PROJECT_LUID_PLACEHOLDER" }
}
```

| フィールド | 必須 | 意味 |
|---|---|---|
| `workbook_luid` | ✅ | 書き換える workbook |
| `pds.luid` | ✅ | 参照させたい calc を持つ PDS（通常は augmenter が publish した augmented PDS。promote 済みなら本番 PDS） |
| `source_pds_luid` | △ | workbook 内で付け替える接続元 PDS。workbook が published datasource を 1 つしか使わないなら省略可。複数使う場合は必須 |
| `swaps[].formula` | ✅ | workbook ローカル calc を特定する formula（prospector の `candidates[].formula` をそのまま渡す。正規化一致で探すためコメント差は無視される） |
| `swaps[].pds_calc_caption` | ✅ | 差し替え先の PDS 側 calc の caption（augmenter change-set の `calcs[].caption`） |
| `mode` | — | `CreateNew`（既定。別名の workbook を新規 publish）/ `Overwrite`（元 workbook を置換。承認ゲート対象） |
| `target.new_name` | CreateNew 時 ✅ | 新規 workbook 名。draft と分かる命名（`…__rewired`）にする |
| `target.project_id` | — | publish 先 project。省略時は元 workbook の project |

## 出力（result.json）

| キー | 意味 |
|---|---|
| `published_luid` / `published_name` / `project_id` | publish された workbook（破棄手順の提示に使う） |
| `swaps[]` | 各差し替えの `old_token` / `new_token` / `refs_replaced`（参照置換数）/ `dependency_calcs_stripped` |
| `repoint` | 付け替えの実施有無と各置換件数（`id_replaced` / `caption_updated` / `dbname_updated`） |
| `roundtrip_checks` | 再 DL した .twb での旧 token 消失・新 token / content_url 残存 |
| `view_compare` | view ごとの前後 render 結果（`views[]` の verdict: `ok` / `candidate_export_failed` / `baseline_export_failed` / `export_failed` / `only_in_one_workbook`）と集計 `tally`。画像並置は `compare/view-compare.html` |
| `graphql_checks` | 補助チェック（upstream / embedded calc）。インデックス遅延時は `_note` |
| `verified` | `roundtrip_checks` 全通過 かつ `view_compare` にブロック verdict（`candidate_export_failed` / `export_failed` / `only_in_one_workbook`）なし。画像内容の同値は目視確認の領分で合否に含めない |

## 巻き戻し（rollback.json）

通常実行は out-dir に原本 `original.twb(x)` と `rollback.json` を保全する。フィールドは `workbook_luid`（rewire 元 workbook）/ `name` / `project_id` / `show_tabs`（いずれも実行時点の値）/ `original_file`（保存された原本のファイル名。.twb か .twbx）。

`rewire_workbook.py --rollback --out-dir <dir>` はこれを読み、原本を元の name / project へ Overwrite 再 publish する（`--spec` 不要）。LUID 維持（`published.id == workbook_luid`）を検証し、不一致なら exit 2。出力は `RESULT_JSON:` 行（`phase: "rollback"` / `published_luid` / `luid_preserved`）。
