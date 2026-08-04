"""One-shot: create a MANAGER account on the live server for a real user.

    python scripts\\create_manager_shereena.py

Writes: postman/.../02 Staff/.resources/Create Staff (shereena@krypsos.tech).resources/examples/... - Success.example.yaml
"""

from __future__ import annotations

import json
import ssl
import sys
import urllib.request
import urllib.error
from datetime import datetime, timezone
from pathlib import Path

BASE = "https://dineos-1unt.onrender.com"
PROJECT_ROOT = Path(__file__).resolve().parent.parent
STAFF_DIR = (
    PROJECT_ROOT
    / "postman"
    / "collections"
    / "DineOS v2 Backend API (Phase 1)"
    / "02 Staff"
    / ".resources"
)

NEW_USER = {
    "email": "shereena@krypsos.tech",
    "password": "Pass123456",
    "role": "MANAGER",
    "name": "Shereena",
}

CTX = ssl.create_default_context()


def http(method, path, *, body=None, bearer=None, timeout=90.0):
    url = f"{BASE}{path}"
    data = json.dumps(body).encode("utf-8") if body is not None else None
    headers = {"Accept": "application/json"}
    if body is not None:
        headers["Content-Type"] = "application/json"
    if bearer:
        headers["Authorization"] = f"Bearer {bearer}"
    req = urllib.request.Request(url, data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout, context=CTX) as r:
            return r.status, (json.dumps(body) if body else ""), r.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as e:
        return e.code, (json.dumps(body) if body else ""), e.read().decode("utf-8", errors="replace")


def yaml_quote(s: str) -> str:
    return "'" + s.replace("'", "''") + "'"


def main() -> int:
    # 1) admin login
    status, _, body = http("POST", "/v1/auth/login/",
                           body={"email": "admin@demo-bistro.demo", "password": "Demo@1234"})
    if status != 200:
        print(f"admin login failed: HTTP {status}: {body[:400]}")
        return 1
    admin_access = json.loads(body)["access"]
    print(f"admin logged in (access token len={len(admin_access)})")

    # 2) create the manager
    status, req_body, resp_body = http("POST", "/v1/staff/", body=NEW_USER, bearer=admin_access)
    if 200 <= status < 300:
        print(f"CREATED MANAGER: HTTP {status}")
        try:
            created = json.loads(resp_body)
            print(f"  id           = {created.get('id')}")
            print(f"  email        = {created.get('email')}")
            print(f"  name         = {created.get('name')}")
            print(f"  role         = {created.get('role')}")
            print(f"  role_id      = {created.get('role_id')}")
            print(f"  role_name    = {created.get('role_name')}")
            print(f"  is_active    = {created.get('is_active')}")
            r = created.get("restaurant") or {}
            print(f"  restaurant   = id={r.get('id')} slug={r.get('slug')} name={r.get('name')}")
        except json.JSONDecodeError:
            print(f"  (response not JSON) body={resp_body[:400]}")
    else:
        print(f"CREATE FAILED: HTTP {status}")
        print(f"  body={resp_body[:600]}")
        return 1

    # 3) confirm the new user can log in (verifies password + is_active)
    login_status, _, login_body = http(
        "POST", "/v1/auth/login/",
        body={"email": NEW_USER["email"], "password": NEW_USER["password"]},
    )
    if login_status == 200:
        login_data = json.loads(login_body)
        print(f"LOGIN VERIFIED: role={login_data.get('role')} role_id={login_data.get('role_id')} name={login_data.get('name')}")
    else:
        print(f"LOGIN VERIFY FAILED: HTTP {login_status}: {login_body[:400]}")

    # 4) save example alongside 02 Staff examples
    ex_dir = STAFF_DIR / "Create Staff (shereena@krypsos.tech).resources" / "examples"
    ex_dir.mkdir(parents=True, exist_ok=True)
    ex_path = ex_dir / "Create Staff (shereena@krypsos.tech) - Success.example.yaml"
    # Redact the password in the saved example so we never commit the plaintext.
    redacted = {**NEW_USER, "password": "<REDACTED>"}
    redacted_body = json.dumps(redacted)
    lines = [
        "$kind: http-example",
        f"name: {yaml_quote('Create Staff (shereena@krypsos.tech) - Success')}",
        "originalRequest:",
        "  method: POST",
        f"  url: {yaml_quote('{{base_url}}/staff/')}",
        "  headers:",
        "    Content-Type: application/json",
        "    Authorization: Bearer {{admin_token}}",
        "  body:",
        "    type: text",
        f"    content: {yaml_quote(redacted_body)}",
        f"code: {status}",
        "body:",
        "  type: text",
        f"  content: {yaml_quote(resp_body)}",
    ]
    ex_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"\nSaved example: {ex_path.relative_to(PROJECT_ROOT)}")
    print(f"(the plaintext password in the request body was replaced with <REDACTED> before writing)")
    print(f"\nDone at {datetime.now(timezone.utc).isoformat(timespec='seconds')}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
