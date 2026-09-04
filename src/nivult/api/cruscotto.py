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

    # ── freschezza del codone: ogni tenant va rivisitato entro la soglia
    # (spazzino a 12h in runner.py), altrimenti le posizioni appena aperte
    # su un tenant prima vuoto si perdono. Qui misuriamo quanti tenant
    # scrapabili sono oltre soglia: se cresce, il codone si sta affamando.
    _fresh = _righe(ats_dsn, """
        SELECT
          count(*),
          count(*) FILTER (WHERE ac.last_fetch_at >= now()-interval '12 hours'
                              OR ac.last_fetch_at IS NULL),
          count(*) FILTER (WHERE ac.last_fetch_at < now()-interval '12 hours'),
          count(*) FILTER (WHERE ac.last_fetch_at < now()-interval '24 hours')
          FROM ats_companies ac JOIN ats_platforms ap ON ap.id=ac.platform_id
         WHERE ac.is_active AND ap.is_active""")
    _tot, _fre, _o12, _o24 = _fresh[0] if _fresh else (0, 0, 0, 0)
    d["salute"]["codone_totale"] = _tot
    d["salute"]["codone_oltre_12h"] = _o12
    d["salute"]["codone_oltre_24h"] = _o24
    d["salute"]["codone_freschi_pct"] = round(100 * _fre / _tot, 1) if _tot else 100.0

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
        # ── ogni iscritto con tutta la sua configurazione ──
        d["iscritti"] = [{
            "nome": nome or "—", "email": em, "lingua": loc,
            "canali": list(can or []), "stato": st,
            "ultimo_digest": str(ld or ""),
            "ricerche": list(ric or [])}
            for nome, em, loc, can, st, ld, ric in _righe(motore_dsn, """
                SELECT u.display_name, u.email, u.locale, u.delivery_channels,
                       u.status, u.last_digest_at,
                       array_remove(array_agg(DISTINCT
                         c.family || ' · ' || c.country)
                         FILTER (WHERE c.status = 'active'), NULL)
                FROM users u
                LEFT JOIN user_clusters uc ON uc.user_id = u.id
                LEFT JOIN clusters c ON c.id = uc.cluster_id
                WHERE u.deleted_at IS NULL
                GROUP BY u.id, u.display_name, u.email, u.locale,
                         u.delivery_channels, u.status, u.last_digest_at
                ORDER BY u.email""")]
    except psycopg.Error:
        d["motore"], d["cluster"], d["iscritti"] = {}, [], []

    # ── ATS pending: piattaforme censite ma senza adattatore, quindi
    # scoperte ma non ancora raccoglibili — vanno «aggiunte» scrivendo
    # l'adattatore. E' la coda di lavoro delle nuove piattaforme.
    try:
        from nivult.ats.adapters import ADAPTERS
        noti = set(ADAPTERS.keys())
    except Exception:                                # noqa: BLE001
        noti = set()
    pend = _righe(ats_dsn, """
        SELECT ac.platform_id, count(*),
               count(*) FILTER (WHERE ac.last_fetch_at IS NULL)
          FROM ats_companies ac
          JOIN ats_platforms ap ON ap.id = ac.platform_id
         WHERE ac.is_active AND ap.is_active
         GROUP BY ac.platform_id""")
    d["ats_pending"] = sorted(
        [{"piattaforma": p, "aziende": tot, "in_attesa": mai}
         for p, tot, mai in pend if p not in noti],
        key=lambda r: -r["aziende"])
    d["salute"]["ats_pending_n"] = len(d["ats_pending"])
    d["salute"]["ats_pending_aziende"] = sum(
        r["aziende"] for r in d["ats_pending"])

    # ── andamento: offerte pubblicate per giorno (30 gg), col dettaglio
    # per fonte dietro ogni numero, per il grafico interattivo ──
    grezzo = _righe(ats_dsn, """
        SELECT to_char(date_trunc('day', posted_at), 'YYYY-MM-DD') AS g,
               platform_id, count(*) AS n
          FROM ats_jobs
         WHERE expired_at IS NULL
           AND posted_at > now() - interval '30 days'
           AND posted_at <= now()
         GROUP BY 1, 2 ORDER BY 1""")
    per_giorno: dict = {}
    for g, pid, n in grezzo:
        v = per_giorno.setdefault(g, {"giorno": g, "offerte": 0, "fonti": {}})
        v["offerte"] += n
        v["fonti"][pid] = v["fonti"].get(pid, 0) + n
    d["andamento"] = []
    for g in sorted(per_giorno):
        v = per_giorno[g]
        fonti = sorted(v["fonti"].items(), key=lambda x: -x[1])[:6]
        d["andamento"].append({
            "giorno": g[8:10] + "/" + g[5:7],   # DD/MM
            "data": g,
            "offerte": v["offerte"],
            "dettaglio": [{"fonte": p, "n": n} for p, n in fonti]})

    # ── funnel della raffineria: da azienda censita a offerta consegnata.
    # Ogni gradino perde qualcosa; il salto piu' stretto e' dove lavorare.
    cens = d["salute"]["aziende_censite"] or 0
    maiv = d["salute"]["aziende_mai_viste"] or 0
    conoff = d["salute"]["aziende_con_offerte"] or 0
    motore_ats = (d.get("motore") or {}).get("da_ats") or 0
    d["funnel"] = [
        {"fase": "Aziende censite", "n": cens},
        {"fase": "Almeno una volta viste", "n": cens - maiv},
        {"fase": "Con offerte vive", "n": conoff},
        {"fase": "Offerte attive", "n": attive},
        {"fase": "Portate nel motore", "n": motore_ats},
    ]

    # ── istogramma della freschezza: quanti tenant per fascia di ultima
    # visita. Se le fasce lontane si gonfiano, lo spazzino non ce la fa.
    _b = _righe(ats_dsn, """
        SELECT
          count(*) FILTER (WHERE ac.last_fetch_at IS NULL),
          count(*) FILTER (WHERE ac.last_fetch_at >= now()-interval '1 hour'),
          count(*) FILTER (WHERE ac.last_fetch_at <  now()-interval '1 hour'
                              AND ac.last_fetch_at >= now()-interval '6 hours'),
          count(*) FILTER (WHERE ac.last_fetch_at <  now()-interval '6 hours'
                              AND ac.last_fetch_at >= now()-interval '12 hours'),
          count(*) FILTER (WHERE ac.last_fetch_at <  now()-interval '12 hours'
                              AND ac.last_fetch_at >= now()-interval '24 hours'),
          count(*) FILTER (WHERE ac.last_fetch_at <  now()-interval '24 hours')
          FROM ats_companies ac JOIN ats_platforms ap ON ap.id=ac.platform_id
         WHERE ac.is_active AND ap.is_active""")
    b = _b[0] if _b else (0, 0, 0, 0, 0, 0)
    _fasce = ["mai", "< 1h", "1–6h", "6–12h", "12–24h", "> 24h"]
    d["freschezza"] = [{"fascia": _fasce[i], "n": b[i], "caldo": i >= 4}
                       for i in range(6)]

    # ── da dove viene l'ampiezza: la fonte di scoperta dei tenant ──
    d["per_scoperta"] = [{"fonte": s or "—", "n": n} for s, n in _righe(ats_dsn,
        "SELECT discovered_from, count(*) FROM ats_companies "
        "GROUP BY 1 ORDER BY 2 DESC LIMIT 10")]

    # ── attivazione per piattaforma: censiti vs con offerte. Rivela i
    # pozzi dormienti (lever) e gli ATS caldi (bamboohr, ashby) ──
    d["attivazione"] = [{"piattaforma": p, "censiti": t, "attivi": a}
        for p, t, a in _righe(ats_dsn,
            "SELECT platform_id, count(*), "
            "count(*) FILTER (WHERE job_count>0) "
            "FROM ats_companies GROUP BY 1 ORDER BY 2 DESC LIMIT 12")]

    # ── nuove aziende scoperte per giorno (30 gg): l'ampiezza che cresce
    d["nuove_aziende"] = [{"giorno": g[8:10] + "/" + g[5:7], "n": n}
        for g, n in _righe(ats_dsn,
            "SELECT to_char(date_trunc('day', created_at),'YYYY-MM-DD'), "
            "count(*) FROM ats_companies "
            "WHERE created_at > now()-interval '30 days' "
            "GROUP BY 1 ORDER BY 1")]

    # ── throughput: offerte toccate per ora nelle ultime 24h ──
    d["raccolta_oraria"] = [{"ora": o[11:16], "n": n}
        for o, n in _righe(ats_dsn,
            "SELECT to_char(date_trunc('hour', fetched_at),'YYYY-MM-DD HH24:MI'),"
            " count(*) FROM ats_jobs "
            "WHERE fetched_at > now()-interval '24 hours' "
            "GROUP BY 1 ORDER BY 1")]

    # ── board live: le offerte piu' recenti per data di pubblicazione,
    # col tempo relativo ("2 min fa"), come il flusso continuo di Fantastic.
    d["live"] = []
    for t, slug, p, loc, city, cty, u, pa, raw, logo_az, dom in _righe(ats_dsn, """
        SELECT j.title, j.slug, j.platform_id, j.location, j.city, j.country,
               j.url, j.posted_at, j.raw, ac.logo_url, ac.logo_domain
          FROM ats_jobs j
          LEFT JOIN ats_companies ac
                 ON ac.platform_id = j.platform_id AND ac.slug = j.slug
         WHERE j.expired_at IS NULL AND j.posted_at IS NOT NULL
           AND j.posted_at <= now()
         ORDER BY j.posted_at DESC LIMIT 30"""):
        azienda = None
        logo = logo_az   # logo a livello azienda (og:image / consolidato)
        favicon = (f"https://www.google.com/s2/favicons?sz=64&domain={dom}"
                   if dom else None)
        if isinstance(raw, dict):
            co = raw.get("company") or raw.get("hiringOrganization")
            if isinstance(co, dict):
                azienda = co.get("name") or co.get("title")
            elif isinstance(co, str):
                azienda = co
            # logo per-offerta (ripiego se manca quello a livello azienda)
            if not logo:
                ho = raw.get("hiringOrganization")
                for cand in (raw.get("company_logo"), raw.get("logo"),
                             raw.get("Company_LogoUrl"), raw.get("logo_url"),
                             (co.get("logo_url") or co.get("logo")
                              if isinstance(co, dict) else None),
                             (ho.get("logo") if isinstance(ho, dict) else None),
                             raw.get("sharing_image")):
                    if isinstance(cand, str) and cand.startswith("http"):
                        logo = cand
                        break
        d["live"].append({
            "titolo": t, "azienda": azienda or slug, "fonte": p,
            "luogo": loc or city, "paese": cty, "url": u,
            "logo": logo, "favicon": favicon,
            "posted": str(pa) if pa else None})
    # offerte pubblicate nell'ultima ora / 24h: il polso del flusso
    d["salute"]["pubblicate_1h"] = _uno(ats_dsn,
        "SELECT count(*) FROM ats_jobs WHERE expired_at IS NULL "
        "AND posted_at > now()-interval '1 hour'") or 0
    d["salute"]["pubblicate_24h"] = _uno(ats_dsn,
        "SELECT count(*) FROM ats_jobs WHERE expired_at IS NULL "
        "AND posted_at > now()-interval '24 hours'") or 0

    d["salute"]["scadute"] = _uno(ats_dsn,
        "SELECT count(*) FROM ats_jobs WHERE expired_at IS NOT NULL") or 0
    d["salute"]["paesi"] = _uno(ats_dsn,
        "SELECT count(DISTINCT country) FROM ats_jobs "
        "WHERE expired_at IS NULL AND country IS NOT NULL") or 0

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
.chart{display:flex;align-items:flex-end;gap:5px;height:150px;padding:14px 8px 0}
.col{flex:1;display:flex;flex-direction:column;align-items:center;justify-content:flex-end;height:100%}
.cbar{width:100%;max-width:30px;background:linear-gradient(180deg,var(--acc),#2a5cbf);
border-radius:4px 4px 0 0;min-height:2px}
.cx{font-size:10px;color:var(--dim);margin-top:6px;white-space:nowrap}
.usr{width:100%;border-collapse:collapse;font-size:13px}
.usr th,.usr td{text-align:left;padding:11px 13px;border-bottom:1px solid var(--line);vertical-align:top}
.usr th{font-size:11px;text-transform:uppercase;letter-spacing:.05em;color:var(--dim);font-weight:600}
.usr tr:last-child td{border-bottom:none}
.usr .em{color:var(--dim);font-size:12px}
.ch{display:inline-block;font-size:11px;font-weight:500;padding:2px 9px;border-radius:7px;
margin:2px 4px 2px 0;background:var(--card2);border:1px solid var(--line);color:var(--dim)}
.ch.email{color:#82b1ff;border-color:rgba(130,177,255,.3)}
.ch.telegram{color:#4cc4ff;border-color:rgba(76,196,255,.3)}
.ch.whatsapp{color:#5fd67a;border-color:rgba(95,214,122,.3)}
.ch.rc{color:var(--ink)}
.lcwrap{padding:12px 8px 2px}
svg.lc{width:100%;height:auto;display:block;overflow:visible}
svg.lc .grid{stroke:var(--line);stroke-width:1;vector-effect:non-scaling-stroke}
svg.lc .area{stroke:none}
svg.lc .ln{fill:none;stroke:var(--acc);stroke-width:2.2;vector-effect:non-scaling-stroke;stroke-linejoin:round;stroke-linecap:round}
svg.lc .pt{fill:var(--card);stroke:var(--acc);stroke-width:2;cursor:pointer;transition:r .12s ease}
svg.lc .pt:hover{r:6}
svg.lc .pt.sel{fill:var(--acc);r:6.5}
svg.lc .xl{fill:var(--dim);font-size:11px;text-anchor:middle}
svg.lc .yl{fill:var(--dim);font-size:10px;text-anchor:end}
.det{border-top:1px solid var(--line);margin-top:8px;padding:6px 6px 4px}
.dethd{font-size:13px;color:var(--ink);padding:8px 10px 6px;font-weight:500}
.dethd b{font-variant-numeric:tabular-nums;color:var(--acc)}
.hint{font-size:12px;color:var(--dim);padding:10px;opacity:.8}
.pend .num{color:var(--warn)}
.pend{border-color:rgba(224,161,50,.32)}
.pl{display:flex;align-items:center;gap:12px;padding:9px 14px;border-bottom:1px solid var(--line)}
.pl:last-child{border-bottom:none}
.pl .k{flex:1;font-size:14px;font-weight:600;text-transform:capitalize}
.pl .badge{font-size:11px;color:var(--warn);background:rgba(224,161,50,.12);
border:1px solid rgba(224,161,50,.3);border-radius:20px;padding:2px 10px;font-weight:600}
.pl .v{font-size:13px;color:var(--dim);font-variant-numeric:tabular-nums;min-width:96px;text-align:right}
/* ── KPI hero ── */
.kpis{display:grid;gap:14px;grid-template-columns:repeat(auto-fit,minmax(200px,1fr));margin-bottom:6px}
.kpi{background:linear-gradient(155deg,var(--card2),var(--card));border:1px solid var(--line);
border-radius:16px;padding:20px 20px 18px;position:relative;overflow:hidden}
.kpi:before{content:"";position:absolute;inset:0 auto 0 0;width:3px;background:var(--acc)}
.kpi.g:before{background:var(--ok)}.kpi.o:before{background:var(--warn)}.kpi.p:before{background:#9a7bff}
.kpi .n{font-size:34px;font-weight:700;letter-spacing:-.03em;font-variant-numeric:tabular-nums;line-height:1.05}
.kpi .l{font-size:12.5px;color:var(--dim);margin-top:6px;font-weight:500}
.sect{display:flex;align-items:baseline;justify-content:space-between;gap:12px;margin:34px 0 14px}
.sect h2{margin:0}
.sect .note{font-size:12px;color:var(--dim);font-weight:500}
/* ── barre verticali (bar chart) ── */
.bars{display:flex;align-items:flex-end;gap:6px;height:140px;padding:16px 10px 0}
.bcol{flex:1;display:flex;flex-direction:column;align-items:center;justify-content:flex-end;height:100%;min-width:0}
.bbar{width:100%;max-width:26px;background:linear-gradient(180deg,var(--acc),#2a5cbf);
border-radius:4px 4px 0 0;min-height:2px;transition:opacity .12s}
.bbar.hot{background:linear-gradient(180deg,var(--warn),#b97c1e)}
.bbar.dim{background:linear-gradient(180deg,#33507f,#22344f)}
.bcol .bx{font-size:10px;color:var(--dim);margin-top:6px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;max-width:100%}
.bcol .bv{font-size:10px;color:var(--dim);margin-bottom:4px;font-variant-numeric:tabular-nums}
/* ── funnel / pipeline stages ── */
.stage{padding:9px 14px}
.stage .sh{display:flex;justify-content:space-between;font-size:13px;margin-bottom:6px}
.stage .sh .sk{font-weight:500}
.stage .sh .sv{font-weight:600;font-variant-numeric:tabular-nums}
.stbar{height:9px;background:#0c1117;border-radius:5px;overflow:hidden}
.stbar>i{display:block;height:100%;background:linear-gradient(90deg,#3a6fd8,var(--acc));border-radius:5px}
.stage.drop .sh .sv{color:var(--warn)}
/* ── barre doppie (attivazione) ── */
.db{padding:8px 14px}
.db .dh{display:flex;justify-content:space-between;font-size:13px;margin-bottom:5px}
.db .dh .dk{font-weight:500;text-transform:capitalize}
.db .dh .dv{font-size:12px;color:var(--dim);font-variant-numeric:tabular-nums}
.db .dt{height:8px;background:#0c1117;border-radius:5px;position:relative;overflow:hidden}
.db .dt .cen{position:absolute;left:0;top:0;height:100%;background:#2c3a52;border-radius:5px}
.db .dt .att{position:absolute;left:0;top:0;height:100%;background:linear-gradient(90deg,#3a6fd8,var(--acc));border-radius:5px}
.spark{width:100%;height:auto;display:block}
.spark .sa{fill:url(#sag);stroke:none}
.spark .sl{fill:none;stroke:var(--ok);stroke-width:2;vector-effect:non-scaling-stroke;stroke-linejoin:round}
.cols3{display:grid;gap:22px;grid-template-columns:1fr 1fr 1fr}
@media(max-width:900px){.cols3{grid-template-columns:1fr}}
/* ── board live delle offerte ── */
.feed{border:1px solid var(--line);border-radius:14px;overflow:hidden;background:var(--card)}
.fr{display:flex;align-items:center;gap:12px;padding:11px 15px;border-bottom:1px solid var(--line)}
.fr:last-child{border-bottom:none}
.fr:first-child{background:rgba(76,141,255,.05)}
.fr .fbadge{position:relative;width:34px;height:34px;border-radius:8px;flex-shrink:0;
display:flex;align-items:center;justify-content:center;font-size:14px;font-weight:600;
color:#fff;text-transform:uppercase;overflow:hidden;letter-spacing:0}
.fr .fbadge .flogo{position:absolute;inset:0;width:100%;height:100%;object-fit:contain;background:#fff}
.fr .ft{flex:1;min-width:0}
.fr .ftt{font-size:14px;font-weight:500;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.fr .ftt a{color:var(--ink);text-decoration:none}
.fr .ftt a:hover{color:var(--acc)}
.fr .fm{font-size:12px;color:var(--dim);margin-top:2px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.fr .fg{font-size:11px;color:var(--dim);background:var(--card2);border:1px solid var(--line);border-radius:6px;padding:2px 8px;white-space:nowrap}
.fr .fa{font-size:12px;color:var(--ok);font-variant-numeric:tabular-nums;white-space:nowrap;min-width:78px;text-align:right;display:flex;align-items:center;gap:6px;justify-content:flex-end}
.fr .fa.old{color:var(--dim)}
.fr .fa .fd{width:6px;height:6px;border-radius:50%;background:var(--ok)}
.fr .fa.old .fd{background:var(--dim)}
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
let _and=[];
function mostraDettaglio(i){
 const r=_and[i];if(!r)return;
 document.querySelectorAll('.pt').forEach(p=>p.classList.remove('sel'));
 const el=document.getElementById('pt'+i);if(el)el.classList.add('sel');
 const det=document.getElementById('det');if(!det)return;
 const dt=r.dettaglio||[];const max=Math.max(...dt.map(x=>x.n),1);
 det.innerHTML=`<div class="dethd">${r.giorno} — <b>${IT(r.offerte)}</b> offerte pubblicate, per fonte:</div>`
  +(dt.length?dt.map(x=>`<div class="row"><div class="k">${x.fonte}</div>`
   +`<div class="track"><i style="width:${Math.round(100*x.n/max)}%"></i></div>`
   +`<div class="v">${IT(x.n)}</div></div>`).join(''):'<div class="hint">nessun dettaglio</div>')}
function grafico(rows){
 if(!rows||!rows.length)return '<div class="hint">nessun dato di andamento</div>';
 _and=rows;
 const W=980,H=220,pl=42,pr=14,pt=14,pb=26;
 const n=rows.length,max=Math.max(...rows.map(r=>r.offerte),1);
 const X=i=>pl+(W-pl-pr)*(n<=1?0.5:i/(n-1));
 const Y=v=>H-pb-(H-pt-pb)*(v/max);
 const P=rows.map((r,i)=>[X(i),Y(r.offerte)]);
 const line=P.map((p,i)=>(i?'L':'M')+p[0].toFixed(1)+' '+p[1].toFixed(1)).join(' ');
 const area=`M${X(0).toFixed(1)} ${(H-pb).toFixed(1)} `+P.map(p=>'L'+p[0].toFixed(1)+' '+p[1].toFixed(1)).join(' ')+` L${X(n-1).toFixed(1)} ${(H-pb).toFixed(1)} Z`;
 let g='';
 for(let k=0;k<=3;k++){const y=pt+(H-pt-pb)*k/3,val=Math.round(max*(3-k)/3);
  g+=`<line class="grid" x1="${pl}" y1="${y.toFixed(1)}" x2="${W-pr}" y2="${y.toFixed(1)}"/>`
   +`<text class="yl" x="${pl-6}" y="${(y+3).toFixed(1)}">${IT(val)}</text>`}
 const step=Math.max(1,Math.ceil(n/9));
 let xl='';rows.forEach((r,i)=>{if(i%step===0||i===n-1)xl+=`<text class="xl" x="${X(i).toFixed(1)}" y="${H-8}">${r.giorno}</text>`});
 let pts='';rows.forEach((r,i)=>{pts+=`<circle id="pt${i}" class="pt" cx="${X(i).toFixed(1)}" cy="${Y(r.offerte).toFixed(1)}" r="4" onclick="mostraDettaglio(${i})"><title>${r.giorno}: ${IT(r.offerte)} offerte</title></circle>`});
 const svg=`<svg class="lc" viewBox="0 0 ${W} ${H}">`
  +`<defs><linearGradient id="ag" x1="0" y1="0" x2="0" y2="1">`
  +`<stop offset="0" stop-color="#4c8dff" stop-opacity=".28"/><stop offset="1" stop-color="#4c8dff" stop-opacity="0"/></linearGradient></defs>`
  +g+`<path class="area" d="${area}" fill="url(#ag)"/><path class="ln" d="${line}"/>`+pts+xl+`</svg>`;
 return `<div class="lcwrap">${svg}</div><div id="det" class="det"><div class="hint">clicca un punto del grafico per vedere le fonti dietro quel numero</div></div>`}
function pending(rows){
 if(!rows||!rows.length)return '<div class="hint" style="padding:14px">nessun ATS in attesa: tutte le piattaforme censite hanno un adattatore.</div>';
 return '<div class="panel">'+rows.map(r=>`<div class="pl"><div class="k">${r.piattaforma}</div>`
  +`<span class="badge">adattatore da scrivere</span>`
  +`<div class="v">${IT(r.aziende)} aziende</div></div>`).join('')+'</div>'}
function iscritti(rows){if(!rows||!rows.length)return '<div class="sub" style="padding:14px">nessun iscritto</div>';
 return '<div class="panel" style="overflow-x:auto;padding:0"><table class="usr">'+
 '<tr><th>Nome</th><th>Email</th><th>Lingua</th><th>Ricezione digest</th><th>Ricerche attive</th><th>Ultimo digest</th></tr>'+
 rows.map(u=>`<tr><td>${u.nome}</td><td class="em">${u.email}</td><td>${(u.lingua||'').toUpperCase()}</td>`+
  `<td>${(u.canali||[]).map(c=>`<span class="ch ${c}">${c}</span>`).join('')||'—'}</td>`+
  `<td>${u.ricerche&&u.ricerche.length?u.ricerche.map(r=>`<span class="ch rc">${r}</span>`).join(''):'<span class="sub">nessuna</span>'}</td>`+
  `<td class="em">${eta(u.ultimo_digest)}</td></tr>`).join('')+'</table></div>'}
function barre(rows,kk,vk,opt){opt=opt||{};
 if(!rows||!rows.length)return '<div class="hint" style="padding:14px">nessun dato</div>';
 const max=Math.max(...rows.map(r=>r[vk]),1),n=rows.length,step=Math.max(1,Math.ceil(n/10));
 return '<div class="panel"><div class="bars">'+rows.map((r,i)=>{
  const hh=Math.max(2,Math.round(100*r[vk]/max));
  const lbl=(n<=12||i%step===0||i===n-1)?r[kk]:'';
  const cls=r.caldo?'hot':(opt.dim?'dim':'');
  const val=(opt.val!==false&&n<=12)?`<div class="bv">${IT(r[vk])}</div>`:'';
  return `<div class="bcol">${val}<div class="bbar ${cls}" style="height:${hh}%"><title>${r[kk]}: ${IT(r[vk])}</title></div><div class="bx">${lbl}</div></div>`;
 }).join('')+'</div></div>'}
function pipeline(rows){if(!rows||!rows.length)return '';
 const max=Math.max(...rows.map(r=>r.n),1);
 return '<div class="panel">'+rows.map((r,i)=>{
  const w=Math.max(1.5,100*r.n/max),drop=i>0&&r.n<rows[i-1].n;
  return `<div class="stage ${drop?'drop':''}"><div class="sh"><span class="sk">${r.fase}</span><span class="sv">${IT(r.n)}</span></div><div class="stbar"><i style="width:${w}%"></i></div></div>`;
 }).join('')+'</div>'}
function attivazione(rows){if(!rows||!rows.length)return '<div class="hint" style="padding:14px">nessun dato</div>';
 const max=Math.max(...rows.map(r=>r.censiti),1);
 return '<div class="panel">'+rows.map(r=>{
  const pc=100*r.censiti/max,pa=100*r.attivi/max,rate=r.censiti?Math.round(100*r.attivi/r.censiti):0;
  return `<div class="db"><div class="dh"><span class="dk">${r.piattaforma}</span><span class="dv">${IT(r.attivi)} / ${IT(r.censiti)} · ${rate}%</span></div><div class="dt"><span class="cen" style="width:${pc}%"></span><span class="att" style="width:${pa}%"></span></div></div>`;
 }).join('')+'</div>'}
function sparkline(rows,vk){if(!rows||rows.length<2)return '<div class="hint" style="padding:14px">dati insufficienti</div>';
 const W=980,H=88,pt=8,pb=8,n=rows.length,max=Math.max(...rows.map(r=>r[vk]),1);
 const X=i=>W*(i/(n-1)),Y=v=>H-pb-(H-pt-pb)*(v/max);
 const P=rows.map((r,i)=>[X(i),Y(r[vk])]);
 const ln=P.map((p,i)=>(i?'L':'M')+p[0].toFixed(1)+' '+p[1].toFixed(1)).join(' ');
 const ar=`M0 ${H-pb} `+P.map(p=>'L'+p[0].toFixed(1)+' '+p[1].toFixed(1)).join(' ')+` L${W} ${H-pb} Z`;
 const last=rows[n-1];
 return `<div class="lcwrap"><svg class="spark" viewBox="0 0 ${W} ${H}" preserveAspectRatio="none" style="height:88px"><defs><linearGradient id="sag" x1="0" y1="0" x2="0" y2="1"><stop offset="0" stop-color="#46c46a" stop-opacity=".3"/><stop offset="1" stop-color="#46c46a" stop-opacity="0"/></linearGradient></defs><path class="sa" d="${ar}"/><path class="sl" d="${ln}"/></svg></div><div class="hint">picco ${IT(max)}/ora · ultima ora ${IT(last.n)} (${last.ora})</div>`}
const esc=s=>(s==null?'':String(s)).replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
const hashS=s=>{let h=0;for(let i=0;i<s.length;i++)h=(h*31+s.charCodeAt(i))>>>0;return h};
function badge(azienda,logo,favicon){
 const nome=(azienda||'?').trim();
 const ini=esc(nome.charAt(0).toUpperCase()||'?');
 const col='hsl('+(hashS(nome)%360)+',42%,40%)';
 // catena: logo vero -> favicon del dominio -> monogramma (sotto)
 const src=logo||favicon;
 const alt=(logo&&favicon)?favicon:'';
 const img=src?`<img class="flogo" src="${esc(src)}" data-alt="${esc(alt)}" loading="lazy" referrerpolicy="no-referrer" onerror="if(this.dataset.alt){this.src=this.dataset.alt;this.dataset.alt='';}else{this.remove();}">`:'';
 return `<div class="fbadge" style="background:${col}">${ini}${img}</div>`;
}
function feed(rows){if(!rows||!rows.length)return '<div class="hint" style="padding:14px">nessuna offerta con data recente</div>';
 return '<div class="feed">'+rows.map(r=>{
  const s=(Date.now()-new Date(r.posted))/1000,old=s>86400;
  const lg=r.luogo||'',pa=r.paese||'';
  const loc=(lg&&pa&&!lg.toUpperCase().includes(pa.toUpperCase()))?lg+' · '+pa:(lg||pa);
  return `<div class="fr">${badge(r.azienda,r.logo,r.favicon)}<div class="ft"><div class="ftt"><a href="${esc(r.url)}" target="_blank" rel="noopener">${esc(r.titolo)}</a></div>`
   +`<div class="fm">${esc(r.azienda)}${loc?' · '+esc(loc):''}</div></div>`
   +`<span class="fg">${esc(r.fonte)}</span><span class="fa ${old?'old':''}"><span class="fd"></span>${eta(r.posted)}</span></div>`;
 }).join('')+'</div>'}
async function tick(){
 let d;try{const r=await fetch('/cruscotto/dati');if(!r.ok)throw 0;d=await r.json()}
 catch(e){document.getElementById('ts').textContent='non raggiungibile';return}
 const s=d.stato,h=d.salute,m=d.motore||{};
 document.getElementById('ts').textContent='aggiornato alle '+new Date().toLocaleTimeString('it-IT');
 const clP=h.senza_paese_pct>60?'bad':h.senza_paese_pct>30?'warn':'ok';
 const clC=h.non_classificate_pct>40?'bad':h.non_classificate_pct>20?'warn':'ok';
 const clF=h.codone_freschi_pct<85?'bad':h.codone_freschi_pct<95?'warn':'ok';
 let H='<div class="kpis">'
 +`<div class="kpi"><div class="n">${IT(h.offerte_attive)}</div><div class="l">offerte attive · ${IT(h.paesi)} paesi</div></div>`
 +`<div class="kpi g"><div class="n">${IT(h.aziende_con_offerte)}</div><div class="l">aziende con offerte vive</div></div>`
 +`<div class="kpi o"><div class="n">${IT(h.piattaforme)}</div><div class="l">piattaforme ATS attive</div></div>`
 +`<div class="kpi p"><div class="n">${IT(m.utenti)}</div><div class="l">iscritti · ${IT(m.cluster_attivi)} ricerche attive</div></div>`
 +'</div>'
 +`<div class="sect"><h2>Offerte in arrivo — live</h2><span class="note">${IT(h.pubblicate_1h)} pubblicate nell\\'ultima ora · ${IT(h.pubblicate_24h)} nelle 24h</span></div>`
 +feed(d.live)
 +'<div class="sect"><h2>In tempo reale</h2><span class="note">aggiornamento ogni 30s</span></div><div class="grid">'
 +card(IT(s.offerte_viste_24h),'raccolte o aggiornate · 24h')
 +card(IT(s.aziende_scrapate_1h),'aziende visitate · ultima ora')
 +card(eta(s.ultimo_scrape),'ultima raccolta')
 +card(eta(s.ultima_classificazione),'ultima classificazione')
 +card(eta(s.ultimo_ponte),'ultimo travaso al motore')
 +'</div>'
 +'<div class="sect"><h2>Throughput — offerte toccate per ora (24h)</h2></div>'
 +'<div class="panel">'+sparkline(d.raccolta_oraria,'n')+'</div>'
 +'<div class="sect"><h2>Andamento — offerte pubblicate per giorno (30gg)</h2><span class="note">clicca un punto per le fonti</span></div>'
 +'<div class="panel">'+grafico(d.andamento)+'</div>'
 +'<div class="sect"><h2>Nuove aziende scoperte per giorno</h2><span class="note">l\\'ampiezza che cresce</span></div>'
 +barre(d.nuove_aziende,'giorno','n',{val:false})
 +'<div class="cols">'
 +'<div><div class="sect"><h2>La pipeline della raffineria</h2></div>'+pipeline(d.funnel)+'</div>'
 +`<div><div class="sect"><h2>Freschezza del codone</h2><span class="note ${clF}">${h.codone_freschi_pct}% entro 12h</span></div>`+barre(d.freschezza,'fascia','n')+'</div>'
 +'</div>'
 +'<div class="sect"><h2>Salute della raffineria</h2></div><div class="grid">'
 +card(`<span class="${clP}">${h.senza_paese_pct}%</span>`,'offerte senza paese',IT(h.senza_paese)+' su '+IT(h.offerte_attive))
 +card(`<span class="${clC}">${h.non_classificate_pct}%</span>`,'offerte non classificate',IT(h.non_classificate)+' su '+IT(h.offerte_attive))
 +card(`<span class="${clF}">${h.codone_freschi_pct}%</span>`,'codone fresco (entro 12h)',IT(h.codone_oltre_12h)+' oltre 12h · '+IT(h.codone_oltre_24h)+' oltre 24h')
 +card(IT(h.aziende_censite),'aziende censite')
 +card(IT(h.aziende_mai_viste),'ancora da visitare')
 +card(IT(h.scadute),'offerte scadute (storico)')
 +`<div class="card pend"><div class="num">${IT(h.ats_pending_n)}</div><div class="lbl">ATS da aggiungere</div>${h.ats_pending_n?`<div class="sub">${IT(h.ats_pending_aziende)} aziende senza adattatore</div>`:'<div class="sub">tutte le piattaforme coperte</div>'}</div>`
 +'</div>'
 +'<div class="sect"><h2>Attivazione per piattaforma</h2><span class="note">attivi / censiti — pozzi dormienti e ATS caldi</span></div>'
 +attivazione(d.attivazione)
 +'<div class="cols"><div><div class="sect"><h2>Offerte per fonte</h2></div>'+lista(d.per_fonte,'fonte','attive')+'</div>'
 +'<div><div class="sect"><h2>Offerte per paese</h2></div>'+lista(d.per_paese,'paese','attive')+'</div></div>'
 +'<div class="cols"><div><div class="sect"><h2>Offerte per famiglia professionale</h2></div>'+lista(d.per_famiglia,'famiglia','attive')+'</div>'
 +'<div><div class="sect"><h2>Come scopriamo i tenant</h2></div>'+lista(d.per_scoperta,'fonte','n')+'</div></div>'
 +(d.agenzie&&d.agenzie.length?'<div class="sect"><h2>Agenzie per il lavoro</h2></div>'+lista(d.agenzie,'agenzia','attive'):'')
 +(d.ats_pending&&d.ats_pending.length?'<div class="sect"><h2>ATS trovati ma non ancora raccoglibili</h2></div>'+pending(d.ats_pending):'')
 +'<div class="sect"><h2>Motore e ricerche degli iscritti</h2></div><div class="grid">'
 +card(IT(m.offerte_nel_motore),'offerte nel motore')
 +card(IT(m.da_ats),'dai nostri ATS diretti')
 +card(IT(m.cluster_attivi),'ricerche attive')
 +card(IT(m.utenti),'iscritti')
 +'</div>';
 if(d.cluster&&d.cluster.length)H+='<div style="margin-top:14px">'+d.cluster.map(c=>
  `<span class="pill ${c.stato==='active'?'on':''}">${c.famiglia} · ${c.paese}</span>`).join('')+'</div>';
 H+='<div class="sect"><h2>Iscritti — configurazione completa</h2></div>'+iscritti(d.iscritti);
 document.getElementById('app').innerHTML=H}
tick();setInterval(tick,30000);
</script></body></html>"""
