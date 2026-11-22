---
purpose: steward が回す 3 つのワークフロー（説明の一括整備バッチ、BP の整備・点検、重複 calc の hoist）の手順
note: パスはプラグインのルートからの相対で書く（`scripts/...` は共通モジュール、`skills/<name>/scripts/...` は各 Skill の同梱スクリプト）。実行は `uv run <プラグインのルート>/<パス>`。出力先・報告の規範は SKILL.md を正とする
---

# ワークフロー

## 目次
- 列 desc を一括整備する（メタデータ整備バッチ）
- Knowledge の BP を整備・点検する
- 重複 calc を寄せて説明も付ける

## 列 desc を一括整備する（メタデータ整備バッチ）

ユーザーが対象範囲（project / PDS 名）を指定して回す自走バッチ。発見性（MCP / VDS が読む description）の整備が目的。

1. **コスト見積もりゲート**: `scripts/batch_augment.py estimate` で per-PDS の「source 列経路で埋まる数 / republish 必至の数 / extract 総量」を算出する。`heavy` なら「全部やると大変」を提示して対象の絞り直しを促す
2. `datasource-inspector`（`read_schema.py`）→ `datasource-describer` で説明草案を作り、**source 列経路の updates** と **republish 経路の change-set** に振り分ける（1:1 対応は inspector の `upstream_1to1` と augmenter の `resolve` が判定）
3. 書き込み（**バッチ実行の宣言 1 回で自走。per-PDS の事前確認は置かない**）:
   - source 列 desc → `update_source_column_descs.py apply`（in-place。PDS を作らない・触らない）
   - field desc / grain → `scripts/batch_augment.py run`（desc-only Overwrite。preflight + diff ゲート + LUID 検証を各 PDS で機械適用、manifest で再開可能）
   - 書くのは未設定を埋めた `descriptions[]` / `datasource.description` だけ。既存の説明を直す草案（`audits[]` の `stale` / `off_format` / `misplaced`）は書かず、報告で承認を得てから次のバッチで `descriptions[]` に移して書く
4. **成果物レビュー面の生成**: 反映後に `scripts/metadata_report.py --projects <対象>` で**ライブカタログを読み直した** HTML レポート（per-PDS の grain + 全列 description + coverage 集計）を生成し、報告に添える。書いた spec ではなく、読み直した実物を示す
5. **orchestrator 報告**: SKILL.md の「報告の規範」に従う

## Knowledge の BP を整備・点検する

1. `knowledge-curator`（台帳モード）で BP を台帳に足す・直す。説明・calc で表せる文は BP にしない。describer の `bp_candidates[]` は、報告でユーザーが採否を決めたものだけを足す
2. `knowledge-curator`（同期モード）で Knowledge のスナップショットと 3 方向比較し、create / update を `manage-knowledge-context` で反映する。`conflict` / `orphan` は報告に回す
3. **データソースの発行・上書きの直後**（augmenter の Overwrite・promote の後）と**定期実行**で、点検モードを回す。改名・削除で切れた参照を修正案付きで報告する
4. `misplaced` で説明から使い方の文を消すのは、行き先の BP が同期後のスナップショットに入ったことを確かめてから

## 重複 calc を寄せて説明も付ける

下流 WB の共通 calc を PDS に集約し、その calc の description まで一度に埋める。CreateNew の draft 上で非破壊に検証できるため、**推論を含めて一気に埋め、成果物を後でレビュー**する自走型で回す。

1. `workbook-calc-prospector` → hoist 候補（`wb_descriptions[]` / コメント込み `formula` 付き）
2. `datasource-describer` → 各 calc の `description` を生成し、出所（`extracted` / `inferred`）と衝突（`conflict` / `variants`）を付与
3. `datasource-augmenter` → `CreateNew` で別名 PDS に注入・publish・round-trip 検証
4. `workbook-rewirer` → 各下流 WB を `CreateNew` で rewired 版として publish（augmented PDS へ付け替え + calc 参照差し替え）し、前後 view の画像並置比較まで実行（**ここまで承認プロンプトなしで自走**）
5. **orchestrator 報告**でユーザー確認を提示する。SKILL.md の「報告の規範」に加えて次を載せる:
   - 公開した PDS / rewired WB の `name` / `published_luid` / project と**破棄手順**（いずれも CreateNew なので破棄は 1 操作ずつ）
   - rewirer の `view_compare` の結果と並置レポート（`compare/view-compare.html`）への参照。export 失敗した view は原文エラー併記で先頭に、前後画像の目視所見も添える
   - `conflict: true` の calc は原文（`variants`）併記で先頭に並べる

この報告で承認が得られたら、本番への反映に進む: PDS の promote（`Overwrite`）→ `workbook-rewirer` の掃除モード（本番 PDS を `pds.luid` に指定。repoint なしの calc 差し替え）で本番 WB を `Overwrite`。CreateNew はビットを守るが**ユーザーの注意は守らない**ため、報告の legibility がこのワークフローの実質的な安全装置。
