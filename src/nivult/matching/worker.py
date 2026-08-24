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
from psycopg.rows import dict_row

from nivult.config import database_url, load_dotenv, safe_dsn
from nivult.delivery import email as email_mod
from nivult.matching import funnel
from nivult.matching.llm import (GLM, motiva_offerta, profilo_come_testo,
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
    delivery_channel: str
    delivery_email: str | None
    telegram_chat_id: str | None
    whatsapp_e164: str | None
    frequency: str
    send_hour_local: int
    send_weekday: int | None
    send_monthday: int | None
    timezone: str
    next_digest_at: datetime
    last_digest_at: datetime | None
    cv_id: str | None
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

    def valuta(self, profilo_testo: str, offerta: dict):
        """-> (punteggio, micro-motivazione, uso token della chiamata)."""
        score, reason, uso = valuta_offerta(self.model, profilo_testo, offerta)
        for k in ("input", "cached", "output"):
            self.totale[k] += uso.get(k, 0)
        self.totale["chiamate"] += 1
        return score, reason, uso

    def motiva(self, profilo_testo: str, offerta: dict):
        """-> (motivazione, uso token della chiamata)."""
        reason, uso = motiva_offerta(self.model, profilo_testo, offerta)
        for k in ("input", "cached", "output"):
            self.totale[k] += uso.get(k, 0)
        self.totale["chiamate"] += 1
        return reason, uso


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
        "SELECT u.id::text, u.email::text, u.plan, u.delivery_channel, "
        "       u.delivery_email::text, u.telegram_chat_id, u.whatsapp_e164, "
        "       u.frequency, u.send_hour_local, u.send_weekday, u.send_monthday, "
        "       u.timezone, u.next_digest_at, u.last_digest_at, "
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
        delivery_channel=r["delivery_channel"], delivery_email=r["delivery_email"],
        telegram_chat_id=r["telegram_chat_id"], whatsapp_e164=r["whatsapp_e164"],
        frequency=r["frequency"], send_hour_local=r["send_hour_local"],
        send_weekday=r["send_weekday"], send_monthday=r["send_monthday"],
        timezone=r["timezone"], next_digest_at=r["next_digest_at"],
        last_digest_at=r["last_digest_at"], cv_id=r["cv_id"],
        profilo=_profilo(r)) for r in cur.fetchall()]


def _orario(giorno: date, ora: int, tz: ZoneInfo) -> datetime:
    return datetime.combine(giorno, time(ora), tzinfo=tz)


def prossimo_slot(u: Utente, adesso: datetime) -> datetime:
    """Il prossimo orario di invio, nel fuso dell'utente.

    Sempre strettamente futuro: se il worker è stato fermo qualche giorno lo
    slot arretrato non si recupera mandando digest a raffica — le offerte
    intanto sono lì, e la prossima consegna le porta tutte.
    """
    tz = ZoneInfo(u.timezone)
    oggi = adesso.astimezone(tz).date()
    if u.frequency == "daily":
        cand = _orario(oggi, u.send_hour_local, tz)
        while cand <= adesso:
            cand = _orario(cand.date() + timedelta(days=1), u.send_hour_local, tz)
        return cand.astimezone(timezone.utc)
    if u.frequency == "weekly":
        # send_weekday è ISODOW: 1 lunedì … 7 domenica.
        avanti = (u.send_weekday - oggi.isoweekday()) % 7
        cand = _orario(oggi + timedelta(days=avanti), u.send_hour_local, tz)
        if cand <= adesso:
            cand = _orario(cand.date() + timedelta(days=7), u.send_hour_local, tz)
        return cand.astimezone(timezone.utc)
    # monthly: send_monthday è 1–28, quindi ogni mese lo contiene.
    cand = _orario(oggi.replace(day=u.send_monthday), u.send_hour_local, tz)
    while cand <= adesso:
        mese = cand.month + 1
        anno = cand.year + (1 if mese > 12 else 0)
        cand = _orario(date(anno, mese % 12 or 12, u.send_monthday),
                       u.send_hour_local, tz)
    return cand.astimezone(timezone.utc)


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
            (u.id, u.delivery_channel, u.next_digest_at, u.last_digest_at,
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
            score, reason, uso = evaluatore.valuta(profilo_testo, offerta)
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
            _rischedula(conn, u, started)
            return esito

        # Seconda passata: la motivazione vera solo per ciò che viene inviato.
        if evaluatore is None:
            evaluatore = ValutatoreGLM()
        for item in items:
            item["reason"], _ = evaluatore.motiva(profilo_testo, item)
            with conn.cursor() as cur:
                cur.execute("UPDATE matches SET reason = %s WHERE id = %s",
                            (item["reason"], item["match_id"]))
            conn.commit()

        with conn.cursor() as cur:
            for pos, item in enumerate(items, start=1):
                cur.execute(
                    "INSERT INTO digest_items (digest_id, job_id, user_id, match_id, "
                    "  rank, score_snapshot, reason_snapshot) VALUES (%s,%s,%s,%s,%s,%s,%s) "
                    "ON CONFLICT (digest_id, job_id) DO NOTHING",
                    (digest_id, item["id"], u.id, item["match_id"], pos,
                     item["score"], item["reason"]))
        conn.commit()

        if u.delivery_channel != "email":
            _chiudi(conn, digest_id, status="failed", valutate=esito["valutate"],
                    inviate=0, error=f"canale {u.delivery_channel} non ancora supportato")
            _rischedula(conn, u, started)
            esito["stato"] = "failed_canale"
            return esito

        destinatario = u.delivery_email or u.email
        if dry_run:
            percorsoHtml = email_mod.anteprima(destinatario, items)
            message_id = None
            log.info("%s: DRY RUN, email compilata in %s", u.email, percorsoHtml)
        else:
            message_id = email_mod.invia(destinatario, items)
        # jobs_evaluated_count: le valutazioni che ALIMENTANO questo digest —
        # quelle pagate in questo run più le recuperate da un tentativo
        # precedente. È ciò che soddisfa il vincolo sent <= evaluated: un
        # digest può consegnare offerte valutate prima di lui.
        alimentano = esito["valutate"] + sum(
            1 for it in items if it["id"] not in valutate_adesso)
        _chiudi(conn, digest_id, status="sent", valutate=alimentano,
                inviate=len(items), message_id=message_id)
        _rischedula(conn, u, started)
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


def _rischedula(conn, u: Utente, adesso: datetime) -> None:
    with conn.cursor() as cur:
        cur.execute("UPDATE users SET last_digest_at = %s, next_digest_at = %s "
                    "WHERE id = %s",
                    (u.next_digest_at, prossimo_slot(u, adesso), u.id))
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
