---
name: datasource-describer
description: Published Data Source の列・calculated field・datasource 自体（grain）の説明文（description）の草案を作り、datasource-augmenter 用の change-set に整える Skill。列名・型・サンプル値・formula・WB 側 description を手がかりに未設定の説明を埋め、各説明に出所（extracted / inferred）と衝突フラグを付ける。加えて inspector が拾った既存 desc・grain が現在のデータに対して適切かを検証（valid / stale / unverifiable）し、乖離は修正草案にする。列の説明を埋めたい・データ辞書を整備したい・grain を記述したい・寄せた calc に説明を付けたい・既存の説明が古くないか点検したいときに使う。注入そのものは augmenter が行う。
---

# datasource-describer

PDS の列・calc・datasource 自体（grain）の description 草案を作り、既存 desc の妥当性も検証する分析 Skill。読取・推論のみ（注入は `datasource-augmenter`）。担う仕事は3つ:

1. **未設定を埋める**: description が無い列・calc・grain に草案を作る
2. **grain を書く**: datasource が「何の 1 行か」のステートメント草案を作る
3. **既存を検証する**: inspector が拾った既存 desc・grain が現在のデータに対して適切かを判定し、乖離は修正草案にする

各説明に**出所（provenance）**を付け、自動解決できない衝突・stale 判定は orchestrator の報告でユーザー確認に回す。

## 入力（2 系統）

- **列の説明・grain・既存 desc**: `datasource-inspector` の棚卸しレポート（列一覧・型・gap・**既存 desc 全文**・**grain の現在値**）。無ければ先に inspector を実行する。未設定は「埋める」対象、既存 desc・grain は「検証する」対象。
- **hoist calc の説明**: `workbook-calc-prospector` の `candidates.json`（`wb_descriptions[]` / コメント込み `formula`）。calc 本体（caption / formula / datatype）は prospector + augmenter が持ち、本 Skill は **`description` と provenance だけ**を足す。

## ワークフロー（列の説明）

進捗:
- [ ] inspector のレポートから description 未設定の列を抽出
- [ ] 各列について、列名・型・role から説明を起こせるか判断
- [ ] 列名だけで意味が曖昧な列は `tableau:query-datasource` でサンプル値を確認（dimension は distinct 上位、measure は MIN/MAX/AVG 等の集計で代表値）
- [ ] 「description の品質規範」に沿って簡潔で一貫した草案を作る。業務語彙・命名規則が不明でも途中でユーザー確認に止まらず、`source: inferred` として orchestrator 報告に回す
- [ ] 各説明に provenance（`source` / 衝突時は `variants`）を付ける
- [ ] `datasource-augmenter` の change-set `descriptions[]` に整えて引き渡す

## ワークフロー（hoist calc の説明）

prospector の各 candidate の `wb_descriptions[]` の件数で分岐する：

- [ ] **0 件** → `formula`（コメント含む）から説明を推論。コメントは「計算の説明か / TODO・dead code 等のノイズか」を判定し、説明に資する部分だけ使う。`source: inferred`
- [ ] **1 件** → その description をほぼそのまま採用（文体だけ品質規範に合わせる）。`source: extracted`
- [ ] **2 件以上** → 内容が整合するなら 1 文に reconcile（`source: extracted`、`variants` に原文を残す）。整合しない（相反する）なら自動マージせず、reconcile 草案 + 原文を `conflict: true` で残し、**orchestrator 報告でユーザー確認に回す**
- [ ] calc 本体（caption / formula / datatype）には触れない。`description` と provenance のみ付与
- [ ] augmenter の `calcs[]` 各要素の `description` として引き渡す

## ワークフロー（grain の草案）

datasource が「何の 1 行か」を 1〜2 文で書く。列 desc とは別レイヤー（テーブル全体の意味）。

進捗:
- [ ] inspector の grain 現在値を確認（未設定なら草案を作る、設定済みなら検証ワークフローへ）
- [ ] スキーマ（キー列・粒度を示す列）と、必要なら `tableau:query-datasource` の行数特性から「1 行 = 何か」を推定
- [ ] 粒度・主キー相当・対象範囲を明示した grain 文を作る（例「注文明細行が 1 行。1 行 = 1 商品 × 1 注文」）
- [ ] change-set の `datasource.description` として引き渡す（provenance は `source`）

## ワークフロー（既存 desc・grain の検証）

inspector が拾った**既存の** desc・grain が、現在のデータに対して今も正しいかを点検する（陳腐化の検出）。未設定を埋める作業とは別。

進捗:
- [ ] 既存 desc・grain を「主張」に分解する：取りうる値の集合 / 単位・集計 / 粒度（grain）/ 派生元・境界条件
- [ ] 主張ごとに、照合が必要なものだけ現データと突き合わせる：
  - 列挙値の主張 → `tableau:query-datasource` の distinct 上位と照合
  - 単位・集計の主張 → default aggregation・サンプルの桁/符号と照合
  - grain の主張 → キー列の distinct・行数特性と照合
