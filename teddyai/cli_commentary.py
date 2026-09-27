#!/usr/bin/env python
r"""
cli_commentary.py -- write the plan through the Claude Code CLI instead of the Anthropic API.

    python cli_commentary.py --check        is the CLI installed and logged in?
    python cli_commentary.py --write        write today's commentary through it

WHY THIS EXISTS. command_center.regenerate_commentary() calls api.anthropic.com and is billed
per rewrite -- roughly 15-25 cents on Opus, three times a weekday. The Claude Code CLI runs on
a Pro/Max SUBSCRIPTION instead, so a scheduled job can write the same commentary against quota
you are already paying for.

WHAT IS AND IS NOT SHARED. Only the sending differs. This module builds nothing and validates
nothing: it hands regenerate_commentary a `send` callable, and the reply comes back through the
same _extract_json, the same _check_cards, the same _structure_check stop recompute, the same
interpret.verify_commentary level check, the same corrective rounds and the same _write_prose.
A plan written this way is exactly as checked as one written through the API.

THE THINGS TO KNOW BEFORE RELYING ON IT

  * The CLI is a separate install from the desktop app, which exposes no scriptable entry
    point:  npm install -g @anthropic-ai/claude-code   then   claude login
  * Its rate limits are the SAME limits as your interactive use. A heavy afternoon in Claude
    Code can leave the 17:15 job with nothing left. The API has no such coupling.
  * The login expires. When it does, an unattended job fails at 06:05 with nobody watching --
    which is why gameplan_bot's `cli-then-api` mode exists, and why --check is worth running
    after any long gap.
  * The CLI is an agentic loop with tools, not a single message: it can read files and take
    several turns. --output-format json plus a system prompt pinning "one JSON object, nothing
    else" is what keeps it to one clean reply, and _extract_json is the backstop.
  * THE CLI BILLS AN API KEY IF IT CAN SEE ONE. Claude Code prefers ANTHROPIC_API_KEY in its
    environment over the subscription login (once the key has been approved in ~/.claude.json
    it does so silently). On 2026-09-10 every "subscription" round was billed to the key the API
    path keeps in the user environment, because subprocess.run inherited it. The CLI is now
    launched with a scrubbed environment (_clean_env), and --check reports what
    `claude auth status` sees from inside that environment.
"""
from __future__ import annotations
import argparse, json, os, re, shutil, subprocess, sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent

# The CLI is a node program; on Windows npm installs it as claude.cmd, which is not executable
# by CreateProcess without the extension, so the .cmd form has to be searched for too.
CANDIDATES = ("claude", "claude.cmd", "claude.exe")

# Long enough for an agentic loop that thinks before it writes; short enough that a wedged
# process cannot hold a scheduled task open until its one-hour limit.
TIMEOUT = int(os.environ.get("CC_CLI_TIMEOUT", "300"))

# The CLI is interactive by nature; -p is its headless "print" mode.
#
# --system-prompt REPLACES Claude Code's own system prompt rather than appending to it, and
# that distinction decided whether this worked at all. With --append-system-prompt the model
# carries the whole coding-agent brief -- tools, repo conventions, the lot -- underneath the
# sheet's instructions, and on 2026-09-10 it failed interpret.verify_commentary three rounds
# running by rewording a scenario trigger. Replacing it makes the call what the API path is:
# the house prompt, the sheet, and nothing else.
SYSTEM = ("You return exactly one JSON object and nothing else: no preamble, no markdown "
          "fences, no commentary after it. Do not use any tools. Do not read or write files. "
          "Answer only from the text of the message.")

# Belt and braces on top of the system prompt: an agentic loop that reaches for a file read
# spends turns and can come back having "checked" something that is not the sheet in front of
# it. The plan must be written from the payload alone, exactly as the API path writes it.
NO_TOOLS = "Bash,Read,Write,Edit,Glob,Grep,WebFetch,WebSearch,Task,NotebookEdit,TodoWrite"


# Names Claude Code reads as a credential. Any of these in the CLI's environment turns a
# subscription run into a billed one, so they never reach the subprocess.
CREDENTIAL_VARS = ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN")


def _clean_env() -> dict:
    """The parent environment minus anything the CLI would bill against."""
    return {k: v for k, v in os.environ.items() if k not in CREDENTIAL_VARS}


def leaked_credentials() -> list[str]:
    """Which credential names the PARENT process is carrying -- what would have leaked."""
    return [k for k in CREDENTIAL_VARS if os.environ.get(k)]


def auth_status() -> str:
    """`claude auth status` as the scrubbed subprocess sees it, or why it could not run."""
    exe = which()
    if not exe:
        return "no CLI"
    try:
        r = subprocess.run([exe, "auth", "status"], capture_output=True, text=True, encoding="utf-8",
                           errors="replace", timeout=60, env=_clean_env())
    except Exception as e:
        return f"auth status would not run: {type(e).__name__}: {e}"
    out = ((r.stdout or "") + (r.stderr or "")).strip()
    return re.sub(r"sk-ant-[A-Za-z0-9_-]+", "sk-ant-<redacted>", out) or f"exit {r.returncode}"


def which() -> str | None:
    """The CLI's path, or None. CLAUDE_CLI overrides the search."""
    env = os.environ.get("CLAUDE_CLI")
    if env and (Path(env).exists() or shutil.which(env)):
        return env if Path(env).exists() else shutil.which(env)
    for name in CANDIDATES:
        p = shutil.which(name)
        if p:
            return p
    return None


