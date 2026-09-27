#!/usr/bin/env python
"""
Basilisk -> Discord. Renders the built sheet as a game plan and pushes it to a channel.

    python discord_post.py --dry-run          # render, post nothing (writes notes/discord-preview.json)
    python discord_post.py                    # post the current sheet
    python discord_post.py --attach-sheet     # ...and attach the dated HTML sheet

The numbers and the prose both come from command_center.normalize() -- the SAME function the
/cc page renders from. That is deliberate: a second parser would eventually disagree with the
page about what the plan says, and the channel would carry a plan nobody could reproduce.

Configuration is environment only (secrets never belong in a committed file):

    DISCORD_WEBHOOK_URL     https://discord.com/api/webhooks/<id>/<token>     -- preferred
    DISCORD_BOT_TOKEN       + DISCORD_CHANNEL_ID                             -- bot alternative
    DISCORD_MENTION         optional, e.g. <@&1234567890>  (a role ping on the first message)
    DISCORD_USERNAME        optional webhook display name (default: the webhook's own)

Either transport works; the webhook needs no gateway process and no bot in the server, which is
why it is the default. With a bot token this posts through the same REST endpoint the bot would
use, so no discord.py and nothing to keep alive.

THE POST IS THREE MESSAGES, not one 6,000-character embed block:
    1. the header + the lead + the reading
    2. the four setup cards, one embed each
    3. the equity cross-reference (OPRA), when a compare file exists for the book
Discord caps a single message at 6,000 characters across ten embeds; splitting sidesteps the cap
entirely and reads better in a channel. Progress is recorded per message, so a retry after a
half-sent post resumes rather than duplicating (see notes/discord-posted.json).
"""
from __future__ import annotations
import argparse, hashlib, html as _html, json, mimetypes, os, re, sys, time, urllib.error, urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent
NOTES = ROOT / "notes"
LEDGER = NOTES / "discord-posted.json"
PREVIEW = NOTES / "discord-preview.json"
LOG = NOTES / "discord.log"

API = "https://discord.com/api/v10"
UA = "Basilisk-gameplan/1.0"            # Discord asks for a User-Agent on every REST call
DASH = "—"

# Discord's hard limits. Exceeding any of them is a 400 with a field path, not a truncation.
LIM_CONTENT, LIM_DESC, LIM_TITLE, LIM_FIELD, LIM_NAME, LIM_FOOTER = 2000, 4096, 256, 1024, 256, 2048
LIM_EMBEDS, LIM_MESSAGE = 10, 6000

# Embed colours. Long/short are the card colours; the header takes its colour from the sign of
# net gamma at spot, which is the one number that changes what the whole plan is about.
C_LONG, C_SHORT, C_POS, C_NEG, C_INFO, C_WARN = 0x2E9E5B, 0xC0392B, 0x2E9E5B, 0xD98324, 0x4A6FA5, 0xB59410


# ----------------------------------------------------------------------------------
# Prose: the sheet's inline HTML -> Discord markdown
# ----------------------------------------------------------------------------------
_MD = r"([*_~`>|\\])"          # the characters Discord reads as markup, escaped inside text nodes


def md(s: str) -> str:
    """<b>/<i> become **/*, every other tag is dropped, and the TEXT is escaped first so a stray
    underscore in the prose cannot italicise the rest of the paragraph. Order matters: escaping
    after the conversion would escape our own asterisks."""
    if not s:
        return ""
    s = re.sub(r"<br\s*/?>\s*<br\s*/?>", "\x00\x00", s, flags=re.I)
    s = re.sub(r"<br\s*/?>", " ", s, flags=re.I)
    out, pos = [], 0
    for m in re.finditer(r"</?([a-zA-Z][a-zA-Z0-9]*)(?:\s[^>]*)?>", s):
        out.append(re.sub(_MD, r"\\\1", _html.unescape(s[pos:m.start()])))
        tag = m.group(1).lower()
        if tag in ("b", "strong"):
            out.append("**")
        elif tag in ("i", "em"):
            out.append("*")
        pos = m.end()
    out.append(re.sub(_MD, r"\\\1", _html.unescape(s[pos:])))
    return re.sub(r"[ \t]+", " ", "".join(out)).replace("\x00\x00", "\n\n").strip()


