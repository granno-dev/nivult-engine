"""Promemoria push del tracker: un giro idempotente, con retry al successivo.

La deduplica viene scritta DOPO l'invio. Un crash puo' quindi ripetere una
notifica, ma non registrarne una mai partita: la politica e' at-least-once.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import psycopg

from nivult.config import database_url
from nivult.delivery import push

log = logging.getLogger("nivult.tracker_notifications")

TESTI = {
    "it": {
        "interview": ("Colloquio in arrivo", "{titolo} · {quando}"),
        "next_action": ("Azione da completare", "{titolo}"),
        "deadline": ("Candidatura in scadenza", "{titolo} · scade {quando}"),
        "closed": ("Offerta chiusa", "{titolo}"),
        "weekly": ("Il tuo riepilogo settimanale", "Pipeline, colloqui e prossime azioni"),
    },
    "en": {
        "interview": ("Upcoming interview", "{titolo} · {quando}"),
        "next_action": ("Next action due", "{titolo}"),
        "deadline": ("Application deadline", "{titolo} · closes {quando}"),
        "closed": ("Posting closed", "{titolo}"),
        "weekly": ("Your weekly summary", "Pipeline, interviews and next actions"),
    },
    "es": {
        "interview": ("Entrevista próxima", "{titolo} · {quando}"),
        "next_action": ("Acción pendiente", "{titolo}"),
        "deadline": ("Solicitud próxima a caducar", "{titolo} · cierra {quando}"),
        "closed": ("Oferta cerrada", "{titolo}"),
        "weekly": ("Tu resumen semanal", "Pipeline, entrevistas y próximas acciones"),
    },
}


@dataclass(frozen=True, slots=True)
class Evento:
    user_id: str
    kind: str
    key: str
    titolo: str
    corpo: str
    match_id: str | None = None

    @property
    def dati(self) -> dict:
        if self.match_id is None:
            return {"schermata": "riepilogo"}
        return {"schermata": "match", "match_id": self.match_id,
                "url": f"mavult://match/{self.match_id}"}


def _testo(locale: str, kind: str, titolo: str, quando: str = "") -> tuple[str, str]:
    titolo_push, sagoma = TESTI.get(locale, TESTI["en"])[kind]
    return titolo_push, sagoma.format(titolo=titolo, quando=quando)


def _quando(valore: datetime, fuso: str) -> str:
    try:
        zona = ZoneInfo(fuso)
    except Exception:
        zona = timezone.utc
    return valore.astimezone(zona).strftime("%d/%m %H:%M")


def _evento(riga, kind: str, chiave: str, quando: str = "") -> Evento:
    user_id, match_id, titolo, locale, fuso = riga[:5]
    titolo_push, corpo = _testo(locale, kind, titolo, quando)
    return Evento(str(user_id), kind, chiave, titolo_push, corpo, str(match_id))


def _eventi_imminenti(conn, adesso: datetime) -> list[Evento]:
    eventi: list[Evento] = []
    with conn.cursor() as cur:
        cur.execute(
            "SELECT i.user_id::text, i.match_id::text, j.title, u.locale, u.timezone, "
            "       i.id::text, i.starts_at, i.reminder_minutes "
            "FROM match_interviews i "
            "JOIN users u ON u.id = i.user_id JOIN matches m ON m.id = i.match_id "
            "JOIN jobs j ON j.id = m.job_id "
            "WHERE u.status = 'active' AND 'app' = ANY(u.delivery_channels) "
            "AND i.reminder_minutes IS NOT NULL "
            "AND i.starts_at - make_interval(mins => i.reminder_minutes) <= %s "
            "AND i.starts_at > %s ORDER BY i.starts_at, i.id LIMIT 500",
            (adesso, adesso))
        for riga in cur.fetchall():
            eventi.append(_evento(
                riga, "interview", f"{riga[5]}:{riga[6].isoformat()}:{riga[7]}",
                _quando(riga[6], riga[4])))

        cur.execute(
            "SELECT m.user_id::text, m.id::text, j.title, u.locale, u.timezone, "
            "       m.next_action_at "
            "FROM matches m JOIN users u ON u.id = m.user_id JOIN jobs j ON j.id = m.job_id "
            "WHERE u.status = 'active' AND 'app' = ANY(u.delivery_channels) "
            "AND m.next_action_at <= %s AND m.next_action_at >= %s "
            "ORDER BY m.next_action_at, m.id LIMIT 500",
            (adesso, adesso - timedelta(days=1)))
        for riga in cur.fetchall():
            eventi.append(_evento(riga, "next_action", riga[5].isoformat()))

        cur.execute(
            "SELECT m.user_id::text, m.id::text, j.title, u.locale, u.timezone, "
            "       j.date_valid_through "
            "FROM matches m JOIN users u ON u.id = m.user_id JOIN jobs j ON j.id = m.job_id "
            "WHERE u.status = 'active' AND 'app' = ANY(u.delivery_channels) "
            "AND m.saved_at IS NOT NULL AND m.dismissed_at IS NULL "
            "AND m.archived_at IS NULL AND m.application_status <> 'rejected' "
            "AND j.status = 'active' AND j.date_valid_through > %s "
            "AND j.date_valid_through <= %s "
            "ORDER BY j.date_valid_through, m.id LIMIT 500",
            (adesso, adesso + timedelta(days=3)))
        for riga in cur.fetchall():
            eventi.append(_evento(
                riga, "deadline", riga[5].isoformat(), _quando(riga[5], riga[4])))

        cur.execute(
            "SELECT m.user_id::text, m.id::text, j.title, u.locale, u.timezone, "
            "       COALESCE(j.expired_at, j.date_valid_through) AS chiusa_il "
            "FROM matches m JOIN users u ON u.id = m.user_id JOIN jobs j ON j.id = m.job_id "
            "WHERE u.status = 'active' AND 'app' = ANY(u.delivery_channels) "
            "AND m.saved_at IS NOT NULL AND m.dismissed_at IS NULL AND m.archived_at IS NULL "
            "AND (j.status <> 'active' OR j.date_valid_through <= %s) "
            "AND COALESCE(j.expired_at, j.date_valid_through) <= %s "
            "AND COALESCE(j.expired_at, j.date_valid_through) >= %s "
            "ORDER BY chiusa_il, m.id LIMIT 500",
            (adesso, adesso, adesso - timedelta(days=1)))
        for riga in cur.fetchall():
            eventi.append(_evento(riga, "closed", riga[5].isoformat()))
    return eventi


def chiave_settimanale(adesso: datetime, fuso: str, ora_locale: int = 9) -> str | None:
    try:
        locale = adesso.astimezone(ZoneInfo(fuso))
    except Exception:
        locale = adesso.astimezone(timezone.utc)
    if locale.weekday() != 0 or locale.hour < ora_locale:
        return None
    return locale.date().isoformat()


def _eventi_settimanali(conn, adesso: datetime) -> list[Evento]:
    eventi = []
    with conn.cursor() as cur:
        cur.execute(
            "SELECT id::text, locale, timezone FROM users "
            "WHERE status = 'active' AND 'app' = ANY(delivery_channels) ORDER BY id")
        for user_id, locale, fuso in cur.fetchall():
            chiave = chiave_settimanale(adesso, fuso)
            if chiave is None:
                continue
            titolo, corpo = _testo(locale, "weekly", "")
            eventi.append(Evento(user_id, "weekly", chiave, titolo, corpo))
    return eventi


def eventi_dovuti(conn, adesso: datetime) -> list[Evento]:
    eventi = _eventi_imminenti(conn, adesso) + _eventi_settimanali(conn, adesso)
    return sorted(eventi, key=lambda e: (e.kind, e.user_id, e.key))


def esegui(conn, adesso: datetime | None = None) -> dict[str, int]:
    adesso = adesso or datetime.now(timezone.utc)
    statistiche = {"eventi": 0, "inviati": 0, "duplicati": 0, "senza_dispositivi": 0}
    for evento in eventi_dovuti(conn, adesso):
        statistiche["eventi"] += 1
        with conn.cursor() as cur:
            cur.execute(
                "SELECT 1 FROM personal_notification_dedup "
                "WHERE user_id = %s AND kind = %s AND event_key = %s",
                (evento.user_id, evento.kind, evento.key))
            if cur.fetchone():
                statistiche["duplicati"] += 1
                continue
        dispositivi = push.invia_notifica(
            conn, evento.user_id, titolo=evento.titolo, corpo=evento.corpo,
            dati=evento.dati, canale="reminders")
        if dispositivi <= 0:
            statistiche["senza_dispositivi"] += 1
            continue
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO personal_notification_dedup (user_id, kind, event_key) "
                    "VALUES (%s,%s,%s)",
                    (evento.user_id, evento.kind, evento.key))
            conn.commit()
        except psycopg.errors.UniqueViolation:
            conn.rollback()
            statistiche["duplicati"] += 1
            continue
        statistiche["inviati"] += 1
    return statistiche


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    try:
        with psycopg.connect(database_url()) as conn:
            statistiche = esegui(conn)
        log.info("tracker push: %s", statistiche)
        return 0
    except Exception as exc:
        log.error("tracker push non eseguito: %s", exc)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
