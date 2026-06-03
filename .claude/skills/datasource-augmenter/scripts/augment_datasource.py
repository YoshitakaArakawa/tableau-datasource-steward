"""Augment a Published Data Source: inject field descriptions and calculated fields,
then publish (CreateNew by default) and verify via round-trip.

Reads a change-set spec (JSON), downloads the source PDS, edits its .tds XML
(newline-agnostic; .tds may use CRLF), republishes, re-downloads and checks that
the edits survived. Publishing defaults to CreateNew (a new datasource); Overwrite
is destructive and must be requested explicitly.

usage:
    python augment_datasource.py --spec spec.json --out-dir <dir>

Auth: OAuth (scripts/tableau_auth.py, signed_in_server()). Spec format:
    references/change-set-format.md
XML edit format:
    references/tds-edit-format.md
"""
from __future__ import annotations

import argparse
import html
import json
import re
import sys
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

# Tableau Desktop の慣例に合わせた calc の opaque id プレフィックス
CALC_NAME_PREFIX = "[Calculation_steward_"


# --- .tdsx / .tds io -----------------------------------------------------------
def read_tds(tdsx: Path):
    z = zipfile.ZipFile(tdsx)
    tds_name = next(n for n in z.namelist() if n.endswith(".tds"))
    return tds_name, z.read(tds_name).decode("utf-8"), z


def write_tdsx(src_zip, tds_name: str, new_txt: str, out_path: Path):
    with zipfile.ZipFile(out_path, "w", zipfile.ZIP_DEFLATED) as zo:
        for item in src_zip.infolist():
            data = src_zip.read(item.filename)
            if item.filename == tds_name:
                data = new_txt.encode("utf-8")
            zo.writestr(item, data)


# --- XML edits (newline-agnostic) ----------------------------------------------
def _xml_escape(text: str) -> str:
    # XML attribute / text escaping incl. single quote (attrs use single quotes)
    return (text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
                .replace("'", "&apos;").replace('"', "&quot;"))


def _desc_block(text: str, indent: str = "  ") -> str:
    return (f"\n{indent}  <desc>\n{indent}    <formatted-text>\n"
            f"{indent}      <run>{_xml_escape(text)}</run>\n"
            f"{indent}    </formatted-text>\n{indent}  </desc>\n{indent}")


def _column_attr_pattern(field_name: str) -> str:
    """Match a datasource-level <column> by display name.

    A column carries `caption='X'` only when its display name differs from its
    internal `name`; when they are equal, only `name='[X]'` is present. Match either.
    """
    esc = re.escape(_xml_escape(field_name))
    return rf"<column\b[^>]*\b(?:caption='{esc}'|name='\[{esc}\]')[^>]*?"


# datatype（属性） / role / type は datatype から導出
def _role_type(datatype: str) -> tuple[str, str]:
    if datatype in ("integer", "real"):
        return "measure", "quantitative"
    return "dimension", "nominal"


def _lookup_metadata_record(txt: str, field: str) -> dict | None:
    """metadata-records から field（remote-name か local-name）に一致する列メタを引く。

    datasource-level <column> が無い列に説明を付けるとき、ここから内部名と型を取り
    新規 <column> を合成する。
    """
    for m in re.finditer(r"<metadata-record class='column'>(.*?)</metadata-record>", txt, re.S):
        body = m.group(1)
        rn = re.search(r"<remote-name>(.*?)</remote-name>", body, re.S)
        ln = re.search(r"<local-name>\[(.*?)\]</local-name>", body, re.S)
        lt = re.search(r"<local-type>(.*?)</local-type>", body, re.S)
        remote = rn.group(1) if rn else None
        local_id = ln.group(1) if ln else None
        if field in (remote, local_id):
            return {"local_id": local_id or field, "datatype": (lt.group(1) if lt else "string")}
    return None


