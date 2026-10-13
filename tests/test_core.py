"""Run with:  python -m pytest -q      (from the project folder)
Rows are copied from the real API dump, plus a few made-up edge cases (marked EDGE)."""
import os
import sys

os.environ.pop("LLM_API_KEY", None)
os.environ.pop("AIPIPE_TOKEN", None)
os.environ.pop("OPENAI_API_KEY", None)
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

import nlq  # noqa: E402
from ledger import Ledger  # noqa: E402

RATES = {"USD": 1, "EUR": 1.08, "INR": 0.01169}


def row(id, customer, region, product, qty, price, amount, cur, status, created, updated):
    return dict(id=id, customer=customer, region=region, product=product, qty=qty, unit_price=price,
                amount=amount, currency=cur, status=status, created_at=created, updated_at=updated)


ROWS = [
    row("ORD-13507", "C1166", "East", "Kettle", 5, 125.58, 627.9, "USD", "paid", "2026-05-16T07:55:42.124+05:30", "2026-05-17T06:25:36.320+05:30"),
    row("ORD-13314", "C1146", "East", "Rice Cooker", 3, 77.41, 232.23, "EUR", "paid", "2026-05-17T16:49:42.096Z", "2026-05-20T05:04:02.077Z"),
    row("ORD-10731", "C1189", "West", "Grinder", 5, 184.55, 922.75, "USD", "paid", "2026-03-24T08:25:30.107+05:30", "2026-03-27T12:58:40.520+05:30"),
    row("ORD-13117", "C1013", "West", "Toaster", 1, 173.45, 173.45, "EUR", "paid", "2026-04-04T07:46:25.379+05:30", "2026-04-06T18:06:11.207+05:30"),
    row("ORD-11697", "C1190", "Central", "Air Fryer", 3, 38.49, 115.47, "USD", "refunded", "2026-04-03T08:54:48.973Z", "2026-04-07T06:19:22.965Z"),
    row("ORD-12082", "C1169", "West", "Blender", 1, 183.07, 183.07, "USD", "refunded", "2026-05-21T18:28:17.619+05:30", "2026-05-23T05:22:00.551+05:30"),
    row("ORD-10539", "C1242", "Central", "Juicer", 2, 13684.35, 27368.7, "INR", "void", "2026-05-25T06:18:13.592+05:30", "2026-05-27T02:42:53.606+05:30"),
    row("ORD-12726", "C1180", "South", "Air Fryer", 3, 3292.56, 9877.68, "INR", "paid", "2026-06-30T06:10:48.203+05:30", "2026-06-30T00:40:48.203Z"),
    # EDGE: 20:00 UTC on 28 Feb is 01:30 IST on 1 March -> belongs to MARCH
    row("ORD-9001", "C1001", "North", "Mixer", 1, 100, 100, "USD", "paid", "2026-02-28T20:00:00Z", "2026-02-28T20:00:00Z"),
    # EDGE: same id as ORD-10731 but later updated_at and now refunded -> this row is current
    row("ORD-10731", "C1189", "West", "Grinder", 5, 184.55, 922.75, "USD", "refunded", "2026-03-24T08:25:30.107+05:30", "2026-04-01T09:00:00+05:30"),
]
L = Ledger.from_rows(ROWS, RATES)


def ask(q):
    return nlq.answer(q, L)


def test_dedupe_latest_wins():
    assert len(L.records) == 9  # 10 rows, one repeated id


def test_revenue_converts_to_usd_and_rounds_to_cent():
    assert ask("What was the total revenue in USD from the East region in May 2026?") == 878.71  # 627.9 + 232.23*1.08
    assert ask("revenue for the South region in June 2026") == 115.47  # INR 9877.68 * 0.01169


def test_ist_month_boundary():
    assert ask("What was the total revenue in USD from the North region in March 2026?") == 100.0
    assert ask("What was the total revenue in USD from the North region in February 2026?") == 0.0


def test_only_paid_counts_and_latest_row_used():
    assert ask("Total revenue from the West region in March 2026") == 0.0  # order was refunded later
    assert ask("How much was refunded in the West region in March 2026?") == 922.75


def test_counts_and_ranking():
    assert ask("How many orders were refunded in April 2026?") == 1
    assert ask("Which region had the highest revenue?") == "East"
    assert ask("Which product had the highest revenue?") == "Kettle"
    assert ask("how many customers placed orders in May 2026?") == 2
    assert ask("What is the total revenue for air fryers?") == 115.47
