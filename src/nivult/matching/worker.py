"""Il worker dei digest: per ogni utente dovuto, valuta e consegna.

    python -m nivult.matching.worker                 # tutti gli utenti dovuti
    python -m nivult.matching.worker --user <uuid>   # un utente solo
    python -m nivult.matching.worker --dry-run       # valuta e compila, non invia

Il ciclo per un utente:

    candidati  = offerte attive dei suoi cluster, mai valutate per lui,
                 sopravvissute ai filtri deterministici (funnel.py)
    budget     = user_try_evaluate: il costo del giudizio è PER UTENTE
    valutazione = GLM, una offerta per chiamata (misurato: nel lotto il modello
                 distribuisce i voti sulla scala del lotto)
    digest     = chi supera la soglia, ordinato per punteggio e poi per
                 trasparenza del link e del datore; motivazione solo per le
                 prime 30 (seconda passata)
    consegna   = email; vuoto è un esito legittimo (skipped_empty)

Tutto è riprendibile: l'anti-ripetizione su matches fa sì che un worker
interrotto e rilanciato non rivaluti nulla di già pagato, e la UNIQUE
(user_id, scheduled_for) su digests fa sì che lo slot non venga consegnato
due volte. Il ruolo della connessione è nivult_app: il worker fa DML, non DDL.
"""

from __future__ import annotations

import argparse
import logging
import sys
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

import psycopg
from psycopg.types.json import Json
from psycopg.rows import dict_row

from nivult.config import database_url, load_dotenv, safe_dsn
from nivult.delivery import email as email_mod
from nivult.delivery import telegram as telegram_mod
from nivult.delivery import whatsapp as whatsapp_mod
from nivult.matching import funnel
from nivult.delivery.testi import LINGUA_PER_GLM, t
from nivult.matching.llm import (GLM, motiva_e_analizza, profilo_come_testo,
                                 valuta_offerta)

log = logging.getLogger("nivult.matching.worker")

# Soglia alta: meglio un digest vuoto che un digest scadente.
THRESHOLD = 80
# Le "prime 30" delle decisioni di architettura: la motivazione di qualità
# si paga solo per ciò che il destinatario vedrà davvero.
MAX_ITEMS = 30
MODELLO = "glm-5.2"


@dataclass(slots=True)
class Utente:
    id: str
    email: str
    plan: str
    delivery_channels: list[str]
    delivery_email: str | None
    telegram_chat_id: str | None
    whatsapp_e164: str | None
    whatsapp_conversation_id: str | None
    frequency: str
    send_hour_local: int
    send_weekday: int | None
    send_monthday: int | None
    timezone: str
    next_digest_at: datetime
    last_digest_at: datetime | None
    cv_id: str | None
    locale: str = "en"
    delivery_failures: int = 0
    profilo: dict = field(default_factory=dict)


class ValutatoreGLM:
    """Il valutatore vero: GLM una-offerta-per-chiamata, con i contatori."""

    def __init__(self):
        self.model = GLM(rate_per_second=4.0)
        self.totale = {"input": 0, "cached": 0, "output": 0, "chiamate": 0}

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.model.close()

    def valuta(self, profilo_testo: str, offerta: dict, lingua: str = "English"):
        # noqa: il desiderio sta dentro l'offerta, messo lì dal funnel.
        """-> (punteggio, micro-motivazione, uso token della chiamata)."""
        score, reason, uso = valuta_offerta(self.model, profilo_testo, offerta,
                                            offerta.get("_wants"), lingua)
        for k in ("input", "cached", "output"):
            self.totale[k] += uso.get(k, 0)
        self.totale["chiamate"] += 1
        return score, reason, uso

    def motiva(self, profilo_testo: str, offerta: dict, lingua: str = "English"):
        # Motivazione E analisi insieme: la chiamata c'era comunque, e
        # l'analisi qui costa solo output — al clic sarebbe costata
        # l'intero prefisso a cache fredda.
        reason, analisi, uso = motiva_e_analizza(self.model, profilo_testo,
                                                 offerta, offerta.get("_wants"),
                                                 lingua)
        for k in ("input", "cached", "output"):
            self.totale[k] += uso.get(k, 0)
        self.totale["chiamate"] += 1
        return reason, analisi, uso


