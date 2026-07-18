---
purpose: rewire_workbook.py に渡す spec (JSON) の全フィールド定義
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
| `view_checks` | view ごとの CSV エクスポート結果（`ok` / `error: ...`） |
| `graphql_checks` | 補助チェック（upstream / embedded calc）。インデックス遅延時は `_note` |
| `verified` | `roundtrip_checks` 全通過 かつ `view_checks` 全 `ok` |
