"""Il worker dei digest: per ogni utente dovuto, valuta e consegna.

    python -m nivult.matching.worker                 # tutti gli utenti dovuti
    python -m nivult.matching.worker --user <uuid>   # un utente solo
    python -m nivult.matching.worker --dry-run       # simula e annulla ogni scrittura

Il ciclo per un utente:

    candidati  = offerte attive dei suoi cluster, mai valutate per lui,
                 sopravvissute ai filtri deterministici (funnel.py)
    budget     = user_try_evaluate addebita i giudizi esterni PER UTENTE, solo
                 dopo una risposta valida; i provider locali non consumano quota
    valutazione = un modello configurato, una offerta per chiamata, con fallback
    digest     = chi supera la soglia corrente di una ricerca attiva, ordinato
                 per punteggio e trasparenza; motivazione solo per le prime 30
    consegna   = canali attivi; vuoto è un esito legittimo (skipped_empty)

Tutto è riprendibile: l'anti-ripetizione su matches fa sì che un worker
interrotto e rilanciato non rivaluti nulla di già pagato, e la UNIQUE
(user_id, scheduled_for) su digests fa sì che lo slot non venga consegnato
due volte. Il ruolo della connessione è nivult_app: il worker fa DML, non DDL.
"""

from __future__ import annotations

import argparse
import logging
import os
import re
import sys
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Json

from nivult.config import database_url, load_dotenv, safe_dsn
from nivult.delivery import email as email_mod
from nivult.delivery import push as push_mod
from nivult.delivery import telegram as telegram_mod
from nivult.delivery import whatsapp as whatsapp_mod
from nivult.delivery.testi import LINGUA_PER_GLM, t
from nivult.matching import funnel, prescore_v3
from nivult.matching import prescore as prescore_mod
from nivult.matching.llm import (
    GLM,
    VERSIONE_RUBRICA,
    Groq,
    MaestroLocale,
    MistralSmall,
    Nvidia,
    RispostaModelloNonValida,
    Scrittore,
    motiva_verificata,
    offerta_per_prompt,
    profilo_come_testo,
    valuta_offerta,
)

log = logging.getLogger("nivult.matching.worker")

# Soglia alta: meglio un digest vuoto che un digest scadente. Alta, non
# proibitiva — ed era diventata proibitiva.
#
# A 80 passava la meta' ALTA della fascia che la rubrica stessa chiama
# «buona: il ruolo e' giusto, con qualche scarto di livello o di ambito».
# Misurato su un utente reale, sulle offerte ancora vive: 10 sopra 80, 18
# sopra 70. E le sette escluse non erano scarti — HR Business Partner in
# Amazon, Global HR Business Partner, People & HR Operations Specialist
# Italia, Change Specialist Milano. Tenerle fuori non proteggeva nessuno.
#
# 70 e' il confine che la rubrica gia' dichiara fra «buona» e
# «plausibile»: sotto ci finiscono le mosse laterali credibili (un
# consulente HR che guarda una posizione commerciale), e quelle e' giusto
# che restino fuori. La soglia non si muove per riempire una quota — si e'
# mossa perche' era tarata male.
#
# Da ottobre 2026 e' il DEFAULT, non la soglia: ogni ricerca puo' alzare o
# abbassare la sua barra (user_clusters.min_score, slider 50-95 dell'app) e
# il candidato la porta con se' dal funnel (`_min_score`). Questa costante
# entra in gioco solo quando la ricerca non dice niente.
THRESHOLD = funnel.DEFAULT_MIN_SCORE
# Le "prime 30" delle decisioni di architettura: la motivazione di qualità
# si paga solo per ciò che il destinatario vedrà davvero.
MAX_ITEMS = 30
PROVIDER_LOCALI = {"maestro-locale", "scrittore-locale"}


def _addebita_valutazione(evaluatore) -> bool:
    provider = getattr(getattr(evaluatore, "model", None), "source", None)
    return not (MaestroLocale.disponibile() and provider in PROVIDER_LOCALI)


@dataclass(slots=True)
class RicercaDovuta:
    id: str
    frequency: str
    send_hour_local: int
    send_weekday: int | None
    send_monthday: int | None
    timezone: str
    next_digest_at: datetime
    last_digest_at: datetime | None
    send_hour_local_raw: int | None = None
    timezone_raw: str | None = None


@dataclass(slots=True)
class Utente:
    id: str
    email: str
    plan: str
    delivery_channels: list[str]
    display_name: str | None
    delivery_email: str | None
    telegram_chat_id: str | None
    whatsapp_e164: str | None
    whatsapp_conversation_id: str | None
    frequency: str
    send_hour_local: int
    send_weekday: int | None
    send_monthday: int | None
    timezone: str
    next_digest_at: datetime | None
    last_digest_at: datetime | None
    cv_id: str | None
    locale: str = "en"
    delivery_failures: int = 0
    delivery_failures_by_channel: dict = field(default_factory=dict)
    profilo: dict = field(default_factory=dict)
    ricerche_dovute: list[RicercaDovuta] = field(default_factory=list)
    # I test legacy che costruiscono Utente direttamente mantengono il
    # comportamento globale; utenti_dovuti calcola sempre il valore reale.
    eredita_dovuta: bool = True


class ValutatoreGLM:
    """Catena dei modelli configurati, con uso reale per modello e operazione."""

    def __init__(self):
        self.totale = {"input": 0, "cached": 0, "output": 0, "chiamate": 0}
        self.totali: dict[tuple[str, str, str], dict] = {}
        modelli = []
        if MaestroLocale.disponibile():
            modelli.append(MaestroLocale(rate_per_second=4.0))
        for cls in (GLM, Groq, Nvidia, MistralSmall):
            if cls.disponibile():
                modelli.append(cls(rate_per_second=2.0))
        if not modelli:
            raise RuntimeError("nessun modello di valutazione configurato")
        self.model = modelli[0]
        self._ripieghi = modelli[1:]
        self.scrittore = (Scrittore(rate_per_second=4.0)
                          if Scrittore.disponibile() else None)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        chiusi = set()
        for modello in ([self.model, self.scrittore] + self._ripieghi):
            if modello is not None and id(modello) not in chiusi:
                modello.close()
                chiusi.add(id(modello))

    def _registra(self, modello, uso: dict, operazione: str) -> None:
        uso = dict(uso or {})
        provider = uso.get("provider") or modello.source
        nome_modello = uso.get("model") or modello.model
        chiave = (provider, nome_modello, operazione)
        totale = self.totali.setdefault(chiave, {
            "input": 0, "cached": 0, "output": 0, "chiamate": 0,
            "complete": True})
        for campo in ("input", "cached", "output"):
            valore = uso.get(campo, 0)
            if not isinstance(valore, (int, float)) or isinstance(valore, bool):
                valore = 0
                totale["complete"] = False
            totale[campo] += valore
            self.totale[campo] += valore
        totale["complete"] = (totale["complete"]
                              and uso.get("complete", True))
        totale["chiamate"] += 1
        self.totale["chiamate"] += 1

    def _registra_fallimento(self, modello, operazione: str, prima: int) -> None:
        if self.totale["chiamate"] != prima:
            return
        uso = dict(getattr(modello, "last_usage", {}) or {})
        uso.setdefault("complete", bool(uso))
        self._registra(modello, uso, operazione)

    def _passa_al_ripiego(self, e: Exception) -> bool:
        testo = str(e)
        ripiegabile = isinstance(e, RispostaModelloNonValida) or any(
            s in testo for s in ("1113", "Insufficient balance", "429",
                                 "Rate limit", "tentativi falliti"))
        if not ripiegabile or not self._ripieghi:
            return False
        self.model.close()
        self.model = self._ripieghi.pop(0)
        log.warning("modello precedente inutilizzabile (%s): il giro "
                    "prosegue su %s", testo[:90], self.model.model)
        return True

    def valuta(self, profilo_testo: str, offerta: dict, lingua: str = "English"):
        while True:
            modello = self.model
            prima = self.totale["chiamate"]
            try:
                return valuta_offerta(
                    modello, profilo_testo, offerta, offerta.get("_wants"),
                    lingua,
                    on_usage=lambda uso, m=modello: self._registra(m, uso, "score"))
            except RuntimeError as e:
                self._registra_fallimento(modello, "score", prima)
                if not self._passa_al_ripiego(e):
                    raise

    def motiva(self, profilo_testo: str, offerta: dict, lingua: str = "English",
               locale: str = "en", evita: list[str] | None = None):
        ripiego = t(locale)["motivo_fallback"]
        usa_scrittore = True
        while True:
            penne = ([self.scrittore] if usa_scrittore and self.scrittore else [])
            penne.append(self.model)
            ultimo: RuntimeError | None = None
            for penna in penne:
                prima = self.totale["chiamate"]
                try:
                    return motiva_verificata(
                        penna, profilo_testo, offerta, offerta.get("_wants"),
                        lingua, ripiego, evita=evita,
                        on_usage=lambda uso, p=penna: self._registra(
                            p, uso, "motivation"))
                except RuntimeError as e:
                    self._registra_fallimento(penna, "motivation", prima)
                    ultimo = e
            usa_scrittore = False
            if ultimo is None or not self._passa_al_ripiego(ultimo):
                raise ultimo


