"""La scheda azienda che il mercato vende: i campi di Coresignal che si ricavano
da cio' che abbiamo gia', senza fonti chiuse (21/09/2026).

Coresignal incolla su ogni annuncio 24 campi di azienda presi da LinkedIn,
Glassdoor e Indeed. Noi quelle fonti non le tocchiamo; ma buona parte di
quei campi si DEDUCE dalle offerte stesse e dalle fonti aperte che gia'
leggiamo, e si etichetta per quello che e':

  size_range        fascia di dipendenti (stile LinkedIn) dal numero migliore
                    che abbiamo: wikidata > sito > registro > dichiarato (il
                    registro conta l'unita' legale: vedi `dipendenti`), con
                    la categoria INSEE (PME/ETI/GE) che corregge la fascia
  legal_form, founded, registration_id, hq con via/CAP/coordinate: dalla
                    scheda di registro (aziende_registro: SIRENE, Brreg, PRH,
                    CVR, EDGAR, GLEIF)
  locations         le sedi VISTE nelle offerte, con quante offerte ciascuna;
                    la prima e' la piu' frequente (is_primary)
  hq_*              la sede principale: dal registro/GLEIF se ce l'abbiamo,
                    altrimenti la sede piu' frequente delle offerte, e
                    `hq_da` dice quale delle due e'
  description       la descrizione che l'azienda da' di se' negli annunci
                    (jsonld hiringOrganization.description, SmartRecruiters
                    companyDescription): parole SUE, non nostre
  keywords          le famiglie e le tecnologie piu' frequenti nei suoi annunci
  external_urls     sameAs / url dichiarati (sito, LinkedIn, ecc.)

Una riga per tenant in `aziende_dettagli`; tutto SQL e un giro Python al
giorno, niente modelli, niente GPU.

    python -m nivult.ats.aziende_dettagli [--limite N]
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import re
import sys
import time

import psycopg

log = logging.getLogger("nivult.ats.aziende_dettagli")

DDL = """
CREATE TABLE IF NOT EXISTS aziende_dettagli (
  company_id     uuid PRIMARY KEY REFERENCES ats_companies(id) ON DELETE CASCADE,
  size_range     text,
  size_da        text,
  employees_best integer,
  employees_scope text,      -- group / legal_entity / self_declared
  legal_name     text,
  legal_form     text,
  legal_form_code text,
  registration_id text,
  founded        date,
  hq_latitude    real,
  hq_longitude   real,
  locations      jsonb,       -- [{city, country, state, offerte, is_primary}]
  n_locations    integer,
  hq_country     text,
  hq_state       text,
  hq_city        text,
  hq_street      text,
  hq_zipcode     text,
  hq_full_address text,
  hq_da          text,        -- gleif / sirene / brreg / prh / cvr / edgar / offerte
  description    text,
  description_da text,        -- jsonld hiringOrganization / smartrecruiters companyDescription / sito (meta description)
  external_urls  jsonb,
  keywords       jsonb,       -- {famiglie: [...], tecnologie: [...]}
  calcolato_at   timestamptz NOT NULL DEFAULT now());
