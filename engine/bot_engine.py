from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from pathlib import Path
from typing import Optional
from urllib.parse import parse_qs

from dotenv import load_dotenv
from playwright.async_api import Browser, Frame, Page, async_playwright

ENV_PATH = Path(__file__).parent.parent / ".env"
load_dotenv(dotenv_path=ENV_PATH, override=True)

MAP_FILE = Path("roulette_map.json")
CHIP_STEPS = (1, 5, 10, 25, 50, 100, 250, 500, 1000)
DOZENS = ("1st12", "2nd12", "3rd12")

BROWSER_ARGS = [
    "--mute-audio",
    "--disable-dev-shm-usage",
    "--disable-background-timer-throttling",
    "--disable-backgrounding-occluded-windows",
    "--disable-renderer-backgrounding",
]

DEFAULTS = {
    "is_demo": True,
    "confirm_real": False,
    "headless": True,

    # ---- The Murunga Strategy ----
    "chip": 10,              # chip placed on EACH of the two dozens
    "streak_length": 4,      # same dozen this many times in a row -> bet next spin
    "multiplier": 2,         # stake multiplier when the same dozen hits again (2 = double)
    "max_doublings": 5,      # chain is abandoned after more doublings than this
    "monitor_chip": 1,       # even+odd bet used only to be able to spin (0 = no bet)

    # ---- Risk limits ----
    "stop_loss": 500.0,      # None = off
    "take_profit": None,     # None = off
    "max_stake_per_spin": 1000.0,
    "min_balance": 0.0,
    "settlement_tolerance": 0.5,

    # ---- Speed / resilience ----
    "max_spins": 100,
    "round_time": 19.0,      # seconds from the RESULT until bets can be placed again
    "result_wait": 6.0,      # no result after this many seconds -> click spin again
    "spin_attempts": 3,
    "pre_spin_drain": 0.5,
    "click_delay": 0.15,
    "chip_delay": 0.10,
    "recovery_wait": 8.0,
    "max_consecutive_errors": 5,
    "spin_retry_delay": 3.0,
    "safe_clicks": False,    # True = use canvas.click (slower, more careful)
}

log = logging.getLogger("bot")


class BotLog(logging.LoggerAdapter):
    def process(self, msg, kwargs):
        return f"[bot {self.extra['bot_id']}] {msg}", kwargs


class StopBot(Exception):
    pass


class SettlementError(StopBot):
    """Raised only when the bot cannot safely know its state."""


# =====================================================================
# 1. HELPERS
# =====================================================================

_MAP_CACHE: Optional[dict] = None


def load_map() -> dict:
    global _MAP_CACHE
    if _MAP_CACHE is None:
        with open(MAP_FILE) as f:
            _MAP_CACHE = json.load(f)
    return _MAP_CACHE


