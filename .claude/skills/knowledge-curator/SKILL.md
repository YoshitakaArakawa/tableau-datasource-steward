---
name: knowledge-curator
description: Tableau Knowledge の Business Preference（BP、組織で共有する「どう使うか」の決まり）を整備する Skill。BP 台帳（英文・訳・理由・依存するフィールド名）を管理し、Tableau MCP の manage-knowledge-context で作成・更新し、BP と PDS メタデータの間の参照切れ（改名・削除されたデータソース名・フィールド名）と二重定義（BP が説明を言い換えている）を点検する。Business Preference を登録・更新したい、BP 台帳を Knowledge と同期したい、データソースの発行・上書き後に BP の参照が切れていないか点検したい、説明と BP の重複を見つけたいときに使う。
---

# knowledge-curator

Tableau Knowledge の Business Preference（BP）を、台帳・同期・点検の3モードで整備する。扱う BP は global スコープだけ（現行の Knowledge は global の BP しか設定できない）。BP はコンテキスト層（どう使うか）、PDS の構造と説明はメタデータ層（それは何か）。BP はメタデータを名前で参照するので、名前の変更で黙って壊れる。この Skill はその参照を守る。

**最優先の規範**:
- BP の文面の正本は Knowledge。台帳は訳・理由・依存を添えた写しで、同期のたびに Knowledge と3方向比較する。
- Knowledge への書き込みは Tableau MCP の `manage-knowledge-context` だけで行う。REST や独自クライアントで書かない。
- create / update は、バッチ実行の宣言に含めて自走してよい。ただし点検（`check`）で error / unknown が0件の項目だけを書く。Knowledge には draft が無く、書いた時点で全員に効くため、この機械ゲートを外さない。
- BP の削除（`action: delete`）は、対象の statement と contextId を示してユーザーの承認を得てから行う。
- 書き込みの前に必ず Knowledge のスナップショットを保存する。これが取り消しの基準になる。
- BP の書式（名前のバッククォート・1件1判断・kind）は [references/bp-format.md](references/bp-format.md) に従う。

## 入力

- **台帳**: 利用者のリポジトリにある BP 台帳 JSON（スキーマは bp-format）。無ければ、スナップショットから `knowledge_ledger.py init --graph-id <読んだグラフの id>` で作る。
- **BP 候補**: datasource-describer の change-set の `bp_candidates[]`。台帳の item と同じ形なので、そのまま台帳に足せる。

## モード1: 台帳に足す・直す

進捗:
- [ ] 足す文が BP の4種（bp-format の「BP に置くもの」）に当たるか確かめる。説明・calc で表せるものは BP にせず、理由を付けて報告に回す
- [ ] bp-format の書式に整える（英語の statement、`in` で修飾したバッククォートの名前、`name` の kind 接頭辞）
- [ ] 訳（`translation`）と理由（`rationale`）を添えて台帳に入れる
- [ ] `name` が台帳の既存項目と重なったら、bp-format の「名前の衝突」に従う（同じ文面なら足さない。違う文面なら名前を区別する）
- [ ] `python ${CLAUDE_SKILL_DIR}/scripts/knowledge_ledger.py refs --ledger <台帳> --write-back` で `depends_on` を更新する
- [ ] モード3の点検を台帳に対して回し、error が無いことを確かめてから同期する

## モード2: Knowledge と同期する

書き込みはこのモードだけで行う。

進捗:
- [ ] `inspect-knowledge-context`（`action: status`）で対象グラフの id を確かめ、台帳の `graph_id` と一致することを確認する。以降の `inspect-knowledge-context` / `manage-knowledge-context` の呼び出しには、すべて `graphId` にこの id を渡す。`graphId` を省くと primary グラフが読み書きされるため、primary を使うときも id を明示する。`plan` は `graph_id` の無い台帳を拒否する
- [ ] `inspect-knowledge-context`（`action: list`, `limit: 100`）で全 statement を読み、応答の JSON をそのまま `work/<yyyymmdd>_<tag>/knowledge_snapshot_before.json` に保存する（既存ファイルを上書きしない）。応答は statement 1 件が 1 行で、`plan` が `contextId` ごとにまとめる。`resultInfo.truncated` が true なら同期しない（`plan` も拒否する。100 件を超えるグラフは現状扱えない）
- [ ] モード3の `check` を台帳に対して、同期の直前にオンラインで回し、`check.json` を作る（`plan` は、カタログを読んでから 24 時間を超えた点検結果では書き込まない）
- [ ] `knowledge_ledger.py plan --ledger <台帳> --snapshot <before> [--snapshot ...] --check <check.json> --out <plan.json>` で計画を作る
- [ ] 計画の action ごとに処理する:
  - `create`: `manage-knowledge-context`（`action: create`, `name`, `statements: [{statement}, ...]`, `isGlobal: true`）
  - `update`: `manage-knowledge-context`（`action: update`, `contextId`）。`fields` に応じて `statements` / `name`（`new_name` の値）を渡す。`statements` は全件置換で、statementId は全件振り直される（文面が同じ statement も新しい id になる）。`statements` を省けば文面は保たれる
  - `pull`: 書き込まない。Knowledge 側の編集（`fields`）を台帳に取り込む（write-back で反映）
  - `blocked`: 書き込まない。点検の error / unknown を直してから次回に回す
  - `conflict` / `recreate` / `orphan` / `invalid`: 書き込まない。報告で判断を仰ぐ。Knowledge は同じ name の context を拒否しないので、create や改名で name が既存 context と重なる項目も `conflict` になる。`orphan` の削除は承認後だけ。`invalid` は Knowledge 側の名前か文面が空の context
