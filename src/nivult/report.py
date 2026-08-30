"""Com'è andato il giro, e cosa sta per andare storto.

    python -m nivult.report              # riepilogo dell'ultimo giro
    python -m nivult.report --days 7     # ultimi 7 giorni
    python -m nivult.report --check      # solo gli allarmi, esce 1 se ce n'è

--check è pensato per il cron: esce 0 in silenzio quando tutto va, esce 1 e
stampa quando c'è qualcosa da guardare.
"""

from __future__ import annotations

import argparse
import sys
from datetime import date

import psycopg

from nivult.config import load_dotenv, migrator_database_url, safe_dsn

# Una fonte che fallisce una notte capita. Due di fila è un guasto.
GIORNI_FALLIMENTO = 2
# Sotto questa frazione del mese trascorsa, un consumo proiettato oltre la
# quota non significa ancora niente: i primi giorni sono troppo rumorosi.
FRAZIONE_MINIMA_MESE = 0.15


def _fetch(cur, sql, params=()):
    cur.execute(sql, params)
    return cur.fetchall()


def riepilogo(cur, giorni: int) -> None:
    print(f"\n── offerte, ultimi {giorni} giorni " + "─" * 34)
    righe = _fetch(cur,
        "SELECT j.source, count(*) FILTER (WHERE j.first_seen_at > now() - make_interval(days=>%s)),"
        "       count(*) FILTER (WHERE j.status='active'),"
        "       count(*) FILTER (WHERE j.status='expired'),"
        "       count(*) FILTER (WHERE j.status='removed'),"
        "       count(*) FILTER (WHERE j.duplicate_of_job_id IS NOT NULL) "
        "FROM jobs j GROUP BY 1 ORDER BY 1", (giorni,))
    if not righe:
        print("  nessuna offerta nel database.")
    else:
        print(f"  {'fonte':<22}{'nuove':>8}{'attive':>9}{'scadute':>9}"
              f"{'rimosse':>9}{'duplicate':>11}")
        for s, nuove, att, sca, rim, dup in righe:
            print(f"  {s:<22}{nuove:>8}{att:>9}{sca:>9}{rim:>9}{dup:>11}")

    print(f"\n── esecuzioni, ultimi {giorni} giorni " + "─" * 31)
    righe = _fetch(cur,
        "SELECT source, count(*) FILTER (WHERE status='success'),"
        "       count(*) FILTER (WHERE status='failed'),"
        "       count(*) FILTER (WHERE status='aborted_budget'),"
        "       count(*) FILTER (WHERE status='success' AND NOT fetch_complete),"
        "       max(finished_at) "
        "FROM ingestion_runs WHERE started_at > now() - make_interval(days=>%s) "
        "GROUP BY 1 ORDER BY 1", (giorni,))
    if not righe:
        print("  nessuna esecuzione registrata.")
    else:
        print(f"  {'fonte':<22}{'ok':>6}{'errori':>8}{'budget':>8}{'troncate':>10}   ultima")
        for s, ok, err, bud, tro, ultima in righe:
            quando = f"{ultima:%d/%m %H:%M}" if ultima else "—"
            print(f"  {s:<22}{ok:>6}{err:>8}{bud:>8}{tro:>10}   {quando}")

    print("\n── crediti del mese " + "─" * 41)
    righe = _fetch(cur,
        "SELECT provider, credits_used, monthly_credits_cap, credits_pct, "
        "       requests_used, monthly_requests_cap, circuit_open "
        "FROM provider_budget_v WHERE monthly_credits_cap > 0 ORDER BY provider")
    if not righe:
        print("  nessun consumo a pagamento registrato questo mese.")
    for p, cu, cc, pct, ru, rc, aperto in righe:
        stato = "  BREAKER APERTO" if aperto else ""
        print(f"  {p:<22}{cu:>7}/{cc}  ({pct}%)   richieste {ru}/{rc}{stato}")

    err = _fetch(cur,
        "SELECT source, left(error_message, 88), count(*), max(started_at) "
        "FROM ingestion_runs WHERE status='failed' "
        "  AND started_at > now() - make_interval(days=>%s) "
        "GROUP BY 1,2 ORDER BY 4 DESC LIMIT 6", (giorni,))
    if err:
        print("\n── errori " + "─" * 51)
        for s, msg, n, quando in err:
            print(f"  {s} ×{n}  ({quando:%d/%m %H:%M})\n    {msg}")


