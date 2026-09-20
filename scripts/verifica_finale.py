"""Verifica finale del 19/09/2026: a caccia di quello che ho rotto oggi.

Non controlla che le cose «sembrino a posto»: cerca le condizioni precise in cui
un errore di oggi si manifesterebbe. Ogni blocco ha un ESITO esplicito, cosi' non
si legge un muro di numeri sperando di accorgersi di qualcosa.
"""
import psycopg, pathlib, re, sys

env = dict(re.findall(r"^(\w+)=(.*)$", pathlib.Path("/opt/nivult/engine/.env").read_text(), re.M))
u = env["DATABASE_URL"].strip().strip("\"'").rsplit("/", 1)[0] + "/nivult_ats"
problemi = []


def esito(ok: bool, titolo: str, dettaglio: str = ""):
    print(f"  [{'ok' if ok else 'DA GUARDARE'}] {titolo}" + (f" — {dettaglio}" if dettaglio else ""))
    if not ok:
        problemi.append(f"{titolo}: {dettaglio}")


with psycopg.connect(u) as c:
    def n(s, *p):
        return c.execute(s, p).fetchone()[0]

    print("=== 1. OFFERTE PERSE (marcate fatte ma senza esito) ===")
    # il bug corretto stamattina: col server muto le offerte venivano marcate e
    # scritte come illeggibili. Se ne restassero, sarebbero perse per sempre.
    orf2b = n("""SELECT count(*) FROM ats_jobs j WHERE j.expired_at IS NULL
                   AND j.estratto_2b_at IS NOT NULL
                   AND NOT EXISTS (SELECT 1 FROM estrazioni_v2b e WHERE e.job_id = j.id)""")
    esito(orf2b == 0, "offerte marcate dal 2B senza riga d'esito", f"{orf2b:,}")
    orfm = n("""SELECT count(*) FROM ats_jobs j WHERE j.expired_at IS NULL
                  AND j.sintesi_mt5_at IS NOT NULL
                  AND NOT EXISTS (SELECT 1 FROM sintesi_mt5 m WHERE m.job_id = j.id)""")
    esito(orfm == 0, "offerte marcate da mT5 senza riga d'esito", f"{orfm:,}")
    ill = n("SELECT count(*) FROM estrazioni_v2b WHERE modello LIKE %s", "%illeggibile%")
    esito(True, "righe «illeggibile» in archivio (109 sono del server muto di oggi)", f"{ill:,}")

    print("\n=== 2. PRENOTAZIONI INCASTRATE ===")
    # Una prenotazione vecchia NON e' un guasto: scade da sola dopo SCADENZA_PRESA
    # (20 min) e la riga torna prendibile. Il guasto sarebbe una riga prenotata,
    # MARCATA come fatta, e senza esito — e quello lo controlla il blocco 1.
    # Qui si informa e basta: dopo ogni riavvio di un operaio se ne trovano.
    for col, fatto, chi in (("preso_2b_at", "estratto_2b_at", "2B"),
                            ("preso_mt5_at", "sintesi_mt5_at", "mT5")):
        v = n(f"SELECT count(*) FROM ats_jobs WHERE {col} < now() - interval '2 hours' "
              f"AND {fatto} IS NULL AND expired_at IS NULL")
        esito(True, f"righe che {chi} aveva prenotato e non ha finito", f"{v:,} (gia' riprendibili)")
        # questo invece sarebbe un guasto vero: prenotata e marcata, ma senza esito
        bloc = n(f"SELECT count(*) FROM ats_jobs WHERE {col} IS NOT NULL AND {fatto} IS NOT NULL")
        esito(bloc == 0, f"righe {chi} marcate ma con la prenotazione ancora addosso", f"{bloc:,}")
    # Il ripasso e' spento (vive sul Mac, riacceso al primo abbonato): una
    # prenotazione vecchia qui vuol dire che qualcuno l'ha lasciata a meta'.
    rip = n("SELECT count(*) FROM sintesi_mt5 WHERE presa_ripasso_at < now() - interval '2 hours' AND ripassata_at IS NULL")
    esito(rip == 0, "ripassi prenotati e mai chiusi", f"{rip:,}")

    print("\n=== 3. LA FIDUCIA (il segnale su cui si regge il ripasso) ===")
    tot = n("SELECT count(*) FROM sintesi_mt5 WHERE sintesi IS NOT NULL")
    senza = n("SELECT count(*) FROM sintesi_mt5 WHERE sintesi IS NOT NULL AND fiducia IS NULL")
    esito(senza == 0, "sintesi senza fiducia", f"{senza:,} su {tot:,}")
    fuori = n("SELECT count(*) FROM sintesi_mt5 WHERE fiducia > 0 OR fiducia < -5")
    esito(fuori == 0, "fiducia fuori scala (dev'essere un log-prob, fra -5 e 0)", f"{fuori:,}")
    if tot:
        mn, mx, av = c.execute("SELECT min(fiducia), max(fiducia), avg(fiducia) FROM sintesi_mt5 WHERE sintesi IS NOT NULL").fetchone()
        print(f"       da {mn:+.4f} a {mx:+.4f}, media {av:+.4f}")

    print("\n=== 4. IL BUTTAFUORI ===")
    but = n("SELECT count(*) FROM estrazioni_v2b WHERE modello LIKE %s", "%famiglia-esclusa%")
    male = n("SELECT count(*) FROM estrazioni_v2b WHERE modello LIKE %s AND (tecnologie IS NULL OR jsonb_array_length(tecnologie) > 0)", "%famiglia-esclusa%")
    esito(male == 0, "righe del buttafuori che non sono una lista vuota", f"{male:,} su {but:,}")
    # non deve aver saltato famiglie che rendono: Logistics e Manufacturing
    sbagliate = n("""SELECT count(*) FROM estrazioni_v2b e
                       JOIN job_classifications k ON k.job_id = e.job_id
                      WHERE e.modello LIKE %s
                        AND coalesce(k.v1_family, k.family) IN ('Logistics','Manufacturing','Technology','Software','Engineering')""",
                  "%famiglia-esclusa%")
    esito(sbagliate == 0, "famiglie che rendono, saltate per errore", f"{sbagliate:,}")

    print("\n=== 5. mT5 NON RIFA' IL LAVORO DEL 2B ===")
    # Una manciata di sovrapposizioni e' attesa e innocua: mT5 prenota un'offerta
    # che il 2B non ha ancora riassunto, e nel frattempo il 2B ci scrive sopra una
    # GEMELLA (copia di una vecchia estrazione piena). La vista da' ragione al 2B.
    # Si allarma solo se diventa una quota, non per il rumore di fondo.
    doppio = n("""SELECT count(*) FROM sintesi_mt5 m
                    JOIN estrazioni_v2b e ON e.job_id = m.job_id
                   WHERE m.sintesi IS NOT NULL AND e.sintesi IS NOT NULL
                     AND m.creato_at > now() - interval '60 minutes'""")
    fatte = n("""SELECT count(*) FROM sintesi_mt5
                  WHERE sintesi IS NOT NULL AND creato_at > now() - interval '60 minutes'""")
    quota = 100 * doppio / max(fatte, 1)
    esito(quota < 2.0, "sintesi mT5 su offerte gia' riassunte dal 2B",
          f"{doppio:,} su {fatte:,} nell'ultima ora = {quota:.2f}% (atteso: sotto il 2%)")

    print("\n=== 6. LA VISTA DELLE SINTESI ===")
    v_tot = n("SELECT count(*) FROM sintesi_finali")
    v_null = n("SELECT count(*) FROM sintesi_finali WHERE sintesi IS NULL OR da IS NULL")
    esito(v_null == 0, "righe della vista senza sintesi o senza provenienza", f"{v_null:,} su {v_tot:,}")
    for da, q in c.execute("SELECT da, count(*) FROM sintesi_finali GROUP BY 1 ORDER BY 2 DESC"):
        print(f"       {da:<22}{q:>9,}")

    print("\n=== 7. I DOMINI DOPO LA PULIZIA ===")
    resta = n("""SELECT count(*) FROM ats_companies a WHERE a.site_domain IN
                  (SELECT site_domain FROM ats_companies WHERE site_domain IS NOT NULL
                    GROUP BY 1 HAVING count(*) >= 10)""")
    esito(resta == 0, "domini ancora rivendicati da 10+ aziende", f"{resta:,}")
    azz = n("SELECT count(*) FROM ats_companies WHERE site_domain_source = %s", "azzerato:dominio-di-fornitore")
    esito(True, "domini azzerati oggi perche' di fornitori", f"{azz:,}")
    nuovi = n("SELECT count(*) FROM ats_companies WHERE site_domain IS NOT NULL AND site_domain_source NOT IN ('vanity','brandfetch')")
    esito(True, "domini trovati dal cacciatore (fonti nuove)", f"{nuovi:,}")

    print("\n=== 8. LA TABELLA CHE SI VENDE ===")
    az = n("SELECT count(*) FROM aziende_vendibili")
    incoerenti = n("SELECT count(*) FROM aziende_vendibili WHERE n_tecnologie > 0 AND (tecnologie IS NULL OR jsonb_array_length(tecnologie) = 0)")
    esito(incoerenti == 0, "righe che dicono di avere tecnologie ma non ne hanno", f"{incoerenti:,}")
    zero = n("SELECT count(*) FROM aziende_vendibili WHERE offerte_attive = 0")
    esito(zero == 0, "aziende in tabella senza offerte attive", f"{zero:,}")
    print(f"       {az:,} aziende | {n('SELECT count(*) FROM aziende_vendibili WHERE dominio IS NOT NULL'):,} con dominio "
          f"| {n('SELECT count(*) FROM aziende_vendibili WHERE dominio IS NOT NULL AND n_tecnologie > 0'):,} con dominio E tecnologie")

print("\n" + "=" * 74)
if problemi:
    print(f"DA GUARDARE: {len(problemi)}")
    for p in problemi:
        print(f"  - {p}")
    sys.exit(1)
print("Nessun problema trovato dai controlli qui sopra.")
