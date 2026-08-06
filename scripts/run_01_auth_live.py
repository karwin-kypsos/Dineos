"""
Re-run every request in the "01 Auth" folder against the LIVE Render server,
capture full unredacted request+response bodies, overwrite the example YAML
files, and emit results as JSON for the docx generator.

Usage:
    .venv\\Scripts\\python.exe scripts\\run_01_auth_live.py

Output:
    - Overwrites: postman/.../01 Auth/.resources/<Name>.resources/examples/<Name> - Success.example.yaml
    - Writes:     docs/01_auth_v2_results.json
"""

from __future__ import annotations

import json
import ssl
import sys
import time
import urllib.request
import urllib.error
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

BASE = "https://dineos-1unt.onrender.com"
V1 = f"{BASE}/v1"

PROJECT_ROOT = Path(__file__).resolve().parent.parent
COLLECTION_DIR = (
    PROJECT_ROOT
    / "postman"
    / "collections"
    / "DineOS v2 Backend API (Phase 1)"
    / "01 Auth"
)
EXAMPLES_ROOT = COLLECTION_DIR / ".resources"
RESULTS_JSON = PROJECT_ROOT / "docs" / "01_auth_v2_results.json"

# Standard TLS context (Render presents a normal cert)
CTX = ssl.create_default_context()


def http(
    method: str,
    path: str,
    *,
    body: dict[str, Any] | None = None,
    bearer: str | None = None,
    timeout: float = 90.0,
) -> tuple[int, str, str]:
    """Return (status, request_body_str, response_body_str). Never raises on HTTP errors."""
    url = f"{BASE}{path}"  # path already begins with /v1/
    data_bytes = None
    request_body_str = ""
    headers = {"Accept": "application/json"}
    if body is not None:
        request_body_str = json.dumps(body)
        data_bytes = request_body_str.encode("utf-8")
        headers["Content-Type"] = "application/json"
    if bearer:
        headers["Authorization"] = f"Bearer {bearer}"

    req = urllib.request.Request(url, data=data_bytes, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout, context=CTX) as resp:
            status = resp.status
            response_body = resp.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as e:
        status = e.code
        response_body = e.read().decode("utf-8", errors="replace")
    return status, request_body_str, response_body


def yaml_quote(s: str) -> str:
    """Single-quote a YAML string safely (double single-quotes inside)."""
    return "'" + s.replace("'", "''") + "'"


def write_example(request_name: str, method: str, path: str, status: int,
                  request_body: str, response_body: str) -> Path:
    """Overwrite the Postman example YAML with the freshly captured response.

    Format matches the v1 examples the subagent created earlier.
    """
    ex_dir = EXAMPLES_ROOT / f"{request_name}.resources" / "examples"
    ex_dir.mkdir(parents=True, exist_ok=True)
    ex_path = ex_dir / f"{request_name} - Success.example.yaml"

    lines: list[str] = [
        "$kind: http-example",
        f"name: {yaml_quote(f'{request_name} - Success')}",
        "originalRequest:",
        f"  method: {method}",
        f"  url: {yaml_quote(f'{{{{base_url}}}}{path[len('/v1'):]}')}",
    ]
    if request_body:
        lines += [
            "  headers:",
            "    Content-Type: application/json",
            "  body:",
            "    type: text",
            f"    content: {yaml_quote(request_body)}",
        ]
    lines += [
        f"code: {status}",
    ]
    if response_body:
        lines += [
            "body:",
            "  type: text",
            f"  content: {yaml_quote(response_body)}",
        ]
    else:
        lines += ["body: {}"]
    ex_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return ex_path


def warm_up() -> None:
    """Ping /healthz/ to wake the Render dyno."""
    for attempt in range(3):
        try:
            with urllib.request.urlopen(f"{BASE}/healthz/", timeout=120, context=CTX) as r:
                if r.status == 200:
                    return
        except Exception as e:  # noqa: BLE001
            print(f"[warm-up attempt {attempt + 1}] {e}")
        time.sleep(3)


