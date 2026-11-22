"""Business Preference 台帳の参照抽出・参照切れ点検・Knowledge との同期計画。

Knowledge への書き込みは行わない（書き込みは Tableau MCP の manage-knowledge-context）。
本スクリプトは決定的に判定できる部分だけを担う。

subcommands:
    refs   台帳の statement からバッククォートの名前を抜き、depends_on を更新する（オフライン）
    check  参照名を実在する PDS 名・フィールド名と照合する（参照切れ・曖昧・未引用）
    plan   台帳と Knowledge のスナップショットを3方向比較し、create / update / pull を計画する

usage:
    python knowledge_ledger.py refs  --ledger bp.json [--write-back]
    python knowledge_ledger.py check --ledger bp.json --out check.json
                                     [--catalog catalog.json | --save-catalog catalog.json]
                                     [--text-file skill.md ...]
    python knowledge_ledger.py plan  --ledger bp.json --snapshot knowledge.json --out plan.json
                                     [--write-back]
"""
from __future__ import annotations

import argparse
import fnmatch
import json
import re
import sys
from pathlib import Path

# Windows コンソール (cp932) の文字化け対策
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")

# セットアップ時に置換されるプレースホルダ（{{NAME}}）。名前の照合対象から外す
PLACEHOLDER = re.compile(r"\{\{[^}]*\}\}")
BACKTICK = re.compile(r"`([^`\n]+)`")
# `field` in `datasource` の対（in の前後は空白のみ許す）
FIELD_IN_DS = re.compile(r"`([^`\n]+)`\s+in\s+`([^`\n]+)`")
# 名前の中の <table> のような可変部。glob の * に書き換えるべきもの
ANGLE_PLACEHOLDER = re.compile(r"<[^<>]+>")
# 未引用検出で拾う名前の最短長。2文字以下は地の文と衝突しやすい
MIN_UNQUOTED_LEN = 3
# Metadata API の 1 ページ件数（connection の first 上限に収まる値）
PAGE_SIZE = 100


# --- 台帳 ------------------------------------------------------------------------
def load_ledger(path: str) -> dict:
    """台帳を読む。title/text 形式（セットアップ用 JSON）も name/statement に読み替える。"""
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    for it in data.get("items", []):
        if "name" not in it and "title" in it:
            it["name"] = it["title"]
        if "statement" not in it and "text" in it:
            it["statement"] = it["text"]
        if not it.get("name") or not it.get("statement"):
            raise SystemExit(f"ledger item lacks name/statement: {json.dumps(it, ensure_ascii=False)[:200]}")
    names = [it["name"] for it in data.get("items", [])]
    dup = sorted({n for n in names if names.count(n) > 1})
    if dup:
        raise SystemExit(f"duplicate item names in ledger (name is the sync key): {dup}")
    return data


def save_ledger(path: str, data: dict) -> None:
    Path(path).write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


# --- 参照の抽出 ------------------------------------------------------------------
def extract_refs(text: str) -> list[dict]:
    """statement からバッククォートの名前を抜く。`X` in `Y` は field X in datasource Y。"""
    text = PLACEHOLDER.sub("", text)
    refs, consumed = [], set()
    for m in FIELD_IN_DS.finditer(text):
        refs.append({"kind": "field", "field": m.group(1), "datasource": m.group(2)})
        consumed.add(m.start(1))
        consumed.add(m.start(2))
    for m in BACKTICK.finditer(text):
        if m.start(1) in consumed:
            continue
        refs.append({"kind": "name", "name": m.group(1)})  # datasource か未修飾の field
    return refs


def ref_names(refs: list[dict]) -> list[str]:
    out = []
    for r in refs:
        out.append(f"{r['field']} in {r['datasource']}" if r["kind"] == "field" else r["name"])
    return sorted(set(out))


def cmd_refs(args) -> None:
    ledger = load_ledger(args.ledger)
    rows = []
    for it in ledger["items"]:
        refs = extract_refs(it["statement"])
        names = ref_names(refs)
        rows.append({"name": it["name"], "depends_on": names})
        if args.write_back:
            it["depends_on"] = names
    if args.write_back:
        save_ledger(args.ledger, ledger)
    print(json.dumps(rows, indent=2, ensure_ascii=False))


