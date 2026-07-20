"""メタデータ整備の成果物レビュー面を生成する（読取専用）。

spec や result.json（書いたつもりの記録）ではなく、**ライブのカタログを GraphQL で
読み直して**「実際に今入っているメタデータ」を人間が網羅的に確認できる HTML 1 枚に
まとめる。バッチ反映後のレビュー・定期的な棚卸しの両方に使う。

レビュー UX:
- 冒頭に「重点レビュー」トリアージ（conflict > 低 confidence > 説明なし > grain 欠落）。
  何を見るべきかをレポート自身が提示する
- 左サイドバーの固定目次（project 別・状態ドット付き・スクロール追従ハイライト）
- 各 PDS タイトルから Tableau Web UI の実ページへ遷移（REST の webpageUrl）
- `--spec-dir` を渡すと change-set spec を join し、説明の出所
  （extracted / inferred / 既存）・confidence・conflict をフィールド単位で表示する。
  ライブカタログは出所を保存しないため、この情報は spec がある場合のみ出せる

論理テーブル擬似列（dataType=TABLE 等）は表示しない（除外数は RESULT_JSON にのみ残す）。

usage:
    python metadata_report.py --projects marts,raw_pds --out report.html \\
        [--spec-dir work/<batch>/specs --spec-dir work/<batch>/specs_oauth]

Auth: OAuth (tableau_auth.py, signed_in_server())。書き込み API は呼ばない。
"""
from __future__ import annotations

