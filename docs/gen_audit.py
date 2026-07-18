#!/usr/bin/env python3
"""Generate the nightly-fbtriton audit HTML report.

Inputs:
  /tmp/audit.tsv     per-commit 4-check status (1-week lookback)
Output:
  docs/nightly-fbtriton-audit.html
"""
import csv, html

BASE = "3.8.0"  # nightly dev series base (next unreleased; main literal is 3.7.0, latest PyPI release 3.7.2)
RETAIN = "30 nightlies"  # prune window (tunable)
MATRIX = "cp312 + cp313 × x86_64 + aarch64"  # shrunk nightly ABI matrix (tunable; formal keeps full 10)

# ─────────────────────────── lookback table ───────────────────────────
ROWS = []
with open("/tmp/audit.tsv") as f:
    rr = csv.reader(f, delimiter="\t")
    next(rr)
    for row in rr:
        if len(row) < 7:
            continue
        sha, date, msg, lit, h100, mi350, b200 = row[:7]
        ROWS.append(dict(sha=sha, date=date, msg=msg, lit=lit, h100=h100, mi350=mi350, b200=b200))

SELECTED = None
for row in ROWS:
    if all(row[k] == "success" for k in ("lit", "h100", "mi350", "b200")):
        SELECTED = row["sha"]
        break


def cell(v):
    cls = {"success": "ok", "failure": "bad", "cancelled": "warn", "—": "na"}.get(v, "na")
    label = {"success": "pass", "failure": "fail", "cancelled": "cxl", "—": "—"}.get(v, v)
    return f'<td class="c {cls}">{label}</td>'


trs = []
for row in ROWS:
    green = all(row[k] == "success" for k in ("lit", "h100", "mi350", "b200"))
    sel = row["sha"] == SELECTED
    rowcls = "sel" if sel else ("green" if green else "")
    short = row["date"][5:16].replace("T", " ")
    badge = ' <span class="pick">◀ selected</span>' if sel else ""
    trs.append(f'<tr class="{rowcls}">'
               f'<td class="sha">{html.escape(row["sha"])}</td>'
               f'<td class="dt">{short}</td>'
               f'<td class="msg">{html.escape(row["msg"])}{badge}</td>'
               f'{cell(row["lit"])}{cell(row["h100"])}{cell(row["mi350"])}{cell(row["b200"])}'
               f'</tr>')
table_body = "\n".join(trs)
n_total = len(ROWS)
n_green = sum(1 for row in ROWS if all(row[k] == "success" for k in ("lit", "h100", "mi350", "b200")))

# ─────────────────── point-in-time nightly pick simulation ───────────────────
# Reads per-check completion timestamps so a check counts as green for a given
# cut time T only if a passing run had COMPLETED by T (later/queued signals that
# only landed after T are invisible — exactly what the nightly would have seen).
from datetime import datetime


def _dt(s):
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


SIG = {}  # (short_sha, check_name) -> list of (completed_at_iso, conclusion, event)
try:
    with open("/tmp/sigtimes.tsv") as f:
        for line in f:
            parts = line.rstrip("\n").split("\t")
            if len(parts) < 5:
                continue
            s, ck, comp, concl, event = parts[:5]
            SIG.setdefault((s, ck), []).append((comp, concl, event))
except FileNotFoundError:
    pass

CHECKS = [("LIT", "LIT Tests"), ("h100", "h100-tlx-test"), ("mi350", "mi350-tlx-test"), ("b200", "b200-tlx-test")]
SRC = {"push": "p", "schedule": "c", "workflow_dispatch": "d", "dynamic": "p"}
SRC_LONG = {"p": "push", "c": "cron", "d": "dispatch", "?": "?"}


def cell_info(sha, name, T):
    """(label, src_letter, tooltip) as of cut T. Rule: use the LATEST genuine
    verdict (a rerun supersedes earlier runs). cancelled != a verdict — it's
    ignored (the real run may still be queued)."""
    runs = [(c, x, e) for (c, x, e) in SIG.get((sha, name), []) if c <= T]
    verdicts = sorted((r for r in runs if r[1] in ("success", "failure", "timed_out")))
    if verdicts:
        c, x, e = verdicts[-1]  # latest completed verdict
        if x == "success":
            return "pass", SRC.get(e, "?"), f"latest verdict: success via {e} @ {c}"
        return "fail", SRC.get(e, "?"), f"latest verdict: {x} via {e} @ {c}"
    cxl = sorted(r for r in runs if r[1] == "cancelled")
    if cxl:
        c, x, e = cxl[-1]
        return "cxl", SRC.get(e, "?"), f"cancelled via {e} @ {c}"
    return "—", "", "no run completed by cut"


def simulate(T):
    walk, pick = [], None
    for row in ROWS:  # ROWS is newest-first
        if row["date"] > T:  # commit didn't exist at cut time
            continue
        cells = {k: cell_info(row["sha"], name, T) for k, name in CHECKS}
        ag = all(cells[k][0] == "pass" for k, _ in CHECKS)
        walk.append((row["sha"], row["date"], row["msg"], cells, ag))
        if ag:
            pick = row["sha"]
            break
    return walk, pick


