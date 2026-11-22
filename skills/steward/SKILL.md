---
name: steward
description: tableau-datasource-steward の入口 Skill。Tableau の Published Data Source（PDS）のメタデータ整備・calc 集約・Tableau Knowledge の Business Preference 整備について、どの Skill をどの順で使うかを決め、複数 Skill をまたぐワークフロー（説明の一括整備バッチ、重複 calc の hoist、BP の同期と点検）を回し、書き込みの承認ゲートと事後報告の規範を適用する。PDS の説明や grain をまとめて整備したい、データソースのメタデータ整備を一通り回したい、calc を PDS に寄せて workbook まで付け替えたい、どの Skill を使えばよいか分からないとき、steward の Skill を実行する前の準備（.env・uv・サインイン・出力先）を確かめたいときに使う。
---

# steward

PDS をセマンティックレイヤーとして継続メンテする Skill 群の入口。ルーティング・横断ポリシー・ワークフローの正本はこの Skill にある。

**最優先の規範**:
- 破壊的操作の前は必ずユーザーの承認を取る。対象は、内容変更を伴う Overwrite（PDS / workbook）、promote、PDS・workbook の削除、Knowledge の context の削除。
- 非破壊〜準非破壊の書き込み（CreateNew・desc-only Overwrite・source 列 desc・BP の create / update）は、バッチ実行の宣言 1 回で自走してよい。代わりに、事後報告で変更対象（name / luid / project）・rollback / 破棄手順・抽出 / 推論の区別を必ず示す。
- 中間成果物は「出力先」の規約に従って置く。プラグインのディレクトリには書かない。

## Skill マップ

| Skill | 系統 | 責務 |
|---|---|---|
| `datasource-inspector` | READ | PDS のスキーマ・メタデータ・既存 calc を GraphQL 1 クエリで棚卸しし、整備 gap を洗い出す |
| `datasource-describer` | ANALYZE | 列・calc・grain の説明草案と既存 desc の検証（valid / stale / off_format / misplaced）。使い方の文は BP 候補、式にできる比べ方は calc 候補に振り分ける |
| `workbook-calc-prospector` | ANALYZE | 下流 workbook の重複 calc を検出し hoist 候補を出す |
| `datasource-augmenter` | WRITE | change-set を受けて PDS メタデータを書き込み、機械検証する |
| `workbook-rewirer` | WRITE | hoist 後の workbook を書き換え（PDS 付け替え + calc 参照差し替え）→ publish → view 描画検証 |
| `knowledge-curator` | WRITE | Knowledge の BP（global のみ）の台帳管理・同期と、参照切れ・二重定義の点検 |

データの流れ: inspector / describer / prospector（読取・提案、副作用なし）→ change-set → augmenter（PDS）→ rewirer（workbook）。describer の `bp_candidates[]` は、ユーザーが採否を決めたものだけ knowledge-curator（Knowledge）へ渡す。write はこの 3 Skill のみ。

## ルーティング

- 現状を見たい / どの列に説明が無いか → `datasource-inspector`
- 列の説明を埋めたい / データ辞書 / grain を書きたい → `datasource-describer`（必要なら先に inspector）
- 既存の説明が現データに合っているか点検したい → `datasource-describer`（検証モード。inspector が既存 desc・grain を渡す）
- 説明に紛れた使い方を切り分けたい → `datasource-describer`（検証モードの `misplaced`）
- BP を登録・更新したい / BP 台帳を Knowledge と同期したい → `knowledge-curator`
- BP の参照切れ・説明との二重定義を点検したい → `knowledge-curator`（点検モード）
- 共通の計算を PDS に寄せたい / 重複 calc を探す → `workbook-calc-prospector`
- 説明 / calc を実際に PDS へ反映したい → `datasource-augmenter`
- 寄せた calc を workbook で使わせたい / workbook のデータソースを付け替えたい → `workbook-rewirer`
- 列の説明を一括で整備したい / 重複 calc を寄せて説明も付けたい / BP を整備・点検したい → [references/workflows.md](references/workflows.md) の該当ワークフロー

## 実行前の準備

進捗:
- [ ] 呼び出し側プロジェクトの作業ディレクトリ（またはその親）に `.env` があり、`SERVER` / `SITE_NAME` が入っているか確かめる。無ければ、テンプレート `${CLAUDE_PLUGIN_ROOT}/.env.template` をコピーして実値を入れるよう依頼する
- [ ] `uv` が使えるか確かめる（`uv --version`）。同梱スクリプトはすべて `uv run <script>` で実行し、依存はスクリプト冒頭の inline metadata から自動で入る
- [ ] `uv run ${CLAUDE_PLUGIN_ROOT}/scripts/tableau_auth.py status` でサインイン済みか確かめる。`no cached session` なら、ブラウザでのサインインを先に依頼する（ブラウザ待ちでハングさせない）
- [ ] 出力先を決めて宣言する（下記）

パスの読み替え: このプラグインのルートは `${CLAUDE_PLUGIN_ROOT}`。references や各 Skill の表にある `scripts/...`（共通モジュール）はこのルート直下、`skills/<name>/scripts/...` は各 Skill の同梱スクリプトを指す。

## 出力先（中間成果物の置き場）

- 呼び出し時にユーザーが置き場を指定したら、そこを使う。
- 指定が無ければ、呼び出し側の作業ディレクトリに `work/<yyyymmdd>_<tag>/` を作る。日付は作業開始日、`<tag>` は作業内容の短い要約。作業の最初にこの場所を宣言する。
- 置き場が git 管理下にあれば `git check-ignore -q <dir>` で無視対象か確かめる。無視対象でなければ、DL した .tdsx / .twbx（接続情報を含みうる）や実 LUID が commit されうると警告し、`.gitignore` への追加を提案する。
- 各 Skill のスクリプトの `--out` / `--out-dir` / `--out-root` には、この置き場の下のパスを渡す。

## 報告の規範

ワークフローの最後に orchestrator 報告をまとめる。本文は要約と判断点だけにし、全 luid・全変更の一覧は出力先にファイルで出す。

- 書き込みの集計（verified / gate_failed / abort と理由）
- 変更対象（name / luid / project）と rollback / 破棄手順
- 説明の出所（`extracted` / `inferred`）。confidence の低い `inferred` と `conflict: true` を先頭に置き、追認にならないよう確認対象を絞る
- 承認待ちの修正草案（`audits[]` の原文・修正案・`moved_to`）、BP 候補・calc 候補の件数と代表例
- レビュー面（ライブカタログを読み直したレポート）への参照

折りたたみ目的で HTML `<details>` を使わない（ターミナルの Markdown では描画されない）。

## 参照

- [references/workflows.md](references/workflows.md): 3 つのワークフロー（説明の一括整備バッチ、BP の整備・点検、重複 calc の hoist）の手順
- [references/policies.md](references/policies.md): メタデータとコンテキストの分離、書き込み方式と承認の対応表、認証と MCP の前提
