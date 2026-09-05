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
import re
import time
from urllib.parse import urlencode

import httpx
import psycopg

from nivult import oauth as _oauth

OPERATORE = os.environ.get("CRUSCOTTO_EMAIL", "g.ranno@outlook.com").lower()
# Venti minuti, non otto ore: un cookie rubato vale poco se muore in
# fretta, e rientrare costa due clic sul conto Microsoft gia' aperto.
DURATA = 20 * 60


# ── accesso: OAuth Microsoft, murato su una sola email ──────────────

def _redirect_uri() -> str:
    return _oauth.api_url() + "/cruscotto/callback"


def _chiave() -> bytes:
    """Il segreto che firma cookie e state. SENZA, niente: un ripiego
    scritto nel codice (e il codice e' su GitHub) equivale a nessuna
    firma — chiunque potrebbe fabbricarsi la sessione a tavolino."""
    segreto = os.environ.get("CRUSCOTTO_TOKEN", "")
    if len(segreto) < 32:
        raise RuntimeError("CRUSCOTTO_TOKEN assente o troppo corto: "
                           "il cruscotto resta chiuso.")
    return segreto.encode()


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
    # Il token deve essere emesso per NOI: un id_token di un'altra app,
    # comunque ottenuto, non apre questa porta.
    if claim.get("aud") != cid:
        return None
    # nOAuth: sugli account aziendali il claim email lo DECIDE
    # l'amministratore del tenant — chiunque crei un tenant gratuito
    # puo' scriversi l'email dell'operatore. La fiducia (tenant
    # consumer, dove l'email e' l'identita') non e' facoltativa qui:
    # scartarla significava lasciare la porta aperta.
    email, fidata = _oauth._email_attendibile("microsoft", claim)
    if not fidata:
        return None
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



# ── lo stato dei giri: demoni, allarmi, ponte, notturno ─────────────

# (unita' systemd, nome umano, spiegazione per chi non conosce Nivult)
_DEMONI = (
    ("scrape", "raccolta completa",
     "rivisita tutte le aziende censite e scarica le loro offerte"),
    ("scrape-veloce", "raccolta rapida",
     "ripassa spesso le aziende che hanno offerte attive"),
    ("profonda", "scoperta profonda",
     "setaccia gli archivi del web piattaforma per piattaforma"),
    ("scoperta", "scoperta archivi",
     "trova aziende nuove negli archivi storici del web"),
    ("classifica", "classificatore",
     "assegna a ogni offerta la famiglia professionale"),
    ("arricchisci", "arricchimento",
     "aggiunge salari, descrizioni, profilo AI, paesi e loghi"),
    ("volano", "scoperta continua",
     "rileva nuovi ATS, verifica i domini, elimina gli account morti"),
    ("certificati", "radar certificati",
     "ascolta i certificati HTTPS del mondo e coglie i career site nuovi"),
    ("api", "sito e cruscotto",
     "risponde al sito, a questa pagina e ai webhook"),
)


