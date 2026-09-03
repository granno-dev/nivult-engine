"""Il cruscotto privato del motore: come gira, cosa fa, cosa e' andato storto.

Una pagina sola, servita dall'API dietro un token segreto, non indicizzata
(noindex + robots), che interroga i due database in tempo reale. Nessun
dato inventato: ogni numero e' una query. Si aggiorna da sola.

Le metriche stanno in `metriche()`; la pagina in `PAGINA` chiama
`/cruscotto/<token>/dati` ogni 30s e ridisegna.
"""

from __future__ import annotations

import psycopg


def _righe(dsn: str, sql: str) -> list[tuple]:
    with psycopg.connect(dsn, connect_timeout=10) as c:
        return c.execute(sql).fetchall()


def _uno(dsn: str, sql: str):
    r = _righe(dsn, sql)
    return r[0][0] if r and r[0] else None


def metriche(ats_dsn: str, motore_dsn: str) -> dict:
    """Tutto lo stato del motore, in un colpo. Ogni voce e' una query vera."""
    d: dict = {}

    # ── stato pipeline: quando ha lavorato ciascun pezzo, di recente ──
    d["stato"] = {
        "ultimo_scrape": str(_uno(ats_dsn,
            "SELECT max(fetched_at) FROM ats_jobs") or ""),
        "ultima_classificazione": str(_uno(ats_dsn,
            "SELECT max(classified_at) FROM job_classifications") or ""),
        "ultimo_ponte": str(_uno(motore_dsn,
            "SELECT max(first_seen_at) FROM jobs WHERE source='ats'") or ""),
        "aziende_scrapate_1h": _uno(ats_dsn,
            "SELECT count(*) FROM ats_companies "
            "WHERE last_fetch_at > now() - interval '1 hour'"),
        "offerte_nuove_24h": _uno(ats_dsn,
            "SELECT count(*) FROM ats_jobs "
            "WHERE fetched_at > now() - interval '24 hours' "
            "AND expired_at IS NULL"),
    }

    # ── salute della raffineria ──
    attive = _uno(ats_dsn,
        "SELECT count(*) FROM ats_jobs WHERE expired_at IS NULL") or 0
    senza_paese = _uno(ats_dsn,
        "SELECT count(*) FROM ats_jobs WHERE expired_at IS NULL "
        "AND country IS NULL") or 0
    non_class = _uno(ats_dsn,
        "SELECT count(*) FROM ats_jobs j LEFT JOIN job_classifications c "
        "ON c.job_id=j.id WHERE c.job_id IS NULL AND j.expired_at IS NULL") or 0
    d["salute"] = {
        "offerte_attive": attive,
        "senza_paese": senza_paese,
        "senza_paese_pct": round(100 * senza_paese / attive, 1) if attive else 0,
        "non_classificate": non_class,
        "non_classificate_pct": round(100 * non_class / attive, 1) if attive else 0,
        "aziende_censite": _uno(ats_dsn, "SELECT count(*) FROM ats_companies"),
        "aziende_mai_viste": _uno(ats_dsn,
            "SELECT count(*) FROM ats_companies WHERE last_fetch_at IS NULL"),
        "aziende_con_offerte": _uno(ats_dsn,
            "SELECT count(DISTINCT (platform_id,slug)) FROM ats_jobs "
            "WHERE expired_at IS NULL"),
    }

    # ── offerte per fonte ──
    d["per_fonte"] = [
        {"fonte": p, "attive": n} for p, n in _righe(ats_dsn,
            "SELECT platform_id, count(*) FROM ats_jobs "
            "WHERE expired_at IS NULL GROUP BY 1 ORDER BY 2 DESC LIMIT 25")]

    # ── offerte per paese ──
    d["per_paese"] = [
        {"paese": p or "—", "attive": n} for p, n in _righe(ats_dsn,
            "SELECT country, count(*) FROM ats_jobs WHERE expired_at IS NULL "
            "GROUP BY 1 ORDER BY 2 DESC LIMIT 20")]

    # ── offerte per famiglia (classificate) ──
    d["per_famiglia"] = [
        {"famiglia": f, "attive": n} for f, n in _righe(ats_dsn,
            "SELECT c.family, count(*) FROM ats_jobs j "
            "JOIN job_classifications c ON c.job_id=j.id "
            "WHERE j.expired_at IS NULL AND c.confidence>=0.5 "
            "GROUP BY 1 ORDER BY 2 DESC LIMIT 25")]

    # ── le agenzie, a parte ──
    d["agenzie"] = [
        {"agenzia": s, "attive": n} for s, n in _righe(ats_dsn,
            "SELECT slug, count(*) FROM ats_jobs "
            "WHERE platform_id='agenzie' AND expired_at IS NULL "
            "GROUP BY 1 ORDER BY 2 DESC")]

    # ── il motore a valle: cluster e utenti ──
    try:
        d["motore"] = {
            "offerte_nel_motore": _uno(motore_dsn,
                "SELECT count(*) FROM jobs WHERE status='active'"),
            "da_ats": _uno(motore_dsn,
                "SELECT count(*) FROM jobs WHERE status='active' AND source='ats'"),
            "cluster_attivi": _uno(motore_dsn,
                "SELECT count(*) FROM clusters WHERE status='active'"),
            "utenti": _uno(motore_dsn,
                "SELECT count(*) FROM users WHERE deleted_at IS NULL"),
        }
        d["cluster"] = [
            {"famiglia": f, "paese": p, "stato": s} for f, p, s in _righe(
                motore_dsn, "SELECT family, country, status FROM clusters "
                "ORDER BY status, family LIMIT 40")]
    except psycopg.Error:
        d["motore"] = {}
        d["cluster"] = []

    return d