def _lingua(v) -> str:
    """Il CV conserva le lingue come jsonb: stringa o oggetto, non decidiamo noi."""
    if isinstance(v, str):
        return v
    return (v or {}).get("language") or (v or {}).get("name") or ""


def _profilo(cv: dict | None) -> dict:
    if not cv:
        return {}
    return {
        "ruolo": ", ".join(cv["families"] or []),
        "seniority": cv.get("seniority_label") or cv.get("seniority") or "—",
        "competenze": list(cv["skills"] or []),
        "lingue": [l for l in map(_lingua, cv["languages"] or []) if l],
        "sedi": [],
        "note": (f"{cv['years_experience']} anni di esperienza"
                 if cv.get("years_experience") else None),
    }


def utenti_dovuti(cur, adesso: datetime, user_id: str | None = None) -> list[Utente]:
    sql = (
        "SELECT u.id::text, u.email::text, u.plan, u.delivery_channels, "
        "       u.delivery_email::text, u.telegram_chat_id, u.whatsapp_e164, "
        "       u.whatsapp_conversation_id, "
        "       u.frequency, u.send_hour_local, u.send_weekday, u.send_monthday, "
        "       u.timezone, u.next_digest_at, u.last_digest_at, u.locale, "
        "       u.delivery_failures, "
        "       cv.id::text AS cv_id, cv.families, cv.seniority, cv.skills, "
        "       cv.languages, cv.years_experience, e.label AS seniority_label "
        "FROM users u "
        "LEFT JOIN user_cvs cv ON cv.user_id = u.id AND cv.status = 'active' "
        "LEFT JOIN experience_levels e ON e.code = cv.seniority "
        "WHERE u.status = 'active' "
        "  AND u.subscription_status IN ('trialing','active') "
        "  AND u.next_digest_at IS NOT NULL AND u.next_digest_at <= %s")
    params: list = [adesso]
    if user_id:
        sql += " AND u.id = %s"
        params.append(user_id)
    cur.execute(sql + " ORDER BY u.next_digest_at", params)
    return [Utente(
        id=r["id"], email=r["email"], plan=r["plan"],
        delivery_channels=r["delivery_channels"], delivery_email=r["delivery_email"],
        telegram_chat_id=r["telegram_chat_id"], whatsapp_e164=r["whatsapp_e164"],
        whatsapp_conversation_id=r["whatsapp_conversation_id"],
        frequency=r["frequency"], send_hour_local=r["send_hour_local"],
        send_weekday=r["send_weekday"], send_monthday=r["send_monthday"],
        timezone=r["timezone"], next_digest_at=r["next_digest_at"],
        last_digest_at=r["last_digest_at"], cv_id=r["cv_id"],
        locale=r["locale"], delivery_failures=r["delivery_failures"],
        profilo=_profilo(r)) for r in cur.fetchall()]


def _canale_ok(conn, u) -> None:
    """Una consegna riuscita azzera il contatore dei guasti."""
    if u.delivery_failures:
        with conn.cursor() as cur:
            cur.execute("UPDATE users SET delivery_failures = 0 WHERE id = %s",
                        (u.id,))
        conn.commit()


def _canale_fallito(conn, u) -> int:
    """Segna un guasto e restituisce quanti ne sono andati storti di fila."""
    with conn.cursor() as cur:
        cur.execute("UPDATE users SET delivery_failures = delivery_failures + 1 "
                    "WHERE id = %s RETURNING delivery_failures", (u.id,))
        n = cur.fetchone()[0]
    conn.commit()
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
                    "  delivery_failures = 0 WHERE id = %s", (rimasti, u.id))
    conn.commit()
    u.delivery_channels = rimasti
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
    if not r or r[0] is None or r[2] is None:
        return None
    inp, cached, out = totale["input"], totale.get("cached", 0), totale["output"]
    dollari = ((inp - cached) / 1e6 * float(r[0])
               + cached / 1e6 * float(r[1] if r[1] is not None else r[0])
               + out / 1e6 * float(r[2]))
    return round(dollari * 1e6)


def _digest_row(conn, u: Utente, started_at: datetime) -> str:
    """La riga del digest per questo slot: creata, o ripresa se un tentativo
    precedente è rimasto a metà (pending/failed)."""
    with conn.cursor() as cur:
        cur.execute("SELECT id::text, status FROM digests "
                    "WHERE user_id = %s AND scheduled_for = %s",
                    (u.id, u.next_digest_at))
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
            (u.id, _canale_principale(u), u.next_digest_at, u.last_digest_at,
             u.next_digest_at, started_at))
        digest_id = cur.fetchone()[0]
    conn.commit()
    return digest_id


