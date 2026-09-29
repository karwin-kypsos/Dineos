"""Live end-to-end run of the Cashier + Customer apps against ONE branch.

    python scripts\\autotest_voyager.py --dry-run     (read-only: checks what a run would touch)
    python scripts\\autotest_voyager.py               (the real run)

Plays both apps through the exact API calls they make - the customer scans a
table, orders in rounds, asks for the bill and leaves feedback; the cashier
opens a shift, takes cash/card/UPI (and takeaway), is refused an underpayment,
and closes the shift once short and once matched - and asserts every
response along the way, printing PASS/FAIL per step.

Everything it creates is LEFT IN PLACE, on purpose: the point (2026-09-25,
per Karwin) is that Shereena can review the run in the Admin app. The
previous automated regression ran against demo-bistro's other branches and
cleaned up after itself, so nothing ever reached the Voyager branch she
reviews. Every name, note and comment carries a "AutoTest dd-Mon HH:MM" tag
so the test data is recognisable at a glance.

Guard rails, since this writes to production:
  * refuses to start if the cashier already has a shift open (the run
    closes its shifts, which would close someone's real one);
  * only uses tables that are AVAILABLE with no open session - never the
    sessions other testers left behind;
  * stops placing orders the moment a dish comes back flagged as out of
    stock, and keeps quantities small (each unit deducts recipe stock);
  * real takeaway orders only run with AUTOTEST_KDS_KEY set - only the
    kitchen can move an order to Ready, and an order that never gets there
    stays on the branch's Kitchen Display forever.

Configuration comes from the repo's .env (overridable by real environment
variables): AUTOTEST_BASE_URL, AUTOTEST_RESTAURANT_SLUG, AUTOTEST_BRANCH_SLUG,
AUTOTEST_CASHIER_EMAIL, AUTOTEST_CASHIER_PASSWORD, AUTOTEST_KDS_KEY (optional).
"""

from __future__ import annotations

import argparse
import json
import os
import re
import ssl
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
CTX = ssl.create_default_context()
MONEY_RE = re.compile(r"^-?\d+\.\d{2}$")
TWO_PLACES = Decimal("0.01")


# ---------------------------------------------------------------------------
# config + http
# ---------------------------------------------------------------------------

def load_config():
    values = {}
    env_file = PROJECT_ROOT / ".env"
    if env_file.exists():
        for line in env_file.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            values[key.strip()] = value.strip()
    for key in list(values) + [k for k in os.environ if k.startswith("AUTOTEST_")]:
        if key in os.environ:
            values[key] = os.environ[key]
    cfg = {
        "base": values.get("AUTOTEST_BASE_URL", "https://dineos-1unt.onrender.com").rstrip("/"),
        "restaurant": values.get("AUTOTEST_RESTAURANT_SLUG", "demo-bistro"),
        "branch": values.get("AUTOTEST_BRANCH_SLUG", "voyager"),
        "email": values.get("AUTOTEST_CASHIER_EMAIL", ""),
        "password": values.get("AUTOTEST_CASHIER_PASSWORD", ""),
        "kds_key": values.get("AUTOTEST_KDS_KEY", ""),
    }
    if not cfg["email"] or not cfg["password"]:
        sys.exit("AUTOTEST_CASHIER_EMAIL / AUTOTEST_CASHIER_PASSWORD are not set (.env or environment).")
    return cfg


class Api:
    def __init__(self, base, log):
        self.base = base
        self.log = log

    def call(self, method, path, body=None, token=None, kds_key=None, retries=None):
        # Only GETs are retried: a POST whose response was lost may still
        # have been applied, and replaying it would create a second order.
        retries = (2 if method == "GET" else 0) if retries is None else retries
        headers = {"Accept": "application/json"}
        data = None
        if body is not None:
            data = json.dumps(body).encode("utf-8")
            headers["Content-Type"] = "application/json"
        if token:
            headers["Authorization"] = f"Bearer {token}"
        if kds_key:
            headers["X-KDS-API-Key"] = kds_key
        for attempt in range(retries + 1):
            started = time.time()
            req = urllib.request.Request(self.base + path, data=data, method=method, headers=headers)
            try:
                with urllib.request.urlopen(req, timeout=150, context=CTX) as resp:
                    status, raw = resp.status, resp.read()
            except urllib.error.HTTPError as e:
                status, raw = e.code, e.read()
            except (urllib.error.URLError, TimeoutError, ConnectionError) as e:
                if attempt < retries:
                    time.sleep(3)
                    continue
                self.log.append({"method": method, "path": path, "body": _redact(body), "status": None, "error": str(e)})
                return None, {"error": str(e)}
            try:
                payload = json.loads(raw) if raw else None
            except ValueError:
                payload = raw.decode("utf-8", errors="replace")[:500]
            self.log.append({
                "method": method, "path": path, "body": _redact(body), "status": status,
                "ms": int((time.time() - started) * 1000), "response": _redact(payload),
            })
            return status, payload
        return None, None