def render_sim(title, T):
    walk, pick = simulate(T)
    cmap = {"pass": "ok", "fail": "bad", "cxl": "warn", "—": "na"}
    trs_ = []
    for sha, date, msg, cells, ag in walk:
        tds = ""
        for k, _ in CHECKS:
            lab, src, tip = cells[k]
            sup = f'<sup>{src}</sup>' if src else ''
            tds += f'<td class="c {cmap[lab]}" title="{tip}">{lab}{sup}</td>'
        tag = ' <span class="pick">◀ pick</span>' if ag else ''
        trs_.append(f'<tr class="{"simpick" if ag else "simskip"}">'
                    f'<td class="sha">{sha}</td><td class="dt">{date[5:16].replace("T"," ")}</td>'
                    f'{tds}<td class="mm">{html.escape(msg[:26])}{tag}</td></tr>')
    if pick:
        pdate = next(r["date"] for r in ROWS if r["sha"] == pick)
        hrs = (_dt(T) - _dt(pdate)).total_seconds() / 3600
        pc = {k: cell_info(pick, name, T) for k, name in CHECKS}
        srcs = " · ".join(f"{k}&larr;<b>{SRC_LONG.get(pc[k][1], '?')}</b>" for k, _ in CHECKS)
        summary = (f'<div class="simsum ok">✓ picks <b>{pick}</b> · committed '
                   f'<b>{hrs:.0f} h</b> before the cut · walked {len(walk)}</div>'
                   f'<div class="srcline">green from: {srcs}</div>')
    else:
        summary = (f'<div class="simsum bad">⚑ no all-green commit as of this cut → '
                   f'would file a tracking issue · walked {len(walk)}</div>')
    return (f'<div class="simcol"><div class="simhdr">{title}</div>{summary}'
            f'<table class="simtab"><thead><tr><th>commit</th><th>committed</th>'
            f'<th>LIT</th><th>h100</th><th>mi350</th><th>b200</th><th>subject</th></tr></thead>'
            f'<tbody>{"".join(trs_)}</tbody></table></div>')


SIM_HTML = (render_sim("Nightly run @ 2026-07-17 04:00 UTC", "2026-07-17T04:00:00Z") +
            render_sim("Nightly run @ 2026-07-16 04:00 UTC", "2026-07-16T04:00:00Z"))

