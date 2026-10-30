from fastapi import FastAPI, BackgroundTasks
from engine.schemas import BotConfig
from engine.bot_engine import run_bot_session
import os

app = FastAPI(title="Roulette Engine")

DJANGO_URL = os.getenv("DJANGO_CALLBACK_URL", "http://localhost:8000")


@app.get("/health")
async def health():
    return {"status": "ok"}


@app.post("/run-bot")
async def run_bot(config: BotConfig, background_tasks: BackgroundTasks):
    """Spawn a bot. Returns immediately; bot runs in background."""
    background_tasks.add_task(run_bot_session, config, DJANGO_URL)
    return {"status": "queued", "bot_id": config.bot_id}