def _giri() -> dict:
    """Come stanno i giri — con gli errori, non solo i verdi: demoni
    systemd, allarmi aperti della sentinella, esito dell'ultima corsa
    del ponte e del giro notturno (contando i passi FALLITO)."""
    import json as _json
    import subprocess
    g: dict = {"demoni": [], "allarmi": [], "sentinella": False,
               "ponte": {}, "notturno": {}}
    for dm, etichetta, spiega in _DEMONI:
        try:
            r = subprocess.run(["systemctl", "is-active", "nivult-" + dm],
                               capture_output=True, text=True, timeout=5)
            attivo = r.stdout.strip() == "active"
        except Exception:                            # noqa: BLE001
            attivo = False
        g["demoni"].append({"nome": dm, "etichetta": etichetta,
                            "spiega": spiega, "ok": attivo})
    try:
        st = _json.load(open("/opt/nivult/sentinella-stato.json"))
        g["allarmi"] = [{"testo": k, "da": v} for k, v
                        in sorted(st.get("attivi", {}).items())]
        g["sentinella"] = True
    except Exception:                                # noqa: BLE001
        pass
    try:
        percorso = "/var/log/nivult-ponte-ats.log"
        g["ponte"]["eta_min"] = int(
            (time.time() - os.path.getmtime(percorso)) // 60)
        coda = open(percorso, errors="replace").read()[-6000:]
        g["ponte"]["errore"] = "ERRORE" in coda.rsplit("=== fine ===", 1)[-1]
    except OSError:
        g["ponte"] = {"eta_min": None, "errore": None}
    try:
        log = open("/opt/nivult/engine/logs/ats-cron.log",
                   errors="replace").read()[-40000:]
        avvii = re.findall(r"=== ATS nightly (20\S+) ===", log)
        blocco = log.rsplit("=== ATS nightly 20", 1)[-1]
        passi = re.findall(r"── ([^\n]+)\n\s+(ok|FALLITO)", blocco)
        falliti = [n.split("(")[0].strip() for n, esito in passi
                   if esito == "FALLITO"]
        g["notturno"] = {"quando": avvii[-1] if avvii else None,
                         "falliti": len(falliti),
                         "passi_falliti": falliti[:6],
                         "completato": "completato" in blocco}
    except OSError:
        pass
    return g


# ── copertura degli arricchimenti (query pesanti: cache 10 minuti) ──

_CACHE_ARR: dict = {"t": 0.0, "v": []}


def _arricchimento(ats_dsn: str, attive: int) -> list[dict]:
    if time.time() - _CACHE_ARR["t"] < 600 and _CACHE_ARR["v"]:
        return _CACHE_ARR["v"]
    r = _righe(ats_dsn, """
        SELECT count(country), count(salary_min), count(seniority),
               count(remote),
               count(*) FILTER (WHERE skills IS NOT NULL
                                  AND array_length(skills, 1) > 0),
               count(*) FILTER (WHERE raw ?| array['description',
                   'descriptionHtml','jobDescription','job_description',
                   'content','externalDescription','descriptionPlain']),
               count(employment_type), count(contact_email)
          FROM ats_jobs WHERE expired_at IS NULL""")[0]
    logo = _righe(ats_dsn, """
        SELECT count(*) FILTER (WHERE logo_url IS NOT NULL
                                   OR logo_domain IS NOT NULL), count(*)
          FROM ats_companies WHERE is_active AND job_count > 0""")[0]
    campi = [("paese", r[0]), ("descrizione", r[5]), ("seniority", r[2]),
             ("lavoro remoto", r[3]), ("competenze", r[4]),
             ("tipo di contratto", r[6]), ("contatto nell'annuncio", r[7]),
             ("salario", r[1])]
    v = [{"campo": k, "n": n,
          "pct": round(100 * n / attive, 1) if attive else 0}
         for k, n in campi]
    v.append({"campo": "logo — aziende vive", "n": logo[0],
              "pct": round(100 * logo[0] / logo[1], 1) if logo[1] else 0})
    _CACHE_ARR.update(t=time.time(), v=v)
    return v


# ── la parte pesante: distribuzioni e censimento, cache 4 minuti ────
# Le query che scansionano tutta ats_jobs non hanno bisogno di girare a
# ogni tick da 30s: il censimento non cambia in 4 minuti. La parte viva
# (feed, battiti, demoni) resta fresca a ogni chiamata.

_CACHE_PES: dict = {"t": 0.0, "v": None, "in_corso": False}
_CACHE_FILE = "/opt/nivult/cruscotto-cache.json"


def _carica_cache_da_disco() -> None:
    """Un riavvio dell'API non deve ripartire freddo: la parte pesante
    salvata dall'ultimo giro si ricarica dal disco se ha meno di 15
    minuti — la pagina apre subito, il ricalcolo avviene in sfondo.
    (Successo: riavvio + database sotto tre passate = minuti di pagina
    bianca e Giuseppe convinto che fosse rotta.)"""
    import json as _json
    if _CACHE_PES["v"] is not None:
        return
    try:
        d = _json.load(open(_CACHE_FILE))
        if time.time() - d["t"] < 900:
            _CACHE_PES.update(t=d["t"], v=d["v"])
    except Exception:                                # noqa: BLE001
        pass


def _pesanti(ats_dsn: str, attive: int) -> dict:
    """Cache con ricalcolo in sfondo: il tick non aspetta mai. Se la
    cache e' scaduta si serve comunque la versione vecchia e un thread
    la rinfresca; solo la primissima chiamata (cache vuota) blocca."""
    import threading
    _carica_cache_da_disco()
    if _CACHE_PES["v"] is not None:
        if time.time() - _CACHE_PES["t"] >= 240 and not _CACHE_PES["in_corso"]:
            _CACHE_PES["in_corso"] = True
            threading.Thread(target=_ricalcola_pesanti,
                             args=(ats_dsn, attive), daemon=True).start()
        return _CACHE_PES["v"]
    if _CACHE_PES["in_corso"]:
        # un altro tick sta gia' calcolando: si aspetta il suo risultato
        for _ in range(240):
            time.sleep(0.5)
            if _CACHE_PES["v"] is not None:
                return _CACHE_PES["v"]
    _CACHE_PES["in_corso"] = True
    return _ricalcola_pesanti(ats_dsn, attive)


def _ricalcola_pesanti(ats_dsn: str, attive: int) -> dict:
    import json as _json
    try:
        d = _calcola_pesanti(ats_dsn, attive)
        _CACHE_PES.update(t=time.time(), v=d)
        try:
            with open(_CACHE_FILE + ".tmp", "w") as f:
                _json.dump({"t": _CACHE_PES["t"], "v": d}, f, default=str)
            os.replace(_CACHE_FILE + ".tmp", _CACHE_FILE)
        except Exception:                            # noqa: BLE001
            pass
        return d
    finally:
        _CACHE_PES["in_corso"] = False


def _calcola_pesanti(ats_dsn: str, attive: int) -> dict:
    d: dict = {}

    senza_paese = _uno(ats_dsn,
        "SELECT count(*) FROM ats_jobs WHERE expired_at IS NULL "
        "AND country IS NULL") or 0
    non_class = _uno(ats_dsn,
        "SELECT count(*) FROM ats_jobs j LEFT JOIN job_classifications c "
        "ON c.job_id=j.id WHERE c.job_id IS NULL AND j.expired_at IS NULL") or 0
    d["salute"] = {
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
        "scadute": _uno(ats_dsn,
            "SELECT count(*) FROM ats_jobs WHERE expired_at IS NOT NULL") or 0,
        "paesi": _uno(ats_dsn,
            "SELECT count(DISTINCT country) FROM ats_jobs "
            "WHERE expired_at IS NULL AND country IS NOT NULL") or 0,
    }

    # ── freschezza del codone: ogni tenant va rivisitato entro la soglia
    # (spazzino a 12h in runner.py); se le fasce lontane si gonfiano, lo
    # scraping si sta affamando.
    _fresh = _righe(ats_dsn, """
        SELECT
          count(*),
          count(*) FILTER (WHERE ac.last_fetch_at >= now()-interval '12 hours'
                              OR ac.last_fetch_at IS NULL),
          count(*) FILTER (WHERE ac.last_fetch_at < now()-interval '12 hours'),
          count(*) FILTER (WHERE ac.last_fetch_at < now()-interval '24 hours'),
          count(*) FILTER (WHERE ac.last_fetch_at IS NULL),
          count(*) FILTER (WHERE ac.last_fetch_at >= now()-interval '1 hour'),
          count(*) FILTER (WHERE ac.last_fetch_at <  now()-interval '1 hour'
                              AND ac.last_fetch_at >= now()-interval '6 hours'),
          count(*) FILTER (WHERE ac.last_fetch_at <  now()-interval '6 hours'
                              AND ac.last_fetch_at >= now()-interval '12 hours'),
          count(*) FILTER (WHERE ac.last_fetch_at <  now()-interval '12 hours'
                              AND ac.last_fetch_at >= now()-interval '24 hours')
          FROM ats_companies ac JOIN ats_platforms ap ON ap.id=ac.platform_id
         WHERE ac.is_active AND ap.is_active""")
    _tot, _fre, _o12, _o24, b0, b1, b2, b3, b4 = \
        _fresh[0] if _fresh else (0,) * 9
    d["salute"]["codone_totale"] = _tot
    d["salute"]["codone_oltre_12h"] = _o12
    d["salute"]["codone_oltre_24h"] = _o24
    d["salute"]["codone_freschi_pct"] = \
        round(100 * _fre / _tot, 1) if _tot else 100.0
    _fasce = ["mai", "< 1h", "1–6h", "6–12h", "12–24h", "> 24h"]
    _val = [b0, b1, b2, b3, b4, _o24]
    d["freschezza"] = [{"fascia": _fasce[i], "n": _val[i], "caldo": i >= 4}
                       for i in range(6)]

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
    d["agenzie"] = [{"agenzia": a, "attive": n} for a, n in _righe(ats_dsn,
        "SELECT slug, count(*) FROM ats_jobs "
        "WHERE platform_id='agenzie' AND expired_at IS NULL "
        "GROUP BY 1 ORDER BY 2 DESC")]

    # ── ATS censiti ma senza adattatore: la coda di lavoro delle nuove
    # piattaforme.
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

    # ── andamento: pubblicate per giorno (30 gg), col dettaglio per fonte
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
            "giorno": g[8:10] + "/" + g[5:7],
            "data": g,
            "offerte": v["offerte"],
            "dettaglio": [{"fonte": p, "n": n} for p, n in fonti]})

    # I codici interni delle fonti, tradotti per chi legge: la pagina
    # deve essere chiara anche a chi non sa cosa sia un "detector".
    _FONTI_UMANE = {
        "archivio": "archivi del web (Wayback/CC)",
        "detector": "rilevatore sui domini",
        "profonda": "scoperta profonda",
        "common_crawl": "Common Crawl diretto",
        "existing_db": "censimento iniziale",
        "production_url": "dagli URL delle offerte",
        "production_full": "dagli URL delle offerte",
        "production": "dagli URL delle offerte",
        "dns_enumeration": "enumerazione DNS",
        "reverse": "riscoperta dalle offerte",
        "vanity": "risolutore domini propri",
        "certificati": "radar certificati",
        "grafo_cc": "grafo dei link",
        "http_archive": "censimento tecnologico",
        "wikidata": "Wikidata",
        "wikidata_big": "Wikidata (grandi aziende)",
        "gleif": "anagrafe GLEIF",
        "research": "ricerca manuale",
        "igiene": "igiene del censimento",
        "feed": "feed dedicati",
    }
    d["per_scoperta"] = [
        {"fonte": _FONTI_UMANE.get(f, f or "—"), "n": n}
        for f, n in _righe(ats_dsn,
            "SELECT discovered_from, count(*) FROM ats_companies "
            "GROUP BY 1 ORDER BY 2 DESC LIMIT 10")]

    # ── attivazione: SOLO il censimento vivo su piattaforme raccolte. I
    # tenant potati (slug morti) non fanno testo, altrimenti il numero
    # mente al ribasso.
    d["attivazione"] = [{"piattaforma": p, "censiti": v, "attivi": a,
                         "potati": m}
        for p, v, a, m in _righe(ats_dsn, """
            SELECT ac.platform_id,
                   count(*) FILTER (WHERE ac.is_active),
                   count(*) FILTER (WHERE ac.is_active AND ac.job_count>0),
                   count(*) FILTER (WHERE NOT ac.is_active)
              FROM ats_companies ac
              JOIN ats_platforms ap ON ap.id = ac.platform_id
             WHERE ap.is_active
             GROUP BY 1 ORDER BY 2 DESC LIMIT 14""")]

    d["nuove_aziende"] = [{"giorno": g[8:10] + "/" + g[5:7], "n": n}
        for g, n in _righe(ats_dsn,
            "SELECT to_char(date_trunc('day', created_at),'YYYY-MM-DD'), "
            "count(*) FROM ats_companies "
            "WHERE created_at > now()-interval '30 days' "
            "GROUP BY 1 ORDER BY 1")]

    # perso nella divisione leggere/pesanti e ripescato: senza, il
    # grafico orario mostrava "dati insufficienti" con la nota piena
    d["raccolta_oraria"] = [{"ora": o[11:16], "n": n}
        for o, n in _righe(ats_dsn,
            "SELECT to_char(date_trunc('hour', fetched_at),"
            "'YYYY-MM-DD HH24:MI'), count(*) FROM ats_jobs "
            "WHERE fetched_at > now()-interval '24 hours' "
            "GROUP BY 1 ORDER BY 1")]

    d["arricchimento"] = _arricchimento(ats_dsn, attive)

    # ── il magazzino da vendere: le grandezze che crescono da sole ──
    def _forse(sql):
        try:
            return _uno(ats_dsn, sql)
        except Exception:                            # noqa: BLE001
            return None
    d["magazzino"] = {
        "coppie_tecnografiche": _forse(
            "SELECT count(*) FROM azienda_skill"),
        "celle_benchmark": _forse(
            "SELECT count(*) FROM stipendi_benchmark"),
        "aziende_con_settore": _forse(
            "SELECT count(*) FROM ats_companies WHERE "
            "coalesce(industry_reg, industry, industry_site, "
            "industry_mix) IS NOT NULL"),
        "aziende_con_organico": _forse(
            "SELECT count(*) FROM ats_companies "
            "WHERE coalesce(employees_reg, employees_wd, "
            "employees_site, employees_self) IS NOT NULL"),
        "storico_chiuse": _forse(
            "SELECT count(*) FROM ats_jobs WHERE expired_at IS NOT NULL"),
        "paesi_memoria": _forse(
            "SELECT count(*) FROM azienda_paese"),
    }

    # ── completezza delle NUOVE (created_at esiste dal 04/09/26 sera:
    # lo spartiacque tiene fuori il backfill del default) ──
    r = _righe(ats_dsn, """
        SELECT count(*), count(country), count(lang),
               count(*) FILTER (WHERE raw ?| array['description',
                   'descriptionHtml','descriptionPlain','jobDescription',
                   'job_description','content','externalDescription']),
               count(*) FILTER (WHERE EXISTS (SELECT 1
                   FROM job_classifications x WHERE x.job_id = j.id))
          FROM ats_jobs j
         WHERE j.created_at > greatest(now() - interval '30 hours',
               '2026-09-04T20:30:00+00:00'::timestamptz)
           AND j.created_at < now() - interval '6 hours'
           AND j.expired_at IS NULL""")[0]
    tot = r[0] or 0
    d["nuove24"] = {"totale": tot} | {
        k: (round(100 * r[i] / tot, 1) if tot else 0)
        for i, k in ((1, "paese"), (2, "lingua"),
                     (3, "descrizione"), (4, "categoria"))}

    return d


