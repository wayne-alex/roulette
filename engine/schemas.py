from pydantic import BaseModel, Field
from typing import Optional
from enum import Enum


class StrategyEnum(str, Enum):
    flat = "flat"
    martingale = "martingale"


class BotConfig(BaseModel):
    bot_id: int
    name: str
    mozzart_user: str
    mozzart_pass: str
    is_demo: bool = True
    strategy: StrategyEnum = StrategyEnum.flat
    target: str                      # "red", "black", "17", "1st12"
    base_chip: float = 1.0
    max_spins: int = 10


class SpinResult(BaseModel):
    spin_number: int
    winning_number: str
    payout: float
    balance_after: float
    bet_amount: float
    strategy_state: dict             # e.g. {"current_chip": 2.0, "consecutive_losses": 1}


class BotStatus(BaseModel):
    bot_id: int
    status: str                      # "running", "completed", "failed"
    spins_completed: int
    initial_balance: float
    current_balance: float
    profit: float
    last_spin: Optional[SpinResult] = None
    error: Optional[str] = None