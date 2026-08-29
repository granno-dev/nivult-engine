"""Il ponte fra il sistema ATS e il funnel dei digest.

Il sistema ATS vive in un database suo (`nivult_ats`) e legge le pagine
carriere delle aziende. Questo modulo NON lo tocca: lo legge e basta, in
sola lettura, e travasa nel motore le offerte che sono pronte a entrare
nel funnel.

**Perché un ponte e non una fonte.** Le fonti di `ingestion/sources/`
sono client HTTP e devono restare senza database — è la regola che tiene
in piedi il probe. L'ATS invece è già in casa: leggerlo è una query, non
una fetch. Quindi il travaso sta qui, e riusa `RawJob` e `upsert_job`
della pipeline esistente: da lì in poi il motore non sa più da dove viene
un'offerta, ed è esattamente la cucitura che permette di aggiungere una
fonte senza toccare niente a valle.

**La soglia d'ingresso.** Passa solo ciò che è utilizzabile davvero:

  · classificata — la famiglia arriva da `job_classifications` e usa lo
    STESSO vocabolario di `job_families`, tutte e 33 identiche. È la
    decisione che rende questo ponte una mappatura dritta invece di una
    tabella di corrispondenze;
  · con un paese — famiglia × paese È la definizione di cluster, senza
    paese non c'è dove metterla;
  · con una data — `jobs.date_posted` è NOT NULL, e senza data l'offerta
    non è nemmeno scrivibile;
  · fresca — vedi sotto, ed è la parte che protegge il prodotto.

**La freschezza non è prudenza, è una toppa.** Nel database ATS non è
mai scaduto niente: `expired_at` è NULL su tutte le righe, la più vecchia
è del 2013 e migliaia hanno più di novanta giorni. Un ponte che le
importasse tutte manderebbe annunci morti nel digest — l'unica cosa che
il prodotto promette di non fare. Quindi entra solo ciò che è stato
pubblicato negli ultimi `GIORNI_FRESCHEZZA` giorni.

**La scadenza a semantica di istantanea.** Qui leggiamo l'INTERO database
locale, non una pagina di API che può troncarsi: quindi «non c'è più,
quindi è scaduta» è una deduzione legittima — che sulle fonti HTTP
richiede invece `fetch_complete`. Ogni giro controlla le offerte
`source='ats'` già nostre e marca scadute quelle sparite o invecchiate.
Il controllo scorre le NOSTRE righe, non le 177.000 dell'ATS: costa
quanto abbiamo importato, non quanto esiste.

**Deduplica con Fantastic: già risolta, e non da noi.** `jobs` ha UNIQUE
su `canonical_url` e `upsert_job` la insegue: la stessa offerta arrivata
da entrambe le fonti aggiorna la riga esistente invece di duplicarsi.
Basta canonicalizzare l'URL con la stessa funzione, ed è ciò che facciamo.

    python scripts/ponte_ats.py --dry-run    # cosa entrerebbe
    python scripts/ponte_ats.py              # travasa
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

import psycopg
from psycopg.types.json import Json

from nivult.config import database_url
from nivult.ingestion.models import RawJob
from nivult.ingestion.store import link_to_cluster, record_usage, upsert_job
from nivult.ingestion.urls import canonicalize, normalize_title

# Trenta giorni: un annuncio più vecchio è spesso già chiuso, e la stessa
# soglia vale per il backfill di Fantastic ("un'offerta di tre settimane fa
# è spesso già chiusa"). Qui serve di più, perché a monte non scade niente.
GIORNI_FRESCHEZZA = 30

# Tutte le offerte ATS vengono dalla pagina carriere dell'azienda: la
# candidatura è diretta. `direct` non esiste fra i link_kind — i valori
# ammessi sono career_site, national_agency, job_board.
LINK_KIND = "career_site"

SORGENTE = "ats"


def ats_database_url() -> str:
    """La stringa dell'ATS: dall'ambiente, o dedotta da quella del motore.

    Dedurla evita di aggiungere un segreto in più da tenere allineato fra
    i due file .env del server — e un segreto in più è un segreto che un
    giorno diverge.
    """
    esplicita = os.environ.get("ATS_DATABASE_URL")
    if esplicita:
        return esplicita
    url = database_url()
    base, _, _ = url.rpartition("/")
    return f"{base}/nivult_ats"


@dataclass(slots=True)
class Riepilogo:
    esaminate: int = 0
    importate: int = 0
    aggiornate: int = 0
    scadute: int = 0
    scartate: dict[str, int] = field(default_factory=dict)

    def scarta(self, motivo: str) -> None:
        self.scartate[motivo] = self.scartate.get(motivo, 0) + 1


def _clusters_attivi(cur: psycopg.Cursor) -> dict[tuple[str, str], str]:
    cur.execute("SELECT id::text, family, country FROM clusters WHERE status = 'active'")
    return {(f, p): i for i, f, p in cur.fetchall()}


def _idonee(cur_ats: psycopg.Cursor, coppie: list[tuple[str, str]],
            giorni: int) -> list[dict]:
    """Le offerte ATS che possono entrare, per i cluster che esistono."""
    if not coppie:
        return []
    soglia = datetime.now(timezone.utc) - timedelta(days=giorni)
    # Le coppie famiglia×paese viaggiano come DUE array paralleli e si
    # ricompongono con unnest: Postgres non accetta una lista di tuple
    # come parametro («input of anonymous composite types is not
    # implemented»), e una IN costruita a stringa sarebbe un'iniezione
    # in attesa di succedere.
    famiglie = [f for f, _ in coppie]
    paesi = [p for _, p in coppie]
    # Il nome del datore ha due ripieghi sul payload grezzo, e la query
    # sotto li applica in COALESCE. `ats_companies.company_name` e'
    # l'autorita', ma e' vuoto sul 55 per cento delle aziende; il grezzo
    # invece porta il nome che la piattaforma dichiara -- «Colisee
    # France», accentato e giusto, non un travestimento dello slug.
    #
    # Misurato sul corpus classificato: 52 per cento col solo campo
    # denormalizzato, 97 per cento con i due ripieghi (smartrecruiters da
    # 10/1012 a 1012/1012, werecruit da 119/987 a 987/987).
    #
    # Non e' un valore inventato -- la regola di casa dice che i campi
    # mancanti si riempiono a regole, mai a occhio. Qui la regola e'
    # «leggi dove la fonte lo ha scritto».
    #
    # Restano scoperti workday e ashby: li' il nome non c'e' nemmeno nel
    # grezzo, quindi l'offerta entra `undisclosed` invece di portare un
    # nome sbagliato. La cura vera e' a monte, riempiendo
    # `ats_companies.company_name`, ed e' dominio dell'ATS: qui non si tocca.
    cur_ats.execute(
        """
        SELECT j.id::text, j.title, j.url, j.country, j.city, j.posted_at,
               j.salary_min, j.salary_max, j.salary_currency, j.raw,
               c.family,
               COALESCE(co.company_name,
                        j.raw->'company'->>'name',
                        j.raw->>'Company_Name')
        FROM ats_jobs j
        JOIN job_classifications c ON c.job_id = j.id
        JOIN unnest(%s::text[], %s::text[]) AS want(family, country)
             ON want.family = c.family AND want.country = j.country
        LEFT JOIN ats_companies co
               ON co.platform_id = j.platform_id AND co.slug = j.slug
        WHERE j.expired_at IS NULL
          AND j.posted_at IS NOT NULL
          AND j.posted_at >= %s
        """,
        (famiglie, paesi, soglia),
    )
    campi = ["id", "title", "url", "country", "city", "posted_at",
             "salary_min", "salary_max", "salary_currency", "raw",
             "family", "company_name"]
    return [dict(zip(campi, r)) for r in cur_ats.fetchall()]


def _come_rawjob(o: dict) -> RawJob:
    """Traduce una riga ATS nel contratto comune del motore.

    I campi AI restano vuoti dove l'ATS non ha un equivalente, e non si
    inventano: `employment_type` per esempio esiste in `ats_jobs` ma con
    un vocabolario suo (FullTime, Permanent Full Time, Employee…) che non
    è quello del motore. Un valore tradotto a occhio diventerebbe un
    filtro che esclude offerte buone, e la regola di casa dice il
    contrario: meglio un campo vuoto. Il funnel non esclude mai per un
    campo assente.
    """
    salario = None
    if o["salary_min"] is not None or o["salary_max"] is not None:
        salario = {"min_value": o["salary_min"], "max_value": o["salary_max"],
                   "currency": o["salary_currency"]}
    return RawJob(
        source=SORGENTE,
        source_job_id=o["id"],
        url=o["url"],
        canonical_url=canonicalize(o["url"]),
        link_kind=LINK_KIND,
        title=o["title"],
        title_normalized=normalize_title(o["title"]),
        # Senza nome del datore `organization` resta NULL, e il trigger
        # scrive employer_kind = 'undisclosed'. È il comportamento giusto:
        # un segnaposto spacciato per azienda è una bugia all'utente.
        organization=o["company_name"] or None,
        date_posted=o["posted_at"],
        raw={"ats": o["raw"]} if o["raw"] is not None else {},
        cities=[o["city"]] if o["city"] else [],
        countries=[o["country"]],
        # La famiglia viaggia con l'offerta, come su Fantastic: è ciò che
        # il funnel legge per sapere di quale scaffale fa parte.
        ai_taxonomies_a=[o["family"]],
        salary=salario,
    )


def _apri_run(cur: psycopg.Cursor, cluster_id: str, giorni: int) -> str:
    cur.execute(
        "INSERT INTO ingestion_runs (cluster_id, source, request_params, "
        "                            fetch_complete) "
        # fetch_complete = true, e non è ottimismo: leggiamo tutto il
        # database locale in una query, non una pagina che può troncarsi.
        # È la condizione che lo sweep delle scadute pretende, ed è
        # l'unica fonte in cui è onestamente vera.
        "VALUES (%s, %s, %s, true) RETURNING id::text",
        (cluster_id, SORGENTE, Json({"freschezza_giorni": giorni})),
    )
    return cur.fetchone()[0]


def _chiudi_run(cur: psycopg.Cursor, run_id: str, nuove: int, aggiornate: int) -> None:
    cur.execute(
        # 'success', non 'ok': il vocabolario di `ingestion_runs.status` e'
        # running/success/failed/aborted_budget, e un valore fuori
        # vocabolario lo rifiuta il CHECK.
        "UPDATE ingestion_runs SET finished_at = now(), status = 'success', "
        "  jobs_fetched = %s, jobs_new = %s, jobs_updated = %s WHERE id = %s",
        (nuove + aggiornate, nuove, aggiornate, run_id),
    )


def _scadi_sparite(cur: psycopg.Cursor, cur_ats: psycopg.Cursor,
                   giorni: int) -> int:
    """Marca scadute le offerte ATS nostre che a monte non valgono più.

    Si scorrono le NOSTRE righe, non quelle dell'ATS: il costo è
    proporzionale a ciò che abbiamo importato. Un'offerta torna viva da
    sola al giro dopo, se ricompare: `upsert_job` rimette status='active'.
    """
    cur.execute(
        "SELECT id::text, source_job_id FROM jobs "
        "WHERE source = %s AND status = 'active'", (SORGENTE,))
    nostre = cur.fetchall()
    if not nostre:
        return 0

    soglia = datetime.now(timezone.utc) - timedelta(days=giorni)
    cur_ats.execute(
        "SELECT id::text FROM ats_jobs "
        "WHERE id::text = ANY(%s) AND expired_at IS NULL "
        "  AND posted_at IS NOT NULL AND posted_at >= %s",
        ([sid for _, sid in nostre], soglia),
    )
    ancora_valide = {r[0] for r in cur_ats.fetchall()}

    morte = [jid for jid, sid in nostre if sid not in ancora_valide]
    if not morte:
        return 0
    cur.execute(
        "UPDATE jobs SET status = 'expired', expired_at = now() "
        "WHERE id::text = ANY(%s)", (morte,))
    return len(morte)


def importa(conn: psycopg.Connection, conn_ats: psycopg.Connection, *,
            giorni: int = GIORNI_FRESCHEZZA, dry_run: bool = False) -> Riepilogo:
    r = Riepilogo()
    with conn.cursor() as cur, conn_ats.cursor() as cur_ats:
        clusters = _clusters_attivi(cur)
        if not clusters:
            return r

        offerte = _idonee(cur_ats, list(clusters.keys()), giorni)
        r.esaminate = len(offerte)

        runs: dict[str, str] = {}
        conteggi: dict[str, list[int]] = {}
        for o in offerte:
            cluster_id = clusters[(o["family"], o["country"])]
            try:
                job = _come_rawjob(o)
            except ValueError as e:
                # Un URL che non si canonicalizza, o un link_kind fuori
                # vocabolario: si conta e si va avanti. Una perdita
                # silenziosa in ingestione non si nota per settimane.
                r.scarta(str(e)[:60])
                continue
            if dry_run:
                r.importate += 1
                continue
            if cluster_id not in runs:
                runs[cluster_id] = _apri_run(cur, cluster_id, giorni)
                conteggi[cluster_id] = [0, 0]
            job_id, nuova = upsert_job(cur, job)
            link_to_cluster(cur, job_id, cluster_id, runs[cluster_id])
            conteggi[cluster_id][0 if nuova else 1] += 1
            if nuova:
                r.importate += 1
            else:
                r.aggiornate += 1

        if not dry_run:
            for cluster_id, run_id in runs.items():
                nuove, agg = conteggi[cluster_id]
                _chiudi_run(cur, run_id, nuove, agg)
                # Anche a costo zero: una fonte che degrada va vista prima
                # che diventi un problema.
                record_usage(cur, provider=SORGENTE, cluster_id=cluster_id,
                             run_id=run_id, requests=1, credits=0,
                             http_status=None, latency_ms=None)
            r.scadute = _scadi_sparite(cur, cur_ats, giorni)
            conn.commit()
    return r
