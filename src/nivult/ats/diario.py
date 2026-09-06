"""Il diario del medico: ogni visita e ogni chat, in `medico_visite`.

Serve a due cose. La prima e' non perdere ciò che il medico ha visto: un
resoconto su Telegram scorre via, una riga in tabella si rilegge. La
seconda e' far crescere il manuale: una volta a settimana si rileggono
le visite e ciò che il medico ha dovuto capire da solo diventa una riga
della tabella «guasti noti e cura», o una cura del pronto soccorso.

    python -m nivult.ats.diario registra <tipo> <motivo> <file-esito> [durata_s]
    python -m nivult.ats.diario ultime [n]
    python -m nivult.ats.diario settimana        # il riepilogo per la revisione
"""
from __future__ import annotations

import re
import sys

import psycopg


def _connetti():
    """Il database delle offerte col ruolo `nivult` (la tabella e' sua, e
    deve poterla creare): stessa strada della sentinella.

    Parametri separati e non una URL: una stringa `postgresql://utente:…@`
    scritta in codice fa scattare lo scanner dei segreti in CI — a ragione,
    perche' e' la forma in cui le password finiscono davvero nei repo.
    """
    for f in ("/opt/nivult/.env", "/opt/nivult/engine/.env"):
        try:
            m = re.search(r"^POSTGRES_PASSWORD=(.*)$", open(f).read(), re.M)
            if m:
                return psycopg.connect(host="127.0.0.1", port=5432, user="nivult",
                                       password=m.group(1).strip(), dbname="nivult_ats",
                                       connect_timeout=10, autocommit=True)
        except OSError:
            pass
    raise SystemExit("POSTGRES_PASSWORD assente")


def _prepara(db) -> None:
    # `IF NOT EXISTS` non protegge da due creazioni SIMULTANEE: Postgres
    # alza una violazione di unicita' su pg_type, e chi crea la tabella a
    # ogni giro puo' morire per questo (successo alla sentinella il
    # 06/09). Qui si tollera: se c'e' gia', va bene comunque.
    try:
        _crea(db)
    except psycopg.errors.UniqueViolation:
        pass


def _crea(db) -> None:
    db.execute("""CREATE TABLE IF NOT EXISTS medico_visite (
        id        bigserial PRIMARY KEY,
        at        timestamptz NOT NULL DEFAULT now(),
        tipo      text NOT NULL CHECK (tipo IN ('visita', 'chat', 'guardiano')),
        motivo    text NOT NULL,
        esito     text,
        durata_s  int,
        rivista   boolean NOT NULL DEFAULT false)""")
    db.execute("GRANT SELECT ON medico_visite TO nivult_app")


def registra(tipo: str, motivo: str, esito: str, durata_s: int | None = None) -> int:
    with _connetti() as db:
        _prepara(db)
        return db.execute("INSERT INTO medico_visite (tipo, motivo, esito, durata_s) VALUES (%s,%s,%s,%s) RETURNING id",
                          (tipo, motivo[:2000], esito[-8000:] if esito else None, durata_s)).fetchone()[0]


def ultime(n: int = 10) -> list[tuple]:
    with _connetti() as db:
        _prepara(db)
        return db.execute("SELECT at, tipo, motivo, esito, durata_s FROM medico_visite ORDER BY at DESC LIMIT %s", (n,)).fetchall()


def settimana() -> str:
    """Il materiale per la revisione: visite degli ultimi 7 giorni non ancora
    riviste, con motivo ed esito, e gli incidenti che le hanno causate."""
    with _connetti() as db:
        _prepara(db)
        vis = db.execute("SELECT id, at, tipo, motivo, esito FROM medico_visite "
                         "WHERE at > now()-interval '7 days' AND NOT rivista ORDER BY at").fetchall()
        inc = db.execute("SELECT chiave, count(*), sum((risolto_da='pronto soccorso')::int), "
                         "sum((risolto_da='rientro dopo la visita del medico')::int) FROM incidenti "
                         "WHERE aperto_at > now()-interval '7 days' GROUP BY 1 ORDER BY 2 DESC").fetchall()
    righe = ["INCIDENTI DELLA SETTIMANA (chiave, quanti, dal pronto soccorso, dopo il medico):"]
    righe += [f"  {k}: {n}  ps={ps or 0}  medico={md or 0}" for k, n, ps, md in inc] or ["  nessuno"]
    righe.append(f"\nVISITE E CHAT NON ANCORA RIVISTE: {len(vis)}")
    for i, at, tipo, motivo, esito in vis:
        righe.append(f"\n--- #{i} {at:%d/%m %H:%M} [{tipo}] {motivo[:200]}\n{(esito or '')[-1200:]}")
    return "\n".join(righe)


def segna_riviste(ids: list[int]) -> None:
    with _connetti() as db:
        db.execute("UPDATE medico_visite SET rivista=true WHERE id = ANY(%s)", (ids,))


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "ultime"
    if cmd == "registra":
        tipo, motivo, f = sys.argv[2], sys.argv[3], sys.argv[4]
        durata = int(sys.argv[5]) if len(sys.argv) > 5 else None
        esito = open(f, errors="replace").read()
        print("diario #", registra(tipo, motivo, esito, durata))
    elif cmd == "settimana":
        print(settimana())
    else:
        n = int(sys.argv[2]) if len(sys.argv) > 2 else 10
        for at, tipo, motivo, esito, d in ultime(n):
            print(f"{at:%d/%m %H:%M} [{tipo}] {motivo[:120]}  ({d or '?'} s)")
            if esito:
                print("   " + esito.strip().replace("\n", "\n   ")[-600:])
