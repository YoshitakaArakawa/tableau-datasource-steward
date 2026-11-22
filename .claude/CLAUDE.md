# tableau-datasource-steward — 開発指針

このリポジトリは Claude Code の**プラグイン兼 marketplace**。Tableau の Published Data Source (PDS) をセマンティックレイヤーとして整備する Skill 群を配布する。本ファイルはこのリポジトリを**開発するとき**の指針で、プラグインの利用者には配布されない（プラグインの CLAUDE.md は読み込まれない）。

利用時のルーティング・横断ポリシー・ワークフローの正本は、入口 Skill `skills/steward/`（SKILL.md と references/）にある。利用時に効くべき規範はここではなく `steward` に書く。

## 構成

| パス | 役割 |
|---|---|
| `.claude-plugin/plugin.json` | プラグインのマニフェスト。リリース時に `version` を上げる |
| `.claude-plugin/marketplace.json` | このリポジトリ自体を marketplace にする定義（`source: "./"`） |
| `skills/steward/` | 入口 Skill。Skill マップ・ルーティング・承認ゲート・出力先・報告の規範・ワークフロー |
| `skills/<name>/` | 各 Skill（inspector / describer / prospector / augmenter / rewirer / curator）。同梱スクリプトは `skills/<name>/scripts/` |
| `scripts/` | 共通モジュール（下表）。各 Skill のスクリプトは自分の位置から親をたどってここを見つける |
| `work/` | 開発セッションの作業物。規約は [work/README.md](../work/README.md) |

## 共通モジュール（`scripts/`）

| ファイル | 役割 |
|---|---|
| `scripts/tableau_auth.py` | OAuth (PKCE) サインイン。`signed_in_server()`。`status` サブコマンドが機械前チェック（exit 0 = cached session alive）。`.env` は作業ディレクトリから親へ探し、セッションはユーザー単位のディレクトリにキャッシュする |
| `scripts/metadata_api.py` | Metadata API (GraphQL) client。`graphql(server, query, vars)` |
| `scripts/batch_augment.py` | メタデータ整備バッチの orchestrator。`estimate` / `run` |
| `scripts/metadata_report.py` | 成果物レビュー面の生成（読取専用） |

## 開発の進め方

- **ローカルでの読み込み**: このリポジトリを開いたセッションでは、Skill は自動では読み込まれない（`.claude/skills/` を持たないため）。CLI は `claude --plugin-dir .` で起動する。デスクトップアプリでは、ユーザースコープに marketplace としてこのディレクトリを追加して install する（パスは利用者の環境に置き、リポジトリに書かない）。変更は `/reload-plugins` か次のセッションで反映される。
- **検証**: 変更後は `claude plugin validate .`（marketplace）と `claude plugin validate .claude-plugin/plugin.json --strict`（プラグイン）を通す。Skill の変更は `agent-assets:creating-skills` のバリデータも通す。
- **スクリプトの実行**: `uv run <script>`。依存は各スクリプト冒頭の PEP 723 inline metadata に書く。依存を足すときは、全スクリプトの inline metadata と `requirements.txt` をそろえる。
- **スクリプトの置き場**: 1 つの Skill だけが使うものは `skills/<name>/scripts/`、2 つ以上の Skill で使うものは `scripts/`。プラグインのディレクトリ外（`../` 等）を参照しない（git から入れたプラグインはキャッシュにコピーされ、外は存在しない）。
- **書き込み先**: スクリプトはプラグインのディレクトリに書かない（版ごとに入れ替わる）。出力は呼び出し側が渡す `--out` / `--out-dir`、キャッシュはユーザー単位のディレクトリ。

## Skill を書くときの規約

- Skill の作成・変更時は、着手前に `creating-skills` を参照する。
- 「何を触るときにどの Skill を使うか」は `steward` を正とする。**`steward` 以外の Skill 本文には他 Skill への routing 参照を書かない**（スコープ除外は行き先なしで書く）。書いてよいのは入出力契約まで（spec のフィールドがどの Skill の成果物由来か、実行手順上どの Skill の出力を前提にするか）。
- SKILL.md からのパスは `${CLAUDE_SKILL_DIR}`（同梱スクリプト）と `${CLAUDE_PLUGIN_ROOT}`（共通モジュール・他 Skill の references）で書く。これらは SKILL.md 本文でだけ置換され、references/ と Bash の環境変数では展開されない。references では「プラグインのルートからの相対パス」と明記して書く。
- 書き込み系 Skill（augmenter / rewirer / curator）は、承認ゲートの要点を自分の本文にも持つ。入口を経由せずに直接呼ばれても効くようにするため。

## 公開前提の規範

- このリポジトリは公開されうる。実 URL / 実 ID / 個人情報 / トークンを本文・コミット・サンプルに書かない。
- 秘匿値は `.env`（gitignore 済み）に置き、配布用はテンプレート（`.env.template`）。
- サンプル値はダミー（`https://example.tableau.com`, `LUID_PLACEHOLDER` 等）。