# --- カタログ（実在する名前） ---------------------------------------------------------
def _repo_modules():
    cur = Path(__file__).resolve()
    for _ in range(8):
        cur = cur.parent
        if (cur / "scripts" / "tableau_auth.py").exists():
            sys.path.insert(0, str(cur / "scripts"))
            break
    else:
        raise RuntimeError("repo-root scripts/ (with tableau_auth.py) not found")
    from tableau_auth import signed_in_server  # noqa: E402  (OAuth)
    from metadata_api import graphql  # noqa: E402
    return signed_in_server, graphql


LIST_Q = ("query($first:Int!,$after:String){publishedDatasourcesConnection("
          "first:$first,after:$after){nodes{name luid projectName}"
          "pageInfo{hasNextPage endCursor}}}")
FIELDS_Q = ("query($l:String!){publishedDatasources(filter:{luid:$l}){"
            "fields{name description __typename}}}")


def fetch_catalog(server, graphql) -> dict:
    """サイトの全 PDS 名を読む。フィールドは参照された PDS だけ後で読む（fields=None）。"""
    out, after = [], None
    while True:
        conn = graphql(server, LIST_Q, {"first": PAGE_SIZE, "after": after})[
            "publishedDatasourcesConnection"]
        out.extend({"name": n["name"], "luid": n["luid"], "project": n.get("projectName"),
                    "fields": None} for n in conn["nodes"])
        if not conn["pageInfo"]["hasNextPage"]:
            return {"datasources": out}
        after = conn["pageInfo"]["endCursor"]


def make_fetcher(server, graphql):
    def fetcher(luid: str) -> list[dict]:
        res = graphql(server, FIELDS_Q, {"l": luid})["publishedDatasources"]
        return [{"name": f["name"], "description": f.get("description"),
                 "type": f["__typename"]} for f in (res[0]["fields"] if res else [])]
    return fetcher


def ensure_fields(catalog: dict, ds: dict, fetcher) -> list[dict]:
    """必要になった PDS だけフィールドを読む（全 PDS の全列は読まない）。"""
    if ds.get("fields") is None:
        ds["fields"] = fetcher(ds["luid"]) if fetcher else []
    return ds["fields"]


# --- 照合 -----------------------------------------------------------------------
def match_datasources(catalog: dict, name: str) -> list[dict]:
    if "*" in name or "?" in name:
        return [d for d in catalog["datasources"] if fnmatch.fnmatchcase(d["name"], name)]
    return [d for d in catalog["datasources"] if d["name"] == name]


def field_names(fields: list[dict]) -> set[str]:
    return {f["name"] for f in fields}


def siblings(name: str, names: set[str]) -> list[str]:
    """`Shipped Date` に対する `Shipped Date (shipment_lines.csv)` のような同名列。"""
    return sorted(n for n in names if n != name and n.startswith(name + " ("))


# 二重引用符で囲んだ語は「問いの言葉」（C2 の解釈対象）で、名前の参照ではない
DOUBLE_QUOTED = re.compile(r'"[^"\n]*"|“[^”\n]*”')


def strip_quoted(text: str) -> str:
    return DOUBLE_QUOTED.sub(" ", BACKTICK.sub(" ", PLACEHOLDER.sub(" ", text)))


def find_unquoted(text: str, names: set[str]) -> list[str]:
    """引用されていない地の文に、実在する名前がそのまま出ていないか。"""
    plain = strip_quoted(text)
    hits = []
    for n in sorted(names, key=len, reverse=True):
        if len(n) < MIN_UNQUOTED_LEN:
            continue
        # 単語の途中に埋まった一致（Revenues の Revenue 等）は拾わない
        if re.search(r"(?<![\w])" + re.escape(n) + r"(?![\w])", plain):
            hits.append(n)
            plain = plain.replace(n, " ")  # 長い名前に含まれる短い名前を二重計上しない
    return hits


