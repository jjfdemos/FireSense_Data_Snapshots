#!/usr/bin/env python3
"""Local edit server for the FireSense_Data_Snapshots page.

Serves index.html with the human-authored text elements (intro copy, card
titles/descriptions, section headings, org-guide labels, appendix labels,
footer) made editable in the browser, and saves edits back into the real
index.html in this working tree. The embedded ~932 KB JSON tree blob
(<script type="application/json" id="tree-data">, one single line) is never
part of any editable region, and every save verifies that line is
byte-identical before the file is written. Writes are atomic (temp file +
rename). Nothing is committed or pushed -- review with `git diff`.

Usage:
    python3 edit_server.py [index.html]

Env (defaults exposed per house scripting style):
    PORT=8765        port on 127.0.0.1
    NO_OPEN=0        set 1 to skip auto-opening the browser
"""

import hashlib
import html.entities
import json
import os
import re
import subprocess
import sys
import tempfile
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
TARGET = os.path.abspath(sys.argv[1]) if len(sys.argv) > 1 else os.path.join(HERE, "index.html")
PORT = int(os.environ.get("PORT", "8765"))
NO_OPEN = os.environ.get("NO_OPEN", "0") == "1"

BLOB_MARK = '<script type="application/json" id="tree-data">'

# Whitelist of editable elements. Each regex has exactly one group: the
# element's inner HTML, which must directly follow a ">" so the data-eid
# attribute can be injected into the start tag right before it.
PATTERNS = [
    r'<p class="eyebrow">(.*?)</p>',
    r'<h1>(.*?)</h1>',
    r'<p class="path-line">(.*?)</p>',
    r'<p class="lead">(.*?)</p>',
    r'<span class="section-title">(.*?)</span>',
    r'<h3>(.*?)</h3>',
    r'<p>(.*?)</p>',                                   # the six card descriptions
    r'<span class="col-meta">(.*?)</span>',
    r'<div class="org-path mono">(.*?)</div>',
    r'<span class="seg mono">(.*?)</span>',
    r'<span class="why">(.*?)</span>',
    r'<p class="section-note"[^>]*>(.*?)</p>',
    r'<li><span>([^<]*)</span>(?=<span class="code mono">)',   # region names
    r'<span class="code mono">(.*?)</span>',
    r'<summary>(.*?)</summary>',
    r'<span class="kpi-label">(.*?)</span>',
    r'<span class="kpi-value">(.*?)</span>',
    r'<span class="kpi-sub">(.*?)</span>',
    r'<div class="cat-name">(.*?)</div>',
    r'<span class="cat-desc">(.*?)</span>',
    r'<th[^>]*>(.*?)</th>',
    r'<td class="num">(.*?)</td>',
    r'<td class="lbl">(.*?)</td>',
    r'<td class="n">(.*?)</td>',
    r'<span class="highlight-num">(.*?)</span>',
    r'<span class="highlight-lbl">(.*?)</span>',
    r'<footer>(.*?)</footer>',
]


def read_page():
    with open(TARGET, encoding="utf-8") as f:
        return f.read()


def blob_line(text):
    """Return the full line holding the JSON blob (must exist exactly once)."""
    for line in text.split("\n"):
        if BLOB_MARK in line:
            return line
    raise RuntimeError("tree-data blob line not found in %s" % TARGET)


def scan(text):
    """Find editable spans. Returns list of (start, end) inner-HTML offsets,
    sorted, with any span nested inside another dropped, and none inside
    the blob line."""
    bstart = text.index(BLOB_MARK)
    bend = text.index("\n", bstart)
    spans = []
    for pat in PATTERNS:
        for m in re.finditer(pat, text, re.DOTALL):
            s, e = m.span(1)
            if s >= bstart and s < bend:
                continue  # never inside the blob line (defensive; none match)
            spans.append((s, e))
    spans.sort()
    keep = []
    for s, e in spans:
        if keep and s < keep[-1][1]:      # nested/overlapping: keep the outer
            continue
        keep.append((s, e))
    return keep