# La pagina: un file solo, tutto inline, tema scuro sobrio. Prende i dati
# da /dati e li disegna; si aggiorna ogni 30 secondi.
PAGINA = """<!doctype html><html lang="it"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="robots" content="noindex,nofollow,noarchive">
<title>Nivult · Motore</title>
<style>
:root{--bg:#0d1117;--card:#161b22;--line:#21262d;--ink:#e6edf3;--dim:#8b949e;
--acc:#2f81f7;--ok:#3fb950;--warn:#d29922;--bad:#f85149}
*{box-sizing:border-box;margin:0;padding:0}
body{background:var(--bg);color:var(--ink);font:14px/1.5 -apple-system,BlinkMacSystemFont,
"Segoe UI",Roboto,sans-serif;padding:24px;max-width:1200px;margin:0 auto}
h1{font-size:20px;font-weight:650;letter-spacing:-.01em}
h2{font-size:12px;text-transform:uppercase;letter-spacing:.08em;color:var(--dim);
margin:28px 0 12px;font-weight:600}
.top{display:flex;justify-content:space-between;align-items:baseline;
border-bottom:1px solid var(--line);padding-bottom:14px}
.live{font-size:12px;color:var(--dim)}
.dot{display:inline-block;width:8px;height:8px;border-radius:50%;background:var(--ok);
margin-right:6px;animation:p 2s infinite}
@keyframes p{50%{opacity:.35}}
.grid{display:grid;gap:12px;grid-template-columns:repeat(auto-fit,minmax(180px,1fr))}
.card{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:16px}
.num{font-size:26px;font-weight:680;letter-spacing:-.02em;font-variant-numeric:tabular-nums}
.lbl{font-size:12px;color:var(--dim);margin-top:2px}
.sub{font-size:11px;color:var(--dim);margin-top:6px}
table{width:100%;border-collapse:collapse;font-variant-numeric:tabular-nums}
th,td{text-align:left;padding:7px 10px;border-bottom:1px solid var(--line)}
th{font-size:11px;text-transform:uppercase;letter-spacing:.05em;color:var(--dim);font-weight:600}
td.n{text-align:right;font-weight:600}
.cols{display:grid;gap:20px;grid-template-columns:1fr 1fr}
@media(max-width:760px){.cols{grid-template-columns:1fr}}
.bar{height:6px;background:var(--line);border-radius:3px;overflow:hidden;margin-top:4px}
.bar>i{display:block;height:100%;background:var(--acc)}
.ok{color:var(--ok)}.warn{color:var(--warn)}.bad{color:var(--bad)}
.pill{display:inline-block;font-size:11px;padding:1px 8px;border-radius:20px;
background:var(--line);color:var(--dim)}
.pill.active{background:rgba(63,185,80,.15);color:var(--ok)}
</style></head><body>
<div class="top"><h1>Nivult · Motore</h1>
<div class="live"><span class="dot"></span><span id="ts">—</span></div></div>
<div id="app"></div>
<script>
const TOKEN = location.pathname.split('/')[2];
function fmt(n){return n==null?'—':n.toLocaleString('it-IT')}
function eta(iso){if(!iso)return '—';const s=(Date.now()-new Date(iso))/1000;
if(s<90)return Math.round(s)+'s fa';if(s<5400)return Math.round(s/60)+'min fa';
if(s<86400)return Math.round(s/3600)+'h fa';return Math.round(s/86400)+'g fa'}
function tab(cols,rows,keyN){let h='<table><tr>'+cols.map(c=>'<th'+(c[2]?' style="text-align:right"':'')+'>'+c[0]+'</th>').join('')+'</tr>';
for(const r of rows){h+='<tr>'+cols.map(c=>'<td'+(c[2]?' class="n"':'')+'>'+(c[2]?fmt(r[c[1]]):r[c[1]])+'</td>').join('')+'</tr>'}
return h+'</table>'}
async function tick(){
 let d;try{d=await (await fetch('/cruscotto/'+TOKEN+'/dati')).json()}catch(e){return}
 document.getElementById('ts').textContent='aggiornato '+new Date().toLocaleTimeString('it-IT');
 const s=d.stato,h=d.salute,m=d.motore||{};
 const spct=h.senza_paese_pct, cpct=h.non_classificate_pct;
 const clsP=spct>60?'bad':spct>30?'warn':'ok', clsC=cpct>40?'bad':cpct>20?'warn':'ok';
 let H='';
 H+='<h2>Stato in tempo reale</h2><div class="grid">';
 H+=card(fmt(h.offerte_attive),'offerte attive');
 H+=card(fmt(s.offerte_nuove_24h),'nuove ultime 24h');
 H+=card(fmt(s.aziende_scrapate_1h),'aziende scrapate ultima ora');
 H+=card(eta(s.ultimo_scrape),'ultimo scrape','');
 H+=card(eta(s.ultima_classificazione),'ultima classificazione','');
 H+=card(eta(s.ultimo_ponte),'ultimo ponte al motore','');
 H+='</div>';
 H+='<h2>Salute della raffineria</h2><div class="grid">';
 H+=card('<span class="'+clsP+'">'+spct+'%</span>','senza paese','di '+fmt(h.offerte_attive));
 H+=card('<span class="'+clsC+'">'+cpct+'%</span>','non classificate','di '+fmt(h.offerte_attive));
 H+=card(fmt(h.aziende_censite),'aziende censite');
 H+=card(fmt(h.aziende_con_offerte),'aziende con offerte');
 H+=card(fmt(h.aziende_mai_viste),'aziende ancora da visitare');
 H+='</div>';
 H+='<div class="cols"><div>';
 H+='<h2>Offerte per fonte</h2>'+barTab(d.per_fonte,'fonte','attive');
 H+='</div><div>';
 H+='<h2>Offerte per paese</h2>'+barTab(d.per_paese,'paese','attive');
 H+='</div></div>';
 H+='<div class="cols"><div>';
 H+='<h2>Offerte per famiglia</h2>'+barTab(d.per_famiglia,'famiglia','attive');
 H+='</div><div>';
 H+='<h2>Agenzie</h2>'+barTab(d.agenzie,'agenzia','attive');
 H+='</div></div>';
 H+='<h2>Motore &amp; cluster degli utenti</h2><div class="grid">';
 H+=card(fmt(m.offerte_nel_motore),'offerte nel motore');
 H+=card(fmt(m.da_ats),'di cui dai nostri ATS');
 H+=card(fmt(m.cluster_attivi),'ricerche attive');
 H+=card(fmt(m.utenti),'iscritti');
 H+='</div>';
 if(d.cluster&&d.cluster.length){H+='<div style="margin-top:12px">'+
   d.cluster.map(c=>'<span class="pill '+(c.stato==='active'?'active':'')+'" style="margin:3px">'+
   c.famiglia+' · '+c.paese+'</span>').join('')+'</div>'}
 document.getElementById('app').innerHTML=H;
}
function card(v,l,sub){return '<div class="card"><div class="num">'+v+'</div>'+
 '<div class="lbl">'+l+'</div>'+(sub!==undefined?'<div class="sub">'+sub+'</div>':'')+'</div>'}
function barTab(rows,k,vk){if(!rows||!rows.length)return '<div class="sub">nessun dato</div>';
 const max=Math.max(...rows.map(r=>r[vk]));let h='<table>';
 for(const r of rows){const w=Math.round(100*r[vk]/max);
  h+='<tr><td>'+r[k]+'</td><td class="n">'+fmt(r[vk])+
  '</td></tr><tr><td colspan="2" style="padding:0 10px 6px"><div class="bar"><i style="width:'+w+'%"></i></div></td></tr>'}
 return h+'</table>'}
tick();setInterval(tick,30000);
</script></body></html>"""