def _chiudi(conn, digest_id: str, *, status: str, valutate: int, inviate: int,
            error: str | None = None, message_id: str | None = None) -> None:
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE digests SET status = %s, "
            "  sent_at = CASE WHEN %s = 'sent' THEN now() END, "
            "  jobs_evaluated_count = %s, jobs_sent_count = %s, "
            "  provider_message_id = %s, error_message = %s WHERE id = %s",
            (status, status, valutate, inviate, message_id, error, digest_id))
    conn.commit()


def _segna_fallito(conn, u: Utente, errore: str) -> None:
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
    try:
        conn.rollback()
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE digests SET status = 'failed', error_message = %s "
                "WHERE user_id = %s AND scheduled_for = %s "
                "  AND status NOT IN ('sent', 'skipped_empty')",
                (errore, u.id, u.next_digest_at))
        conn.commit()
    except Exception:  # noqa: BLE001
        log.warning("digest di %s: guasto non registrato a database", u.email)


def _items_del_digest(conn, user_id: str) -> list[dict]:
    """I match che superano la soglia e non sono MAI stati spediti.

    Non "quelli di questa passata": un worker interrotto e ripreso deve
    ritrovarli (i loro match esistono già, non si rivalutano), e un match
    rimasto fuori dal top-30 di ieri merita un'altra chance domani. Il
    registro di ciò che l'utente ha ricevuto è digest_items, ed è l'anti-join
    su quella tabella a decidere, non la finestra temporale.

    A parità di punteggio comanda la trasparenza: prima la candidatura
    diretta, poi l'ente pubblico; prima il datore diretto, poi l'agenzia, poi
    il non dichiarato. L'ordine sta nei rank delle tabelle, non nel codice.
    """
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(
            "SELECT m.id::text AS match_id, m.score, m.reason, "
            "       j.id::text AS id, j.source_job_id, j.title, "
            "       j.organization, j.cities, "
            "       j.url, j.source, j.link_kind, j.employer_kind, j.salary, "
            "       j.date_posted "
            "FROM matches m "
            "JOIN jobs j ON j.id = m.job_id "
            "LEFT JOIN digest_items di ON di.match_id = m.id "
            "LEFT JOIN link_kinds lk ON lk.kind = j.link_kind "
            "LEFT JOIN employer_kinds ek ON ek.kind = j.employer_kind "
            "WHERE m.user_id = %s AND m.passed AND di.match_id IS NULL "
            "  AND j.status = 'active' "
            "ORDER BY m.score DESC, lk.rank NULLS LAST, ek.rank NULLS LAST, "
            "         j.date_posted DESC, m.job_id "
            "LIMIT %s", (user_id, MAX_ITEMS))
        return cur.fetchall()


