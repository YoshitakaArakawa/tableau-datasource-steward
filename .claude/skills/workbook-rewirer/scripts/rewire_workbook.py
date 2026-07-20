"""Rewire a downstream workbook after calc hoisting: repoint its published-datasource
connection to the (augmented) PDS, swap local calc references to the PDS-side calc,
then publish (CreateNew by default) and verify every view still renders.

Reads a rewire spec (JSON), downloads the workbook (.twb/.twbx), edits the .twb XML
(newline-agnostic), republishes, re-downloads to check the edits survived, and
renders every view of the ORIGINAL and the REWIRED workbook as fresh PNGs into a
side-by-side compare report (compare/view-compare.html). Export success is the
machine verdict; visual equivalence is eyeball material for the reviewer.
Query View Data is deliberately not used as comparison evidence: on dashboards it
returns only the first sheet, and hidden sheets have no LUID to query at all,
while dashboard images draw every sheet they contain.

usage:
    python rewire_workbook.py --spec spec.json --out-dir <dir>
    python rewire_workbook.py --rollback --out-dir <dir>

A normal run preserves the pristine workbook (original.twb/.twbx) plus a
rollback.json (name / project / show_tabs / LUID at run time) in out-dir;
--rollback republishes that original over the source workbook (Overwrite) and
verifies the LUID survived (exit 2 otherwise). No --spec needed for rollback.

Auth: OAuth (scripts/tableau_auth.py, signed_in_server()). Spec format:
    ../references/rewire-spec-format.md
XML edit format:
    ../references/twb-edit-format.md
"""
from __future__ import annotations

import argparse
import html
import json
import re
import sys
import time
import zipfile
from pathlib import Path


# --- locate shared modules in the repo-root scripts/ directory -----------------
def _find_repo_scripts() -> Path:
    cur = Path(__file__).resolve()
    for _ in range(8):
        cur = cur.parent
        cand = cur / "scripts" / "tableau_auth.py"
        if cand.exists():
            return cand.parent
    raise RuntimeError("repo-root scripts/ (with tableau_auth.py) not found")


sys.path.insert(0, str(_find_repo_scripts()))
import tableauserverclient as TSC  # noqa: E402
from tableau_auth import signed_in_server  # noqa: E402  (OAuth)
from metadata_api import graphql  # noqa: E402


# --- packaged / plain document io ----------------------------------------------
def read_document(path: Path, inner_suffix: str):
    """Return (inner_name, xml_text, zip_or_None). Handles .twb/.tds (plain) and
    .twbx/.tdsx (zip with the XML document inside)."""
    if zipfile.is_zipfile(path):
        z = zipfile.ZipFile(path)
        inner = next(n for n in z.namelist() if n.endswith(inner_suffix))
        return inner, z.read(inner).decode("utf-8"), z
    return path.name, path.read_text(encoding="utf-8"), None


def write_document(src_zip, inner_name: str, new_txt: str, out_path: Path):
    """Write the edited XML back, re-packaging when the source was a zip."""
    if src_zip is None:
        out_path.write_text(new_txt, encoding="utf-8")
        return
    with zipfile.ZipFile(out_path, "w", zipfile.ZIP_DEFLATED) as zo:
        for item in src_zip.infolist():
            data = src_zip.read(item.filename)
            if item.filename == inner_name:
                data = new_txt.encode("utf-8")
            zo.writestr(item, data)


# --- formula normalization (same dedup key as workbook-calc-prospector) --------
def strip_comments(formula: str) -> str:
    no_block = re.sub(r"/\*.*?\*/", " ", formula, flags=re.DOTALL)
    return re.sub(r"//[^\n]*", " ", no_block)


def normalize(formula: str) -> str:
    return re.sub(r"\s+", " ", strip_comments(formula).strip())


# --- XML helpers (newline-agnostic; Tableau writes single-quoted attrs) --------
def _xml_escape(text: str) -> str:
    return (text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
                .replace("'", "&apos;").replace('"', "&quot;"))


# non-greedy element matcher; <column> does not nest so tempered dot is safe
COLUMN_RE = re.compile(r"<column\b([^>]*)>((?:(?!</column>).)*?)</column>", re.S)