def check_text(text: str, catalog: dict, fetcher) -> dict:
    refs = extract_refs(text)
    findings = []
    all_ds_names = {d["name"] for d in catalog["datasources"]}

    # 1) データソース参照を先に解決する（未修飾の field の候補範囲になる）
    ds_in_scope: list[dict] = []
    name_refs = [r for r in refs if r["kind"] == "name"]
    pending_fields = []
    resolved: list[tuple[str, dict]] = []  # (field 名, PDS) の解決済みの対
    for r in refs:
        ds_name = r["datasource"] if r["kind"] == "field" else r["name"]
        if ANGLE_PLACEHOLDER.search(ds_name):
            glob = ANGLE_PLACEHOLDER.sub("*", ds_name)
            findings.append({"status": "placeholder_in_name", "ref": ds_name,
                             "hint": f"可変部は * の glob で書く（`{glob}`）",
                             "glob_matches": len(match_datasources(catalog, glob))})
            continue
        if r["kind"] == "name" and not match_datasources(catalog, ds_name):
            pending_fields.append(r)  # データソースでなければ未修飾 field の候補
            continue
        matched = match_datasources(catalog, ds_name)
        if not matched:
            findings.append({"status": "missing_datasource", "ref": ds_name})
            continue
        if "*" not in ds_name and "?" not in ds_name and len(matched) > 1:
            findings.append({"status": "duplicate_datasource_name", "ref": ds_name,
                             "projects": sorted(str(d.get("project")) for d in matched)})
        ds_in_scope.extend(matched)
        if r["kind"] == "field":
            hits = [d for d in matched if r["field"] in field_names(ensure_fields(catalog, d, fetcher))]
            ref = f"{r['field']} in {ds_name}"
            if not hits:
                findings.append({"status": "missing_field", "ref": ref})
                continue
            resolved.extend((r["field"], d) for d in hits)
            sib = sorted({s for d in hits for s in siblings(r["field"], field_names(d["fields"]))})
            if sib:
                findings.append({"status": "ambiguous_field", "ref": ref, "siblings": sib})
            else:
                findings.append({"status": "ok", "ref": ref,
                                 "datasources": [d["name"] for d in hits]})
        else:
            findings.append({"status": "ok", "ref": ds_name,
                             "datasources": [d["name"] for d in matched]})

    # 2) データソースでなかった単独の名前 = 未修飾の field
    for r in pending_fields:
        hits = [d for d in ds_in_scope if r["name"] in field_names(ensure_fields(catalog, d, fetcher))]
        if not ds_in_scope:
            findings.append({"status": "unresolved", "ref": r["name"],
                             "hint": "データソースでもなく、同じ文にデータソースの参照も無い"})
        elif not hits:
            findings.append({"status": "missing_field", "ref": r["name"],
                             "searched": sorted({d["name"] for d in ds_in_scope})})
        else:
            resolved.extend((r["name"], d) for d in hits)
            f = {"status": "unqualified_field", "ref": r["name"],
                 "datasources": sorted({d["name"] for d in hits}),
                 "hint": "`field` in `datasource` の形で修飾する"}
            sib = sorted({s for d in hits for s in siblings(r["name"], field_names(d["fields"]))})
            if sib:
                f["siblings"] = sib
                f["hint"] += "。同名の列があるので、どちらかを正確な名前で書く"
            findings.append(f)

    # 3) 地の文に出ている実在名（引用漏れ）
    unq_ds = find_unquoted(text, all_ds_names)
    for n in unq_ds:
        findings.append({"status": "unquoted_datasource", "ref": n})
        ds_in_scope.extend(match_datasources(catalog, n))
    scope_fields: set[str] = set()
    for d in {id(d): d for d in ds_in_scope}.values():
        scope_fields |= field_names(ensure_fields(catalog, d, fetcher))
    for n in find_unquoted(text, scope_fields):
        findings.append({"status": "unquoted_field", "ref": n})
        resolved.extend((n, d) for d in ds_in_scope if n in field_names(d.get("fields") or []))

    # 二重定義の判定材料: 参照先フィールドの現在の説明（判定は Skill 側で行う）
    defs, seen = [], set()
    for fname, d in resolved:
        key = (fname, d["luid"])
        if key in seen:
            continue
        seen.add(key)
        for fld in d.get("fields") or []:
            if fld["name"] == fname and fld.get("description"):
                defs.append({"field": fname, "datasource": d["name"],
                             "description": fld["description"]})
    return {"findings": findings, "field_descriptions": defs}


