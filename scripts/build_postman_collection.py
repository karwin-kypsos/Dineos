"""
Build a real Postman Collection v2.1 JSON file from the git-synced YAML
collection under postman/collections/, so it can be executed live through
Newman (Postman's official CLI collection runner) — the same execution
engine behind the Postman desktop app's Collection Runner.

Usage:
    .venv\\Scripts\\python.exe scripts\\build_postman_collection.py "02 Staff" [more folders...] -o out.json

Always prepends the four role logins from "01 Auth" as a setup step so
admin_token / manager_token / server_token / cashier_token are populated
via the collection's own afterResponse scripts, exactly as they would be
if a human ran the folders in order inside Postman.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import yaml

PROJECT_ROOT = Path(__file__).resolve().parent.parent
COLLECTION_ROOT = PROJECT_ROOT / "postman" / "collections" / "DineOS v2 Backend API (Phase 1)"

SETUP_LOGIN_REQUESTS = [
    "Login — Admin",
    "Login — Manager",
    "Login — Server",
    "Login — Cashier",
]


def load_yaml(path: Path) -> dict:
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def headers_to_list(headers: dict | None) -> list[dict]:
    if not headers:
        return []
    return [{"key": k, "value": v} for k, v in headers.items()]


def scripts_to_events(scripts: list[dict] | None) -> list[dict]:
    if not scripts:
        return []
    events = []
    for s in scripts:
        listen = "test" if s.get("type") == "afterResponse" else "prerequest"
        code = s.get("code", "")
        events.append({
            "listen": listen,
            "script": {"type": "text/javascript", "exec": code.split("\n")},
        })
    return events


def build_request_item(request_yaml_path: Path) -> dict:
    data = load_yaml(request_yaml_path)
    name = request_yaml_path.stem.replace(".request", "")
    item: dict = {
        "name": name,
        "request": {
            "method": data["method"],
            "header": headers_to_list(data.get("headers")),
            "url": data["url"],
        },
    }
    body = data.get("body")
    if body and body.get("type") == "text":
        item["request"]["body"] = {"mode": "raw", "raw": body.get("content", "")}
    events = scripts_to_events(data.get("scripts"))
    if events:
        item["event"] = events
    item["_order"] = data.get("order", 0)
    return item


def build_folder_item(folder_dir: Path) -> dict:
    request_files = sorted(folder_dir.glob("*.request.yaml"))
    items = [build_request_item(p) for p in request_files]
    items.sort(key=lambda i: i.pop("_order"))
    return {"name": folder_dir.name, "item": items}


def find_folder(folder_name: str) -> Path:
    matches = [p for p in COLLECTION_ROOT.iterdir() if p.is_dir() and p.name == folder_name]
    if not matches:
        raise SystemExit(f"Folder not found: {folder_name!r} under {COLLECTION_ROOT}")
    return matches[0]


def collection_variables() -> list[dict]:
    root_def = load_yaml(COLLECTION_ROOT / ".resources" / "definition.yaml")
    out = []
    for v in root_def.get("variables", []):
        out.append({"key": v["key"], "value": v.get("value", "")})
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("folders", nargs="+", help="Folder names to include, e.g. \"02 Staff\"")
    ap.add_argument("-o", "--output", required=True, help="Output collection JSON path")
    ap.add_argument("--no-setup-logins", action="store_true",
                     help="Skip prepending the 01 Auth role logins")
    args = ap.parse_args()

    auth_dir = find_folder("01 Auth")
    setup_items = []
    if not args.no_setup_logins:
        for req_name in SETUP_LOGIN_REQUESTS:
            req_path = auth_dir / f"{req_name}.request.yaml"
            setup_items.append(build_request_item(req_path))
        setup_items.sort(key=lambda i: i.pop("_order", 0) if "_order" in i else 0)

    folder_items = [build_folder_item(find_folder(name)) for name in args.folders]

    top_items = []
    if setup_items:
        for i in setup_items:
            i.pop("_order", None)
        top_items.append({"name": "00 Setup (logins)", "item": setup_items})
    top_items.extend(folder_items)

    collection = {
        "info": {
            "name": "DineOS v2 Backend API (Phase 1) — live run",
            "schema": "https://schema.getpostman.com/json/collection/v2.1.0/collection.json",
        },
        "item": top_items,
        "variable": collection_variables(),
    }

    out_path = Path(args.output)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(collection, indent=2, ensure_ascii=False), encoding="utf-8")
    total_requests = sum(len(f["item"]) for f in top_items)
    print(f"Wrote {out_path} ({total_requests} requests across {len(top_items)} folders)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