# ─────────────────────────── flow SVG (with annotation) ───────────────────────────
FLOW_SVG = r'''<svg id="flow-svg" width="100%" viewBox="0 0 1180 880" style="display:block;font-family:Inter,sans-serif">
<defs>
  <marker id="a" markerWidth="9" markerHeight="7" refX="7" refY="3" orient="auto"><polygon points="0 0,8 3,0 6" fill="#94a3b8"/></marker>
  <marker id="ag" markerWidth="9" markerHeight="7" refX="7" refY="3" orient="auto"><polygon points="0 0,8 3,0 6" fill="#16a34a"/></marker>
  <marker id="ar" markerWidth="9" markerHeight="7" refX="7" refY="3" orient="auto"><polygon points="0 0,8 3,0 6" fill="#dc2626"/></marker>
  <filter id="sh"><feDropShadow dx="0" dy="1" stdDeviation="3" flood-color="#0f172a" flood-opacity="0.08"/></filter>
</defs>
<g filter="url(#sh)"><rect x="330" y="16" width="360" height="52" rx="9" fill="#fffbeb" stroke="#fbbf24"/></g>
<text x="510" y="38" text-anchor="middle" font-size="13" font-weight="700" fill="#92400e">⏱ Scheduled trigger — cron 04:00 UTC daily</text>
<text x="510" y="55" text-anchor="middle" font-size="10" fill="#92400e">nightly-fbtriton.yml · also workflow_dispatch · guarded to facebookexperimental</text>
<g filter="url(#sh)"><rect x="330" y="100" width="360" height="52" rx="9" fill="#eff6ff" stroke="#3b82f6"/></g>
<text x="510" y="122" text-anchor="middle" font-size="12" font-weight="700" fill="#1e40af">① Read last-shipped commit</text>
<text x="510" y="139" text-anchor="middle" font-size="10" fill="#1e40af">nightly index → latest.json (commit + date)</text>
<g filter="url(#sh)"><rect x="330" y="184" width="360" height="52" rx="9" fill="#eff6ff" stroke="#3b82f6"/></g>
<text x="510" y="206" text-anchor="middle" font-size="12" font-weight="700" fill="#1e40af">② Enumerate NEW commits on main since last release</text>
<text x="510" y="223" text-anchor="middle" font-size="10" fill="#1e40af">newest → oldest (safety cap ~100 commits)</text>
<polygon points="510,264 610,312 510,360 410,312" fill="#f9fafb" stroke="#d1d5db" filter="url(#sh)"/>
<text x="510" y="308" text-anchor="middle" font-size="12" font-weight="700" fill="#374151">any new</text>
<text x="510" y="324" text-anchor="middle" font-size="12" font-weight="700" fill="#374151">commits?</text>
<g filter="url(#sh)"><rect x="800" y="286" width="330" height="52" rx="9" fill="#f9fafb" stroke="#9ca3af"/></g>
<text x="965" y="308" text-anchor="middle" font-size="12" font-weight="700" fill="#374151">■ Exit silently</text>
<text x="965" y="325" text-anchor="middle" font-size="10" fill="#374151">no tag · no wheel · no issue</text>
<g filter="url(#sh)"><rect x="330" y="392" width="360" height="52" rx="9" fill="#f0fdf4" stroke="#22c55e"/></g>
<text x="510" y="414" text-anchor="middle" font-size="12" font-weight="700" fill="#15803d">③ Take next commit (newest first)</text>
<text x="510" y="431" text-anchor="middle" font-size="10" fill="#15803d">query check-runs API · dedupe to latest run per check</text>
<polygon points="510,472 640,532 510,592 380,532" fill="#f9fafb" stroke="#d1d5db" filter="url(#sh)"/>
<text x="510" y="522" text-anchor="middle" font-size="11" font-weight="700" fill="#374151">all 4 required</text>
<text x="510" y="537" text-anchor="middle" font-size="11" font-weight="700" fill="#374151">checks green?</text>
<text x="510" y="553" text-anchor="middle" font-size="8.5" fill="#656d76">LIT·h100·mi350·b200</text>
<path d="M 380 532 L 250 532 L 250 418 L 330 418" fill="none" stroke="#dc2626" stroke-width="1.6" stroke-dasharray="5,3" marker-end="url(#ar)"/>
<text x="250" y="478" text-anchor="middle" font-size="10" font-weight="700" fill="#dc2626">No →</text>
<text x="250" y="492" text-anchor="middle" font-size="10" font-weight="700" fill="#dc2626">next</text>
<g filter="url(#sh)"><rect x="800" y="506" width="330" height="60" rx="9" fill="#fef2f2" stroke="#dc2626"/></g>
<text x="965" y="528" text-anchor="middle" font-size="12" font-weight="700" fill="#dc2626">⚑ None green → file tracking issue</text>
<text x="965" y="545" text-anchor="middle" font-size="10" fill="#b91c1c">reuse report-nightly-failure.yml</text>
<text x="965" y="559" text-anchor="middle" font-size="10" fill="#b91c1c">(dedup by title · labels: nightly)</text>
<g filter="url(#sh)"><rect x="330" y="624" width="360" height="66" rx="9" fill="#e0f2fe" stroke="#7dd3fc"/></g>
<text x="510" y="645" text-anchor="middle" font-size="12" font-weight="700" fill="#0369a1">④ Build wheels — wheels_fb.yml (ref, version, subset matrix)</text>
<text x="510" y="662" text-anchor="middle" font-size="10" fill="#0369a1">checkout SHA · wheel ver 3.8.0.dev&lt;date&gt; (clean)</text>
<text x="510" y="677" text-anchor="middle" font-size="10" fill="#0369a1">rewrite __init__.py __version__ = 3.8.0.dev&lt;date&gt;+fb.git&lt;hash&gt;</text>
<g filter="url(#sh)"><rect x="330" y="718" width="360" height="60" rx="9" fill="#e0f2fe" stroke="#7dd3fc"/></g>
<text x="510" y="738" text-anchor="middle" font-size="12" font-weight="700" fill="#0369a1">⑤ Upload to rolling 'nightly' Release + refresh Pages index</text>
<text x="510" y="754" text-anchor="middle" font-size="10" fill="#0369a1">write latest.json · prune assets &gt; 30 days</text>
<text x="510" y="768" text-anchor="middle" font-size="10" fill="#0369a1">GITHUB_TOKEN only (no PAT)</text>
<g filter="url(#sh)"><rect x="380" y="806" width="260" height="46" rx="9" fill="#f0fdf4" stroke="#16a34a"/></g>
<text x="510" y="834" text-anchor="middle" font-size="13" font-weight="800" fill="#15803d">✓ Nightly .dev wheel live</text>
<path d="M 510 68 L 510 98" fill="none" stroke="#94a3b8" stroke-width="1.6" marker-end="url(#a)"/>
<path d="M 510 152 L 510 182" fill="none" stroke="#94a3b8" stroke-width="1.6" marker-end="url(#a)"/>
<path d="M 510 236 L 510 262" fill="none" stroke="#94a3b8" stroke-width="1.6" marker-end="url(#a)"/>
<path d="M 610 312 L 798 312" fill="none" stroke="#94a3b8" stroke-width="1.6" marker-end="url(#a)"/>
<text x="700" y="304" text-anchor="middle" font-size="10" font-weight="700" fill="#656d76">No</text>
<path d="M 510 360 L 510 390" fill="none" stroke="#16a34a" stroke-width="1.6" marker-end="url(#ag)"/>
<text x="524" y="380" font-size="10" font-weight="700" fill="#15803d">Yes</text>
<path d="M 510 444 L 510 470" fill="none" stroke="#94a3b8" stroke-width="1.6" marker-end="url(#a)"/>
<path d="M 640 532 L 798 532" fill="none" stroke="#dc2626" stroke-width="1.6" marker-end="url(#ar)"/>
<text x="720" y="524" text-anchor="middle" font-size="10" font-weight="700" fill="#dc2626">commits exhausted</text>
<path d="M 510 592 L 510 622" fill="none" stroke="#16a34a" stroke-width="1.6" marker-end="url(#ag)"/>
<text x="524" y="612" font-size="10" font-weight="700" fill="#15803d">Yes</text>
<path d="M 510 690 L 510 720" fill="none" stroke="#94a3b8" stroke-width="1.6" marker-end="url(#a)"/>
<path d="M 510 778 L 510 804" fill="none" stroke="#94a3b8" stroke-width="1.6" marker-end="url(#a)"/>
</svg>'''