SEVERITY = {"missing_datasource": "error", "missing_field": "error", "unresolved": "error",
            "ambiguous_field": "warning", "unqualified_field": "warning",
            "duplicate_datasource_name": "warning", "placeholder_in_name": "warning",
            "unquoted_datasource": "warning", "unquoted_field": "warning", "ok": "ok"}


def cmd_check(args) -> None:
    if args.catalog:
        catalog = json.loads(Path(args.catalog).read_text(encoding="utf-8"))
        run_check(args, catalog, None)
        return
    signed_in_server, graphql = _repo_modules()
    with signed_in_server() as server:
        run_check(args, fetch_catalog(server, graphql), make_fetcher(server, graphql))


def run_check(args, catalog: dict, fetcher) -> None:
    items = []
    if args.ledger:
        for it in load_ledger(args.ledger)["items"]:
            items.append({"source": "ledger", "name": it["name"], "text": it["statement"]})
    for tf in args.text_file or []:
        items.append({"source": "text_file", "name": Path(tf).name,
                      "text": Path(tf).read_text(encoding="utf-8")})
    if not items:
        raise SystemExit("nothing to check: pass --ledger and/or --text-file")

    results, totals = [], {"error": 0, "warning": 0, "ok": 0}
    for it in items:
        r = check_text(it["text"], catalog, fetcher)
        for f in r["findings"]:
            f["severity"] = SEVERITY[f["status"]]
            totals[f["severity"]] += 1
        results.append({**{k: it[k] for k in ("source", "name")}, **r})

    out = {"totals": totals, "items": results}
    Path(args.out).parent.mkdir(parents=True, exist_ok=True)
    Path(args.out).write_text(json.dumps(out, indent=2, ensure_ascii=False), encoding="utf-8")
    if args.save_catalog:
        Path(args.save_catalog).write_text(json.dumps(catalog, indent=2, ensure_ascii=False),
                                           encoding="utf-8")
    print("RESULT_JSON:", json.dumps({"items": len(results), **totals}, ensure_ascii=False))


# --- 同期計画 ---------------------------------------------------------------------
def normalize_snapshot(raw) -> list[dict]:
    """inspect-knowledge-context list の出力（を保存した JSON）を正規形に寄せる。

    正規形: {context_id, name, statement, is_global, node_id}。statements が複数なら改行で連結する。
    """
    if isinstance(raw, dict):
        for key in ("contexts", "statements", "items", "data", "results"):
            if isinstance(raw.get(key), list):
                raw = raw[key]
                break
    if not isinstance(raw, list):
        raise SystemExit("snapshot: expected a list of contexts (or a dict wrapping one)")
    out = []
    for c in raw:
        sts = c.get("statements")
        if isinstance(sts, list):
            text = "\n".join(s["statement"] if isinstance(s, dict) else str(s) for s in sts)
        else:
            text = c.get("statement") or c.get("text") or ""
        cid = c.get("context_id") or c.get("contextId") or c.get("id")
        if not cid:
            raise SystemExit(f"snapshot entry lacks context id: {json.dumps(c, ensure_ascii=False)[:200]}")
        out.append({"context_id": cid, "name": c.get("name"), "statement": text,
                    "is_global": c.get("is_global", c.get("isGlobal")),
                    "node_id": c.get("node_id") or c.get("targetNodeId") or c.get("nodeId")})
    return out


def _norm(s: str | None) -> str:
    return re.sub(r"\s+", " ", (s or "")).strip()


