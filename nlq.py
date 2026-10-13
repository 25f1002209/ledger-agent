"""Turns an English question into a structured query (a 'spec') for ledger.py.

Two parsers:
  1. An LLM (any OpenAI-compatible API) - understands lots of different wordings.
  2. A rule-based fallback - used if there is no key, the LLM is slow (>8 s) or it returns junk.
The LLM only *translates* the question. The numbers are always computed by ledger.py.
"""
import calendar
import json
import logging
import os
import re
from datetime import date, datetime, timedelta

import requests

from ledger import IST, METRICS, norm

log = logging.getLogger("nlq")

LLM_KEY = os.getenv("LLM_API_KEY") or os.getenv("AIPIPE_TOKEN") or os.getenv("OPENAI_API_KEY") or ""
LLM_BASE = os.getenv("LLM_BASE_URL", "https://aipipe.org/openai/v1").rstrip("/")
LLM_MODEL = os.getenv("LLM_MODEL", "gpt-4.1-mini")
LLM_TIMEOUT = float(os.getenv("LLM_TIMEOUT", "8"))

MONTHS = {}
for i in range(1, 13):
    MONTHS[calendar.month_name[i].lower()] = i
    MONTHS[calendar.month_abbr[i].lower()] = i
MONTHS["sept"] = 9


def today():
    return datetime.now(IST).date()


def month_range(year, month):
    start = date(year, month, 1)
    end = date(year + (month == 12), (month % 12) + 1, 1) - timedelta(days=1)
    return start, end


# --------------------------------------------------------------------------- LLM
SYSTEM = """You translate a question about a company's order ledger into a JSON query.
Reply with ONE JSON object only, with exactly these keys:

"metric": one of
   "revenue"          total money from paid orders (USD)
   "refund_amount"    total money of refunded orders (USD)
   "refund_count"     how many orders were refunded
   "order_count"      how many orders (paid, unless a status is given)
   "units"            total quantity (items) sold
   "avg_order_value"  average USD value per paid order
   "unique_customers" how many different customers
   "unique_products"  how many different products
   "refund_rate"      refunded orders as a percentage of paid+refunded orders
"status": null normally. Use a list like ["void"] only when the question is about a specific
   order status other than the metric's default, or "all" when it explicitly counts every order.
"regions", "products", "customers": lists of values to filter on ([] = no filter).
   Use values exactly as listed in KNOWN VALUES below.
"start", "end": inclusive date range as YYYY-MM-DD (India time), or null. Expand months, quarters,
   years, "first half" etc. into dates. If a month is given without a year, use the latest year in the data.
"group_by": null, or "region", "product", "customer", "month" - use when the question asks
   "which / what / top / best / worst / each / per / by".
"rank": null, "top" (highest/most/best) or "bottom" (lowest/least/worst). Only with group_by.
"n": how many items to return when ranking (default 1).

Examples:
 "Total revenue in USD from the North region in March 2026?" ->
   {"metric":"revenue","status":null,"regions":["North"],"products":[],"customers":[],"start":"2026-03-01","end":"2026-03-31","group_by":null,"rank":null,"n":1}
 "Which product had the most refunds in Q2 2026?" ->
   {"metric":"refund_count","status":null,"regions":[],"products":[],"customers":[],"start":"2026-04-01","end":"2026-06-30","group_by":"product","rank":"top","n":1}
"""


def llm_parse(question, ledger):
    if not LLM_KEY:
        return None
    known = {
        "regions": ledger.known("region"),
        "products": ledger.known("product"),
        "statuses": ledger.known("status"),
        "latest_year_in_data": ledger.max_year(),
        "today": today().isoformat(),
    }
    user = f"KNOWN VALUES: {json.dumps(known)}\n\nQUESTION: {question}"
    resp = requests.post(
        f"{LLM_BASE}/chat/completions",
        headers={"Authorization": f"Bearer {LLM_KEY}"},
        json={
            "model": LLM_MODEL,
            "temperature": 0,
            "response_format": {"type": "json_object"},
            "messages": [{"role": "system", "content": SYSTEM}, {"role": "user", "content": user}],
        },
        timeout=LLM_TIMEOUT,
    )
    resp.raise_for_status()
    text = resp.json()["choices"][0]["message"]["content"].strip()
    text = re.sub(r"^```(?:json)?|```$", "", text, flags=re.M).strip()
    return json.loads(text)