def available() -> tuple[bool, str]:
    """(usable, why not). Checks the binary exists and answers --version; a login that has
    lapsed is not visible here and only shows up as a failed send."""
    exe = which()
    if not exe:
        return False, ("the Claude Code CLI is not installed. "
                       "npm install -g @anthropic-ai/claude-code, then: claude login "
                       "(or set CLAUDE_CLI to its path)")
    try:
        r = subprocess.run([exe, "--version"], capture_output=True, text=True,
                           encoding="utf-8", errors="replace", timeout=60, env=_clean_env())
    except Exception as e:
        return False, f"{exe} would not run: {type(e).__name__}: {e}"
    if r.returncode != 0:
        return False, f"{exe} --version exited {r.returncode}: {(r.stderr or r.stdout)[:200]}"
    return True, (r.stdout or "").strip().splitlines()[0] if r.stdout else exe


def _text_from(stdout: str) -> str:
    """The model's reply out of whatever --output-format json wrapped it in.

    The CLI's envelope has moved between versions, so this is deliberately forgiving: take the
    documented field if it is there, else any obvious text field, else the raw stdout and let
    _extract_json do the work. Never raise here -- a shape change must degrade, not break."""
    s = (stdout or "").strip()
    if not s:
        return ""
    try:
        doc = json.loads(s)
    except Exception:
        return s                                    # plain text output, or a stray banner
    if isinstance(doc, list):                       # a stream of events: the last text wins
        for item in reversed(doc):
            if isinstance(item, dict):
                for k in ("result", "text", "content"):
                    v = item.get(k)
                    if isinstance(v, str) and v.strip():
                        return v
        return s
    if isinstance(doc, dict):
        for k in ("result", "text", "response", "content", "output"):
            v = doc.get(k)
            if isinstance(v, str) and v.strip():
                return v
            if isinstance(v, list):                 # content blocks
                joined = "".join(b.get("text", "") for b in v
                                 if isinstance(b, dict) and isinstance(b.get("text"), str))
                if joined.strip():
                    return joined
        if doc.get("is_error") or doc.get("error"):
            raise RuntimeError(f"the CLI returned an error: {str(doc.get('error') or doc)[:300]}")
    return s


def send(user_msg: str) -> str:
    """The `send` callable regenerate_commentary takes. Returns the raw reply text.

    The prompt goes in on STDIN, not as an argument: it is tens of kilobytes of JSON and
    Windows caps a command line at 32,767 characters."""
    exe = which()
    if not exe:
        ok, why = available()
        raise RuntimeError(why)

    cmd = [exe, "-p", "--output-format", "json",
           "--system-prompt", SYSTEM,                      # REPLACE, do not append -- see above
           "--exclude-dynamic-system-prompt-sections",     # no cwd/git/env preamble either
           "--disallowed-tools", NO_TOOLS]
    model = os.environ.get("CC_CLI_MODEL")
    if model:
        cmd += ["--model", model]
    try:
        # env=_clean_env(): with ANTHROPIC_API_KEY inherited the CLI bills the key, not the
        # subscription, and the whole reason this module exists is defeated silently.
        r = subprocess.run(cmd, input=user_msg, capture_output=True, text=True,
                           encoding="utf-8", errors="replace", timeout=TIMEOUT, cwd=ROOT,
                           env=_clean_env())
    except subprocess.TimeoutExpired:
        raise RuntimeError(f"the CLI did not answer within {TIMEOUT}s "
                           f"(raise CC_CLI_TIMEOUT if this is normal for your machine)")
    if r.returncode != 0:
        err = ((r.stderr or "") + (r.stdout or "")).strip()[:400]
        # The two failures worth naming, because they look like bugs and are not
        if re.search(r"log ?in|auth|unauthor|credential|session", err, re.I):
            raise RuntimeError(f"the CLI is not logged in -- run: claude login  ({err})")
        if re.search(r"rate|limit|quota|usage", err, re.I):
            raise RuntimeError(f"subscription limit reached -- the plan cannot be written now ({err})")
        raise RuntimeError(f"the CLI exited {r.returncode}: {err or 'no output'}")
    text = _text_from(r.stdout)
    if not text.strip():
        raise RuntimeError(f"the CLI returned nothing on stdout (stderr: {(r.stderr or '')[:200]})")
    return text


def main():
    for s in (sys.stdout, sys.stderr):
        try:
            s.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass
    ap = argparse.ArgumentParser(description="Write the commentary through the Claude Code CLI")
    ap.add_argument("--check", action="store_true", help="is the CLI installed and answering?")
    ap.add_argument("--write", action="store_true", help="write today's commentary through it")
    a = ap.parse_args()

    if a.check or not a.write:
        ok, why = available()
        print(("OK   " if ok else "NO   ") + why)
        leaked = leaked_credentials()
        if leaked:
            print(f"\nNOTE {', '.join(leaked)} is set in this environment. It is STRIPPED before the CLI\n"
                  f"     runs, so the CLI cannot bill it. Remove it from the user environment if nothing\n"
                  f"     else needs it -- the /cc Rewrite button is the only thing that does.")
        if ok:
            print("\nclaude auth status, from inside the scrubbed environment (authMethod must read\n"
                  "claude.ai, not apiKey):")
            print("     " + auth_status().replace("\n", "\n     "))
            print("\nA --version that answers does not prove the LOGIN is current. The only real\n"
                  "test is a write; --write does one, and costs nothing but subscription quota.")
        return 0 if ok else 1

    if str(ROOT) not in sys.path:
        sys.path.insert(0, str(ROOT))
    import command_center
    command_center.regenerate_commentary(send=send)
    print("commentary written through the CLI; run build.py to re-render, or "
          "gameplan_bot.py --post-only to publish")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
