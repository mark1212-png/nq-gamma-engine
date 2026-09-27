"""Headless check: load the chart against the sim server, wait for the replay,
capture console errors, measure frame cost, screenshot a few configurations."""
import sys, json, asyncio, time
from playwright.async_api import async_playwright

URL = "http://127.0.0.1:5138/"
OUT = sys.argv[1] if len(sys.argv) > 1 else "/tmp/shots"

async def main():
    import os; os.makedirs(OUT, exist_ok=True)
    async with async_playwright() as p:
        b = await p.chromium.launch()
        pg = await b.new_page(viewport={"width": 1600, "height": 900}, color_scheme="dark")
        errors = []
        pg.on("console", lambda m: errors.append(f"{m.type}: {m.text}") if m.type in ("error", "warning") else None)
        pg.on("pageerror", lambda e: errors.append(f"pageerror: {e}"))
        await pg.goto(URL)
        await pg.wait_for_timeout(4000)
        st = await pg.evaluate("document.getElementById('st_state').textContent + ' | prints ' + document.getElementById('st_prints').textContent + ' | fps ' + document.getElementById('st_fps').textContent")
        print("status:", st)
        await pg.screenshot(path=f"{OUT}/01-default.png")
        # footprint on a 15s chart, zoomed in
        await pg.keyboard.press("3"); await pg.keyboard.press("p")
        for _ in range(3): await pg.keyboard.press("+")
        await pg.keyboard.press("End")
        await pg.wait_for_timeout(1500)
        await pg.screenshot(path=f"{OUT}/02-footprint.png")
        # light theme candles 1m with tpo, settings drawer open
        await pg.emulate_media(color_scheme="light")
        await pg.keyboard.press("5"); await pg.keyboard.press("c"); await pg.keyboard.press("f"); await pg.keyboard.press(",")
        await pg.wait_for_timeout(1200)
        await pg.screenshot(path=f"{OUT}/03-light-drawer.png")
        await pg.keyboard.press("Escape")
        # frame timing over 3 s of live ticks
        await pg.wait_for_timeout(3000)
        fps = await pg.evaluate("document.getElementById('st_fps').textContent")
        print("fps/frame:", fps)
        # hover for legend + crosshair
        await pg.mouse.move(900, 400); await pg.wait_for_timeout(400)
        await pg.screenshot(path=f"{OUT}/04-hover.png")
        leg = await pg.evaluate("document.getElementById('legend').innerText")
        print("legend:", leg.replace("\n", " / ")[:400])
        # TPO at session start, tick bars, range bars
        await pg.keyboard.press("Escape"); await pg.keyboard.press("Home")
        for _ in range(2): await pg.keyboard.press("-")
        await pg.wait_for_timeout(800); await pg.screenshot(path=f"{OUT}/05-tpo-start.png")
        await pg.keyboard.press("End")
        await pg.evaluate("[...document.querySelectorAll('.pill')].find(b=>b.dataset.tf==='500t').click()"); await pg.wait_for_timeout(800)
        await pg.screenshot(path=f"{OUT}/06-tick.png")
        await pg.evaluate("[...document.querySelectorAll('.pill')].find(b=>b.dataset.tf==='10r').click()"); await pg.keyboard.press("f"); await pg.wait_for_timeout(800)
        await pg.screenshot(path=f"{OUT}/07-range.png")
        print("console:", "\n  ".join(errors) if errors else "clean")
        await b.close()

asyncio.run(main())
