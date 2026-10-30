# control_api.py  — run with: uvicorn control_api:app --host 0.0.0.0 --port 8001
import asyncio
import os
import signal
import sys
import uuid
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, Header, HTTPException
from pydantic import BaseModel
from dotenv import load_dotenv

load_dotenv(dotenv_path=Path(__file__).parent / ".env", override=False)
CONTROL_TOKEN = os.environ["BOT_HOST_TOKEN"]

app = FastAPI()
_running: dict[int, asyncio.Task] = {}


def _auth(authorization: Optional[str]):
    if authorization != f"Bearer {CONTROL_TOKEN}":
        raise HTTPException(status_code=401, detail="bad token")


class StartPayload(BaseModel):
    bot_id: int
    mozzart_user: str
    mozzart_pass: str
    is_demo: bool
    config: dict
    django_url: str
    callback_token: str


@app.post("/bots/{bot_id}/start")
async def start(bot_id: int, payload: StartPayload,
                authorization: Optional[str] = Header(None)):
    _auth(authorization)
    if bot_id in _running and not _running[bot_id].done():
        raise HTTPException(status_code=400, detail="already running")

    from engine.bot_engine  import run_bot_session
    from engine.bot_engine import post_json

    config = {
        **payload.config,
        "bot_id": payload.bot_id,
        "mozzart_user": payload.mozzart_user,
        "mozzart_pass": payload.mozzart_pass,
        "is_demo": payload.is_demo,
        "confirm_real": payload.config.get("confirm_real", False),
    }

    os.environ["BOT_CALLBACK_TOKEN"] = payload.callback_token

    async def _run():
        try:
            await run_bot_session(config, django_callback_url=payload.django_url)
        except Exception as e:
            # Belt-and-braces: runner should already have sent a failed status
            await post_json(f"{payload.django_url}/api/bots/{bot_id}/status/", {
                "bot_id": bot_id, "status": "failed",
                "stop_reason": f"control_api: {e}"[:200],
                "spins_completed": 0,
                "initial_balance": None, "current_balance": None,
                "profit": 0, "last_spin": None,
            })

    _running[bot_id] = asyncio.create_task(_run())
    return {"ok": True, "pid": id(_running[bot_id])}   # informational only


@app.post("/bots/{bot_id}/stop")
async def stop(bot_id: int, authorization: Optional[str] = Header(None)):
    _auth(authorization)
    task = _running.get(bot_id)
    if not task or task.done():
        raise HTTPException(status_code=400, detail="not running")
    task.cancel()
    try:
        await task
    except (asyncio.CancelledError, Exception):
        pass
    _running.pop(bot_id, None)
    return {"ok": True}


@app.get("/bots/{bot_id}/health")
async def health(bot_id: int):
    task = _running.get(bot_id)
    return {
        "running": bool(task and not task.done()),
        "bot_id": bot_id,
    }