def _lingua(v) -> str:
    """Il CV conserva le lingue come jsonb: stringa o oggetto, non decidiamo noi."""
    if isinstance(v, str):
        return v
    return (v or {}).get("language") or (v or {}).get("name") or ""


def _profilo(cv: dict | None) -> dict:
    if not cv:
        return {}
    note = (f"{cv['years_experience']} anni di esperienza"
            if cv.get("years_experience") else None)
    if cv.get("relocation_open"):
        note = " · ".join(parte for parte in (note, "Disponibile a trasferirsi") if parte)
    return {
        "ruolo": ", ".join(cv["families"] or []),
        "seniority": cv.get("seniority_label") or cv.get("seniority") or "—",
        "competenze": list(cv["skills"] or []),
        "lingue": [l for l in map(_lingua, cv["languages"] or []) if l],
        "residenza": (" · ".join(parte for parte in (
            cv.get("location_city"), cv.get("location_country")) if parte) or None),
        "note": note,
    }


def utenti_dovuti(cur, adesso: datetime, user_id: str | None = None) -> list[Utente]:
    ereditata_attiva = (
        "EXISTS (SELECT 1 FROM user_clusters uc_e "
        "JOIN clusters c_e ON c_e.id = uc_e.cluster_id "
        "WHERE uc_e.user_id = u.id AND uc_e.frequency IS NULL "
        "  AND NOT uc_e.is_paused AND c_e.status = 'active')")
    scadenza_override = (
        "SELECT min(uc.next_digest_at) FROM user_clusters uc "
        "JOIN clusters c_ov ON c_ov.id = uc.cluster_id "
        "WHERE uc.user_id = u.id AND uc.frequency IS NOT NULL "
        "  AND NOT uc.is_paused AND c_ov.status = 'active' "
        "  AND uc.next_digest_at IS NOT NULL AND uc.next_digest_at <= %s")
    sql = (
        "SELECT u.id::text, u.email::text, u.plan, u.delivery_channels, "
        "       u.delivery_email::text, u.telegram_chat_id, u.whatsapp_e164, "
        "       u.whatsapp_conversation_id, "
        "       u.frequency, u.send_hour_local, u.send_weekday, u.send_monthday, "
        "       u.timezone, u.next_digest_at, u.last_digest_at, u.locale, "
        "       u.delivery_failures, u.delivery_failures_by_channel, "
        "       u.display_name, "
        "       cv.id::text AS cv_id, cv.families, cv.seniority, cv.skills, "
        "       cv.languages, cv.years_experience, cv.location_city, "
        "       cv.location_country, cv.relocation_open, e.label AS seniority_label, "
        + ereditata_attiva + " AS eredita_attiva "
        "FROM users u "
        "LEFT JOIN user_cvs cv ON cv.user_id = u.id AND cv.status = 'active' "
        "LEFT JOIN experience_levels e ON e.code = cv.seniority "
        "WHERE u.status = 'active' "
        "  AND u.subscription_status IN ('trialing','active') "
        "  AND ((u.next_digest_at IS NOT NULL AND u.next_digest_at <= %s AND "
        + ereditata_attiva + ") OR EXISTS ("
        + scadenza_override.replace("min(uc.next_digest_at)", "1") + "))")
    params: list = [adesso, adesso]
    if user_id:
        sql += " AND u.id = %s"
        params.append(user_id)
    sql += (" ORDER BY LEAST(CASE WHEN " + ereditata_attiva
            + " THEN u.next_digest_at END, (" + scadenza_override + "))")
    params.append(adesso)
    cur.execute(sql, params)
    utenti = [Utente(
        id=r["id"], email=r["email"], plan=r["plan"],
        delivery_channels=r["delivery_channels"], delivery_email=r["delivery_email"],
        display_name=r["display_name"],
        telegram_chat_id=r["telegram_chat_id"], whatsapp_e164=r["whatsapp_e164"],
        whatsapp_conversation_id=r["whatsapp_conversation_id"],
        frequency=r["frequency"], send_hour_local=r["send_hour_local"],
        send_weekday=r["send_weekday"], send_monthday=r["send_monthday"],
        timezone=r["timezone"], next_digest_at=r["next_digest_at"],
        last_digest_at=r["last_digest_at"], cv_id=r["cv_id"],
        locale=r["locale"], delivery_failures=r["delivery_failures"],
        delivery_failures_by_channel=r["delivery_failures_by_channel"],
        profilo=_profilo(r),
        eredita_dovuta=(r["eredita_attiva"] and r["next_digest_at"] is not None
                        and r["next_digest_at"] <= adesso))
        for r in cur.fetchall()]
    if not utenti:
        return utenti

    per_id = {u.id: u for u in utenti}
    cur.execute(
        "SELECT uc.user_id::text, uc.id::text, uc.frequency, "
        "       COALESCE(uc.send_hour_local, u.send_hour_local) AS send_hour_local, "
        "       uc.send_weekday, uc.send_monthday, "
        "       COALESCE(uc.timezone, u.timezone) AS timezone, "
        "       uc.next_digest_at, uc.last_digest_at, "
        "       uc.send_hour_local AS send_hour_local_raw, uc.timezone AS timezone_raw "
        "FROM user_clusters uc JOIN users u ON u.id = uc.user_id "
        "JOIN clusters c ON c.id = uc.cluster_id "
        "WHERE uc.user_id = ANY(%s::uuid[]) AND uc.frequency IS NOT NULL "
        "  AND NOT uc.is_paused AND c.status = 'active' "
        "  AND uc.next_digest_at IS NOT NULL AND uc.next_digest_at <= %s "
        "ORDER BY uc.user_id, uc.next_digest_at, uc.id",
        (list(per_id), adesso))
    for r in cur.fetchall():
        per_id[r["user_id"]].ricerche_dovute.append(RicercaDovuta(
            id=r["id"], frequency=r["frequency"],
            send_hour_local=r["send_hour_local"], send_weekday=r["send_weekday"],
            send_monthday=r["send_monthday"], timezone=r["timezone"],
            next_digest_at=r["next_digest_at"], last_digest_at=r["last_digest_at"],
            send_hour_local_raw=r["send_hour_local_raw"],
            timezone_raw=r["timezone_raw"]))
    return utenti


