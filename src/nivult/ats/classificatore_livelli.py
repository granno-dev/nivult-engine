"""Il classificatore a tre livelli — completo, in cascata.

    python -m nivult.ats.classificatore_livelli --limite 50000
    python -m nivult.ats.classificatore_livelli --stats

LIVELLO 1 — DIZIONARIO: le parole chiave nel titolo ("nurse",
"entwickler", "commis de cuisine") matchano direttamente.

LIVELLO 2 — TITOLI NOTI: fuzzy matching contro i titoli già
classificati nel database. Se "Senior ICU Nurse" non matcha il
dizionario ma "ICU Nurse" è già Healthcare nel database, lo è
anche il Senior.

LIVELLO 3 — GLM SOLO PER I RESIDUI: i titoli che nessuno dei due
livelli matcha vanno a GLM (una chiamata per offerta, solo per
il 5-10% del totale).
"""

from __future__ import annotations

import argparse
import logging
import os
import re
import unicodedata
from difflib import SequenceMatcher

import psycopg

from nivult.ats.classificatore_veloce import (
    FAMIGLIE, classifica_titolo, classifica_da_raw, _pulisci_titolo)

log = logging.getLogger("nivult.ats.classificatore_livelli")

ATS_DSN = os.environ.get(
    "ATS_DATABASE_URL",
    "postgresql://giusepperanno@127.0.0.1:5432/nivult_ats")

MODELLO = "glm-5.2"

PROMPT_GRUPPO = """Assegna UNA famiglia professionale a ciascuno di questi titoli di offerte di lavoro.

Famiglie ammesse (usa ESATTAMENTE una di queste, in inglese):
{famiglie}

Titoli:
{elenco}

Rispondi SOLO con una riga per titolo, nel formato numero=Famiglia
(nient'altro: niente JSON, niente spiegazioni, nessuna riga in piu').
Esempio:
1=Logistics
2=Healthcare"""

# Due misure guidano questi numeri, prese sull'API vera:
#  - l'elenco delle famiglie pesa ~200 token e va ripetuto a ogni
#    chiamata: in gruppi da 40 il preambolo si divide per 40;
#  - l'output costa 3,7 volte l'input, quindi il formato della risposta
#    conta piu' della domanda: "1=Logistics" invece di un oggetto JSON
#    con la sicurezza fa scendere l'output da 25 a 5 token per titolo.
# Insieme: da $0.063 a $0.019 ogni mille titoli, a parita' di qualita'
# (30/40 d'accordo col dizionario in entrambi i formati).
PER_GRUPPO = 40

# La sicurezza non la chiediamo piu' (costava token per un valore che il
# modello inventava comunque): il livello 3 vale quanto vale, 0.7 fisso.
SICUREZZA_GLM = 0.7


# ── LIVELLO 2: TITOLI NOTI (fuzzy matching) ───────────────────────

def _token(titolo: str) -> set[str]:
    """Il titolo come insieme di parole significative."""
    t = _pulisci_titolo(titolo)
    return {w for w in re.split(r"[\s/,-]+", t) if len(w) > 2}


def _somiglianza(a: set[str], b: set[str]) -> float:
    """Quanto due insiemi di parole si sovrappongono (Jaccard)."""
    if not a or not b:
        return 0.0
    intersezione = a & b
    unione = a | b
    return len(intersezione) / len(unione)