def metriche(ats_dsn: str, motore_dsn: str) -> dict:
    d: dict = {}

    # ── parte viva: fresca a ogni tick ──
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

    # ── board live: le offerte piu' recenti per data di pubblicazione,
    # col tempo relativo, come il flusso continuo di Fantastic.
    d["live"] = []
    for t, slug, p, loc, city, cty, u, pa, raw, logo_az, dom in _righe(ats_dsn, """
        SELECT j.title, j.slug, j.platform_id, j.location, j.city, j.country,
               j.url, j.posted_at, j.raw, ac.logo_url,
               coalesce(ac.logo_domain, ac.site_domain)
          FROM ats_jobs j
          LEFT JOIN ats_companies ac
                 ON ac.platform_id = j.platform_id AND ac.slug = j.slug
         WHERE j.expired_at IS NULL AND j.posted_at IS NOT NULL
           AND j.posted_at <= now()
         ORDER BY j.posted_at DESC LIMIT 30"""):
        azienda = None
        logo = logo_az   # logo a livello azienda (og:image / consolidato)
        favicon = (f"https://www.google.com/s2/favicons?sz=128&domain={dom}"
                   if dom else None)
        if isinstance(raw, dict):
            co = raw.get("company") or raw.get("hiringOrganization")
            if isinstance(co, dict):
                azienda = co.get("name") or co.get("title")
            elif isinstance(co, str):
                azienda = co
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

    d["giri"] = _giri()

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
        d["cluster"] = [{"famiglia": f, "paese": p, "stato": st}
            for f, p, st in _righe(motore_dsn,
                "SELECT family, country, status FROM clusters "
                "ORDER BY status, family LIMIT 40")]
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

    # ── parte pesante (cache 4 minuti) ──
    pes = _pesanti(ats_dsn, attive)
    for k in ("per_fonte", "per_paese", "per_famiglia", "agenzie",
              "andamento", "freschezza", "per_scoperta", "attivazione",
              "nuove_aziende", "ats_pending", "arricchimento", "nuove24",
              "raccolta_oraria", "magazzino"):
        d[k] = pes[k]
    d["salute"] = dict(pes["salute"])
    d["salute"]["offerte_attive"] = attive

    d["salute"]["pubblicate_1h"] = _uno(ats_dsn,
        "SELECT count(*) FROM ats_jobs WHERE expired_at IS NULL "
        "AND posted_at > now()-interval '1 hour' AND posted_at <= now() "
        "AND NOT coalesce(posted_at_estimated, false)") or 0
    d["salute"]["pubblicate_24h"] = _uno(ats_dsn,
        "SELECT count(*) FROM ats_jobs WHERE expired_at IS NULL "
        "AND posted_at > now()-interval '24 hours' AND posted_at <= now() "
        "AND NOT coalesce(posted_at_estimated, false)") or 0

    # ── funnel: da azienda censita a offerta consegnata ──
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

    return d


