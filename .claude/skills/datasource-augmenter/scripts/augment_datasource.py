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
import difflib
import html
import json
import re
import sys
import time
import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path

# Windows コンソール (cp932) は日本語フィールド名を化けさせ、RESULT_JSON の照合を壊す。
# フィールド名の照合はファイル経由が正だが、stdout/stderr も UTF-8 に固定する。
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")


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
import requests  # noqa: E402
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


def _existing_display_names(txt: str) -> set[str]:
    """.tds 中の全 <column> の表示名を XML-escaped のまま集める。

    caption='X' があればそれが表示名。caption が無い列は name='[X]' が表示名を兼ねる
    （caption は display 名と内部名が違うときだけ存在する）。calc 列に限定しないのは、
    Tableau が同一 caption の列を区別できないため（通常列との衝突も注入不可）。
    """
    names: set[str] = set()
    for m in re.finditer(r"<column\b[^>]*>", txt):
        tag = m.group(0)
        cap = re.search(r"\bcaption='([^']*)'", tag)
        if cap:
            names.add(cap.group(1))
        else:
            name = re.search(r"\bname='\[([^']*)\]'", tag)
            if name:
                names.add(name.group(1))
    return names


def inject_calcs(txt: str, calcs: list[dict]) -> str:
    if not calcs:
        return txt
    # 冪等性ガード: promote 再実行や部分失敗後のリトライで同一 calc を重複注入しない。
    # 1 件でも衝突があれば、どの calc も注入せずに全体を止める（部分適用を作らない）。
    existing = _existing_display_names(txt)
    dup = [c["caption"] for c in calcs if _xml_escape(c["caption"]) in existing]
    if dup:
        raise SystemExit(
            "calc caption が既に PDS に存在する: "
            + ", ".join(repr(d) for d in dup)
            + "。promote / 注入済みでないか確認せよ。再実行なら該当 calc を spec から除外する。")
    # 内部名 [Calculation_steward_N] は既存最大 N の続番から振る（内部名の衝突回避）
    start = 1 + max(
        (int(n) for n in re.findall(re.escape(CALC_NAME_PREFIX) + r"(\d+)\]", txt)),
        default=0)
    blocks = ""
    for i, c in enumerate(calcs, start=start):
        role = c.get("role") or _role_type(c["datatype"])[0]
        type_ = c.get("type") or _role_type(c["datatype"])[1]
        blocks += calc_block(i, c["caption"], c["formula"], c["datatype"],
                             role, type_, c.get("description"))
    return _insert_at_datasource_level(txt, blocks)


# --- desc-only Overwrite: diff gate & preflight --------------------------------
def desc_only_diff_gate(orig_txt: str, edited_txt: str) -> dict:
    """publish 前の機械証明: 編集差分が <desc> 関連に限られることを確認する。

    per-PDS の人間承認の代わりになる安全装置。両 XML から (a) すべての <desc> を除去し、
    (b) 除去後に空になった「元に存在しない <column>」（desc だけを載せるために
    metadata-record から合成した殻）を取り除いた上で、canonical XML の一致を要求する。
    一致しなければ desc 以外への変更が混入しており、publish してはならない。
    """
    def _strip_desc(txt: str):
        root = ET.fromstring(txt)
        for parent in root.iter():
            for child in list(parent):
                if child.tag == "desc":
                    parent.remove(child)
        # desc 挿入は周囲の整形用空白（改行・インデント）も変える。意味を持たない
        # 空白のみのテキストノードは比較から外す（非空白テキストは厳密比較のまま）。
        for el in root.iter():
            if el.text is not None and not el.text.strip():
                el.text = None
            if el.tail is not None and not el.tail.strip():
                el.tail = None
        return root

    orig_root = _strip_desc(orig_txt)
    orig_cols = {c.get("name") for c in orig_root.iter("column")}
    edited_root = _strip_desc(edited_txt)
    synthesized = []
    for parent in edited_root.iter():
        for child in list(parent):
            if (child.tag == "column" and len(child) == 0
                    and not (child.text or "").strip()
                    and child.get("name") not in orig_cols):
                synthesized.append(child.get("name"))
                parent.remove(child)

    a = ET.canonicalize(ET.tostring(orig_root, encoding="unicode"))
    b = ET.canonicalize(ET.tostring(edited_root, encoding="unicode"))
    ok = a == b
    gate = {"ok": ok, "synthesized_desc_columns": synthesized}
    if not ok:
        # 差分の先頭だけをヒントとして残す（全文 diff は out-dir の .tds を読む）
        delta = list(difflib.unified_diff(a.splitlines(), b.splitlines(), lineterm=""))
        gate["diff_head"] = [l for l in delta if l.startswith(("+", "-"))][:10]
    return gate


