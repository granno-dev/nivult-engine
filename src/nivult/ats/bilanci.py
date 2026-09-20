"""I BILANCI delle aziende, dalle fonti ufficiali gratuite. Primo canale:
ESEF, i bilanci annuali di tutte le quotate UE/SEE, via filings.xbrl.org.

Perche' qui e non su Yahoo Finance: Yahoo non ha API dal 2017 e blocca gli
IP; filings.xbrl.org e' l'indice ufficiale dei depositi ESEF (XBRL
International), gratuito, JSON:API, e ogni bilancio e' un xBRL-JSON con i
fatti gia' marcati (ifrs-full:Revenue, ProfitLoss, Assets, Equity...).
La chiave e' il LEI: e' cosi' che il bilancio si aggancia all'azienda, e
il LEI e' cio' che poi apre GLEIF (ISIN, quindi borsa) e OpenCorporates.

Cosa si tiene di ogni bilancio (tabella `bilanci`, una riga per LEI e
chiusura): ricavi, utile, totale attivo, patrimonio netto, dipendenti,
valuta, periodo, fonte. Solo i fatti CONSOLIDATI, cioe' senza dimensioni
di segmento (un fatto con `dimensions` oltre a concept/entity/period/unit
e' una fetta — per area, per prodotto — e non va confuso col totale).

L'aggancio ai nostri datori: nome normalizzato (`_norm` dei registri) in
modalita' STRETTA, stesso paese, contro le ats_companies. Meglio nessun
LEI che il LEI di un omonimo (regola di registri_imprese, 09/2026). Il
LEI trovato va in ats_companies.lei con lei_source='esef:nome'.

Il magazzino grosso (tutte le aziende del mondo, PDL 22M, GLEIF 2,7M) non
sta qui: sta nel lago dati sul N5 (DuckDB/Parquet). Qui, su Hetzner, solo
cio' che si aggancia ai datori che seguiamo.

    python -m nivult.ats.bilanci --esef --limite 300          # giro notturno
    python -m nivult.ats.bilanci --esef --paese IT --limite 20
    python -m nivult.ats.bilanci --stato
"""
from __future__ import annotations

import argparse
import json
import logging
import re
import time
from collections import defaultdict

import httpx
import psycopg

from nivult.ats.jsonld import _dsn
from nivult.ats.registri_imprese import _colonna_manca, _norm

log = logging.getLogger("nivult.ats.bilanci")

API = "https://filings.xbrl.org"
UA = {"User-Agent": "nivult-ats/1.0 (bilanci da depositi ufficiali; contact: ops@nivult.com)"}

# Concetti IFRS, in ordine di preferenza per ogni voce. Il primo che si
# trova consolidato e sul periodo giusto vince.
CONCETTI = {
    "ricavi": ["ifrs-full:Revenue", "ifrs-full:RevenueFromContractsWithCustomers",
               "ifrs-full:RevenueAndOperatingIncome", "ifrs-full:RevenueFromSaleOfGoods",
               "ifrs-full:RevenueFromRenderingOfServices"],
    "utile": ["ifrs-full:ProfitLoss", "ifrs-full:ProfitLossAttributableToOwnersOfParent",
              "ifrs-full:ProfitLossFromContinuingOperations"],
    "attivo": ["ifrs-full:Assets"],
    "patrimonio": ["ifrs-full:Equity", "ifrs-full:EquityAttributableToOwnersOfParent"],
    "dipendenti": ["ifrs-full:AverageNumberOfEmployees", "ifrs-full:NumberOfEmployees"],
}
_DIM_BASE = {"concept", "entity", "period", "unit", "language"}


def prepara(c) -> None:
    c.execute("""CREATE TABLE IF NOT EXISTS bilanci (
        lei text NOT NULL, periodo_fine date NOT NULL, fonte text NOT NULL,
        nome text, paese text, valuta text,
        ricavi numeric, utile numeric, attivo numeric, patrimonio numeric, dipendenti integer,
        periodo_inizio date, fxo_id text, fatti integer,
        aggiornato timestamptz NOT NULL DEFAULT now(),
        PRIMARY KEY (lei, periodo_fine, fonte))""")
    c.execute("CREATE INDEX IF NOT EXISTS bilanci_paese_idx ON bilanci (paese)")
    if _colonna_manca(c, "bilanci", "dominio"):
        c.execute("ALTER TABLE bilanci ADD COLUMN dominio text")
    if _colonna_manca(c, "ats_companies", "lei"):
        c.execute("ALTER TABLE ats_companies ADD COLUMN lei text, ADD COLUMN lei_source text")
        c.execute("CREATE INDEX IF NOT EXISTS ats_companies_lei_idx ON ats_companies (lei)")
    if _colonna_manca(c, "company_domains", "lei"):
        c.execute("ALTER TABLE company_domains ADD COLUMN lei text, ADD COLUMN lei_source text")
        c.execute("CREATE INDEX IF NOT EXISTS company_domains_lei_idx ON company_domains (lei)")