COMMENT_JS = r'''
<script>
const svg = document.getElementById('flow-svg');
const VB_W = 1180, VB_H = 880;
let mode = 'none', comments = [], nextId = 1;
let dragStart = null, dragRect = null, dragMoved = false;
const ZONES = [
  ['Trigger', 330, 16, 690, 68],['Read PyPI last commit', 330, 100, 690, 152],
  ['Enumerate new commits', 330, 184, 690, 236],['Decision: any new commits', 410, 264, 610, 360],
  ['Exit silently', 800, 286, 1130, 338],['Walk / next commit', 330, 392, 690, 444],
  ['Decision: all 4 green', 380, 472, 640, 592],['File tracking issue', 800, 506, 1130, 566],
  ['Build wheels', 330, 624, 690, 690],['Publish PyPI', 330, 722, 690, 774],['Done', 380, 806, 640, 852],
];
function hitZone(x, y){let best=null,ba=Infinity;for(const[l,x1,y1,x2,y2]of ZONES){if(x>=x1&&x<=x2&&y>=y1&&y<=y2){const a=(x2-x1)*(y2-y1);if(a<ba){ba=a;best=l;}}}return best;}
function svgCoords(e){const r=svg.getBoundingClientRect();return{x:Math.round((e.clientX-r.left)*VB_W/r.width),y:Math.round((e.clientY-r.top)*VB_H/r.height)};}
function setMode(m){mode=m;const b=document.getElementById('btn-point'),s=document.getElementById('comment-status'),a=m!=='none';b.style.background=a?'#dbeafe':'#fff';b.style.borderColor=a?'#2563eb':'#d0d7de';b.style.color=a?'#1d4ed8':'#1f2328';svg.style.cursor=a?'crosshair':'';s.textContent=a?'Click to pin · drag to highlight.':'';}
svg.addEventListener('click',e=>{if(mode==='none'||dragMoved)return;const{x,y}=svgCoords(e),zone=hitZone(x,y),text=prompt('Comment:');if(!text?.trim())return;const id=nextId++;comments.push({id,x,y,zone,text:text.trim(),type:'point'});renderPin(id,x,y);updateStatus();});
svg.addEventListener('mousedown',e=>{if(mode==='none')return;dragStart=svgCoords(e);dragMoved=false;});
svg.addEventListener('mousemove',e=>{if(mode==='none'||!dragStart)return;dragMoved=true;const cur=svgCoords(e),ns='http://www.w3.org/2000/svg';if(!dragRect){dragRect=document.createElementNS(ns,'rect');dragRect.setAttribute('fill','rgba(34,197,94,0.1)');dragRect.setAttribute('stroke','#16a34a');dragRect.setAttribute('stroke-width','2');dragRect.setAttribute('stroke-dasharray','6,3');dragRect.style.pointerEvents='none';svg.appendChild(dragRect);}const rx=Math.min(dragStart.x,cur.x),ry=Math.min(dragStart.y,cur.y);dragRect.setAttribute('x',rx);dragRect.setAttribute('y',ry);dragRect.setAttribute('width',Math.abs(cur.x-dragStart.x));dragRect.setAttribute('height',Math.abs(cur.y-dragStart.y));});
svg.addEventListener('mouseup',e=>{if(mode==='none'||!dragStart||!dragMoved){dragStart=null;return;}const end=svgCoords(e);if(dragRect){dragRect.remove();dragRect=null;}const rx=Math.min(dragStart.x,end.x),ry=Math.min(dragStart.y,end.y),rw=Math.abs(end.x-dragStart.x),rh=Math.abs(end.y-dragStart.y);dragStart=null;if(rw<10||rh<10)return;const cx=Math.round(rx+rw/2),cy=Math.round(ry+rh/2),zone=hitZone(cx,cy),text=prompt('Comment on this area:');if(!text?.trim())return;const id=nextId++;comments.push({id,x:rx,y:ry,w:rw,h:rh,zone,text:text.trim(),type:'area'});renderArea(id,rx,ry,rw,rh);updateStatus();});
document.addEventListener('keydown',e=>{if(e.key==='Shift'&&mode==='none')setMode('point');});
document.addEventListener('keyup',e=>{if(e.key==='Shift'&&!dragStart)setMode('none');});
function renderPin(id,x,y){const ns='http://www.w3.org/2000/svg',g=document.createElementNS(ns,'g');g.id='ann-'+id;const c=document.createElementNS(ns,'circle');c.setAttribute('cx',x);c.setAttribute('cy',y);c.setAttribute('r',13);c.setAttribute('fill','#ef4444');c.setAttribute('stroke','#fff');c.setAttribute('stroke-width','2');c.style.cursor='pointer';c.addEventListener('click',ev=>{ev.stopPropagation();showComment(id);});const t=document.createElementNS(ns,'text');t.setAttribute('x',x);t.setAttribute('y',y+5);t.setAttribute('text-anchor','middle');t.setAttribute('font-size','11');t.setAttribute('font-weight','700');t.setAttribute('fill','#fff');t.style.pointerEvents='none';t.textContent=id;g.appendChild(c);g.appendChild(t);svg.appendChild(g);}
function renderArea(id,x,y,w,h){const ns='http://www.w3.org/2000/svg',g=document.createElementNS(ns,'g');g.id='ann-'+id;const r=document.createElementNS(ns,'rect');r.setAttribute('x',x);r.setAttribute('y',y);r.setAttribute('width',w);r.setAttribute('height',h);r.setAttribute('fill','rgba(239,68,68,0.08)');r.setAttribute('stroke','#ef4444');r.setAttribute('stroke-width','2');r.style.cursor='pointer';r.addEventListener('click',ev=>{ev.stopPropagation();showComment(id);});const t=document.createElementNS(ns,'text');t.setAttribute('x',x+4);t.setAttribute('y',y+14);t.setAttribute('font-size','11');t.setAttribute('font-weight','700');t.setAttribute('fill','#ef4444');t.style.pointerEvents='none';t.textContent='['+id+']';g.appendChild(r);g.appendChild(t);svg.appendChild(g);}
function showComment(id){const c=comments.find(c=>c.id===id);if(c)alert(`[${id}]${c.zone?' '+c.zone:''}\n\n${c.text}`);}
function updateStatus(){document.getElementById('comment-status').textContent=`${comments.length} comment${comments.length!==1?'s':''} added.`;}
function exportComments(){const box=document.getElementById('export-box'),area=document.getElementById('export-text-top');if(!comments.length){alert('No comments yet.');return;}const lines=comments.map(c=>{const loc=c.type==='area'?`area (${c.x},${c.y})→(${c.x+c.w},${c.y+c.h})`:`point (${c.x},${c.y})`;return`[${c.id}]${c.zone?' ['+c.zone+']':''} ${loc} — ${c.text}`;});const txt='--- Diagram Comments ---\n'+lines.join('\n')+'\n--- End ---';area.value=txt;box.style.display='block';if(navigator.clipboard)navigator.clipboard.writeText(txt).then(()=>{document.getElementById('comment-status').textContent=`${comments.length} copied to clipboard.`;}).catch(()=>{area.select();try{document.execCommand('copy');}catch(e){}});else{area.select();try{document.execCommand('copy');}catch(e){}}}
function clearComments(){comments.forEach(c=>{const el=document.getElementById('ann-'+c.id);if(el)el.remove();});comments=[];nextId=1;document.getElementById('comment-status').textContent='';document.getElementById('export-box').style.display='none';setMode('none');}
</script>'''