def _rest_get(server, path: str, params: dict | None = None):
    r = requests.get(
        f"{server.server_address}/api/{server.version}/sites/{server.site_id}/{path}",
        headers={"X-Tableau-Auth": server.auth_token, "Accept": "application/json"},
        params=params or {}, timeout=30)
    if r.status_code != 200:
        raise RuntimeError(f"REST GET {path}: HTTP {r.status_code} {r.text[:200]}")
    return r.json()


def preflight_desc_only(server, source_luid: str, oauth_username: str | None = None) -> dict:
    """desc-only Overwrite を自走してよいかを機械判定する。

    - connections: embedPassword=true の接続があれば block。republish は connection
      オブジェクトを作り直すため、埋め込み資格情報は失われうる（資格情報を埋めない
      PDS = extract / Published DS / 仮想接続入力なら影響なし）。例外は spec が
      `connection_credentials.oauth_username` で OAuth 再 embed を明示した場合:
      publish 時に実行ユーザーの Saved Credential を embed し直せるため、
      接続の userName が oauth_username と一致することを検証して続行する
      （不一致は block。実行ユーザーの Saved Credential では認証できない）。
    - upstream flow: 実行中 (InProgress / Pending) の flow run があれば block。
      download→publish の間に flow が完走するとデータが巻き戻るため。
      次回スケジュール実行 (nextRunAt) が近い場合は warning（block はしない。
      往復は通常数分で、実行と重なったケースは round-trip 検証と次回 flow 実行で
      自己回復する）。
    """
    pf: dict = {"ok": True, "blocks": [], "warnings": []}

    conns = (_rest_get(server, f"datasources/{source_luid}/connections")
             .get("connections", {}) or {}).get("connection", [])
    embedded = [c for c in conns if str(c.get("embedPassword")).lower() == "true"]
    pf["connections"] = {"n": len(conns), "embed_password": [c["id"] for c in embedded]}
    if embedded and not oauth_username:
        pf["ok"] = False
        pf["blocks"].append(
            f"embedPassword=true の接続あり: {[c['id'] for c in embedded]}。"
            "OAuth コネクタ（BigQuery / Google Drive 等）なら spec の"
            " connection_credentials.oauth_username（= 接続の userName）を指定すると"
            "実行ユーザーの Saved Credential を embed し直して republish できる。")
    elif embedded and oauth_username:
        mismatch = [c["id"] for c in embedded if c.get("userName") != oauth_username]
        if mismatch:
            pf["ok"] = False
            pf["blocks"].append(
                f"oauth_username {oauth_username!r} が接続の userName と不一致: {mismatch}。"
                "実行ユーザーの Saved Credential では認証できないため中止。")
        else:
            pf["reembed"] = {"oauth_username": oauth_username,
                             "connections": [c["id"] for c in embedded]}

    try:
        flows = (graphql(server,
                         "query($l:String!){publishedDatasources(filter:{luid:$l})"
                         "{upstreamFlows{luid name}}}", {"l": source_luid})
                 .get("publishedDatasources") or [{}])[0].get("upstreamFlows") or []
        pf["upstream_flows"] = [{"luid": f["luid"], "name": f["name"]} for f in flows]
        flow_luids = {f["luid"] for f in flows}
        if flow_luids:
            runs = (_rest_get(server, "flows/runs").get("flowRuns", {}) or {}).get("flowRuns", [])
            active = [r for r in runs
                      if r.get("flowId") in flow_luids
                      and r.get("status") in ("InProgress", "Pending", "Queued")]
            if active:
                pf["ok"] = False
                pf["blocks"].append(
                    f"実行中の upstream flow run: {[r.get('id') for r in active]}")
            tasks = (_rest_get(server, "tasks/runFlow").get("tasks", {}) or {}).get("task", [])
            next_runs = [t.get("flowRun", {}).get("schedule", {}).get("nextRunAt")
                         for t in tasks
                         if (t.get("flowRun", {}).get("flow", {}) or {}).get("id") in flow_luids]
            next_runs = sorted(n for n in next_runs if n)
            pf["next_scheduled_run"] = next_runs[0] if next_runs else None
            if next_runs:
                pf["warnings"].append(
                    f"upstream flow の次回実行 {next_runs[0]}。往復と重なるなら後回しを検討")
    except Exception as e:  # flow 情報が取れない場合は warning に落とす（block しない）
        pf["warnings"].append(f"flow 状態の確認が不完全: {str(e)[:150]}")

    return pf


