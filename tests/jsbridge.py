"""Run individual browser functions from the shipped UI under Node.

Half of the group and card behaviour lives in the embedded UI, not in Python — tombstones,
`groupOf` skipping deleted groups, and the hybrid logical clock are all browser code, and
the group defect that this project actually shipped was browser-side. Testing only the
Python half would leave exactly the code that broke uncovered.

Functions are extracted **verbatim from `mp32_gui.py` at test time**, never copied into the
tests. If a function is renamed or removed the extraction fails loudly instead of silently
testing a stale copy that no longer resembles what ships.

Requires `node` on PATH. Suites that use this skip with a clear message when it is absent,
rather than reporting a pass they did not earn.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import tempfile
from typing import Dict, List

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
GUI = os.path.join(ROOT, "mp32_gui.py")

_source_cache: str = ""


def node_available() -> bool:
    return shutil.which("node") is not None


def _gui_source() -> str:
    global _source_cache
    if not _source_cache:
        with open(GUI, "r", encoding="utf-8") as fh:
            _source_cache = fh.read()
    return _source_cache


def extract_function(name: str) -> str:
    """Return the full text of `function <name>(...){...}` from the shipped UI.

    Brace-matched rather than regex-terminated, so a function containing nested braces,
    object literals or template strings comes back whole.
    """
    src = _gui_source()
    m = re.search(r"^function\s+" + re.escape(name) + r"\s*\(", src, re.M)
    if not m:
        raise LookupError(
            f"function {name}() is no longer present in mp32_gui.py — the test needs "
            f"updating to match the shipped UI, not the other way round")
    start = m.start()
    i = src.index("{", m.end() - 1)
    depth, j = 0, i
    in_str, quote, escaped = False, "", False
    while j < len(src):
        ch = src[j]
        if in_str:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == quote:
                in_str = False
        elif ch in "\"'`":
            in_str, quote = True, ch
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return src[start:j + 1]
        j += 1
    raise LookupError(f"unterminated function {name}() in mp32_gui.py")


def source_contains(pattern: str) -> bool:
    """Guard check for behaviour that cannot be extracted as a standalone function."""
    return re.search(pattern, _gui_source(), re.S) is not None


PRELUDE = """
// Minimal stubs for the handful of globals the extracted functions touch. Anything a
// function actually depends on is provided; nothing is reimplemented.
let __pushed = [];   // pushMeta() calls
let __api = [];      // direct /api/meta_event posts, which pushCard/pushLock use
let localStorageBacking = {};
const localStorage = {
  getItem: k => (k in localStorageBacking ? localStorageBacking[k] : null),
  setItem: (k, v) => { localStorageBacking[k] = String(v); },
};
let meta = { names:{}, colors:{}, groups:{}, cards:{}, locks:{}, _ts:{} };
function saveMeta(){}
function pushMeta(key, value){ __pushed.push({key, value: JSON.parse(JSON.stringify(value))}); }
function api(path, body){ __api.push({path, body: JSON.parse(JSON.stringify(body||{}))}); return Promise.resolve({}); }
function apiKeys(){ return __api.filter(c => c.path === '/api/meta_event').map(c => c.body.key); }
"""


def run(functions: List[str], script: str, extra_prelude: str = "") -> Dict:
    """Extract `functions` from the shipped UI, run `script` after them under node, and
    return whatever the script assigns to `result`."""
    if not node_available():
        raise RuntimeError("node is not on PATH")
    body = [PRELUDE, extra_prelude]
    body += [extract_function(f) for f in functions]
    body.append(script)
    body.append("\nconsole.log('__RESULT__' + JSON.stringify(result));\n")
    with tempfile.TemporaryDirectory() as td:
        path = os.path.join(td, "case.mjs")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write("\n".join(body))
        proc = subprocess.run(["node", path], capture_output=True, text=True, timeout=30)
    if proc.returncode != 0:
        raise RuntimeError(f"node failed:\n{proc.stdout}\n{proc.stderr}")
    for line in proc.stdout.splitlines():
        if line.startswith("__RESULT__"):
            return json.loads(line[len("__RESULT__"):])
    raise RuntimeError(f"no result from node:\n{proc.stdout}\n{proc.stderr}")
