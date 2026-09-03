"""Arricchisce le offerte Phenom (e altre) leggendo le pagine di dettaglio.

    python -m nivult.ats.arricchisci --phenom --limite 5000

Phenom è la piattaforma con più offerte senza paese (40.000+): il sitemap
dà solo titolo e URL, ma la pagina di ogni offerta porta un JSON-LD
JobPosting completo — città, paese, data di pubblicazione. Una richiesta
per offerta, sparse su centinaia di domini aziendali: nessun server
sotto pressione.

Le offerte SuccessFactors e Radancy senza paese prendono come ripiego
il paese dell'azienda dal censimento (ats_companies.country) — meglio
di niente per il ponte, che comunque filtra per soglia.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime

import httpx
import psycopg

log = logging.getLogger("nivult.ats.arricchisci")

ATS_DSN = os.environ.get(
    "ATS_DATABASE_URL",
    "postgresql://giusepperanno@127.0.0.1:5432/nivult_ats")

_NOMI_PAESI = {
    "ITALY": "IT", "FRANCE": "FR", "GERMANY": "DE", "DEUTSCHLAND": "DE",
    "SPAIN": "ES", "ESPANA": "ES", "UNITED KINGDOM": "GB", "NETHERLANDS": "NL",
    "BELGIUM": "BE", "SWEDEN": "SE", "SWITZERLAND": "CH", "AUSTRIA": "AT",
    "POLAND": "PL", "PORTUGAL": "PT", "DENMARK": "DK", "IRELAND": "IE",
    "NORWAY": "NO", "FINLAND": "FI", "UNITED STATES": "US", "USA": "US",
    "CANADA": "CA", "CHINA": "CN", "INDIA": "IN", "AUSTRALIA": "AU",
    "JAPAN": "JP", "BRAZIL": "BR", "MEXICO": "MX", "SINGAPORE": "SG",
    "LUXEMBOURG": "LU", "CZECH REPUBLIC": "CZ", "GREECE": "GR",
    "HUNGARY": "HU", "ROMANIA": "RO", "TURKEY": "TR", "ISRAEL": "IL",
}


def _iso(nome: str | None) -> str | None:
    if not nome:
        return None
    n = nome.strip().upper()
    if len(n) == 2 and n.isalpha():
        return n
    return _NOMI_PAESI.get(n)


def _estrai_jsonld(html: str) -> dict:
    """Il JSON-LD JobPosting dalla pagina, se c'è."""
    for m in re.finditer(
            r'<script type="application/ld\+json">(.*?)</script>', html, re.S):
        try:
            d = json.loads(m.group(1))
            if isinstance(d, dict) and d.get("@type") == "JobPosting":
                loc = d.get("jobLocation") or {}
                if isinstance(loc, list):
                    loc = loc[0] if loc else {}
                addr = loc.get("address") or {}
                paese = _iso(addr.get("addressCountry"))
                citta = addr.get("addressLocality")
                data = d.get("datePosted")
                try:
                    dt = datetime.fromisoformat(data) if data else None
                except ValueError:
                    dt = None
                if paese or citta or dt:
                    return {"country": paese, "city": citta, "posted_at": dt}
        except (json.JSONDecodeError, KeyError):
            continue
    return {}