_HOST_TASSONOMIA_NO = ("xbrl.org", "esma.europa.eu", "ifrs.org", "w3.org", "example.com", "xbrl.ifrs.org")


def dominio_emittente(doc: dict) -> str | None:
    """Il sito dell'emittente, dal namespace della sua tassonomia di estensione:
    ogni bilancio ESEF ne ha una, e per convenzione vive sotto il dominio
    della societa' (http://soft.it/2025-12-31/... per Softlab). E' la chiave
    che aggancia il LEI ai nostri domini senza passare dal nome."""
    from urllib.parse import urlsplit
    for t in (doc.get("documentInfo") or {}).get("taxonomy") or []:
        host = (urlsplit(t).hostname or "").lower()
        if not host or any(host == h or host.endswith("." + h) for h in _HOST_TASSONOMIA_NO):
            continue
        host = host[4:] if host.startswith("www.") else host
        parti = host.split(".")
        return ".".join(parti[-2:]) if len(parti) >= 2 and parti[-2] not in ("co", "com") else ".".join(parti[-3:])
    return None


def _api(cli: httpx.Client, path: str, **params) -> dict:
    r = cli.get(API + path, params=params)
    r.raise_for_status()
    return r.json()


def depositi(cli: httpx.Client, paese: str | None, pagine_max: int = 50) -> list[dict]:
    """I depositi piu' recenti per periodo, con LEI e nome dell'entita'."""
    out = []
    params = {"page[size]": 200, "sort": "-period_end", "include": "entity"}
    if paese:
        params["filter[country]"] = paese
    for n in range(1, pagine_max + 1):
        params["page[number]"] = n
        d = _api(cli, "/api/filings", **params)
        entita = {e["id"]: e["attributes"] for e in d.get("included", []) if e.get("type") == "entity"}
        for f in d.get("data", []):
            a = f["attributes"]
            eid = ((f.get("relationships") or {}).get("entity") or {}).get("data", {}) or {}
            ent = entita.get(eid.get("id"), {})
            lei = ent.get("identifier") or a.get("fxo_id", "").split("-")[0]
            if not a.get("json_url") or not lei or len(lei) != 20:
                continue
            # chiusure nel futuro: errori alla fonte (Recordati «2032-12-31»,
            # 13/09/2026) che altrimenti diventano «l'ultimo bilancio» per sempre
            if (a.get("period_end") or "") > time.strftime("%Y-%m-%d"):
                continue
            out.append({"lei": lei, "nome": ent.get("name"), "paese": a.get("country"),
                        "periodo_fine": a.get("period_end"), "json_url": a["json_url"], "fxo_id": a.get("fxo_id"),
                        "errori": a.get("error_count") or 0, "aggiunto": a.get("date_added") or ""})
        if not d.get("data") or len(d["data"]) < 200:
            break
        time.sleep(0.5)
    # Doppioni (notati da Giuseppe il 13/09/2026): la stessa societa' ha piu'
    # depositi — periodi diversi, e per lo stesso periodo l'originale e la
    # correzione. Ordine: periodo piu' recente, poi deposito aggiunto per
    # ultimo; chi legge tiene il primo per LEI.
    out.sort(key=lambda d: (d["periodo_fine"] or "", d["aggiunto"]), reverse=True)
    return out


def _periodo(p: str) -> tuple[str, str]:
    """'2025-01-01T00:00:00/2026-01-01T00:00:00' -> (inizio, fine-istante).
    In xBRL-JSON la fine e' l'istante di chiusura: 2026-01-01T00:00 = 31/12/2025."""
    if "/" in p:
        a, b = p.split("/", 1)
        return a[:10], b[:10]
    return "", p[:10]