ALTER TABLE ats_companies ADD COLUMN IF NOT EXISTS dettagli_at timestamptz;
"""

FASCE = ((1, 10, "1-10"), (11, 50, "11-50"), (51, 200, "51-200"), (201, 500, "201-500"), (501, 1000, "501-1000"),
         (1001, 5000, "1001-5000"), (5001, 10000, "5001-10000"), (10001, 10**9, "10001+"))


def fascia(n: int | None) -> str | None:
    if not n or n <= 0:
        return None
    for a, b, et in FASCE:
        if a <= n <= b:
            return et
    return None


def dipendenti(e_reg, e_site, e_self, e_wd, band, categoria=None) -> tuple[int | None, str | None, str | None, str | None, str | None]:
    """LA regola dei dipendenti, una sola per scheda ed export (21/09/2026).

    Il registro conta l'UNITA' LEGALE: SIRENE dava Veolia Environnement SA a
    1.499, Renault a 4, Eurofins a 374, mentre Wikidata (che conta il
    gruppo) li da' a 220.000, 179.000 e 62.000. Il compratore vuole il
    gruppo: prima Wikidata, poi cio' che l'azienda dichiara sul proprio
    sito, poi il registro, per ultimo il numero letto negli annunci (rumoroso:
    «765» per Eurofins). La categoria INSEE, quando c'e', corregge la fascia:
    GE = impresa da 5.000+, ETI = 250-4.999, anche se l'unita' legale e'
    piccola. Restituisce (numero, fonte del numero, portata del numero,
    fascia, fonte della fascia): il numero resta quello della sua fonte con
    la sua portata (1.499 = unita' legale), la fascia puo' venire da un'altra
    (INSEE GE = 5001+)."""
    best, da, portata = None, None, None
    for n, fonte, scope in ((e_wd, "wikidata", "group"), (e_site, "sito", "group"),
                            (e_reg, "registro", "legal_entity"), (e_self, "dichiarato", "self_declared")):
        if n and int(n) > 0:
            best, da, portata = int(n), fonte, scope
            break
    size, size_da = fascia(best), da
    if not size and band:
        size, size_da = band, "registro (fascia)"
        if not da:
            portata = "legal_entity"
    if categoria == "GE" and (best or 0) < 5000:
        size, size_da = "5001+", "INSEE catégorie GE (impresa, non unità legale)"
    elif categoria == "ETI" and (best or 0) < 250:
        size, size_da = "251-5000", "INSEE catégorie ETI (impresa, non unità legale)"
    return best, da, portata, size, size_da


SQL_TENANT = """
SELECT c.id, c.employees_reg, c.employees_site, c.employees_self_n, c.employees_wd, c.employees_reg_band, c.country,
       c.site_description
  FROM ats_companies c
 WHERE c.job_count > 0 AND (c.dettagli_at IS NULL OR c.dettagli_at < now() - interval '7 days')
 ORDER BY c.job_count DESC
 LIMIT %s
"""
# la scheda di registro: GLEIF (sede operativa) batte i registri nazionali
# (sede legale), che battono le offerte
SQL_REGISTRO = """
SELECT fonte, legal_name, legal_form, legal_form_code, registro_id, street, postal_code, city, region, country,
       latitude, longitude, founded, website, categoria
  FROM aziende_registro WHERE company_id = %s
 ORDER BY CASE fonte WHEN 'gleif' THEN 0 ELSE 1 END, fetched_at DESC
"""
SQL_SEDI = """
SELECT coalesce(j.city, ''), coalesce(j.country, ''), coalesce(d.state, ''), count(*) AS n
  FROM ats_jobs j LEFT JOIN offerte_dettagli d ON d.job_id = j.id
 WHERE j.platform_id = %s AND j.slug = %s AND j.expired_at IS NULL
 GROUP BY 1, 2, 3 ORDER BY n DESC LIMIT 25
"""
SQL_FAMIGLIE = """
SELECT coalesce(x.family, x.v1_family) AS f, count(*) AS n
  FROM ats_jobs j JOIN job_classifications x ON x.job_id = j.id
 WHERE j.platform_id = %s AND j.slug = %s AND j.expired_at IS NULL AND coalesce(x.family, x.v1_family) IS NOT NULL
 GROUP BY 1 ORDER BY n DESC LIMIT 5
"""
SQL_TEC = """
SELECT technology, annunci FROM azienda_tecnologie WHERE platform_id = %s AND slug = %s
 ORDER BY annunci_attivi DESC, annunci DESC LIMIT 12
"""
SQL_ORG = """
SELECT j.raw->'hiringOrganization', j.raw->'jobAd'->'sections'->'companyDescription'->>'text'
  FROM ats_jobs j
 WHERE j.platform_id = %s AND j.slug = %s AND j.expired_at IS NULL
   AND (j.raw ? 'hiringOrganization' OR j.raw->'jobAd'->'sections' ? 'companyDescription')
 ORDER BY j.posted_at DESC NULLS LAST LIMIT 3
"""
COLONNE = ("company_id", "size_range", "size_da", "employees_best", "employees_scope", "locations", "n_locations",
           "hq_country", "hq_state", "hq_city", "hq_street", "hq_zipcode", "hq_full_address", "hq_da",
           "hq_latitude", "hq_longitude", "legal_name", "legal_form", "legal_form_code", "registration_id", "founded",
           "description", "description_da", "external_urls", "keywords")
SQL_SCRIVI = ("INSERT INTO aziende_dettagli (" + ", ".join(COLONNE) + ") VALUES (" + ", ".join(["%s"] * len(COLONNE)) + ") "
              "ON CONFLICT (company_id) DO UPDATE SET " + ", ".join(f"{c} = EXCLUDED.{c}" for c in COLONNE[1:]) + ", calcolato_at = now()")
_TAG = re.compile(r"<[^>]+>")


def _indirizzo_intero(reg: dict) -> str | None:
    """Via, CAP citta', paese: senza ripetere cio' che la via gia' contiene
    (SIRENE scrive «3 RUE DU PRE FAUCON 74000 ANNECY» tutto in `adresse`)."""
    via = (reg.get("street") or "").strip()
    pezzi = [via] if via else []
    cap_citta = " ".join(y for y in (reg.get("postal_code"), reg.get("city")) if y).strip()
    if cap_citta and cap_citta.lower() not in via.lower():
        if reg.get("city") and reg["city"].lower() in via.lower() and reg.get("postal_code") and reg["postal_code"] in via:
            pass
        else:
            pezzi.append(cap_citta)
    if reg.get("country"):
        pezzi.append(reg["country"])
    return ", ".join(pezzi) or None


def _pulito(t) -> str:
    import html as _html
    t = t if isinstance(t, str) else ""
    return re.sub(r"\s+", " ", _TAG.sub(" ", _html.unescape(_html.unescape(t)))).strip()


def _descrizione_e_url(righe) -> tuple[str | None, str | None, list[str]]:
    urls: list[str] = []
    for org, sr in righe:
        if isinstance(org, dict):
            for k in ("url", "sameAs"):
                v = org.get(k)
                for u in (v if isinstance(v, list) else [v]):
                    if isinstance(u, str) and u.startswith("http") and u not in urls:
                        urls.append(u[:200])
            d = _pulito(org.get("description"))
            if len(d) >= 80:
                return d[:1500], "jsonld hiringOrganization", urls
        if isinstance(sr, str) and len(_pulito(sr)) >= 80:
            return _pulito(sr)[:1500], "smartrecruiters companyDescription", urls
    return None, None, urls


def _schema(c) -> None:
    """Il DDL solo se manca qualcosa: `ALTER TABLE ... ADD COLUMN IF NOT
    EXISTS` prende il lock esclusivo su ats_companies anche quando la
    colonna c'e' gia' (vedi dettagli._schema, 21/09)."""
    tabella = c.execute("SELECT 1 FROM information_schema.tables WHERE table_name = 'aziende_dettagli'").fetchone()
    colonna = c.execute("SELECT 1 FROM information_schema.columns WHERE table_name = 'ats_companies' AND column_name = 'dettagli_at'").fetchone()
    c.execute("SET lock_timeout = '10s'")
    if not (tabella and colonna):
        c.execute(DDL)
    # le colonne aggiunte il 21/09 sera (scheda di registro): solo su
    # aziende_dettagli, che nessun demone legge
    presenti = {r[0] for r in c.execute("SELECT column_name FROM information_schema.columns WHERE table_name = 'aziende_dettagli'")}
    for col, tipo in (("employees_scope", "text"), ("legal_name", "text"), ("legal_form", "text"), ("legal_form_code", "text"),
                      ("registration_id", "text"), ("founded", "date"), ("hq_latitude", "real"), ("hq_longitude", "real")):
        if col not in presenti:
            c.execute(f"ALTER TABLE aziende_dettagli ADD COLUMN {col} {tipo}")
    # site_description la scrive scheda_sito; se non e' ancora passato, la
    # colonna si crea qui (lock_timeout 10s: se la tabella e' occupata si
    # rinuncia e si riprova al prossimo giro)
    if not c.execute("SELECT 1 FROM information_schema.columns WHERE table_name = 'ats_companies' AND column_name = 'site_description'").fetchone():
        c.execute("ALTER TABLE ats_companies ADD COLUMN IF NOT EXISTS site_description text")
        c.execute("ALTER TABLE ats_companies ADD COLUMN IF NOT EXISTS site_description_at timestamptz")
    c.execute("RESET lock_timeout")


def applica(dsn: str, limite: int = 20000) -> dict:
    st = {"viste": 0, "fascia": 0, "sedi": 0, "descrizione": 0, "keywords": 0}
    t0 = time.time()
    with psycopg.connect(dsn, autocommit=True) as c:
        _schema(c)
        tenants = c.execute(SQL_TENANT, (limite,)).fetchall()
        for cid, e_reg, e_site, e_self, e_wd, band, paese, descr_sito in tenants:
            pid, slug = c.execute("SELECT platform_id, slug FROM ats_companies WHERE id = %s", (cid,)).fetchone()
            st["viste"] += 1
            try:
                registri = c.execute(SQL_REGISTRO, (cid,)).fetchall()
            except psycopg.errors.UndefinedTable:
                registri = []
            reg = dict(zip(("fonte", "legal_name", "legal_form", "legal_form_code", "registro_id", "street", "postal_code",
                            "city", "region", "country", "latitude", "longitude", "founded", "website", "categoria"),
                           registri[0])) if registri else {}
            categoria = next((r[14] for r in registri if r[14]), None)
            best, dip_da, portata, size, size_da = dipendenti(e_reg, e_site, e_self, e_wd, band, categoria)
            sedi = c.execute(SQL_SEDI, (pid, slug)).fetchall()
            locs = [{"city": ci or None, "country": co or None, "state": s or None, "offerte": n, "is_primary": i == 0}
                    for i, (ci, co, s, n) in enumerate(sedi) if ci or co]
            # la sede: dal registro se ce l'ha (con via e CAP), altrimenti la
            # citta' piu' frequente nelle offerte
            if reg.get("city") or reg.get("street"):
                hq = {"country": reg.get("country") or paese, "state": reg.get("region"), "city": reg.get("city"),
                      "street": reg.get("street"), "zipcode": reg.get("postal_code"),
                      "full_address": _indirizzo_intero(reg),
                      "lat": reg.get("latitude"), "lon": reg.get("longitude"), "da": reg["fonte"]}
            elif locs:
                hq = {"country": locs[0]["country"] or paese, "state": locs[0]["state"], "city": locs[0]["city"],
                      "street": None, "zipcode": None, "full_address": None, "lat": None, "lon": None, "da": "offerte"}
            else:
                hq = None
            fam = [f for f, _ in c.execute(SQL_FAMIGLIE, (pid, slug)).fetchall()]
            try:
                tec = [t for t, _ in c.execute(SQL_TEC, (pid, slug)).fetchall()]
            except psycopg.errors.UndefinedTable:
                tec = []
            desc, desc_da, urls = _descrizione_e_url(c.execute(SQL_ORG, (pid, slug)).fetchall())
            if not desc and descr_sito:
                desc, desc_da = descr_sito, "sito (meta description)"
            if reg.get("website") and not any(reg["website"].split("/")[-1] in u for u in urls):
                w = reg["website"] if reg["website"].startswith("http") else "https://" + reg["website"]
                urls.append(w[:200])
            kw = {"famiglie": fam, "tecnologie": tec} if (fam or tec) else None
            c.execute(SQL_SCRIVI, (cid, size, size_da, best, portata, json.dumps(locs, ensure_ascii=False) if locs else None, len(locs),
                                   (hq or {}).get("country") or paese, (hq or {}).get("state"), (hq or {}).get("city"),
                                   (hq or {}).get("street"), (hq or {}).get("zipcode"), (hq or {}).get("full_address"), (hq or {}).get("da"),
                                   (hq or {}).get("lat"), (hq or {}).get("lon"),
                                   reg.get("legal_name"), reg.get("legal_form"), reg.get("legal_form_code"), reg.get("registro_id"), reg.get("founded"),
                                   desc, desc_da, json.dumps(urls) if urls else None, json.dumps(kw, ensure_ascii=False) if kw else None))
            c.execute("UPDATE ats_companies SET dettagli_at = now() WHERE id = %s", (cid,))
            st["fascia"] += bool(size); st["sedi"] += bool(locs); st["descrizione"] += bool(desc); st["keywords"] += bool(kw)
            if st["viste"] % 500 == 0:
                log.info("aziende_dettagli: %s in %.0f s", st, time.time() - t0)
    return st


def main(argv=None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    ap = argparse.ArgumentParser()
    ap.add_argument("--limite", type=int, default=20000)
    ap.add_argument("--tutte", action="store_true", help="rifa' anche le schede fresche (dopo una modifica della regola)")
    a = ap.parse_args(argv)
    if a.tutte:
        global SQL_TENANT
        SQL_TENANT = SQL_TENANT.replace("AND (c.dettagli_at IS NULL OR c.dettagli_at < now() - interval '7 days')", "")
    dsn = os.environ.get("ATS_DATABASE_URL")
    if not dsn:
        for f in ("/opt/nivult/.env", "/opt/nivult/engine/.env"):
            try:
                m = re.search(r"^POSTGRES_PASSWORD=(.*)$", open(f).read(), re.M)
                if m:
                    dsn = "postgresql://nivult:" + m.group(1).strip() + "@127.0.0.1:5432/nivult_ats"
                    break
            except OSError:
                pass
    print("Aziende dettagli:", applica(dsn, a.limite))
    return 0


if __name__ == "__main__":
    sys.exit(main())