def clip(s: str, n: int) -> str:
    """Trim to a limit on a word boundary. Discord counts characters, not bytes."""
    s = s or ""
    if len(s) <= n:
        return s
    cut = s[: n - 1]
    sp = cut.rfind(" ")
    return (cut[:sp] if sp > n * 0.6 else cut).rstrip() + "…"


def px(v, dp=None) -> str:
    """A price the way the sheet writes it: thousands separated, .25/.5/.75 kept, .0 dropped."""
    if v is None:
        return DASH
    v = float(v)
    if dp is not None:
        return f"{v:,.{dp}f}"
    return f"{v:,.0f}" if abs(v - round(v)) < 1e-9 else f"{v:,.2f}".rstrip("0")


def signed(v, dp=0) -> str:
    return DASH if v is None else f"{float(v):+,.{dp}f}"


# ----------------------------------------------------------------------------------
# The report
# ----------------------------------------------------------------------------------
def load_report() -> dict:
    """command_center.normalize(): trinity.json for the numbers, trinity.html for the prose."""
    if str(ROOT) not in sys.path:
        sys.path.insert(0, str(ROOT))
    import command_center
    return command_center.normalize()


def is_stale(R: dict) -> bool:
    """True when the sheet carries build.py's drift banner -- fresh numbers under older words.
    The caveat block is where build.py writes it, and it is the only place normalize() surfaces it."""
    c = (R.get("caveat") or "").lower()
    from publication import validated
    return not validated(ROOT, R.get('snapshot_id')) or "the words are not" in c or "have not been rewritten" in c


def fingerprint(R: dict) -> str:
    """What makes this post a DIFFERENT post: the session, the pricing instant, the words, the cards.
    A rebuild that moved nothing re-renders to the same hash and is not posted twice."""
    S, ref = R.get("session") or {}, R.get("reference") or {}
    key = {
        "date": S.get("date"), "kind": S.get("kind"), "asof": S.get("asof"),
        "price": ref.get("price"), "lead": R.get("lead"),
        "cards": [[c.get("name"), c.get("entry"), c.get("sl"), c.get("tps")] for c in (R.get("setups") or [])],
    }
    return hashlib.sha1(json.dumps(key, sort_keys=True, default=str).encode("utf-8")).hexdigest()[:16]


def opra_cross(R: dict) -> dict | None:
    """The equity books for the same settle the NQ sheet is priced on, from opra_compare.py.

    Only ever the per-book measurements. compare.json's `cross` row carries
    hypothesis_status = UNTESTED and is explicitly not for prose -- it is not read here."""
    settle = (R.get("session") or {}).get("settle")
    if not settle:
        return None
    p = ROOT / "data" / "opra" / f"{settle}-compare.json"
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception as e:
        log(f"[opra] {p.name} unreadable: {type(e).__name__}: {e}")
        return None


# ----------------------------------------------------------------------------------
# Embeds
# ----------------------------------------------------------------------------------
def _field(name, value, inline=True):
    return {"name": clip(name, LIM_NAME), "value": clip(value or DASH, LIM_FIELD), "inline": inline}