def _slot_digest(u: Utente, adesso: datetime) -> datetime | None:
    """Il primo slot realmente dovuto, senza anticipare quello globale."""
    scadenze = [r.next_digest_at for r in u.ricerche_dovute
                if r.next_digest_at <= adesso]
    if (u.eredita_dovuta and u.next_digest_at is not None
            and u.next_digest_at <= adesso):
        scadenze.append(u.next_digest_at)
    return min(scadenze) if scadenze else None


def _canale_ok(conn, u, canale: str) -> None:
    """Azzera soltanto il canale che ha appena consegnato con successo."""
    if not u.delivery_failures_by_channel.get(canale):
        return
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE users SET delivery_failures_by_channel = "
            "jsonb_set(delivery_failures_by_channel, ARRAY[%s], '0'::jsonb, true) "
            "WHERE id = %s", (canale, u.id))
    conn.commit()
    u.delivery_failures_by_channel[canale] = 0


def _canale_fallito(conn, u, canale: str) -> int:
    """Incrementa in modo atomico il contatore del solo canale fallito."""
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE users SET delivery_failures_by_channel = jsonb_set("
            "  delivery_failures_by_channel, ARRAY[%s], to_jsonb("
            "    COALESCE((delivery_failures_by_channel->>%s)::int, 0) + 1), true) "
            "WHERE id = %s RETURNING delivery_failures_by_channel->>%s",
            (canale, canale, u.id, canale))
        n = cur.fetchone()[0]
    conn.commit()
    u.delivery_failures_by_channel[canale] = n
    return n


def _canale_principale(u) -> str:
    """Il canale registrato sulla riga del digest: l'email se e' attiva,
    altrimenti il primo. Ogni CONSEGNA avviene su un canale; e' l'utente
    ad averne piu' d'uno."""
    return "email" if "email" in u.delivery_channels else u.delivery_channels[0]


def _stacca_canale(conn, u, canale: str, motivo: str) -> None:
    """Toglie UN canale dall'insieme e lo DICE via email.

    Non si torna piu' «all'email»: si perde solo il canale rotto, gli altri
    restano. Se era l'ultimo, l'email subentra — un utente senza nessun
    canale e' un abbonato che paga per niente, e il vincolo in tabella
    giustamente lo vieta. Il silenzio sarebbe la cosa peggiore: uno che ha
    scelto Telegram e smette di ricevere li' senza spiegazione pensa che
    il prodotto sia rotto.
    """
    rimasti = [c for c in u.delivery_channels if c != canale] or ["email"]
    with conn.cursor() as cur:
        cur.execute("UPDATE users SET delivery_channels = %s, "
                    "  delivery_failures_by_channel = "
                    "  delivery_failures_by_channel - %s WHERE id = %s",
                    (rimasti, canale, u.id))
    conn.commit()
    u.delivery_channels = rimasti
    u.delivery_failures_by_channel.pop(canale, None)
    x = t(u.locale)
    try:
        email_mod.invia_generica(
            u.delivery_email or u.email,
            x["canale_ripiego_oggetto"],
            x["canale_ripiego_testo"].format(motivo=motivo), "")
    except Exception:
        log.warning("%s: non sono riuscito ad avvisare del distacco", u.email)


def _orario(giorno: date, ora: int, tz: ZoneInfo) -> datetime:
    return datetime.combine(giorno, time(ora), tzinfo=tz)


def calcola_slot(frequency: str, send_hour_local: int, send_weekday: int | None,
                 send_monthday: int | None, tz_nome: str,
                 adesso: datetime) -> datetime:
    """Il prossimo orario di invio, dai soli campi dell'orario.

    Firma sui campi e non sull'Utente perche' serve anche all'API: quando
    qualcuno cambia la frequenza dal pannello, il primo slot va ricalcolato
    li'. Finche' questa funzione stava solo nel worker, next_digest_at lo
    scriveva solo il worker — DOPO un invio — e chi si iscriveva restava con
    NULL, cioe' mai dovuto, cioe' senza digest per sempre.

    Sempre strettamente futuro: se il worker e' stato fermo qualche giorno lo
    slot arretrato non si recupera mandando digest a raffica — le offerte
    intanto sono li', e la prossima consegna le porta tutte.
    """
    tz = ZoneInfo(tz_nome)
    oggi = adesso.astimezone(tz).date()
    if frequency == "daily":
        cand = _orario(oggi, send_hour_local, tz)
        while cand <= adesso:
            cand = _orario(cand.date() + timedelta(days=1), send_hour_local, tz)
        return cand.astimezone(timezone.utc)
    if frequency == "weekly":
        # send_weekday è ISODOW: 1 lunedì … 7 domenica.
        avanti = ((send_weekday or 1) - oggi.isoweekday()) % 7
        cand = _orario(oggi + timedelta(days=avanti), send_hour_local, tz)
        if cand <= adesso:
            cand = _orario(cand.date() + timedelta(days=7), send_hour_local, tz)
        return cand.astimezone(timezone.utc)
    # monthly: send_monthday è 1–28, quindi ogni mese lo contiene.
    giorno = send_monthday or 1
    cand = _orario(oggi.replace(day=giorno), send_hour_local, tz)
    while cand <= adesso:
        mese = cand.month + 1
        anno = cand.year + (1 if mese > 12 else 0)
        cand = _orario(date(anno, mese % 12 or 12, giorno), send_hour_local, tz)
    return cand.astimezone(timezone.utc)


def prossimo_slot(u: Utente, adesso: datetime) -> datetime:
    """Come sopra, per l'utente che il worker ha in mano."""
    return calcola_slot(u.frequency, u.send_hour_local, u.send_weekday,
                        u.send_monthday, u.timezone, adesso)


def _costo_micros(cur, modello: str, totale: dict) -> int | None:
    """Costo in micros di dollaro, dal prezzo in tabella quando c'è.

    Con prezzo NULL il costo non si calcola — filosofia di model_pricing — ma
    i token restano misurati: il conto si completa dopo.
    """
    cur.execute("SELECT input_per_mtok, cached_per_mtok, output_per_mtok "
                "FROM model_pricing WHERE model = %s", (modello,))
    r = cur.fetchone()
    if (not r or r[0] is None or r[2] is None
            or not totale.get("complete", True)):
        return None
    inp, cached, out = totale["input"], totale.get("cached", 0), totale["output"]
    dollari = ((inp - cached) / 1e6 * float(r[0])
               + cached / 1e6 * float(r[1] if r[1] is not None else r[0])
               + out / 1e6 * float(r[2]))
    return round(dollari * 1e6)


def _digest_row(conn, u: Utente, started_at: datetime,
                scheduled_for: datetime | None = None) -> str:
    """Crea o riprende il digest dello slot effettivo, globale o override."""
    scheduled_for = (scheduled_for or _slot_digest(u, started_at)
                     or u.next_digest_at)
    if scheduled_for is None:
        raise ValueError("digest senza uno slot dovuto")
    with conn.cursor() as cur:
        cur.execute("SELECT id::text, status FROM digests "
                    "WHERE user_id = %s AND scheduled_for = %s",
                    (u.id, scheduled_for))
        r = cur.fetchone()
        if r:
            if r[1] in ("sent", "skipped_empty"):
                return ""
            cur.execute("UPDATE digests SET status = 'pending', started_at = %s, "
                        "attempt_count = attempt_count + 1 WHERE id = %s",
                        (started_at, r[0]))
            conn.commit()
            return r[0]
        cur.execute(
            "INSERT INTO digests (user_id, channel, scheduled_for, period_start, "
            "  period_end, started_at, attempt_count) VALUES (%s,%s,%s,%s,%s,%s,1) "
            "RETURNING id::text",
            (u.id, _canale_principale(u), scheduled_for, u.last_digest_at,
             scheduled_for, started_at))
        digest_id = cur.fetchone()[0]
    conn.commit()
    return digest_id