def main() -> int:
    warm_up()
    print(f"[{datetime.now(timezone.utc).isoformat(timespec='seconds')}] warmed up")

    results: list[dict[str, Any]] = []
    tokens: dict[str, str] = {}  # role -> access; also 'refresh' -> refresh
    steps = [
        ("Login â€” Admin",   "POST", "/v1/auth/login/",           {"email": "admin@demo-bistro.demo",   "password": "Demo@1234"}, None,          "admin"),
        ("Login â€” Manager", "POST", "/v1/auth/login/",           {"email": "manager@demo-bistro.demo", "password": "Demo@1234"}, None,          "manager"),
        ("Login â€” Server",  "POST", "/v1/auth/login/",           {"email": "server@demo-bistro.demo",  "password": "Demo@1234"}, None,          "server"),
        ("Login â€” Cashier", "POST", "/v1/auth/login/",           {"email": "cashier@demo-bistro.demo", "password": "Demo@1234"}, None,          "cashier"),
        ("Me",              "GET",  "/v1/auth/me/",              None,                                                             "admin",       None),
        ("Refresh Token",   "POST", "/v1/auth/refresh-token/",   "__REFRESH__",                                                    None,          "refreshed"),
        ("Change Password", "PATCH","/v1/auth/change-password/", {"current_password": "Demo@1234", "new_password": "Demo@1234"},   "admin",       None),
        ("Forgot Password", "POST", "/v1/auth/forgot-password/", {"email": "admin@demo-bistro.demo"},                              None,          None),
        ("Logout",          "POST", "/v1/auth/logout/",          "__REFRESH_FOR_LOGOUT__",                                          "admin",       None),
    ]

    for i, (name, method, path, body_or_marker, auth_role, capture_as) in enumerate(steps, start=1):
        # Resolve body markers that depend on prior tokens
        if body_or_marker == "__REFRESH__":
            body = {"refresh": tokens["refresh"]}
        elif body_or_marker == "__REFRESH_FOR_LOGOUT__":
            # Use the ORIGINAL admin refresh (matching the v1 flow), since the
            # Refresh Token request has no afterResponse script and the
            # Postman collection variable refresh_token is not updated.
            body = {"refresh": tokens["refresh"]}
        else:
            body = body_or_marker  # dict | None

        bearer = tokens.get(auth_role) if auth_role else None

        status, req_body_str, resp_body_str = http(method, path, body=body, bearer=bearer)
        pass_fail = "PASS" if 200 <= status < 300 else "FAIL"

        # Capture tokens from role logins
        try:
            resp_json = json.loads(resp_body_str) if resp_body_str else {}
        except json.JSONDecodeError:
            resp_json = {}

        if capture_as in {"admin", "manager", "server", "cashier"} and "access" in resp_json:
            tokens[capture_as] = resp_json["access"]
            if capture_as == "admin" and "refresh" in resp_json:
                tokens["refresh"] = resp_json["refresh"]

        # Save example only for 2xx
        example_saved = False
        if pass_fail == "PASS":
            ex_path = write_example(name, method, path, status, req_body_str, resp_body_str)
            example_saved = True
            print(f"[{i}] {name}: {status} PASS  (wrote {ex_path.relative_to(PROJECT_ROOT)})")
        else:
            print(f"[{i}] {name}: {status} FAIL  body={resp_body_str[:200]}")

        results.append({
            "step": i,
            "name": name,
            "method": method,
            "path": path,
            "fullUrl": f"{BASE}{path}",
            "requestBody": req_body_str if req_body_str else "(no request body â€” GET)",
            "responseBody": resp_body_str if resp_body_str else "(empty â€” 204 No Content)",
            "statusCode": status,
            "passFail": pass_fail,
            "exampleSaved": example_saved,
            "auth": f"Bearer {{{{{auth_role}_token}}}}" if auth_role else "None (public)",
            "notes": "",
        })

    # Write JSON for the docx generator
    RESULTS_JSON.parent.mkdir(parents=True, exist_ok=True)
    verification = {
        "loginAdminHasRoleId":   "\"role_id\":1"       in results[0]["responseBody"],
        "loginAdminHasRoleName": "\"role_name\":\"org_admin\"" in results[0]["responseBody"],
        "loginManagerRoleId":    2 if "\"role_id\":2" in results[1]["responseBody"] else None,
        "loginManagerRoleName":  "manager"  if "\"role_name\":\"manager\""  in results[1]["responseBody"] else None,
        "loginServerRoleId":     3 if "\"role_id\":3" in results[2]["responseBody"] else None,
        "loginServerRoleName":   "server"   if "\"role_name\":\"server\""   in results[2]["responseBody"] else None,
        "loginCashierRoleId":    4 if "\"role_id\":4" in results[3]["responseBody"] else None,
        "loginCashierRoleName":  "cashier"  if "\"role_name\":\"cashier\""  in results[3]["responseBody"] else None,
        "meHasRoleId":   "\"role_id\":1"           in results[4]["responseBody"],
        "meHasRoleName": "\"role_name\":\"org_admin\"" in results[4]["responseBody"],
    }
    RESULTS_JSON.write_text(
        json.dumps({
            "environment": {
                "server_root": BASE,
                "base_url": V1,
                "tenant_slug": "demo-bistro",
                "collection": "DineOS v2 Backend API (Phase 1)",
                "folder": "01 Auth",
                "run_date_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            },
            "verificationOfNewFields": verification,
            "results": results,
        }, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    print(f"\nWrote results: {RESULTS_JSON.relative_to(PROJECT_ROOT)}")

    passed = sum(1 for r in results if r["passFail"] == "PASS")
    failed = len(results) - passed
    print(f"Summary: {passed}/{len(results)} passed, {failed} failed")
    print(f"role_id/role_name verification: {verification}")

    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