def parse_calc_columns(xml_text: str):
    """Yield calc columns as dicts: caption / token (internal name w/o brackets) /
    normalized formula / (start, end) span of the whole <column> element."""
    for m in COLUMN_RE.finditer(xml_text):
        attrs, body = m.group(1), m.group(2)
        fm = re.search(r"<calculation\b[^>]*\bformula='([^']*)'", body)
        if not fm:
            continue
        name = re.search(r"name='\[([^\]]*)\]'", attrs)
        cap = re.search(r"caption='([^']*)'", attrs)
        token = html.unescape(name.group(1)) if name else None
        yield {
            "caption": html.unescape(cap.group(1)) if cap else token,
            "token": token,
            "formula_norm": normalize(html.unescape(fm.group(1))),
            "span": m.span(),
        }


def replace_token(txt: str, old: str, new: str) -> tuple[str, int]:
    """Replace a field's internal-name token everywhere it is referenced.

    Tokens appear delimited: bracketed (`[Calculation_1]`, incl. attr values like
    column='[...]') and colon-qualified instance names (`[none:Calculation_1:qk]`).
    Lookaround keeps the replacement inside those delimiters only, so an opaque
    token never bleeds into unrelated text.
    """
    pat = re.compile(rf"(?<=[\[:]){re.escape(_xml_escape(old))}(?=[\]:])")
    return pat.subn(_xml_escape(new), txt)


def strip_dependency_calculations(txt: str, token: str) -> tuple[str, int]:
    """Drop cached <calculation> children from worksheet datasource-dependencies
    copies of the swapped column. The authoritative local definition is already
    deleted; leaving a cached formula on the (now remote) field invites the client
    to keep treating it as a local calc."""
    esc = re.escape(_xml_escape(token))
    stripped = 0

    def _clean(m: re.Match) -> str:
        nonlocal stripped
        body, n = re.subn(r"\s*<calculation\b[^>]*?(?:/>|>(?:(?!</calculation>).)*?</calculation>)",
                          "", m.group(2), flags=re.S)
        stripped += n
        return m.group(1) + body + m.group(3)

    pat = re.compile(rf"(<column\b[^>]*name='\[{esc}\]'[^>]*>)((?:(?!</column>).)*?)(</column>)", re.S)
    return pat.sub(_clean, txt), stripped


# --- before/after view rendering ------------------------------------------------
def _slug(name: str) -> str:
    return "".join(c if c.isalnum() else "_" for c in name).strip("_").lower()


def export_view_images(server, wb_item, prefix: str, out_dir: Path) -> dict[str, dict]:
    """Freshly render every LUID-addressable view (visible sheets + dashboards)
    of a workbook as PNG. Hidden sheets have no LUID and cannot be exported
    individually, but dashboards draw every sheet they contain, so dashboard
    images cover them."""
    server.workbooks.populate_views(wb_item)
    recs: dict[str, dict] = {}
    for v in wb_item.views:
        rec = {"png": "", "error": ""}
        try:
            # maxage=1 forces a fresh render instead of a stale cached image
            opts = TSC.ImageRequestOptions(
                imageresolution=TSC.ImageRequestOptions.Resolution.High, maxage=1)
            server.views.populate_image(v, opts)
            name = f"{prefix}_{_slug(v.name)}.png"
            (out_dir / name).write_bytes(v.image)
            rec["png"] = name
        except Exception as e:  # a single broken view must not hide the others
            rec["error"] = str(e)[:200]
        recs[v.name] = rec
    return recs


# These verdicts force verified=false. baseline_export_failed does not: the
# original was already broken there; the rewired copy rendering is an
# improvement to confirm, not a defect introduced by the rewire.
BLOCKING_VERDICTS = ("candidate_export_failed", "export_failed", "only_in_one_workbook")


