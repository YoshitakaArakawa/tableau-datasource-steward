# tableau-datasource-steward — オーケストレーション指針

このリポジトリは、Tableau Server / Tableau Cloud 上の **Published Data Source (PDS)** を
セマンティックレイヤーとして継続メンテする Agent。中核ロジックは `.claude/skills/` の複数 Skill に分かれ、
本ファイルが orchestrator（ルーティングと横断ポリシー）を担う。

## Skill マップ

| Skill | 系統 | 責務 |
|---|---|---|
| `datasource-inspector` | READ | PDS のスキーマ・メタデータ・既存 calc を棚卸しし、整備 gap を洗い出す |
| `datasource-column-describer` | ANALYZE | 列 / hoist calc の field description 草案を作り、出所（extracted / inferred）付きで change-set 化 |
| `workbook-calc-prospector` | ANALYZE | 下流 workbook の重複 calc を検出し hoist 候補（+ WB 側 desc・コメント込み formula）を出す |
| `datasource-augmenter` | WRITE | change-set を受けて download → XML 編集（desc / calc 注入）→ publish → 検証 |

データの流れ: inspector / describer / prospector（読取・提案、副作用なし）→ change-set → **augmenter（唯一の write）**。

## ルーティング

- 「現状を見たい / どの列に説明が無いか」→ `datasource-inspector`
- 「列の説明を埋めたい / データ辞書」→ `datasource-column-describer`（必要なら先に inspector）
- 「共通の計算を PDS に寄せたい / 重複 calc を探す」→ `workbook-calc-prospector`
- 「説明 / calc を実際に PDS へ反映したい」→ `datasource-augmenter`

Skill の作成・変更時は、着手前に `creating-skills` を参照する。

## ワークフロー：重複 calc を寄せて説明も付ける

下流 WB の共通 calc を PDS に集約し、その calc の description まで一度に埋めるユースケース。列メタ整備（CreateNew）は非破壊で低コストなため、**推論を含めて一気に埋め、成果物を後でレビュー**する自走型で回す。

1. `workbook-calc-prospector` → hoist 候補（`wb_descriptions[]` / コメント込み `formula` 付き）
2. `datasource-column-describer` → 各 calc の `description` を生成し、出所（`extracted` / `inferred`）と衝突（`conflict` / `variants`）を付与
3. `datasource-augmenter` → `CreateNew` で別名 PDS に注入・publish・round-trip 検証（**ここまで承認プロンプトなしで自走**）
4. **orchestrator 報告**（このワークフローの最後）でユーザー確認を提示：
   - 公開した PDS の `name` / `published_luid` / project と**破棄手順**（CreateNew なので破棄は 1 操作）
   - 説明の**抽出 / 推論の区別**、`conflict: true` の calc は原文（`variants`）併記で先頭に並べる
   - confidence の低い `inferred` を上位に置き、追認（rubber-stamp）にならないよう確認対象を絞る

この報告で承認が得られたら、本番 PDS への反映（promote / `Overwrite`）に進む。CreateNew はビットを守るが**ユーザーの注意は守らない**ため、報告の legibility がこのワークフローの実質的な安全装置。

## 横断ポリシー

### publish は既定 CreateNew
- `datasource-augmenter` の publish は **既定 `CreateNew`**（別名で新規 PDS を作り、元を壊さない）。
- `Overwrite` は破壊的（下流 workbook を巻き込みうる）。**明示要求 + 下流影響の提示 + ユーザー承認**がそろったときのみ。
- steward は「更新案 PDS を作る」まで。本番 PDS の置換はユーザーの意図的操作。

### 承認ゲート
- 破壊的操作（**Overwrite publish、PDS 削除、promote**）の前は必ずユーザー確認。真のゲートはここ。
- `CreateNew`・読取・change-set 生成は非破壊。メタ整備の自走ユースケース（上記ワークフロー）では**事前プロンプトなしで publish してよい**。代わりに事後の orchestrator 報告で、公開物（name / luid / project）と破棄手順、抽出/推論の区別を必ず提示する（confirm は write の前ではなくワークフロー末尾に束ねる）。
- 自走時も draft と分かる命名（例 `…__augmented`）を使い、第三者が正式版と誤認しないようにする。

### 認証
- Skill から Tableau API を叩くときは **OAuth**（`scripts/tableau_auth.py` の `signed_in_server()`）を使う。
- 読取系の一部は Tableau MCP（`.mcp.json`）。MCP は Metadata API をほぼ非対応のため、lineage / 既存 calc は `scripts/metadata_api.py` の GraphQL で自前に叩く。

### 公開前提の規範
- このリポジトリは公開されうる。実 URL / 実 ID / 個人情報 / トークンを本文・コミット・サンプルに書かない。
- 秘匿値は `.env` / `.mcp.json`（gitignore 済み）に置き、配布用はテンプレート（`.env.template` / `.mcp.json.template`）。
- サンプル値はダミー（`https://example.tableau.com`, `LUID_PLACEHOLDER` 等）。

## 共通モジュール（`scripts/`）

| ファイル | 役割 |
|---|---|
| `scripts/tableau_auth.py` | OAuth (PKCE) サインイン。`signed_in_server()` |
| `scripts/metadata_api.py` | Metadata API (GraphQL) client。`graphql(server, query, vars)` |

## 依存

`tableauserverclient` / `python-dotenv` / `requests`（`requirements.txt`）。
