"""nivult-mT5 in produzione: la sintesi di ogni offerta, sul N5.

Gemello di `estrai_2b.py` — stessa coda, stessa prenotazione, stesse regole — ma
scrive SOLO la sintesi, e insieme alla sintesi salva la FIDUCIA del modello:
la media del logaritmo della probabilita' delle parole che ha scelto.

La fiducia serve al ripasso. Misurata il 18/09/2026 su 250 sintesi giudicate una
per una, correla +0,472 col voto del giudice: le sintesi in cui mT5 esita sono
davvero quelle che sbaglia. Il 2B, sul Mac mini, riscrive quelle sotto soglia.

  ATS_DATABASE_URL=... python sintesi_mt5.py [--limite N] [--continuo]
                                             [--lotto 8] [--dry-run]
"""
from __future__ import annotations
import argparse, hashlib, html as _html, json, os, re, sys, time
import psycopg
import torch
from transformers import AutoTokenizer, AutoModelForSeq2SeqLM
from transformers import LogitsProcessor, LogitsProcessorList

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from sintesi_ancorata import ripulisci                             # noqa: E402

MODELLO = "nivult-mt5"
PERCORSO = os.environ.get("MT5", "/opt/nivult/mt5")
CAMPI_TESTO = ("description", "content", "descriptionHtml", "descriptionPlain", "externalDescription",
               "jobDescription", "job_description", "Job_Description", "body", "content_html",
               "description_html", "descriptionBody", "text", "ShortDescriptionStr")
_TAG = re.compile(r"<[^>]+>")

# IL FRENO. La Radeon del N5 non ha memoria propria: la prende dalla RAM di sistema
# (GTT, tetto 47 GB su 91). Quando si riempie la macchina non crea piu' processi, ssh
# accetta la connessione e non completa il banner, e i servizi muoiono a uno a uno.
# E' successo il 17 e il 18/09/2026. Qui si guarda PRIMA di ogni lotto.
TETTO_GRAFICA = float(os.environ.get("TETTO_GRAFICA", "0.55"))


def grafica_piena() -> float:
    try:
        u = int(open("/sys/class/drm/card0/device/mem_info_gtt_used").read())
        t = int(open("/sys/class/drm/card0/device/mem_info_gtt_total").read())
        return u / t
    except Exception:                                              # noqa: BLE001
        return 0.0


def pulito(t: str | None) -> str:
    t = _html.unescape(_html.unescape(t or ""))
    return re.sub(r"\s+", " ", _TAG.sub(" ", t).replace("\xa0", " ")).strip()


def normalizza(t: str) -> str:
    """La stessa che ha visto in addestramento: ricetta.json dice normalizza=true."""
    return re.sub(r"\s+", " ", re.sub(r"\n+", " ", (t or "").strip()))


def impronta(testo: str) -> str:
    return hashlib.sha1(testo.encode("utf-8", "ignore")).hexdigest()


class Fiducia(LogitsProcessor):
    """Raccoglie il log-prob della parola scelta a ogni passo, senza output_scores.

    `output_scores=True` tiene in memoria un punteggio per OGNI parola possibile a
    OGNI passo: 250.112 parole x 256 passi x lotto. Con lotti da 4 il 18/09/2026 la
    memoria grafica e' arrivata al 99,6% — la condizione esatta che aveva gia' ucciso
    la macchina. Qui si sfruttano i punteggi che `generate` calcola comunque: in
    decodifica avida la parola scelta e' l'argmax, quindi il suo log-prob e' il
    massimo del log_softmax. Si conservano B numeri per passo invece di B x 250.112.

    Tutto resta sulla scheda fino alla fine: leggere anche un solo numero a ogni
    passo costringerebbe la CPU ad aspettare la GPU 256 volte per ogni riga.
    """

    def __init__(self, n: int, eos: int):
        self.eos = eos
        self.somma = self.conta = self.viva = None

    def __call__(self, ids, punteggi):
        v, k = torch.log_softmax(punteggi.float(), -1).max(-1)
        if self.somma is None:
            self.somma = torch.zeros_like(v)
            self.conta = torch.zeros_like(v)
            self.viva = torch.ones_like(v)
        self.somma += v * self.viva          # le righe gia' finite non contano piu'
        self.conta += self.viva
        self.viva = self.viva * (k != self.eos)   # l'ultima parola conta, poi si tace
        return punteggi

    def medie(self) -> list[float | None]:
        if self.somma is None:
            return []
        m = (self.somma / self.conta.clamp(min=1)).tolist()
        return [x if c > 0 else None for x, c in zip(m, self.conta.tolist())]