def digest_utente(conn: psycopg.Connection, u: Utente, *, dry_run: bool = False,
                  threshold: int = THRESHOLD, evaluatore=None) -> dict:
    """Un utente, un digest. Ritorna il riepilogo per il log del giro."""
    esito = {"utente": u.email, "slot": u.next_digest_at, "valutate": 0,
             "inviate": 0, "stato": "?"}
    started = datetime.now(timezone.utc)
    profilo_testo = profilo_come_testo(u.profilo)

    digest_id = _digest_row(conn, u, started)
    if not digest_id:
        esito["stato"] = "già consegnato"
        return esito

    # Senza CV non c'è profilo, e GLM giudicherebbe contro «Ruolo cercato: —»
    # spendendo budget per punteggi privi di senso. Non è un errore
    # dell'utente ma uno stato incompleto: il digest lo dice e si riprova al
    # prossimo slot, quando magari il CV ci sarà.
    if not u.cv_id:
        _chiudi(conn, digest_id, status="failed", valutate=0, inviate=0,
                error="nessun CV attivo: profilo non valutabile")
        _rischedula(conn, u, started, consegnato=False)
        esito["stato"] = "failed_senza_cv"
        return esito

    lingua_glm = LINGUA_PER_GLM.get(u.locale, "English")
    candidati = funnel.candidati(conn, u.id)
    budget_finito = False
    if candidati:
        # Il budget del piano: si valuta ciò che resta, preferendo le più
        # recenti. La lettura serve a dimensionare e ad avvisare; il CONSUMO
        # vero è per offerta, atomico: un errore a metà digest non butta via
        # la dotazione di valutazioni non ancora fatte.
        with conn.cursor() as cur:
            cur.execute("SELECT q.monthly_evaluations, "
                        "COALESCE(b.evaluations_used, 0) "
                        "FROM plan_quotas q "
                        "LEFT JOIN user_evaluation_budget b ON b.user_id = %s "
                        "  AND b.period_month = date_trunc('month', current_date)::date "
                        "WHERE q.plan = %s", (u.id, u.plan))
            cap, usate = cur.fetchone()
        rimaste = cap - usate
        if rimaste <= 0:
            budget_finito = True
            log.warning("%s: budget di valutazione esaurito (%s/%s) — non valuto, "
                        "ma consegno ciò che resta da spedire", u.email, usate, cap)
            candidati = []
        elif len(candidati) > rimaste:
            log.warning("%s: %d candidati ma %d valutazioni rimaste — taglio le più vecchie",
                        u.email, len(candidati), rimaste)
            candidati = candidati[:rimaste]

    try:
        # Prima passata: punteggio (e micro-motivazione) su tutte.
        if candidati and evaluatore is None:
            evaluatore = ValutatoreGLM()
        valutate_adesso: set[str] = set()
        for i, offerta in enumerate(candidati):
            with conn.cursor() as cur:
                cur.execute("SELECT user_try_evaluate(%s, 1)", (u.id,))
                if not cur.fetchone()[0]:
                    log.warning("%s: budget finito a metà digest — fermo le valutazioni",
                                u.email)
                    break
            conn.commit()
            score, reason, uso = evaluatore.valuta(profilo_testo, offerta,
                                                   lingua_glm)
            with conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO matches (user_id, job_id, cv_id, score, reason, "
                    "  threshold_used, model, input_tokens, output_tokens) "
                    "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s) ON CONFLICT (user_id, job_id) DO NOTHING",
                    (u.id, offerta["id"], u.cv_id, score, reason, threshold,
                     MODELLO, uso.get("input"), uso.get("output")))
            esito["valutate"] += 1
            valutate_adesso.add(offerta["id"])
            if i % 50 == 49:
                conn.commit()
        conn.commit()

        items = _items_del_digest(conn, u.id)

        if not items:
            # Niente da spedire. Se per di più il budget è a zero, il digest
            # fallisce col motivo esplicito: è l'esito che merita un allarme.
            if budget_finito and not esito["valutate"]:
                _chiudi(conn, digest_id, status="failed", valutate=0, inviate=0,
                        error=f"budget di valutazione esaurito ({usate}/{cap}, piano {u.plan})")
                esito["stato"] = "failed_budget"
            else:
                _chiudi(conn, digest_id, status="skipped_empty",
                        valutate=esito["valutate"], inviate=0)
                esito["stato"] = "skipped_empty"
            _rischedula(conn, u, started, consegnato=False)
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
        for item in items:
            item["reason"], analisi, _ = evaluatore.motiva(profilo_testo, item,
                                                           lingua_glm)
            with conn.cursor() as cur:
                cur.execute(
                    "UPDATE matches SET reason = %s, analysis = %s "
                    "WHERE id = %s",
                    (item["reason"], Json(analisi), item["match_id"]))
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
                    if canale == "email":
                        mid = email_mod.invia(destinatario, items, u.locale)
                        # L'id registrato e' quello dell'email quando c'e':
                        # e' il canale con la tracciabilita' migliore.
                        message_id = mid
                    elif canale == "telegram" and u.telegram_chat_id:
                        mid = telegram_mod.invia(u.telegram_chat_id, items,
                                                 u.locale)
                        message_id = message_id or mid
                    elif canale == "whatsapp" and u.whatsapp_e164:
                        mid, conv = whatsapp_mod.invia(
                            u.whatsapp_e164, items[:3], u.locale)
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
                except telegram_mod.BotBloccato as exc:
                    # 403 per sempre: non e' un guasto da ritentare, e' un
                    # canale che non esiste piu'. Si stacca subito.
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
                    # Finestra temporanea (Meta rivede in ore): il canale
                    # NON si stacca, salta solo questo giro.
                    log.warning("%s: template WhatsApp non pronto (%s)",
                                u.email, exc)
                    problemi.append(f"whatsapp: template non pronto")
                except Exception as exc:
                    # Guasto passeggero: si conta, e al secondo di fila il
                    # canale si stacca invece di accumulare fallimenti che
                    # nessuno guarda. L'email non si stacca mai: e' la rete
                    # di sicurezza, e sotto ha i suoi retry SMTP.
                    log.warning("%s: consegna %s fallita: %s", u.email,
                                canale, exc)
                    problemi.append(f"{canale}: {exc}")
                    if canale != "email" and _canale_fallito(conn, u) >= 2:
                        _stacca_canale(conn, u, canale,
                                       "consegna fallita due volte")

            if riusciti:
                _canale_ok(conn, u)
            else:
                # NESSUN canale ha consegnato. L'email di riserva parte
                # anche se non era fra i canali scelti: un digest pagato e
                # mai consegnato e' il danno peggiore, e la casella c'e'
                # sempre. Se fallisce anche lei, l'eccezione risale e il
                # digest resta pending per il ritento orario.
                if "email" not in u.delivery_channels:
                    message_id = email_mod.invia(destinatario, items, u.locale)
                    riusciti.append("email(riserva)")
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
                inviate=len(items), message_id=message_id)
        _rischedula(conn, u, started, consegnato=True)
        esito["stato"] = "sent"
        esito["inviate"] = len(items)
        return esito
    finally:
        if isinstance(evaluatore, ValutatoreGLM):
            with conn.cursor() as cur:
                t = evaluatore.totale
                cur.execute(
                    "INSERT INTO api_usage (provider, operation, user_id, requests, "
                    "  input_tokens, output_tokens, cost_micros) "
                    "VALUES ('glm','score',%s,%s,%s,%s,%s)",
                    (u.id, t["chiamate"], t["input"], t["output"],
                     _costo_micros(cur, MODELLO, t)))
            conn.commit()