def _insert_at_datasource_level(txt: str, block: str) -> str:
    """<aliases .../> 直後（無ければ </connection> 直後）に block を挿入（改行非依存）。"""
    anchor = "<aliases enabled='yes' />"
    if anchor in txt:
        return txt.replace(anchor, anchor + "\n" + block, 1)
    m = re.search(r"</connection>", txt)
    if not m:
        raise ValueError("no <aliases> nor </connection> anchor for insertion")
    return txt[:m.end()] + "\n" + block + txt[m.end():]


def inject_description(txt: str, field_caption: str, text: str) -> str:
    """Set the <desc> on a datasource-level <column> identified by display name.

    Matches by caption='X' or name='[X]'. Handles self-closing columns and
    open columns (existing <desc> is replaced). If no datasource-level <column>
    exists for the field (common in extract-based .tds), synthesize one from the
    matching metadata-record.
    """
    base = _column_attr_pattern(field_caption)
    # self-closing: <column ... />
    m = re.search(base + r"/>", txt)
    if m:
        col = m.group(0)
        opened = col[:-2].rstrip() + ">"  # drop '/>'
        return txt.replace(col, opened + _desc_block(text) + "</column>", 1)
    # open form: <column ...> ... </column>
    m = re.search("(" + base + r">)(.*?)(</column>)", txt, re.S)
    if not m:
        # no datasource-level <column>: synthesize from metadata-record
        meta = _lookup_metadata_record(txt, field_caption)
        if not meta:
            raise ValueError(f"field not found (no <column> nor metadata-record): {field_caption!r}")
        role, type_ = _role_type(meta["datatype"])
        col = (f"  <column caption='{_xml_escape(field_caption)}' datatype='{meta['datatype']}' "
               f"name='[{meta['local_id']}]' role='{role}' type='{type_}'>"
               f"{_desc_block(text)}\n  </column>\n")
        return _insert_at_datasource_level(txt, col)
    open_tag, body, close = m.group(1), m.group(2), m.group(3)
    body = re.sub(r"\s*<desc>.*?</desc>", "", body, flags=re.S)  # drop existing desc
    return txt[:m.start()] + open_tag + _desc_block(text) + body.strip() + "\n  " + close + txt[m.end():]


def calc_block(idx: int, caption: str, formula: str, datatype: str,
               role: str, type_: str, description: str | None) -> str:
    desc = _desc_block(text=description) if description else ""
    name = f"{CALC_NAME_PREFIX}{idx}]"
    return (
        f"  <column caption='{_xml_escape(caption)}' datatype='{datatype}' "
        f"name='{name}' role='{role}' type='{type_}'>\n"
        f"    <calculation class='tableau' formula='{_xml_escape(formula)}' />"
        f"{desc}\n  </column>\n"
    )


def inject_calcs(txt: str, calcs: list[dict]) -> str:
    if not calcs:
        return txt
    blocks = ""
    for i, c in enumerate(calcs, start=1):
        role = c.get("role") or _role_type(c["datatype"])[0]
        type_ = c.get("type") or _role_type(c["datatype"])[1]
        blocks += calc_block(i, c["caption"], c["formula"], c["datatype"],
                             role, type_, c.get("description"))
    return _insert_at_datasource_level(txt, blocks)


# --- verify --------------------------------------------------------------------
def verify_tds(txt: str, spec: dict) -> dict:
    checks = {}
    for d in spec.get("descriptions", []):
        checks[f"desc:{d['field_caption']}"] = _xml_escape(d["text"]) in txt
    for c in spec.get("calcs", []):
        checks[f"calc:{c['caption']}"] = _xml_escape(c["caption"]) in txt
    return checks


