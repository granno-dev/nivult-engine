"""Reminder delle call prenotate da nivult.com/booking.

Nato il 10/10/2026. Gira ogni 15 minuti (deploy/cron.sh): manda il
reminder 24 ore prima e 1 ora prima, via email sempre e via WhatsApp
se il lead ha lasciato il numero. I bottoni «cancella» e «riprenota»
stanno nel testo della mail — chi non puo' piu' venire libera lo slot
da solo, senza scriverci.

L'invio e' marcato DOPO la mail (reminder_24h_at/1h_at): un SMTP giu'
non deve bruciare l'unico reminder, il giro dopo ritenta. Il rovescio
e' un possibile doppio invio se il processo muore fra invio e commit:
un reminder in piu' si perdona, uno in meno fa saltare la call.
"""
from __future__ import annotations

import sys
from datetime import datetime, timezone

import psycopg

from nivult.api import booking
from nivult.config import database_url, load_dotenv
from nivult.delivery import whatsapp as wa_mod

FINESTRA_MIN = 15


def giri(conn, ore: int, colonna: str) -> int:
    righe = conn.execute(
        f"""select nome, email, telefono, starts_at, join_url, token
            from booking_calls
            where status = 'confirmed'
              and {colonna} is null
              and starts_at between now() + make_interval(hours => %s)
                                and now() + make_interval(hours => %s)
                                        + make_interval(mins => %s)""",
        (ore, ore, FINESTRA_MIN)).fetchall()
    mandate = 0
    for nome, email, telefono, starts_at, join_url, token in righe:
        try:
            booking.mail_reminder(nome, email, starts_at, join_url or "",
                                  token, ore)
        except Exception as e:
            # niente marcatura: il prossimo giro ritenta, la finestra
            # di 15 minuti e' larga quanto il periodo del cron
            print(f"ERRORE mail reminder {ore}h a {email}: {e}",
                  file=sys.stderr)
            continue
        # marcatura DOPO la mail: un SMTP giu' non deve bruciare l'unico
        # reminder. Il doppio invio possibile (crash fra invio e commit)
        # e' il male minore: un reminder in piu' si perdona, uno in meno
        # fa saltare la call.
        conn.execute(
            f"update booking_calls set {colonna} = now() where token = %s",
            (token,))
        conn.commit()
        mandate += 1
        if telefono and wa_mod.configurato():
            try:
                booking.wa_reminder(nome, telefono, starts_at,
                                    join_url or "", token, ore)
            except Exception as e:
                print(f"AVVISO whatsapp reminder {ore}h a {email}: {e}",
                      file=sys.stderr)
    return mandate


def main() -> int:
    load_dotenv()
    with psycopg.connect(database_url()) as conn:
        n24 = giri(conn, 24, "reminder_24h_at")
        n1 = giri(conn, 1, "reminder_1h_at")
    print(f"{datetime.now(timezone.utc).isoformat(timespec='seconds')} "
          f"reminder 24h: {n24}, 1h: {n1}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