def build_view_compare(baseline: dict, candidate: dict) -> list[dict]:
    """Per-view verdicts, matched by view name. Export-based only — whether each
    side rendered. Pixel equality is deliberately not judged (refresh timing and
    render nondeterminism make it noisy); the images are reviewer material."""
    rows = []
    for name in sorted(set(baseline) | set(candidate)):
        b, c = baseline.get(name), candidate.get(name)
        if not (b and c):
            verdict = "only_in_one_workbook"
        elif b["error"] and c["error"]:
            verdict = "export_failed"
        elif c["error"]:
            verdict = "candidate_export_failed"  # rewire broke a working view
        elif b["error"]:
            verdict = "baseline_export_failed"
        else:
            verdict = "ok"
        rows.append({"view": name, "verdict": verdict,
                     "baseline": b or {}, "candidate": c or {}})
    return rows


def write_compare_html(view_compare: list[dict], out_dir: Path):
    """Side-by-side original/rewired images per view, for the approval report."""
    esc = html.escape

    def cell(rec: dict) -> str:
        if rec.get("png"):
            return (f"<figure><a href='{esc(rec['png'])}'>"
                    f"<img src='{esc(rec['png'])}' alt='' loading='lazy'></a></figure>")
        return f"<figure><p class='err'>{esc(rec.get('error') or 'missing')}</p></figure>"

    body = "".join(
        f"<h2>{esc(r['view'])} — {esc(r['verdict'])}</h2>"
        f"<div class='pair'>{cell(r['baseline'])}{cell(r['candidate'])}</div>"
        for r in view_compare)
    page = (
        "<!doctype html><meta charset='utf-8'><title>view compare</title>"
        "<style>body{font-family:sans-serif;margin:1rem}"
        ".pair{display:flex;gap:1rem;align-items:flex-start;overflow-x:auto}"
        "figure{margin:0;flex:1;min-width:0}img{max-width:100%;border:1px solid #ccc}"
        ".err{color:#c62828}</style>"
        "<h1>original (left) vs rewired (right)</h1>" + body)
    (out_dir / "view-compare.html").write_text(page, encoding="utf-8")


# --- datasource block location --------------------------------------------------
REPOLOC_RE = re.compile(r"<repository-location\b[^>]*/>")
DS_TAG_RE = re.compile(r"<datasource\b[^>]*?(/)?>|</datasource>")


def _block_end(txt: str, start: int) -> int:
    """End index (exclusive) of the <datasource> element opening at `start`.

    A definition block can NEST another <datasource> (Tableau Cloud inserts a
    server-side shadow copy of the published datasource, with its own
    repository-location and sqlproxy connection), so taking the next close tag
    would truncate the outer block — count nesting depth instead."""
    depth = 0
    for m in DS_TAG_RE.finditer(txt, start):
        if m.group(0).startswith("</"):
            depth -= 1
            if depth == 0:
                return m.end()
        elif not m.group(1):  # opening tag; self-closing worksheet refs don't nest
            depth += 1
    raise SystemExit("unbalanced <datasource> tags in .twb")


def find_pds_block(txt: str, source_content_url: str | None,
                   swap_formulas: list[str]) -> tuple[int, int, str]:
    """Locate the published-datasource definition block to rewire.

    Anchors on <repository-location> elements whose path ends with '/datasources'
    (the workbook's own repository-location uses a '/workbooks' path). Nested
    shadow copies carry the SAME repository id as their outer block, so nested
    candidates are dropped; remaining ambiguity (several published datasources)
    is resolved by source_content_url and, failing that, by which block holds a
    local calc matching a swap formula. Returns (start, end, repository_id).
    """
    cands = []
    for m in REPOLOC_RE.finditer(txt):
        loc = m.group(0)
        path = re.search(r"path='([^']*)'", loc)
        if not (path and path.group(1).endswith("/datasources")):
            continue
        rid = re.search(r"\bid='([^']*)'", loc)
        bstart = txt.rfind("<datasource ", 0, m.start())
        if bstart < 0:
            continue
        cands.append((bstart, _block_end(txt, bstart),
                      html.unescape(rid.group(1)) if rid else ""))
    # drop blocks nested inside another candidate (server-side shadow copies)
    cands = [c for c in cands
             if not any(o[0] < c[0] and c[1] <= o[1] for o in cands if o is not c)]
    if source_content_url:
        matched = [c for c in cands if c[2] == source_content_url]
        cands = matched or cands  # fall through to formula match if url mismatches
    if len(cands) > 1 and swap_formulas:
        wanted = {normalize(f) for f in swap_formulas}
        cands = [c for c in cands
                 if any(col["formula_norm"] in wanted
                        for col in parse_calc_columns(txt[c[0]:c[1]]))]
    if not cands:
        raise SystemExit(f"no published-datasource block found"
                         f" (source_content_url={source_content_url!r})")
    if len(cands) > 1:
        raise SystemExit("cannot disambiguate the datasource block to rewire; "
                         f"candidates (repository ids): {[c[2] for c in cands]}")
    return cands[0]