def digest(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def encode_entities(s):
    """Encode non-ASCII chars as named (or numeric) entities to match the
    file's pure-ASCII house style."""
    out = []
    for ch in s:
        cp = ord(ch)
        if cp < 128:
            out.append(ch)
        elif cp in html.entities.codepoint2name:
            out.append("&%s;" % html.entities.codepoint2name[cp])
        else:
            out.append("&#x%X;" % cp)
    return "".join(out)


EDITOR = r"""
<style id="fs-edit-style">
  [data-eid]{outline:1px dashed rgba(120,160,255,.55);outline-offset:2px;min-height:1em;}
  [data-eid]:hover{outline-color:rgba(120,160,255,.95);background:rgba(120,160,255,.07);}
  [data-eid]:focus{outline:2px solid rgba(120,160,255,1);background:rgba(120,160,255,.10);}
  [data-eid].fs-dirty{outline-color:rgba(255,170,60,.95);}
  #fs-bar{position:fixed;left:0;right:0;bottom:0;z-index:99999;display:flex;gap:12px;
    align-items:center;padding:10px 18px;background:#101828;color:#e6ecf5;
    font:13px/1.4 -apple-system,system-ui,sans-serif;box-shadow:0 -2px 12px rgba(0,0,0,.4);}
  #fs-bar b{color:#8ab4ff;} #fs-count{color:#ffc46b;}
  #fs-bar button{font:inherit;padding:5px 14px;border-radius:6px;border:1px solid #3a4a66;
    background:#1c2a44;color:#e6ecf5;cursor:pointer;}
  #fs-bar button:hover{background:#26375a;}
  #fs-bar #fs-save{background:#2456c4;border-color:#2456c4;font-weight:600;}
  #fs-bar #fs-save:hover{background:#2f66dd;}
  #fs-msg{margin-left:auto;color:#9fb0c8;}
  #fs-diff{position:fixed;inset:5% 8% 12% 8%;z-index:99998;background:#0b1220;color:#dbe4f0;
    border:1px solid #3a4a66;border-radius:10px;padding:16px;overflow:auto;display:none;
    white-space:pre;font:12px/1.5 ui-monospace,Menlo,monospace;}
  body{padding-bottom:60px !important;}
</style>
<div id="fs-bar">
  <b>EDIT MODE</b>
  <span>click outlined text to edit</span>
  <span id="fs-count">0 changed</span>
  <button id="fs-save" title="Cmd/Ctrl+S">Save to index.html</button>
  <button id="fs-discard">Discard (reload)</button>
  <button id="fs-diffbtn">View git diff</button>
  <span id="fs-msg"></span>
</div>
<pre id="fs-diff"></pre>
<script>
(function(){
  var base = document.documentElement.getAttribute('data-fs-hash');
  var els = Array.prototype.slice.call(document.querySelectorAll('[data-eid]'));
  var orig = {};
  els.forEach(function(el){
    orig[el.dataset.eid] = el.innerHTML;
    el.contentEditable = true; // rich edits allowed; inline markup like <b> preserved
    el.spellcheck = true;
    el.addEventListener('input', refresh);
  });
  // keep the appendix open and stop <summary> clicks from toggling it shut
  document.querySelectorAll('details').forEach(function(d){ d.open = true; });
  document.querySelectorAll('summary').forEach(function(s){
    s.addEventListener('click', function(e){ e.preventDefault(); });
  });
  function dirty(){
    return els.filter(function(el){ return el.innerHTML !== orig[el.dataset.eid]; });
  }
  function refresh(){
    var d = dirty();
    els.forEach(function(el){ el.classList.toggle('fs-dirty', el.innerHTML !== orig[el.dataset.eid]); });
    document.getElementById('fs-count').textContent = d.length + ' changed';
  }
  function msg(t){ document.getElementById('fs-msg').textContent = t; }
  function save(){
    var d = dirty();
    if(!d.length){ msg('nothing to save'); return; }
    var edits = {};
    d.forEach(function(el){ edits[el.dataset.eid] = el.innerHTML; });
    msg('saving…');
    fetch('/save', {method:'POST', headers:{'Content-Type':'application/json'},
      body: JSON.stringify({base: base, edits: edits})})
    .then(function(r){ return r.json().then(function(j){ return {ok:r.ok, j:j}; }); })
    .then(function(res){
      if(res.ok){ msg('saved ✓ reloading…'); setTimeout(function(){ location.reload(); }, 400); }
      else { msg('SAVE FAILED: ' + (res.j.error || 'unknown')); alert('Save failed:\n' + (res.j.error || 'unknown')); }
    })
    .catch(function(e){ msg('SAVE FAILED: ' + e); });
  }
  document.getElementById('fs-save').addEventListener('click', save);
  document.getElementById('fs-discard').addEventListener('click', function(){
    if(!dirty().length || confirm('Discard unsaved edits?')) location.reload();
  });
  document.getElementById('fs-diffbtn').addEventListener('click', function(){
    var box = document.getElementById('fs-diff');
    if(box.style.display === 'block'){ box.style.display = 'none'; return; }
    fetch('/diff').then(function(r){ return r.text(); }).then(function(t){
      box.textContent = t || '(working tree clean — no diff)';
      box.style.display = 'block';
    });
  });
  window.addEventListener('keydown', function(e){
    if((e.metaKey || e.ctrlKey) && e.key === 's'){ e.preventDefault(); save(); }
  });
  window.addEventListener('beforeunload', function(e){
    if(dirty().length){ e.preventDefault(); e.returnValue = ''; }
  });
})();
</script>
"""


def annotated_page():
    """Current file with data-eid attributes + editor UI injected. Also
    returns the content hash the client must echo back on save."""
    text = read_page()
    spans = scan(text)
    h = digest(text)
    out = []
    pos = 0
    for i, (s, e) in enumerate(spans):
        # inject into the start tag: the inner HTML directly follows its '>'
        assert text[s - 1] == ">", "span %d not preceded by '>'" % i
        out.append(text[pos:s - 1])
        out.append(' data-eid="%d">' % i)
        out.append(text[s:e])
        pos = e
    out.append(text[pos:])
    page = "".join(out)
    page = page.replace("<html", '<html data-fs-hash="%s"' % h, 1)
    page = page.replace("</body>", EDITOR + "\n</body>", 1)
    return page


def apply_edits(base_hash, edits):
    """Apply {eid: newInnerHTML} to the file. Atomic, blob-guarded."""
    text = read_page()
    if digest(text) != base_hash:
        raise ValueError("index.html changed on disk since this page was loaded -- reload the browser tab and redo the edits")
    spans = scan(text)
    blob_before = blob_line(text)
    changes = []
    for k, v in edits.items():
        i = int(k)
        if not (0 <= i < len(spans)):
            raise ValueError("unknown element id %s -- reload the page" % k)
        if re.search(r"<\s*/?\s*script", v, re.I):
            raise ValueError("edit for element %s contains a <script> tag -- refused" % k)
        changes.append((spans[i][0], spans[i][1], encode_entities(v)))
    changes.sort(reverse=True)
    for s, e, new in changes:
        text = text[:s] + new + text[e:]
    if blob_line(text) != blob_before:
        raise RuntimeError("refusing to save: the embedded tree-data JSON line would change")
    scan(text)  # must still parse cleanly for the next round-trip
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(TARGET), prefix=".fs-edit-")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
        os.replace(tmp, TARGET)
    except BaseException:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise
    return len(changes)


