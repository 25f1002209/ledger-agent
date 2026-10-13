"""Loads the Acme order ledger and answers structured queries *exactly*.

Rules we learned from the API's own notes (and the sample rows):
  * Business dates use Asia/Kolkata (IST). Timestamps come mixed: "+05:30" and "Z".
  * An order id can appear more than once; the row with the latest updated_at wins.
  * Only status "paid" counts as revenue.
  * Amounts come in USD / EUR / INR; /rates gives "USD value of one unit".
All the maths happens here in plain Python (Decimal), never inside an LLM.
"""
import json
import logging
import os
import re
import threading
import time
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from decimal import ROUND_HALF_UP, Decimal
from difflib import get_close_matches
from zoneinfo import ZoneInfo

import requests

log = logging.getLogger("ledger")

IST = ZoneInfo("Asia/Kolkata")
CENT = Decimal("0.01")

# Which timestamp decides the "business date" of an order. Default: when it was placed.
DATE_FIELD = os.getenv("DATE_FIELD", "created_at")

METRICS = [
    "revenue",           # USD, paid orders
    "refund_amount",     # USD, refunded orders
    "refund_count",      # number of refunded orders
    "order_count",       # number of paid orders
    "units",             # sum of qty on paid orders
    "avg_order_value",   # USD, revenue / paid orders
    "unique_customers",  # distinct customers with paid orders
    "unique_products",   # distinct products with paid orders
    "refund_rate",       # refunded / (paid + refunded) * 100
]
DEFAULT_STATUS = {
    "revenue": {"paid"},
    "refund_amount": {"refunded"},
    "refund_count": {"refunded"},
    "order_count": {"paid"},
    "units": {"paid"},
    "avg_order_value": {"paid"},
    "unique_customers": {"paid"},
    "unique_products": {"paid"},
}
MONEY = {"revenue", "refund_amount", "avg_order_value"}


def parse_ts(s):
    s = str(s).strip()
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    dt = datetime.fromisoformat(s)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=IST)
    return dt


def norm(s):
    return re.sub(r"[^a-z0-9]", "", str(s).lower())


def money(d):
    return float(Decimal(d).quantize(CENT, rounding=ROUND_HALF_UP))