def _rischedula(conn, u: Utente, adesso: datetime, *, consegnato: bool) -> None:
    """Sposta l'appuntamento. Il PRIMO digest pero' non si perde per un vuoto.

    NASCE DA UN CONTO CHE NON TORNAVA. Il sito promette il primo digest
    entro 24 ore, ma la frequenza predefinita e' SETTIMANALE — e un giro
    vuoto avanzava lo stesso allo slot regolare. Chi si iscriveva su un
    mercato appena aperto riceveva quindi: un digest vuoto entro l'ora,
    l'appuntamento spostato al lunedi' dopo, e il primo digest vero fino a
    SETTE GIORNI piu' tardi. Chi sceglieva mensile, fino a un mese. Nel
    frattempo `last_digest_at` veniva scritto lo stesso, quindi l'utente
    risultava servito.

    Finche' non e' arrivato NIENTE davvero, il primo digest riprova il
    giorno dopo invece di cadere nella cadenza scelta: e' il momento in cui
    una persona ha appena consegnato il proprio CV e sta decidendo se
    fidarsi. Dal primo invio riuscito in poi comanda la frequenza, sempre.
    """
    mai_ricevuto = u.last_digest_at is None
    if not consegnato and mai_ricevuto:
        prossimo = adesso + timedelta(days=1)
    else:
        prossimo = prossimo_slot(u, adesso)
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE users SET last_digest_at = %s, next_digest_at = %s "
            "WHERE id = %s",
            # `last_digest_at` resta NULL finche' non e' partito niente: e'
            # cio' che rende riconoscibile «non ha mai ricevuto nulla» al
            # giro successivo, ed e' anche piu' onesto verso il pannello.
            (u.next_digest_at if consegnato else u.last_digest_at,
             prossimo, u.id))
    conn.commit()


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="nivult.matching.worker", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--user", help="elabora solo questo utente (uuid)")
    ap.add_argument("--dry-run", action="store_true",
                    help="valuta e compila l'email, ma non la invia")
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
            try:
                e = digest_utente(conn, u, dry_run=args.dry_run,
                                  threshold=args.threshold)
            except Exception as exc:  # noqa: BLE001
                log.error("digest di %s fallito: %s", u.email, exc)
                _segna_fallito(conn, u, str(exc)[:500])
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
