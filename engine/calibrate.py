import asyncio
import json
from pathlib import Path
from playwright.async_api import async_playwright

MAP_FILE = Path(__file__).parent / "roulette_map.json"

TARGETS = (
    [str(i) for i in range(37)]
    + ["1st12", "2nd12", "3rd12", "1-18", "even", "red", "black", "odd", "19-36"]
    + ["chip_minus", "chip_plus"]     # ← The single chip buttons
    + ["clear", "spin", "rebet"]
)

CALIBRATE_JS = """() => new Promise(resolve => {
    const canvas = document.querySelector('canvas');
    const dot = document.createElement('div');
    dot.style.cssText = 'position:fixed;width:20px;height:20px;border:2px solid red;' +
        'border-radius:50%;pointer-events:none;z-index:999999;transform:translate(-50%,-50%)';
    document.body.appendChild(dot);
    const move = e => { dot.style.left = e.clientX + 'px'; dot.style.top = e.clientY + 'px'; };
    const block = e => { e.preventDefault(); e.stopImmediatePropagation(); };
    const events = ['pointerdown', 'pointerup', 'mousedown', 'mouseup'];
    let resolved = false;   // ← guard against double-firing

    function cleanup() {
        dot.remove();
        window.removeEventListener('mousemove', move, true);
        window.removeEventListener('click', click, true);
        events.forEach(t => window.removeEventListener(t, block, true));
    }
    function click(e) {
        if (resolved) return;    // ← prevent double resolution
        resolved = true;
        block(e);
        const r = canvas.getBoundingClientRect();
        const result = {
            x_pct: (e.clientX - r.left) / r.width,
            y_pct: (e.clientY - r.top) / r.height
        };
        cleanup();
        resolve(result);
    }
    window.addEventListener('mousemove', move, true);
    window.addEventListener('click', click, true);
    events.forEach(t => window.addEventListener(t, block, true));
})"""


async def main():
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=False)
        context = await browser.new_context(
            user_agent=("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                        "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"),
            viewport={"width": 1366, "height": 900},
        )
        page = await context.new_page()

        import os
        from dotenv import load_dotenv
        load_dotenv(Path(__file__).parent.parent / ".env", override=True)

        await page.goto("https://www.mozzartbet.co.ke/en", wait_until="domcontentloaded")
        await page.wait_for_selector("input[type='password']", state="visible", timeout=20000)
        await page.locator("input[placeholder*='Mobile'], input[type='text']").first.fill(
            str(os.getenv("MOZZART_USER")))
        await page.locator("input[type='password']").first.fill(str(os.getenv("MOZZART_PASS")))
        await page.locator("button:has-text('OK'), .login-btn, button[type='submit']").first.click()
        await page.wait_for_selector(".user-nav-group.logged-in", timeout=20000)
        print("Logged in.")

        await page.goto("https://www.mozzartbet.co.ke/en#/casino/", wait_until="domcontentloaded")
        await page.wait_for_timeout(3000)
        card = page.locator(".single-game, .single-game-mobile").filter(has_text="Roulette Pragmatic").first
        await card.wait_for(state="visible", timeout=30000)
        await page.mouse.move(0, 0)
        await card.hover()
        await page.wait_for_timeout(1000)
        await card.locator("p.button:has-text('DEMO')").click(force=True)

        print("Waiting for canvas...")
        frame = None
        for _ in range(60):
            for f in page.frames:
                try:
                    c = f.locator("canvas").first
                    if await c.count() and await c.is_visible():
                        box = await c.bounding_box()
                        if box and box["width"] > 300:
                            frame = f
                            break
                except Exception:
                    pass
            if frame:
                break
            await page.wait_for_timeout(1000)

        if not frame:
            raise SystemExit("Could not find the game canvas.")

        print(f"\nCalibrating {len(TARGETS)} targets.")
        print("A red crosshair follows your mouse. Click each target.\n")

        coords = {}
        for target in TARGETS:
            print(f"👉 CLICK: {target.upper()}")
            c = await frame.evaluate(CALIBRATE_JS)
            coords[target] = {
                "x_pct": round(c["x_pct"], 4),
                "y_pct": round(c["y_pct"], 4),
            }
            print(f"   ✅ {target}: X={c['x_pct']:.4f}, Y={c['y_pct']:.4f}\n")

        with MAP_FILE.open("w") as f:
            json.dump(coords, f, indent=4)
        print(f"\nCalibration saved to {MAP_FILE}")

        await context.close()
        await browser.close()


if __name__ == "__main__":
    asyncio.run(main())