def estrai(doc: dict, periodo_fine: str) -> dict:
    """Le cinque voci dal documento, consolidate e sul periodo del bilancio."""
    facts = doc.get("facts", {})
    per_concetto: dict[str, list[dict]] = defaultdict(list)
    for f in facts.values():
        dims = f.get("dimensions") or {}
        if set(dims) - _DIM_BASE:      # segmento, non totale
            continue
        c = dims.get("concept")
        if c:
            per_concetto[c].append(f)
    out = {"fatti": len(facts), "valuta": None, "periodo_inizio": None}
    # la data-istante di chiusura in xBRL-JSON e' il giorno DOPO periodo_fine
    fine_istante = _giorno_dopo(periodo_fine)
    for voce, concetti in CONCETTI.items():
        for c in concetti:
            cands = per_concetto.get(c) or []
            buoni = []
            for f in cands:
                ini, fin = _periodo(f["dimensions"].get("period", ""))
                if fin not in (fine_istante, periodo_fine):
                    continue
                if voce in ("ricavi", "utile", "dipendenti") and ini and _giorni(ini, fin) < 300:
                    continue   # semestrale o trimestrale: non e' l'annuale
                try:
                    v = float(f["value"])
                except (TypeError, ValueError):
                    continue
                buoni.append((v, f["dimensions"].get("unit"), ini))
            if buoni:
                v, u, ini = buoni[0]
                out[voce] = round(v) if voce == "dipendenti" else v
                if u and u.startswith("iso4217:") and not out["valuta"]:
                    out["valuta"] = u.split(":", 1)[1]
                if ini and not out["periodo_inizio"]:
                    out["periodo_inizio"] = ini
                break
    return out


def _giorno_dopo(d: str) -> str:
    from datetime import date, timedelta
    y, m, g = (int(x) for x in d.split("-"))
    return (date(y, m, g) + timedelta(days=1)).isoformat()


def _giorni(a: str, b: str) -> int:
    from datetime import date
    ya, ma, ga = (int(x) for x in a.split("-")); yb, mb, gb = (int(x) for x in b.split("-"))
    return (date(yb, mb, gb) - date(ya, ma, ga)).days


def _compatto(nome: str) -> str:
    """Nocciolo senza spazi: i nomi in ats_companies sono spesso slug
    («Delonghigroup», «Enavatecareers»), il bilancio dice «DE' LONGHI S.P.A.»."""
    k = _norm(re.sub(r"^(careers?|jobs)\s+|\s*(careers?|jobs|group|holding)$", "", nome, flags=re.I))
    return k.replace(" ", "")


def _indice_nomi(c) -> dict[str, list[tuple[str, str]]]:
    """nome compatto -> [(tabella, id)] su ats_companies E company_domains.
    Il paese in ats_companies e' vuoto nel 97% dei casi (misurato 13/09/2026),
    quindi la guardia anti-omonimi e' l'UNICITA': un nocciolo che compare
    per piu' aziende diverse non si aggancia a nessuna."""
    idx: dict[str, list[tuple[str, str]]] = defaultdict(list)
    for cid, nome in c.execute("SELECT id::text, company_name FROM ats_companies WHERE company_name IS NOT NULL"):
        k = _compatto(nome)
        if len(k) >= 5:
            idx[k].append(("ats_companies", cid))
    for dom, nome in c.execute("SELECT domain, company_name FROM company_domains WHERE company_name IS NOT NULL"):
        k = _compatto(nome)
        if len(k) >= 5:
            idx[k].append(("company_domains", dom))
    return idx


def _upd(c, sql: str, params: tuple) -> int:
    """In psycopg 3 `rowcount` vive sul cursore che execute() restituisce."""
    return c.execute(sql, params).rowcount


def aggancia(c, lei: str, nome: str | None, dominio: str | None, indici: dict) -> int:
    """Il LEI ai nostri datori: prima per DOMINIO (company_domains, e i tenant
    con site_domain/logo_domain), poi per nome compatto se unico."""
    n = 0
    if dominio:
        n += _upd(c, "UPDATE company_domains SET lei=%s, lei_source='esef:dominio' WHERE domain=%s AND lei IS NULL",
                  (lei, dominio))
        n += _upd(c, "UPDATE ats_companies SET lei=%s, lei_source='esef:dominio' "
                     "WHERE (site_domain=%s OR logo_domain=%s) AND lei IS NULL", (lei, dominio, dominio))
    if nome:
        if "nomi" not in indici:
            indici["nomi"] = _indice_nomi(c)
        k = _compatto(nome)
        voci = indici["nomi"].get(k, []) if len(k) >= 5 else []
        # unicita': al massimo un tenant e un dominio, e i domini devono coincidere col dominio dell'emittente se lo conosciamo
        tenant = [v for v in voci if v[0] == "ats_companies"]
        domini = [v for v in voci if v[0] == "company_domains"]
        if len(tenant) == 1:
            n += _upd(c, "UPDATE ats_companies SET lei=%s, lei_source='esef:nome' WHERE id=%s::uuid AND lei IS NULL",
                      (lei, tenant[0][1]))
        if len(domini) == 1 and (not dominio or domini[0][1] == dominio):
            n += _upd(c, "UPDATE company_domains SET lei=%s, lei_source='esef:nome' WHERE domain=%s AND lei IS NULL",
                      (lei, domini[0][1]))
    return n


