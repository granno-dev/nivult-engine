"""Il cruscotto privato del motore: come gira, cosa fa, cosa e' andato storto.

Una pagina sola, servita dall'API, che interroga i due database in tempo
reale e si aggiorna da sola. Ogni numero e' una query, niente inventato.

**Accesso blindato.** Non un token nell'URL — troppo fragile: se trapela,
chiunque entra. Si entra solo con l'OAuth Microsoft del sistema, e SOLO
l'email dell'operatore (CRUSCOTTO_EMAIL) passa; ogni altro account, per
quanto valido su Microsoft, riceve un rifiuto. Cosi' la sicurezza e'
quella di Microsoft, ma murata su un unico indirizzo.

Il flusso: /cruscotto -> se non loggato, /cruscotto/entra manda a
Microsoft -> /cruscotto/callback verifica l'email e mette un cookie
firmato (HMAC, 8 ore) -> la pagina chiama /cruscotto/dati.
"""

from __future__ import annotations

import hashlib
import hmac
import os
import time
from urllib.parse import urlencode

import httpx
import psycopg

from nivult import oauth as _oauth

OPERATORE = os.environ.get("CRUSCOTTO_EMAIL", "g.ranno@outlook.com").lower()
DURATA = 8 * 3600  # quanto dura la sessione del cruscotto


# ── accesso: OAuth Microsoft, murato su una sola email ──────────────

def _redirect_uri() -> str:
    return _oauth.api_url() + "/cruscotto/callback"


def _chiave() -> bytes:
    # riusa il segreto gia' presente per firmare cookie e state
    return os.environ.get("CRUSCOTTO_TOKEN", "nivult-fallback").encode()


def firma(payload: str) -> str:
    mac = hmac.new(_chiave(), payload.encode(), hashlib.sha256).hexdigest()[:40]
    return f"{payload}.{mac}"


def _spacchetta(token: str) -> str | None:
    if not token or "." not in token:
        return None
    payload, _, mac = token.rpartition(".")
    atteso = hmac.new(_chiave(), payload.encode(),
                      hashlib.sha256).hexdigest()[:40]
    return payload if hmac.compare_digest(mac, atteso) else None


def url_login(state: str) -> str:
    cfg = _oauth._config("microsoft")
    cid, _ = _oauth._credenziali("microsoft")
    return cfg["autorizzazione"] + "?" + urlencode({
        "client_id": cid,
        "response_type": "code",
        "redirect_uri": _redirect_uri(),
        "scope": cfg["scope"],
        "state": state,
        "response_mode": "query",
        # forza la scelta dell'account: non riusa un login altrui gia' aperto
        "prompt": "select_account",
    })


def email_da_code(code: str) -> str | None:
    """Scambia il code con Microsoft e ritorna l'email verificata (o None)."""
    cfg = _oauth._config("microsoft")
    cid, segreto = _oauth._credenziali("microsoft")
    try:
        r = httpx.post(cfg["token"], timeout=15, data={
            "client_id": cid, "client_secret": segreto, "code": code,
            "redirect_uri": _redirect_uri(),
            "grant_type": "authorization_code"})
    except httpx.HTTPError:
        return None
    if r.status_code != 200:
        return None
    id_token = r.json().get("id_token")
    if not id_token:
        return None
    try:
        claim = _oauth._decodifica_payload(id_token)
    except Exception:                                # noqa: BLE001
        return None
    email, _ = _oauth._email_attendibile("microsoft", claim)
    return (email or "").lower() or None


def cookie_sessione() -> str:
    return firma(f"{OPERATORE}|{int(time.time()) + DURATA}")


def sessione_valida(cookie: str) -> bool:
    p = _spacchetta(cookie or "")
    if not p or "|" not in p:
        return False
    email, _, scad = p.partition("|")
    try:
        return email == OPERATORE and int(scad) > time.time()
    except ValueError:
        return False


# ── le metriche: ogni voce una query vera ───────────────────────────

def _righe(dsn: str, sql: str) -> list[tuple]:
    with psycopg.connect(dsn, connect_timeout=10) as c:
        return c.execute(sql).fetchall()


def _uno(dsn: str, sql: str):
    r = _righe(dsn, sql)
    return r[0][0] if r and r[0] else None


