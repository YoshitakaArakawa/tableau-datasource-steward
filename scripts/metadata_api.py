"""Tableau Metadata API (GraphQL) client.

Tableau MCP は Metadata API をほぼ非対応のため、lineage 系（下流 workbook 列挙・
workbook 埋め込み calc の取得）は本モジュールで自前に GraphQL を叩く。

PAT でサインイン済みの TSC Server から auth token を取り出し、
POST {SERVER}/api/metadata/graphql を呼ぶ。

Usage:
    from tableau_auth import signed_in_server
    from metadata_api import graphql
    with signed_in_server() as server:
        data = graphql(server, "query { publishedDatasources(first:1){ name luid } }")
"""
from __future__ import annotations

import json
import requests


def graphql(server, query: str, variables: dict | None = None) -> dict:
    """Execute a GraphQL query against the Metadata API. Returns the `data` dict.

    Raises RuntimeError on HTTP error or GraphQL `errors`.
    """
    url = f"{server.server_address}/api/metadata/graphql"
    headers = {
        "X-Tableau-Auth": server.auth_token,
        "Content-Type": "application/json",
        "Accept": "application/json",
    }
    body = {"query": query}
    if variables:
        body["variables"] = variables
    resp = requests.post(url, headers=headers, data=json.dumps(body), timeout=60)
    if resp.status_code != 200:
        raise RuntimeError(f"Metadata API HTTP {resp.status_code}: {resp.text[:500]}")
    payload = resp.json()
    if payload.get("errors"):
        raise RuntimeError(f"GraphQL errors: {json.dumps(payload['errors'])[:800]}")
    return payload.get("data", {})


if __name__ == "__main__":
    import sys
    sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parent))
    from tableau_auth import signed_in_server

    with signed_in_server() as server:
        data = graphql(
            server,
            "query { publishedDatasourcesConnection(first: 2) { nodes { name luid } totalCount } }",
        )
        print("OK metadata api:", json.dumps(data)[:400])