def allarmi(cur) -> list[str]:
    """Solo ciò che merita di svegliare qualcuno."""
    fuori: list[str] = []

    # Una fonte che fallisce due giorni di fila.
    for source, giorni in _fetch(cur,
        "SELECT source, count(DISTINCT date_trunc('day', started_at)) "
        "FROM ingestion_runs "
        "WHERE started_at > now() - interval '3 days' AND status = 'failed' "
        "GROUP BY 1 HAVING count(DISTINCT date_trunc('day', started_at)) >= %s",
        (GIORNI_FALLIMENTO,)):
        fuori.append(f"{source}: fallita in {giorni} giorni distinti negli ultimi 3")

    # Una fonte che non gira più affatto, pur avendo cluster che la richiedono.
    for source, ultima in _fetch(cur,
        "SELECT source, max(finished_at) FROM ingestion_runs GROUP BY 1 "
        "HAVING max(finished_at) < now() - interval '48 hours'"):
        fuori.append(f"{source}: nessuna esecuzione riuscita da {ultima:%d/%m %H:%M}")

    # Il ritmo di consumo esaurirebbe il mese prima della fine.
    oggi = date.today()
    inizio = oggi.replace(day=1)
    giorni_mese = (inizio.replace(year=inizio.year + (inizio.month == 12),
                                  month=inizio.month % 12 + 1) - inizio).days
    trascorsi = (oggi - inizio).days + 1
    frazione = trascorsi / giorni_mese
    if frazione >= FRAZIONE_MINIMA_MESE:
        for p, usati, cap in _fetch(cur,
            "SELECT provider, credits_used, monthly_credits_cap "
            "FROM provider_budget_v WHERE monthly_credits_cap > 0"):
            proiezione = int(usati / frazione)
            if proiezione > cap:
                giorno = int(giorni_mese * cap / max(proiezione, 1))
                fuori.append(
                    f"{p}: a questo ritmo il mese finisce a {proiezione} crediti su {cap} "
                    f"— la quota si esaurirebbe intorno al giorno {giorno}")

    # I DIGEST FALLITI. Vengono prima di tutto il resto: un'ingestione
    # persa si recupera la notte dopo e nessuno se ne accorge, un digest
    # non consegnato e' il prodotto che non arriva.
    #
    # Mancava, e si e' visto: il 2026-08-30 il credito GLM si e' esaurito,
    # i digest sono falliti per quattro ore e questo controllo usciva
    # pulito. L'ha scoperto l'utente.
    for email, n_falliti, motivo in _fetch(cur,
        "SELECT u.email, count(*), max(d.error_message) "
        "FROM digests d JOIN users u ON u.id = d.user_id "
        "WHERE d.status = 'failed' AND d.started_at > now() - interval '24 hours' "
        "GROUP BY 1 ORDER BY 2 DESC"):
        fuori.append(f"{email}: {n_falliti} digest falliti nelle ultime 24 ore "
                     f"— {(motivo or 'motivo non registrato')[:120]}")

    # Uno slot aperto e mai chiuso: il worker e' morto a meta' del giro,
    # senza nemmeno arrivare a scrivere il guasto. Tre ore di margine,
    # perche' il giro parte ogni ora e un digest lento non e' un guasto.
    for email, quando in _fetch(cur,
        "SELECT u.email, d.scheduled_for FROM digests d "
        "JOIN users u ON u.id = d.user_id "
        "WHERE d.status = 'pending' AND d.started_at < now() - interval '3 hours'"):
        fuori.append(f"{email}: digest dello slot {quando:%d/%m %H:%M} "
                     f"rimasto aperto — il giro non l'ha mai chiuso")

    # Breaker rimasti aperti.
    for p, motivo in _fetch(cur,
        "SELECT provider, circuit_reason FROM provider_budget "
        "WHERE circuit_open AND period_month = date_trunc('month', current_date)::date"):
        fuori.append(f"{p}: breaker mensile aperto — {motivo}")

    return fuori


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="nivult.report", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--days", type=int, default=1)
    ap.add_argument("--check", action="store_true",
                    help="solo gli allarmi; esce 1 se ce n'è almeno uno")
    args = ap.parse_args(argv)

    load_dotenv()
    with psycopg.connect(migrator_database_url()) as conn, conn.cursor() as cur:
        problemi = allarmi(cur)
        if args.check:
            for p in problemi:
                print(f"ALLARME  {p}", file=sys.stderr)
            return 1 if problemi else 0

        print(f"Nivult — {safe_dsn(migrator_database_url())}")
        riepilogo(cur, args.days)
        print("\n── allarmi " + "─" * 50)
        if problemi:
            for p in problemi:
                print(f"  ⚠ {p}")
        else:
            print("  nessuno.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