def arricchisci_phenom(dsn: str, limite: int = 5000, thread: int = 10) -> dict:
    """Legge le pagine di dettaglio delle offerte Phenom senza paese."""
    stats = {"viste": 0, "paesi": 0, "citta": 0, "date": 0, "errori": 0}
    with psycopg.connect(dsn) as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT id, url FROM ats_jobs
                 WHERE platform_id = 'phenom'
                   AND country IS NULL AND expired_at IS NULL
                 ORDER BY id
                 LIMIT %s
            """, (limite,))
            righe = cur.fetchall()

    log.info("phenom: %d pagine da leggere (%d thread)", len(righe), thread)
    if not righe:
        return stats

    def leggi(riga):
        jid, url = riga
        try:
            with httpx.Client(timeout=15, follow_redirects=True,
                              headers={"User-Agent": "nivult-ats/0.1"}) as c:
                r = c.get(url)
                if r.status_code == 200:
                    return jid, _estrai_jsonld(r.text)
        except httpx.HTTPError:
            pass
        return jid, {}

    risultati = []
    with ThreadPoolExecutor(max_workers=thread) as pool:
        futures = [pool.submit(leggi, r) for r in righe]
        for i, fut in enumerate(as_completed(futures)):
            try:
                jid, dati = fut.result()
                stats["viste"] += 1
                if dati:
                    stats["paesi"] += 1 if dati.get("country") else 0
                    stats["citta"] += 1 if dati.get("city") else 0
                    stats["date"] += 1 if dati.get("posted_at") else 0
                    risultati.append((jid, dati))
            except Exception:
                stats["errori"] += 1
            if (i + 1) % 500 == 0:
                log.info("  … %d lette: %s", i + 1, stats)

    with psycopg.connect(dsn) as conn:
        for jid, dati in risultati:
            with conn.cursor() as cur:
                cur.execute("""
                    UPDATE ats_jobs
                       SET country = COALESCE(country, %s),
                           city = COALESCE(city, %s),
                           location = COALESCE(location, %s),
                           posted_at = COALESCE(posted_at, %s)
                     WHERE id = %s
                """, (dati.get("country"), dati.get("city"),
                      dati.get("city"), dati.get("posted_at"), jid))
        conn.commit()
    return stats


# Il paese scritto NEL TESTO della localita'. Nomi per esteso nelle
# lingue in cui le piattaforme li scrivono davvero (inglese, lingua
# locale, italiano), piu' le sigle inequivocabili. NIENTE citta': sapere
# che «Boston» sta in America e' conoscenza geografica, e la regola di
# casa vieta di riempire a occhio — qui si legge solo cio' che la fonte
# ha scritto. «Georgia» manca apposta: e' uno stato USA e un paese.
PAESE_NEL_TESTO = {
    "IT": ["italy", "italia", "italien", "italie"],
    "FR": ["france", "francia", "frankreich", "frankrijk"],
    "DE": ["germany", "deutschland", "germania", "allemagne", "alemania"],
    "ES": ["spain", "espana", "españa", "spagna", "espagne", "spanien"],
    "PT": ["portugal", "portogallo"],
    "NL": ["netherlands", "nederland", "paesi bassi", "olanda", "the netherlands", "pays-bas"],
    "BE": ["belgium", "belgique", "belgie", "belgië", "belgio", "belgien"],
    "AT": ["austria", "osterreich", "österreich", "autriche"],
    "CH": ["switzerland", "schweiz", "svizzera", "suisse"],
    "GB": ["united kingdom", "great britain", "england", "scotland", "wales",
           "regno unito", "inghilterra", "uk"],
    "IE": ["ireland", "irlanda", "irland"],
    "SE": ["sweden", "sverige", "svezia", "suede", "suède"],
    "NO": ["norway", "norge", "norvegia"],
    "DK": ["denmark", "danmark", "danimarca"],
    "FI": ["finland", "suomi", "finlandia"],
    "PL": ["poland", "polska", "polonia", "polen"],
    "CZ": ["czech republic", "czechia", "cesko", "česko", "repubblica ceca"],
    "SK": ["slovakia", "slovensko", "slovacchia"],
    "HU": ["hungary", "magyarorszag", "magyarország", "ungheria"],
    "RO": ["romania", "românia"],
    "BG": ["bulgaria"],
    "GR": ["greece", "grecia", "hellas"],
    "HR": ["croatia", "hrvatska", "croazia"],
    "SI": ["slovenia", "slovenija"],
    "EE": ["estonia", "eesti"],
    "LV": ["latvia", "latvija", "lettonia"],
    "LT": ["lithuania", "lietuva", "lituania"],
    "LU": ["luxembourg", "lussemburgo", "luxemburg"],
    "US": ["united states", "usa", "u.s.a", "stati uniti", "estados unidos"],
    "CA": ["canada"],
    "MX": ["mexico", "méxico", "messico"],
    "BR": ["brazil", "brasil", "brasile"],
    "IN": ["india"],
    "CN": ["china", "cina"],
    "JP": ["japan", "giappone"],
    "AU": ["australia"],
    "TW": ["taiwan"],
    "SG": ["singapore"],
    "AE": ["united arab emirates", "uae", "dubai", "emirati arabi"],
    "TR": ["turkey", "turkiye", "türkiye", "turchia"],
}

_RX_PAESE = None


def _paese_dal_testo(testo: str):
    """L'ISO2 se il testo nomina UN paese solo; None se zero o piu' d'uno.

    L'ambiguita' non si scioglie, si salta: una riga che nomina due paesi
    («relocation from France to Germany») non insegna niente di certo.
    """
    import re as _re
    global _RX_PAESE
    if _RX_PAESE is None:
        coppie = [(_re.compile(r"(?<![a-z])" + _re.escape(v) + r"(?![a-z])"),
                   iso) for iso, vv in PAESE_NEL_TESTO.items() for v in vv]
        _RX_PAESE = coppie
    trovati = {iso for rx, iso in _RX_PAESE if rx.search(testo)}
    return trovati.pop() if len(trovati) == 1 else None


def arricchisci_da_localita(dsn: str) -> dict:
    """Il paese letto dal testo di `location`/`city`: riempie e CORREGGE.

    Nasce da un campione che parlava da solo: «Atlanta, GA, United
    States» con country=ES, «USA, PA, Brier Hill» con country=BE. Erano
    i regali di `--da-azienda`, che da' alle offerte il paese della sede
    del portale: giusto come ripiego, sbagliato quando la riga della
    localita' dichiara il paese per iscritto. Il testo della fonte batte
    la deduzione dalla sede, sempre — percio' questo passo va PRIMA di
    `--da-azienda` nel giro notturno, e in piu' corregge cio' che i giri
    passati hanno gia' sporcato.
    """
    riempiti = corretti = 0
    with psycopg.connect(dsn) as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT id, COALESCE(location,'') || ' ' || COALESCE(city,''),
                       country
                  FROM ats_jobs
                 WHERE expired_at IS NULL
                   AND (location IS NOT NULL OR city IS NOT NULL)
            """)
            righe = cur.fetchall()
        aggiorna = []
        for jid, testo, attuale in righe:
            iso = _paese_dal_testo(testo.lower())
            if iso and iso != attuale:
                aggiorna.append((iso, jid))
                if attuale is None:
                    riempiti += 1
                else:
                    corretti += 1
        with conn.cursor() as cur:
            cur.executemany(
                "UPDATE ats_jobs SET country = %s WHERE id = %s", aggiorna)
        conn.commit()
    log.info("da_localita: %d riempiti, %d corretti dal testo",
             riempiti, corretti)
    return {"riempiti": riempiti, "corretti": corretti}


