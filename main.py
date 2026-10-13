"""The web service. POST {"question": "..."} -> {"answer": ...}"""
import logging
import os
import threading
import time
from contextlib import asynccontextmanager

from fastapi import Body, FastAPI
from fastapi.middleware.cors import CORSMiddleware

import nlq
from ledger import Ledger

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("main")

ROOT_URL = os.environ.get("ROOT_URL", "")
REFRESH_SECONDS = int(os.getenv("REFRESH_SECONDS", "300"))

ledger = Ledger(ROOT_URL)


def reload_data():
    if ledger.refreshing:
        return
    ledger.refreshing = True
    try:
        ledger.load()
    except Exception:  # noqa: BLE001
        log.exception("data load failed")
    finally:
        ledger.refreshing = False


@asynccontextmanager
async def lifespan(_app):
    reload_data()  # load everything before we start answering
    yield


app = FastAPI(lifespan=lifespan)
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])


@app.get("/")
def health():
    return {"status": "ok", "orders_loaded": len(ledger.records), "llm_configured": bool(nlq.LLM_KEY)}


@app.post("/")
@app.post("/ask")
def ask(payload: dict = Body(...)):
    question = str(payload.get("question", "")).strip()
    if not ledger.records:  # first load failed earlier - try again now
        reload_data()
    elif time.time() - ledger.loaded_at > REFRESH_SECONDS and not ledger.refreshing:
        threading.Thread(target=reload_data, daemon=True).start()  # refresh without making anyone wait
    try:
        return {"answer": nlq.answer(question, ledger)}
    except Exception as e:  # noqa: BLE001
        log.exception("could not answer %r", question)
        return {"answer": None, "error": str(e)}