# --- rollback -------------------------------------------------------------------
def write_rollback_meta(out: Path, src_wb, wb_path: Path) -> dict:
    """Preserve what --rollback needs to undo an Overwrite: the identity of the
    source workbook AS OF THIS RUN (name / project may drift later) and which
    original file was saved (TSC picks the .twb/.twbx extension on download)."""
    meta = {
        "workbook_luid": src_wb.id,
        "name": src_wb.name,
        "project_id": src_wb.project_id,
        "show_tabs": bool(src_wb.show_tabs),
        "original_file": wb_path.name,
    }
    (out / "rollback.json").write_text(
        json.dumps(meta, indent=2, ensure_ascii=False), encoding="utf-8")
    return meta


def rollback(out_dir: str) -> None:
    """out-dir に保全した原本 .twb(x) を元 workbook へ Overwrite 再 publish して巻き戻す。"""
    out = Path(out_dir)
    meta_path = out / "rollback.json"
    if not meta_path.exists():
        raise SystemExit(f"rollback.json not found in {out_dir}"
                         " (written by a normal run; nothing to roll back)")
    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    orig = out / meta["original_file"]
    if not orig.exists():
        raise SystemExit(f"original workbook {meta['original_file']!r} not found in {out_dir}")
    with signed_in_server() as server:
        item = TSC.WorkbookItem(meta["project_id"])
        item.name = meta["name"]
        item.show_tabs = bool(meta["show_tabs"])
        published = server.workbooks.publish(
            item, str(orig), mode=TSC.Server.PublishMode.Overwrite)
        result = {"phase": "rollback", "published_luid": published.id,
                  "luid_preserved": published.id == meta["workbook_luid"]}
        print("RESULT_JSON:", json.dumps(result, ensure_ascii=False))
        if not result["luid_preserved"]:
            raise SystemExit(2)