# --- verify --------------------------------------------------------------------
def verify_tds(txt: str, spec: dict) -> dict:
    checks = {}
    for d in spec.get("descriptions", []):
        checks[f"desc:{d['field_caption']}"] = _xml_escape(d["text"]) in txt
    for c in spec.get("calcs", []):
        checks[f"calc:{c['caption']}"] = _xml_escape(c["caption"]) in txt
    return checks


# --- OAuth saved-credential re-embed --------------------------------------------
def _oauth_credentials(spec: dict):
    """spec の connection_credentials.oauth_username から publish 用の資格情報を作る。

    OAuth コネクタ（BigQuery / Google Drive 等）の republish は connection を作り直す
    ため embed 済み資格情報が失われる。publish リクエストに oauth=True / embed=True の
    ConnectionCredentials を付けると、実行ユーザーの「保存済み認証情報 (Saved
    Credentials)」がサーバー側で embed され直す（生トークンは API に流れない）。
    前提: name = 接続の userName、かつその Saved Credential が実行ユーザー本人に
    登録済み（初回のみ Tableau の Account Settings で UI 登録）。
    """
    username = (spec.get("connection_credentials") or {}).get("oauth_username")
    if not username:
        return None
    return TSC.ConnectionCredentials(name=username, password="", embed=True, oauth=True)


def verify_embed(server, published_luid: str) -> dict:
    """publish 後に接続の embedPassword が維持されているかを REST 直読で検証する。"""
    conns = (_rest_get(server, f"datasources/{published_luid}/connections")
             .get("connections", {}) or {}).get("connection", [])
    embedded = [str(c.get("embedPassword")).lower() == "true" for c in conns]
    return {"connections": len(conns), "embed_password_all": bool(conns) and all(embedded)}