def costruisci_indice_titoli(dsn: str) -> dict[str, str]:
    """L'indice dei titoli già classificati: parole → famiglia.

    Costruito dai titoli nel database che hanno già una
    classificazione (dal dizionario o da GLM): il loro insieme di
    parole diventa la firma di quella famiglia.
    """
    firme: dict[str, str] = {}  # "parola1|parola2|..." → famiglia
    with psycopg.connect(dsn) as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT lower(j.title), c.family
                  FROM ats_jobs j
                  JOIN job_classifications c ON c.job_id = j.id
                 WHERE j.expired_at IS NULL
                 LIMIT 50000
            """)
            for titolo, famiglia in cur.fetchall():
                parole = _token(titolo)
                if parole:
                    chiave = "|".join(sorted(parole))
                    firme[chiave] = famiglia
    return firme


def _match_titoli_noti(titolo: str, firme: dict[str, str]) -> tuple[str | None, float]:
    """(famiglia, confidenza) dal matching contro i titoli noti."""
    token = _token(titolo)
    if not token:
        return None, 0.0

    migliore_famiglia = None
    migliore_score = 0.0

    for firma, famiglia in firme.items():
        parole_firma = set(firma.split("|"))
        score = _somiglianza(token, parole_firma)
        if score > migliore_score:
            migliore_score = score
            migliore_famiglia = famiglia

    # soglia ALTISSIMA: il fuzzy matching a soglia bassa (0.45) classificava
    # 'ordinatore pacchi' come Energy e 'consegnatore giornali' come Food.
    # Un'offerta nella famiglia SBAGLIATA entra nel cluster sbagliato e
    # arriva nel digest di chi non c'entra — peggio di non classificarla.
    # A 0.85 passa solo il quasi-identico: 'Senior ICU Nurse' quando
    # 'ICU Nurse' è già noto. Il resto va a GLM.
    if migliore_score >= 0.85:
        return migliore_famiglia, round(migliore_score, 2)
    return None, 0.0


# ── LIVELLO 3: GLM (solo per i residui) ──────────────────────────

def _classifica_gruppo(titoli: list[str],
                       modello) -> tuple[dict[int, tuple[str, float]], bool]:
    """Una chiamata per un gruppo di titoli. Ritorna {indice: (famiglia,
    sicurezza)} e False se la CHIAMATA e' fallita (429/credito/rete).
    Le risposte fuori vocabolario si scartano: meglio nessuna famiglia
    che una inventata."""
    elenco = "\n".join(f"{i + 1}. {t[:110]}" for i, t in enumerate(titoli))
    prompt = PROMPT_GRUPPO.format(famiglie=", ".join(FAMIGLIE),
                                  elenco=elenco)
    try:
        grezzo = modello.chat([{"role": "user", "content": prompt}],
                              max_tokens=20 * len(titoli) + 100)
    except Exception:                                # noqa: BLE001
        return {}, False
    esiti: dict[int, tuple[str, float]] = {}
    for m in re.finditer(r"^\s*(\d+)\s*=\s*(.+?)\s*$", grezzo, re.M):
        i = int(m.group(1)) - 1
        if not 0 <= i < len(titoli):
            continue
        famiglia = m.group(2).strip()
        if famiglia not in FAMIGLIE:
            famiglia = next(
                (f for f in FAMIGLIE if f.lower() == famiglia.lower()
                 or f.lower() in famiglia.lower()), "")
            if not famiglia:
                continue         # fuori vocabolario: meglio niente
        esiti[i] = (famiglia, SICUREZZA_GLM)
    return esiti, True           # la chiamata c'e' stata (niente allarme)


# ── IL CLASSIFICATORE COMPLETO A TRE LIVELLI ──────────────────────

def classifica(dsn: str, limite: int = 50000, usa_glm: bool = True,
               glm_max: int = 400) -> dict:
    """`glm_max` e' il tetto di CHIAMATE a pagamento per corsa: un giro
    non puo' costare piu' di quanto deciso, qualunque cosa succeda."""
    stats = {"viste": 0, "livello1": 0, "livello2": 0, "livello3": 0,
             "classificate": 0, "non_classificate": 0,
             "chiamate_glm": 0, "titoli_ripetuti": 0}

    # costruisci l'indice dei titoli noti (livello 2)
    log.info("costruisco l'indice dei titoli noti...")
    firme = costruisci_indice_titoli(dsn)
    log.info("indice: %d firme", len(firme))

    # prendi le offerte non classificate
    with psycopg.connect(dsn) as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT j.id, j.title, j.platform_id, j.raw, j.slug
                  FROM ats_jobs j
             LEFT JOIN job_classifications c ON c.job_id = j.id
                 WHERE c.job_id IS NULL AND j.expired_at IS NULL
                 LIMIT %s
            """, (limite,))
            offerte = cur.fetchall()

    # livello 3: inizializza GLM solo se serve
    modello = None
    if usa_glm:
        try:
            from nivult.ats.classificatore import GLMLight
            modello = GLMLight()
            log.info("GLM disponibile per il livello 3")
        except SystemExit:
            log.info("GLM non disponibile (manca la chiave) — livello 3 disattivato")

    def _salva(righe: list[tuple]) -> None:
        """Scrive un blocco e basta: chiamata ogni 5000 cosi' il lavoro
        e' al sicuro nel database man mano, non tutto in fondo. Prima si
        accumulava tutto in memoria e si scriveva solo alla fine: se il
        processo moriva a meta' (o veniva riavviato), le decine di
        migliaia di classificazioni gia' calcolate erano perse."""
        if not righe:
            return
        with psycopg.connect(dsn) as conn:
            with conn.cursor() as cur:
                cur.executemany("""
                    INSERT INTO job_classifications
                      (job_id, family, confidence, model)
                    VALUES (%s, %s, %s, %s)
                    ON CONFLICT (job_id) DO NOTHING
                """, righe)
            conn.commit()

    # ── PASSATA 1: i due livelli gratuiti su tutto il lotto ──
    da_scrivere: list[tuple] = []
    residuo: list[tuple] = []        # (jid, titolo) per il livello 3
    for jid, titolo, pid, raw, slug in offerte:
        stats["viste"] += 1

        famiglia, conf = classifica_da_raw(raw, pid)      # LIVELLO 1
        livello = 1 if famiglia else None
        if not famiglia:
            famiglia, conf = classifica_titolo(titolo)
            if famiglia:
                livello = 1
        if not famiglia:                                  # LIVELLO 2
            famiglia, conf = _match_titoli_noti(titolo, firme)
            if famiglia:
                livello = 2

        if famiglia:
            da_scrivere.append((jid, famiglia, conf, f"livello{livello}"))
            stats["classificate"] += 1
            stats[f"livello{livello}"] += 1
        elif modello and (titolo or "").strip():
            residuo.append((jid, titolo))
        else:
            stats["non_classificate"] += 1

        if stats["viste"] % 5000 == 0:
            log.info("  … %d viste: %s", stats["viste"], stats)
            _salva(da_scrivere)          # al sicuro nel DB, poi si svuota
            da_scrivere = []
    _salva(da_scrivere)
    da_scrivere = []

    # ── PASSATA 2: il residuo a GLM, in gruppi e una volta per titolo ──
    # Lo stesso titolo puo' comparire in cento offerte: si paga una volta
    # sola e la risposta si riusa. Il tetto glm_max chiude il rubinetto.
    if residuo and modello:
        log.info("livello 3: %d offerte residue da mandare a GLM",
                 len(residuo))
        noti: dict[str, tuple[str, float]] = {}
        da_chiedere: list[str] = []
        ko_di_fila = 0

        def _svuota() -> bool:
            """Manda il gruppo accumulato. False = fermarsi (GLM giu')."""
            nonlocal ko_di_fila, da_chiedere
            if not da_chiedere:
                return True
            esiti, ok = _classifica_gruppo(da_chiedere, modello)
            stats["chiamate_glm"] += 1
            if ok:
                ko_di_fila = 0
                for i, (fam, sic) in esiti.items():
                    noti[da_chiedere[i].lower()] = (fam, sic)
            else:
                ko_di_fila += 1
                if ko_di_fila >= 3:
                    log.warning("GLM giu' (429/credito?): livello 3 "
                                "interrotto per questa corsa")
                    da_chiedere = []
                    return False
            da_chiedere = []
            return True

        visti: set[str] = set()
        for _, titolo in residuo:
            chiave = titolo.lower()
            if chiave in visti:
                continue
            visti.add(chiave)
            da_chiedere.append(titolo)
            if len(da_chiedere) >= PER_GRUPPO:
                if stats["chiamate_glm"] >= glm_max:
                    log.info("tetto di %d chiamate raggiunto: il resto "
                             "aspetta la prossima corsa", glm_max)
                    da_chiedere = []
                    break
                if not _svuota():
                    break
        else:
            if stats["chiamate_glm"] < glm_max:
                _svuota()

        for jid, titolo in residuo:
            esito = noti.get(titolo.lower())
            if esito:
                da_scrivere.append((jid, esito[0], esito[1], "livello3"))
                stats["classificate"] += 1
                stats["livello3"] += 1
            else:
                stats["non_classificate"] += 1
        stats["titoli_ripetuti"] = len(residuo) - len(visti)
        _salva(da_scrivere)

    return stats