def header_embed(R: dict) -> dict:
    S, ref, rails = R.get("session") or {}, R.get("reference") or {}, R.get("rails") or {}
    flips, net, gas = R.get("flips") or {}, R.get("net") or {}, R.get("gamma_at_spot") or {}

    at_spot = gas.get("0dte") if gas.get("0dte") is not None else gas.get("near")
    colour = C_NEG if (at_spot or 0) < 0 else C_POS

    title = f"NQ game plan · {S.get('date') or ''} · {rails.get('label') or S.get('kind') or ''}"
    d = ref.get("vs_settle")
    ref_line = f"**{px(ref.get('price'))}**\n{ref.get('source') or ''}" + (f" ({signed(d)})" if d else "")
    if ref.get("stale"):
        ref_line += "\n⚠ reference is stale"

    flip_line = "\n".join(f"{k} **{px(v)}**" for k, v in
                          (("0DTE", flips.get("0dte")), ("near", flips.get("near")), ("full", flips.get("full")))
                          if v is not None) or DASH
    rail_line = (f"**{px(rails.get('lo'))} – {px(rails.get('hi'))}**\n"
                 f"±{px(rails.get('em'))} at {float(rails.get('scale') or 1):.2f}×")
    net_line = " · ".join(f"{k} {signed(net.get(k))}" for k in ("0dte", "near", "full")
                               if net.get(k) is not None) or DASH
    gas_line = " · ".join(f"{k} {signed(gas.get(k), 1)}" for k in ("0dte", "near", "full")
                               if gas.get(k) is not None) or DASH

    iv = S.get("iv_atm")
    book_line = (f"{S.get('settle') or DASH} {S.get('oi_vintage') or ''}".strip()
                 + f" · {S.get('expiries') or '?'} expiries"
                 + (f"\n{float(S['hours']):.1f}h to the front expiry" if S.get("hours") else "")
                 + (f" · ATM IV {float(iv) * 100:.1f}%" if iv else ""))

    e = {"title": clip(title.strip(" ·"), LIM_TITLE), "color": colour,
         "description": clip(md(R.get("lead") or ""), LIM_DESC),
         "fields": [
             _field("Reference", ref_line),
             _field("Flips", flip_line),
             _field(f"Rails · {rails.get('label') or ''}".strip(" ·"), rail_line),
             _field("Net gamma (column)", net_line, inline=False),
             _field("Net gamma in the band", gas_line, inline=False),
             _field("Book", book_line, inline=False),
         ],
         "footer": {"text": clip(f"Basilisk · priced {S.get('asof') or ''}", LIM_FOOTER)}}
    ts = _iso8601(S.get("asof") or "")
    if ts:
        e["timestamp"] = ts
    return e


def reading_embed(R: dict) -> dict | None:
    items = [x for x in (md(b.get("s") or "") for b in (R.get("reading") or [])) if x]
    if not items:
        return None
    body, used = [], 0
    for i in items:
        line = "• " + i
        if used + len(line) + 2 > LIM_DESC:
            break
        body.append(line)
        used += len(line) + 2
    return {"title": "The reading", "color": C_INFO, "description": "\n\n".join(body)}


def _r_multiple(entry, sl, tp):
    try:
        risk = abs(float(entry) - float(sl))
        return abs(float(tp) - float(entry)) / risk if risk > 1e-9 else None
    except (TypeError, ValueError):
        return None


def setup_embed(c: dict) -> dict:
    side = (c.get("side") or "").lower()
    entry, sl = c.get("entry"), c.get("sl")
    tps = c.get("tps") or ([c["tp"]] if c.get("tp") is not None else [])

    title = c.get("name") or "Setup"
    if c.get("rank"):
        title += f" — {c['rank']}"

    risk = abs(float(entry) - float(sl)) if entry is not None and sl is not None else None
    stop_line = f"**{px(sl)}**" + (f"\nrisk {px(risk)}" if risk else "")

    tp_lines = []
    for n, t in enumerate(tps, 1):
        r = _r_multiple(entry, sl, t)
        tp_lines.append(f"T{n} **{px(t)}**" + (f"  ({r:.1f}R)" if r else ""))

    tag, why = md(c.get("tag") or ""), md(c.get("why") or "")
    # The tag is the trigger and the why is the argument; run together on one line they read as
    # one sentence that is missing a verb. Trigger first, italic, then the reasoning underneath.
    desc = "\n\n".join(x for x in (f"*{tag}*" if tag else "", why) if x)
    return {"title": clip(title, LIM_TITLE),
            "color": C_SHORT if side == "short" else C_LONG,
            "description": clip(desc, LIM_DESC),
            "fields": [_field(f"Entry ({side or 'n/a'})", f"**{px(entry)}**"),
                       _field("Stop", stop_line),
                       _field("Targets", "\n".join(tp_lines) or DASH)]}


