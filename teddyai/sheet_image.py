#!/usr/bin/env python
r"""
sheet_image.py -- the rendered sheet as PNGs, so a channel sees Basilisk and not a paraphrase.

    python sheet_image.py --out notes\shots            # the default parts
    python sheet_image.py --parts all                  # every section, ladder included
    python sheet_image.py --list                       # what sections the sheet has

Reformatting the sheet into Discord embeds loses the thing that makes it readable: the stat
strip, the card grid, the ladder. This renders the real page instead, in sections, and
discord_post.py attaches the PNGs.

HOW IT WORKS, AND WHY THERE IS NO DEPENDENCY

trinity.html is one self-contained document: one <style> block, no scripts, fonts from Google.
So a "section" can be rendered on its own by writing a temporary page that is the sheet's own
preamble plus that one <section> inside the sheet's own .wrap -- identical CSS, identical
layout, no browser automation needed to clip it.

The screenshot itself is Chrome or Edge in headless mode, which is already on any Windows
machine, driven by the command line. Playwright would be tidier and is what teddychart's test
harness uses, but it is a 150 MB install for a scheduled job whose whole point is to run
unattended, and headless Chrome does this correctly with nothing to install. Pillow trims the
result to its content, because the command line cannot ask the page how tall it is.

The fonts come from fonts.googleapis.com. Offline, the sheet renders in the fallback stack --
different, still correct. That is the only network dependency and it is not worth removing.
"""
from __future__ import annotations
import argparse, os, re, shutil, subprocess, sys, tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent

# The parts a game plan post carries. Just the cards: they are what the channel opens the post
# for, and three images of a plan is a wall nobody scrolls. The levels the overview image used
# to carry -- reference, flips, rails, net gamma, book -- are in discord_post.header_line, as
# text, on the same message. Everything else is on request: --parts overview,gameplan,reading
# for the old three, "analysis" for the ladder, "state" for the market-state panel, or "all".
DEFAULT_PARTS = ("gameplan",)

WIDTH = 1100            # CSS px. The sheet's .wrap is built for roughly this; wider only pads.
# Per-section overrides. The card grid is repeat(auto-fit,minmax(320px,1fr)): at 1100 it lays
# out three across and strands D alone on a second row beside a third of an image of nothing.
# At 900 it is a 2x2 with no dead space, which is also a better shape in a Discord message.
PART_WIDTH = {"gameplan": 900,
              # .mast is a flex row with the title left and the reference block right,
              # and h1 is clamp(28px,4.4vw,42px). At 1100 an intraday masthead ("... as of
              # 09:54 PT - built 14:35") fills the row and the reference block wraps under
              # it, stranded mid-page. 1280 keeps them on one line.
              "overview": 1280}
SCALE = 2               # device pixel ratio -- 2 keeps the mono numerals crisp after Discord's resize
MAX_HEIGHT = 20000      # a guard, not a target: the ladder runs past 6,000 CSS px on a full board
PAD = 12                # px of background kept around the trimmed content


def browser() -> str:
    """Chrome or Edge, whichever is there. TEDDY_BROWSER overrides."""
    env = os.environ.get("TEDDY_BROWSER")
    if env and Path(env).exists():
        return env
    for c in (r"C:\Program Files\Google\Chrome\Application\chrome.exe",
              r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
              r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
              r"C:\Program Files\Microsoft\Edge\Application\msedge.exe"):
        if Path(c).exists():
            return c
    for name in ("chrome", "msedge", "chromium", "google-chrome"):
        p = shutil.which(name)
        if p:
            return p
    raise RuntimeError("no Chrome or Edge found -- set TEDDY_BROWSER to a browser executable")


# ----------------------------------------------------------------------------------
# Splitting the sheet
# ----------------------------------------------------------------------------------
def _slug(label: str) -> str:
    l = label.lower()
    for key, name in (("reading", "reading"), ("gameplan", "gameplan"),
                      ("teddy analysis", "analysis"), ("market state", "state")):
        if key in l:
            return name
    return re.sub(r"[^a-z0-9]+", "-", l).strip("-")[:24] or "part"