# Le piattaforme il cui raw porta il paese vero: il loro riempimento e'
# compito di mantenimento.arricchisci, e i loro paesi NON NULL non si
# toccano qui — potrebbero venire dal raw, che e' evidenza.
PIATTAFORME_RAW_PAESE = {"lever", "softgarden", "oracle", "cornerstone",
                         "greenhouse", "jibe",
                         # Fonti a paese CERTO o affidabile alla sorgente,
                         # da non azzerare mai: i servizi pubblici sono
                         # nazionali (il paese lo SA il servizio stesso),
                         # SmartRecruiters lo porta nel feed, le agenzie
                         # lo dichiarano, In-recruiting ha il campo nation.
                         # Senza questi in lista, l'arricchimento da-azienda
                         # cancellava il loro paese: era la causa di 200k+
                         # offerte «senza paese» scartate dal ponte.
                         "francetravail", "bundesanstellung",
                         "arbetsformedlingen", "eures", "smartrecruiters",
                         "agenzie", "inrecruiting"}


def _dizionario_citta() -> dict:
    """citta' -> paese, imparato dal corpus del MOTORE (Fantastic e fonti
    pubbliche), dove ogni offerta porta citta' e paese verificati.

    Niente conoscenza geografica esterna: la regola di casa dice che i
    campi mancanti si riempiono a regole dai dati, mai a occhio, e i dati
    li abbiamo — migliaia di coppie citta'/paese gia' pagate. I guardrail
    sono il punto: almeno 3 occorrenze, UNANIMI, e almeno 4 caratteri.
    Senza, il corpus insegnava «rome -> FR» da una riga sporca.
    """
    import os
    dsn = os.environ.get("DATABASE_URL", "")
    if not dsn:
        return {}
    try:
        with psycopg.connect(dsn) as conn, conn.cursor() as cur:
            cur.execute("""
                WITH coppie AS (
                  SELECT lower(trim(unnest(cities))) AS citta,
                         (countries)[1] AS paese
                    FROM jobs
                   WHERE cities IS NOT NULL AND countries IS NOT NULL
                     AND cardinality(countries) = 1)
                SELECT citta, min(paese) FROM coppie
                 WHERE length(citta) >= 4
                 GROUP BY citta
                HAVING count(*) >= 3 AND count(DISTINCT paese) = 1
            """)
            return dict(cur.fetchall())
    except psycopg.Error as exc:
        log.warning("dizionario citta' non disponibile: %s", exc)
        return {}