def vex_embed(R: dict) -> dict | None:
    v = R.get("vex") or {}
    note, total = md(v.get("note") or ""), v.get("total")
    if not note and total is None:
        return None
    head = f"Total **{signed(total)}** per vol point" if total is not None else ""
    return {"title": "Vanna", "color": C_INFO,
            "description": clip("\n\n".join(x for x in (head, note) if x), LIM_DESC)}


def caveat_embed(R: dict) -> dict | None:
    c = md(R.get("caveat") or "")
    if not c:
        return None
    return {"title": "Caveat", "color": C_WARN, "description": clip(c, LIM_DESC)}


def opra_embed(cmp_: dict) -> dict | None:
    """The equity books beside NQ: dealer gamma within 1% and each book's 0DTE flip.

    NQ is in compare.json too, on the same convention -- it is shown so the channel sees the
    equity complex and the future measured the same way, not two numbers from two methods."""
    books = (cmp_ or {}).get("books") or {}
    if not books:
        return None
    rows, stale = [], []
    for label, b in books.items():
        p0 = (b.get("panels") or {}).get("0DTE") or {}
        bits = [f"**{label}**"]
        if b.get("within_1pct_musd") is not None:
            bits.append(f"within 1%: **{float(b['within_1pct_musd']):+,.0f}** $mn/1%")
        if p0.get("flip") is not None:
            bits.append(f"0DTE flip {px(p0['flip'], 2)}")
        if b.get("ref") is not None:
            bits.append(f"ref {px(b['ref'], 2)}")
        rows.append("  ·  ".join(bits))
        if b.get("stale"):
            stale.append(label)
    foot = [cmp_.get("unit") or "", cmp_.get("sign_convention") or ""]
    if stale:
        foot.append("stale open interest: " + ", ".join(stale))
    return {"title": f"Equity cross-reference · OPRA · book {cmp_.get('date') or ''}",
            "color": C_INFO,
            "description": clip("\n".join(rows), LIM_DESC),
            "footer": {"text": clip(" — ".join(f for f in foot if f), LIM_FOOTER)}}


def _iso8601(s: str) -> str:
    """trinity.json writes '2026-09-10T16:54+00:00'; Discord wants seconds in the timestamp."""
    s = (s or "").strip()
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}[+-]\d{2}:\d{2}", s):
        return s[:16] + ":00" + s[16:]
    return s


def header_line(R: dict) -> str:
    """The image post's one line of text. Images are not searchable, do not show in a phone's
    notification preview, and cannot be copied out of -- so the levels go in the message body
    as well, even though they are also in the picture."""
    S, ref, rails = R.get("session") or {}, R.get("reference") or {}, R.get("rails") or {}
    flips, net = R.get("flips") or {}, R.get("net") or {}
    bits = [f"ref **{px(ref.get('price'))}**"]
    # whole numbers here, as the sheet's own stat strip prints them -- a summary line
    # carrying 29,333.9 reads as a precision the flip does not have
    f = " / ".join(px(flips[k], 0) for k in ("0dte", "near", "full") if flips.get(k) is not None)
    if f:
        bits.append(f"flips {f}")
    if rails.get("lo") and rails.get("hi"):
        bits.append(f"rails **{px(rails['lo'], 0)}–{px(rails['hi'], 0)}**")
    n = " / ".join(signed(net[k]) for k in ("0dte", "near", "full") if net.get(k) is not None)
    if n:
        bits.append(f"net {n}")
    if S.get("settle"):
        bits.append(f"book {S['settle']} {S.get('oi_vintage') or ''}".strip())
    title = f"**NQ game plan · {S.get('date') or ''} · {rails.get('label') or S.get('kind') or ''}**"
    return clip(title.strip(" ·*") + "**\n" + " · ".join(bits) if not title.endswith("**")
                else title + "\n" + " · ".join(bits), LIM_CONTENT)


