#!/usr/bin/env python3
"""
pyleup.py — produce an HTML report where:
  • Each ROW is a function call (indented by call depth).
  • Optionally sample executed lines.
  • Click a function/line to view the memory snapshot at that moment (locals + tracemalloc).

Defaults avoid slowdowns:
  • Only traces files under the target script’s directory (not stdlib/imports).
  • Snapshots on CALL/RETURN only (fast). Use --lines N for sampled line snapshots.

Examples:
  python3 pyleup.py -- python3 your_script.py
  python3 pyleup.py --lines 20 -- python3 your_script.py
  python3 pyleup.py --include-stdlib --heap-stats -- python3 your_script.py
"""

import argparse
import html
import json
import os
import runpy
import sys
import sysconfig
import time
import tracemalloc
from types import FrameType

# ------------------------- helpers -------------------------

def is_stdlib(path: str) -> bool:
    if not path:
        return True
    p = sysconfig.get_paths()
    std = p.get("stdlib", "") or ""
    plat = p.get("platstdlib", "") or ""
    ap = os.path.abspath(path)
    return ap.startswith(std + os.sep) or ap.startswith(plat + os.sep)

def esc(x) -> str:
    return html.escape(str(x), quote=True)

def safe_repr(val, maxlen=200) -> str:
    try:
        s = repr(val)
    except Exception:
        return f"<unrepr {type(val).__name__}>"
    if len(s) > maxlen:
        s = s[:maxlen] + "…"
    return s

# ------------------------- main -------------------------