- [ ] 書き込みの後にもう一度 `list` して `knowledge_snapshot_after.json` に保存し、`plan --ledger <台帳> --snapshot <after> --check <check.json> --out <plan_after.json> --write-back` で台帳に `context_id` と同期済みの値（`synced_*`）を記録する。このとき create / update した項目が `unchanged` になっていることを確かめる。`pull` で文面を取り込んだ項目は、次回の `plan` の前に `check` をやり直す（文面が変わったので、やり直すまで `blocked` になる）
- [ ] 台帳の変更は利用者のリポジトリ側の差分として残す（commit は利用者が判断する）

## モード3: 点検する（読取専用）

データソースの発行・上書きの直後と、定期実行で回す。

進捗:
- [ ] `python scripts/tableau_auth.py status`（リポジトリ直下）で cached session を確認する
- [ ] `knowledge_ledger.py check --ledger <台帳> --save-catalog work/<yyyymmdd>_<tag>/catalog.json --out <check.json>` を実行する。台帳が無ければ、モード2の手順でスナップショットを取り、`init` で台帳を作ってから回す
- [ ] error（`missing_datasource` / `missing_field` / `unresolved`）は、改名先の候補を Metadata API の結果か `query-knowledge-context`（`intent: ground` のノード検索）で探し、修正案を付ける
- [ ] unknown（`unknown_fields`）は、保存済みカタログにフィールドが無く判定できなかったもの。オンラインで再実行する
- [ ] warning は bp-format の「点検結果の意味」に従って直し方を付ける。`ambiguous_field` は、どちらの列を指すつもりかを文脈から推定して示す
- [ ] 二重定義を判定する: 各 item の `field_descriptions`（参照先の現在の説明）と statement を突き合わせ、次のどちらかに当たれば `duplicate_definition` として報告する
  - BP が説明の内容（それは何か）を言い換えている → BP から削る
  - 説明が BP の内容（どう使うか）を含んでいる → 説明から削る（その修正は説明の検証の対象）
- [ ] 修正は台帳に反映してからモード2で同期する。点検モード自体は Knowledge にも PDS にも書かない

## 出力と報告

- `work/<yyyymmdd>_<tag>/` に `knowledge_snapshot_before.json` / `knowledge_snapshot_after.json` / `plan.json` / `check.json` / `catalog.json` を置く。
- 報告の本文は判断点だけを書く。順序は次のとおり:
  1. error の参照切れと修正案、`blocked` になった項目
  2. `conflict` / `recreate` / `orphan`
  3. 二重定義
  4. 書き込んだ件数（create / update）と、それぞれの contextId
- 取り消しの基準は `knowledge_snapshot_before.json`。create したものは `delete`（承認必須）、update したものは before の name / statements に戻す `update`。

## 認証 / 依存

- Knowledge の読み書き: Tableau MCP（`inspect-knowledge-context` / `manage-knowledge-context` / `query-knowledge-context`）。Tableau+ が要る。接続は環境側で用意する。
- 点検のカタログ読取: OAuth（リポジトリ直下 `scripts/tableau_auth.py`）+ Metadata API（`scripts/metadata_api.py`）。依存は `tableauserverclient` / `python-dotenv` / `requests`。
- `--catalog` に保存済みのカタログを渡せば、点検はオフラインで再実行できる。

## Scripts

| スクリプト | 役割 |
|---|---|
| `scripts/knowledge_ledger.py` | `init`（スナップショットから台帳を作る）/ `refs`（参照名を抜いて `depends_on` を更新）/ `check`（参照切れ・曖昧・未引用の判定と、参照先の説明の収集）/ `plan`（台帳と Knowledge スナップショットの、文面・名前の3方向比較による同期計画。`--check` で error のある項目を `blocked` にする） |