def metriche(ats_dsn: str, motore_dsn: str) -> dict:
    d: dict = {}

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
        "offerte_viste_24h": _uno(ats_dsn,
            "SELECT count(*) FROM ats_jobs "
            "WHERE fetched_at > now() - interval '24 hours'"),
    }

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
        "piattaforme": _uno(ats_dsn,
            "SELECT count(DISTINCT platform_id) FROM ats_jobs "
            "WHERE expired_at IS NULL"),
    }

    d["per_fonte"] = [{"fonte": p, "attive": n} for p, n in _righe(ats_dsn,
        "SELECT platform_id, count(*) FROM ats_jobs "
        "WHERE expired_at IS NULL GROUP BY 1 ORDER BY 2 DESC LIMIT 25")]

    d["per_paese"] = [{"paese": p or "—", "attive": n} for p, n in _righe(ats_dsn,
        "SELECT country, count(*) FROM ats_jobs WHERE expired_at IS NULL "
        "GROUP BY 1 ORDER BY 2 DESC LIMIT 20")]

    d["per_famiglia"] = [{"famiglia": f, "attive": n} for f, n in _righe(ats_dsn,
        "SELECT c.family, count(*) FROM ats_jobs j "
        "JOIN job_classifications c ON c.job_id=j.id "
        "WHERE j.expired_at IS NULL AND c.confidence>=0.5 "
        "GROUP BY 1 ORDER BY 2 DESC LIMIT 25")]

    d["agenzie"] = [{"agenzia": s, "attive": n} for s, n in _righe(ats_dsn,
        "SELECT slug, count(*) FROM ats_jobs "
        "WHERE platform_id='agenzie' AND expired_at IS NULL "
        "GROUP BY 1 ORDER BY 2 DESC")]

    try:
        d["motore"] = {
            "offerte_nel_motore": _uno(motore_dsn,
                "SELECT count(*) FROM jobs WHERE status='active'"),
            "da_ats": _uno(motore_dsn,
                "SELECT count(*) FROM jobs WHERE status='active' "
                "AND source='ats'"),
            "cluster_attivi": _uno(motore_dsn,
                "SELECT count(*) FROM clusters WHERE status='active'"),
            "utenti": _uno(motore_dsn,
                "SELECT count(*) FROM users WHERE deleted_at IS NULL"),
        }
        d["cluster"] = [{"famiglia": f, "paese": p, "stato": s}
            for f, p, s in _righe(motore_dsn,
                "SELECT family, country, status FROM clusters "
                "ORDER BY status, family LIMIT 40")]
    except psycopg.Error:
        d["motore"], d["cluster"] = {}, []

    return d


