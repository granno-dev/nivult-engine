"""Un dominio rivendicato da molte aziende diverse non e' il dominio di nessuna.

Il 19/09/2026 **457 aziende diverse** — Barry's Bootcamp, Appriss Retail, Black
Kite — avevano tutte `rippling.com`. Rippling non e' la loro azienda: e' il loro
ATS, e la sua assenza dalla lista `_ATS_HOST` bastava a far scrivere il suo
dominio a chiunque lo usasse.

E' la terza volta che un fornitore non elencato ci frega: `cdn.ashbyprd.com` la
mattina stessa, `careerpuck.com` qui accanto. Elencare i fornitori uno per uno e'
una corsa che si perde — ne nascono di nuovi. Questa regola invece non dipende
dall'elenco: **se venti aziende senza rapporto fra loro dicono di stare sullo
stesso dominio, quel dominio e' di un fornitore, non loro.**

Prudenza sulla soglia: un gruppo vero PUO' avere piu' tenant sullo stesso
dominio — veolia.com ne ha 3, sika.com 3. Sopra i 10 non e' piu' spiegabile
cosi'. Fra 3 e 10 si segnala e basta: non si cancella un dato su un sospetto.

  ATS_DATABASE_URL=... python pulisci_domini.py [--soglia 10] [--dry-run]
"""
from __future__ import annotations
import argparse, os, sys
import psycopg

# La stessa lista che usa chi SCRIVE i domini (nivult.ats.riscoperta._ATS_HOST).
# Serve anche qui perche' bloccarla nel writer impedisce le righe nuove, non
# cancella quelle gia' scritte: `careerpuck.com` stava su 8 aziende, e 8 e' sotto
# la soglia automatica — un gruppo vero puo' averne 3 o 4, un fornitore no.
try:
    sys.path.insert(0, "/opt/nivult/engine/src")
    from nivult.ats.riscoperta import _ATS_HOST
    FORNITORI = _ATS_HOST.pattern
except Exception:                                            # noqa: BLE001
    FORNITORI = None

TROVA = """
SELECT site_domain, count(*) AS n, string_agg(DISTINCT company_name, ' | ') AS chi
  FROM ats_companies
 WHERE site_domain IS NOT NULL
 GROUP BY 1 HAVING count(*) >= %s
 ORDER BY 2 DESC
"""


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--soglia", type=int, default=10, help="da quante aziende in su il dominio e' di un fornitore")
    ap.add_argument("--segnala-da", type=int, default=3, help="sotto la soglia: solo segnalazione")
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()

    with psycopg.connect(os.environ["ATS_DATABASE_URL"], autocommit=True) as c:
        da_pulire = c.execute(TROVA, (a.soglia,)).fetchall()
        noti = []
        if FORNITORI:
            # ATTENZIONE: un fornitore e' anche un DATORE. `workday.com` per
            # l'azienda «Workday», `remote.com` per «Remote», `ukg.com` per «UKG»
            # sono giusti — sono loro che assumono. Si azzera solo quando il nome
            # dell'azienda non ha niente a che vedere col dominio, che e' il caso
            # di `Coursera -> careerpuck.com`. Senza nome non si decide: si lascia.
            noti = c.execute("""
                SELECT site_domain, count(*), string_agg(DISTINCT company_name, ' | ')
                  FROM ats_companies
                 WHERE site_domain IS NOT NULL AND site_domain ~* %s
                   -- Senza nome non c'e' nessun «fornitore che e' anche datore» da
                   -- proteggere: se il dominio registrabile e' quello di un ATS ed
                   -- e' l'unica cosa che sappiamo, e' una bacheca. Prima il
                   -- `company_name IS NOT NULL` ne risparmiava 228 per niente.
                   -- Il confronto va fatto col dominio REGISTRABILE, non col primo
                   -- pezzo: `anevis-solutions.zohorecruit.eu` ha il nome dell'azienda
                   -- nel sottodominio e verrebbe risparmiato per sbaglio, mentre e'
                   -- proprio una bacheca. Il pezzo che conta e' il penultimo:
                   -- `zohorecruit` li', `workday` in `workday.com`.
                   AND position(split_part(site_domain, '.',
                         greatest(array_length(string_to_array(site_domain, '.'), 1) - 1, 1))
                                in lower(regexp_replace(coalesce(company_name, ''), '[^a-zA-Z0-9]', '', 'g'))) = 0
                   AND position(lower(regexp_replace(coalesce(company_name, 'zzz-nessun-nome'), '[^a-zA-Z0-9]', '', 'g'))
                         in split_part(site_domain, '.',
                         greatest(array_length(string_to_array(site_domain, '.'), 1) - 1, 1))) = 0
                 GROUP BY 1 ORDER BY 2 DESC""", (FORNITORI,)).fetchall()
        sospetti = [r for r in c.execute(TROVA, (a.segnala_da,)).fetchall() if r[1] < a.soglia]

        print(f"DA PULIRE (>= {a.soglia} aziende sullo stesso dominio):")
        tot = 0
        for dom, n, chi in da_pulire:
            tot += n
            print(f"  {dom:<34}{n:>5} aziende   {(chi or '')[:60]}")
        print(f"  ---> {tot:,} righe su {len(da_pulire)} domini\n")

        if noti:
            print("FORNITORI NOTI (nella stessa lista di chi scrive i domini):")
            for dom, q, chi in noti:
                print(f"  {dom:<34}{q:>5} aziende   {(chi or '')[:60]}")
            print(f"  ---> {sum(r[1] for r in noti):,} righe su {len(noti)} domini\n")

        print(f"SOLO SEGNALATI ({a.segnala_da}-{a.soglia-1} aziende: puo' essere un gruppo vero):")
        for dom, n, chi in sospetti[:12]:
            print(f"  {dom:<34}{n:>5} aziende   {(chi or '')[:60]}")
        print(f"  ---> {sum(r[1] for r in sospetti):,} righe su {len(sospetti)} domini, lasciate come sono\n")

        if a.dry_run:
            print("(prova a secco: non ho cambiato niente)")
            return 0
        da_pulire = da_pulire + [r for r in noti if r[0] not in {d for d, _, _ in da_pulire}]
        if not da_pulire:
            print("niente da pulire")
            return 0
        # si azzera il dominio, NON la riga: l'azienda resta, perde solo un dato falso.
        # E si lascia detto perche', o fra un mese nessuno ricordera' cos'e' successo.
        n = c.execute("""
            UPDATE ats_companies
               SET site_domain = NULL,
                   site_domain_source = 'azzerato:dominio-di-fornitore'
             WHERE site_domain = ANY(%s)""", ([d for d, _, _ in da_pulire],)).rowcount
        print(f"azzerati {n:,} domini falsi")
    return 0


if __name__ == "__main__":
    sys.exit(main())
