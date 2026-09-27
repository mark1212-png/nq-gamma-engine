#!/usr/bin/env python3
"""
print_sheet.py -- the sheet as a PDF that was laid out for paper.

    python print_sheet.py                          # trinity.html -> sheets/<title>.pdf
    python print_sheet.py --sheet sheets/2026-09-07-holiday-teddyai.html --out sheets/x.pdf

trinity.html is a responsive web page; printing it as-is wraps the strip, crushes
the ladder's columns and shrinks the type to fit. This applies a print
stylesheet on top of the page's own CSS -- Letter, three pages: (1) header,
strip, lead, reading, caveats; (2) the four cards; (3) the ladder and the VEX
note -- and drops the What-changed block and the method footer, which are for
the screen. Nothing in the sheet itself changes. Needs playwright (the same
install teddychart's test uses).
"""
import argparse, pathlib, re, sys

ROOT = pathlib.Path(__file__).parent.resolve()

PRINT_CSS = """
@page { size: Letter; margin: 0.45in 0.5in 0.5in; }
html, body { background: #fff !important; color: #14181A !important; }
body { font-size: 11.5px; line-height: 1.45; }
.wrap { max-width: none; padding: 0; gap: 14px; }
.mast { padding-bottom: 8px; border-bottom-width: 2px; }
.mast h1 { font-size: 24px; }
.mast .sub { font-size: 11px; margin-top: 4px; max-width: none; }
.stamp { font-size: 9.5px; line-height: 1.6; }
.strip { grid-template-columns: repeat(7, 1fr); border-radius: 0; }
.st { padding: 8px 6px; }
.st .k { font-size: 8.5px; letter-spacing: .06em; }
.st .v { font-size: 16px; margin-top: 2px; }
.st .n { font-size: 9px; line-height: 1.3; }
.chg, .chg0, .foot { display: none !important; }
.note { font-size: 11px; padding: 9px 12px; border-radius: 0; }
.note.w { font-size: 10.5px; }
ul.t { gap: 4px; }
.sech { padding-bottom: 5px; }
.sech h2 { font-size: 14px; }
.sech .hint { font-size: 9.5px; }
.sec { gap: 9px; }
/* page 2: the cards */
section.sec:nth-of-type(2) { break-before: page; }
.plans { grid-template-columns: 1fr 1fr; gap: 12px; }
.pl { break-inside: avoid; border-radius: 0; }
.pl .ph { padding: 9px 12px; }
.pl h3 { font-size: 13px; }
.pl .tg { font-size: 10.5px; }
.pl .cv { font-size: 8.5px; }
.lv { padding: 8px 12px; font-size: 11px; gap: 0 10px; }
.lv div { padding: 2.5px 0; }
.lv .a { font-size: 9px; }
.pl .wy { font-size: 10.5px; padding: 8px 12px 10px; line-height: 1.45; }
/* page 3: the ladder */
section.sec:nth-of-type(3) { break-before: page; }
.lad { overflow: visible; border-radius: 0; break-inside: avoid; }
.grid { min-width: 0; grid-template-columns: 58px 1fr 40px 1fr 40px 1fr 40px 1fr 38px 20px; }
.hd > div { position: static; font-size: 8.5px; padding: 6px 0 5px; }
.k { font-size: 10.5px; padding-right: 8px !important; }
.v { font-size: 9.5px; padding-right: 4px !important; }
.b { height: 12px; }
.b i { height: 9px; top: 1.5px; }
.r > * { padding: 1px 0; }
.mk .mkl { font-size: 8.5px; }
.mk .mkr span { font-size: 8.5px; top: -7px; }
.cf b { width: 14px; height: 14px; line-height: 14px; font-size: 8.5px; }
.note.v { font-size: 10.5px; }
"""


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--sheet", default=str(ROOT / "trinity.html"))
    ap.add_argument("--out", default=None)
    a = ap.parse_args()
    src = pathlib.Path(a.sheet)
    html = src.read_text(encoding="utf-8")
    title = re.search(r"<title>(.*?)</title>", html)
    name = (title.group(1) if title else src.stem).replace("/", "-").replace(" ", "-")
    out = pathlib.Path(a.out) if a.out else (ROOT / "sheets" / f"{name}.pdf")
    out.parent.mkdir(exist_ok=True)
    # the standalone file has no charset meta (the artifact wrapper adds one);
    # without it Chromium guesses and the arrows and minus signs come out as mojibake
    tmp = ROOT / "notes" / "print-tmp.html"
    tmp.parent.mkdir(exist_ok=True)
    tmp.write_text('<!doctype html><html><head><meta charset="utf-8"></head><body>' + html
                   + "</body></html>", encoding="utf-8")
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        sys.exit("pip install playwright && playwright install chromium")
    with sync_playwright() as p:
        b = p.chromium.launch()
        pg = b.new_page(viewport={"width": 1000, "height": 1200})
        pg.goto(tmp.resolve().as_uri(), wait_until="networkidle")
        pg.wait_for_timeout(600)
        pg.emulate_media(media="screen", color_scheme="light")
        # append at the END of the body: the sheet's own <style> sits inside the
        # body (the file has no head of its own), so a tag added to <head>
        # loses every tie on specificity and none of this applies
        pg.evaluate("css => { const s = document.createElement('style'); s.textContent = css; "
                    "document.body.appendChild(s); }", PRINT_CSS)
        pg.pdf(path=str(out), format="Letter", print_background=True, prefer_css_page_size=True)
        b.close()
    tmp.unlink(missing_ok=True)
    print(f"wrote {out}")


if __name__ == "__main__":
    main()