- [ ] 各 desc に verdict を付ける：`valid`（現データと整合）/ `stale`（乖離あり。乖離内容と修正草案を付す）/ `unverifiable`（現データからは確認できない。理由を付す）
- [ ] `stale` は修正草案を `descriptions[]`（grain は `datasource.description`）に載せ、原文を `previous_text` に残す。判断は orchestrator 報告でユーザー確認に回す
- [ ] コスト管理は「サンプル値の使い方」に従う（行レベル取得を避ける）

## カバレッジ（取りこぼしを防ぐ）

「未 desc 列ゼロ」を目標にするが、機械的に全列へ文を書くと同義反復（列名の言い換え）を量産し品質規範に反する。したがって**全列を「処理済み」にする**ことを保証し、文を書けない列は捨てずに残す。

- gap の各列について、情報量のある desc を書けたものは `descriptions[]`、真に情報を足せない列（不透明 ID 等）は `skipped[]` に理由付きで残す
- 完了条件（検算）：`len(新規 descriptions) + len(skipped) == inspector の gap 列数`。未処理列が無いことを確認する

## サンプル値の使い方

- 主たる手がかりは列名・型・既存メタ。サンプル値は「列名だけでは意味が不明」なときの補助。
- コスト管理: 行レベル取得は避け、distinct + limit や集計を使う（query-datasource の TOP / 集計を活用）。

## 出力（change-set 候補）

列の説明は `descriptions[]`、hoist calc の説明は `calcs[]` 各要素の `description`、grain は `datasource.description` に載せる。検証結果は `audits[]`、書けなかった列は `skipped[]` に残す。各説明に provenance を添える：

```json
{
  "datasource": { "description": "注文明細行が 1 行。1 行 = 1 商品 × 1 注文。", "source": "inferred" },
  "descriptions": [
    { "field_caption": "Segment", "text": "顧客セグメント（Consumer / Corporate / Home Office）",
      "source": "inferred" }
  ],
  "calcs": [
    { "caption": "Profit Ratio", "description": "利益率（割引後利益 / 売上）",
      "source": "extracted", "conflict": false,
      "variants": ["利益率", "Profit / Sales の比率"] }
  ],
  "audits": [
    { "field_caption": "Segment", "verdict": "stale",
      "claim": "取りうる値は Consumer / Corporate / Home Office",
      "observed": "distinct に Small Business が追加されている",
      "previous_text": "顧客セグメント（Consumer / Corporate / Home Office）",
      "revised_text": "顧客セグメント（Consumer / Corporate / Home Office / Small Business）" }
  ],
  "skipped": [
    { "field_caption": "ID_売付", "reason": "不透明な内部 ID。列名・型・サンプルから業務的意味を起こせない" }
  ]
}
```

`stale` の `revised_text` は `descriptions[]`（grain なら `datasource.description`）にも載せ、augmenter がそれを注入する。`audits[]` / `skipped[]` と provenance フィールド（`source` / `conflict` / `variants` / `previous_text`）は **orchestrator がユーザー報告で「抽出 / 推論」「衝突」「stale の原文」を区別するための注記**。augmenter は自身が定義したキーしか読まないため、これらの注記は change-set にそのまま残してよい（キーの正典は augmenter の references/change-set-format.md）。

## description の品質規範

dbt Semantic Layer の指針（grain / 単位 / 包含 / 取りうる値を明示し、列名の同義反復を避ける）を列レベルに適用する。

- 言い換え禁止：列名の和訳だけ（`Order Date` →「注文日」）は情報量ゼロ。列名だけでは分からないこと（基準時点・タイムゾーン・null の意味）を書く。
- measure は単位・集計・包含を必須：通貨/個数、税抜・割引後か、inspector が拾う default aggregation と整合させる。
- dimension は取りうる値の集合を示す：列名で曖昧なものだけ `tableau:query-datasource` で distinct 上位を確認して列挙する。
- calc 列は派生元と境界条件（しきい値・case 分岐）を書く。
- 文体を統一：体言止めか「〜を表す」に揃え、主語を省き 1〜2 文。change-set 内で文体を揃えてから augmenter に渡す。

grain（テーブル全体が何の 1 行か）は datasource レベルの description であり列単位ではない。列 desc とは別レイヤーとして「grain の草案」ワークフローで扱い、粒度・主キー相当・対象範囲を明示する（列名の言い換えにしない）。注入は augmenter が publish 時に行う。

## 認証 / 依存

サンプル値取得は Tableau MCP `tableau:query-datasource`（`.mcp.json`、実値は gitignore）。スキーマ参照は inspector 経由。