def _chiudi(conn, digest_id: str, *, status: str, valutate: int, inviate: int,
            error: str | None = None, message_id: str | None = None,
            commit: bool = True) -> None:
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE digests SET status = %s, "
            "  sent_at = CASE WHEN %s = 'sent' THEN now() END, "
            "  jobs_evaluated_count = %s, jobs_sent_count = %s, "
            "  provider_message_id = %s, error_message = %s WHERE id = %s",
            (status, status, valutate, inviate, message_id, error, digest_id))
    if commit:
        conn.commit()


def _segna_fallito(conn, u: Utente, errore: str,
                   scheduled_for: datetime | None = None) -> None:
    """Scrive il guasto sulla riga del digest, non solo nel log.

    NASCE DA UN GUASTO VERO. Il 2026-08-30 il credito GLM si e' esaurito e
    i digest sono falliti per quattro ore di fila. Il gestore in cima al
    giro registrava l'errore SOLO col logger: la riga restava `pending` con
    `error_message` vuoto, quindi niente che interroghi il database poteva
    accorgersene — gli allarmi compresi, che infatti uscivano puliti. Se ne
    e' accorto l'utente, che il digest lo aspettava.

    `failed` non blocca il ritentativo: `_digest_row` riporta a `pending`
    una riga fallita al giro successivo, e `next_digest_at` non e' stato
    avanzato — quindi appena la causa sparisce la consegna riparte da sola.

    Il rollback prima dell'UPDATE non e' prudenza: l'eccezione puo' aver
    lasciato la connessione in transazione abortita, e senza rollback anche
    questa scrittura fallirebbe — perdendo di nuovo la traccia, proprio nel
    momento in cui serve.
    """
    scheduled_for = (scheduled_for
                     or _slot_digest(u, datetime.now(timezone.utc))
                     or u.next_digest_at)
    try:
        conn.rollback()
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE digests SET status = 'failed', error_message = %s "
                "WHERE user_id = %s AND scheduled_for = %s "
                "  AND status NOT IN ('sent', 'skipped_empty')",
                (errore, u.id, scheduled_for))
        conn.commit()
    except Exception:  # noqa: BLE001
        log.warning("digest di %s: guasto non registrato a database", u.email)


def _items_del_digest(conn, user_id: str, ricerca_ids: list[str] | None = None,
                      includi_ereditate: bool = True) -> list[dict]:
    """Match consegnabili nello slot, con gli UUID delle ricerche eleggibili."""
    filtro_slot = ""
    params: list = []
    if ricerca_ids is not None or not includi_ereditate:
        filtro_slot = (" AND ((%s AND uc.frequency IS NULL) "
                       "OR uc.id = ANY(%s::uuid[]))")
        params += [includi_ereditate, list(ricerca_ids or [])]
    params += [user_id, MAX_ITEMS]
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(
            "SELECT m.id::text AS match_id, m.score, m.reason, "
            "       j.id::text AS id, j.source_job_id, j.title, "
            "       j.organization, j.cities, j.countries, "
            "       j.url, j.source, j.link_kind, j.employer_kind, j.salary, "
            "       j.date_posted, j.date_posted_declared, "
            "       j.ai_job_language, j.ai_experience_level, "
            "       j.ai_work_arrangement, j.ai_employment_type, "
            "       j.ai_visa_sponsorship, j.ai_key_skills, "
            "       j.ai_requirements_summary, j.ai_core_responsibilities, "
            "       j.raw->'ai_benefits' AS ai_benefits, j.ai_working_hours, "
            "       ricerca.ricerca_ids, contesto.wants, contesto.target_role, "
            "       contesto.desired_technologies, contesto.needs_visa_sponsorship "
            "FROM matches m "
            "JOIN jobs j ON j.id = m.job_id "
            "LEFT JOIN digest_items di ON di.match_id = m.id "
            "LEFT JOIN link_kinds lk ON lk.kind = j.link_kind "
            "LEFT JOIN employer_kinds ek ON ek.kind = j.employer_kind "
            "LEFT JOIN LATERAL ("
            "  SELECT COALESCE(array_agg(uc.id::text ORDER BY uc.created_at, uc.id), '{}') "
            "         AS ricerca_ids, "
            "         (jsonb_agg(jsonb_build_object("
            "           'wants', uc.wants, 'target_role', uc.target_role, "
            "           'desired_technologies', uc.desired_technologies, "
            "           'needs_visa_sponsorship', uc.needs_visa_sponsorship) "
            "          ORDER BY COALESCE(uc.min_score, 70), uc.cluster_id, uc.id) -> 0) "
            "         AS contesto "
            "  FROM job_clusters jc_r "
            "  JOIN user_clusters uc ON uc.cluster_id = jc_r.cluster_id "
            "  JOIN clusters c ON c.id = uc.cluster_id "
            "  WHERE jc_r.job_id = m.job_id AND uc.user_id = m.user_id "
            "    AND NOT uc.is_paused AND c.status = 'active' "
            "    AND m.score >= COALESCE(uc.min_score, 70) "
            + filtro_slot +
            ") ricerca ON TRUE "
            "LEFT JOIN LATERAL jsonb_to_record(ricerca.contesto) AS contesto("
            "  wants text, target_role text, desired_technologies text[], "
            "  needs_visa_sponsorship boolean) ON TRUE "
            "WHERE m.user_id = %s AND di.match_id IS NULL "
            "  AND cardinality(ricerca.ricerca_ids) > 0 "
            "  AND m.dismissed_at IS NULL AND m.archived_at IS NULL "
            "  AND m.application_status = 'not_started' "
            "  AND j.status = 'active' "
            "ORDER BY m.score DESC, lk.rank NULLS LAST, ek.rank NULLS LAST, "
            "         j.date_posted DESC, m.job_id "
            "LIMIT %s", params)
        items = cur.fetchall()

    visti: set[tuple[str, str]] = set()
    unici = []
    for it in items:
        it["_ricerca_ids"] = list(it["ricerca_ids"] or [])
        it["_wants"] = funnel.desiderio_ricerca(it)
        chiave = (re.sub(r"\s+", " ", (it["title"] or "").lower()).strip(),
                  re.sub(r"\s+", " ", (it["organization"] or "").lower()).strip())
        if chiave in visti:
            continue
        visti.add(chiave)
        unici.append(it)
    return unici


class _DryRunConnection:
    """Lascia leggere le scritture nella transazione, ma neutralizza i commit."""

    def __init__(self, conn):
        self._conn = conn

    def __getattr__(self, nome):
        return getattr(self._conn, nome)

    def commit(self):
        pass


def _digest_ha_items(conn, digest_id: str) -> bool:
    with conn.cursor() as cur:
        cur.execute("SELECT EXISTS (SELECT 1 FROM digest_items WHERE digest_id = %s)",
                    (digest_id,))
        return cur.fetchone()[0]


