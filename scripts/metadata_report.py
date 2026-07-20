"""メタデータ整備の成果物レビュー面を生成する（読取専用）。

spec や result.json（書いたつもりの記録）ではなく、**ライブのカタログを GraphQL で
読み直して**「実際に今入っているメタデータ」を人間が網羅的に確認できる HTML 1 枚に
まとめる。バッチ反映後のレビュー・定期的な棚卸しの両方に使う。

出力: per-PDS の grain + 全 field の description 一覧 + coverage 集計。
論理テーブル由来の擬似列は分母から除外して別掲する（inspector / augmenter と同じ規則）。

usage:
    python metadata_report.py --projects marts,raw_pds --out report.html
    python metadata_report.py --names "PDS 名,別の PDS" --out report.html

Auth: OAuth (tableau_auth.py, signed_in_server())。書き込み API は呼ばない。
"""
from __future__ import annotations

import argparse
import datetime
import html
import json
import sys
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")

sys.path.insert(0, str(Path(__file__).resolve().parent))
from tableau_auth import signed_in_server  # noqa: E402
from metadata_api import graphql  # noqa: E402

QUERY = """
{
  publishedDatasources {
    luid name projectName description
    upstreamTables { name }
    fields {
      __typename name description
      ... on ColumnField { dataType upstreamColumns { luid } }
      ... on CalculatedField { dataType formula }
    }
  }
}
"""

CSS = """
:root { --fg:#1a1a1a; --bg:#fff; --muted:#667; --line:#d8dce2; --ok:#0a7a3d;
        --warn:#b3261e; --chip:#eef1f5; --card:#fafbfc; }
@media (prefers-color-scheme: dark) {
  :root { --fg:#e8eaed; --bg:#17191c; --muted:#9aa3ad; --line:#3a3f46;
          --ok:#5dc389; --warn:#f28b82; --chip:#252a30; --card:#1e2126; } }
/* ビューワ側のテーマトグル（root への data-theme 付与）が OS 設定より優先される */
:root[data-theme="dark"] { --fg:#e8eaed; --bg:#17191c; --muted:#9aa3ad;
  --line:#3a3f46; --ok:#5dc389; --warn:#f28b82; --chip:#252a30; --card:#1e2126; }
:root[data-theme="light"] { --fg:#1a1a1a; --bg:#fff; --muted:#667;
  --line:#d8dce2; --ok:#0a7a3d; --warn:#b3261e; --chip:#eef1f5; --card:#fafbfc; }
* { box-sizing:border-box }
body { font-family:-apple-system,"Segoe UI","Hiragino Sans","Noto Sans JP",sans-serif;
       color:var(--fg); background:var(--bg); margin:0 auto; max-width:70rem;
       padding:2rem 1.25rem; line-height:1.55 }
h1 { font-size:1.35rem; margin:0 0 .25rem }
h2 { font-size:1.05rem; margin:2.2rem 0 .2rem; padding-top:.6rem;
     border-top:1px solid var(--line) }
.meta,.grain { color:var(--muted); font-size:.85rem }
.grain { color:var(--fg); background:var(--card); border:1px solid var(--line);
         border-radius:6px; padding:.5rem .7rem; margin:.45rem 0 .6rem;
         font-size:.9rem }
.grain.missing { color:var(--warn) }
.tally { display:flex; gap:.9rem; flex-wrap:wrap; margin:.8rem 0 0;
         font-size:.9rem }
.tally b { font-size:1.15rem }
table { border-collapse:collapse; width:100%; font-size:.85rem; margin:.3rem 0 }
th,td { text-align:left; padding:.3rem .55rem; border-bottom:1px solid var(--line);
        vertical-align:top }
th { color:var(--muted); font-weight:600; white-space:nowrap }
td.name { white-space:nowrap; font-family:ui-monospace,Consolas,monospace;
          font-size:.8rem }
td.desc { width:60% }
.nodesc { color:var(--warn) }
.chip { display:inline-block; background:var(--chip); border-radius:4px;
        padding:0 .4rem; font-size:.72rem; color:var(--muted); margin-left:.4rem }
.toc { columns:2; font-size:.85rem; margin:.8rem 0 0; padding-left:1.2rem }
.toc .miss { color:var(--warn) }
details { margin:.2rem 0 } summary { cursor:pointer; color:var(--muted);
          font-size:.82rem }
.overflow { overflow-x:auto }
"""