PAGINA = """<!doctype html><html lang="it"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="robots" content="noindex,nofollow,noarchive">
<title>Nivult · Motore</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&display=swap" rel="stylesheet">
<style>
:root{--bg:#0b0f14;--card:#141a22;--card2:#1a212b;--line:#242c37;--ink:#e8eef5;
--dim:#8a97a6;--acc:#4c8dff;--ok:#46c46a;--warn:#e0a132;--bad:#ff5d54}
*{box-sizing:border-box;margin:0;padding:0}
body{background:var(--bg);color:var(--ink);
font:15px/1.55 Inter,-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif;
padding:32px 24px 64px;max-width:1180px;margin:0 auto;-webkit-font-smoothing:antialiased}
.top{display:flex;justify-content:space-between;align-items:center;
padding-bottom:18px;border-bottom:1px solid var(--line);margin-bottom:8px}
.brand{display:flex;align-items:baseline;gap:10px}
.brand b{font-size:22px;font-weight:700;letter-spacing:-.02em}
.brand span{font-size:13px;color:var(--dim);font-weight:500}
.live{font-size:12px;color:var(--dim);display:flex;align-items:center;gap:7px}
.dot{width:8px;height:8px;border-radius:50%;background:var(--ok);
box-shadow:0 0 0 0 rgba(70,196,106,.5);animation:pulse 2.2s infinite}
@keyframes pulse{0%{box-shadow:0 0 0 0 rgba(70,196,106,.45)}70%{box-shadow:0 0 0 7px rgba(70,196,106,0)}100%{box-shadow:0 0 0 0 rgba(70,196,106,0)}}
h2{font-size:12px;text-transform:uppercase;letter-spacing:.09em;color:var(--dim);
margin:34px 0 14px;font-weight:600}
.grid{display:grid;gap:14px;grid-template-columns:repeat(auto-fit,minmax(172px,1fr))}
.card{background:linear-gradient(180deg,var(--card2),var(--card));
border:1px solid var(--line);border-radius:14px;padding:18px 18px 16px}
.num{font-size:28px;font-weight:700;letter-spacing:-.025em;font-variant-numeric:tabular-nums;line-height:1.1}
.lbl{font-size:13px;color:var(--dim);margin-top:5px;font-weight:500}
.sub{font-size:12px;color:var(--dim);margin-top:7px;opacity:.85}
.cols{display:grid;gap:22px;grid-template-columns:1fr 1fr}
@media(max-width:800px){.cols{grid-template-columns:1fr}}
.panel{background:var(--card);border:1px solid var(--line);border-radius:14px;
padding:6px 4px 10px}
.row{display:flex;align-items:center;gap:12px;padding:8px 14px}
.row .k{flex:1;font-size:14px;font-weight:500;text-transform:capitalize;
white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.row .v{font-size:14px;font-weight:600;font-variant-numeric:tabular-nums;color:var(--ink)}
.track{flex:1.6;height:7px;background:#0c1117;border-radius:4px;overflow:hidden}
.track>i{display:block;height:100%;background:linear-gradient(90deg,#3a6fd8,var(--acc));border-radius:4px}
.ok{color:var(--ok)}.warn{color:var(--warn)}.bad{color:var(--bad)}
.pill{display:inline-block;font-size:12px;padding:3px 11px;border-radius:20px;
background:var(--card2);color:var(--dim);border:1px solid var(--line);margin:3px 4px 0 0}
.pill.on{background:rgba(70,196,106,.13);color:var(--ok);border-color:rgba(70,196,106,.3)}
.foot{margin-top:40px;font-size:12px;color:var(--dim);text-align:center}
</style></head><body>
<div class="top">
  <div class="brand"><b>Nivult</b><span>Cruscotto del motore</span></div>
  <div class="live"><span class="dot"></span><span id="ts">connessione…</span></div>
</div>
<div id="app"></div>
<div class="foot">Aggiornamento automatico ogni 30 secondi · accesso riservato</div>
<script>
const IT=n=>n==null?'—':n.toLocaleString('it-IT');
function eta(iso){if(!iso)return '—';const s=(Date.now()-new Date(iso))/1000;
 if(s<0)return 'ora';if(s<90)return Math.round(s)+' sec fa';
 if(s<5400)return Math.round(s/60)+' min fa';
 if(s<86400)return Math.round(s/3600)+' ore fa';return Math.round(s/86400)+' giorni fa'}
const card=(v,l,sub)=>`<div class="card"><div class="num">${v}</div><div class="lbl">${l}</div>${sub!==undefined?`<div class="sub">${sub}</div>`:''}</div>`;
function lista(rows,k,vk){if(!rows||!rows.length)return '<div class="sub" style="padding:12px 14px">nessun dato</div>';
 const max=Math.max(...rows.map(r=>r[vk]),1);
 return '<div class="panel">'+rows.map(r=>`<div class="row"><div class="k">${r[k]}</div>`+
  `<div class="track"><i style="width:${Math.round(100*r[vk]/max)}%"></i></div>`+
  `<div class="v">${IT(r[vk])}</div></div>`).join('')+'</div>'}
async function tick(){
 let d;try{const r=await fetch('/cruscotto/dati');if(!r.ok)throw 0;d=await r.json()}
 catch(e){document.getElementById('ts').textContent='non raggiungibile';return}
 const s=d.stato,h=d.salute,m=d.motore||{};
 document.getElementById('ts').textContent='aggiornato alle '+new Date().toLocaleTimeString('it-IT');
 const clP=h.senza_paese_pct>60?'bad':h.senza_paese_pct>30?'warn':'ok';
 const clC=h.non_classificate_pct>40?'bad':h.non_classificate_pct>20?'warn':'ok';
 let H='<h2>In tempo reale</h2><div class="grid">'
 +card(IT(h.offerte_attive),'offerte attive')
 +card(IT(s.offerte_viste_24h),'raccolte o aggiornate nelle 24 ore')
 +card(IT(s.aziende_scrapate_1h),'aziende visitate nell\\'ultima ora')
 +card(eta(s.ultimo_scrape),'ultima raccolta')
 +card(eta(s.ultima_classificazione),'ultima classificazione')
 +card(eta(s.ultimo_ponte),'ultimo travaso ai clienti')
 +'</div>'
 +'<h2>Salute della raffineria</h2><div class="grid">'
 +card(`<span class="${clP}">${h.senza_paese_pct}%</span>`,'offerte senza paese',IT(h.senza_paese)+' su '+IT(h.offerte_attive))
 +card(`<span class="${clC}">${h.non_classificate_pct}%</span>`,'offerte non classificate',IT(h.non_classificate)+' su '+IT(h.offerte_attive))
 +card(IT(h.piattaforme),'piattaforme ATS attive')
 +card(IT(h.aziende_con_offerte),'aziende con offerte')
 +card(IT(h.aziende_censite),'aziende censite in totale')
 +card(IT(h.aziende_mai_viste),'aziende ancora da visitare')
 +'</div>'
 +'<div class="cols"><div><h2>Offerte per fonte</h2>'+lista(d.per_fonte,'fonte','attive')+'</div>'
 +'<div><h2>Offerte per paese</h2>'+lista(d.per_paese,'paese','attive')+'</div></div>'
 +'<div class="cols"><div><h2>Offerte per famiglia professionale</h2>'+lista(d.per_famiglia,'famiglia','attive')+'</div>'
 +'<div><h2>Agenzie per il lavoro</h2>'+lista(d.agenzie,'agenzia','attive')+'</div></div>'
 +'<h2>Motore e ricerche degli iscritti</h2><div class="grid">'
 +card(IT(m.offerte_nel_motore),'offerte nel motore')
 +card(IT(m.da_ats),'dai nostri ATS diretti')
 +card(IT(m.cluster_attivi),'ricerche attive')
 +card(IT(m.utenti),'iscritti')
 +'</div>';
 if(d.cluster&&d.cluster.length)H+='<div style="margin-top:14px">'+d.cluster.map(c=>
  `<span class="pill ${c.stato==='active'?'on':''}">${c.famiglia} · ${c.paese}</span>`).join('')+'</div>';
 document.getElementById('app').innerHTML=H}
tick();setInterval(tick,30000);
</script></body></html>"""