def render_images(R: dict, parts=None, log=None) -> list[Path]:
    """The sheet's own sections as PNGs. Returns [] on any failure -- an image post that cannot
    be rendered falls back to embeds, because a plan that does not go out is worse than a plan
    that goes out looking plainer."""
    log = log or globals()["log"]
    try:
        import sheet_image
        sheet = ROOT / "trinity.html"
        if not sheet.exists():
            log("[image] no trinity.html to render")
            return []
        return sheet_image.render(sheet, ROOT / "notes" / "shots",
                                  parts or sheet_image.DEFAULT_PARTS, log=log)
    except Exception as e:
        log(f"[image] render failed ({type(e).__name__}: {e}) -- falling back to embeds")
        return []


def build_messages(R: dict, mention: str = "", cmp_: dict | None = None,
                   images: list[Path] | None = None) -> list[dict]:
    """The post, split into messages. Every message is independently under Discord's caps.

    With `images`, the sheet's own sections carry the plan and the text is one header line:
    Discord renders a lone attachment at full message width, so one image per message reads
    far better than three squeezed into a mosaic. Without them, the embed rendering."""
    if images:
        msgs = [{"content": ((mention + " ") if mention else "") + header_line(R),
                 "embeds": [], "_files": [images[0]]}]
        msgs += [{"content": "", "embeds": [], "_files": [p]} for p in images[1:]]
        if cmp_:
            e = opra_embed(cmp_)
            if e:
                msgs.append({"content": "", "embeds": [e]})
        for m in msgs:
            m["allowed_mentions"] = {"parse": ["roles"] if m.get("content") and mention else []}
        return msgs

    msgs = []
    first = [header_embed(R)]
    for e in (reading_embed(R), vex_embed(R), caveat_embed(R)):
        if e:
            first.append(e)
    msgs.append({"content": clip(mention, LIM_CONTENT) if mention else "", "embeds": first})

    cards = [setup_embed(c) for c in (R.get("setups") or [])][:LIM_EMBEDS]
    if cards:
        msgs.append({"content": "", "embeds": cards})

    if cmp_:
        e = opra_embed(cmp_)
        if e:
            msgs.append({"content": "", "embeds": [e]})

    for m in msgs:                       # cap enforcement, message by message
        m["embeds"] = _fit(m["embeds"])
        m["allowed_mentions"] = {"parse": ["roles"] if m.get("content") else []}
    return msgs


def _embed_len(e: dict) -> int:
    return (len(e.get("title") or "") + len(e.get("description") or "")
            + len((e.get("footer") or {}).get("text") or "")
            + sum(len(f["name"]) + len(f["value"]) for f in e.get("fields") or []))


def _fit(embeds: list[dict]) -> list[dict]:
    """Bring a message under 6,000 characters by trimming descriptions from the LAST embed back.
    The header and its numbers are never what gets cut -- the tail prose is."""
    embeds = embeds[:LIM_EMBEDS]
    for _ in range(40):
        total = sum(_embed_len(e) for e in embeds)
        if total <= LIM_MESSAGE:
            return embeds
        over = total - LIM_MESSAGE
        for e in reversed(embeds):
            d = e.get("description") or ""
            if len(d) > 200:
                e["description"] = clip(d, max(200, len(d) - over - 1))
                break
        else:
            if len(embeds) == 1:
                return embeds
            embeds = embeds[:-1]
    return embeds