def esef(dsn: str, paese: str | None = None, limite: int = 300, pausa: float = 0.7, rifai: bool = False) -> dict:
    stats = {"depositi_visti": 0, "scaricati": 0, "scritti": 0, "senza_ricavi": 0, "errori": 0,
             "con_dominio": 0, "agganciati": 0}
    indici: dict = {}
    with psycopg.connect(dsn, autocommit=True) as c, httpx.Client(timeout=120, headers=UA, follow_redirects=True) as cli:
        prepara(c)
        gia = set() if rifai else {r[0] for r in c.execute("SELECT fxo_id FROM bilanci WHERE fonte='esef' AND fxo_id IS NOT NULL")}
        visti_lei: set[str] = set()
        for d in depositi(cli, paese):
            stats["depositi_visti"] += 1
            if d["lei"] in visti_lei:          # ordinati per periodo decrescente: il primo e' l'ultimo bilancio
                continue
            visti_lei.add(d["lei"])
            if d["fxo_id"] in gia:
                continue
            if stats["scaricati"] >= limite:
                break
            if not d["nome"]:                  # l'indice non sempre include l'entita'
                try:
                    d["nome"] = _api(cli, f"/api/entities/{d['lei']}")["data"]["attributes"].get("name")
                except (httpx.HTTPError, ValueError, KeyError):
                    pass
            try:
                r = cli.get(API + d["json_url"])
                r.raise_for_status()
                doc = r.json()
            except (httpx.HTTPError, ValueError) as exc:
                stats["errori"] += 1
                log.warning("  %s %s: %s", d["lei"], d["periodo_fine"], type(exc).__name__)
                continue
            stats["scaricati"] += 1
            v = estrai(doc, d["periodo_fine"])
            dominio = dominio_emittente(doc)
            if v.get("ricavi") is None and v.get("attivo") is None:
                stats["senza_ricavi"] += 1
            if dominio:
                stats["con_dominio"] += 1
            c.execute("""INSERT INTO bilanci (lei, periodo_fine, fonte, nome, paese, valuta, ricavi, utile, attivo, patrimonio,
                                              dipendenti, periodo_inizio, fxo_id, fatti, dominio)
                         VALUES (%s, %s, 'esef', %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                         ON CONFLICT (lei, periodo_fine, fonte) DO UPDATE SET nome=EXCLUDED.nome, valuta=EXCLUDED.valuta,
                           ricavi=EXCLUDED.ricavi, utile=EXCLUDED.utile, attivo=EXCLUDED.attivo, patrimonio=EXCLUDED.patrimonio,
                           dipendenti=EXCLUDED.dipendenti, periodo_inizio=EXCLUDED.periodo_inizio, fxo_id=EXCLUDED.fxo_id,
                           fatti=EXCLUDED.fatti, dominio=COALESCE(EXCLUDED.dominio, bilanci.dominio), aggiornato=now()""",
                      (d["lei"], d["periodo_fine"], d["nome"], d["paese"], v["valuta"], v.get("ricavi"), v.get("utile"),
                       v.get("attivo"), v.get("patrimonio"), v.get("dipendenti"), v["periodo_inizio"], d["fxo_id"], v["fatti"],
                       dominio))
            stats["scritti"] += 1
            agg = aggancia(c, d["lei"], d["nome"], dominio, indici)
            stats["agganciati"] += agg
            log.info("  %s %-2s %s %-32s %-22s ricavi=%s utile=%s agganci=%d", d["lei"], d["paese"], d["periodo_fine"],
                     (d["nome"] or "")[:32], dominio or "-", v.get("ricavi"), v.get("utile"), agg)
            time.sleep(pausa)
    return stats


def stato(dsn: str) -> None:
    with psycopg.connect(dsn) as c:
        prepara(c)
        for r in c.execute("SELECT paese, count(*), count(ricavi), count(dipendenti) FROM bilanci GROUP BY 1 ORDER BY 2 DESC"):
            print(f"  {r[0]:3} bilanci={r[1]:5} con_ricavi={r[2]:5} con_dipendenti={r[3]:5}")
        n = c.execute("SELECT count(*) FROM ats_companies WHERE lei IS NOT NULL").fetchone()[0]
        print(f"  datori con LEI agganciato: {n}")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="nivult.ats.bilanci", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--esef", action="store_true", help="bilanci delle quotate UE da filings.xbrl.org")
    ap.add_argument("--paese", default=None, help="ISO2")
    ap.add_argument("--limite", type=int, default=300, help="bilanci scaricati per giro")
    ap.add_argument("--stato", action="store_true")
    ap.add_argument("--rifai", action="store_true", help="riprocessa anche i bilanci gia' scritti")
    a = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)-7s %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    if a.stato:
        stato(_dsn())
        return 0
    if a.esef:
        print("bilanci esef:", esef(_dsn(), a.paese, a.limite, rifai=a.rifai))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