def main():
    ap = argparse.ArgumentParser(
        description="Trace Python calls/lines and inspect memory snapshots in an HTML report."
    )
    ap.add_argument("--include-stdlib", action="store_true",
                    help="Also trace frames in the Python stdlib (can be noisy/slow).")
    ap.add_argument("--lines", type=int, default=0,
                    help="Sample every Nth executed line within traced files (0 = off).")
    ap.add_argument("--heap-stats", action="store_true",
                    help="Include tracemalloc top allocation sites (heavier).")
    ap.add_argument("--out", default="pyleup_report.html",
                    help="Path to write the HTML report.")
    ap.add_argument("rest", nargs=argparse.REMAINDER,
                    help="Target script + args (e.g., test.py arg1 arg2)")
    args = ap.parse_args()


    if not args.rest:
        print("Usage: python3 pyleup.py -- python3 your_script.py [args...]")
        sys.exit(2)

    # Target script & root
    prog, *prog_args = args.rest
    prog_abs = os.path.abspath(prog)
    if not os.path.exists(prog_abs):
        print(f"Target script '{prog}' not found.")
        sys.exit(1)
    target_root = os.path.dirname(prog_abs)

    def want(path: str) -> bool:
        """Decide if this file should be traced at all."""
        if not path:
            return False
        apath = os.path.abspath(path)
        # Only files under the target script's directory (or the script itself)
        if not (apath == prog_abs or apath.startswith(target_root + os.sep)):
            return False
        if (not args.include_stdlib) and is_stdlib(apath):
            return False
        return True

    # Data structures
    class Node:
        __slots__ = ("id","parent","name","file","first_line","depth","start_ns","end_ns","lines")
        def __init__(self, id, parent, name, file, first_line, depth, start_ns):
            self.id = id
            self.parent = parent  # Node | None
            self.name = name
            self.file = file
            self.first_line = first_line
            self.depth = depth
            self.start_ns = start_ns
            self.end_ns = None
            self.lines = []  # list of (label, snap_index)

    nodes = []
    stack = []
    next_id = 1
    line_counter = 0
    snaps = []  # embedded snapshots (dicts) rendered directly in HTML

    tracemalloc.start()

    def take_snapshot(kind: str, frame: FrameType, node_id: int, lineno: int):
        """Capture a lightweight snapshot: locals + tracemalloc current/peak (+ optional top stats)."""
        current, peak = tracemalloc.get_traced_memory()
        payload = {
            "ts": time.time(),
            "kind": kind,           # "call" | "line" | "return"
            "node_id": node_id,
            "file": frame.f_code.co_filename,
            "lineno": lineno,
            "func": frame.f_code.co_name,
            "locals": {k: safe_repr(v) for k, v in frame.f_locals.items()},
            "tracemalloc_current": current,
            "tracemalloc_peak": peak,
        }
        if args.heap_stats:
            try:
                snap = tracemalloc.take_snapshot()
                top = snap.statistics('lineno')[:20]
                payload["tracemalloc_top"] = [
                    {
                        "file": str(s.traceback[0].filename),
                        "line": s.traceback[0].lineno,
                        "size": s.size,
                        "count": s.count,
                    }
                    for s in top
                    if s.traceback
                ]
            except Exception as e:
                payload["tracemalloc_top_error"] = safe_repr(e)

        snaps.append(payload)
        return len(snaps) - 1  # index

    def tracer(frame: FrameType, event: str, arg):
        nonlocal next_id, line_counter
        fpath = frame.f_code.co_filename

        # IMPORTANT: return None for uninteresting frames so they are not traced at all
        if not want(fpath):
            return None

        if event == "call":
            n = Node(
                next_id,
                stack[-1] if stack else None,
                frame.f_code.co_name,
                fpath,
                frame.f_code.co_firstlineno,
                len(stack),
                time.perf_counter_ns(),
            )
            next_id += 1
            if stack:
                stack[-1].lines.append(("__child__", None))  # visual spacer
            nodes.append(n)
            stack.append(n)
            idx = take_snapshot("call", frame, n.id, n.first_line)
            n.lines.append((f"def@{n.first_line}", idx))
            return tracer

        if event == "line":
            if stack and args.lines > 0:
                line_counter += 1
                if (line_counter % args.lines) == 0:
                    idx = take_snapshot("line", frame, stack[-1].id, frame.f_lineno)
                    stack[-1].lines.append((frame.f_lineno, idx))
            return tracer  # keep tracing this frame

        if event == "return":
            if stack:
                idx = take_snapshot("return", frame, stack[-1].id, frame.f_lineno)
                stack[-1].end_ns = time.perf_counter_ns()
                stack[-1].lines.append(("return", idx))
                stack.pop()
            return tracer

        return tracer

    # Run the target script with tracing enabled
    sys.settrace(tracer)
    try:
        sys.argv = [prog] + prog_args
        runpy.run_path(prog, run_name="__main__")
    finally:
        sys.settrace(None)
        tracemalloc.stop()

    # Prepare rows for HTML
    rows = []
    for n in nodes:
        dur_ms = ((n.end_ns or n.start_ns) - n.start_ns) / 1e6
        rows.append({
            "id": n.id,
            "name": n.name,
            "file": n.file,
            "first_line": n.first_line,
            "depth": n.depth,
            "dur_ms": dur_ms,
            "lines": n.lines,  # (label, snap_index)
        })

    # Emit a single self-contained HTML
    html_doc = f"""<!doctype html><meta charset="utf-8">
<title>MemTrace Report</title>
<style>
body{{font:14px system-ui,Segoe UI,Roboto,Helvetica,Arial;display:grid;grid-template-columns:1fr 420px;gap:12px;margin:16px}}
table{{border-collapse:collapse;width:100%}}
th,td{{border-bottom:1px solid #eee;padding:6px 8px;vertical-align:top}}
tr:hover{{background:#fafafa}}
.ind{{display:inline-block}}
.badge{{background:#eee;border-radius:8px;padding:0 6px;margin-left:6px;font-size:12px}}
.lines{{background:#f8f9fb;border-left:3px solid #e1e5ea;margin:6px 0;padding:6px 10px;font-family:ui-monospace,monospace}}
#side{{border-left:1px solid #eee;padding-left:10px;overflow:auto;max-height:90vh}}
pre{{white-space:pre-wrap}}
small{{color:#666}}
h1{{margin:0 0 6px 0}}
</style>

<h1>MemTrace</h1>
<p><small>Rows = function calls. Click a line tag to load the snapshot →</small></p>

<div>
  <table>
    <thead><tr><th>Call</th><th>File:Line</th><th>Duration (ms)</th></tr></thead>
    <tbody id="tbody"></tbody>
  </table>
</div>

<aside id="side">
  <h2>Snapshot</h2>
  <div id="meta"><small>Click a function or line…</small></div>
  <pre id="dump"></pre>
</aside>

<script>
const rows = {json.dumps(rows)};
const snaps = {json.dumps(snaps)};

function el(tag, attrs={{}}, html=""){{
  const e=document.createElement(tag);
  for(const k in attrs) e.setAttribute(k, attrs[k]);
  if(html) e.innerHTML = html;
  return e;
}}

function loadSnap(i){{
  if(i==null || i<0 || i>=snaps.length){{
    document.getElementById('meta').innerHTML="<small>No snapshot.</small>";
    document.getElementById('dump').textContent="";
    return;
  }}
  const j = snaps[i];
  document.getElementById('meta').innerHTML =
    "<b>"+j.func+"</b> at "+j.file+":"+j.lineno+"<br><small>"+new Date(j.ts*1000).toISOString()+"</small>";
  document.getElementById('dump').textContent = JSON.stringify(j, null, 2);
}}

const tbody = document.getElementById('tbody');

rows.forEach(n => {{
  const tr = el('tr', {{class:'call'}});
  tr.innerHTML = "<td><span class='ind' style='width:"+(n.depth*16)+"px'></span><b>"+n.name+
                 "</b><span class='badge'>depth "+n.depth+
                 "</span></td><td>"+n.file+":"+n.first_line+
                 "</td><td>"+n.dur_ms.toFixed(3)+"</td>";
  tbody.appendChild(tr);

  const lr = el('tr');
  const td = el('td', {{colspan:"3"}});
  const box = el('div', {{class:'lines'}});
  if(n.lines.length === 0) {{
    box.appendChild(el('div', {{}}, "<small>No events recorded.</small>"));
  }} else {{
    n.lines.forEach(([label, idx], k) => {{
      const text = (typeof label === "number") ? ("line " + label) : String(label);
      const a = el('a', {{href:"#"}}, text);
      a.onclick = (ev) => {{ ev.preventDefault(); loadSnap(idx); }};
      box.appendChild(a);
      if (k < n.lines.length-1) box.appendChild(el('span', {{}}, " · "));
    }});
  }}
  td.appendChild(box); lr.appendChild(td); tbody.appendChild(lr);
}});
</script>
"""

    with open(args.out, "w", encoding="utf-8") as f:
        f.write(html_doc)

    print(f"Wrote {args.out}")

if __name__ == "__main__":
    main()