def build_html(rows: list[dict], scope_label: str, generated_at: str) -> str:
    def esc(s):  # None 安全
        return html.escape(s or "")

    total_cols = sum(r["n_cols"] for r in rows)
    total_desc = sum(r["n_described"] for r in rows)
    total_calcs = sum(r["n_calcs"] for r in rows)
    total_calcs_desc = sum(r["n_calcs_described"] for r in rows)
    grain_ok = sum(1 for r in rows if r["grain"])

    def has_gap(r):  # calc も記述対象（セマンティックレイヤーの一部）として gap に数える
        return (r["n_undescribed"] > 0 or not r["grain"]
                or r["n_calcs_described"] < r["n_calcs"])

    toc = "".join(
        f'<li class="{"miss" if has_gap(r) else ""}">'
        f'<a href="#{esc(r["luid"])}">{esc(r["name"])}</a>'
        f' ({r["n_described"]}/{r["n_cols"]}'
        + (f' +calc {r["n_calcs_described"]}/{r["n_calcs"]}' if r["n_calcs"] else "")
        + ')</li>'
        for r in rows)

    sections = []
    for r in rows:
        field_rows = "".join(
            f'<tr><td class="name">{esc(f["name"])}</td>'
            f'<td>{esc(f["dataType"])}{"<span class=chip>calc</span>" if f["is_calc"] else ""}</td>'
            f'<td class="desc">{esc(f["description"]) or "<span class=nodesc>（説明なし）</span>"}</td></tr>'
            for f in r["fields"])
        pseudo = (f'<details><summary>擬似列・対象外 {len(r["excluded"])} 件</summary>'
                  f'{esc("、".join(r["excluded"]))}</details>') if r["excluded"] else ""
        grain_div = (f'<div class="grain">{esc(r["grain"])}</div>' if r["grain"]
                     else '<div class="grain missing">grain（datasource 説明）未設定</div>')
        sections.append(
            f'<h2 id="{esc(r["luid"])}">{esc(r["name"])}'
            f'<span class="chip">{esc(r["project"])}</span></h2>'
            f'<div class="meta">luid: {esc(r["luid"])} ・ 記述 {r["n_described"]}/{r["n_cols"]} 列</div>'
            f'{grain_div}'
            f'<div class="overflow"><table><tr><th>field</th><th>type</th><th>description</th></tr>'
            f'{field_rows}</table></div>{pseudo}')

    return (
        f'<title>PDS メタデータレポート — {esc(scope_label)}</title>'
        f'<style>{CSS}</style>'
        f'<h1>PDS メタデータレポート</h1>'
        f'<div class="meta">スコープ: {esc(scope_label)} ・ 生成: {esc(generated_at)}（ライブカタログを GraphQL で読取）</div>'
        f'<div class="tally"><span>PDS <b>{len(rows)}</b></span>'
        f'<span>実列 <b>{total_desc}</b>/{total_cols} 記述</span>'
        f'<span>calc <b>{total_calcs_desc}</b>/{total_calcs} 記述</span>'
        f'<span>grain <b>{grain_ok}</b>/{len(rows)} 設定</span></div>'
        f'<ol class="toc">{toc}</ol>'
        + "".join(sections))


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--projects", help="project 名のカンマ区切り")
    ap.add_argument("--names", help="PDS 名のカンマ区切り")
    ap.add_argument("--out", required=True, help="出力 HTML パス")
    args = ap.parse_args()
    if not (args.projects or args.names):
        ap.error("--projects か --names のどちらかを指定する")
    want_projects = {p.strip() for p in (args.projects or "").split(",") if p.strip()}
    want_names = {n.strip() for n in (args.names or "").split(",") if n.strip()}

    with signed_in_server() as server:
        ds = graphql(server, QUERY, {}).get("publishedDatasources") or []

    rows = []
    for d in ds:
        if want_projects and d.get("projectName") not in want_projects:
            if not (want_names and d["name"] in want_names):
                continue
        elif want_names and d["name"] not in want_names and not want_projects:
            continue
        upstream = {t["name"] for t in d.get("upstreamTables") or []}
        fields, excluded = [], []
        for f in sorted(d.get("fields") or [], key=lambda x: x["name"].lower()):
            is_calc = f["__typename"] == "CalculatedField"
            # 論理テーブル自体を指す擬似列は記述対象外。dataType=TABLE が確定的目印
            # （Custom SQL は upstreamTables が空で名前一致が効かない）。分母から除外
            # して別掲する（inspector / augmenter の coverage と同じ規則）
            if (not is_calc and not (f.get("upstreamColumns") or [])
                    and (f.get("dataType") == "TABLE" or f["name"] in upstream)):
                excluded.append(f["name"])
                continue
            fields.append({"name": f["name"], "is_calc": is_calc,
                           "dataType": f.get("dataType") or "",
                           "description": (f.get("description") or "").strip()})
        cols = [f for f in fields if not f["is_calc"]]
        calcs = [f for f in fields if f["is_calc"]]
        rows.append({
            "luid": d["luid"], "name": d["name"], "project": d.get("projectName") or "",
            "grain": (d.get("description") or "").strip(),
            "fields": fields, "excluded": excluded,
            "n_cols": len(cols),
            "n_described": sum(1 for f in cols if f["description"]),
            "n_undescribed": sum(1 for f in cols if not f["description"]),
            "n_calcs": len(calcs),
            "n_calcs_described": sum(1 for f in calcs if f["description"]),
        })
    rows.sort(key=lambda r: (r["project"], r["name"].lower()))
    if not rows:
        raise SystemExit("対象 PDS が 0 件。--projects / --names を確認")

    scope = args.projects or args.names
    generated = datetime.datetime.now().strftime("%Y-%m-%d %H:%M JST")
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(build_html(rows, scope, generated), encoding="utf-8")

    summary = {
        "out": str(out),
        "n_pds": len(rows),
        "columns_described": sum(r["n_described"] for r in rows),
        "columns_total": sum(r["n_cols"] for r in rows),
        "calcs_described": sum(r["n_calcs_described"] for r in rows),
        "calcs_total": sum(r["n_calcs"] for r in rows),
        "grain_set": sum(1 for r in rows if r["grain"]),
        # calc も記述対象（セマンティックレイヤーの一部）として gap に数える
        "pds_with_gaps": [r["name"] for r in rows
                          if r["n_undescribed"] or not r["grain"]
                          or r["n_calcs_described"] < r["n_calcs"]],
    }
    print("RESULT_JSON:", json.dumps(summary, ensure_ascii=False))


if __name__ == "__main__":
    main()