# --- main -----------------------------------------------------------------------
def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--spec")
    ap.add_argument("--out-dir", required=True)
    ap.add_argument("--rollback", action="store_true",
                    help="out-dir の原本 .twb(x) を元 workbook へ Overwrite 再 publish して巻き戻す")
    args = ap.parse_args()

    if args.rollback:
        rollback(args.out_dir)
        return
    if not args.spec:
        ap.error("--spec is required unless --rollback")

    spec = json.loads(Path(args.spec).read_text(encoding="utf-8"))
    out = Path(args.out_dir)
    out.mkdir(parents=True, exist_ok=True)

    mode = spec.get("mode", "CreateNew")
    if mode not in ("CreateNew", "Overwrite"):
        raise SystemExit(f"invalid mode: {mode}")
    target = spec.get("target", {})
    if mode == "CreateNew" and not target.get("new_name"):
        raise SystemExit("CreateNew requires target.new_name")
    swaps = spec.get("swaps") or []
    if not swaps:
        raise SystemExit("spec.swaps is empty: nothing to rewire")

    with signed_in_server() as server:
        pds = server.datasources.get_by_id(spec["pds"]["luid"])

        # PDS-side calc captions -> internal name tokens (from the PDS .tds)
        pds_path = Path(server.datasources.download(
            pds.id, filepath=str(out / "pds.tdsx"), include_extract=False))
        _, pds_txt, _ = read_document(pds_path, ".tds")
        pds_calcs = {c["caption"]: c["token"] for c in parse_calc_columns(pds_txt)}
        for s in swaps:
            if s["pds_calc_caption"] not in pds_calcs:
                raise SystemExit(f"calc {s['pds_calc_caption']!r} not found in PDS"
                                 f" {pds.name!r}; available: {sorted(pds_calcs)}")

        # download workbook (keep the original for revert)
        src_wb = server.workbooks.get_by_id(spec["workbook_luid"])
        wb_path = Path(server.workbooks.download(
            src_wb.id, filepath=str(out / "original"), include_extract=True))
        twb_name, txt, src_zip = read_document(wb_path, ".twb")
        (out / "original.twb").write_text(txt, encoding="utf-8")
        write_rollback_meta(out, src_wb, wb_path)  # enables --rollback later

        # baseline renders BEFORE any edit: the untouched original is the
        # reference the rewired copy is compared against
        compare_dir = out / "compare"
        compare_dir.mkdir(exist_ok=True)
        baseline = export_view_images(server, src_wb, "baseline", compare_dir)

        source_content_url = None
        if spec.get("source_pds_luid"):
            source_content_url = server.datasources.get_by_id(
                spec["source_pds_luid"]).content_url
        bstart, bend, current_id = find_pds_block(
            txt, source_content_url, [s["formula"] for s in swaps])
        block = txt[bstart:bend]

        # 1) delete the local calc definitions (match by normalized formula)
        swap_results = []
        for s in swaps:
            want = normalize(s["formula"])
            hit = next((c for c in parse_calc_columns(block)
                        if c["formula_norm"] == want), None)
            if not hit:
                have = [c["caption"] for c in parse_calc_columns(block)]
                raise SystemExit(f"local calc not found for formula {s['formula']!r};"
                                 f" workbook-local calcs: {have}")
            block = block[:hit["span"][0]] + block[hit["span"][1]:]
            swap_results.append({
                "pds_calc_caption": s["pds_calc_caption"],
                "wb_caption": hit["caption"],
                "old_token": hit["token"],
                "new_token": pds_calcs[s["pds_calc_caption"]],
            })

        # 2) repoint the connection when the workbook still targets another PDS
        repoint = {"performed": False, "current_id": current_id,
                   "target_id": pds.content_url}
        if current_id != pds.content_url:
            block, n_id = re.subn(rf"(\bid=')({re.escape(_xml_escape(current_id))})(')",
                                  rf"\g<1>{_xml_escape(pds.content_url)}\g<3>", block)
            # a pinned revision belongs to the old datasource; drop it (=latest)
            block, n_rev = re.subn(r"(<repository-location\b[^>]*?)\s+revision='[^']*'",
                                   r"\1", block)
            block, n_cap = re.subn(r"(<datasource\b[^>]*?caption=')[^']*(')",
                                   rf"\g<1>{_xml_escape(pds.name)}\g<2>", block, count=1)
            # sqlproxy connections carry the datasource CONTENT URL in dbname
            # (same value as the repository-location id, not the display name)
            block, n_db = re.subn(r"(<connection\b[^>]*class='sqlproxy'[^>]*?dbname=')[^']*(')",
                                  rf"\g<1>{_xml_escape(pds.content_url)}\g<2>", block)
            repoint.update({"performed": True, "id_replaced": n_id,
                            "revision_dropped": n_rev, "caption_updated": n_cap,
                            "dbname_updated": n_db})
            if n_id == 0:
                raise SystemExit("repoint failed: repository-location id not rewritten")

        txt = txt[:bstart] + block + txt[bend:]

        # 3) rename every reference from the local token to the PDS token,
        #    and strip cached <calculation> copies from datasource-dependencies
        for r in swap_results:
            txt, n_refs = replace_token(txt, r["old_token"], r["new_token"])
            txt, n_dep = strip_dependency_calculations(txt, r["new_token"])
            r.update({"refs_replaced": n_refs, "dependency_calcs_stripped": n_dep})

        (out / "edited.twb").write_text(txt, encoding="utf-8")
        edited_path = out / f"edited{wb_path.suffix}"
        write_document(src_zip, twb_name, txt, edited_path)

        # 4) publish (CreateNew: draft copy under a new name; Overwrite: gated upstream)
        project_id = target.get("project_id") or src_wb.project_id
        item = TSC.WorkbookItem(project_id)
        item.name = src_wb.name if mode == "Overwrite" else target["new_name"]
        item.show_tabs = bool(src_wb.show_tabs)
        pub_mode = (TSC.Server.PublishMode.CreateNew if mode == "CreateNew"
                    else TSC.Server.PublishMode.Overwrite)
        published = server.workbooks.publish(item, str(edited_path), mode=pub_mode)

        # 5) verify round-trip: old token gone, new token present, repoint survived
        ver_path = Path(server.workbooks.download(
            published.id, filepath=str(out / "verified"), include_extract=False))
        _, v_txt, _ = read_document(ver_path, ".twb")
        (out / "verified.twb").write_text(v_txt, encoding="utf-8")
        roundtrip = {}
        for r in swap_results:
            old_pat = re.compile(rf"(?<=[\[:]){re.escape(_xml_escape(r['old_token']))}(?=[\]:])")
            roundtrip[f"old_token_gone:{r['old_token']}"] = not old_pat.search(v_txt)
            roundtrip[f"new_token_present:{r['new_token']}"] = (
                _xml_escape(r["new_token"]) in v_txt)
        roundtrip["repository_id"] = _xml_escape(pds.content_url) in v_txt

        # 6) render check + compare evidence: rendering forces server-side query
        #    execution, so a broken field reference fails the export here. The
        #    baseline/candidate image pairs go into a side-by-side report for
        #    the reviewer (visual equivalence is not auto-judged).
        candidate = export_view_images(server, published, "candidate", compare_dir)
        view_compare = build_view_compare(baseline, candidate)
        write_compare_html(view_compare, compare_dir)
        compare_tally = {k: sum(1 for r in view_compare if r["verdict"] == k)
                         for k in ("ok", "baseline_export_failed") + BLOCKING_VERDICTS}

        # 7) supplementary GraphQL check (subject to Metadata API indexing lag):
        #    the rewired workbook should list the target PDS upstream and should
        #    no longer embed the hoisted formula. Never gates `verified`.
        gql = {}
        try:
            # fresh publishes index asynchronously; ~15s x3 covers typical lag
            for _attempt in range(3):
                wbs = graphql(server,
                              "query($l:String!){workbooks(filter:{luid:$l}){"
                              "upstreamDatasources{luid} embeddedDatasources{fields{"
                              "__typename ... on CalculatedField{formula}}}}}",
                              {"l": published.id}).get("workbooks") or []
                if wbs:
                    up = {d["luid"] for d in wbs[0].get("upstreamDatasources") or []}
                    embedded = {normalize(f["formula"]) for eds in
                                wbs[0].get("embeddedDatasources") or []
                                for f in eds.get("fields") or []
                                if f["__typename"] == "CalculatedField" and f.get("formula")}
                    gql["upstream_has_target_pds"] = pds.id in up
                    for s in swaps:
                        gql[f"embedded_calc_gone:{s['pds_calc_caption']}"] = (
                            normalize(s["formula"]) not in embedded)
                    break
                if _attempt < 2:
                    time.sleep(15)
            else:
                gql["_note"] = "not yet indexed by Metadata API (re-read to confirm)"
        except Exception as e:  # transient: don't fail the run
            gql = {"_note": f"graphql check skipped: {str(e)[:100]}"}

        result = {
            "published_luid": published.id,
            "published_name": published.name,
            "project_id": project_id,
            "mode": mode,
            "swaps": swap_results,
            "repoint": repoint,
            "roundtrip_checks": roundtrip,
            "view_compare": {
                "views": view_compare,
                "tally": compare_tally,
                "html": "compare/view-compare.html",
                "_note": ("" if view_compare else "workbook has no views to verify"),
            },
            "graphql_checks": gql,
            # gate on what this run can prove: edits survived AND no view broke
            # where the original rendered (visual equivalence stays with the reviewer)
            "verified": (all(roundtrip.values())
                         and not any(r["verdict"] in BLOCKING_VERDICTS
                                     for r in view_compare)),
        }
        (out / "result.json").write_text(json.dumps(result, indent=2, ensure_ascii=False),
                                         encoding="utf-8")
        print("RESULT_JSON:", json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()