def _redact(obj):
    if isinstance(obj, dict):
        return {k: ("<redacted>" if k in ("password", "access", "refresh") else _redact(v)) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_redact(v) for v in obj]
    return obj


def money(value):
    return Decimal(str(value)).quantize(TWO_PLACES, rounding=ROUND_HALF_UP)


def is_money_string(value):
    return isinstance(value, str) and bool(MONEY_RE.match(value))


# ---------------------------------------------------------------------------
# result recording
# ---------------------------------------------------------------------------

class Run:
    def __init__(self, tag):
        self.tag = tag
        self.results = []
        self.bills = []        # {"id","label","method","total","shift"}
        self.shifts = []       # {"id","label","expected_cash","counted","discrepancy","reason","total"}
        self.tables_used = set()
        self.units_ordered = 0
        self.feedback = []
        self.stock_out = False

    def check(self, sid, title, ok, detail=""):
        self.results.append({"id": sid, "title": title, "result": "PASS" if ok else "FAIL", "detail": detail})
        print(f"  {'PASS' if ok else 'FAIL'}  {sid:<4} {title}" + (f"  -- {detail}" if detail else ""))
        return ok

    def skip(self, sid, title, why):
        self.results.append({"id": sid, "title": title, "result": "SKIP", "detail": why})
        print(f"  SKIP  {sid:<4} {title}  -- {why}")

    def section(self, name):
        print(f"\n== {name}")


class Abort(Exception):
    pass


# ---------------------------------------------------------------------------
# the run
# ---------------------------------------------------------------------------