# ----------------------------------------------------------------------------------
# Transport
# ----------------------------------------------------------------------------------
def target():
    """(kind, url, headers). Webhook wins when both are configured -- it is the cheaper path."""
    hook = (os.environ.get("DISCORD_WEBHOOK_URL") or "").strip()
    tok = (os.environ.get("DISCORD_BOT_TOKEN") or "").strip()
    ch = (os.environ.get("DISCORD_CHANNEL_ID") or "").strip()
    if hook:
        if not re.match(r"https://discord(app)?\.com/api/webhooks/", hook):
            raise RuntimeError("DISCORD_WEBHOOK_URL is not a Discord webhook URL")
        return "webhook", hook + ("&" if "?" in hook else "?") + "wait=true", {}
    if tok and ch:
        return "bot", f"{API}/channels/{ch}/messages", {"Authorization": f"Bot {tok}"}
    raise RuntimeError("no Discord target: set DISCORD_WEBHOOK_URL, or DISCORD_BOT_TOKEN + DISCORD_CHANNEL_ID")


def _multipart(payload: dict, files: list[Path]) -> tuple[bytes, str]:
    """payload_json + attachments, the shape Discord's file upload wants."""
    boundary = "----teddyai" + hashlib.sha1(str(time.time()).encode()).hexdigest()[:16]
    parts = []

    def part(headers: str, body: bytes):
        parts.append(f"--{boundary}\r\n{headers}\r\n\r\n".encode("utf-8") + body + b"\r\n")

    payload = dict(payload)
    payload["attachments"] = [{"id": i, "filename": p.name} for i, p in enumerate(files)]
    part('Content-Disposition: form-data; name="payload_json"\r\nContent-Type: application/json',
         json.dumps(payload).encode("utf-8"))
    for i, p in enumerate(files):
        ctype = mimetypes.guess_type(p.name)[0] or "application/octet-stream"
        part(f'Content-Disposition: form-data; name="files[{i}]"; filename="{p.name}"\r\n'
             f"Content-Type: {ctype}", p.read_bytes())
    parts.append(f"--{boundary}--\r\n".encode("utf-8"))
    return b"".join(parts), f"multipart/form-data; boundary={boundary}"


def send(payload: dict, files: list[Path] | None = None, retries: int = 4) -> dict:
    """One message. Honours 429 Retry-After and backs off on 5xx; any other 4xx is fatal --
    a malformed embed will never succeed, and retrying only posts the same failure again."""
    kind, url, headers = target()
    if kind == "webhook" and os.environ.get("DISCORD_USERNAME"):
        payload = {**payload, "username": os.environ["DISCORD_USERNAME"][:80]}

    if files:
        body, ctype = _multipart(payload, files)
    else:
        body, ctype = json.dumps(payload).encode("utf-8"), "application/json"

    last = None
    for attempt in range(retries):
        req = urllib.request.Request(url, data=body, method="POST",
                                     headers={**headers, "Content-Type": ctype, "User-Agent": UA})
        try:
            with urllib.request.urlopen(req, timeout=45) as r:
                raw = r.read().decode("utf-8", "replace")
            return json.loads(raw) if raw.strip() else {}
        except urllib.error.HTTPError as e:
            detail = e.read().decode("utf-8", "replace")[:600]
            last = RuntimeError(f"Discord HTTP {e.code}: {detail}")
            if e.code == 429:
                try:
                    wait = float(json.loads(detail).get("retry_after", 2))
                except Exception:
                    wait = float(e.headers.get("Retry-After") or 2)
                log(f"[send] rate limited, waiting {wait:.1f}s")
                time.sleep(min(wait + 0.3, 60))
                continue
            if 500 <= e.code < 600:
                time.sleep(2 ** attempt)
                continue
            raise last                      # 400/401/403/404: the payload or the target is wrong
        except (urllib.error.URLError, TimeoutError) as e:
            last = RuntimeError(f"Discord unreachable: {e}")
            time.sleep(2 ** attempt)
    raise last or RuntimeError("Discord post failed")


