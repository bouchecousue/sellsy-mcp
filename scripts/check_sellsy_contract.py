"""Compare the official API contracts used by the MCP. No credentials or business API calls."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
from urllib.parse import unquote

import httpx
import yaml

URL = "https://docs.sellsy.com/api/v2/dist/sellsy.v2.latest.yaml"
BASELINE = Path(__file__).resolve().parents[1] / "docs" / "sellsy-contract.json"
OPERATIONS = (
    [("/search", "get")]
    + [
        (f"/{family}/search", "post")
        for family in (
            "companies",
            "contacts",
            "items",
            "estimates",
            "invoices",
            "credit-notes",
            "orders",
            "deliveries",
            "staffs",
            "subscriptions",
            "opportunities",
            "comments",
        )
    ]
    + [
        ("/estimates", "post"),
        ("/estimates/{id}", "put"),
        ("/opportunities", "post"),
        ("/opportunities/{id}", "get"),
        ("/opportunities/pipelines", "get"),
        ("/opportunities/pipelines/{pipelineId}/steps", "get"),
        ("/subscriptions/payment-installments/search", "post"),
        ("/files/{id}", "get"),
        ("/taxes", "get"),
    ]
)


def dereference(spec, ref):
    current = spec
    for part in ref.removeprefix("#/").split("/"):
        part = part.replace("~1", "/").replace("~0", "~")
        if isinstance(current, list):
            current = current[int(part)]
        else:
            current = current[part if part in current else unquote(part)]
    return current


def fingerprints(spec):
    result = {}
    for path, method in OPERATIONS:
        root = {
            "operation": spec["paths"][path][method],
            "parameters": spec["paths"][path].get("parameters", []),
        }
        nodes = {}

        def visit(value, nodes=nodes):
            if isinstance(value, dict):
                ref = value.get("$ref")
                if ref and ref not in nodes:
                    nodes[ref] = dereference(spec, ref)
                    visit(nodes[ref])
                for key, item in value.items():
                    if key not in {"description", "summary", "example", "examples"}:
                        visit(item)
            elif isinstance(value, list):
                for item in value:
                    visit(item)

        visit(root)

        def contracts_only(value):
            if isinstance(value, dict):
                return {
                    k: contracts_only(v)
                    for k, v in value.items()
                    if k not in {"description", "summary", "example", "examples", "externalDocs"}
                }
            if isinstance(value, list):
                return [contracts_only(v) for v in value]
            return value

        raw = json.dumps(contracts_only({"root": root, "refs": nodes}), sort_keys=True).encode()
        result[f"{method.upper()} {path}"] = hashlib.sha256(raw).hexdigest()
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--refresh", action="store_true", help="Update baseline after reviewing the API changes"
    )
    parser.add_argument("--input", type=Path, help="Use a downloaded YAML or JSON instead of the network")
    args = parser.parse_args()
    if args.input:
        raw = args.input.read_bytes()
    else:
        response = httpx.get(URL, timeout=30, follow_redirects=True)
        response.raise_for_status()
        raw = response.content
    spec = yaml.safe_load(raw)
    current = {"url": URL, "api_version": spec["info"]["version"], "operations": fingerprints(spec)}
    if args.refresh:
        BASELINE.write_text(json.dumps(current, indent=2) + "\n")
    else:
        baseline = json.loads(BASELINE.read_text())
        changed = [
            key for key, value in current["operations"].items() if baseline["operations"].get(key) != value
        ]
        print(json.dumps({"api_version": current["api_version"], "changed_operations": changed}))
        if changed:
            raise SystemExit(1)


if __name__ == "__main__":
    main()