# --- main ----------------------------------------------------------------------
def rollback(spec_path: str, out_dir: str) -> None:
    """out-dir に保全した original.tdsx を元 PDS へ Overwrite 再 publish して巻き戻す。"""
    spec = json.loads(Path(spec_path).read_text(encoding="utf-8"))
    orig = Path(out_dir) / "original.tdsx"
    if not orig.exists():
        raise SystemExit(f"original.tdsx not found in {out_dir}")
    with signed_in_server() as server:
        src_item = server.datasources.get_by_id(spec["source_luid"])
        item = TSC.DatasourceItem(src_item.project_id)
        item.name = src_item.name
        if src_item.description:  # grain も元の値で戻す（publish 時にしか設定できない）
            item.description = src_item.description
        creds = _oauth_credentials(spec)  # 巻き戻しも republish。embed を同様に保持する
        published = server.datasources.publish(
            item, str(orig), mode=TSC.Server.PublishMode.Overwrite,
            connection_credentials=creds)
        result = {"phase": "rollback", "published_luid": published.id,
                  "luid_preserved": published.id == spec["source_luid"]}
        if creds:
            result["embed_check"] = verify_embed(server, published.id)
        print("RESULT_JSON:", json.dumps(result, ensure_ascii=False))
        if not result["luid_preserved"]:
            raise SystemExit(2)


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--spec", required=True)
    ap.add_argument("--out-dir", required=True)
    ap.add_argument("--rollback", action="store_true",
                    help="out-dir の original.tdsx を元 PDS へ Overwrite 再 publish して巻き戻す")
    args = ap.parse_args()

    if args.rollback:
        rollback(args.spec, args.out_dir)
        return

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

    # desc-only Overwrite: calcs を含まない Overwrite は「準非破壊」経路。
    # 人間の per-PDS 承認の代わりに preflight + diff ゲートで機械的に安全を証明する。
    desc_only = (mode == "Overwrite") and not spec.get("calcs")

    oauth_creds = _oauth_credentials(spec)
    oauth_username = oauth_creds.name if oauth_creds else None

    with signed_in_server() as server:
        preflight = None
        if desc_only:
            preflight = preflight_desc_only(server, source_luid, oauth_username)
            if not preflight["ok"]:
                result = {"mode": mode, "desc_only": True, "preflight": preflight,
                          "verified": False, "aborted": "preflight"}
                (out / "result.json").write_text(
                    json.dumps(result, indent=2, ensure_ascii=False), encoding="utf-8")
                print("RESULT_JSON:", json.dumps(result, ensure_ascii=False))
                raise SystemExit(2)

        # download source（filepath は拡張子抜きで渡す。TSC が正しい拡張子を付ける）
        orig_tdsx = Path(server.datasources.download(
            source_luid, filepath=str(out / "original"), include_extract=True))
        tds_name, txt, src_zip = read_tds(orig_tdsx)
        orig_txt = txt  # diff ゲートの比較元。ディスク経由にしない（下記 newline 注記）
        # evidence の .tds は newline='' で無変換書き出し。既定の write_text は Windows で
        # \n→\r\n 変換するため、CRLF の .tds が \r\r\n に化け、Custom SQL 等の複数行
        # text node を持つ PDS で読み戻しテキストが原本と一致しなくなる。
        (out / "original.tds").write_text(txt, encoding="utf-8", newline="")

        # edits
        for d in spec.get("descriptions", []):
            txt = inject_description(txt, d["field_caption"], d["text"])
        txt = inject_calcs(txt, spec.get("calcs", []))
        (out / "edited.tds").write_text(txt, encoding="utf-8", newline="")
        pre = verify_tds(txt, spec)
        if not all(pre.values()):
            raise SystemExit(f"pre-publish edit incomplete: {pre}")

        diff_gate = None
        if desc_only:
            diff_gate = desc_only_diff_gate(orig_txt, txt)
            if not diff_gate["ok"]:
                result = {"mode": mode, "desc_only": True, "preflight": preflight,
                          "diff_gate": diff_gate, "verified": False, "aborted": "diff_gate"}
                (out / "result.json").write_text(
                    json.dumps(result, indent=2, ensure_ascii=False), encoding="utf-8")
                print("RESULT_JSON:", json.dumps(result, ensure_ascii=False))
                raise SystemExit(2)

        edited_tdsx = out / "edited.tdsx"
        write_tdsx(src_zip, tds_name, txt, edited_tdsx)

        # project resolution。Overwrite の同一性判定は「名前 + プロジェクト」なので、
        # project を継承しないと既定プロジェクトへ同名の別 PDS を新規作成してしまう。
        src_item = server.datasources.get_by_id(source_luid)
        project_id = target.get("project_id") or src_item.project_id

        item = TSC.DatasourceItem(project_id)
        item.name = src_item.name if mode == "Overwrite" else new_name
        # datasource-level description (grain statement). REST exposes this only at
        # publish time: the publish request accepts a description attribute, while
        # Update Data Source does not. TSC's publish serializes item.description.
        ds_desc = (spec.get("datasource") or {}).get("description")
        if not ds_desc and mode == "Overwrite":
            # Overwrite re-publishes the whole datasource; grain lives in the catalog,
            # not the .tds, so an unset description here would silently CLEAR it.
            # Carry the existing grain forward when the spec doesn't override it.
            ds_desc = src_item.description or None
        if ds_desc:
            item.description = ds_desc
        pub_mode = (TSC.Server.PublishMode.CreateNew if mode == "CreateNew"
                    else TSC.Server.PublishMode.Overwrite)
        published = server.datasources.publish(item, str(edited_tdsx), mode=pub_mode,
                                               connection_credentials=oauth_creds)

        # verify round-trip
        ver_tdsx = Path(server.datasources.download(
            published.id, filepath=str(out / "verified"), include_extract=False))
        _, v_txt, _ = read_tds(ver_tdsx)
        (out / "verified.tds").write_text(v_txt, encoding="utf-8", newline="")
        post = verify_tds(v_txt, spec)

        # Supplementary GraphQL check (subject to Metadata API indexing lag): confirm
        # calc registration AND compute description coverage in a single fetch. A
        # freshly published PDS may not be indexed yet -> values become "_note"; this
        # never gates `verified` (calc/desc survival is proven by the .tds round-trip).
        # coverage.undescribed = physical (non-calc) columns still lacking a <desc>.
        calc_seen, coverage = {}, {}
        fields = None
        try:
            # Metadata API indexes a fresh publish asynchronously; retry so the check
            # returns real numbers instead of "_note". ~15s x5 covers typical lag
            # without long stalls, and breaks early once the PDS is indexed.
            upstream_tables: set[str] = set()
            for _attempt in range(5):
                pubs = graphql(server,
                               "query($l:String!){publishedDatasources(filter:{luid:$l})"
                               "{upstreamTables{name}"
                               "fields{name description __typename "
                               "... on ColumnField{dataType upstreamColumns{luid}}}}}",
                               {"l": published.id}).get("publishedDatasources") or []
                if pubs and pubs[0]["fields"]:
                    fields = pubs[0]["fields"]
                    upstream_tables = {t["name"] for t in pubs[0].get("upstreamTables") or []}
                    break
                if _attempt < 4:
                    time.sleep(15)
        except Exception as e:  # transient: don't fail the run
            calc_seen = coverage = {"_note": f"graphql check skipped: {str(e)[:100]}"}
        if fields is not None:
            calc_names = {f["name"] for f in fields
                          if f["__typename"] == "CalculatedField"}
            for c in spec.get("calcs", []):
                calc_seen[c["caption"]] = c["caption"] in calc_names
            # GraphQL は論理テーブル自体を ColumnField として数える。dataType=TABLE が
            # 確定的な目印（Custom SQL は upstreamTables が空で名前一致が効かない）。
            # 実列ではないので分母から除外する。
            pseudo = sorted(f["name"] for f in fields
                            if f["__typename"] == "ColumnField"
                            and not (f.get("upstreamColumns") or [])
                            and (f.get("dataType") == "TABLE"
                                 or f["name"] in upstream_tables))
            cols = [f for f in fields
                    if f["__typename"] != "CalculatedField" and f["name"] not in pseudo]
            undescribed = sorted(f["name"] for f in cols
                                 if not (f.get("description") or "").strip())
            coverage = {"regular_columns": len(cols),
                        "described": len(cols) - len(undescribed),
                        "undescribed": len(undescribed),
                        "undescribed_columns": undescribed,
                        "pseudo_table_fields_excluded": pseudo}
        elif not calc_seen:  # not indexed within window, no exception
            note = {"_note": "not yet indexed by Metadata API (re-read to confirm)"}
            calc_seen = dict(note) if spec.get("calcs") else {}
            coverage = dict(note)

        # grain is a server-side catalog attribute (not in the .tds), so re-query it
        ds_desc_check = {}
        if ds_desc:
            got = server.datasources.get_by_id(published.id).description
            ds_desc_check["datasource.description"] = (got == ds_desc)

        # Overwrite は「名前 + プロジェクト」一致で LUID を保持する。別 LUID になったら
        # project 解決を誤って重複 PDS を作っている（下流参照が繋がらない）。
        luid_preserved = (published.id == source_luid) if mode == "Overwrite" else None

        # OAuth 再 embed を要求した publish は、embed が実際に維持されたかを REST 直読で
        # 検証し verified の合否に含める（embed 喪失 = 次回リフレッシュが認証エラー）。
        embed_check = None
        if oauth_creds:
            embed_check = verify_embed(server, published.id)

        result = {
            "published_luid": published.id,
            "published_name": published.name,
            "mode": mode,
            "desc_only": desc_only,
            "luid_preserved": luid_preserved,
            "preflight": preflight,
            "diff_gate": diff_gate,
            "roundtrip_checks": post,
            "calc_registered_graphql": calc_seen,
            "coverage": coverage,
            "datasource_description_check": ds_desc_check,
            "embed_check": embed_check,
            # calc survival is covered by post (.tds round-trip); calc_registered_graphql
            # is supplementary (subject to indexing lag) and excluded from verified.
            "verified": (all(post.values()) and all(ds_desc_check.values() or [True])
                         and luid_preserved is not False
                         and (embed_check is None or embed_check["embed_password_all"])),
        }
        (out / "result.json").write_text(json.dumps(result, indent=2, ensure_ascii=False),
                                         encoding="utf-8")
        print("RESULT_JSON:", json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()