def dozen_of(num: str) -> str:
    try:
        n = int(num)
    except (ValueError, TypeError):
        return "unknown"
    if n == 0:
        return "0"
    if 1 <= n <= 36:
        return DOZENS[(n - 1) // 12]
    return "unknown"


def expected_pnl(bets: dict, number: str) -> Optional[float]:
    """Net cash change for the spin (dozens pay 3x, even/odd pay 2x)."""
    try:
        n = int(number)
    except (ValueError, TypeError):
        return None
    returned = 0.0
    dz = dozen_of(number)
    for target, stake in bets.items():
        if target in DOZENS:
            if dz == target:
                returned += stake * 3
        elif target == "even":
            if n != 0 and n % 2 == 0:
                returned += stake * 2
        elif target == "odd":
            if n % 2 == 1:
                returned += stake * 2
    return returned - sum(bets.values())


def _float(parsed: dict, key: str) -> Optional[float]:
    try:
        return float(parsed[key][0].replace(",", ""))
    except (KeyError, IndexError, ValueError):
        return None


# =====================================================================
# 2. THE MURUNGA STRATEGY
# =====================================================================

class MurungaStrategy:
    """
    Watch the dozens. When the SAME dozen comes `streak_length` times in a row,
    bet `chip` on each of the OTHER two dozens on the next spin.
      - other dozens hit      -> WIN, back to watching, chip back to base
      - same dozen hits again -> LOSS, double the chip and rebet the same two dozens
      - 0 hits                -> LOSS, but NO doubling: rebet the same chip
    """

    def __init__(self, cfg: dict):
        self.base_chip = int(cfg["chip"])
        self.streak_length = int(cfg["streak_length"])
        self.multiplier = int(cfg["multiplier"])
        self.max_doublings = int(cfg["max_doublings"])
        self.monitor_chip = int(cfg["monitor_chip"])
        self.streak_dz: Optional[str] = None
        self.streak = 0
        self.hot_dz: Optional[str] = None      # the dozen that is streaking
        self.pair: Optional[tuple] = None      # the two dozens we bet on
        self.chip = self.base_chip
        self.losses = 0                        # number of doublings so far
        self.zeros = 0                         # zeros hit during this chain (info only)

    @property
    def in_chain(self) -> bool:
        return self.pair is not None

    def _clear_chain(self):
        self.pair = None
        self.hot_dz = None
        self.chip = self.base_chip
        self.losses = 0
        self.zeros = 0

    def abandon(self):
        """Give up the chain and start counting again from zero."""
        self._clear_chain()
        self.streak_dz, self.streak = None, 0

    def next_bets(self) -> dict:
        if self.pair:
            return {d: self.chip for d in self.pair}
        if self.monitor_chip > 0:
            return {"even": self.monitor_chip, "odd": self.monitor_chip}
        return {}

    def _track(self, dz: str):
        if dz in ("0", "unknown"):
            self.streak_dz, self.streak = None, 0
        elif dz == self.streak_dz:
            self.streak += 1
        else:
            self.streak_dz, self.streak = dz, 1

    def on_result(self, number: str):
        dz = dozen_of(number)

        if self.pair:  # we just played the two-dozen bet
            if dz in self.pair:                      # WIN
                self._clear_chain()
                self.streak_dz, self.streak = None, 0
                self._track(dz)
                return

            if dz == self.hot_dz:                    # same dozen again: double + rebet
                self.losses += 1
                if self.losses > self.max_doublings:
                    log.warning("Murunga: %d doublings reached, abandoning chain", self.losses)
                    self.abandon()
                    self._track(dz)
                    return
                self.chip *= self.multiplier
                return

            # 0 (or unknown): lose the stake, rebet the SAME chip, no doubling
            self.zeros += 1
            log.info("Murunga: 0 hit, rebetting same chip %s (zeros in chain: %d)",
                     self.chip, self.zeros)
            return

        self._track(dz)
        if self.streak >= self.streak_length:
            self.hot_dz = self.streak_dz
            self.pair = tuple(d for d in DOZENS if d != self.hot_dz)
            self.chip = self.base_chip
            self.losses = 0
            self.zeros = 0

    def state(self) -> dict:
        return {
            "mode": "hedge" if self.pair else "monitor",
            "streak_dz": self.streak_dz,
            "streak": self.streak,
            "hot_dz": self.hot_dz,
            "pair": list(self.pair) if self.pair else None,
            "chip": self.chip,
            "doublings": self.losses,
            "zeros": self.zeros,
        }


# =====================================================================
# 3. SPIN LISTENER (event based)
# =====================================================================

class SpinListener:
    def __init__(self, page: Page):
        self.page = page
        self.result: Optional[dict] = None
        self.settled: Optional[float] = None
        self.last_winning_number: Optional[str] = None
        self.last_filtered: Optional[str] = None
        self._result_evt = asyncio.Event()
        self._settled_evt = asyncio.Event()
        self._tasks: set = set()
        self._first_logged = False

    def start(self):
        self.page.on("response", self._on_response)

    def _on_response(self, resp):
        url = resp.url
        if "gameService" not in url and "reloadBalance" not in url:
            return
        task = asyncio.create_task(self._handle(resp))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _handle(self, resp):
        try:
            text = await resp.text()
        except Exception:
            return

        if "reloadBalance" in resp.url:
            bal = _float(parse_qs(text), "balance_cash")
            if bal is not None:
                self.settled = bal
                self._settled_evt.set()
            return

        if "nom=" not in text:
            return
        if "oldw=" in text or "oldbalance=" in text or "oldstime=" in text:
            self.last_filtered = text[:300]
            return

        if not self._first_logged:
            log.info("First spin response: %s", text[:300])
            self._first_logged = True

        parsed = parse_qs(text)
        nom = parsed.get("nom", ["?"])[0]
        self.last_winning_number = nom
        self.result = {"winning_number": nom, "spin_balance": _float(parsed, "balance_cash")}
        self._result_evt.set()

    def clear(self):
        self.result = None
        self.settled = None
        self.last_winning_number = None
        self.last_filtered = None
        self._result_evt.clear()
        self._settled_evt.clear()

    async def wait_result(self, timeout: float) -> Optional[dict]:
        try:
            await asyncio.wait_for(self._result_evt.wait(), timeout)
            return self.result
        except asyncio.TimeoutError:
            return None

    async def wait_settled(self, timeout: float) -> Optional[float]:
        try:
            await asyncio.wait_for(self._settled_evt.wait(), timeout)
            return self.settled
        except asyncio.TimeoutError:
            return None


# =====================================================================
# 4. CLICKING
# =====================================================================

class Clicker:
    def __init__(self, frame: Frame, page: Page, cfg: dict, is_demo: bool):
        self.frame = frame
        self.page = page
        self.cfg = cfg
        self.is_demo = is_demo
        self.canvas = frame.locator("canvas").first
        self.map = load_map()
        self.box: Optional[dict] = None
        self.chip_idx: Optional[int] = None
        self.click_delay = float(cfg["click_delay"])
        self.chip_delay = float(cfg["chip_delay"])
        self.safe = bool(cfg["safe_clicks"])
        self.listener = SpinListener(page)

    async def prepare(self):
        required = ["spin", "chip_minus", "chip_plus", *DOZENS]
        if int(self.cfg["monitor_chip"]) > 0:
            required += ["even", "odd"]
        missing = [k for k in required if k not in self.map]
        if missing:
            raise StopBot(f"roulette_map.json is missing: {missing}")
        await self.canvas.scroll_into_view_if_needed()
        await self.refresh_box()
        self.listener.start()

    async def refresh_box(self):
        box = await self.canvas.bounding_box()
        if not box:
            raise StopBot("Game canvas has no bounding box")
        self.box = box

    async def click(self, key: str, delay: Optional[float] = None):
        c = self.map[key]
        b = self.box
        x, y = c["x_pct"] * b["width"], c["y_pct"] * b["height"]
        if self.safe:
            await self.canvas.click(position={"x": x, "y": y})
        else:
            await self.page.mouse.click(b["x"] + x, b["y"] + y)
        await asyncio.sleep(self.click_delay if delay is None else delay)

    async def set_chip(self, idx: int):
        if self.chip_idx is None:  # unknown state: go all the way down once
            for _ in range(len(CHIP_STEPS) + 1):
                await self.click("chip_minus", self.chip_delay)
            self.chip_idx = 0
        delta = idx - self.chip_idx
        key = "chip_plus" if delta > 0 else "chip_minus"
        for _ in range(abs(delta)):
            await self.click(key, self.chip_delay)
        self.chip_idx = idx

    async def place_bets(self, bets: dict) -> bool:
        plan: dict = {}
        for target, amount in bets.items():
            if target not in self.map:
                log.error("Target %s not in map", target)
                return False
            remaining = int(round(amount))
            for chip in reversed(CHIP_STEPS):
                n, remaining = divmod(remaining, chip)
                if n:
                    plan.setdefault(chip, []).append((target, n))
        if not plan:
            return True

        try:
            await self.refresh_box()
            chips = sorted(plan)
            cur = self.chip_idx or 0
            if abs(cur - CHIP_STEPS.index(chips[0])) > abs(cur - CHIP_STEPS.index(chips[-1])):
                chips.reverse()
            for chip in chips:
                await self.set_chip(CHIP_STEPS.index(chip))
                for target, n in plan[chip]:
                    for _ in range(n):
                        await self.click(target)
            return True
        except Exception:
            self.chip_idx = None  # force a re-sync next time
            raise

    async def clear_table(self):
        """Optional: add a 'clear' (or 'undo') spot to roulette_map.json to use this."""
        if "clear" in self.map:
            await self.click("clear", 0.3)

    async def click_spin(self):
        await self.refresh_box()
        await self.click("spin", 0.3)

    async def read_winning_number_from_page(self) -> Optional[str]:
        try:
            for sel in (
                ".last-result .number",
                ".history-list .number:first-child",
                "[class*='lastResult'] [class*='number']",
                "[class*='winning-number']",
                "[class*='lastNumber']",
            ):
                loc = self.frame.locator(sel).first
                if await loc.count():
                    txt = (await loc.text_content() or "").strip()
                    if txt.isdigit() and 0 <= int(txt) <= 36:
                        return txt
        except Exception as e:
            log.debug("Winning-number scrape failed: %s", e)
        return None

    async def fetch_balance(self, username: str) -> Optional[float]:
        payload = {"username": username, "dontFetchOmegaBalance": False,
                   "shouldReloadOmegaBonuses": True}
        try:
            r = await self.page.request.post(
                "https://www.mozzartbet.co.ke/myBalances",
                headers={"accept": "application/json, text/plain, */*",
                         "content-type": "application/json;charset=UTF-8",
                         "origin": "https://www.mozzartbet.co.ke",
                         "referer": "https://www.mozzartbet.co.ke/en"},
                data=json.dumps(payload),
            )
            if r.ok:
                value = (await r.json()).get("casinoTotal")
                return float(value) if value is not None else None
        except Exception as e:
            log.warning("Balance API failed: %s", e)
        return None


# =====================================================================
# 5. SESSION + LAUNCHER
# =====================================================================

class MozzartSession:
    def __init__(self, browser: Browser, base_url: str, username: str, password: str,
                 session_dir: Path, idx: int = 0):
        self.browser = browser
        self.base_url = base_url.rstrip("/")
        self.username = username
        self.password = password
        session_dir.mkdir(parents=True, exist_ok=True)
        self.session_file = session_dir / f"auth_{idx}.json"
        self.context = None
        self.page: Optional[Page] = None

    async def start(self):
        storage = str(self.session_file) if self.session_file.exists() else None
        self.context = await self.browser.new_context(
            storage_state=storage,
            user_agent=("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                        "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"),
            viewport={"width": 1366, "height": 900},
        )
        self.page = await self.context.new_page()
        return self

    async def close(self):
        if self.context:
            await self.context.close()

    async def is_logged_in(self) -> bool:
        await self.page.goto(self.base_url + "/en", wait_until="domcontentloaded")
        try:
            await self.page.wait_for_selector(".user-nav-group.logged-in", timeout=5000)
            return True
        except Exception:
            return False

    async def login(self):
        if not self.username or not self.password:
            raise StopBot("Missing MOZZART_USER / MOZZART_PASS")
        await self.page.goto(self.base_url + "/en", wait_until="domcontentloaded")
        await self.page.wait_for_selector("input[type='password']", state="visible", timeout=20000)
        await self.page.locator(
            "input[placeholder*='Mobile'], input[placeholder*='mobile'], input[type='text']"
        ).first.fill(str(self.username))
        await self.page.locator("input[type='password']").first.fill(str(self.password))
        await self.page.locator("button:has-text('OK'), .login-btn, button[type='submit']").first.click()
        await self.page.wait_for_selector(".user-nav-group.logged-in", timeout=20000)
        await self.context.storage_state(path=str(self.session_file))

    async def ensure_login(self):
        if not await self.is_logged_in():
            await self.login()


async def find_game_canvas(page: Page, timeout: float = 90.0) -> Frame:
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        for frame in page.frames:
            try:
                canvas = frame.locator("canvas").first
                if await canvas.count() and await canvas.is_visible():
                    box = await canvas.bounding_box()
                    if box and box["width"] > 300 and box["height"] > 200:
                        return frame
            except Exception:
                pass
        await asyncio.sleep(1.0)
    raise StopBot("Game canvas did not appear in time")


_JS_CLICK = """(mode) => {
    const cards = Array.from(document.querySelectorAll('.single-game, .single-game-mobile'));
    const roulette = cards.find(c => c.textContent.includes('Roulette Pragmatic'));
    if (!roulette) return 'no-card';
    for (const el of roulette.querySelectorAll('*')) {
        if (el.textContent.trim().toUpperCase() === mode) { el.click(); return 'clicked'; }
    }
    return 'no-button';
}"""


async def open_roulette(session: MozzartSession, mode: str, blog) -> Frame:
    page = session.page
    blog.info("Opening casino lobby...")
    await page.goto(f"{session.base_url}/en#/casino/", wait_until="domcontentloaded")
    try:
        await page.wait_for_selector(".single-game, .single-game-mobile", timeout=30000)
    except Exception:
        raise StopBot("Casino lobby never rendered any games.")

    await page.wait_for_timeout(3000)
    card = page.locator(".single-game, .single-game-mobile").filter(
        has_text="Roulette Pragmatic").first
    await card.wait_for(state="visible", timeout=30000)
    await card.scroll_into_view_if_needed()
    await page.wait_for_timeout(500)

    box = await card.bounding_box()
    if not box:
        raise StopBot("Could not find the card's bounding box.")

    sx, sy = 10, 10
    tx, ty = box["x"] + box["width"] / 2, box["y"] + box["height"] / 2
    await page.mouse.move(sx, sy)
    await page.wait_for_timeout(100)
    for i in range(1, 11):
        await page.mouse.move(sx + (tx - sx) * i / 10, sy + (ty - sy) * i / 10)
        await page.wait_for_timeout(50)
    await page.wait_for_timeout(2000)

    blog.info("Launching game (%s)...", mode)
    play_btn = card.locator("*:has-text('DEMO'), *:has-text('PLAY')").filter(has_text=mode).last
    try:
        await play_btn.wait_for(state="visible", timeout=4000)
        await play_btn.click(timeout=5000)
    except Exception as e:
        blog.warning("Locator click failed (%s). Falling back to JS click.", e)
        res = await page.evaluate(_JS_CLICK, mode.upper())
        if res != "clicked":
            raise StopBot(f"Fallback JS click failed: {res}")

    await page.wait_for_timeout(3000)
    if await page.locator(".casino-money-transfer").first.is_visible():
        raise StopBot("Casino wallet transfer dialog opened: move money to the casino wallet first")
    return await find_game_canvas(page)


# =====================================================================
# 6. CALLBACKS
# =====================================================================

_http = None


def _client():
    global _http
    if _http is None:
        import httpx
        _http = httpx.AsyncClient(timeout=10)
    return _http


async def close_http():
    global _http
    if _http is not None:
        await _http.aclose()
        _http = None


async def post_json(url: str, payload: dict):
    try:
        await _client().post(url, json=payload)
    except Exception as e:
        log.warning("Django callback failed: %s", e)


def fire(coro, pending: set):
    task = asyncio.create_task(coro)
    pending.add(task)
    task.add_done_callback(pending.discard)


# =====================================================================
# 7. SPIN + BOT LOOP
# =====================================================================

async def do_spin(clicker: Clicker, cfg: dict, username: str, is_demo: bool, blog) -> dict:
    await asyncio.sleep(cfg["pre_spin_drain"])
    clicker.listener.clear()

    attempts = int(cfg["spin_attempts"])
    res = None
    clicked = False
    t_ref = time.monotonic()

    for attempt in range(1, attempts + 1):
        try:
            await clicker.click_spin()
            clicked = True
            t_ref = time.monotonic()
        except Exception as e:
            blog.warning("Spin click failed (attempt %d/%d): %s", attempt, attempts, e)
            continue
        res = await clicker.listener.wait_result(cfg["result_wait"])
        if res:
            t_ref = time.monotonic()
            break
        blog.warning("No result %.0fs after spin click (attempt %d/%d)%s",
                     cfg["result_wait"], attempt, attempts,
                     ", clicking spin again" if attempt < attempts else "")

    if not clicked:
        return {"click_failed": True}

    if res is None:  # last chance: the result may just be slow
        res = await clicker.listener.wait_result(15.0)
        if res:
            t_ref = time.monotonic()

    number = res["winning_number"] if res else None
    spin_balance = res["spin_balance"] if res else None
    if number is None:
        blog.warning("No winning number. Last ignored response: %s",
                     clicker.listener.last_filtered)

    # wait until the game is ready for the next bets
    await asyncio.sleep(max(0.0, cfg["round_time"] - (time.monotonic() - t_ref)))

    if is_demo:
        settled = spin_balance
    else:
        settled = clicker.listener.settled
        if settled is None:
            settled = await clicker.listener.wait_settled(10.0)

    recovered = False
    if number is None:
        await asyncio.sleep(cfg["recovery_wait"])
        late = clicker.listener.result
        if late:
            number, recovered = late["winning_number"], True
            if is_demo and settled is None:
                settled = late["spin_balance"]
        if number is None:
            number = await clicker.read_winning_number_from_page()
            recovered = number is not None
        if number is None:
            blog.warning("Winning number unrecoverable for this spin")

    if settled is None and not is_demo:
        await asyncio.sleep(3.0)
        settled = await clicker.fetch_balance(username)
        if settled is None:
            return {"failed_hard": True}

    return {"winning_number": number, "settled_balance": settled, "recovered": recovered}


async def run_bot_session(config: dict, django_callback_url: Optional[str] = None,
                          browser: Optional[Browser] = None):
    cfg = {**DEFAULTS, **config}
    is_demo = bool(cfg["is_demo"])
    if not is_demo and not cfg["confirm_real"]:
        raise SystemExit("Real money mode needs confirm_real=True in the config.")
    if int(cfg["chip"]) < 1:
        raise SystemExit("chip must be at least 1")

    bot_id = cfg.get("bot_id", 0)
    blog = BotLog(log, {"bot_id": bot_id})
    mode = "DEMO" if is_demo else "PLAY"
    username = cfg["mozzart_user"]
    strategy = MurungaStrategy(cfg)
    blog.info("Murunga: mode=%s chip=%s streak=%s x%s max_doublings=%s",
              mode, cfg["chip"], cfg["streak_length"], cfg["multiplier"], cfg["max_doublings"])

    initial_balance: Optional[float] = None
    last_balance: Optional[float] = None
    spins_done = 0
    stop_reason = "max_spins"
    status = "failed"
    last_spin = None
    pending: set = set()
    spin_log = open(f"spins_log_{bot_id}.jsonl", "a", buffering=1)

    pw = None
    owns_browser = browser is None
    if owns_browser:
        pw = await async_playwright().start()
        browser = await pw.chromium.launch(headless=cfg["headless"], args=BROWSER_ARGS)
    session = MozzartSession(browser, "https://www.mozzartbet.co.ke", username,
                             cfg["mozzart_pass"], Path("sessions"), idx=bot_id)

    try:
        await session.start()
        await session.ensure_login()
        frame = await open_roulette(session, mode, blog)
        clicker = Clicker(frame, session.page, cfg, is_demo)
        await clicker.prepare()
        await asyncio.sleep(3.0)

        if not is_demo:
            initial_balance = await clicker.fetch_balance(username)
            if initial_balance is None:
                raise StopBot("Could not read starting balance; refusing to bet blind")
            last_balance = initial_balance
            blog.info("Start balance=%.2f", last_balance)
        # In demo, the start balance is worked out from the first spin.

        errors = 0
        table_ready = False  # True = bets are already on the table, only the spin is pending
        bets: dict = {}
        stake = 0.0

        while spins_done < cfg["max_spins"]:
            # ---- stop conditions ----
            if initial_balance is not None and last_balance is not None:
                pnl = last_balance - initial_balance
                if cfg["stop_loss"] is not None and pnl <= -cfg["stop_loss"]:
                    stop_reason = "stop_loss"
                    break
                if cfg["take_profit"] is not None and pnl >= cfg["take_profit"]:
                    stop_reason = "take_profit"
                    break
            if not is_demo and last_balance is not None and last_balance <= cfg["min_balance"]:
                stop_reason = "min_balance"
                break

            if not table_ready:
                bets = strategy.next_bets()
                stake = float(sum(bets.values()))

                too_big = stake > cfg["max_stake_per_spin"]
                broke = (not is_demo) and last_balance is not None and stake > last_balance
                if too_big or broke:
                    if strategy.in_chain:
                        blog.error("Stake %.2f not allowed (limit %.2f, balance %s): abandoning chain",
                                   stake, cfg["max_stake_per_spin"], last_balance)
                        strategy.abandon()
                        continue
                    raise StopBot(f"Cannot afford the smallest bet ({stake:.2f})")

                blog.info("---- SPIN %d/%d ---- bets %s (stake %.2f) %s",
                          spins_done + 1, cfg["max_spins"], bets, stake, strategy.state())

                try:
                    placed = await clicker.place_bets(bets)
                except Exception as e:
                    blog.warning("Bet placement raised: %s", e)
                    placed = False

                if not placed:
                    errors += 1
                    if errors >= cfg["max_consecutive_errors"]:
                        raise StopBot(f"Bet placement failed {errors} times in a row")
                    try:
                        await clicker.clear_table()
                    except Exception:
                        pass
                    await asyncio.sleep(cfg["spin_retry_delay"])
                    continue
                table_ready = True

            # ---- spin ----
            try:
                out = await do_spin(clicker, cfg, username, is_demo, blog)
            except Exception as e:
                blog.exception("Spin raised")
                raise SettlementError(f"Unrecoverable spin error: {e}")

            if out.get("failed_hard"):
                raise SettlementError("Spin fired but balance unreadable")

            if out.get("click_failed"):
                errors += 1
                if errors >= cfg["max_consecutive_errors"]:
                    raise StopBot(f"Spin never fired in {errors} attempts")
                await asyncio.sleep(cfg["spin_retry_delay"])
                continue  # bets stay on the table, we only retry the spin click

            errors = 0
            table_ready = False
            number = out["winning_number"]
            new_balance = out["settled_balance"]
            pnl_exp = expected_pnl(bets, number) if number is not None else None

            # ---- balance checks ----
            if not is_demo:
                if new_balance is None or new_balance < 0:
                    raise SettlementError(f"Bad balance after spin: {new_balance}")
                if last_balance is not None:
                    if new_balance < last_balance - stake - cfg["settlement_tolerance"]:
                        raise SettlementError(
                            f"Balance dropped more than stake: {last_balance:.2f} -> "
                            f"{new_balance:.2f} (stake {stake:.2f})")
                    if pnl_exp is not None and abs((new_balance - last_balance) - pnl_exp) > cfg["settlement_tolerance"]:
                        blog.warning("Balance change %.2f differs from expected %.2f "
                                     "(a chip click may have missed)",
                                     new_balance - last_balance, pnl_exp)
            elif new_balance is not None and initial_balance is None:
                initial_balance = new_balance - (pnl_exp or 0.0)

            if new_balance is not None:
                last_balance = new_balance
            spins_done += 1
            total_pnl = (last_balance - initial_balance) if None not in (last_balance, initial_balance) else 0.0

            if number is None:
                blog.warning("SPIN %d: no winning number, strategy not updated", spins_done)
            else:
                strategy.on_result(number)
                blog.info("Number %s%s | expected %+.2f | balance %s | total P/L %+.2f",
                          number, " (recovered)" if out.get("recovered") else "",
                          pnl_exp if pnl_exp is not None else 0.0, last_balance, total_pnl)

            last_spin = {
                "spin_number": spins_done,
                "winning_number": number,
                "expected_pnl": pnl_exp,
                "recovered": out.get("recovered", False),
                "bet_amount": stake,
                "balance_after": last_balance,
                "strategy_state": strategy.state(),
            }
            spin_log.write(json.dumps(
                {"ts": time.strftime("%Y-%m-%d %H:%M:%S"), "bot_id": bot_id,
                 "mode": mode, "bets": bets, **last_spin}) + "\n")
            if django_callback_url:
                fire(post_json(f"{django_callback_url}/api/bots/{bot_id}/spin/", last_spin), pending)

        else:
            stop_reason = "max_spins"
        status = "completed"

    except SettlementError as e:
        blog.error("FATAL: %s", e)
        stop_reason, status = str(e), "settlement_halt"
    except StopBot as e:
        blog.error("Stopped: %s", e)
        stop_reason, status = str(e), "stopped"
    except Exception as e:
        blog.exception("Bot crashed")
        stop_reason, status = str(e), "failed"
    finally:
        profit = (last_balance - initial_balance) if None not in (last_balance, initial_balance) else 0.0
        blog.info("DONE | spins=%d start=%s end=%s P/L=%+.2f | reason=%s",
                  spins_done, initial_balance, last_balance, profit, stop_reason)
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
        if django_callback_url:
            await post_json(f"{django_callback_url}/api/bots/{bot_id}/status/", {
                "bot_id": bot_id, "status": status, "stop_reason": stop_reason,
                "spins_completed": spins_done, "initial_balance": initial_balance,
                "current_balance": last_balance, "profit": profit, "last_spin": last_spin,
            })
        spin_log.close()
        await session.close()
        if owns_browser:
            await browser.close()
            await pw.stop()


async def run_bots(configs: list, django_callback_url: Optional[str] = None,
                   headless: bool = True, stagger: float = 4.0,
                   max_concurrent: Optional[int] = None):
    """Run many bots in ONE browser process (one context per bot)."""
    sem = asyncio.Semaphore(max_concurrent) if max_concurrent else None

    async def one(i: int, cfg: dict):
        await asyncio.sleep(i * stagger)  # spread the logins and game loads
        if sem:
            async with sem:
                await run_bot_session(cfg, django_callback_url, browser)
        else:
            await run_bot_session(cfg, django_callback_url, browser)

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=headless, args=BROWSER_ARGS)
        try:
            await asyncio.gather(*(one(i, c) for i, c in enumerate(configs)),
                                 return_exceptions=True)
        finally:
            await browser.close()
            await close_http()


# =====================================================================
# 8. STANDALONE RUN
# =====================================================================

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s",
                        datefmt="%H:%M:%S")

    BOT_COUNT = int(os.getenv("BOT_COUNT", "1"))
    configs = []
    for i in range(1, BOT_COUNT + 1):
        # MOZZART_USER_1 / MOZZART_PASS_1 ... fall back to MOZZART_USER / MOZZART_PASS
        user = os.getenv(f"MOZZART_USER_{i}") or os.getenv("MOZZART_USER")
        pw_ = os.getenv(f"MOZZART_PASS_{i}") or os.getenv("MOZZART_PASS")
        if not user or not pw_:
            raise SystemExit(f"Missing credentials for bot {i} in {ENV_PATH}")
        configs.append({
            "bot_id": i,
            "mozzart_user": user,
            "mozzart_pass": pw_,
            "is_demo": True,
            "confirm_real": False,
            "chip": 10,
            "streak_length": 4,
            "multiplier": 2,
            "max_doublings": 5,
            "max_spins": 1000,
            "stop_loss": 500.0,
            "max_stake_per_spin": 1000.0,
        })

    asyncio.run(run_bots(configs, headless=True, stagger=4.0))