class Handler(BaseHTTPRequestHandler):
    def _send(self, code, body, ctype="text/html; charset=utf-8"):
        data = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        path = self.path.split("?")[0]
        if path in ("/", "/index.html"):
            self._send(200, annotated_page())
        elif path == "/diff":
            try:
                out = subprocess.run(
                    ["git", "diff", "--", os.path.basename(TARGET)],
                    cwd=os.path.dirname(TARGET), capture_output=True, text=True, timeout=15,
                ).stdout
            except Exception as e:  # git optional for the UI
                out = "git diff unavailable: %s" % e
            self._send(200, out, "text/plain; charset=utf-8")
        else:
            self._send(404, "not found", "text/plain")

    def do_POST(self):
        if self.path != "/save":
            self._send(404, json.dumps({"error": "not found"}), "application/json")
            return
        try:
            n = int(self.headers.get("Content-Length", "0"))
            req = json.loads(self.rfile.read(n).decode("utf-8"))
            count = apply_edits(req["base"], req["edits"])
            self._send(200, json.dumps({"ok": True, "applied": count}), "application/json")
            print("saved %d edit(s) -> %s" % (count, TARGET))
        except Exception as e:
            self._send(409, json.dumps({"error": str(e)}), "application/json")
            print("save rejected: %s" % e)

    def log_message(self, *a):
        pass


def main():
    text = read_page()
    blob_line(text)  # fail fast if the blob is missing
    n = len(scan(text))
    branch = "?"
    try:
        branch = subprocess.run(
            ["git", "rev-parse", "--abbrev-ref", "HEAD"],
            cwd=os.path.dirname(TARGET), capture_output=True, text=True, timeout=10,
        ).stdout.strip() or "?"
    except Exception:
        pass
    url = "http://127.0.0.1:%d/" % PORT
    print("FireSense snapshot page editor")
    print("  file    : %s" % TARGET)
    print("  branch  : %s" % branch)
    print("  editable: %d elements" % n)
    print("  url     : %s" % url)
    print("Edit outlined text in the browser, then 'Save to index.html'.")
    print("Review with: git diff   (nothing is committed or pushed)")
    print("Stop with Ctrl-C.")
    srv = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    if not NO_OPEN:
        webbrowser.open(url)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\nbye")


if __name__ == "__main__":
    main()