# ----------------------------------------------------------------------------------
# The ledger -- what has already gone out
# ----------------------------------------------------------------------------------
def _ledger() -> dict:
    try:
        return json.loads(LEDGER.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _ledger_save(d: dict):
    NOTES.mkdir(exist_ok=True)
    if len(d) > 400:                       # keep the last few hundred posts, not every one ever
        d = dict(sorted(d.items(), key=lambda kv: kv[1].get("at", ""))[-400:])
    LEDGER.write_text(json.dumps(d, indent=1), encoding="utf-8")


def log(line: str):
    NOTES.mkdir(exist_ok=True)
    with open(LOG, "a", encoding="utf-8") as f:
        f.write(time.strftime("%Y-%m-%d %H:%M:%S ") + line + "\n")


# ----------------------------------------------------------------------------------
# The post
# ----------------------------------------------------------------------------------
def post_gameplan(dry_run=False, force=False, allow_stale=False, attach_sheet=False,
                  mention: str | None = None, with_opra=True, fmt="image", parts=None) -> dict:
    """Render the current sheet and push it. Returns a result dict; never posts a stale plan
    unless asked to. Raises only on a transport or configuration failure."""
    R = load_report()
    S = R.get("session") or {}
    stale = is_stale(R)
    # A dry run still renders a stale sheet -- the point of a preview is to see what the refusal
    # is about. Only a real post is refused.
    if stale and not allow_stale and not dry_run:
        why = "the sheet's numbers are newer than its words -- rewrite the commentary before posting"
        log(f"[post] refused: {why}")
        return {"posted": False, "reason": "stale", "detail": why, "session": S.get("date")}

    fp = fingerprint(R)
    led = _ledger()
    done = int((led.get(fp) or {}).get("messages") or 0)

    mention = os.environ.get("DISCORD_MENTION", "") if mention is None else mention
    images = render_images(R, parts) if fmt in ("image", "both") else []
    if fmt == "image" and not images:
        log("[post] no images -- posting the embed rendering instead")
    msgs = build_messages(R, mention=mention, cmp_=opra_cross(R) if with_opra else None,
                          images=images if fmt != "embeds" else None)
    if fmt == "both" and images:
        # the embeds first, then the pictures: the numbers stay copyable at the top of the post
        msgs = build_messages(R, mention=mention, cmp_=opra_cross(R) if with_opra else None) + \
               [{"content": "", "embeds": [], "_files": [p],
                 "allowed_mentions": {"parse": []}} for p in images]

    if done >= len(msgs) and not force:
        log(f"[post] {fp} already posted ({done} messages) -- nothing to do")
        return {"posted": False, "reason": "duplicate", "fingerprint": fp, "session": S.get("date")}
    if force:
        done = 0

    files = []
    if attach_sheet:
        p = sheet_path(R)
        if p and p.exists():
            files = [p]
        else:
            log("[post] --attach-sheet: no dated sheet found for this session")

    if dry_run:
        NOTES.mkdir(exist_ok=True)
        PREVIEW.write_text(json.dumps(msgs, indent=1, ensure_ascii=False, default=str),
                           encoding="utf-8")
        return {"posted": False, "reason": "dry-run", "messages": msgs, "fingerprint": fp,
                "stale": stale, "attach": [str(f) for f in files], "images": [str(p) for p in images],
                "session": S.get("date")}

    ids = list((led.get(fp) or {}).get("ids") or [])
    for i, m in enumerate(msgs):
        if i < done:
            continue
        m = dict(m)
        # a message's own image, plus the HTML sheet on the last one when it was asked for
        att = list(m.pop("_files", []))
        if files and i == len(msgs) - 1:
            att += files
        r = send(m, files=att or None)
        ids.append(r.get("id"))
        led[fp] = {"at": time.strftime("%Y-%m-%dT%H:%M:%S"), "session": S.get("date"),
                   "kind": S.get("kind"), "asof": S.get("asof"), "messages": i + 1, "ids": ids}
        _ledger_save(led)
        time.sleep(0.4)                      # Discord's per-channel bucket is 5 messages / 5 s

    log(f"[post] {S.get('date')} {S.get('kind')} -- {len(msgs)} messages, fingerprint {fp}")
    return {"posted": True, "messages": len(msgs), "fingerprint": fp, "ids": ids,
            "session": S.get("date"), "kind": S.get("kind")}


def sheet_path(R: dict) -> Path | None:
    """The newest dated sheet for this session in sheets/ -- what this build wrote."""
    date = (R.get("session") or {}).get("date")
    if not date:
        return None
    cands = sorted((ROOT / "sheets").glob(f"{date}*teddyai.html"), key=lambda p: p.stat().st_mtime)
    return cands[-1] if cands else None


def render_text(msgs: list[dict]) -> str:
    """A readable dry-run rendering -- what the channel will look like, without the JSON."""
    out = []
    for n, m in enumerate(msgs, 1):
        out.append(f"\n=== message {n} " + "=" * 50)
        if m.get("content"):
            out.append(m["content"])
        for p in m.get("_files", []):
            p = Path(p)
            kb = p.stat().st_size // 1024 if p.exists() else 0
            out.append(f"  [attachment] {p.name}  ({kb} KB)")
        for e in m.get("embeds", []):
            out.append(f"\n  [{e.get('title', '')}]")
            if e.get("description"):
                out.append("  " + e["description"].replace("\n", "\n  "))
            for f in e.get("fields", []):
                out.append(f"    {f['name']}: " + f["value"].replace("\n", " / "))
            if (e.get("footer") or {}).get("text"):
                out.append("    -- " + e["footer"]["text"])
        if m.get("embeds"):
            out.append(f"  ({sum(_embed_len(e) for e in m['embeds'])} chars)")
    return "\n".join(out)


def main():
    # The prose carries en dashes and minus signs; a Windows console defaults to cp1252 and a
    # print() of the preview would die on the first one. Same rule as everywhere else here: utf-8.
    for s in (sys.stdout, sys.stderr):
        try:
            s.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass

    ap = argparse.ArgumentParser(description="Post the current TeddyAI game plan to Discord")
    ap.add_argument("--dry-run", action="store_true", help="render and print; post nothing")
    ap.add_argument("--force", action="store_true", help="post even if this exact plan already went out")
    ap.add_argument("--allow-stale", action="store_true",
                    help="post even when the numbers are newer than the words (they will disagree)")
    ap.add_argument("--attach-sheet", action="store_true", help="attach the dated HTML sheet")
    ap.add_argument("--no-opra", action="store_true", help="skip the equity cross-reference embed")
    ap.add_argument("--mention", default=None, help="override DISCORD_MENTION, e.g. '<@&123>'")
    ap.add_argument("--format", choices=("image", "embeds", "both"), default="image",
                    help="image (default): the sheet's own sections as PNGs; embeds: Discord "
                         "embeds; both: the embeds followed by the pictures")
    ap.add_argument("--parts", default=None,
                    help="which sheet sections to render, comma-separated, or 'all' "
                         "(default: overview,gameplan,reading)")
    a = ap.parse_args()

    parts = None
    if a.parts:
        parts = "all" if a.parts == "all" else [p.strip() for p in a.parts.split(",") if p.strip()]

    try:
        r = post_gameplan(dry_run=a.dry_run, force=a.force, allow_stale=a.allow_stale,
                          attach_sheet=a.attach_sheet, mention=a.mention, with_opra=not a.no_opra,
                          fmt=a.format, parts=parts)
    except Exception as e:
        log(f"[post] FAILED: {type(e).__name__}: {e}")
        print(f"FAILED: {e}", file=sys.stderr)
        return 2

    if a.dry_run:
        print(render_text(r["messages"]))
        print(f"\nfingerprint {r['fingerprint']}  attach={r['attach'] or 'none'}  ->  {PREVIEW}")
        if r["stale"]:
            print("STALE: the words were written for an older snapshot. A real post is refused "
                  "until the commentary is rewritten (--allow-stale overrides).")
        return 0
    if not r["posted"]:
        print(f"not posted ({r['reason']}): {r.get('detail', '')}".rstrip(": "))
        return 1 if r["reason"] == "stale" else 0
    print(f"posted {r['messages']} messages for {r['session']} {r['kind']} ({r['fingerprint']})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