class Tester:
    def __init__(self, cfg, api, run, dry_run):
        self.cfg = cfg
        self.api = api
        self.run = run
        self.dry = dry_run
        self.token = None
        self.cashier = None
        self.free_tables = []
        self.dish = None
        self.price = None
        self.shift_id = None
        self.shift_total = Decimal("0")

    # ---- small wrappers -------------------------------------------------
    def staff(self, method, path, body=None):
        return self.api.call(method, path, body=body, token=self.token)

    def public(self, method, path, body=None):
        return self.api.call(method, path, body=body)

    def kitchen(self, method, path, body=None):
        return self.api.call(method, path, body=body, kds_key=self.cfg["kds_key"])

    # ---- setup ----------------------------------------------------------
    def setup(self):
        r = self.run
        r.section("Setup")
        status, _ = self.api.call("GET", "/healthz/", retries=4)
        if status != 200:
            raise Abort(f"server health check failed (HTTP {status})")
        status, login = self.public("POST", "/v1/auth/login/", {"email": self.cfg["email"], "password": self.cfg["password"]})
        if not r.check("A1", "Cashier logs in", status == 200 and isinstance(login, dict) and login.get("role") == "CASHIER",
                       f"HTTP {status}"):
            raise Abort("cashier login failed")
        self.token = login["access"]
        branch = login.get("branch") or {}
        self.cashier = {"id": login.get("user_id"), "name": login.get("name"), "branch": branch}
        if not r.check("A1b", f"Cashier belongs to the '{self.cfg['branch']}' branch",
                       branch.get("slug") == self.cfg["branch"], f"branch={branch.get('name')} ({branch.get('slug')})"):
            raise Abort("cashier is not in the target branch - refusing to run")

        status, home = self.staff("GET", "/v1/cashier/shifts/current/")
        if status != 200:
            raise Abort(f"could not read Cashier Home (HTTP {status})")
        if home.get("shift"):
            raise Abort("this cashier already has a shift OPEN. The run closes its shifts, which would close "
                        "that one - close it in the app (or wait until it is closed) and run again.")

        # Tables: the customer app's own QR lookup, table 1 upward.
        tables, misses = [], 0
        for n in range(1, 41):
            status, q = self.public("GET", f"/v1/tables/qr/{self.cfg['restaurant']}/{self.cfg['branch']}/{n}/")
            if status == 404:
                misses += 1
                if misses >= 3:
                    break
                continue
            misses = 0
            if status == 200 and isinstance(q, dict) and isinstance(q.get("table"), dict):
                t = q["table"]
                tables.append({"id": t["id"], "number": t["table_number"], "status": t["status"],
                               "session": t.get("active_session_id")})
        self.free_tables = [t for t in tables if t["status"] == "AVAILABLE" and not t["session"]]
        busy = [t["number"] for t in tables if t not in self.free_tables]
        print(f"  info  tables found: {[t['number'] for t in tables]}; free: {[t['number'] for t in self.free_tables]};"
              f" left alone (in use): {busy}")
        if not self.free_tables:
            raise Abort("no free table in the branch - nothing safe to test on")

        status, menu = self.public("GET", f"/v1/menu/customer/{self.free_tables[0]['id']}/")
        items = menu if isinstance(menu, list) else (menu or {}).get("results", []) if isinstance(menu, dict) else []
        available = [m for m in items if m.get("is_available")]
        if not available:
            raise Abort("the branch's customer menu has no available dish")
        # Cheapest available dish keeps the run's stock draw and totals small.
        self.dish = min(available, key=lambda m: Decimal(str(m["price"])))
        self.price = money(self.dish["price"])
        print(f"  info  dish used: {self.dish['name']} (id {self.dish['id']}) at Rs {self.price}")
        print(f"  info  kitchen steps: {'ON (AUTOTEST_KDS_KEY set)' if self.cfg['kds_key'] else 'OFF (no AUTOTEST_KDS_KEY)'}")
        if self.cfg["kds_key"]:
            status, _ = self.kitchen("GET", "/v1/kitchen/devices/me/")
            if not r.check("K0", "Kitchen Display key is valid", status == 200, f"HTTP {status}"):
                self.cfg["kds_key"] = ""

    # ---- customer side ----------------------------------------------------
    def next_table(self, index):
        # Prefer a different free table per scenario; tables free up again
        # once paid, so wrapping around to an earlier one is safe.
        return self.free_tables[index % len(self.free_tables)]

    def seat(self, table):
        status, sess = self.public("POST", f"/v1/tables/{table['id']}/session/")
        if status not in (200, 201) or not isinstance(sess, dict):
            raise Abort(f"could not open a session on table {table['number']} (HTTP {status})")
        if sess.get("status") != "ACTIVE" or (status == 200):
            # 200 means the table already had an open session: someone sat
            # down since setup. Not ours to touch.
            raise Abort(f"table {table['number']} was taken by someone else during the run")
        self.run.tables_used.add(table["number"])
        return sess["id"]

    def order(self, session_id, qty, item_note="", order_note=""):
        if self.run.stock_out:
            return None
        status, o = self.public("POST", "/v1/orders/", {
            "session_id": session_id, "notes": order_note,
            "items": [{"menu_item": self.dish["id"], "quantity": qty, "notes": item_note}],
        })
        if status != 201:
            raise Abort(f"customer order failed (HTTP {status}): {o}")
        self.run.units_ordered += qty
        if o.get("unavailable_items"):
            self.run.stock_out = True
            self.run.check("STK", f"{self.dish['name']} still in stock", False,
                           f"flagged out of stock: {o['unavailable_items']} - no further orders will be placed")
        return o

    def kitchen_to_ready(self, order_id):
        for target in ("ACCEPTED", "PREPARING", "READY"):
            status, _ = self.kitchen("PATCH", f"/v1/orders/{order_id}/status/", {"status": target})
            if status != 200:
                return False, f"{target}: HTTP {status}"
        return True, ""

    # ---- cashier side -----------------------------------------------------
    def home(self):
        status, home = self.staff("GET", "/v1/cashier/shifts/current/")
        return home if status == 200 else {}

    def pay(self, session_id, method, received=None):
        body = {"session_id": session_id, "payment_method": method}
        if received is not None:
            body["amount_received"] = str(received)
        return self.staff("POST", "/v1/bills/payment/", body)

    def record_bill(self, bill, label):
        self.run.bills.append({
            "id": bill["id"], "label": label, "method": bill.get("payment_method"),
            "total": bill.get("total_amount"), "shift": len(self.run.shifts) + 1,
        })
        self.shift_total += money(bill["total_amount"])

    # ---- scenarios --------------------------------------------------------
    def shift_one(self):
        r = self.run
        r.section("Shift 1 - open")
        status, shift = self.staff("POST", "/v1/cashier/shifts/open/")
        if not r.check("A2", "Start Shift opens a shift", status == 201 and shift.get("status") == "OPEN", f"HTTP {status}"):
            raise Abort("could not open a shift")
        self.shift_id = shift["id"]
        status, again = self.staff("POST", "/v1/cashier/shifts/open/")
        r.check("A3", "Start Shift again returns the same shift", status in (200, 201) and again.get("id") == self.shift_id)

        # --- Table A: two rounds, bill request, underpayment, cash + change, feedback
        t = self.next_table(0)
        r.section(f"Table {t['number']} - two rounds, cash with change")
        sid = self.seat(t)
        o1 = self.order(sid, 2, item_note=f"{r.tag}: less ice")
        h = self.home()
        r.check("B1", "Seated table appears on Cashier Home",
                any(x.get("table_number") == t["number"] for x in h.get("active_tables", [])))
        o2 = self.order(sid, 1, order_note=f"{r.tag}: round 2")
        if not (o1 and o2):
            raise Abort("ran out of stock before the first bill")
        if self.cfg["kds_key"]:
            for label, o in (("round 1", o1), ("round 2", o2)):
                ok, why = self.kitchen_to_ready(o["id"])
                r.check("KDS", f"Kitchen moves {label} to Ready", ok, why)
                if ok:
                    s1, _ = self.staff("PATCH", f"/v1/orders/{o['id']}/collected/")
                    s2, _ = self.staff("PATCH", f"/v1/orders/{o['id']}/served/")
                    r.check("KDS", f"{label} marked collected and served", s1 == 200 and s2 == 200, f"HTTP {s1}/{s2}")
        status, _ = self.public("POST", f"/v1/tables/{t['id']}/bill-request/")
        h = self.home()
        r.check("B2", "Bill request moves the table to Awaiting payment", status == 200 and
                any(x.get("table_number") == t["number"] for x in h.get("awaiting_payment", [])), f"HTTP {status}")

        status, pv = self.staff("GET", f"/v1/bills/session/{sid}/")
        ok = status == 200 and isinstance(pv, dict)
        detail = f"HTTP {status}"
        if ok:
            subtotal = money(pv["subtotal"])
            gst = Decimal(str(pv.get("gst_percentage", 0)))
            sc = Decimal(str(pv.get("service_charge_percentage", 0)))
            exp_sub = self.price * 3
            exp_tax = (exp_sub * gst / 100).quantize(TWO_PLACES, rounding=ROUND_HALF_UP)
            exp_sc = (exp_sub * sc / 100).quantize(TWO_PLACES, rounding=ROUND_HALF_UP)
            strings = all(is_money_string(pv[k]) for k in ("subtotal", "tax_amount", "service_charge", "total_amount"))
            ok = (subtotal == exp_sub and money(pv["tax_amount"]) == exp_tax and money(pv["service_charge"]) == exp_sc
                  and money(pv["total_amount"]) == exp_sub + exp_tax + exp_sc and strings
                  and pv.get("payment_status") == "BILL_REQUESTED")
            detail = (f"subtotal {pv['subtotal']} + GST {gst}% {pv['tax_amount']} + service {sc}% "
                      f"{pv['service_charge']} = {pv['total_amount']}; decimal strings: {strings}")
        r.check("C1", "Bill preview totals are right and shown as 2-decimal money", ok, detail)
        total = money(pv["total_amount"]) if status == 200 else None
        if total is None:
            raise Abort("no bill preview")

        short = min(Decimal("50.00"), total - Decimal("1.00"))
        status, body = self.pay(sid, "CASH", total - short)
        r.check("C2", "Underpayment is refused and names the shortfall",
                status == 400 and isinstance(body, dict) and body.get("shortfall") == str(short),
                f"HTTP {status}, shortfall {body.get('shortfall') if isinstance(body, dict) else body}")
        status, body = self.pay(sid, "CASH", "0.00")
        r.check("C3", "Rs 0.00 tendered is refused", status == 400, f"HTTP {status}")

        received = ((total // 500) + 1) * 500
        status, bill = self.pay(sid, "CASH", received)
        ok = (status == 201 and bill.get("payment_method") == "CASH"
              and money(bill.get("change_given")) == received - total
              and bill.get("table_number") == t["number"] and bool(bill.get("processed_by_name")))
        r.check("C4", "Cash payment with change is recorded", ok,
                f"total {bill.get('total_amount')}, received {received}, change {bill.get('change_given')}" if status == 201 else f"HTTP {status}")
        if status != 201:
            raise Abort("cash payment failed")
        self.record_bill(bill, f"Table {t['number']} (2 rounds)")
        bill_a = bill

        h = self.home()
        gone = not any(x.get("table_number") == t["number"] for x in h.get("awaiting_payment", []) + h.get("active_tables", []))
        r.check("C5", "Paid table leaves Home and Collected equals this shift's takings",
                gone and money(h.get("collected_today", "0")) == self.shift_total,
                f"collected_today {h.get('collected_today')} vs expected {self.shift_total}")

        comment = f"{r.tag}: mojito was great, automated test feedback"[:140]
        status, fb = self.public("POST", "/v1/feedback/submit/", {"bill_id": bill_a["id"], "rating": 4, "comment": comment})
        if r.check("F4", "Customer leaves 4-star feedback on the paid bill", status == 201, f"HTTP {status}"):
            r.feedback.append({"bill": bill_a["id"], "rating": 4, "comment": comment})

        # --- Table B: card, paid twice (double tap) -> one bill
        t = self.next_table(1)
        r.section(f"Table {t['number']} - card, double tap")
        sid = self.seat(t)
        if self.order(sid, 1):
            status, bill = self.pay(sid, "CARD")
            r.check("D1", "Card without amount received is recorded with no change",
                    status == 201 and bill.get("payment_method") == "CARD" and bill.get("change_given") is None,
                    f"HTTP {status}")
            if status == 201:
                self.record_bill(bill, f"Table {t['number']}")
                status2, again = self.pay(sid, "CARD")
                r.check("C6", "Paying the same table twice returns the same bill",
                        status2 in (200, 201) and again.get("id") == bill["id"], f"HTTP {status2}")
                self.card_bill = bill
        else:
            r.skip("D1", "Card payment", "out of stock")

        # --- Table C: UPI
        t = self.next_table(2)
        r.section(f"Table {t['number']} - UPI")
        sid = self.seat(t)
        if self.order(sid, 1):
            status, bill = self.pay(sid, "UPI")
            r.check("D2", "UPI at the counter is recorded", status == 201 and bill.get("payment_method") == "UPI", f"HTTP {status}")
            if status == 201:
                self.record_bill(bill, f"Table {t['number']}")
        else:
            r.skip("D2", "UPI payment", "out of stock")

        self.bill_a = bill_a

    def takeaway(self):
        r = self.run
        r.section("Takeaway")
        item = {"menu_item": self.dish["id"], "quantity": 1}
        status, body = self.staff("POST", "/v1/orders/takeaway/", {"items": [item]})
        r.check("G1", "Takeaway without a customer name is refused",
                status == 400 and isinstance(body, dict) and "customer_name" in body, f"HTTP {status}")
        status, body = self.staff("POST", "/v1/orders/takeaway/",
                                  {"customer_name": f"{r.tag} Check", "customer_phone": "abcdefghij", "items": [item]})
        r.check("G2", "Takeaway with phone 'abcdefghij' is refused",
                status == 400 and isinstance(body, dict) and "customer_phone" in body, f"HTTP {status}")
        status, body = self.staff("POST", "/v1/orders/takeaway/",
                                  {"customer_name": f"{r.tag} Check", "items": [{"menu_item": self.dish["id"], "quantity": 1000}]})
        r.check("G3", "Quantity 1000 on one line is refused", status == 400, f"HTTP {status}")

        if not self.cfg["kds_key"]:
            for sid, title in (("G5", "Real takeaway order"), ("G6", "Second takeaway round"),
                               ("G7", "Kitchen marks takeaway Ready"), ("G8", "Takeaway paid in cash")):
                r.skip(sid, title, "needs AUTOTEST_KDS_KEY - without it the order would be stuck on the Kitchen Display")
            return
        if r.stock_out:
            r.skip("G5", "Real takeaway order", "out of stock")
            return

        name = f"{r.tag} Priya"
        status, o1 = self.staff("POST", "/v1/orders/takeaway/", {
            "customer_name": name, "customer_phone": "9876543210", "notes": f"{r.tag}: takeaway",
            "items": [item],
        })
        if not r.check("G5", "Takeaway order is created", status == 201 and o1.get("unavailable_items") == [], f"HTTP {status}"):
            return
        self.run.units_ordered += 1
        status, o2 = self.staff("POST", "/v1/orders/takeaway/", {"existing_order_id": o1["id"], "items": [item]})
        ok2 = r.check("G6", "Second round is added without asking for the name again", status == 201, f"HTTP {status}")
        if ok2:
            self.run.units_ordered += 1
        ready = True
        for o in [o1] + ([o2] if ok2 else []):
            ok, why = self.kitchen_to_ready(o["id"])
            ready = ready and ok
        r.check("G7", "Kitchen moves the takeaway rounds to Ready", ready)

        status, pv = self.staff("GET", f"/v1/bills/takeaway/{o1['id']}/")
        rounds = 2 if ok2 else 1
        if not r.check("G8a", "Takeaway bill preview covers every round",
                       status == 200 and isinstance(pv, dict) and money(pv.get("subtotal", "0")) == self.price * rounds,
                       f"subtotal {pv.get('subtotal') if isinstance(pv, dict) else pv}"):
            return
        total = money(pv["total_amount"])
        received = ((total // 100) + 1) * 100
        status, bill = self.staff("POST", "/v1/bills/takeaway-payment/",
                                  {"order_id": o1["id"], "payment_method": "CASH", "amount_received": str(received)})
        ok = status == 201 and bill.get("table_number") is None and money(bill.get("change_given")) == received - total
        r.check("G8", "Takeaway paid in cash with change, no table number", ok,
                f"total {bill.get('total_amount')}, change {bill.get('change_given')}" if status == 201 else f"HTTP {status}")
        if status == 201:
            self.record_bill(bill, f"Takeaway '{name}' ({rounds} rounds)")
            self.takeaway_name = name
            for o in [o1] + ([o2] if ok2 else []):
                self.staff("PATCH", f"/v1/orders/{o['id']}/collected/")

    def history(self):
        r = self.run
        r.section("Bill History, bill detail, My Sales")
        ours = {b["id"] for b in r.bills}
        status, page = self.staff("GET", "/v1/bills/?date=today&page_size=100")
        rows = page.get("results", []) if isinstance(page, dict) else []
        found = {b["id"] for b in rows} & ours
        r.check("H1", "Today's Bill History lists every bill from this run (paginated)",
                status == 200 and isinstance(page, dict) and "count" in page and found == ours,
                f"{len(found)}/{len(ours)} found")
        paid = [b["paid_at"] for b in rows if b["id"] in ours]
        r.check("H1b", "Newest bill first", paid == sorted(paid, reverse=True))

        if getattr(self, "card_bill", None):
            status, page = self.staff("GET", "/v1/bills/?date=today&payment_method=CARD&page_size=100")
            rows = page.get("results", []) if isinstance(page, dict) else []
            r.check("H3", "Card filter shows only card bills, including ours",
                    status == 200 and rows and all(b["payment_method"] == "CARD" for b in rows)
                    and any(b["id"] == self.card_bill["id"] for b in rows))
        status, _ = self.staff("GET", "/v1/bills/?payment_method=CAHS")
        r.check("H3b", "A misspelt method filter is refused, not ignored", status == 400, f"HTTP {status}")

        if getattr(self, "takeaway_name", None):
            status, page = self.staff("GET", f"/v1/bills/?date=today&search={urllib.parse.quote(self.takeaway_name)}")
            rows = page.get("results", []) if isinstance(page, dict) else []
            r.check("H4", "Search by customer name finds the takeaway bill", status == 200 and len(rows) >= 1)
        amount = str(int(money(self.bill_a["total_amount"])))
        status, page = self.staff("GET", f"/v1/bills/?date=today&search={amount}")
        rows = page.get("results", []) if isinstance(page, dict) else []
        r.check("H4b", f"Search by amount ({amount}) finds the table bill",
                status == 200 and any(b["id"] == self.bill_a["id"] for b in rows))

        status, bill = self.staff("GET", f"/v1/bills/{self.bill_a['id']}/")
        labels = [e["label"] for e in bill.get("timeline", [])] if isinstance(bill, dict) else []
        r.check("H6", "Bill detail has both rounds and a timeline ending in 'Bill paid'",
                status == 200 and bill.get("rounds_count") == 2 and labels and labels[-1] == "Bill paid"
                and "Bill requested by customer" in labels, f"{len(labels)} timeline events")

        status, sales = self.staff("GET", "/v1/cashier/collections/my-sales/")
        ok = status == 200 and money(sales.get("total_collected", "0")) == self.shift_total
        r.check("I1", "My Sales total equals this shift's bills", ok,
                f"{sales.get('total_collected') if isinstance(sales, dict) else sales} vs {self.shift_total}")

    def close_shift_one(self):
        r = self.run
        r.section("Shift 1 - reconcile and close SHORT by Rs 10")
        status, rec = self.staff("GET", f"/v1/cashier/shifts/{self.shift_id}/reconciliation/")
        buckets = ("cash", "card", "upi", "netbanking", "wallet")
        ok = status == 200 and all(b in rec for b in buckets)
        if ok:
            ok = sum(money(rec[b]) for b in buckets) == money(rec["total"]) == self.shift_total
        r.check("J1", "Reconciliation lists all five methods and they add up to the total", ok,
                ", ".join(f"{b} {rec.get(b)}" for b in buckets) + f" = {rec.get('total')}" if isinstance(rec, dict) else "")
        expected_cash = money(rec["cash"])
        status, detail = self.staff("GET", f"/v1/cashier/shifts/{self.shift_id}/bills/")
        r.check("J1b", "Shift detail shows expected cash = cash taken",
                status == 200 and money(detail["shift"]["expected_cash"]) == expected_cash)

        counted = expected_cash - 10
        status, body = self.staff("POST", f"/v1/cashier/shifts/{self.shift_id}/close/", {"counted_cash": str(counted)})
        r.check("J2", "Counting Rs 10 short is caught before closing",
                status == 409 and isinstance(body, dict) and body.get("discrepancy") == "-10.00", f"HTTP {status}, {body}")
        status, body = self.staff("POST", f"/v1/cashier/shifts/{self.shift_id}/close/",
                                  {"counted_cash": str(counted), "acknowledge_discrepancy": True})
        r.check("J3", "Proceeding without a reason is refused", status == 409, f"HTTP {status}")
        reason = f"{r.tag}: deliberately Rs 10 short (automated test)"
        status, closed = self.staff("POST", f"/v1/cashier/shifts/{self.shift_id}/close/",
                                    {"counted_cash": str(counted), "acknowledge_discrepancy": True, "discrepancy_reason": reason})
        ok = status == 200 and closed.get("status") == "CLOSED" and closed.get("discrepancy_amount") == "-10.00"
        r.check("J4", "Shift closes with the Rs 10 shortfall and the reason recorded", ok, f"HTTP {status}")
        r.shifts.append({"id": self.shift_id, "label": "Shift 1", "total": str(self.shift_total),
                         "expected_cash": str(expected_cash), "counted": str(counted),
                         "discrepancy": "-10.00", "reason": reason, "status": "DISCREPANCY"})
        status, _ = self.staff("POST", f"/v1/cashier/shifts/{self.shift_id}/close/", {"counted_cash": str(counted)})
        r.check("J6", "Closing the same shift again is refused", status == 409, f"HTTP {status}")

    def shift_two(self):
        r = self.run
        r.section("Shift 2 - one exact cash bill, closes MATCHED")
        if r.stock_out:
            r.skip("J5", "Matched shift", "out of stock")
            return
        self.shift_total = Decimal("0")
        status, shift = self.staff("POST", "/v1/cashier/shifts/open/")
        if not r.check("J5a", "A new shift opens after the first one closed",
                       status == 201 and shift.get("id") != self.shift_id, f"HTTP {status}"):
            return
        self.shift_id = shift["id"]
        t = self.next_table(3)
        sid = self.seat(t)
        if not self.order(sid, 1):
            r.skip("J5", "Matched shift", "out of stock")
        else:
            self.public("POST", f"/v1/tables/{t['id']}/bill-request/")
            status, pv = self.staff("GET", f"/v1/bills/session/{sid}/")
            total = money(pv["total_amount"])
            status, bill = self.pay(sid, "CASH", total)
            r.check("J5b", "Exact cash gives Rs 0.00 change",
                    status == 201 and money(bill.get("change_given")) == Decimal("0.00"), f"HTTP {status}")
            if status == 201:
                self.record_bill(bill, f"Table {t['number']} (shift 2)")
        status, rec = self.staff("GET", f"/v1/cashier/shifts/{self.shift_id}/reconciliation/")
        expected_cash = money(rec["cash"])
        status, closed = self.staff("POST", f"/v1/cashier/shifts/{self.shift_id}/close/", {"counted_cash": str(expected_cash)})
        r.check("J5", "Counting exactly the expected cash closes the shift with no discrepancy",
                status == 200 and closed.get("discrepancy_amount") == "0.00", f"HTTP {status}")
        r.shifts.append({"id": self.shift_id, "label": "Shift 2", "total": str(self.shift_total),
                         "expected_cash": str(expected_cash), "counted": str(expected_cash),
                         "discrepancy": "0.00", "reason": "", "status": "MATCHED"})
        h = self.home()
        r.check("J7a", "Cashier Home shows no open shift afterwards", h.get("shift") is None)


# ---------------------------------------------------------------------------
# reporting
# ---------------------------------------------------------------------------

def shereena_checklist(run, tester):
    lines = []
    total = sum(money(b["total"]) for b in run.bills)
    branch = tester.cashier["branch"].get("name") if tester.cashier else "the branch"
    lines.append(f"What to look for in the Admin app - branch {branch}, today ({run.tag}):")
    lines.append(f"- Bill History: {len(run.bills)} new bills from cashier {tester.cashier['name']}, Rs {total} in all:")
    for b in run.bills:
        lines.append(f"    {b['label']}: {b['method']} Rs {b['total']} (shift {b['shift']}, bill {b['id'][:8]})")
    lines.append(f"- Dashboards: today's revenue up by Rs {total} from these bills.")
    for s in run.shifts:
        extra = f", counted {s['counted']} vs expected {s['expected_cash']}, reason \"{s['reason']}\"" if s["reason"] else ""
        lines.append(f"- Cashier Collections: {s['label']} for {tester.cashier['name']} shows {s['status']}"
                     f" (collected Rs {s['total']}, discrepancy {s['discrepancy']}{extra}).")
    for f in run.feedback:
        lines.append(f"- Feedback: {f['rating']} stars, \"{f['comment']}\".")
    lines.append("- Notifications: a \"Payment received - Table N\" alert for each table bill.")
    lines.append(f"- Tables {', '.join(sorted(run.tables_used, key=lambda x: int(x) if x.isdigit() else 0))} are Available again.")
    lines.append(f"- Inventory: stock for {tester.dish['name']}'s recipe down by {run.units_ordered} portions.")
    return "\n".join(lines)


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--dry-run", action="store_true", help="read-only: log in, inspect tables/menu/shift, change nothing")
    parser.add_argument("--out", default="", help="write the JSON log and summary next to this path prefix")
    args = parser.parse_args()

    cfg = load_config()
    tag = "AutoTest " + datetime.now().strftime("%d-%b %H:%M")
    log = []
    run = Run(tag)
    tester = Tester(cfg, Api(cfg["base"], log), run, args.dry_run)
    print(f"{tag} against {cfg['base']} - restaurant '{cfg['restaurant']}', branch '{cfg['branch']}'"
          + ("  [DRY RUN]" if args.dry_run else ""))

    aborted = None
    try:
        tester.setup()
        if not args.dry_run:
            tester.shift_one()
            tester.takeaway()
            tester.history()
            tester.close_shift_one()
            tester.shift_two()
    except Abort as e:
        aborted = str(e)
        print(f"\n  ABORTED: {aborted}")
        if tester.shift_id and not args.dry_run:
            print(f"  NOTE: shift {tester.shift_id} may still be open - check Cashier Home.")

    counts = {k: sum(1 for x in run.results if x["result"] == k) for k in ("PASS", "FAIL", "SKIP")}
    print(f"\n{counts['PASS']} passed, {counts['FAIL']} failed, {counts['SKIP']} skipped"
          + (f" - aborted: {aborted}" if aborted else ""))
    checklist = shereena_checklist(run, tester) if run.bills else ""
    if checklist:
        print("\n" + checklist)

    if args.out:
        prefix = Path(args.out)
        prefix.parent.mkdir(parents=True, exist_ok=True)
        (prefix.with_suffix(".json")).write_text(json.dumps({
            "tag": tag, "base": cfg["base"], "branch": cfg["branch"], "aborted": aborted, "counts": counts,
            "results": run.results, "bills": run.bills, "shifts": run.shifts, "feedback": run.feedback,
            "requests": log,
        }, indent=2, default=str), encoding="utf-8")
        (prefix.with_suffix(".txt")).write_text(
            "\n".join(f"{x['result']:<5} {x['id']:<4} {x['title']}" + (f" -- {x['detail']}" if x["detail"] else "")
                      for x in run.results) + "\n\n" + checklist + "\n", encoding="utf-8")
        print(f"\nlog: {prefix.with_suffix('.json')}\nsummary: {prefix.with_suffix('.txt')}")
    return 1 if (aborted or counts["FAIL"]) else 0


if __name__ == "__main__":
    sys.exit(main())