def _ids_ricerche_del_digest(conn, user_id: str, digest_id: str,
                             ricerca_ids: list[str], includi_ereditate: bool) -> set[str]:
    """UUID oggi eleggibili fra gli item già consegnati, senza rivalutarli."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT DISTINCT uc.id::text FROM digest_items di "
            "JOIN matches m ON m.id = di.match_id "
            "JOIN job_clusters jc ON jc.job_id = di.job_id "
            "JOIN user_clusters uc ON uc.cluster_id = jc.cluster_id "
            "                         AND uc.user_id = di.user_id "
            "JOIN clusters c ON c.id = uc.cluster_id "
            "WHERE di.digest_id = %s AND di.user_id = %s "
            "  AND NOT uc.is_paused AND c.status = 'active' "
            "  AND m.score >= COALESCE(uc.min_score, 70) "
            "  AND ((%s AND uc.frequency IS NULL) OR uc.id = ANY(%s::uuid[]))",
            (digest_id, user_id, includi_ereditate, list(ricerca_ids)))
        return {r[0] for r in cur.fetchall()}


def _recupera_digest(conn, u: Utente, adesso: datetime, scheduled_for: datetime,
                     globale_dovuta: bool, override_ids: list[str],
                     digest_id: str | None = None) -> None:
    """Chiude una consegna già provata dagli item e riprende lo scheduling.

    Non crea né modifica item e non chiama alcun canale: un pending/failed con
    item è una consegna interrotta dopo la registrazione, non un nuovo digest.
    """
    with conn.cursor() as cur:
        if digest_id is None:
            cur.execute(
                "SELECT id::text, status, jobs_evaluated_count, jobs_sent_count "
                "FROM digests WHERE user_id = %s AND scheduled_for = %s "
                "  AND status IN ('sent', 'skipped_empty')",
                (u.id, scheduled_for))
        else:
            cur.execute(
                "SELECT id::text, status, jobs_evaluated_count, jobs_sent_count "
                "FROM digests WHERE id = %s AND user_id = %s",
                (digest_id, u.id))
        riga = cur.fetchone()
        if riga is None:
            return
        digest_id, status, valutate_prima, inviate_prima = riga
        cur.execute("SELECT count(*) FROM digest_items WHERE digest_id = %s",
                    (digest_id,))
        n_items = cur.fetchone()[0]
        if n_items and (status != "sent" or inviate_prima != n_items
                        or valutate_prima < n_items):
            cur.execute(
                "UPDATE digests SET status = 'sent', "
                "  sent_at = COALESCE(sent_at, now()), "
                "  jobs_evaluated_count = GREATEST(jobs_evaluated_count, %s), "
                "  jobs_sent_count = %s, error_message = NULL WHERE id = %s",
                (n_items, n_items, digest_id))
    consegnate = (_ids_ricerche_del_digest(
        conn, u.id, digest_id, override_ids, globale_dovuta)
        if n_items else set())
    _rischedula(conn, u, adesso, consegnato=n_items > 0,
                scheduled_for=scheduled_for, ricerche_consegnate=consegnate)


def digest_utente(conn: psycopg.Connection, u: Utente, *, dry_run: bool = False,
                  threshold: int = THRESHOLD, evaluatore=None,
                  scheduled_for: datetime | None = None) -> dict:
    """Esegue il digest; in dry-run annulla ogni scrittura alla fine."""
    if not dry_run:
        return _digest_utente(conn, u, dry_run=False, threshold=threshold,
                              evaluatore=evaluatore, scheduled_for=scheduled_for)
    try:
        return _digest_utente(_DryRunConnection(conn), u, dry_run=True,
                              threshold=threshold, evaluatore=evaluatore,
                              scheduled_for=scheduled_for)
    finally:
        conn.rollback()


def _digest_utente(conn: psycopg.Connection, u: Utente, *, dry_run: bool,
                   threshold: int, evaluatore=None,
                   scheduled_for: datetime | None = None) -> dict:
    """Un utente, un digest. Ritorna il riepilogo per il log del giro."""
    started = datetime.now(timezone.utc)
    slot = scheduled_for or _slot_digest(u, started)
    esito = {"utente": u.email, "slot": slot, "valutate": 0,
             "inviate": 0, "stato": "?"}
    if slot is None:
        esito["stato"] = "non_dovuto"
        return esito
    globale_dovuta = (u.eredita_dovuta and u.next_digest_at is not None
                      and u.next_digest_at <= started)
    override_ids = [r.id for r in u.ricerche_dovute
                    if r.next_digest_at <= started]
    profilo_testo = profilo_come_testo(u.profilo)

    digest_id = _digest_row(conn, u, started, slot)
    if not digest_id:
        _recupera_digest(conn, u, started, slot, globale_dovuta, override_ids)
        esito["stato"] = "già consegnato"
        return esito
    if _digest_ha_items(conn, digest_id):
        _recupera_digest(conn, u, started, slot, globale_dovuta, override_ids,
                         digest_id)
        esito["stato"] = "già consegnato"
        return esito

    # Senza CV non c'è profilo, e GLM giudicherebbe contro «Ruolo cercato: —»
    # spendendo budget per punteggi privi di senso. Non è un errore
    # dell'utente ma uno stato incompleto: il digest lo dice e si riprova al
    # prossimo slot, quando magari il CV ci sarà.
    if not u.cv_id:
        _chiudi(conn, digest_id, status="failed", valutate=0, inviate=0,
                error="nessun CV attivo: profilo non valutabile", commit=False)
        _rischedula(conn, u, started, consegnato=False, scheduled_for=slot,
                    ricerche_consegnate=set())
        esito["stato"] = "failed_senza_cv"
        return esito

    lingua_glm = LINGUA_PER_GLM.get(u.locale, "English")
    vettore = None
    if os.environ.get("PRESCORE_V3_ANN") == "1" and prescore_v3.disponibile():
        try:
            copertura = funnel.copertura_embedding(conn, u.id, override_ids, globale_dovuta)
            if copertura >= 0.98:
                vettore = prescore_v3.query_embedding(profilo_testo)
            else:
                log.warning("%s: cache embedding copre %.1f%% — uso il funnel completo",
                            u.email, copertura * 100)
        except Exception as exc:  # noqa: BLE001
            log.warning("%s: ANN non disponibile (%s) — uso il funnel completo",
                        u.email, exc)
    lingue_profilo = {x for x in u.profilo.get("lingue", []) if isinstance(x, str)}
    try:
        candidati = funnel.candidati(
            conn, u.id, override_ids, globale_dovuta, vettore=vettore,
            lingue_profilo=lingue_profilo)
    except Exception as exc:
        if vettore is None:
            raise
        log.warning("%s: retrieval ANN fallito (%s) — uso il funnel completo", u.email, exc)
        candidati = funnel.candidati(
            conn, u.id, override_ids, globale_dovuta, lingue_profilo=lingue_profilo)

    # Il retrieval v3 è opzionale e disabilitato senza PRESCORE_V3_URL.
    # Gli scartati non prendono una riga matches e restano per il giro dopo.
    retrieval_v3 = False
    if candidati and prescore_v3.disponibile():
        try:
            punteggi = prescore_v3.rank_batch(profilo_testo, candidati)
            lingue = {x for x in u.profilo.get("lingue", []) if isinstance(x, str)}
            cap = prescore_v3.cap_adattivo(u.profilo.get("ruolo"))
            indici = prescore_v3.seleziona_top(punteggi, candidati, lingue,
                                               max_items=cap)
            log.info("%s: retrieval v3: %d candidati, %d selezionati (cap %d)",
                     u.email, len(candidati), len(indici), cap)
            candidati = [candidati[i] for i in indici]
            retrieval_v3 = True
        except Exception as exc:  # noqa: BLE001
            log.warning("%s: retrieval v3 irraggiungibile (%s) — uso v1", u.email, exc)

    if not retrieval_v3 and candidati and prescore_mod.disponibile():
        try:
            punteggi = prescore_mod.prescore_batch(
                profilo_testo, [offerta_per_prompt(c) for c in candidati])
            indici = prescore_mod.seleziona_per_glm(
                punteggi,
                [c.get("_min_score") or threshold for c in candidati],
                max_items=MAX_ITEMS)
            log.info("%s: prescore: %d candidati, %d valutati, risparmiate "
                     "%d chiamate", u.email, len(candidati), len(indici),
                     len(candidati) - len(indici))
            candidati = [candidati[i] for i in indici]
        except Exception as exc:  # noqa: BLE001
            log.warning("%s: prescore irraggiungibile (%s) — fallback con la "
                        "catena configurata entro il cap del digest", u.email, exc)

    # Il cap resta 30 con v1 o giudici esterni; v3 ne usa 150 solo in locale.
    cap_valutazioni = (prescore_v3.MAX_CANDIDATI
                       if retrieval_v3 and MaestroLocale.disponibile() else MAX_ITEMS)
    candidati = candidati[:cap_valutazioni]

    # Il guardrail HR agisce solo sui pochi candidati gia' selezionati: non
    # riempie i posti liberati e in caso di guasto non scarta nessuno.
    if candidati and prescore_mod.family_guard_disponibile():
        try:
            distribuzioni = []
            for i in range(0, len(candidati), 30):
                distribuzioni.extend(prescore_mod.famiglia_batch(candidati[i:i + 30]))
            prima = len(candidati)
            candidati = [c for c, p in zip(candidati, distribuzioni)
                         if not prescore_mod.incompatibile_hr(c.get("_families"), p)]
            log.info("%s: family guard HR: %d candidati esclusi",
                     u.email, prima - len(candidati))
        except Exception as exc:  # noqa: BLE001
            log.warning("%s: family guard non disponibile (%s) — "
                        "candidati conservati", u.email, exc)

    budget_finito = False
    if candidati:
        if evaluatore is None:
            evaluatore = ValutatoreGLM()
        # La lettura dimensiona e avvisa; il CONSUMO vero è per offerta,
        # atomico e solo esterno. Il limite si applica nel ciclo perché un
        # fallback può cambiare il provider della valutazione successiva.
        with conn.cursor() as cur:
            cur.execute("SELECT q.monthly_evaluations, "
                        "COALESCE(b.evaluations_used, 0) "
                        "FROM plan_quotas q "
                        "LEFT JOIN user_evaluation_budget b ON b.user_id = %s "
                        "  AND b.period_month = date_trunc('month', current_date)::date "
                        "WHERE q.plan = %s", (u.id, u.plan))
            cap, usate = cur.fetchone()
        rimaste = cap - usate
        addebita_iniziale = _addebita_valutazione(evaluatore)
        if addebita_iniziale and rimaste <= 0:
            budget_finito = True
            log.warning("%s: budget di valutazione esaurito (%s/%s) — non valuto, "
                        "ma consegno ciò che resta da spedire", u.email, usate, cap)
            candidati = []
        elif addebita_iniziale and len(candidati) > rimaste:
            log.warning("%s: %d candidati ma %d valutazioni esterne rimaste — "
                        "le più vecchie restano al prossimo giro",
                        u.email, len(candidati), rimaste)

    try:
        # Prima passata: punteggio (e micro-motivazione) su tutte.
        valutate_adesso: set[str] = set()
        for offerta in candidati:
            addebita = _addebita_valutazione(evaluatore)
            if addebita and rimaste <= 0:
                budget_finito = True
                log.warning("%s: budget finito a metà digest — fermo "
                            "le valutazioni", u.email)
                break
            score, reason, uso = evaluatore.valuta(profilo_testo, offerta,
                                                   lingua_glm)
            # La quota esterna si consuma solo DOPO una risposta valida. Se è
            # avvenuto un fallback, vale il provider presente all'inizio di
            # questa offerta: il ripiego di emergenza non addebita due volte.
            if addebita:
                if not dry_run:
                    with conn.cursor() as cur:
                        cur.execute("SELECT user_try_evaluate(%s, 1)", (u.id,))
                        if not cur.fetchone()[0]:
                            budget_finito = True
                            log.warning("%s: budget finito a metà digest — fermo "
                                        "le valutazioni", u.email)
                            break
                rimaste -= 1
            soglia = offerta.get("_min_score") or threshold
            modello_usato = uso.get("model") or getattr(
                getattr(evaluatore, "model", None), "model", "custom")
            provider_usato = uso.get("provider") or getattr(
                getattr(evaluatore, "model", None), "source", None)
            with conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO matches (user_id, job_id, cv_id, score, reason, "
                    "  threshold_used, model, provider, rubric_version, input_tokens, "
                    "  output_tokens) "
                    "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) "
                    "ON CONFLICT (user_id, job_id) DO UPDATE SET "
                    "  cv_id = EXCLUDED.cv_id, score = EXCLUDED.score, "
                    "  reason = EXCLUDED.reason, threshold_used = EXCLUDED.threshold_used, "
                    "  model = EXCLUDED.model, provider = EXCLUDED.provider, "
                    "  rubric_version = EXCLUDED.rubric_version, "
                    "  input_tokens = EXCLUDED.input_tokens, "
                    "  output_tokens = EXCLUDED.output_tokens, evaluated_at = now() "
                    "WHERE matches.rubric_version IS DISTINCT FROM EXCLUDED.rubric_version",
                    (u.id, offerta["id"], u.cv_id, score, reason, soglia,
                     modello_usato, provider_usato, VERSIONE_RUBRICA,
                     uso.get("input"), uso.get("output")))
            esito["valutate"] += 1
            valutate_adesso.add(offerta["id"])
            # Match ed eventuale quota diventano visibili insieme a questo
            # commit; un crash non costringe a ripagare le risposte già valide.
            conn.commit()

        items = _items_del_digest(conn, u.id, override_ids, globale_dovuta)

        if not items:
            # Niente da spedire. Se per di più il budget è a zero, il digest
            # fallisce col motivo esplicito: è l'esito che merita un allarme.
            if budget_finito and not esito["valutate"]:
                _chiudi(conn, digest_id, status="failed", valutate=0, inviate=0,
                        error=f"budget di valutazione esaurito ({usate}/{cap}, piano {u.plan})",
                        commit=False)
                esito["stato"] = "failed_budget"
            else:
                _chiudi(conn, digest_id, status="skipped_empty",
                        valutate=esito["valutate"], inviate=0, commit=False)
                esito["stato"] = "skipped_empty"
            _rischedula(conn, u, started, consegnato=False, scheduled_for=slot,
                        ricerche_consegnate=set())
            return esito

        if "whatsapp" in u.delivery_channels:
            # Il template promette «Reply STOP to stop this digest», e una
            # promessa stampata in ogni digest si mantiene PRIMA di inviare,
            # non dopo un reclamo. Oltre al rispetto dovuto, e' cio' che
            # protegge il punteggio qualita' del numero — il vincolo vero
            # del canale, che nessuna verifica aziendale ripara.
            if u.whatsapp_conversation_id and not dry_run:
                try:
                    if whatsapp_mod.ha_chiesto_stop(u.whatsapp_conversation_id):
                        log.info("%s: STOP ricevuto su WhatsApp, stacco il canale",
                                 u.email)
                        _stacca_canale(conn, u, "whatsapp",
                                       "hai risposto STOP su WhatsApp")
                except Exception:
                    pass  # non riuscire a leggere non e' una richiesta di stop
        if u.delivery_channels == ["whatsapp"] and len(items) > 3:
            # I template hanno caselle fisse: tre offerte al massimo
            # (nivult_digest_1/2/3). Il taglio GLOBALE pero' vale solo se
            # WhatsApp e' l'UNICO canale: se c'e' anche l'email, e' lei a
            # portare il digest intero e WhatsApp prende le sue tre in
            # consegna — tagliare tutto per il canale piu' stretto
            # degraderebbe quello largo. Solo-WhatsApp: le altre restano
            # fuori da digest_items e il recupero del giro successivo le
            # riprende. Il taglio sta QUI, prima della motivazione:
            # motivare offerte che non partono sarebbe spesa GLM buttata.
            log.info("%s: %d offerte, WhatsApp ne porta 3 — %d rinviate "
                     "al prossimo digest", u.email, len(items), len(items) - 3)
            items = items[:3]

        # Seconda passata: la motivazione vera solo per ciò che viene inviato.
        if evaluatore is None:
            evaluatore = ValutatoreGLM()
        scritte: list[str] = []
        for item in items:
            item["reason"], analisi, uso_analisi = evaluatore.motiva(
                profilo_testo, item, lingua_glm, u.locale, evita=scritte)
            if item["reason"] not in scritte:
                scritte.append(item["reason"])
            modello_analisi = uso_analisi.get("model") or getattr(
                getattr(evaluatore, "model", None), "model", "custom")
            provider_analisi = uso_analisi.get("provider") or getattr(
                getattr(evaluatore, "model", None), "source", None)
            with conn.cursor() as cur:
                cur.execute(
                    "UPDATE matches SET reason = %s, analysis = %s, "
                    "  analysis_model = %s, analysis_provider = %s, "
                    "  analysis_input_tokens = %s, analysis_output_tokens = %s "
                    "WHERE id = %s",
                    (item["reason"], Json(analisi), modello_analisi,
                     provider_analisi, uso_analisi.get("input"),
                     uso_analisi.get("output"), item["match_id"]))
            conn.commit()

        destinatario = u.delivery_email or u.email

        # UN GIRO SUI CANALI ATTIVI, non un ramo solo: l'utente puo' averne
        # piu' d'uno e ogni canale consegna il suo formato — l'email il
        # digest intero, Telegram lo stesso in chat, WhatsApp le prime tre
        # via template. Un canale che inciampa non ferma gli altri: si
        # annota e si prosegue, perche' il digest e' gia' stato pagato in
        # valutazioni e UNA consegna riuscita vale piu' della purezza.
        riusciti: list[str] = []
        problemi: list[str] = []
        message_id = None

        if dry_run:
            percorsoHtml = email_mod.anteprima(destinatario, items, u.locale)
            log.info("%s: DRY RUN su %s, email compilata in %s", u.email,
                     ",".join(u.delivery_channels), percorsoHtml)
        else:
            for canale in list(u.delivery_channels):
                try:
                    if canale == "app":
                        # Cambio di paradigma (Mavult): il digest non e' una
                        # lettera spedita ma una pagina che esiste — la tab
                        # Digest legge digest_items, e la push nativa e'
                        # l'avviso. La consegna E' la registrazione qui
                        # sotto: niente da chiamare, niente che possa
                        # fallire. Non tocca `message_id`: non c'e' un
                        # provider da tracciare.
                        pass
                    elif canale == "email":
                        mid = email_mod.invia(destinatario, items, u.locale)
                        # L'id registrato e' quello dell'email quando c'e':
                        # e' il canale con la tracciabilita' migliore.
                        message_id = mid
                    elif canale == "telegram" and u.telegram_chat_id:
                        mid = telegram_mod.invia(u.telegram_chat_id, items,
                                                 u.locale,
                                                 nome=u.display_name)
                        message_id = message_id or mid
                    elif canale == "whatsapp" and u.whatsapp_e164:
                        mid, conv = whatsapp_mod.invia(
                            u.whatsapp_e164, items[:3], u.locale,
                            nome=u.display_name)
                        if conv and conv != u.whatsapp_conversation_id:
                            with conn.cursor() as cur:
                                cur.execute(
                                    "UPDATE users SET "
                                    "whatsapp_conversation_id = %s "
                                    "WHERE id = %s", (conv, u.id))
                            conn.commit()
                        message_id = message_id or mid
                    else:
                        continue
                    riusciti.append(canale)
                    _canale_ok(conn, u, canale)
                except telegram_mod.BotBloccato as exc:
                    log.warning("%s: bot bloccato (%s), stacco Telegram",
                                u.email, exc)
                    _stacca_canale(conn, u, "telegram",
                                   "il bot e' stato bloccato")
                    problemi.append("telegram: bot bloccato")
                except whatsapp_mod.OptOut:
                    log.warning("%s: opt-out WhatsApp, stacco il canale",
                                u.email)
                    _stacca_canale(conn, u, "whatsapp",
                                   "il numero risulta disiscritto")
                    problemi.append("whatsapp: opt-out")
                except whatsapp_mod.TemplateNonPronto as exc:
                    log.warning("%s: template WhatsApp non pronto (%s)",
                                u.email, exc)
                    problemi.append("whatsapp: template non pronto")
                    _canale_fallito(conn, u, "whatsapp")
                except Exception as exc:
                    log.warning("%s: consegna %s fallita: %s", u.email,
                                canale, exc)
                    problemi.append(f"{canale}: {exc}")
                    fallimenti = _canale_fallito(conn, u, canale)
                    if canale != "email" and fallimenti >= 2:
                        _stacca_canale(conn, u, canale,
                                       "consegna fallita due volte")

            if not riusciti:
                # NESSUN canale ha consegnato. L'email di riserva parte
                # anche se non era fra i canali scelti: un digest pagato e
                # mai consegnato e' il danno peggiore, e la casella c'e'
                # sempre. Se fallisce anche lei, l'eccezione risale e il
                # digest resta pending per il ritento orario.
                # Chi ha 'app' fra i canali invece non la riceve MAI: per
                # lui la consegna e' la pagina nell'app, e un'email non
                # richiesta sarebbe solo rumore. (Di fatto il ramo 'app'
                # riesce sempre e qui non ci si arriva — il guard rail
                # resta, perche' il ramo riserva appartiene a Nivult.)
                if ("email" not in u.delivery_channels
                        and "app" not in u.delivery_channels):
                    message_id = email_mod.invia(destinatario, items, u.locale)
                    riusciti.append("email(riserva)")
                    _canale_ok(conn, u, "email")
                else:
                    raise RuntimeError("; ".join(problemi) or
                                       "nessuna consegna riuscita")

        # Le voci del digest si registrano DOPO l'invio riuscito: scriverle
        # prima renderebbe un invio fallito indistinguibile da uno avvenuto,
        # e l'anti-join del retry scarterebbe offerte mai consegnate. Il
        # costo di quest'ordine è il caso limite invio-riuscito-crash-prima-
        # del-registro: al retry l'email parte due volte. Meglio un doppione
        # raro che un digest perso.
        with conn.cursor() as cur:
            for pos, item in enumerate(items, start=1):
                cur.execute(
                    "INSERT INTO digest_items (digest_id, job_id, user_id, match_id, "
                    "  rank, score_snapshot, reason_snapshot) VALUES (%s,%s,%s,%s,%s,%s,%s) "
                    "ON CONFLICT (digest_id, job_id) DO NOTHING",
                    (digest_id, item["id"], u.id, item["match_id"], pos,
                     item["score"], item["reason"]))
        conn.commit()

        # jobs_evaluated_count: le valutazioni che ALIMENTANO questo digest —
        # quelle pagate in questo run più le recuperate da un tentativo
        # precedente. È ciò che soddisfa il vincolo sent <= evaluated: un
        # digest può consegnare offerte valutate prima di lui.
        alimentano = esito["valutate"] + sum(
            1 for it in items if it["id"] not in valutate_adesso)
        _chiudi(conn, digest_id, status="sent", valutate=alimentano,
                inviate=len(items), message_id=message_id, commit=False)
        ricerche_consegnate = {ricerca_id for item in items
                               for ricerca_id in item["_ricerca_ids"]}
        _rischedula(conn, u, started, consegnato=True, scheduled_for=slot,
                    ricerche_consegnate=ricerche_consegnate)
        if not dry_run:
            try:
                push_mod.invia_digest_pronto(conn, u.id)
            except Exception as exc:  # noqa: BLE001
                log.warning("%s: avviso push fallito dopo digest consegnato: %s",
                            u.email, exc)
        esito["stato"] = "dry_run" if dry_run else "sent"
        esito["inviate"] = len(items)
        return esito
    finally:
        if not dry_run and isinstance(evaluatore, ValutatoreGLM):
            with conn.cursor() as cur:
                for (provider, modello, operazione), totale in \
                        evaluatore.totali.items():
                    if not totale["chiamate"]:
                        continue
                    cur.execute(
                        "INSERT INTO api_usage (provider, operation, model, user_id, "
                        "  requests, input_tokens, cached_tokens, output_tokens, "
                        "  cost_micros) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                        (provider, operazione, modello, u.id, totale["chiamate"],
                         totale["input"], totale["cached"], totale["output"],
                         _costo_micros(cur, modello, totale)))
            conn.commit()


def _rischedula(conn, u: Utente, adesso: datetime, *, consegnato: bool,
                scheduled_for: datetime | None = None,
                ricerche_consegnate: set[str] | None = None) -> None:
    """Rischedula senza sovrascrivere una modifica API più recente.

    La consegna aggiorna sempre last_digest_at in modo monotono. Il prossimo
    slot si muove soltanto se scadenza e configurazione lette all'inizio sono
    ancora correnti; questo rende anche i retry terminali idempotenti.
    """
    scheduled_for = (scheduled_for or _slot_digest(u, adesso)
                     or u.next_digest_at)
    legacy = ricerche_consegnate is None
    consegnate = ricerche_consegnate or set()
    globale_dovuta = (u.eredita_dovuta and u.next_digest_at is not None
                      and u.next_digest_at <= adesso)

    def last_globale(cur, valore) -> None:
        cur.execute(
            "UPDATE users SET last_digest_at = CASE "
            "  WHEN %s::timestamptz IS NULL THEN last_digest_at "
            "  ELSE GREATEST(COALESCE(last_digest_at, %s::timestamptz), %s::timestamptz) END "
            "WHERE id = %s", (valore, valore, valore, u.id))

    def last_ricerca(cur, ricerca_id: str, valore) -> None:
        cur.execute(
            "UPDATE user_clusters SET last_digest_at = CASE "
            "  WHEN %s::timestamptz IS NULL THEN last_digest_at "
            "  ELSE GREATEST(COALESCE(last_digest_at, %s::timestamptz), %s::timestamptz) END "
            "WHERE id = %s AND user_id = %s",
            (valore, valore, valore, ricerca_id, u.id))

    with conn.cursor() as cur:
        if globale_dovuta:
            cur.execute(
                "SELECT count(*) FILTER (WHERE NOT uc.is_paused AND c.status = 'active'), "
                "       count(*) FILTER (WHERE uc.id = ANY(%s::uuid[])) "
                "FROM user_clusters uc JOIN clusters c ON c.id = uc.cluster_id "
                "WHERE uc.user_id = %s AND uc.frequency IS NULL",
                (list(consegnate), u.id))
            ereditate, ereditate_consegnate = cur.fetchone()
            contenuto_globale = consegnato and (legacy or ereditate_consegnate > 0)
            if contenuto_globale:
                cur.execute(
                    "UPDATE user_clusters SET last_digest_at = CASE "
                    "  WHEN %s::timestamptz IS NULL THEN last_digest_at "
                    "  ELSE GREATEST(COALESCE(last_digest_at, %s::timestamptz), %s::timestamptz) END "
                    "WHERE user_id = %s AND frequency IS NULL "
                    "  AND (%s OR id = ANY(%s::uuid[]))",
                    (scheduled_for, scheduled_for, scheduled_for,
                     u.id, legacy, list(consegnate)))
            if not contenuto_globale and u.last_digest_at is None and ereditate:
                prossimo = adesso + timedelta(days=1)
            else:
                prossimo = prossimo_slot(u, adesso)
            last_globale(cur, scheduled_for if contenuto_globale else u.last_digest_at)
            cur.execute(
                "UPDATE users SET next_digest_at = %s "
                "WHERE id = %s AND next_digest_at = %s AND frequency = %s "
                "  AND send_hour_local = %s "
                "  AND send_weekday IS NOT DISTINCT FROM %s "
                "  AND send_monthday IS NOT DISTINCT FROM %s AND timezone = %s "
                "  AND EXISTS (SELECT 1 FROM user_clusters uc_e "
                "              JOIN clusters c_e ON c_e.id = uc_e.cluster_id "
                "              WHERE uc_e.user_id = users.id AND uc_e.frequency IS NULL "
                "                AND NOT uc_e.is_paused AND c_e.status = 'active')",
                (prossimo, u.id, u.next_digest_at, u.frequency,
                 u.send_hour_local, u.send_weekday, u.send_monthday, u.timezone))

        for ricerca in u.ricerche_dovute:
            if ricerca.next_digest_at > adesso:
                continue
            contenuto = consegnato and (legacy or ricerca.id in consegnate)
            if not contenuto and ricerca.last_digest_at is None:
                prossimo = adesso + timedelta(days=1)
            else:
                prossimo = calcola_slot(
                    ricerca.frequency, ricerca.send_hour_local,
                    ricerca.send_weekday, ricerca.send_monthday,
                    ricerca.timezone, adesso)
            last_ricerca(cur, ricerca.id,
                         scheduled_for if contenuto else ricerca.last_digest_at)
            cur.execute(
                "UPDATE user_clusters uc SET next_digest_at = %s FROM users u, clusters c "
                "WHERE u.id = uc.user_id AND c.id = uc.cluster_id "
                "  AND NOT uc.is_paused AND c.status = 'active' "
                "  AND uc.id = %s AND uc.user_id = %s "
                "  AND uc.next_digest_at = %s AND uc.frequency = %s "
                "  AND uc.send_weekday IS NOT DISTINCT FROM %s "
                "  AND uc.send_monthday IS NOT DISTINCT FROM %s "
                "  AND uc.send_hour_local IS NOT DISTINCT FROM %s "
                "  AND uc.timezone IS NOT DISTINCT FROM %s "
                "  AND COALESCE(uc.send_hour_local, u.send_hour_local) = %s "
                "  AND COALESCE(uc.timezone, u.timezone) = %s",
                (prossimo, ricerca.id, u.id, ricerca.next_digest_at,
                 ricerca.frequency, ricerca.send_weekday, ricerca.send_monthday,
                 ricerca.send_hour_local_raw, ricerca.timezone_raw,
                 ricerca.send_hour_local, ricerca.timezone))
    conn.commit()


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="nivult.matching.worker", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--user", help="elabora solo questo utente (uuid)")
    ap.add_argument("--dry-run", action="store_true",
                    help="simula il digest e annulla match, usage, slot e consegna")
    ap.add_argument("--threshold", type=int, default=THRESHOLD,
                    help=f"soglia di superamento (default {THRESHOLD})")
    args = ap.parse_args(argv)

    load_dotenv()
    logging.basicConfig(level=logging.INFO, format="%(levelname)-7s %(message)s",
                        stream=sys.stderr)
    dsn = database_url()
    print(f"database: {safe_dsn(dsn)}")

    adesso = datetime.now(timezone.utc)
    esiti: list[dict] = []
    with psycopg.connect(dsn) as conn:
        with conn.cursor(row_factory=dict_row) as cur:
            utenti = utenti_dovuti(cur, adesso, args.user)
        print(f"{len(utenti)} utenti dovuti"
              f"{' — DRY RUN, nulla verrà inviato' if args.dry_run else ''}\n")
        for u in utenti:
            slot = _slot_digest(u, adesso)
            try:
                e = digest_utente(conn, u, dry_run=args.dry_run,
                                  threshold=args.threshold, scheduled_for=slot)
            except Exception as exc:  # noqa: BLE001
                log.error("digest di %s fallito: %s", u.email, exc)
                if not args.dry_run:
                    _segna_fallito(conn, u, str(exc)[:500], slot)
                e = {"utente": u.email, "stato": "errore", "errore": str(exc)[:200]}
            esiti.append(e)
            log.info("%s: %s (valutate %s, inviate %s)", u.email, e["stato"],
                     e.get("valutate", 0), e.get("inviate", 0))

    print("\nriepilogo:")
    contatore: dict[str, int] = {}
    for e in esiti:
        contatore[e["stato"]] = contatore.get(e["stato"], 0) + 1
    for stato, n in sorted(contatore.items()):
        print(f"  {stato:<14} {n}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