SQL = """
UPDATE ats_jobs j SET preso_mt5_at = now()
 WHERE j.id IN (
   SELECT k.id FROM ats_jobs k
    WHERE k.expired_at IS NULL
      AND k.sintesi_mt5_at IS NULL
      AND (k.preso_mt5_at IS NULL OR k.preso_mt5_at < now() - interval '{scadenza} minutes')
      -- LA GARA COL DETTAGLIO (21/09/2026). Meta' delle piattaforme mette il testo
      -- solo nella pagina di dettaglio, che un altro passo va a prendere dopo la
      -- lista. Questo demone prende le offerte piu' nuove per prime, quindi le
      -- leggeva PRIMA del dettaglio, le marcava «senza testo» per sempre e non ci
      -- tornava: 48.787 righe cosi', 15.721 delle quali attive e col testo arrivato
      -- dopo. Regola della testa tecnologie: senza testo si aspetta fino a
      -- {giorni} giorni, poi si prende atto che il testo non arrivera'.
      AND (EXISTS (SELECT 1 FROM unnest(ARRAY[{campi_k}]) v WHERE length(v) >= 300)
           OR k.created_at < now() - interval '{giorni} days')
      -- Le offerte che il 2B aveva gia' riassunto in modalita' piena (116.592 al
      -- 19/09/2026) non si rifanno: la sua sintesi vale 4,71 contro 4,44, e la vista
      -- `sintesi_finali` dara' comunque la precedenza alla sua. Sarebbe lavoro buttato.
      AND NOT EXISTS (SELECT 1 FROM estrazioni_v2b e
                       WHERE e.job_id = k.id AND e.sintesi IS NOT NULL)
    ORDER BY k.posted_at DESC NULLS LAST
    LIMIT %s
    FOR UPDATE SKIP LOCKED)
RETURNING j.id, j.title, coalesce(j.location, j.city, ''), j.country, j.lang,
       coalesce((SELECT v FROM unnest(ARRAY[{campi}]) v WHERE length(v) >= 300 LIMIT 1), '')
""".format(campi=", ".join(f"j.raw->>'{c}'" for c in CAMPI_TESTO),
           campi_k=", ".join(f"k.raw->>'{c}'" for c in CAMPI_TESTO),
           scadenza=int(os.environ.get("SCADENZA_PRESA", "20")),
           giorni=int(os.environ.get("GIORNI_ATTESA_TESTO", "7")))

SQL_INSERISCI = ("INSERT INTO sintesi_mt5 "
                 "(job_id, sintesi, fiducia, modello, testo_hash, sintesi_pulita, frasi_tolte, pulita_at) "
                 "VALUES (%s,%s,%s,%s,%s,%s,%s,now()) ON CONFLICT (job_id) DO NOTHING")


def valida(s: str | None) -> str | None:
    """Stessa regola del 2B: una sintesi fuori misura non e' una sintesi."""
    s = (s or "").strip()
    n = len(s.split())
    return s if s and 20 <= n <= 200 else None


FASCIA = os.environ.get("FASCIA_SILENZIOSA",
                       "/opt/nivult/engine/logs/.fascia-silenziosa")


