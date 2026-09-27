#!/usr/bin/env python3
"""
check_ui.py -- syntax-check the dashboard's JavaScript as the BROWSER sees it.

Run this after ANY edit to teddy.py's CONSOLE block:

    python check_ui.py

Why it exists: CONSOLE is a plain (non-raw) Python triple-quoted string, so a
backslash escape written for JavaScript is consumed by PYTHON first. Writing
`split('\\n')` in the source puts a real newline inside a JS string literal and
takes the entire dashboard down with "Invalid or unexpected token" -- every
field blank, no chart, no report, and nothing in the server log because the
failure is in the browser.

Checking the source text with a regex does NOT catch this: in the file the
backslash is still there. Only the string Python actually BUILDS is the truth,
which is why this reads it back with ast.
"""
import ast, re, subprocess, sys, shutil, pathlib

ROOT = pathlib.Path(__file__).parent
src = (ROOT / "teddy.py").read_text(encoding="utf-8")

con = None
for node in ast.parse(src).body:
    if (isinstance(node, ast.Assign) and getattr(node.targets[0], "id", "") == "CONSOLE"
            and isinstance(node.value, ast.Constant)):
        con = node.value.value
if con is None:
    sys.exit("could not find the CONSOLE string in teddy.py")

blocks = re.findall(r"<script>(.*?)</script>", con, re.S)
if not blocks:
    sys.exit("no <script> block found in CONSOLE")

ok = True
if not shutil.which("node"):
    print("  node not found -- skipping the parse check (install Node to enable it)")
else:
    for i, b in enumerate(blocks):
        p = ROOT / f".ui_block_{i}.js"
        p.write_text(b, encoding="utf-8")
        r = subprocess.run(["node", "--check", str(p)], capture_output=True, text=True)
        if r.returncode:
            ok = False
            print(f"  BLOCK {i} FAILED TO PARSE:\n{r.stderr}")
        else:
            print(f"  block {i}: {len(b):,} chars — parses clean")
        p.unlink(missing_ok=True)

# A real newline inside a single-quoted JS string is the specific failure above.
for n, line in enumerate(con.split("\n"), 1):
    q = line.count("'") - line.count("\\'")
    if q % 2 and "//" not in line and "/*" not in line:
        print(f"  warn: line {n} has an odd number of quotes — {line.strip()[:80]}")

sys.exit(0 if ok else 1)