# --- main ----------------------------------------------------------------------
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

    source_luid = spec["source_luid"]
    target = spec.get("target", {})
    new_name = target.get("new_name")
    if mode == "CreateNew" and not new_name:
        raise SystemExit("CreateNew requires target.new_name")

    with signed_in_server() as server:
        # download source
        orig_tdsx = Path(server.datasources.download(
            source_luid, filepath=str(out / "original.tdsx"), include_extract=True))
        tds_name, txt, src_zip = read_tds(orig_tdsx)
        (out / "original.tds").write_text(txt, encoding="utf-8")

        # edits
        for d in spec.get("descriptions", []):
            txt = inject_description(txt, d["field_caption"], d["text"])
        txt = inject_calcs(txt, spec.get("calcs", []))
        (out / "edited.tds").write_text(txt, encoding="utf-8")
        pre = verify_tds(txt, spec)
        if not all(pre.values()):
            raise SystemExit(f"pre-publish edit incomplete: {pre}")

        edited_tdsx = out / "edited.tdsx"
        write_tdsx(src_zip, tds_name, txt, edited_tdsx)

        # project resolution
        project_id = target.get("project_id")
        if not project_id and mode == "CreateNew":
            src = server.datasources.get_by_id(source_luid)
            project_id = src.project_id  # inherit source project

        item = TSC.DatasourceItem(project_id)
        item.name = new_name if mode == "CreateNew" else server.datasources.get_by_id(source_luid).name
        # datasource-level description (grain statement). REST exposes this only at
        # publish time: the publish request accepts a description attribute, while
        # Update Data Source does not. TSC's publish serializes item.description.
        ds_desc = (spec.get("datasource") or {}).get("description")
        if ds_desc:
            item.description = ds_desc
        pub_mode = (TSC.Server.PublishMode.CreateNew if mode == "CreateNew"
                    else TSC.Server.PublishMode.Overwrite)
        published = server.datasources.publish(item, str(edited_tdsx), mode=pub_mode)

        # verify round-trip
        ver_tdsx = Path(server.datasources.download(
            published.id, filepath=str(out / "verified.tdsx"), include_extract=False))
        _, v_txt, _ = read_tds(ver_tdsx)
        (out / "verified.tds").write_text(v_txt, encoding="utf-8")
        post = verify_tds(v_txt, spec)

        # calcs are not surfaced by VDS metadata; confirm via GraphQL (supplementary).
        # A freshly published PDS may not be indexed by the Metadata API yet, so this
        # is best-effort: None means "not yet indexed", and calc survival is already
        # proven by the .tds round-trip (post). verified does not depend on this.
        calc_seen = {}
        if spec.get("calcs"):
            try:
                pubs = graphql(server,
                               "query($l:String!){publishedDatasources(filter:{luid:$l})"
                               "{fields{name __typename}}}", {"l": published.id}
                               ).get("publishedDatasources") or []
                names = ({f["name"] for f in pubs[0]["fields"]
                          if f["__typename"] == "CalculatedField"} if pubs else None)
                for c in spec["calcs"]:
                    calc_seen[c["caption"]] = (c["caption"] in names) if names is not None else None
            except Exception as e:  # indexing lag / transient: don't fail the run
                calc_seen = {"_note": f"graphql check skipped: {str(e)[:100]}"}

        # grain is a server-side catalog attribute (not in the .tds), so re-query it
        ds_desc_check = {}
        if ds_desc:
            got = server.datasources.get_by_id(published.id).description
            ds_desc_check["datasource.description"] = (got == ds_desc)

        result = {
            "published_luid": published.id,
            "published_name": published.name,
            "mode": mode,
            "roundtrip_checks": post,
            "calc_registered_graphql": calc_seen,
            "datasource_description_check": ds_desc_check,
            # calc survival is covered by post (.tds round-trip); calc_registered_graphql
            # is supplementary (subject to indexing lag) and excluded from verified.
            "verified": (all(post.values()) and all(ds_desc_check.values() or [True])),
        }
        (out / "result.json").write_text(json.dumps(result, indent=2, ensure_ascii=False),
                                         encoding="utf-8")
        print("RESULT_JSON:", json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()