class Ledger:
    def __init__(self, root_url=None):
        self.root_url = root_url
        self.records = []
        self.rates = {}
        self.canon = {"region": {}, "product": {}, "customer": {}, "status": {}}
        self.loaded_at = 0.0
        self.refreshing = False
        self.lock = threading.Lock()

    # ------------------------------------------------------------------ loading
    def _get(self, url):
        last = None
        for attempt in range(3):
            try:
                r = requests.get(url, timeout=30)
                r.raise_for_status()
                return r
            except Exception as e:  # noqa: BLE001
                last = e
                time.sleep(0.5 * (attempt + 1))
        raise last

    def _fetch_pages(self, first_url):
        first = self._get(first_url).json()
        rows = list(first.get("orders", []))
        total = int(first.get("pages", 1))

        def get_page(n):
            url = re.sub(r"([&?])page=\d+", rf"\g<1>page={n}", first_url)
            return self._get(url).json().get("orders", [])

        with ThreadPoolExecutor(8) as ex:
            for chunk in ex.map(get_page, range(2, total + 1)):
                rows.extend(chunk)
        return rows

    def _fetch_export(self, url):
        rows = []
        for line in self._get(url).text.splitlines():
            line = line.strip()
            if line:
                rows.append(json.loads(line))
        return rows

    def load(self):
        root = self._get(self.root_url).json()
        links = root.get("links", {})
        rates = self._get(links["rates"]).json()["usd_per_unit"]
        rows = []
        if links.get("orders"):
            try:
                pages = self._fetch_pages(links["orders"])
                log.info("paged orders: %d rows", len(pages))
                rows += pages
            except Exception:  # noqa: BLE001
                log.exception("paged fetch failed")
        if links.get("export"):
            try:
                exp = self._fetch_export(links["export"])
                log.info("export: %d rows", len(exp))
                rows += exp
            except Exception:  # noqa: BLE001
                log.exception("export fetch failed")
        if not rows:
            raise RuntimeError("could not load any orders")
        # Both sources are merged; duplicates collapse because the latest updated_at wins.
        self._build(rows, rates)

    @classmethod
    def from_rows(cls, rows, rates):
        led = cls(None)
        led._build(rows, rates)
        return led

    def _build(self, rows, rates):
        latest = {}
        for i, r in enumerate(rows):
            oid = r.get("id")
            if oid is None:
                continue
            try:
                stamp = parse_ts(r["updated_at"]).timestamp()
            except Exception:  # noqa: BLE001
                stamp = 0.0
            cur = latest.get(oid)
            if cur is None or (stamp, i) > cur[0]:
                latest[oid] = ((stamp, i), r)

        dec_rates = {k.upper(): Decimal(str(v)) for k, v in rates.items()}
        records = []
        spell = {k: defaultdict(Counter) for k in ("region", "product", "customer", "status")}
        for _, r in latest.values():
            cur = str(r.get("currency", "USD")).strip().upper()
            rate = dec_rates.get(cur)
            if rate is None:
                log.warning("unknown currency %s on %s - skipped", cur, r.get("id"))
                continue
            amount = r.get("amount")
            if amount is None:
                amount = Decimal(str(r.get("qty", 0))) * Decimal(str(r.get("unit_price", 0)))
            usd = Decimal(str(amount)) * rate
            try:
                day = parse_ts(r[DATE_FIELD]).astimezone(IST).date()
            except Exception:  # noqa: BLE001
                continue
            rec = {
                "id": r["id"],
                "customer": str(r.get("customer", "")).strip(),
                "region": str(r.get("region", "")).strip(),
                "product": str(r.get("product", "")).strip(),
                "status": str(r.get("status", "")).strip().lower(),
                "qty": Decimal(str(r.get("qty", 0))),
                "usd": usd,
                "day": day,
            }
            records.append(rec)
            for kind in spell:
                spell[kind][norm(rec[kind])][rec[kind]] += 1
        canon = {k: {n: c.most_common(1)[0][0] for n, c in v.items()} for k, v in spell.items()}
        with self.lock:
            self.records, self.rates, self.canon = records, dec_rates, canon
            self.loaded_at = time.time()
        log.info("ledger ready: %d orders after de-duplication", len(records))

    # ---------------------------------------------------------------- helpers
    def known(self, kind):
        return sorted(self.canon[kind].values())

    def canonical(self, kind, value):
        """Map 'air fryers' / 'NORTH' to the API's own spelling. Unknown -> as given."""
        table = self.canon[kind]
        n = norm(value)
        if n in table:
            return table[n]
        if n.endswith("s") and n[:-1] in table:
            return table[n[:-1]]
        close = get_close_matches(n, list(table), n=1, cutoff=0.8)
        return table[close[0]] if close else str(value)

    def max_year(self):
        return max((r["day"].year for r in self.records), default=datetime.now(IST).year)

    # ------------------------------------------------------------------ query
    def _value(self, recs, metric, status):
        if metric == "refund_rate":
            paid = sum(1 for r in recs if r["status"] == "paid")
            ref = sum(1 for r in recs if r["status"] == "refunded")
            return Decimal(0) if paid + ref == 0 else Decimal(ref) * 100 / Decimal(paid + ref)
        wanted = DEFAULT_STATUS[metric] if status is None else status
        sel = [r for r in recs if wanted == "all" or r["status"] in wanted]
        if metric in ("revenue", "refund_amount"):
            return sum((r["usd"] for r in sel), Decimal(0))
        if metric in ("refund_count", "order_count"):
            return len(sel)
        if metric == "units":
            return int(sum(r["qty"] for r in sel))
        if metric == "avg_order_value":
            return Decimal(0) if not sel else sum((r["usd"] for r in sel), Decimal(0)) / len(sel)
        if metric == "unique_customers":
            return len({r["customer"] for r in sel})
        if metric == "unique_products":
            return len({norm(r["product"]) for r in sel})
        raise ValueError(f"unknown metric {metric}")

    @staticmethod
    def _fmt(metric, v):
        if metric in MONEY or metric == "refund_rate":
            return money(v)
        return int(v)

    def execute(self, spec):
        metric = spec["metric"]
        status = spec.get("status")
        if isinstance(status, list):
            status = {s.strip().lower() for s in status} or None
        elif isinstance(status, str):
            status = "all" if status.lower() == "all" else {status.strip().lower()}

        def keep(r):
            if spec.get("regions") and norm(r["region"]) not in {norm(x) for x in spec["regions"]}:
                return False
            if spec.get("products") and norm(r["product"]) not in {norm(x) for x in spec["products"]}:
                return False
            if spec.get("customers") and norm(r["customer"]) not in {norm(x) for x in spec["customers"]}:
                return False
            if spec.get("start") and r["day"] < spec["start"]:
                return False
            if spec.get("end") and r["day"] > spec["end"]:
                return False
            return True

        recs = [r for r in self.records if keep(r)]
        group_by = spec.get("group_by")
        if not group_by:
            return self._fmt(metric, self._value(recs, metric, status))

        groups = defaultdict(list)
        for r in recs:
            if group_by == "month":
                key = r["day"].strftime("%Y-%m")
            else:
                key = self.canon[group_by][norm(r[group_by])]
            groups[key].append(r)
        values = {k: self._value(v, metric, status) for k, v in groups.items()}

        rank = spec.get("rank")
        if rank:
            n = max(1, int(spec.get("n") or 1))
            ordered = sorted(values.items(), key=lambda kv: (-kv[1] if rank == "top" else kv[1], kv[0]))
            names = [k for k, _ in ordered[:n]]
            if not names:
                return None
            return names[0] if n == 1 else names
        return {k: self._fmt(metric, values[k]) for k in sorted(values)}