# ── la porta d'ingresso: una pagina, un bottone, una sola email ─────
# L'estetica e' della casa; la serratura resta l'OAuth Microsoft murato
# su CRUSCOTTO_EMAIL. Questa pagina non sa niente e non mostra niente:
# puo' vederla chiunque, apre solo per l'operatore.

LOGIN_PAGINA = """<!doctype html><html lang="it"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="robots" content="noindex,nofollow,noarchive">
<title>Nivult</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&display=swap" rel="stylesheet">
<style>
:root{--bg:#0b0f14;--card:#141a22;--card2:#1a212b;--line:#242c37;--ink:#e8eef5;
--dim:#8a97a6;--acc:#4c8dff;--ok:#46c46a;--bad:#ff5d54}
*{box-sizing:border-box;margin:0;padding:0}
body{background:var(--bg);color:var(--ink);min-height:100vh;display:flex;
align-items:center;justify-content:center;padding:24px;
font:15px/1.55 Inter,-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif;
-webkit-font-smoothing:antialiased;position:relative;overflow:hidden}
body:before{content:"";position:absolute;width:720px;height:720px;left:50%;top:-320px;
transform:translateX(-50%);border-radius:50%;pointer-events:none;
background:radial-gradient(circle,rgba(76,141,255,.14),transparent 62%)}
.card{position:relative;width:100%;max-width:400px;background:linear-gradient(165deg,var(--card2),var(--card));
border:1px solid var(--line);border-radius:20px;padding:44px 38px 36px;text-align:center;
box-shadow:0 24px 70px rgba(0,0,0,.45)}
.brand{font-size:30px;font-weight:700;letter-spacing:-.03em}
.brand i{font-style:normal;color:var(--acc)}
.vivo{display:inline-flex;align-items:center;gap:7px;font-size:12px;color:var(--dim);
margin-top:18px;padding:5px 13px;border:1px solid var(--line);border-radius:20px;background:var(--bg)}
.vivo .dot{width:7px;height:7px;border-radius:50%;background:var(--ok);
box-shadow:0 0 0 0 rgba(70,196,106,.5);animation:pulse 2.2s infinite}
@keyframes pulse{0%{box-shadow:0 0 0 0 rgba(70,196,106,.45)}70%{box-shadow:0 0 0 7px rgba(70,196,106,0)}100%{box-shadow:0 0 0 0 rgba(70,196,106,0)}}
@media(prefers-reduced-motion:reduce){.vivo .dot{animation:none}}
.entra{display:flex;align-items:center;justify-content:center;gap:11px;width:100%;
margin-top:28px;padding:13px 18px;border-radius:12px;border:1px solid var(--line);
background:var(--bg);color:var(--ink);font:600 14.5px Inter,sans-serif;cursor:pointer;
text-decoration:none;transition:border-color .15s,background .15s}
.entra:hover{border-color:var(--acc);background:#0e1520}
.entra:focus-visible{outline:2px solid var(--acc);outline-offset:2px}
.entra svg{flex-shrink:0}
.negato{margin-top:20px;font-size:13px;color:var(--bad);background:rgba(255,93,84,.08);
border:1px solid rgba(255,93,84,.35);border-radius:10px;padding:10px 14px;display:none}
.negato.si{display:block}
.piede{position:absolute;bottom:18px;left:0;right:0;text-align:center;
font-size:11.5px;color:var(--dim);opacity:.7}
</style></head><body>
<div class="card">
  <div class="brand">Nivult<i>.</i></div>
  <div class="vivo"><span class="dot"></span>portale operativo</div>
  <a class="entra" href="/cruscotto/entra">
    <svg width="17" height="17" viewBox="0 0 21 21" aria-hidden="true">
      <rect x="0" y="0" width="10" height="10" fill="#f25022"/>
      <rect x="11" y="0" width="10" height="10" fill="#7fba00"/>
      <rect x="0" y="11" width="10" height="10" fill="#00a4ef"/>
      <rect x="11" y="11" width="10" height="10" fill="#ffb900"/>
    </svg>
    Entra con Microsoft
  </a>
  <div id="negato" class="negato">Questo account non è autorizzato.</div>
</div>
<script>
if(new URLSearchParams(location.search).has('negato'))
  document.getElementById('negato').classList.add('si');
</script></body></html>"""


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
html{scroll-behavior:smooth}
body{background:var(--bg);color:var(--ink);
font:15px/1.55 Inter,-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif;
padding:0 24px 64px;max-width:1180px;margin:0 auto;-webkit-font-smoothing:antialiased}
.top{position:sticky;top:0;z-index:10;display:flex;justify-content:space-between;
align-items:center;gap:14px;padding:16px 0 14px;border-bottom:1px solid var(--line);
margin-bottom:20px;background:rgba(11,15,20,.93);backdrop-filter:blur(8px)}
.brand{display:flex;align-items:baseline;gap:10px}
.brand b{font-size:20px;font-weight:700;letter-spacing:-.02em}
.brand span{font-size:13px;color:var(--dim);font-weight:500}
.nav{display:flex;gap:2px}
.nav a{font-size:12.5px;color:var(--dim);text-decoration:none;padding:6px 11px;
border-radius:8px;font-weight:500}
.nav a:hover{color:var(--ink);background:var(--card2)}
@media(max-width:760px){.nav{display:none}}
.live{font-size:12px;color:var(--dim);display:flex;align-items:center;gap:10px}
.esci{font-size:12px;font-weight:500;color:var(--dim);text-decoration:none;
padding:5px 12px;border:1px solid var(--line);border-radius:8px;white-space:nowrap}
.esci:hover{color:var(--ink);border-color:var(--bad)}
.dot{width:8px;height:8px;border-radius:50%;background:var(--ok);
box-shadow:0 0 0 0 rgba(70,196,106,.5);animation:pulse 2.2s infinite}
@keyframes pulse{0%{box-shadow:0 0 0 0 rgba(70,196,106,.45)}70%{box-shadow:0 0 0 7px rgba(70,196,106,0)}100%{box-shadow:0 0 0 0 rgba(70,196,106,0)}}
@media(prefers-reduced-motion:reduce){.dot{animation:none}}
h2{font-size:12px;text-transform:uppercase;letter-spacing:.09em;color:var(--dim);
margin:0;font-weight:600}
.sect{display:flex;align-items:baseline;justify-content:space-between;gap:12px;margin:26px 0 12px}
.sect .note{font-size:12px;color:var(--dim);font-weight:500;text-align:right}
.gband{display:flex;align-items:baseline;gap:14px;margin:48px 0 4px;scroll-margin-top:74px}
.gband .gt{font-size:15px;font-weight:700;letter-spacing:.12em;text-transform:uppercase}
.gband .gr{flex:1;height:1px;background:var(--line);align-self:center}
.gband .gn{font-size:12px;color:var(--dim)}
.grid{display:grid;gap:14px;grid-template-columns:repeat(auto-fit,minmax(172px,1fr))}
.card{background:linear-gradient(180deg,var(--card2),var(--card));
border:1px solid var(--line);border-radius:14px;padding:18px 18px 16px}
.num{font-size:26px;font-weight:700;letter-spacing:-.025em;font-variant-numeric:tabular-nums;line-height:1.1}
.lbl{font-size:13px;color:var(--dim);margin-top:5px;font-weight:500}
.sub{font-size:12px;color:var(--dim);margin-top:7px;opacity:.85}
.cols{display:grid;gap:22px;grid-template-columns:1fr 1fr}
@media(max-width:800px){.cols{grid-template-columns:1fr}}
.cols3{display:grid;gap:22px;grid-template-columns:1fr 1fr 1fr}
@media(max-width:1000px){.cols3{grid-template-columns:1fr}}
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
.kpis{display:grid;gap:14px;grid-template-columns:repeat(auto-fit,minmax(200px,1fr));margin-bottom:6px}
.kpi{background:linear-gradient(155deg,var(--card2),var(--card));border:1px solid var(--line);
border-radius:16px;padding:20px 20px 18px;position:relative;overflow:hidden}
.kpi:before{content:"";position:absolute;inset:0 auto 0 0;width:3px;background:var(--acc)}
.kpi.g:before{background:var(--ok)}.kpi.o:before{background:var(--warn)}.kpi.p:before{background:#9a7bff}
.kpi .n{font-size:34px;font-weight:700;letter-spacing:-.03em;font-variant-numeric:tabular-nums;line-height:1.05}
.kpi .l{font-size:12.5px;color:var(--dim);margin-top:6px;font-weight:500}
.chiprow{display:flex;flex-wrap:wrap;gap:8px}
.chipd{display:inline-flex;align-items:center;gap:7px;font-size:12.5px;font-weight:500;
padding:6px 12px;border-radius:20px;background:var(--card2);border:1px solid var(--line);color:var(--ink)}
.chipd i{width:7px;height:7px;border-radius:50%;background:var(--ok);flex-shrink:0}
.chipd.down{color:var(--bad);border-color:rgba(255,93,84,.4);background:rgba(255,93,84,.08)}
.chipd.down i{background:var(--bad)}
.alarms{border-color:rgba(255,93,84,.4);margin-top:14px}
.alarms .k{text-transform:none}
.adot{width:8px;height:8px;border-radius:50%;background:var(--bad);flex-shrink:0}
.dim2{color:var(--dim);font-weight:400}
.bars{display:flex;align-items:flex-end;gap:6px;height:140px;padding:16px 10px 0}
.bcol{flex:1;display:flex;flex-direction:column;align-items:center;justify-content:flex-end;height:100%;min-width:0}
.bbar{width:100%;max-width:26px;background:linear-gradient(180deg,var(--acc),#2a5cbf);
border-radius:4px 4px 0 0;min-height:2px;transition:opacity .12s}
.bbar.hot{background:linear-gradient(180deg,var(--warn),#b97c1e)}
.bcol .bx{font-size:10px;color:var(--dim);margin-top:6px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;max-width:100%}
.bcol .bv{font-size:10px;color:var(--dim);margin-bottom:4px;font-variant-numeric:tabular-nums}
.stage{padding:9px 14px}
.stage .sh{display:flex;justify-content:space-between;font-size:13px;margin-bottom:6px}
.stage .sh .sk{font-weight:500}
.stage .sh .sv{font-weight:600;font-variant-numeric:tabular-nums}
.stbar{height:9px;background:#0c1117;border-radius:5px;overflow:hidden}
.stbar>i{display:block;height:100%;background:linear-gradient(90deg,#3a6fd8,var(--acc));border-radius:5px}
.stage.drop .sh .sv{color:var(--warn)}
.db{padding:8px 14px}
.db .dh{display:flex;justify-content:space-between;gap:10px;font-size:13px;margin-bottom:5px}
.db .dh .dk{font-weight:500;text-transform:capitalize;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.db .dh .dv{font-size:12px;color:var(--dim);font-variant-numeric:tabular-nums;white-space:nowrap}
.db .dh .dv b{color:var(--ink);font-weight:600}
.db .dt{height:8px;background:#0c1117;border-radius:5px;position:relative;overflow:hidden}
.db .dt .cen{position:absolute;left:0;top:0;height:100%;background:#2c3a52;border-radius:5px}
.db .dt .att{position:absolute;left:0;top:0;height:100%;background:linear-gradient(90deg,#3a6fd8,var(--acc));border-radius:5px}
.spark{width:100%;height:auto;display:block}
.spark .sa{fill:url(#sag);stroke:none}
.spark .sl{fill:none;stroke:var(--ok);stroke-width:2;vector-effect:non-scaling-stroke;stroke-linejoin:round}
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
a:focus-visible,.pt:focus-visible{outline:2px solid var(--acc);outline-offset:2px}
.cols3 .ciamb{flex-direction:column;align-items:stretch}
.cols3 .ciamb svg{align-self:center}
.ciamb{display:flex;align-items:center;gap:14px;padding:14px 16px}
.ciamb svg{flex-shrink:0}
.ciamb .ctot{font-size:15px;font-weight:700;fill:var(--ink);text-anchor:middle;font-variant-numeric:tabular-nums}
.ciamb .clbl{font-size:8.5px;fill:var(--dim);text-anchor:middle}
.leg{flex:1;min-width:0;display:flex;flex-direction:column;gap:5px}
.leg .lr{display:flex;align-items:center;gap:8px;font-size:12.5px;min-width:0}
.leg .lq{width:9px;height:9px;border-radius:3px;flex-shrink:0}
.leg .lk{flex:1;font-weight:500;text-transform:capitalize;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.leg .lv{color:var(--dim);font-variant-numeric:tabular-nums;white-space:nowrap}
</style></head><body>
<div class="top">
  <div class="brand"><b>Nivult</b><span>Cruscotto</span></div>
  <nav class="nav">
    <a href="#operazioni">Operazioni</a><a href="#flusso">Flusso</a>
    <a href="#raccolta">Raccolta</a><a href="#dati">Dati</a><a href="#iscritti">Iscritti</a>
  </nav>
  <div class="live"><span class="dot" id="dotv"></span><span id="ts">primo caricamento — dopo un riavvio serve fino a un minuto…</span>
    <a class="esci" href="/cruscotto/esci" title="chiude la sessione del cruscotto">Esci</a></div>
</div>
<div id="app"></div>
<div class="foot">Aggiornamento automatico ogni 30 secondi · accesso riservato</div>
<script>
const IT=n=>n==null?'—':n.toLocaleString('it-IT');
function eta(iso){if(!iso)return '—';const s=(Date.now()-new Date(iso))/1000;
 if(s<0)return 'ora';if(s<90)return Math.round(s)+' sec fa';
 if(s<5400)return Math.round(s/60)+' min fa';
 if(s<86400)return Math.round(s/3600)+' ore fa';return Math.round(s/86400)+' giorni fa'}
const esc=s=>(s==null?'':String(s)).replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
const card=(v,l,sub)=>`<div class="card"><div class="num">${v}</div><div class="lbl">${l}</div>${sub!==undefined?`<div class="sub">${sub}</div>`:''}</div>`;
const gband=(id,t,n)=>`<div class="gband" id="${id}"><span class="gt">${t}</span><span class="gr"></span>${n?`<span class="gn">${n}</span>`:''}</div>`;
function lista(rows,k,vk){if(!rows||!rows.length)return '<div class="sub" style="padding:12px 14px">nessun dato</div>';
 const max=Math.max(...rows.map(r=>r[vk]),1);
 return '<div class="panel">'+rows.map(r=>`<div class="row"><div class="k">${esc(r[k])}</div>`+
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
 if(!rows||!rows.length)return '';
 return '<div class="panel">'+rows.map(r=>`<div class="pl"><div class="k">${r.piattaforma}</div>`
  +`<span class="badge">in coda di collegamento</span>`
  +`<div class="v">${IT(r.aziende)} aziende</div></div>`).join('')+'</div>'}
function iscritti(rows){if(!rows||!rows.length)return '<div class="sub" style="padding:14px">nessun iscritto</div>';
 return '<div class="panel" style="overflow-x:auto;padding:0"><table class="usr">'+
 '<tr><th>Nome</th><th>Email</th><th>Lingua</th><th>Ricezione digest</th><th>Ricerche attive</th><th>Ultimo digest</th></tr>'+
 rows.map(u=>`<tr><td>${esc(u.nome)}</td><td class="em">${esc(u.email)}</td><td>${(u.lingua||'').toUpperCase()}</td>`+
  `<td>${(u.canali||[]).map(c=>`<span class="ch ${c}">${c}</span>`).join('')||'—'}</td>`+
  `<td>${u.ricerche&&u.ricerche.length?u.ricerche.map(r=>`<span class="ch rc">${esc(r)}</span>`).join(''):'<span class="sub">nessuna</span>'}</td>`+
  `<td class="em">${eta(u.ultimo_digest)}</td></tr>`).join('')+'</table></div>'}
function barre(rows,kk,vk,opt){opt=opt||{};
 if(!rows||!rows.length)return '<div class="hint" style="padding:14px">nessun dato</div>';
 const max=Math.max(...rows.map(r=>r[vk]),1),n=rows.length,step=Math.max(1,Math.ceil(n/10));
 return '<div class="panel"><div class="bars">'+rows.map((r,i)=>{
  const hh=Math.max(2,Math.round(100*r[vk]/max));
  const lbl=(n<=12||i%step===0||i===n-1)?r[kk]:'';
  const cls=r.caldo?'hot':'';
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
  const pot=r.potati?` · <span class="dim2">${IT(r.potati)} potati</span>`:'';
  return `<div class="db"><div class="dh"><span class="dk">${r.piattaforma}</span><span class="dv"><b>${rate}%</b> · ${IT(r.attivi)} / ${IT(r.censiti)} vivi${pot}</span></div><div class="dt"><span class="cen" style="width:${pc}%"></span><span class="att" style="width:${pa}%"></span></div></div>`;
 }).join('')+'</div>'}
function copertura(rows){if(!rows||!rows.length)return '<div class="hint" style="padding:14px">in calcolo…</div>';
 return '<div class="panel">'+rows.map(r=>
  `<div class="db"><div class="dh"><span class="dk">${r.campo}</span><span class="dv"><b>${r.pct}%</b> · ${IT(r.n)}</span></div><div class="dt"><span class="att" style="width:${Math.max(0.5,r.pct)}%"></span></div></div>`
 ).join('')+'</div>'}
const _TAV=['#4c8dff','#46c46a','#e0a132','#9a7bff','#ff5d54','#4cc4ff','#5fd67a','#e884c0','#8a97a6'];
function ciambella(rows,kk,vk,etich){
 if(!rows||!rows.length)return '<div class="hint" style="padding:14px">nessun dato</div>';
 const tot=rows.reduce((a,r)=>a+r[vk],0);
 if(!tot)return '<div class="hint" style="padding:14px">nessun dato</div>';
 const parti=rows.slice(0,8).map(r=>({k:String(r[kk]),v:r[vk]}));
 const resto=tot-parti.reduce((a,p)=>a+p.v,0);
 if(resto>0)parti.push({k:'altro',v:resto});
 let a0=-Math.PI/2,archi='';
 parti.forEach((p,i)=>{
  const fr=p.v/tot,a1=a0+fr*2*Math.PI,c=_TAV[i%_TAV.length];
  if(fr>=0.999){archi+=`<circle cx="62" cy="62" r="46" fill="none" stroke="${c}" stroke-width="15"/>`}
  else{
   const ep=0.008;
   const x0=62+46*Math.cos(a0+ep),y0=62+46*Math.sin(a0+ep);
   const x1=62+46*Math.cos(a1-ep),y1=62+46*Math.sin(a1-ep);
   const grande=fr>0.5?1:0;
   archi+=`<path d="M${x0.toFixed(1)} ${y0.toFixed(1)} A46 46 0 ${grande} 1 ${x1.toFixed(1)} ${y1.toFixed(1)}" fill="none" stroke="${c}" stroke-width="15"><title>${esc(p.k)}: ${IT(p.v)} (${(100*fr).toFixed(1)}%)</title></path>`;
  }
  a0=a1;});
 const kcorta=n=>n>=1e6?(n/1e6).toFixed(1).replace('.',',')+'M':n>=1e4?Math.round(n/1e3)+'k':IT(n);
 const legenda=parti.map((p,i)=>`<div class="lr"><span class="lq" style="background:${_TAV[i%_TAV.length]}"></span><span class="lk">${esc(p.k)}</span><span class="lv">${IT(p.v)} · ${(100*p.v/tot).toFixed(1)}%</span></div>`).join('');
 return `<div class="panel"><div class="ciamb"><svg width="124" height="124" viewBox="0 0 124 124" role="img">${archi}<text class="ctot" x="62" y="60">${kcorta(tot)}</text><text class="clbl" x="62" y="74">${esc(etich||'totale')}</text></svg><div class="leg">${legenda}</div></div></div>`;
}
function demoni(g){
 if(!g||!g.demoni||!g.demoni.length)return '<div class="hint" style="padding:14px">stato dei servizi non disponibile</div>';
 const giu=g.demoni.filter(x=>!x.ok);
 return '<div class="panel" style="padding:13px 15px"><div class="chiprow">'+g.demoni.map(x=>
  `<span class="chipd ${x.ok?'':'down'}" title="${esc(x.spiega)}${x.ok?'':' — SPENTO: va riavviato'}"><i></i>${esc(x.etichetta||x.nome)}</span>`).join('')
  +'</div>'+(giu.length?`<div class="hint" style="padding:10px 2px 0">${giu.length===1?'Un servizio è spento':giu.length+' servizi sono spenti'}: il lavoro che copre è fermo finché non riparte (la sentinella ti ha scritto via email).</div>`:'')+'</div>'}
function allarmi(g){if(!g||!g.allarmi||!g.allarmi.length)return '';
 const n=g.allarmi.length;
 return `<div class="panel alarms"><div class="dethd" style="color:var(--bad)">${n===1?'C’è un problema in corso':'Ci sono '+n+' problemi in corso'} — il controllo automatico ti ha già avvisato via email:</div>`
  +g.allarmi.map(a=>`<div class="row"><span class="adot"></span><div class="k" style="white-space:normal">${esc(a.testo)}</div><div class="v" style="color:var(--dim);font-weight:400">${a.da?'da '+eta(new Date(a.da*1000).toISOString()).replace(' fa',''):''}</div></div>`).join('')+'</div>'}
function sparkline(rows,vk){if(!rows||rows.length<2)return '<div class="hint" style="padding:14px">dati insufficienti</div>';
 const W=980,H=88,pt=8,pb=8,n=rows.length,max=Math.max(...rows.map(r=>r[vk]),1);
 const X=i=>W*(i/(n-1)),Y=v=>H-pb-(H-pt-pb)*(v/max);
 const P=rows.map((r,i)=>[X(i),Y(r[vk])]);
 const ln=P.map((p,i)=>(i?'L':'M')+p[0].toFixed(1)+' '+p[1].toFixed(1)).join(' ');
 const ar=`M0 ${H-pb} `+P.map(p=>'L'+p[0].toFixed(1)+' '+p[1].toFixed(1)).join(' ')+` L${W} ${H-pb} Z`;
 const last=rows[n-1];
 return `<div class="lcwrap"><svg class="spark" viewBox="0 0 ${W} ${H}" preserveAspectRatio="none" style="height:88px"><defs><linearGradient id="sag" x1="0" y1="0" x2="0" y2="1"><stop offset="0" stop-color="#46c46a" stop-opacity=".3"/><stop offset="1" stop-color="#46c46a" stop-opacity="0"/></linearGradient></defs><path class="sa" d="${ar}"/><path class="sl" d="${ln}"/></svg></div><div class="hint">picco ${IT(max)}/ora · ultima ora ${IT(last.n)} (${last.ora})</div>`}
const hashS=s=>{let h=0;for(let i=0;i<s.length;i++)h=(h*31+s.charCodeAt(i))>>>0;return h};
function badge(azienda,logo,favicon){
 const nome=(azienda||'?').trim();
 const ini=esc(nome.charAt(0).toUpperCase()||'?');
 const col='hsl('+(hashS(nome)%360)+',42%,40%)';
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
function cardPonte(po){
 let v='—',c='',sub='stato non leggibile';
 if(po&&po.errore===true){v='ERRORE';c='bad';sub='l’ultimo passaggio è fallito: le offerte nuove non arrivano agli iscritti'}
 else if(po&&po.errore===false){v='OK';c='ok';sub='ultimo passaggio pulito'}
 if(po&&po.eta_min!=null)sub+=' · '+po.eta_min+' min fa';
 return card(`<span class="${c}">${v}</span>`,'passaggio offerte agli iscritti',sub)}
function cardNotturno(nt){
 nt=nt||{};let v='—',c='',sub='stato non leggibile';
 if(nt.quando){
  const ore=Math.max(0,Math.round((Date.now()-new Date(nt.quando))/36e5));
  const pf=nt.passi_falliti||[];
  const quandoTxt='corsa del '+nt.quando.slice(8,10)+'/'+nt.quando.slice(5,7)+' alle '+nt.quando.slice(11,16);
  if(nt.completato&&!nt.falliti){v='OK';c='ok';sub=quandoTxt+' · completa, nessun passo fallito'}
  else if(nt.completato){v=nt.falliti+(nt.falliti===1?' passo fallito':' passi falliti');c='warn';sub=quandoTxt+' · falliti: '+pf.map(esc).join(', ')+' — la prossima riprova'}
  else if(ore>14){v='mai finita';c='bad';sub='partita '+ore+' ore fa e mai completata: probabilmente un passo si è bloccato — dettaglio nei log'}
  else{v='in corso';c='warn';sub='partita alle '+nt.quando.slice(11,16)+', dura qualche ora; la raccolta continua gira comunque'+(pf.length?' · falliti finora: '+pf.map(esc).join(', '):'')}}
 return card(`<span class="${c}">${v}</span>`,'manutenzione (parte alle 02:30)',sub)}
function cardSentinella(g){
 let v,c,sub;const n=(g.allarmi||[]).length;
 if(!g.sentinella){v='—';c='warn';sub='stato non leggibile'}
 else if(n){v=n===1?'1 problema':n+' problemi';c='bad';sub='il dettaglio è nel riquadro rosso qui sopra'}
 else{v='tutto ok';c='ok';sub='ogni 5 minuti controlla servizi, dati, disco e credito AI'}
 return card(`<span class="${c}">${v}</span>`,'controllo automatico',sub)}
async function tick(){
 let d;try{const r=await fetch('/cruscotto/dati');
  if(r.status===404){location.replace('/cruscotto');return}
  if(!r.ok)throw 0;d=await r.json()}
 catch(e){document.getElementById('ts').textContent='non raggiungibile';
  document.getElementById('dotv').style.background='var(--bad)';return}
 document.getElementById('dotv').style.background='';
 const s=d.stato,h=d.salute,m=d.motore||{},g=d.giri||{};
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

 +gband('operazioni','Operazioni','se è tutto verde il motore gira da solo — il rosso spiega cosa si è rotto')
 +'<div class="sect"><h2>I servizi del motore</h2><span class="note">girano giorno e notte — verde = acceso · passa il mouse per la spiegazione</span></div>'
 +demoni(g)
 +allarmi(g)
 +'<div class="sect"><h2>Ultime corse ed errori</h2></div><div class="grid">'
 +cardSentinella(g)
 +cardPonte(g.ponte)
 +cardNotturno(g.notturno)
 +card(eta(s.ultimo_scrape),'ultima offerta raccolta')
 +card(eta(s.ultima_classificazione),'ultima offerta classificata')
 +card(eta(s.ultimo_ponte),'ultima offerta passata agli iscritti')
 +'</div>'
 +'<div class="sect"><h2>Salute del dato</h2></div><div class="grid">'
 +card(`<span class="${clP}">${h.senza_paese_pct}%</span>`,'offerte senza paese',IT(h.senza_paese)+' su '+IT(h.offerte_attive))
 +card(`<span class="${clC}">${h.non_classificate_pct}%</span>`,'offerte senza categoria',IT(h.non_classificate)+' su '+IT(h.offerte_attive))
 +card(`<span class="${clF}">${h.codone_freschi_pct}%</span>`,'aziende riviste entro 12 ore',IT(h.codone_oltre_12h)+' oltre 12h · '+IT(h.codone_oltre_24h)+' oltre 24h')
 +`<div class="card pend"><div class="num">${IT(h.ats_pending_n)}</div><div class="lbl">piattaforme da collegare</div>${h.ats_pending_n?`<div class="sub">${IT(h.ats_pending_aziende)} aziende trovate ma non ancora leggibili</div>`:'<div class="sub">leggiamo tutte le piattaforme trovate</div>'}</div>`
 +'</div>'

 +gband('flusso','Flusso','le offerte mentre arrivano')
 +`<div class="sect"><h2>Offerte in arrivo — live</h2><span class="note">${IT(h.pubblicate_1h)} pubblicate nell’ultima ora · ${IT(h.pubblicate_24h)} nelle 24 ore</span></div>`
 +feed(d.live)
 +`<div class="sect"><h2>Offerte raccolte o aggiornate, ora per ora — ultime 24 ore</h2><span class="note">${IT(s.offerte_viste_24h)} raccolte o aggiornate · ${IT(s.aziende_scrapate_1h)} aziende visitate nell’ultima ora</span></div>`
 +'<div class="panel">'+sparkline(d.raccolta_oraria,'n')+'</div>'
 +'<div class="sect"><h2>Offerte pubblicate per giorno — ultimi 30 giorni</h2><span class="note">clicca un punto per le fonti</span></div>'
 +'<div class="panel">'+grafico(d.andamento)+'</div>'

 +gband('raccolta','Raccolta','censimento, freschezza, ampiezza')
 +'<div class="cols">'
 +`<div><div class="sect"><h2>Dall’azienda scoperta all’offerta consegnata</h2><span class="note">${IT(h.scadute)} scadute in archivio</span></div>`+pipeline(d.funnel)+'</div>'
 +`<div><div class="sect"><h2>Da quanto non rivediamo le aziende</h2><span class="note ${clF}">${h.codone_freschi_pct}% entro 12h</span></div>`+barre(d.freschezza,'fascia','n')+'</div>'
 +'</div>'
 +'<div class="sect"><h2>Aziende con offerte aperte, per piattaforma</h2><span class="note">il 100% non esiste: molte aziende vive oggi non assumono, il tetto reale è 60–70% · «potati» = account morti, esclusi dal conto</span></div>'
 +attivazione(d.attivazione)
 +'<div class="cols">'
 +'<div><div class="sect"><h2>Nuove aziende per giorno</h2></div>'+barre(d.nuove_aziende,'giorno','n',{val:false})+'</div>'
 +'<div><div class="sect"><h2>Da dove arrivano le aziende</h2></div>'+lista(d.per_scoperta,'fonte','n')+'</div>'
 +'</div>'
 +(d.ats_pending&&d.ats_pending.length?'<div class="sect"><h2>Piattaforme trovate ma non ancora leggibili</h2></div>'+pending(d.ats_pending):'')

 +gband('dati','Dati','cosa sappiamo di ogni offerta')
 +(function(nv){if(!nv||!nv.totale)return '';
   const cl=p=>p>=70?'ok':p>=40?'warn':'bad';
   return '<div class="sect"><h2>Le offerte nuove arrivano complete?</h2><span class="note">nate da 6–30 ore: l’arricchimento ha avuto il suo tempo — se descrizione o paese crollano, la sentinella ti scrive</span></div><div class="grid">'
   +card(IT(nv.totale),'offerte nuove nelle 24h')
   +card(`<span class="${cl(nv.descrizione)}">${nv.descrizione}%</span>`,'con descrizione')
   +card(`<span class="${cl(nv.paese)}">${nv.paese}%</span>`,'con paese')
   +card(`<span class="${cl(nv.categoria)}">${nv.categoria}%</span>`,'con categoria')
   +card(`<span class="${cl(nv.lingua)}">${nv.lingua}%</span>`,'con lingua')
   +'</div>';})(d.nuove24)
 +(function(mg){if(!mg)return '';
   const c=(v,l,sub)=>v==null?'':card(IT(v),l,sub);
   return '<div class="sect"><h2>Il magazzino da vendere</h2><span class="note">i dataset per i clienti dati — crescono da soli, giorno e notte</span></div><div class="grid">'
   +c(mg.coppie_tecnografiche,'coppie azienda-competenza','la memoria tecnografica')
   +c(mg.storico_chiuse,'offerte nello storico','con data di chiusura: mai cancellate')
   +c(mg.celle_benchmark,'celle benchmark salari','base minima 20 annunci l’una')
   +c(mg.aziende_con_settore,'aziende con settore','registri pubblici, Wikidata o mix delle offerte')
   +c(mg.aziende_con_organico,'aziende con organico','registri, Wikidata o dichiarato negli annunci')
   +c(mg.paesi_memoria,'coppie azienda-paese','per i segnali di espansione')
   +'</div>';})(d.magazzino)
 +'<div class="sect"><h2>Quanto sappiamo di ogni offerta</h2><span class="note">percentuale di offerte attive con quel dato · si aggiorna ogni 10 min</span></div>'
 +copertura(d.arricchimento)
 +'<div class="cols3">'
 +'<div><div class="sect"><h2>Per piattaforma</h2></div>'+ciambella(d.per_fonte,'fonte','attive','offerte')+'</div>'
 +'<div><div class="sect"><h2>Per paese</h2></div>'+ciambella(d.per_paese,'paese','attive','offerte')+'</div>'
 +'<div><div class="sect"><h2>Per famiglia professionale</h2></div>'+ciambella(d.per_famiglia,'famiglia','attive','offerte')+'</div>'
 +'</div>'
 +(d.agenzie&&d.agenzie.length?'<div class="cols"><div><div class="sect"><h2>Agenzie per il lavoro</h2></div>'+lista(d.agenzie,'agenzia','attive')+'</div><div></div></div>':'')

 +gband('iscritti','Iscritti','il motore visto dai clienti')
 +'<div class="grid" style="margin-top:14px">'
 +card(IT(m.offerte_nel_motore),'offerte nel motore')
 +card(IT(m.da_ats),'dai nostri ATS diretti')
 +card(IT(m.cluster_attivi),'ricerche attive')
 +card(IT(m.utenti),'iscritti')
 +'</div>';
 if(d.cluster&&d.cluster.length)H+='<div style="margin-top:14px">'+d.cluster.map(c=>
  `<span class="pill ${c.stato==='active'?'on':''}">${c.famiglia} · ${c.paese}</span>`).join('')+'</div>';
 H+='<div class="sect"><h2>Configurazione completa</h2></div>'+iscritti(d.iscritti);
 document.getElementById('app').innerHTML=H}
tick();setInterval(tick,30000);
</script></body></html>"""