import argparse
import datetime
import html
import json
import os
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
    luid name projectName description vizportalUrlId
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
:root { --fg:#16181c; --bg:#fff; --muted:#5b6470; --line:#dde1e7; --accent:#2563a8;
        --warn:#b3261e; --warn-bg:#fdeeee; --chip:#eef1f5; --card:#f6f8fa;
        --side:#f1f3f6; --ok:#0a7a3d; }
@media (prefers-color-scheme: dark) {
  :root { --fg:#e8eaed; --bg:#141619; --muted:#a3adb8; --line:#373d45;
          --accent:#7fb2e5; --warn:#f28b82; --warn-bg:#3a2020; --chip:#262b31;
          --card:#1b1e23; --side:#191c20; --ok:#5dc389; } }
:root[data-theme="dark"] { --fg:#e8eaed; --bg:#141619; --muted:#a3adb8;
  --line:#373d45; --accent:#7fb2e5; --warn:#f28b82; --warn-bg:#3a2020;
  --chip:#262b31; --card:#1b1e23; --side:#191c20; --ok:#5dc389; }
:root[data-theme="light"] { --fg:#16181c; --bg:#fff; --muted:#5b6470;
  --line:#dde1e7; --accent:#2563a8; --warn:#b3261e; --warn-bg:#fdeeee;
  --chip:#eef1f5; --card:#f6f8fa; --side:#f1f3f6; --ok:#0a7a3d; }
* { box-sizing:border-box }
html { scroll-behavior:smooth; scroll-padding-top:1rem }
@media (prefers-reduced-motion: reduce) { html { scroll-behavior:auto } }
body { font-family:-apple-system,"Segoe UI","Hiragino Sans","Noto Sans JP",sans-serif;
       color:var(--fg); background:var(--bg); margin:0; line-height:1.6 }
.layout { display:grid; grid-template-columns:17rem minmax(0,1fr) }
a { color:var(--accent) }
nav { background:var(--side); border-right:1px solid var(--line);
      position:sticky; top:0; height:100vh; overflow-y:auto; padding:1.1rem 0;
      font-size:.84rem }
nav .nav-title { font-weight:700; padding:0 1rem .5rem; font-size:.9rem }
nav .proj { color:var(--muted); text-transform:uppercase; letter-spacing:.06em;
            font-size:.7rem; padding:.8rem 1rem .2rem }
nav a { display:flex; align-items:center; gap:.45rem; padding:.22rem 1rem;
        color:var(--fg); text-decoration:none; border-left:3px solid transparent;
        white-space:nowrap; overflow:hidden; text-overflow:ellipsis }
nav a:hover { background:var(--chip) }
nav a.active { border-left-color:var(--accent); background:var(--chip);
               font-weight:600 }
.dot { width:.5rem; height:.5rem; border-radius:50%; background:var(--ok);
       flex:none }
.dot.gap { background:var(--warn) }
main { padding:1.6rem 2rem 4rem; max-width:62rem; min-width:0 }
h1 { font-size:1.3rem; margin:0 0 .2rem }
h2 { font-size:1.05rem; margin:0 }
.meta { color:var(--muted); font-size:.84rem }
.tally { display:flex; gap:1.1rem; flex-wrap:wrap; margin:.7rem 0 0;
         font-size:.9rem }
.tally b { font-size:1.2rem; font-variant-numeric:tabular-nums }
.triage { border:1px solid var(--line); border-radius:8px; background:var(--card);
          margin:1.2rem 0 0; padding: .8rem 1rem }
.triage h2 { font-size:.95rem; margin:0 0 .4rem }
.triage li { margin:.15rem 0 }
.triage .why { color:var(--warn); font-weight:600 }
.triage .allclear { color:var(--ok); font-weight:600 }
.pds { border:1px solid var(--line); border-radius:8px; background:var(--card);
       margin:1.4rem 0 0; padding:1rem 1.2rem }
.pds-head { display:flex; align-items:baseline; gap:.6rem; flex-wrap:wrap }
.ext { font-size:.78rem; text-decoration:none; white-space:nowrap }
.grain { border-left:3px solid var(--accent); padding:.35rem .7rem;
         margin:.55rem 0 .7rem; font-size:.9rem; background:var(--bg);
         border-radius:0 6px 6px 0 }
.grain.missing { border-left-color:var(--warn); color:var(--warn) }
.overflow { overflow-x:auto }
table { border-collapse:collapse; width:100%; font-size:.85rem; background:var(--bg);
        border-radius:6px }
th,td { text-align:left; padding:.34rem .6rem; border-bottom:1px solid var(--line);
        vertical-align:top }
tr:last-child td { border-bottom:none }
th { color:var(--muted); font-weight:600; white-space:nowrap; font-size:.78rem }
td.name { white-space:nowrap; font-family:ui-monospace,Consolas,monospace;
          font-size:.8rem }
td.desc { width:58% }
tr.gap td { background:var(--warn-bg) }
.nodesc { color:var(--warn); font-weight:600 }
.chip { display:inline-block; background:var(--chip); border-radius:4px;
        padding:0 .4rem; font-size:.7rem; color:var(--muted); margin-left:.35rem;
        white-space:nowrap }
.chip.warn { color:var(--warn); font-weight:600 }
:focus-visible { outline:2px solid var(--accent); outline-offset:2px }
@media (max-width: 60rem) {
  .layout { grid-template-columns:1fr }
  nav { position:static; height:auto; border-right:none;
        border-bottom:1px solid var(--line) }
  main { padding:1.2rem 1rem 3rem } }
"""

SCROLLSPY_JS = """
const links = [...document.querySelectorAll('nav a[data-luid]')];
const byId = Object.fromEntries(links.map(a => [a.dataset.luid, a]));
const io = new IntersectionObserver(entries => {
  for (const e of entries) {
    if (e.isIntersecting) {
      links.forEach(a => a.classList.remove('active'));
      byId[e.target.id]?.classList.add('active');
    }
  }
}, { rootMargin: '-10% 0px -70% 0px' });
document.querySelectorAll('section.pds').forEach(s => io.observe(s));
"""


def _slug(name: str) -> str:
    return "".join(c if c.isalnum() else "_" for c in name)


def load_provenance(spec_dirs: list[Path]) -> dict:
    """spec ディレクトリ群から luid → {field_caption → 出所メタ} を組み立てる。

    describer 規約のフィールド（source / confidence / conflict / variants）を
    そのまま拾う。source 無しの spec エントリは inferred 扱い（本リポの生成経路で
    extracted はラベル必須のため、無ラベル = 推論生成）。
    """
    prov: dict[str, dict[str, dict]] = {}
    for d in spec_dirs:
        for p in sorted(d.glob("*.json")):
            try:
                spec = json.loads(p.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                continue
            luid = spec.get("source_luid")
            if not luid:
                continue
            entries = prov.setdefault(luid, {})
            for e in (spec.get("descriptions") or []):
                entries[e["field_caption"]] = {
                    "source": e.get("source") or "inferred",
                    "confidence": e.get("confidence"),
                    "conflict": bool(e.get("conflict")),
                    "variants": e.get("variants"),
                }
            for c in (spec.get("calcs") or []):
                if c.get("description"):
                    entries[c["caption"]] = {
                        "source": c.get("source") or "inferred",
                        "confidence": c.get("confidence"),
                        "conflict": bool(c.get("conflict")),
                        "variants": c.get("variants"),
                    }
    return prov


def build_triage(rows: list[dict]) -> list[dict]:
    """レビュー優先度順の項目リスト。conflict > 低 confidence > 説明なし > grain 欠落。"""
    items = []
    for r in rows:
        for f in r["fields"]:
            pv = f.get("prov") or {}
            anchor = f'{r["luid"]}-f-{_slug(f["name"])}'
            if pv.get("conflict"):
                items.append({"pri": 0, "why": "出所間で説明が衝突（原文比較が必要）",
                              "pds": r["name"], "field": f["name"], "anchor": anchor})
            elif pv.get("confidence") == "low":
                items.append({"pri": 1, "why": "低 confidence の推論",
                              "pds": r["name"], "field": f["name"], "anchor": anchor})
            elif not f["description"]:
                items.append({"pri": 2, "why": "説明なし",
                              "pds": r["name"], "field": f["name"], "anchor": anchor})
        if not r["grain"]:
            items.append({"pri": 3, "why": "grain（datasource 説明）未設定",
                          "pds": r["name"], "field": None, "anchor": r["luid"]})
    return sorted(items, key=lambda x: (x["pri"], x["pds"]))


def build_html(rows: list[dict], scope_label: str, generated_at: str,
               with_prov: bool) -> str:
    esc = lambda s: html.escape(s or "")  # noqa: E731

    total_cols = sum(r["n_cols"] for r in rows)
    total_desc = sum(r["n_described"] for r in rows)
    total_calcs = sum(r["n_calcs"] for r in rows)
    total_calcs_desc = sum(r["n_calcs_described"] for r in rows)
    grain_ok = sum(1 for r in rows if r["grain"])
    triage = build_triage(rows)
    gap_luids = {t["anchor"].split("-f-")[0] for t in triage}

    # sidebar（project 別グループ・状態ドット）
    nav, cur_proj = [], None
    for r in rows:
        if r["project"] != cur_proj:
            cur_proj = r["project"]
            nav.append(f'<div class="proj">{esc(cur_proj)}</div>')
        dot = "dot gap" if r["luid"] in gap_luids else "dot"
        nav.append(f'<a href="#{esc(r["luid"])}" data-luid="{esc(r["luid"])}">'
                   f'<span class="{dot}"></span>{esc(r["name"])}</a>')

    # triage
    if triage:
        tri_items = "".join(
            f'<li><a href="#{esc(t["anchor"])}">{esc(t["pds"])}'
            + (f' / <code>{esc(t["field"])}</code>' if t["field"] else "")
            + f'</a> — <span class="why">{esc(t["why"])}</span></li>'
            for t in triage)
        tri_body = f'<ol>{tri_items}</ol>'
    else:
        note = ("説明の出所・confidence は spec 由来の表示。" if with_prov else
                "出所情報なし（--spec-dir 未指定）。")
        tri_body = (f'<p><span class="allclear">機械チェックはすべて通過。</span> '
                    f'{note}残るリスクは推論生成（inferred）の内容妥当性のみ — '
                    f'各 PDS の説明を業務知識と突き合わせるスポットチェックを推奨。</p>')

    sections = []
    for r in rows:
        frows = []
        for f in r["fields"]:
            pv = f.get("prov") or {}
            chips = ""
            if f["is_calc"]:
                chips += '<span class="chip">calc</span>'
            if with_prov and f["description"]:
                if pv:
                    chips += f'<span class="chip">{esc(pv["source"])}</span>'
                    if pv.get("confidence"):
                        cls = "chip warn" if pv["confidence"] == "low" else "chip"
                        chips += f'<span class="{cls}">conf: {esc(pv["confidence"])}</span>'
                    if pv.get("conflict"):
                        chips += '<span class="chip warn">conflict</span>'
                else:
                    chips += '<span class="chip">既存</span>'
            desc = (esc(f["description"]) if f["description"]
                    else '<span class="nodesc">（説明なし）</span>')
            anchor = f'{r["luid"]}-f-{_slug(f["name"])}'
            frows.append(
                f'<tr id="{esc(anchor)}"{"" if f["description"] else " class=gap"}>'
                f'<td class="name">{esc(f["name"])}{chips}</td>'
                f'<td>{esc(f["dataType"])}</td><td class="desc">{desc}</td></tr>')
        ext = (f'<a class="ext" href="{esc(r["webpage_url"])}" target="_blank" '
               f'rel="noopener">Tableau で開く ↗</a>') if r["webpage_url"] else ""
        grain_div = (f'<div class="grain">{esc(r["grain"])}</div>' if r["grain"]
                     else '<div class="grain missing">grain（datasource 説明）未設定</div>')
        stat = f'記述 {r["n_described"]}/{r["n_cols"]} 列'
        if r["n_calcs"]:
            stat += f' + calc {r["n_calcs_described"]}/{r["n_calcs"]}'
        sections.append(
            f'<section class="pds" id="{esc(r["luid"])}">'
            f'<div class="pds-head"><h2>{esc(r["name"])}</h2>'
            f'<span class="chip">{esc(r["project"])}</span>{ext}</div>'
            f'<div class="meta">{stat}</div>{grain_div}'
            f'<div class="overflow"><table>'
            f'<tr><th>field</th><th>type</th><th>description</th></tr>'
            f'{"".join(frows)}</table></div></section>')

    return (
        '<meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width, initial-scale=1">'
        f'<title>PDS メタデータレポート — {esc(scope_label)}</title>'
        f'<style>{CSS}</style>'
        f'<div class="layout">'
        f'<nav><div class="nav-title">対象 PDS</div>{"".join(nav)}</nav>'
        f'<main><h1>PDS メタデータレポート</h1>'
        f'<div class="meta">スコープ: {esc(scope_label)} ・ 生成: {esc(generated_at)}'
        f'（ライブカタログを GraphQL で読取）</div>'
        f'<div class="tally"><span>PDS <b>{len(rows)}</b></span>'
        f'<span>実列 <b>{total_desc}</b>/{total_cols} 記述</span>'
        f'<span>calc <b>{total_calcs_desc}</b>/{total_calcs} 記述</span>'
        f'<span>grain <b>{grain_ok}</b>/{len(rows)} 設定</span></div>'
        f'<div class="triage"><h2>重点レビュー（{len(triage)} 件）</h2>{tri_body}</div>'
        f'{"".join(sections)}</main></div>'
        f'<script>{SCROLLSPY_JS}</script>')


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--projects", help="project 名のカンマ区切り")
    ap.add_argument("--names", help="PDS 名のカンマ区切り")
    ap.add_argument("--spec-dir", action="append", default=[],
                    help="change-set spec のディレクトリ（複数指定可）。出所・confidence の join 元")
    ap.add_argument("--out", required=True, help="出力 HTML パス")
    args = ap.parse_args()
    if not (args.projects or args.names):
        ap.error("--projects か --names のどちらかを指定する")
    want_projects = {p.strip() for p in (args.projects or "").split(",") if p.strip()}
    want_names = {n.strip() for n in (args.names or "").split(",") if n.strip()}
    prov = load_provenance([Path(d) for d in args.spec_dir])

    with signed_in_server() as server:
        ds = graphql(server, QUERY, {}).get("publishedDatasources") or []
        server_address = server.server_address

    # Web UI への深いリンクは vizportalUrlId から組み立てる。REST の webpageUrl は
    # Tableau Cloud で pageSize が大きいと省略されるため使わない（実測）。
    site_name = os.environ.get("SITE_NAME", "")

    def web_url(d: dict) -> str:
        vid = d.get("vizportalUrlId")
        if not (vid and site_name):
            return ""
        return f"{server_address}/#/site/{site_name}/datasources/{vid}"

    rows, n_pseudo = [], 0
    for d in ds:
        if want_projects and d.get("projectName") not in want_projects:
            if not (want_names and d["name"] in want_names):
                continue
        elif want_names and d["name"] not in want_names and not want_projects:
            continue
        upstream = {t["name"] for t in d.get("upstreamTables") or []}
        fields = []
        pv_map = prov.get(d["luid"], {})
        for f in sorted(d.get("fields") or [], key=lambda x: x["name"].lower()):
            is_calc = f["__typename"] == "CalculatedField"
            # 論理テーブル擬似列（dataType=TABLE が確定的目印。Custom SQL は
            # upstreamTables が空で名前一致が効かない）は表示しない
            if (not is_calc and not (f.get("upstreamColumns") or [])
                    and (f.get("dataType") == "TABLE" or f["name"] in upstream)):
                n_pseudo += 1
                continue
            fields.append({"name": f["name"], "is_calc": is_calc,
                           "dataType": f.get("dataType") or "",
                           "description": (f.get("description") or "").strip(),
                           "prov": pv_map.get(f["name"])})
        cols = [f for f in fields if not f["is_calc"]]
        calcs = [f for f in fields if f["is_calc"]]
        rows.append({
            "luid": d["luid"], "name": d["name"], "project": d.get("projectName") or "",
            "grain": (d.get("description") or "").strip(),
            "webpage_url": web_url(d),
            "fields": fields,
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
    out.write_text(build_html(rows, scope, generated, bool(args.spec_dir)),
                   encoding="utf-8")

    triage = build_triage(rows)
    summary = {
        "out": str(out),
        "n_pds": len(rows),
        "columns_described": sum(r["n_described"] for r in rows),
        "columns_total": sum(r["n_cols"] for r in rows),
        "calcs_described": sum(r["n_calcs_described"] for r in rows),
        "calcs_total": sum(r["n_calcs"] for r in rows),
        "grain_set": sum(1 for r in rows if r["grain"]),
        "pseudo_excluded": n_pseudo,
        "triage_items": len(triage),
        "pds_with_gaps": sorted({t["pds"] for t in triage}),
    }
    print("RESULT_JSON:", json.dumps(summary, ensure_ascii=False))


if __name__ == "__main__":
    main()