# ----------------------------------------------------------------------- rules
def rule_parse(question, ledger):
    q = question.lower()
    spec = {"metric": "revenue", "status": None, "regions": [], "products": [], "customers": [],
            "start": None, "end": None, "group_by": None, "rank": None, "n": 1}

    # --- metric
    many = bool(re.search(r"how many|number of|count|total number", q))
    if "refund rate" in q or "refund percentage" in q or "percent" in q and "refund" in q:
        spec["metric"] = "refund_rate"
    elif "refund" in q:
        spec["metric"] = "refund_count" if many else "refund_amount"
    elif re.search(r"average|avg|mean", q):
        spec["metric"] = "avg_order_value"
    elif many and "customer" in q:
        spec["metric"] = "unique_customers"
    elif many and re.search(r"product", q) and "per" not in q:
        spec["metric"] = "unique_products"
    elif re.search(r"units|quantity|items", q) or (many and "sold" in q):
        spec["metric"] = "units"
    elif many and "order" in q:
        spec["metric"] = "order_count"

    # --- filters from known values (longest names first so 'Rice Cooker' beats 'Cooker')
    for kind, key in (("region", "regions"), ("product", "products")):
        for name in sorted(ledger.known(kind), key=len, reverse=True):
            if re.search(rf"\b{re.escape(name.lower())}s?\b", q):
                spec[key].append(name)
                q = q.replace(name.lower(), " ")
    spec["customers"] = [c.upper() for c in re.findall(r"\bc\d{3,}\b", q)]

    # --- dates
    year = (re.search(r"\b(20\d{2})\b", q) or [None, None])[1]
    year = int(year) if year else None
    m = re.search(r"\bq([1-4])\b", q)
    if m:
        y = year or ledger.max_year()
        qn = int(m.group(1))
        spec["start"] = date(y, 3 * qn - 2, 1)
        spec["end"] = month_range(y, 3 * qn)[1]
    else:
        mm = re.search(r"\b(" + "|".join(sorted(MONTHS, key=len, reverse=True)) + r")\b", q)
        if mm and (year or len(mm.group(1)) > 3 or mm.group(1) != "may"):
            y = year or ledger.max_year()
            spec["start"], spec["end"] = month_range(y, MONTHS[mm.group(1)])
        elif year:
            spec["start"], spec["end"] = date(year, 1, 1), date(year, 12, 31)

    # --- grouping / ranking
    for kind in ("region", "product", "customer", "month"):
        if re.search(rf"\b(which|what|top|best|worst|each|per|by)\b[^?]*\b{kind}s?\b", q):
            spec["group_by"] = kind
            break
    if spec["group_by"]:
        if re.search(r"lowest|least|worst|fewest|smallest|bottom", q):
            spec["rank"] = "bottom"
        elif re.search(r"highest|most|best|top|largest|biggest|greatest", q) or re.search(r"\bwhich\b", q):
            spec["rank"] = "top"
        n = re.search(r"\btop\s+(\d+)\b", q)
        if n:
            spec["n"] = int(n.group(1))
        if spec["group_by"] in ("region", "product", "customer"):
            # a filter on the same dimension means they asked about one item, not a ranking
            filt = {"region": "regions", "product": "products", "customer": "customers"}[spec["group_by"]]
            if spec[filt] and not spec["rank"]:
                spec["group_by"] = None
    return spec


# ------------------------------------------------------------------- clean + run
def _to_date(v):
    if not v:
        return None
    if isinstance(v, date):
        return v
    return date.fromisoformat(str(v)[:10])


def clean(spec, ledger):
    if spec.get("metric") not in METRICS:
        raise ValueError(f"bad metric {spec.get('metric')}")
    out = {"metric": spec["metric"], "status": spec.get("status") or None}
    for kind, key in (("region", "regions"), ("product", "products"), ("customer", "customers")):
        vals = spec.get(key) or []
        if isinstance(vals, str):
            vals = [vals]
        out[key] = [ledger.canonical(kind, v) for v in vals]
    out["start"], out["end"] = _to_date(spec.get("start")), _to_date(spec.get("end"))
    gb = spec.get("group_by")
    out["group_by"] = gb if gb in ("region", "product", "customer", "month") else None
    rank = spec.get("rank")
    out["rank"] = rank if rank in ("top", "bottom") and out["group_by"] else None
    try:
        out["n"] = max(1, int(spec.get("n") or 1))
    except (TypeError, ValueError):
        out["n"] = 1
    return out


def answer(question, ledger):
    spec = None
    try:
        raw = llm_parse(question, ledger)
        if raw:
            spec = clean(raw, ledger)
            log.info("LLM spec: %s", spec)
    except Exception as e:  # noqa: BLE001
        log.warning("LLM parse failed (%s) - using rules", e)
    if spec is None:
        spec = clean(rule_parse(question, ledger), ledger)
        log.info("rule spec: %s", spec)
    return ledger.execute(spec)