def split(html: str) -> list[tuple[str, str]]:
    """[(name, standalone html)] -- the masthead block, then one entry per <section class="sec">.

    Each fragment is wrapped in the sheet's own preamble and .wrap, so it renders with the
    sheet's CSS and the sheet's page width and needs nothing else."""
    wrap = html.find('<div class="wrap"')
    if wrap < 0:
        raise RuntimeError("trinity.html has no .wrap container -- has build.py changed shape?")
    preamble = html[:wrap]
    open_tag = re.match(r'<div class="wrap"[^>]*>', html[wrap:])
    open_tag = open_tag.group(0) if open_tag else '<div class="wrap">'

    starts = [m.start() for m in re.finditer(r'<section class="sec">', html)]
    bounds = [wrap + len(open_tag)] + starts + [len(html)]

    out = []
    for i in range(len(bounds) - 1):
        frag = html[bounds[i]:bounds[i + 1]]
        if i == 0:
            name = "overview"
        else:
            h = re.search(r"<h[12][^>]*>(.*?)</h[12]>", frag, re.S)
            name = _slug(re.sub(r"<[^>]+>", " ", h.group(1))) if h else f"part{i}"
        # the tail fragment carries the sheet's closing </div>; everything else needs one added
        page = preamble + open_tag + frag
        if page.count("<div") > page.count("</div>"):
            page += "</div>" * (page.count("<div") - page.count("</div>"))
        out.append((name, page))
    return out


# ----------------------------------------------------------------------------------
# Screenshot and trim
# ----------------------------------------------------------------------------------
def _shoot(page: Path, png: Path, width: int, height: int, scale: int, profile: Path) -> tuple[bool, str]:
    """(wrote a file, what the browser said if it did not).

    Chrome exits 0 whether or not it managed to write the screenshot -- a bad --screenshot path
    is an ERROR line on stderr and a successful exit -- so the file, not the return code, is
    the test, and stderr is only worth reading when the file is missing."""
    cmd = [browser(),
           "--headless=new", "--disable-gpu", "--hide-scrollbars",
           "--no-first-run", "--no-default-browser-check", "--disable-extensions",
           # a throwaway profile: without it Chrome hands the job to the user's running window
           # and this returns instantly having rendered nothing ("Opening in existing session")
           f"--user-data-dir={profile}",
           f"--force-device-scale-factor={scale}",
           "--virtual-time-budget=5000",              # let the webfonts arrive before the capture
           # ABSOLUTE. A relative --screenshot is not resolved against the working directory and
           # Chrome fails with "cannot find the path specified" while still exiting 0.
           f"--screenshot={png.resolve()}",
           f"--window-size={width},{height}",
           page.resolve().as_uri()]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8",
                           errors="replace", timeout=180)
    except subprocess.TimeoutExpired:
        return False, "the browser did not finish in 180s"
    if png.exists() and png.stat().st_size > 0:
        return True, ""
    err = " | ".join(l.strip() for l in (r.stderr or "").splitlines() if "ERROR" in l)
    return False, (err or (r.stdout or "").strip() or f"browser exited {r.returncode}")[:300]


def _trim(png: Path, pad: int = PAD) -> tuple[int, int, bool]:
    """Crop the uniform background away. Returns (w, h, hit_bottom).

    hit_bottom means the content ran to the last row of the capture, i.e. the window was too
    short and the section is cut off -- the caller re-shoots taller."""
    from PIL import Image, ImageChops
    with Image.open(png) as im:
        im = im.convert("RGB")
        bg = Image.new("RGB", im.size, im.getpixel((0, 0)))
        box = ImageChops.difference(im, bg).getbbox()
        if not box:
            return im.width, im.height, False
        hit_bottom = box[3] >= im.height - 1
        box = (max(0, box[0] - pad), max(0, box[1] - pad),
               min(im.width, box[2] + pad), min(im.height, box[3] + pad))
        out = im.crop(box)
        out.save(png, optimize=True)
        return out.width, out.height, hit_bottom