def arricchisci_da_azienda(dsn: str) -> dict:
    """Il paese DOMINANTE dell'azienda, misurato sui suoi annunci — non
    piu' la sede del portale.

    La versione precedente dava a ogni offerta senza paese il paese della
    sede, e il risultato era nel cluster Italia di un utente vero: Perth e
    Brisbane (Australia), Cholet e Tarn (Francia), Queretaro (Messico) —
    circa trenta offerte estere su quarantaquattro, tutte timbrate IT
    perche' il portale era censito in Italia. «Impreciso ma meglio di
    niente» era vero per il ponte di ieri; oggi il ponte consegna a utenti
    paganti, e un'offerta nel paese sbagliato e' peggio di nessuna.

    La regola nuova si fida solo di cio' che misura:

      1. l'EVIDENZA di un annuncio e' il paese scritto nel testo della sua
         localita' (`_paese_dal_testo`);
      2. un'azienda ha un paese DOMINANTE se almeno 3 suoi annunci hanno
         evidenza e almeno il 90%% concorda;
      3. gli annunci SENZA evidenza prendono il dominante dell'azienda —
         o NULL se l'azienda non ne ha uno. Anche quelli gia' timbrati:
         il timbro della sede non era evidenza, e tenerlo significherebbe
         non guarire mai i cluster gia' sporcati.

    NULL non e' una sconfitta: il ponte importa solo paesi certi, e
    un'offerta senza paese resta nell'archivio in attesa di evidenza
    migliore (una localita' compilata, un raw piu' ricco al rifetch).
    """
    with psycopg.connect(dsn) as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT id, platform_id, slug,
                       COALESCE(location,'') || ' ' || COALESCE(city,''),
                       country
                  FROM ats_jobs
                 WHERE expired_at IS NULL
            """)
            righe = cur.fetchall()

        citta_nota = _dizionario_citta()
        evidenze: dict[tuple, dict] = {}
        con_evidenza: list[tuple] = []   # (id, paese_attuale, evidenza)
        senza: list[tuple] = []          # (id, azienda, paese_attuale, piattaforma)
        for jid, pid, slug, testo, paese in righe:
            chiave = (pid, slug)
            pulito = testo.strip().lower()
            # Prima il paese scritto per esteso, poi la citta' che il
            # nostro corpus conosce: due evidenze, stessa dignita'.
            ev = _paese_dal_testo(pulito) if pulito else None
            if not ev and pulito:
                ev = citta_nota.get(pulito) or next(
                    (iso for c, iso in ((c, citta_nota.get(c)) for c in
                     [x.strip() for x in pulito.split(",")]) if iso), None)
            if ev:
                conta = evidenze.setdefault(chiave, {})
                conta[ev] = conta.get(ev, 0) + 1
                if ev != paese:
                    con_evidenza.append((jid, paese, ev))
            else:
                senza.append((jid, chiave, paese, pid))

        dominante: dict[tuple, str] = {}
        for chiave, conta in evidenze.items():
            tot = sum(conta.values())
            iso, n = max(conta.items(), key=lambda kv: kv[1])
            if tot >= 3 and n / tot >= 0.9:
                dominante[chiave] = iso

        aggiorna: list[tuple] = [(ev, jid) for jid, _, ev in con_evidenza]
        for jid, chiave, paese, pid in senza:
            voluto = dominante.get(chiave)
            if paese is not None and pid in PIATTAFORME_RAW_PAESE:
                # Un paese gia' scritto su queste piattaforme puo' venire
                # dal raw: e' evidenza, non timbro. Non si tocca.
                continue
            if voluto != paese:
                aggiorna.append((voluto, jid))

        with conn.cursor() as cur:
            cur.executemany(
                "UPDATE ats_jobs SET country = %s WHERE id = %s", aggiorna)
        conn.commit()

    da_evidenza = len(con_evidenza)
    riempiti = sum(1 for v, _ in aggiorna if v is not None) - da_evidenza
    azzerati = sum(1 for v, _ in aggiorna if v is None)
    log.info("da_azienda: %d da evidenza diretta, %d col dominante "
             "dell'azienda, %d senza evidenza azzerati (aziende con "
             "dominante: %d)", da_evidenza, riempiti, azzerati, len(dominante))
    return {"da_evidenza": da_evidenza, "riempiti": riempiti,
            "azzerati": azzerati, "aziende_con_dominante": len(dominante)}


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO,
                        format="%(levelname)-8s %(message)s")
    ap = argparse.ArgumentParser(prog="nivult.ats.arricchisci",
                                 description=__doc__)
    ap.add_argument("--phenom", action="store_true",
                    help="legge le pagine di dettaglio Phenom (JSON-LD)")
    ap.add_argument("--da-localita", action="store_true",
                    help="paese letto dal testo di location/city: riempie e corregge")
    ap.add_argument("--da-azienda", action="store_true",
                    help="paese del portale carriere come ripiego")
    ap.add_argument("--limite", type=int, default=5000)
    ap.add_argument("--thread", type=int, default=10)
    args = ap.parse_args(argv)

    if args.phenom:
        s = arricchisci_phenom(ATS_DSN, args.limite, args.thread)
        print(f"\nPhenom: {s}")
    if args.da_localita:
        esito = arricchisci_da_localita(ATS_DSN)
        print(f"Da localita: {esito}")
    if args.da_azienda:
        esito_a = arricchisci_da_azienda(ATS_DSN)
        print(f"\nDa azienda: {esito_a}")
    if not (args.phenom or args.da_azienda or args.da_localita):
        ap.print_help()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
