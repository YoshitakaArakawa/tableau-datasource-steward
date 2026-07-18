# work/

セッション作業物の公式置き場。**この README 以外は git で追跡しない**（`.gitignore` で除外済み）。

## 形式

```
work/
└── <yyyymmdd>_<tag>/     # 例: 20260601_marts-dictionary/
    ├── inspection.json    # inspector 出力
    ├── candidates.json    # prospector 出力
    ├── specs/             # describer が生成した change-set spec
    └── out/               # augmenter の out-dir（original/edited/verified の tds/tdsx, result.json）
```

- 日付は**作業開始日**、`<tag>` は作業内容の短い要約（kebab-case / snake_case 可）
- 上記の内部構成は例。バッチ運用（manifest / spec-dir）の設計が固まったら、そのフォーマットに合わせてここを更新する
- Skill 出力だけでなく、検討メモ・backlog・受領したフィードバック等のセッション横断ドキュメントも `<yyyymmdd>_<tag>/` フォルダに置く（例: `20260718_skill-improvement/`）

## 注意

- 中身は**公開リポジトリに出ない**前提。実サーバーの PDS 名・LUID・DL した .tdsx を置いてよい
- `git add -f` で誤って強制追加しない
- 作業終了後、不要なら削除してよい（特に .tdsx 等の大きいファイル）

## 昇格ルール

`work/` で試して固まった内容は repo に昇格させる:

- 規約・判断基準 → `CLAUDE.md` または該当 Skill の `references/`
- 実装ロジック → 該当 Skill の `scripts/`（2 つ以上の Skill で使うなら repo 直下 `scripts/`）
- 未確定のものは昇格させず、ここに置いたままでよい
