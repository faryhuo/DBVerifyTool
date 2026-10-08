"""Inline CSS and JavaScript for the HTML report."""
from __future__ import annotations


CSS = r"""
:root{--bg:#0d1117;--panel:#161b22;--panel2:#1c2128;--grp:#11161d;--border:#30363d;--text:#c9d1d9;--muted:#8b949e;
--blue:#58a6ff;--red:#f85149;--yellow:#d29922;--green:#3fb950;--chip:#1f2a37}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--text);font:13px/1.55 "Cascadia Mono",Consolas,"SFMono-Regular",Menlo,monospace}
header{position:sticky;top:0;z-index:5;background:var(--panel);border-bottom:1px solid var(--border);padding:16px 28px 12px}
h1{margin:0 0 6px;font-size:20px;color:var(--blue)}
.meta{color:var(--muted);font-size:12px}.meta b{color:var(--text);font-weight:600}.meta span{margin-right:14px}
nav{margin-top:10px;display:flex;flex-wrap:wrap;gap:6px}
nav a{color:var(--blue);text-decoration:none;border:1px solid var(--border);border-radius:4px;padding:2px 10px;font-size:12px;background:var(--panel2)}
nav a:hover{border-color:var(--blue)}
main{padding:22px 28px;max-width:1600px}
section.card{background:var(--panel);border:1px solid var(--border);border-radius:8px;margin-bottom:22px;scroll-margin-top:175px}
.card>h2{margin:0;padding:13px 18px;font-size:15px;border-bottom:1px solid var(--border);display:flex;gap:10px;align-items:center;flex-wrap:wrap}
.body{padding:16px 18px}
.badge{display:inline-block;padding:0 8px;border-radius:10px;font-size:11px;border:1px solid;white-space:nowrap;line-height:18px;font-weight:600}
.b-crit{color:var(--red);border-color:var(--red);background:rgba(248,81,73,.12)}
.b-warn{color:var(--yellow);border-color:var(--yellow);background:rgba(210,153,34,.12)}
.b-ok{color:var(--green);border-color:var(--green);background:rgba(63,185,80,.12)}
.b-info{color:var(--blue);border-color:var(--blue);background:rgba(88,166,255,.12)}
.b-mut{color:var(--muted);border-color:var(--border)}
.cards{display:grid;grid-template-columns:repeat(auto-fit,minmax(140px,1fr));gap:12px}
.stat{background:var(--panel2);border:1px solid var(--border);border-radius:6px;padding:16px 10px;text-align:center}
.stat .n{font-size:26px;font-weight:700}.stat .l{color:var(--muted);font-size:11px}
.c-blue{color:var(--blue)}.c-red{color:var(--red)}.c-yellow{color:var(--yellow)}.c-green{color:var(--green)}.muted{color:var(--muted)}
.legend{display:flex;gap:18px;flex-wrap:wrap;padding:12px 0;border-top:1px solid var(--border);border-bottom:1px solid var(--border);margin:16px 0;font-size:12px}
.dot{display:inline-block;width:10px;height:10px;border-radius:50%;margin-right:6px;vertical-align:-1px}
.label{color:var(--muted);font-size:11px;letter-spacing:.08em;text-transform:uppercase;margin:18px 0 8px}
.tbl{overflow-x:auto;border:1px solid var(--border);border-radius:6px;margin-bottom:6px}
table{border-collapse:collapse;width:100%}
th,td{padding:7px 10px;border-bottom:1px solid var(--border);text-align:left;vertical-align:top}
th{color:var(--muted);font-size:11px;text-transform:uppercase;background:var(--panel2);white-space:nowrap}
th.s{cursor:pointer;user-select:none}th.s:after{content:" \2195";opacity:.45}
th.s[data-dir]:after{opacity:1;color:var(--blue)}
tr.grp td{background:var(--grp);color:var(--blue);font-weight:700}
tbody tr:hover td{background:rgba(88,166,255,.04)}
.chip{display:inline-block;background:var(--chip);border:1px solid var(--border);border-radius:4px;padding:0 6px;color:var(--blue);font-size:12px;word-break:break-all}
.wide{min-width:460px}
.path{color:var(--muted);font-size:11px;word-break:break-all}
.item{margin:2px 0}
details{border:1px solid var(--border);border-radius:6px;margin:10px 0;background:var(--panel2)}
details>summary{cursor:pointer;padding:10px 14px;font-weight:700;list-style:none}
details>summary::-webkit-details-marker{display:none}
details>summary:before{content:"\25B8  ";color:var(--muted)}details[open]>summary:before{content:"\25BE  "}
details .tbl{margin:0 12px 12px}
ul.checks{margin:6px 0 10px;padding-left:20px}ul.checks li{margin:3px 0}
.flow{display:flex;gap:8px;align-items:center;flex-wrap:wrap;font-size:15px;margin:6px 0 12px}
.rng{font-size:11px;color:var(--muted);border:1px solid var(--border);border-radius:4px;padding:0 5px}
.prio{list-style:none;margin:0;padding:0}
.prio li{display:flex;gap:10px;align-items:baseline;padding:7px 10px;margin:4px 0;border-left:3px solid var(--red);background:rgba(248,81,73,.06)}
.prio li.w{border-left-color:var(--yellow);background:rgba(210,153,34,.06)}
.prio li.i{border-left-color:var(--blue);background:rgba(88,166,255,.06)}
.prio .n{color:var(--muted);min-width:28px;text-align:right}
.note{color:var(--muted);font-size:12px}
footer{color:var(--muted);font-size:11px;padding:0 28px 30px}
"""

JS = r"""
(function(){
function key(c){if(!c)return'';var v=c.getAttribute('data-v');if(v===null)v=c.textContent.trim();
 return /^-?\d+(\.\d+)?$/.test(v)?parseFloat(v):v.toLowerCase();}
document.querySelectorAll('table th.s').forEach(function(th){
 th.addEventListener('click',function(){
  var table=th.closest('table'),idx=th.cellIndex,asc=th.getAttribute('data-dir')!=='asc';
  table.querySelectorAll('th.s').forEach(function(h){h.removeAttribute('data-dir');});
  th.setAttribute('data-dir',asc?'asc':'desc');
  table.querySelectorAll('tbody').forEach(function(tb){
   var rows=[].slice.call(tb.rows).filter(function(r){return !r.classList.contains('grp');});
   rows.sort(function(a,b){var x=key(a.cells[idx]),y=key(b.cells[idx]);
    if(typeof x==='number'&&typeof y==='number')return asc?x-y:y-x;
    return asc?String(x).localeCompare(String(y)):String(y).localeCompare(String(x));});
   rows.forEach(function(r){tb.appendChild(r);});
  });
 });
});
})();
"""