# ─────────────────────────── assemble HTML ───────────────────────────
CSS = """
  :root{--ok:#16a34a;--okbg:#f0fdf4;--bad:#dc2626;--badbg:#fef2f2;--warn:#d97706;
    --warnbg:#fffbeb;--na:#9ca3af;--nabg:#f9fafb;--ink:#1f2328;--muted:#656d76;
    --line:#d0d7de;--accent:#1d4ed8;}
  *{box-sizing:border-box;}
  body{font-family:"Inter",-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;
    background:#f3f4f6;color:var(--ink);margin:0;padding:28px;line-height:1.5;}
  .wrap{max-width:1320px;margin:0 auto;}
  h1{font-size:24px;font-weight:800;letter-spacing:-0.4px;margin:0 0 4px;}
  h2{font-size:16px;font-weight:700;letter-spacing:-0.2px;margin:0 0 12px;
    padding-bottom:6px;border-bottom:2px solid var(--line);}
  .sub{color:var(--muted);font-size:13px;margin:0 0 20px;}
  .panel{background:#fff;border:1px solid var(--line);border-radius:12px;padding:20px 22px;
    margin-bottom:20px;box-shadow:0 1px 3px rgba(15,23,42,.06);}
  .subh{font-size:14px;font-weight:700;margin:22px 0 8px;color:var(--ink);}
  .keycard{background:#eff6ff;border:1px solid #bfdbfe;border-left:4px solid var(--accent);
    border-radius:12px;padding:16px 20px;margin-bottom:20px;box-shadow:0 1px 3px rgba(15,23,42,.06);}
  .keyhdr{font-size:15px;font-weight:800;letter-spacing:-0.2px;color:var(--accent);margin-bottom:8px;}
  .keycard p{font-size:13.5px;color:var(--ink);}
  p{margin:0 0 10px;font-size:13.5px;}
  code{background:#f6f8fa;border:1px solid #e2e8f0;border-radius:5px;padding:1px 5px;
    font-size:12px;font-family:ui-monospace,SFMono-Regular,Menlo,monospace;}
  ul,ol{margin:6px 0 10px;padding-left:22px;font-size:13.5px;}
  li{margin:5px 0;}
  ol.flow{counter-reset:none;}
  ol.flow li{margin:7px 0;padding-left:2px;}
  .tag{display:inline-block;font-size:11px;font-weight:700;padding:2px 8px;border-radius:99px;margin-right:6px;}
  .tag.g{background:var(--okbg);color:var(--ok);border:1px solid #bbf7d0;}
  .tag.r{background:var(--badbg);color:var(--bad);border:1px solid #fecaca;}
  .tag.a{background:var(--warnbg);color:var(--warn);border:1px solid #fde68a;}
  .stats{display:grid;grid-template-columns:repeat(auto-fit,minmax(180px,1fr));gap:12px;margin-top:6px;}
  .card{border:1px solid var(--line);border-radius:10px;padding:14px 16px;}
  .card .nm{font-size:13px;font-weight:700;font-family:ui-monospace,Menlo,monospace;}
  .card .rate{font-size:28px;font-weight:800;letter-spacing:-1px;margin:4px 0;}
  .card .meta{font-size:11.5px;color:var(--muted);}
  .bar{height:6px;border-radius:99px;background:#eef0f2;margin-top:8px;overflow:hidden;}
  .bar>i{display:block;height:100%;border-radius:99px;}
  .req{font-size:10px;font-weight:700;text-transform:uppercase;letter-spacing:.5px;padding:1px 7px;border-radius:99px;}
  .req.y{background:#eff6ff;color:var(--accent);border:1px solid #bfdbfe;}
  .req.n{background:#f9fafb;color:var(--muted);border:1px solid #e5e7eb;}
  table{border-collapse:collapse;width:100%;font-size:12.5px;}
  thead th{position:sticky;top:0;background:#f6f8fa;text-align:left;padding:8px 10px;
    border-bottom:2px solid var(--line);font-size:11px;text-transform:uppercase;
    letter-spacing:.4px;color:var(--muted);}
  tbody td{padding:6px 10px;border-bottom:1px solid #eef0f2;}
  td.sha{font-family:ui-monospace,Menlo,monospace;font-size:12px;color:#0550ae;}
  td.dt{color:var(--muted);white-space:nowrap;font-size:12px;}
  td.msg{max-width:520px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;}
  td.c{text-align:center;font-weight:700;font-size:11px;width:64px;}
  td.c.ok{color:var(--ok);background:var(--okbg);}
  td.c.bad{color:var(--bad);background:var(--badbg);}
  td.c.warn{color:var(--warn);background:var(--warnbg);}
  td.c.na{color:var(--na);background:var(--nabg);}
  tr.green td{background:rgba(240,253,244,.5);}
  tr.sel td{background:#fef9c3 !important;}
  tr.sel td.sha{font-weight:800;}
  .pick{background:var(--accent);color:#fff;font-size:10px;font-weight:700;padding:1px 7px;border-radius:99px;margin-left:6px;}
  .legend{font-size:12px;color:var(--muted);margin:10px 0 0;display:flex;gap:16px;flex-wrap:wrap;align-items:center;}
  .legend b{display:inline-block;width:11px;height:11px;border-radius:3px;vertical-align:-1px;margin-right:5px;}
  .scroll{max-height:560px;overflow:auto;border:1px solid var(--line);border-radius:10px;}
  .note{font-size:12px;color:var(--muted);background:#fbfcfd;border:1px dashed var(--line);
    border-radius:8px;padding:10px 12px;margin-top:12px;}
  /* pick simulation */
  .simgrid{display:grid;grid-template-columns:1fr 1fr;gap:16px;margin-top:8px;}
  @media(max-width:900px){.simgrid{grid-template-columns:1fr;}}
  .simcol{border:1px solid var(--line);border-radius:10px;padding:12px 14px;background:#fbfcfd;}
  .simhdr{font-size:13px;font-weight:800;letter-spacing:-0.2px;margin-bottom:6px;}
  .simsum{font-size:12px;font-weight:600;border-radius:7px;padding:6px 10px;margin-bottom:8px;}
  .simsum.ok{background:var(--okbg);color:var(--ok);border:1px solid #bbf7d0;}
  .simsum.bad{background:var(--badbg);color:var(--bad);border:1px solid #fecaca;}
  .simtab{width:100%;border-collapse:collapse;font-size:11.5px;}
  .simtab th{text-align:left;padding:4px 6px;border-bottom:1.5px solid var(--line);
    font-size:10px;text-transform:uppercase;letter-spacing:.3px;color:var(--muted);}
  .simtab td{padding:3px 6px;border-bottom:1px solid #eef0f2;}
  .simtab td.mm{color:var(--muted);max-width:150px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;}
  .simtab tr.simskip{opacity:.62;}
  .simtab tr.simpick td{background:#fef9c3;font-weight:600;}
  .simtab tr.simpick td.sha{font-weight:800;}
  .simtab td.c sup{font-size:8px;font-weight:800;opacity:.75;margin-left:1px;}
  .srcline{font-size:11px;color:var(--muted);margin:-4px 0 8px;padding-left:2px;}
  .srcline b{color:var(--ink);}
"""