def cmd_plan(args) -> None:
    ledger = load_ledger(args.ledger)
    snap = normalize_snapshot(json.loads(Path(args.snapshot).read_text(encoding="utf-8")))
    by_id = {c["context_id"]: c for c in snap}
    by_name: dict[str, list[dict]] = {}
    for c in snap:
        by_name.setdefault(c["name"], []).append(c)

    actions, linked = [], set()
    for it in ledger["items"]:
        mine, synced = _norm(it["statement"]), it.get("synced_statement")
        k = by_id.get(it.get("context_id")) if it.get("context_id") else None
        if it.get("context_id") and not k:
            actions.append({"action": "recreate", "name": it["name"], "context_id": it["context_id"],
                            "reason": "台帳の contextId が Knowledge に無い（外部で削除された可能性）。作り直すかは確認する"})
            continue
        if not k:
            cands = by_name.get(it["name"], [])
            if len(cands) > 1:
                actions.append({"action": "conflict", "name": it["name"],
                                "reason": "同名の context が Knowledge に複数ある",
                                "context_ids": [c["context_id"] for c in cands]})
                continue
            if not cands:
                actions.append({"action": "create", "name": it["name"], "statement": it["statement"],
                                "scope": it.get("scope", "global"),
                                "target_node_id": it.get("target_node_id")})
                continue
            k = cands[0]
            it["context_id"] = k["context_id"]  # 名前で紐づけた（write-back 時に保存）
        linked.add(k["context_id"])
        theirs = _norm(k["statement"])
        name_changed = k["name"] != it["name"]
        if theirs == mine:
            act = "rename" if name_changed else "unchanged"
            actions.append({"action": act, "name": it["name"], "context_id": k["context_id"]})
            if args.write_back:
                it["synced_statement"] = it["statement"]
        elif synced is not None and _norm(synced) == theirs:
            # Knowledge は前回同期のまま。台帳側の編集を押し出す
            actions.append({"action": "update", "name": it["name"], "context_id": k["context_id"],
                            "statement": it["statement"], "previous": k["statement"]})
        elif synced is not None and _norm(synced) == mine:
            # 台帳は前回同期のまま。Knowledge 側の編集を取り込む（正本は Knowledge）
            actions.append({"action": "pull", "name": it["name"], "context_id": k["context_id"],
                            "statement": k["statement"], "previous": it["statement"]})
            if args.write_back:
                it["statement"] = it["synced_statement"] = k["statement"]
                it["translation_stale"] = True
        else:
            actions.append({"action": "conflict", "name": it["name"], "context_id": k["context_id"],
                            "reason": "台帳と Knowledge の両方が前回同期から変わった（または未同期で文面が違う）",
                            "ledger": it["statement"], "knowledge": k["statement"]})

    for c in snap:
        if c["context_id"] not in linked and not any(
                a.get("context_id") == c["context_id"] for a in actions):
            actions.append({"action": "orphan", "name": c["name"], "context_id": c["context_id"],
                            "statement": c["statement"],
                            "reason": "台帳に無い。台帳に取り込むか、削除（要承認）するかを確認する"})

    counts: dict[str, int] = {}
    for a in actions:
        counts[a["action"]] = counts.get(a["action"], 0) + 1
    out = {"counts": counts, "actions": actions}
    Path(args.out).parent.mkdir(parents=True, exist_ok=True)
    Path(args.out).write_text(json.dumps(out, indent=2, ensure_ascii=False), encoding="utf-8")
    if args.write_back:
        save_ledger(args.ledger, ledger)
    print("RESULT_JSON:", json.dumps(counts, ensure_ascii=False))


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("refs", help="statement の参照名を抜き depends_on を更新する")
    p.add_argument("--ledger", required=True)
    p.add_argument("--write-back", action="store_true")
    p.set_defaults(func=cmd_refs)

    p = sub.add_parser("check", help="参照名を実在する名前と照合する")
    p.add_argument("--ledger")
    p.add_argument("--text-file", action="append", help="スキル等の Markdown（複数可）")
    p.add_argument("--catalog", help="照合に使うカタログ JSON（省略時は Metadata API から読む）")
    p.add_argument("--save-catalog", help="読んだカタログを保存する（再実行・オフライン用）")
    p.add_argument("--out", required=True)
    p.set_defaults(func=cmd_check)

    p = sub.add_parser("plan", help="台帳と Knowledge のスナップショットから同期計画を作る")
    p.add_argument("--ledger", required=True)
    p.add_argument("--snapshot", required=True)
    p.add_argument("--out", required=True)
    p.add_argument("--write-back", action="store_true",
                   help="一致した項目の contextId / synced_statement と pull を台帳に保存する")
    p.set_defaults(func=cmd_plan)

    args = ap.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