def render(html_path: Path | str = None, out_dir: Path | str = None, parts=DEFAULT_PARTS,
           width: int = WIDTH, scale: int = SCALE, log=lambda s: None) -> list[Path]:
    """Render the sheet's sections to PNGs. Returns the files in the order `parts` asks for.

    `parts` may be a sequence of names or "all". A name that is not in the sheet is skipped
    with a log line, never an error -- build.py may rename a section and a plan must still go
    out with the sections that are still there."""
    html_path = Path(html_path or ROOT / "trinity.html")
    out_dir = Path(out_dir or ROOT / "notes" / "shots")
    out_dir.mkdir(parents=True, exist_ok=True)

    available = split(html_path.read_text(encoding="utf-8"))
    by_name = dict(available)
    if parts == "all":
        # the default order first, then whatever else the sheet has, so "all" is the normal
        # post plus the extra sections rather than a differently ordered post
        names = [n for n, _ in available]
        wanted = [n for n in DEFAULT_PARTS if n in by_name] + \
                 [n for n in names if n not in DEFAULT_PARTS]
    else:
        wanted = list(parts)

    files = []
    with tempfile.TemporaryDirectory(prefix="teddyshot-") as tmp:
        tmp = Path(tmp)
        profile = tmp / "profile"
        for name in wanted:
            if name not in by_name:
                log(f"[image] no '{name}' section in the sheet -- skipped "
                    f"(have: {', '.join(n for n, _ in available)})")
                continue
            page = tmp / f"{name}.html"
            page.write_text(by_name[name], encoding="utf-8")
            png = out_dir / f"{name}.png"
            w_px = width if width != WIDTH else PART_WIDTH.get(name, width)

            height = 2600
            for attempt in range(3):
                ok, why = _shoot(page, png, w_px, height, scale, profile)
                if not ok:
                    log(f"[image] {name}: no screenshot -- {why}")
                    png = None
                    break
                w, h, cut = _trim(png)
                if not cut or height >= MAX_HEIGHT:
                    if cut:
                        log(f"[image] {name}: still cut off at {height}px -- capped")
                    log(f"[image] {name}.png {w}x{h} ({png.stat().st_size // 1024} KB)")
                    break
                height = min(height * 2, MAX_HEIGHT)      # too short: the section runs past it
            if png:
                files.append(png)
    return files


def main():
    for s in (sys.stdout, sys.stderr):
        try:
            s.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass
    ap = argparse.ArgumentParser(description="Render trinity.html to PNGs, section by section")
    ap.add_argument("--sheet", default=None, help="an HTML sheet (default: trinity.html)")
    ap.add_argument("--out", default=None, help="output folder (default: notes\\shots)")
    ap.add_argument("--parts", default=",".join(DEFAULT_PARTS),
                    help="comma-separated section names, or 'all' (default: %(default)s)")
    ap.add_argument("--width", type=int, default=WIDTH)
    ap.add_argument("--scale", type=int, default=SCALE, help="device pixel ratio (default 2)")
    ap.add_argument("--list", action="store_true", help="list the sheet's sections and exit")
    a = ap.parse_args()

    sheet = Path(a.sheet or ROOT / "trinity.html")
    if a.list:
        for n, frag in split(sheet.read_text(encoding="utf-8")):
            print(f"  {n:<10} {len(frag):>7,} chars")
        return 0

    parts = "all" if a.parts == "all" else [p.strip() for p in a.parts.split(",") if p.strip()]
    files = render(sheet, a.out, parts, a.width, a.scale, log=print)
    if not files:
        print("nothing rendered", file=sys.stderr)
        return 2
    print("\n".join(str(f) for f in files))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