def stats(dsn: str) -> None:
    with psycopg.connect(dsn) as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT count(*) FROM ats_jobs WHERE expired_at IS NULL")
            vive = cur.fetchone()[0]
            cur.execute("SELECT count(*) FROM job_classifications")
            tot = cur.fetchone()[0]
            cur.execute("""
                SELECT model, count(*) FROM job_classifications
                GROUP BY 1 ORDER BY 2 DESC
            """)
            per_modello = cur.fetchall()
            cur.execute("""
                SELECT family, count(*) FROM job_classifications
                GROUP BY 1 ORDER BY 2 DESC LIMIT 15
            """)
            top = cur.fetchall()
    print(f"\nofferte vive: {vive}")
    print(f"classificate: {tot} ({tot / max(vive, 1) * 100:.0f}%)")
    print("per metodo:")
    for m, n in per_modello:
        print(f"  {m:15s} {n}")
    print("top famiglie:")
    for f, n in top:
        print(f"  {f:35s} {n}")


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO,
                        format="%(levelname)-8s %(message)s")
    ap = argparse.ArgumentParser(prog="nivult.ats.classificatore_livelli",
                                 description=__doc__)
    ap.add_argument("--limite", type=int, default=50000)
    ap.add_argument("--no-glm", action="store_true",
                    help="solo livelli 1-2, senza GLM")
    ap.add_argument("--glm-max", type=int, default=400,
                    help="tetto di chiamate a pagamento per corsa "
                         "(ognuna copre %d titoli)" % PER_GRUPPO)
    ap.add_argument("--stats", action="store_true")
    args = ap.parse_args(argv)

    if args.stats:
        stats(ATS_DSN)
    else:
        s = classifica(ATS_DSN, args.limite, usa_glm=not args.no_glm,
                       glm_max=args.glm_max)
        print(f"\nClassificatore a livelli: {s}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