BODY = f"""<!DOCTYPE html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Nightly fbtriton — Audit Report</title><style>{CSS}</style></head>
<body><div class="wrap">

  <h1>Nightly fbtriton — Design &amp; Audit Report</h1>
  <p class="sub">Automated nightly <code>.dev</code> wheel selection for
    <code>facebookexperimental/triton</code> (<code>main</code>, base <code>{BASE}</code>) ·
    nightly series <code>{BASE}.dev&lt;date&gt;</code> · generated 2026-07-17 · data via GitHub
    Actions <code>check-runs</code> / run-timing APIs</p>

  <div class="keycard">
    <div class="keyhdr">◆ Proposal</div>
    <p><b>Version logic.</b> A one-time repo commit bumps the base to <code>{BASE}</code>
      (<code>setup.py</code> + <code>python/triton/__init__.py</code>). Wheels keep a clean PEP 440
      version — <code>{BASE}</code> (formal, → PyPI) or <code>{BASE}.dev&lt;date&gt;</code>
      (nightly, → GitHub Release). The runtime <code>triton.__version__</code> carries the
      fork/commit local segment: <code>{BASE}+fb</code> (formal) or
      <code>{BASE}.dev&lt;date&gt;+fb.git&lt;hash&gt;</code> (nightly). The nightly caller derives
      <code>{BASE}.dev&lt;date&gt;</code> (base read from source) and <code>wheels_fb</code>
      applies it via <code>setup.py</code>'s substitution hook to both
      <code>TRITON_VERSION</code> and <code>__version__</code>.</p>
    <p><b>Selection logic.</b> Fixed daily trigger (<code>04:00 UTC</code>) → read the last shipped
      commit from the nightly index (<code>latest.json</code>) → enumerate <i>new</i> commits since
      then → walk newest→oldest, pulling each commit's SHA-keyed <code>check-runs</code>
      (trigger-agnostic; a check is green if its <b>latest</b> run on that SHA passed — a rerun
      supersedes earlier runs; cancelled ignored) → first commit where <b>all four required checks
      pass</b> → build the shrunk matrix → upload to the rolling <code>nightly</code> Release,
      refresh the Pages index, prune &gt; {RETAIN}. No new commits → <b>exit silently</b>. New
      commits but none green → <b>file a tracking issue</b>.</p>
    <p><b>Storage &amp; retention.</b> Nightly wheels ({MATRIX}) are uploaded as assets on a single
      rolling <code>nightly</code> <b>GitHub Release</b> (one tag, no per-day tags) behind a
      <b>GitHub Pages</b> PEP 503 index; a prune step keeps the last <b>{RETAIN}</b>. PyPI can't do
      this (~10 GB project cap, no delete API), so it's reserved for formal releases. Everything
      runs on <code>GITHUB_TOKEN</code> — no PAT or App token.</p>
    <p style="margin:0"><b>Install (nightly).</b>
      <code>pip install --pre fbtriton --index-url https://facebookexperimental.github.io/triton/nightly/simple/</code></p>
  </div>

  <div class="panel">
    <h2>1 · Context</h2>
    <p><b>Goal.</b> Publish one <code>fbtriton</code> nightly <code>.dev</code> wheel per day to
      PyPI, built from a <i>verified</i> commit on <code>main</code> — not just whatever HEAD
      happens to be.</p>
    <p><b>Why verification is required.</b> On this fork, <b>GitHub CI is <u>not</u> a merge
      gate.</b> No ruleset defines <code>required_status_checks</code>. Commits are authored /
      reviewed inside Meta's monorepo and imported to public <code>main</code> via
      <b>CodeSync/ShipIt</b> — landing is allowed <i>without</i> CI signals. The GitHub Actions
      workflows run <b>after</b> a commit is already on <code>main</code>, so they are a
      <b>post-merge monitoring signal</b>. An arbitrary commit may therefore carry failing — or
      missing — CI, and the nightly must independently pick a green one.</p>
    <p><b>Constraints that shaped the design:</b></p>
    <ul>
      <li><b>Retention forces storage off PyPI.</b> Full-matrix nightlies are <b>~2.4 GB/day</b>,
        PyPI's project cap is ~10 GB, and PyPI has <b>no delete API</b> (manual, filenames burned) —
        so nightlies are hosted on a <b>GitHub Release</b> (binary asset storage) behind a
        <b>GitHub Pages</b> PEP 503 index, with a <b>prune</b> step keeping the last
        <b>{RETAIN}</b>. A <b>shrunk ABI matrix</b> ({MATRIX}) further cuts volume. Formal releases
        still go to PyPI.</li>
      <li><b>No exploded tags.</b> Nightlies reuse a single rolling <code>nightly</code> Release
        (one tag) — assets are added and pruned, never a per-day tag.</li>
      <li><b>Commit provenance.</b> Baked into the runtime <code>__version__</code> and a small
        <code>latest.json</code> manifest in the Release, which the next run reads back as the
        boundary marker (no state branch, can't drift).</li>
      <li><b>Clean wheel versions.</b> Wheels carry a plain PEP 440 version
        (<code>{BASE}.dev&lt;date&gt;</code>); the <code>+fb.git&lt;hash&gt;</code> local segment
        lives only in the runtime <code>__version__</code> (PyPI would reject it, and it keeps the
        formal + nightly paths consistent).</li>
      <li><b>H100 capacity.</b> H100 runs frequently queue 20+h or never run, so selection is
        event-driven (walk back to the newest already-green commit) and never blocks on the queue.</li>
      <li><b>No token.</b> <code>GITHUB_TOKEN</code> manages Releases and deploys Pages, so no PAT
        or GitHub App is required.</li>
    </ul>
    <h3 class="subh">CI signals — stats &amp; status</h3>
    <p>The nightly gates on four checks. Pass rates are over the <b>last 7 days</b>, counting only
      commits where the check actually ran (deduped to the latest run per check). "Coverage" = how
      many of {n_total} commits even got that check.</p>
    <div class="stats">
      <div class="card"><div class="nm">LIT Tests <span class="req y">required</span></div>
        <div class="rate" style="color:var(--ok)">97%</div>
        <div class="meta">62 / 64 passed · coverage 64/{n_total}</div>
        <div class="meta">⏱ avg exec <b>~23 min</b> · compiler LIT (CPU)</div>
        <div class="bar"><i style="width:97%;background:var(--ok)"></i></div></div>
      <div class="card"><div class="nm">h100-tlx-test <span class="req y">required</span></div>
        <div class="rate" style="color:var(--ok)">96%</div>
        <div class="meta">50 / 52 passed · coverage 52/{n_total}</div>
        <div class="meta">⏱ avg exec <b>~12 min</b> · queue-limited coverage</div>
        <div class="bar"><i style="width:96%;background:var(--ok)"></i></div></div>
      <div class="card"><div class="nm">mi350-tlx-test <span class="req y">required</span></div>
        <div class="rate" style="color:var(--ok)">97%</div>
        <div class="meta">63 / 65 passed · coverage 65/{n_total}</div>
        <div class="meta">⏱ avg exec <b>~17 min</b> · most reliable</div>
        <div class="bar"><i style="width:97%;background:var(--ok)"></i></div></div>
      <div class="card"><div class="nm">b200-tlx-test <span class="req y">required</span></div>
        <div class="rate" style="color:var(--warn)">65%</div>
        <div class="meta">43 / 66 passed · coverage 66/{n_total}</div>
        <div class="meta">⏱ avg exec <b>~9 min</b> · weak link</div>
        <div class="bar"><i style="width:65%;background:var(--warn)"></i></div></div>
      <div class="card" style="opacity:.7"><div class="nm">*-meta-triton-test <span class="req n">excluded</span></div>
        <div class="rate" style="color:var(--bad)">~37%</div>
        <div class="meta">tritonbench perf — flaky, deliberately not gated on</div>
        <div class="bar"><i style="width:37%;background:var(--bad)"></i></div></div>
    </div>
    <div class="note"><b>Status read:</b>
      <span class="tag g">mi350 / h100 / LIT solid (~96–97%)</span>
      <span class="tag a">b200 is the practical limiter (65% + often absent)</span>
      <span class="tag r">meta-triton excluded (chronically flaky)</span><br>
      <b>Avg exec time</b> is the job's own run time (queue excluded) — informational only; it
      does <b>not</b> gate the nightly, which just consumes whatever signal each commit already
      has. Note <code>h100-tlx-test</code> runs in <b>~12 min</b>; its long "time to signal" is
      pure queue wait, which is exactly why the walk-back (not HEAD) is used.</div>
  </div>

  <div class="panel">
    <h2>2 · High-level flow of the nightly build</h2>
    {FLOW_SVG}
  </div>

  <div class="panel">
    <h2>3 · Nightly pick simulation — point-in-time (04:00 UTC cut)</h2>
    <p>Replays the selection at two cut times. A check is green iff its <b>latest verdict completed
      by that moment</b> is a pass — later signals (especially queued <code>h100</code>) are
      invisible, faithfully reproducing what the nightly would have seen. Rows are the walk
      newest→oldest; greyed rows were skipped (not all-green as of the cut); the highlighted row is
      the pick. Each cell's superscript is the signal's <b>source</b> — <b>p</b>=push,
      <b>c</b>=cron (schedule); hover a cell for the exact run &amp; time. The <b>green from</b>
      line shows where each of the pick's four winning signals came from.</p>
    <div class="simgrid">{SIM_HTML}</div>
    <div class="legend">
      <span><b style="background:var(--okbg);border:1px solid var(--ok)"></b><code>pass</code> — latest verdict by the cut is success</span>
      <span><b style="background:var(--badbg);border:1px solid var(--bad)"></b><code>fail</code> — latest verdict by the cut is failure</span>
      <span><b style="background:var(--warnbg);border:1px solid var(--warn)"></b><code>cxl</code> — only a cancelled run so far (no verdict yet)</span>
      <span><b style="background:var(--nabg);border:1px solid var(--na)"></b><code>—</code> — no run completed by the cut</span>
      <span>source: <sup>p</sup>=push · <sup>c</sup>=cron</span>
    </div>
    <div class="note"><b>Point-in-time, not live status.</b> Cells reflect what was known
      <i>at 04:00 that day</i>. A commit whose <code>h100</code> only finished at, say, 18:46 reads
      <code>cxl</code>/<code>—</code> at the 04:00 cut even though it would be <code>pass</code>
      today — so each cut picks an older commit whose four signals had all completed in time.
      Notice <code>5b19662d1</code>: skipped at the 7/16 cut (its <code>h100</code> cron run was
      still queued, showing <code>cxl</code>) but the <b>pick</b> at 7/17 once that cron run finally
      completed at 03:01.</div>

  </div>

</div>
</body></html>"""

with open("docs/nightly-fbtriton-audit.html", "w") as f:
    f.write(BODY + "\n")
print("wrote docs/nightly-fbtriton-audit.html")
print("selected:", SELECTED, "| all-green:", n_green, "/", n_total)
