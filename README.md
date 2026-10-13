# Ledger Agent

POST `{"question": "..."}` to `/` (or `/ask`) and get `{"answer": ...}` back.

## Files
- `main.py` – the web server (FastAPI).
- `ledger.py` – downloads all orders, cleans them, does the exact maths.
- `nlq.py` – turns an English question into a structured query (LLM first, rule-based fallback).
- `render.yaml`, `requirements.txt` – tell Render how to run it.
- `tests/test_core.py` – checks the tricky rules (`python tests/test_core.py` style or pytest).

## Data rules baked in (from the API's own notes)
1. Dates are in Asia/Kolkata (IST); timestamps arrive as both `+05:30` and `Z`.
2. If an order id repeats, the row with the latest `updated_at` is the real one.
3. Only `paid` orders are revenue. `refunded` orders are refunds. `void` is ignored.
4. EUR and INR are converted to USD using `/rates`; totals are rounded to the cent at the end.
5. An order's date = `created_at` (change with the env var `DATE_FIELD=updated_at` if the grader disagrees).

## Environment variables (set on Render, never commit them)
| Name | What |
|---|---|
| `ROOT_URL` | your long exam API URL (the one starting `https://exam.sanand.workers.dev/questionData?...`) |
| `LLM_API_KEY` | your AIPIPE_TOKEN (or any OpenAI-compatible key) |
| `LLM_BASE_URL` | optional, default `https://aipipe.org/openai/v1` |
| `LLM_MODEL` | optional, default `gpt-4.1-mini` |

## Deploy on Render
1. Put this folder on GitHub (new repo, upload the files; `.gitignore` already keeps junk out).
2. render.com -> New -> Web Service -> connect the repo.
3. Runtime Python 3. Build: `pip install -r requirements.txt`. Start: `uvicorn main:app --host 0.0.0.0 --port $PORT`.
4. Add the environment variables above (plus `PYTHON_VERSION` = `3.11.9`).
5. Deploy. Your endpoint is the `https://<name>.onrender.com` URL.
6. Test:
   `curl -X POST https://<name>.onrender.com/ -H "Content-Type: application/json" -d '{"question":"What was the total revenue in USD from the North region in March 2026?"}'`

Free Render services sleep after 15 minutes idle (wake-up ~1 minute). Open the URL just before you submit.