def fascia_silenziosa() -> bool:
    """Siamo nella fascia silenziosa? Fra le 23:30 e le 7:00 di Roma, e solo se
    l'interruttore c'e'.

    Il N5 sta in casa e la ventola si sente: l'iGPU al 99% tiene il package
    caldo e la ventola alta tutta la notte (misurato il 20/09/2026 all'01:04,
    con carico di sistema 1,89 e scheda al 99%). Le sintesi alimentano il digest
    B2C, che non ha ancora utenti, quindi fermarsi qui non costa niente.
    """
    if not os.path.exists(FASCIA):
        return False
    from datetime import datetime
    from zoneinfo import ZoneInfo
    o = datetime.now(ZoneInfo("Europe/Rome"))
    m = o.hour * 60 + o.minute
    return m >= 23 * 60 + 30 or m < 7 * 60


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--limite", type=int, default=2000)
    ap.add_argument("--lotto", type=int, default=int(os.environ.get("LOTTO_MT5", "8")))
    ap.add_argument("--max-nuovi", type=int, default=256)
    ap.add_argument("--continuo", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()

    ric = json.load(open(f"{PERCORSO}/ricetta.json"))
    max_in = int(ric.get("max_in", 1024))
    tok = AutoTokenizer.from_pretrained(PERCORSO)
    mod = AutoModelForSeq2SeqLM.from_pretrained(PERCORSO, dtype=torch.bfloat16).cuda().eval()
    mod.generation_config.no_repeat_ngram_size = 0     # come in esame: i punteggi devono essere grezzi
    # LA CACHE VA ACCESA, e non e' una questione di velocita'. Il checkpoint si porta
    # dietro da XLSum `use_cache: false`: senza cache il decoder ricalcola tutte le
    # parole precedenti a ogni passo e tiene in memoria le attivazioni di tutte. Il
    # 19/09/2026 una prova a lotti da 8 ha portato la memoria grafica del N5 da 9 GB
    # a 47.050 MB su 47.083 — la scheda piena, cioe' la condizione che il 17 e il 18
    # avevano gia' ucciso la macchina. Con la cache accesa lo stesso lotto sta in
    # pochi GB.
    mod.generation_config.use_cache = True
    mod.config.use_cache = True
    print(f"mt5: modello caricato da {PERCORSO} | max_in {max_in} | lotto {a.lotto}", flush=True)

    st = {"viste": 0, "modello": 0, "gemelle": 0, "scritte": 0, "vuote": 0, "senza_testo": 0}
    t0 = time.time()
    with psycopg.connect(os.environ["ATS_DATABASE_URL"], autocommit=True) as c:
        detta = False
        while True:
            if fascia_silenziosa():
                if not detta:
                    print("fascia silenziosa 23:30-07:00: mi fermo, la ventola del N5 "
                          "sta in casa di Giuseppe", flush=True)
                    detta = True
                time.sleep(300)
                continue
            detta = False
            q = grafica_piena()
            if q > TETTO_GRAFICA:
                print(f"FRENO: memoria grafica al {100*q:.0f}%, aspetto per non uccidere il N5", flush=True)
                time.sleep(60)
                continue
            righe = c.execute(SQL, (a.limite,)).fetchall()
            if not righe:
                print("mt5: niente da fare, aspetto", flush=True)
                if not a.continuo:
                    break
                time.sleep(120)
                continue

            lavoro, tutti, esiti = [], [], []
            for jid, titolo, sede, paese, lingua, grezzo in righe:
                st["viste"] += 1
                tutti.append(jid)
                testo = pulito(grezzo)
                if len(testo) < 300:
                    st["senza_testo"] += 1
                    # niente impronta: una riga d'esito non deve finire fra le gemelle
                    esiti.append((jid, None, None, MODELLO + "+saltato:senza-testo", None, None, None))
                    continue
                lavoro.append((jid, titolo, sede, paese, lingua, testo, impronta(testo)))

            # 1. le gemelle: stesso testo gia' riassunto, si copia senza spendere calcolo
            scritte = []
            if lavoro:
                impronte = list({x[6] for x in lavoro})
                copie = {h: (s, f) for h, s, f in c.execute(
                    "SELECT DISTINCT ON (testo_hash) testo_hash, sintesi, fiducia FROM sintesi_mt5 "
                    "WHERE testo_hash = ANY(%s) AND sintesi IS NOT NULL ORDER BY testo_hash, creato_at",
                    (impronte,)).fetchall()}
                da_fare = [x for x in lavoro if x[6] not in copie]
                for g in (x for x in lavoro if x[6] in copie):
                    s, f = copie[g[6]]
                    # anche la copia passa dal filtro: la gemella l'aveva scritta un
                    # giro precedente, magari prima che il filtro esistesse
                    p, tolte, _ = ripulisci(s, f"{g[1] or ''} {g[5]}")
                    scritte.append((g[0], s, f, MODELLO + "+gemella", g[6], p, tolte))
                    st["gemelle"] += 1
            else:
                da_fare = []

            # 2. il modello, a lotti piccoli: l'attenzione cresce col quadrato dei token
            for i in range(0, len(da_fare), a.lotto):
                gruppo = da_fare[i:i + a.lotto]
                if grafica_piena() > TETTO_GRAFICA:
                    # Tutto quello che resta, non solo questo gruppo: una riga marcata
                    # e mai lavorata non torna piu' in coda, e la perderemmo.
                    resto = [x[0] for x in da_fare[i:]]
                    print(f"FRENO a meta' infornata: lascio {len(resto)} offerte alla prossima", flush=True)
                    for jid in resto:
                        tutti.remove(jid)           # non marcarle: torneranno in coda
                    break
                # Il testo entra INTERO: il taglio a 6.000 caratteri che stava qui era
                # nato per il modello a 1024 token, e col modello a 2048 (ricetta.json)
                # lascerebbe fuori cio' che il modello ora puo' leggere. Quel che
                # supera max_in lo tronca ancora il tokenizer: e' il residuo da
                # curare con la lettura a pezzi, non con un taglio a monte.
                pr = [normalizza(f"Titolo: {x[1] or ''}\nSede: {x[2] or ''} ({x[3] or '-'})\n"
                                 f"Lingua dell'annuncio: {x[4] or '?'}\n\n{x[5]}") for x in gruppo]
                enc = tok(pr, return_tensors="pt", padding=True, truncation=True, max_length=max_in).to("cuda")
                fid = Fiducia(len(gruppo), tok.eos_token_id)
                # Il freno guarda PRIMA del lotto, ma la memoria si riempie DENTRO il
                # lotto: se succede si molla il gruppo invece di insistere, e le righe
                # tornano in coda da sole. Meglio perdere otto offerte che la macchina.
                try:
                    with torch.no_grad():
                        out = mod.generate(**enc, max_new_tokens=a.max_nuovi, num_beams=1, do_sample=False,
                                           logits_processor=LogitsProcessorList([fid]))
                except (RuntimeError, torch.cuda.OutOfMemoryError) as e:   # noqa: BLE001
                    print(f"LOTTO FALLITO ({type(e).__name__}: {str(e)[:120]}), lo lascio alla prossima", flush=True)
                    for x in gruppo:
                        tutti.remove(x[0])
                    del enc
                    torch.cuda.empty_cache()
                    continue
                testi = tok.batch_decode(out, skip_special_tokens=True)
                for x, t, f in zip(gruppo, testi, fid.medie()):
                    st["modello"] += 1
                    s = valida(t)
                    if not s:
                        st["vuote"] += 1
                        esiti.append((x[0], None, None, MODELLO + "+saltato:illeggibile", None, None, None))
                        continue
                    # IL FILTRO SULLE CIFRE. mT5 inventa salari: nel 13,2% delle sue
                    # sintesi c'e' una cifra di paga che in tutto il raw non esiste —
                    # a volte un numero vero con le cifre scambiate ($116.975 che
                    # diventa $116.775), a volte un range tondo tirato fuori dal nulla.
                    # Il 2B, sullo stesso metro, sta allo 0,3%. La frase che porta una
                    # cifra non ancorata si butta; l'originale resta in `sintesi`, chi
                    # legge prende `sintesi_pulita`. Misurato il 20/09/2026.
                    p, tolte, _ = ripulisci(s, f"{x[1] or ''} {x[5]}")
                    if tolte:
                        st["ripulite"] = st.get("ripulite", 0) + 1
                    scritte.append((x[0], s, f, MODELLO, x[6], p, tolte))
                del out, enc
                torch.cuda.empty_cache()

            if not a.dry_run:
                if scritte or esiti:
                    with c.cursor() as cur:
                        if scritte:
                            cur.executemany(SQL_INSERISCI, scritte)
                        # L'esito di chi e' stato saltato: senza questa riga l'offerta
                        # resta segnata come fatta e non torna mai piu' (17/09/2026).
                        if esiti:
                            cur.executemany(SQL_INSERISCI, esiti)
                    st["scritte"] += len(scritte)
                # Marcate TUTTE le righe viste, comprese quelle senza testo e quelle
                # illeggibili: altrimenti tornerebbero a ogni giro.
                c.execute("UPDATE ats_jobs SET sintesi_mt5_at = now(), preso_mt5_at = NULL "
                          "WHERE id = ANY(%s)", (tutti,))

            dt = max(time.time() - t0, 1e-6)
            print(f"mt5: viste {st['viste']} | modello {st['modello']} | gemelle {st['gemelle']} "
                  f"| scritte {st['scritte']} | vuote {st['vuote']} | senza testo {st['senza_testo']} "
                  f"| grafica {100*grafica_piena():.0f}% "
                  f"| {st['scritte']/dt:.2f}/s = {int(86400*st['scritte']/dt):,}/giorno", flush=True)

            if st["modello"] >= 300 and st["vuote"] / max(st["modello"], 1) > 0.10:
                print("INTERRUTTORE: oltre il 10% di sintesi inutilizzabili, mi fermo", flush=True)
                return 3
            if not a.continuo and st["viste"] >= a.limite:
                break
            if a.dry_run and st["viste"] >= a.limite:
                break
    return 0


if __name__ == "__main__":
    sys.exit(main())
