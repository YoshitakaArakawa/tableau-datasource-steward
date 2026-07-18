"""Rewire a downstream workbook after calc hoisting: repoint its published-datasource
connection to the (augmented) PDS, swap local calc references to the PDS-side calc,
then publish (CreateNew by default) and verify every view still renders.

Reads a rewire spec (JSON), downloads the workbook (.twb/.twbx), edits the .twb XML
(newline-agnostic), republishes, re-downloads to check the edits survived, and
exports each view as CSV to prove the swapped calc actually resolves on the server.

usage:
    python rewire_workbook.py --spec spec.json --out-dir <dir>

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


# --- datasource block location --------------------------------------------------
REPOLOC_RE = re.compile(r"<repository-location\b[^>]*/>")


def find_pds_block(txt: str, source_content_url: str | None) -> tuple[int, int, str]:
    """Locate the published-datasource definition block to rewire.

    Anchors on <repository-location> elements whose path ends with '/datasources'
    (the workbook's own repository-location uses a '/workbooks' path). Returns
    (block_start, block_end, current_repository_id). If the workbook uses several
    published datasources, source_content_url is required to disambiguate.
    """
    cands = []
    for m in REPOLOC_RE.finditer(txt):
        path = re.search(r"path='([^']*)'", m.group(0))
        if not (path and path.group(1).endswith("/datasources")):
            continue
        rid = re.search(r"\bid='([^']*)'", m.group(0))
        cands.append((m.start(), html.unescape(rid.group(1)) if rid else ""))
    if source_content_url:
        cands = [c for c in cands if c[1] == source_content_url]
    if not cands:
        raise SystemExit(f"no published-datasource repository-location found"
                         f" (source_content_url={source_content_url!r})")
    if len(cands) > 1:
        raise SystemExit("workbook uses multiple published datasources; set"
                         f" source_pds_luid to pick one of: {[c[1] for c in cands]}")
    pos, rid = cands[0]
    start = txt.rfind("<datasource ", 0, pos)
    end = txt.find("</datasource>", pos)
    if start < 0 or end < 0:
        raise SystemExit("could not delimit the enclosing <datasource> element")
    return start, end + len("</datasource>"), rid


# --- main -----------------------------------------------------------------------
def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--spec", required=True)
    ap.add_argument("--out-dir", required=True)
    args = ap.parse_args()

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

        source_content_url = None
        if spec.get("source_pds_luid"):
            source_content_url = server.datasources.get_by_id(
                spec["source_pds_luid"]).content_url
        bstart, bend, current_id = find_pds_block(txt, source_content_url)
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
            # sqlproxy connections carry the datasource display name in dbname
            block, n_db = re.subn(r"(<connection\b[^>]*class='sqlproxy'[^>]*?dbname=')[^']*(')",
                                  rf"\g<1>{_xml_escape(pds.name)}\g<2>", block)
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

        # 6) render check: exporting each view as CSV forces server-side query
        #    execution, so a broken field reference fails loudly here
        view_checks = {}
        server.workbooks.populate_views(published)
        for v in published.views:
            try:
                server.views.populate_csv(v)
                b"".join(v.csv)  # csv is lazy; consuming it triggers the request
                view_checks[v.name] = "ok"
            except Exception as e:
                view_checks[v.name] = f"error: {str(e)[:200]}"
        if not view_checks:
            view_checks["_note"] = "workbook has no views to verify"

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
            "view_checks": view_checks,
            "graphql_checks": gql,
            # gate on what this run can prove: edits survived AND every view renders
            "verified": (all(roundtrip.values())
                         and all(v == "ok" for k, v in view_checks.items()
                                 if k != "_note")),
        }
        (out / "result.json").write_text(json.dumps(result, indent=2, ensure_ascii=False),
                                         encoding="utf-8")
        print("RESULT_JSON:", json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()
