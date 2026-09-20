"""nivult-2b in produzione: tecnologie e sintesi per ogni offerta, via llama-server.

Ordine di lavoro: prima le offerte nuove d'Europa, poi l'arretrato europeo dalla piu' recente,
poi il resto del mondo. Prima di chiamare il modello cerca una GEMELLA (stesso testo gia' letto):
se c'e', copia il risultato senza spendere un token.

Dal 19/09/2026 il lavoro e' diviso fra i modelli (vedi `sintesi_mt5.py`):

  --solo-tecnologie  il 2B smette di scrivere le sintesi, e le famiglie che di
                     tecnologie non ne hanno (vedi FUORI) non le guarda nemmeno. Il 93% di quello che
                     scriveva era la sintesi: toglierla lo rende da tre a cinque
                     volte piu' veloce, e le sintesi le fa mT5 sul N5.
  --ripasso          con la capacita' che avanza il 2B RISCRIVE le sintesi dove
                     mT5 ha esitato. Non a caso: la fiducia di mT5 correla +0,472
                     col voto del giudice (misurato il 18/09 su 250 sintesi).

Le due opzioni stanno insieme, ed e' cosi' che gira in produzione: le tecnologie
hanno la precedenza, il ripasso prende solo il tempo che avanza.

  ATS_DATABASE_URL=... python estrai_2b.py [--limite N] [--continuo] [--fuori-europa]
                                           [--url http://127.0.0.1:8089] [--par 32]
                                           [--solo-tecnologie] [--ripasso] [--soglia -0.3166]
                                           [--grammatica FILE] [--dry-run]
"""
from __future__ import annotations
import argparse, hashlib, json, os, re, sys, time, urllib.request
import concurrent.futures as cf
import psycopg
from psycopg.types.json import Jsonb

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from sintesi_ancorata import ripulisci                             # noqa: E402

# Accanto a questo file, non nel percorso di Python: le tre copie del codice
# (Hetzner, N5, Mac mini) girano da cartelle diverse. Import duro: senza il
# filtro sulle invenzioni il demone non deve partire.
import importlib.util as _ilu
_sp = _ilu.spec_from_file_location("ancoraggio", str(__import__("pathlib").Path(__file__).parent / "ancoraggio.py"))
ancoraggio = _ilu.module_from_spec(_sp)
_sp.loader.exec_module(ancoraggio)

MODELLO = "nivult-2b"
_COMUNE = ("Sei l'estrattore di Nivult. Leggi l'annuncio di lavoro e rispondi SOLO con un JSON con le chiavi: ")
SISTEMA = (_COMUNE +
           "family, seniority, employment_type, remote, citta, regione, paese, lingue_obbligatorie, tecnologie, salario, sintesi. "
           "employment_type e remote solo se scritti nel testo, altrimenti null. tecnologie = lista di {nome, ruolo} con ruolo in usata|servizio|gradita. "
           "salario = {min, max, valuta, periodo} solo se scritto, altrimenti null. sintesi = 40-120 parole nella lingua dell'annuncio, solo fatti.")
# Senza la sintesi il modello scrive ~32 parole invece di ~172: e' tutto il guadagno.
SISTEMA_TEC = (_COMUNE +
               "tecnologie. tecnologie = lista di {nome, ruolo} con ruolo in usata|servizio|gradita. "
               "Solo le tecnologie scritte nell'annuncio, niente altro.")
# Il ripasso: qui la sintesi e' l'unica cosa che serve, ed e' il motivo del ripasso.
SISTEMA_SIN = (_COMUNE +
               "sintesi. sintesi = 40-120 parole nella lingua dell'annuncio, solo fatti scritti nell'annuncio.")
EUROPA = ('AT','BE','BG','HR','CY','CZ','DK','EE','FI','FR','DE','GR','HU','IE','IT','LV','LT','LU','MT','NL','PL','PT','RO','SK','SI','ES','SE','NO','IS','LI','CH','GB','UA','RS','BA','ME','MK','AL','XK','MD','AD','MC','SM','GI','JE','GG','IM','FO','GL')
CAMPI_TESTO = ("description","content","descriptionHtml","descriptionPlain","externalDescription","jobDescription",
               "job_description","Job_Description","body","content_html","description_html","descriptionBody","text","ShortDescriptionStr")
_TAG = re.compile(r"<[^>]+>")

# La soglia sotto la quale una sintesi di mT5 va riscritta. -0,3166 e' il ventesimo
# percentile misurato sulle 250 sintesi giudicate: sotto quel valore sta il 48% delle
# sintesi con errori. Si alza per ripassarne di piu', si abbassa per ripassarne meno.
# La soglia sotto la quale una sintesi di mT5 va riscritta. Misurato il 18/09 su
# 250 sintesi giudicate, quanto si intercetta degli errori GRAVI (voto <= 2):
#   ripassando il  5% (-0,4194):  4 su 18 = 22%
#   ripassando il 10% (-0,3734):  9 su 18 = 50%   <- il miglior rapporto
#   ripassando il 20% (-0,3166): 13 su 18 = 72%
# Scendere al 5% dimezzerebbe il lavoro ma butterebbe via piu' della meta' del
# beneficio: 22% invece di 50%.
#
# E la soglia conta meno di quanto sembri: la coda si lavora dalla MENO CONVINTA
# in avanti (ORDER BY fiducia). Finche' non riusciamo a svuotarla — e oggi non ci
# riusciamo — il 2B sta comunque riscrivendo le peggiori. La soglia e' un tetto a
# cio' che e' ammissibile, non un bersaglio da raggiungere.
SOGLIA = float(os.environ.get("SOGLIA_RIPASSO", "-0.3734"))

# IL BUTTAFUORI. Non ha senso far cercare tecnologie al 2B in un annuncio per
# camerieri o magazzinieri. v1 classifica la famiglia professionale al 92,3%, e la
# resa di ogni famiglia e' stata MISURATA il 19/09/2026 su 71.747 offerte gia'
# estratte (99.031 tecnologie). Queste otto stanno tutte sotto 0,25 tecnologie per
# offerta: escluderle taglia il 28,2% del flusso in entrata e costa il 3,0% delle
# tecnologie. Da 67.130 offerte al giorno a 48.232.
#
# Attenzione a due famiglie che sembrano da escludere e NON lo sono: Logistics
# rende 0,65 (SAP, gestionali di magazzino) e Manufacturing 0,54. Escluderle a
# naso, come verrebbe da fare, butterebbe prodotto vero.
#
# Le offerte senza famiglia (8,8% della coda: v1 tace sotto il 90% di confidenza)
# passano comunque: su di loro il buttafuori non ha nulla su cui decidere.
FUORI = tuple(x for x in os.environ.get("FAMIGLIE_ESCLUSE", "Agriculture|Social Services|"
              "Retail|Sports & Recreation|Food & Beverage|Transportation|Trades|"
              "Healthcare").split("|") if x)

def pulito(t: str | None) -> str:
    import html as _h
    t = _h.unescape(_h.unescape(t or ""))
    return re.sub(r"\s+", " ", _TAG.sub(" ", t).replace("\xa0", " ")).strip()

def impronta(testo: str) -> str:
    return hashlib.sha1(testo.encode("utf-8", "ignore")).hexdigest()

# La coda si trova col MARCATORE `estratto_2b_at`, come fa v1 con `locale_v1_at`, e con
# l'indice parziale `ats_jobs_2b_da_fare_idx`. La prima versione cercava invece le offerte
# mancanti con un anti-join su estrazioni_v2b piu' l'estrazione del testo dal JSON per ogni
# candidata: una singola query restava minuti a leggere dal disco (16/09/2026) e il demone
# non partiva mai. Ogni riga VISTA viene marcata, anche quella senza testo utile: cosi' non
# si ripresenta a ogni giro.
# La coda si PRENOTA, non si legge e basta. Due operai (il N5 e il Mac mini) che
# leggessero la stessa coda riceverebbero le stesse 320 righe: il marcatore si scrive
# solo a fine infornata, e in quei minuti il secondo chiede e ottiene le identiche.
# Due macchine, la produzione di una. Qui si prenota in una sola istruzione atomica,
# con SKIP LOCKED perche' chi arriva secondo salti le righe gia' prese invece di
# aspettarle. La prenotazione SCADE: se un operaio muore a meta' infornata, dopo
# SCADENZA_PRESA minuti le sue righe tornano libere da sole (18/09/2026).
SQL = """
UPDATE ats_jobs j SET preso_2b_at = now()
 WHERE j.id IN (
   SELECT k.id FROM ats_jobs k
    WHERE k.expired_at IS NULL
      AND k.estratto_2b_at IS NULL
      AND (k.preso_2b_at IS NULL OR k.preso_2b_at < now() - interval '{scadenza} minutes')
      -- Non si prenota cio' che non si puo' ancora leggere: il raccoglitore
      -- scarica la pagina in media un giorno dopo aver visto l'annuncio, e una
      -- riga vista troppo presto verrebbe marcata «senza testo» per sempre
      -- (33.051 bruciate cosi' fra il 17 e il 19/09). Oltre i 7 giorni invece
      -- si prenota comunque: gli annunci che una descrizione non l'avranno mai
      -- vanno marcati una volta e togliersi dalla coda, o il cancello rallenta.
      AND (EXISTS (SELECT 1 FROM unnest(ARRAY[{campi_k}]) v WHERE length(v) >= 300)
           OR k.created_at < now() - interval '{giorni_attesa} days')
      AND {dove_paese}
    ORDER BY k.posted_at DESC NULLS LAST
    LIMIT %s
    FOR UPDATE SKIP LOCKED)
RETURNING j.id, j.title, coalesce(j.location, j.city, ''), j.country,
       coalesce((SELECT v FROM unnest(ARRAY[{campi}]) v WHERE length(v) >= 300 LIMIT 1), ''),
       (SELECT coalesce(k.v1_family, k.family) FROM job_classifications k WHERE k.job_id = j.id)
""".format(campi=", ".join(f"j.raw->>'{c}'" for c in CAMPI_TESTO),
           campi_k=", ".join(f"k.raw->>'{c}'" for c in CAMPI_TESTO), dove_paese="{dove}",
           scadenza=int(os.environ.get("SCADENZA_PRESA", "20")),
           giorni_attesa=int(os.environ.get("GIORNI_ATTESA_TESTO", "7")))

# La coda del ripasso: le sintesi in cui mT5 ha creduto di meno, le peggiori per prime.
# Stessa prenotazione con scadenza della coda principale.
SQL_RIPASSO = """
UPDATE sintesi_mt5 s SET presa_ripasso_at = now()
 WHERE s.job_id IN (
   SELECT m.job_id FROM sintesi_mt5 m
     JOIN ats_jobs j ON j.id = m.job_id AND j.expired_at IS NULL
    WHERE m.sintesi IS NOT NULL
      AND m.ripassata_at IS NULL
      AND m.fiducia <= %s
      AND (m.presa_ripasso_at IS NULL OR m.presa_ripasso_at < now() - interval '{scadenza} minutes')
    ORDER BY m.fiducia
    LIMIT %s
    FOR UPDATE SKIP LOCKED)
RETURNING s.job_id,
       (SELECT j.title FROM ats_jobs j WHERE j.id = s.job_id),
       (SELECT coalesce(j.location, j.city, '') FROM ats_jobs j WHERE j.id = s.job_id),
       (SELECT j.country FROM ats_jobs j WHERE j.id = s.job_id),
       (SELECT coalesce((SELECT v FROM unnest(ARRAY[{campi}]) v WHERE length(v) >= 300 LIMIT 1), '')
          FROM ats_jobs j WHERE j.id = s.job_id)
""".format(campi=", ".join(f"j.raw->>'{c}'" for c in CAMPI_TESTO),
           scadenza=int(os.environ.get("SCADENZA_PRESA", "20")))

SQL_INSERISCI = (
    "INSERT INTO estrazioni_v2b (job_id, tecnologie, sintesi, modello, testo_hash) "
    "VALUES (%s,%s,%s,%s,%s) ON CONFLICT (job_id) DO NOTHING")

# Il ripasso riscrive DENTRO sintesi_mt5, non in estrazioni_v2b.
# Il primo disegno inseriva in estrazioni_v2b una riga con le sole sintesi: ma mT5
# e' tre volte piu' veloce del 2B, quindi arriva regolarmente su offerte di cui il
# 2B non ha ancora fatto le tecnologie — e quella riga, per via dell'ON CONFLICT DO
# NOTHING del passaggio tecnologie, sarebbe rimasta senza tecnologie per sempre.
# La sintesi originale di mT5 si conserva: serve per confrontare i due modelli.
#
# `sintesi_pulita` si riscrive INSIEME a `sintesi`, o la vista servirebbe il testo
# vecchio: chi legge prende la colonna ripulita, e quella qui dentro e' ancora la
# versione di mT5 di prima del ripasso (20/09/2026).
SQL_RISCRIVI = (
    "UPDATE sintesi_mt5 SET sintesi_originale = sintesi, sintesi = %s, "
    "sintesi_pulita = %s, frasi_tolte = %s, pulita_at = now(), modello = %s, "
    "ripassata_at = now(), presa_ripasso_at = NULL WHERE job_id = %s")

def chiedi(url: str, sistema: str, testo_utente: str, gram: str | None, max_nuovi: int, tentativi: int = 3):
    body = {"model": "m", "messages": [{"role": "system", "content": sistema}, {"role": "user", "content": testo_utente}],
            "max_tokens": max_nuovi, "temperature": 0, "chat_template_kwargs": {"enable_thinking": False}}
    if gram: body["grammar"] = gram
    dati = json.dumps(body).encode()
    for k in range(tentativi):
        try:
            rq = urllib.request.Request(url + "/v1/chat/completions", data=dati, headers={"Content-Type": "application/json"})
            d = json.load(urllib.request.urlopen(rq, timeout=1200))
            return d["choices"][0]["message"]["content"].strip()
        except Exception as e:
            if k == tentativi - 1: return f"__ERRORE__ {e}"
            time.sleep(2 * (k + 1))
    return "__ERRORE__"

def estrai_json(t: str):
    m = re.search(r"\{.*\}", t or "", re.S)
    if not m: return None
    try: return json.loads(m.group(0))
    except Exception: return None

def valida(d):
    """Tiene solo cio' che vendiamo: tecnologie con nome e ruolo noto, sintesi di lunghezza sensata."""
    if not isinstance(d, dict): return None
    tec = []
    for x in d.get("tecnologie") or []:
        if isinstance(x, dict) and isinstance(x.get("nome"), str) and x["nome"].strip():
            r = str(x.get("ruolo") or "").lower()
            tec.append({"nome": x["nome"].strip()[:80], "ruolo": r if r in ("usata", "servizio", "gradita") else "usata"})
        if len(tec) >= 40: break
    s = d.get("sintesi")
    s = s.strip() if isinstance(s, str) else None
    n = len(s.split()) if s else 0
    return {"tecnologie": tec, "sintesi": s if s and 20 <= n <= 200 else None, "sintesi_parole": n}

def prompt(titolo, sede, paese, testo) -> str:
    return f"Titolo: {titolo or ''}\nSede: {sede or ''} ({paese or '-'})\nLingua dell'annuncio: ?\n\n{testo[:6000]}"

def infornata_ripasso(c, a, gram_rip, st) -> int:
    """Riscrive le sintesi dove mT5 ha esitato. Ritorna quante ne ha viste.

    La grammatica del ripasso impone il solo campo `sintesi`: quella normale
    pretende anche `tecnologie`, e il modello sprecherebbe token a riempirla.
    """
    righe = c.execute(SQL_RIPASSO, (a.soglia, a.limite)).fetchall()
    if not righe: return 0
    lavoro = []
    for jid, titolo, sede, paese, grezzo in righe:
        testo = pulito(grezzo)
        if len(testo) < 300:
            # il testo e' sparito da sotto: la si chiude, non si ripassa all'infinito
            c.execute("UPDATE sintesi_mt5 SET ripassata_at = now(), presa_ripasso_at = NULL WHERE job_id = %s", (jid,))
            continue
        lavoro.append((jid, titolo, sede, paese, testo, impronta(testo)))
    if not lavoro: return len(righe)
    def una(x):
        return x, chiedi(a.url, SISTEMA_SIN, prompt(x[1], x[2], x[3], x[4]), gram_rip, 300)
    with cf.ThreadPoolExecutor(a.par) as ex: risposte = list(ex.map(una, lavoro))
    scritte, fatte = [], []
    for x, grezza in risposte:
        st["ripassate"] += 1
        if str(grezza).startswith("__ERRORE__"):
            st["muti"] += 1          # server muto: si riprova al prossimo giro
            continue
        v = valida(estrai_json(grezza))
        if not v or not v["sintesi"]:
            st["errori"] += 1
            # niente sintesi nuova: si tiene quella di mT5 e non si riprova
            fatte.append(x[0]); continue
        p, tolte, _ = ripulisci(v["sintesi"], f"{x[1] or ''} {x[4] or ''}")
        scritte.append((v["sintesi"], p, tolte, MODELLO + "+ripasso", x[0]))
    if not a.dry_run:
        if scritte:
            with c.cursor() as cur: cur.executemany(SQL_RISCRIVI, scritte)
            st["riscritte"] += len(scritte)
        # chi non e' stato riscritto va marcato lo stesso: tiene la sintesi di mT5
        # e non deve ripresentarsi a ogni giro
        if fatte:
            c.execute("UPDATE sintesi_mt5 SET ripassata_at = now(), presa_ripasso_at = NULL "
                      "WHERE job_id = ANY(%s) AND ripassata_at IS NULL", (fatte,))
    return len(righe)

def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--limite", type=int, default=2000)
    ap.add_argument("--par", type=int, default=32)
    ap.add_argument("--url", default=os.environ.get("URL_2B", "http://127.0.0.1:8089"))
    ap.add_argument("--grammatica")
    ap.add_argument("--grammatica-ripasso")
    ap.add_argument("--max-nuovi", type=int, default=450)
    ap.add_argument("--continuo", action="store_true")
    ap.add_argument("--fuori-europa", action="store_true")
    ap.add_argument("--solo-tecnologie", action="store_true")
    ap.add_argument("--ripasso", action="store_true")
    ap.add_argument("--soglia", type=float, default=SOGLIA)
    # una infornata di ripasso ogni quante di tecnologie: 6 vuol dire
    # circa il 17% della capacita' al ripasso, il resto alle tecnologie
    ap.add_argument("--ogni-quante", type=int, default=int(os.environ.get("OGNI_QUANTE", "6")))
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()
    gram = open(a.grammatica).read() if a.grammatica else None
    gram_rip = open(a.grammatica_ripasso).read() if a.grammatica_ripasso else None
    sistema = SISTEMA_TEC if a.solo_tecnologie else SISTEMA
    if a.solo_tecnologie and a.max_nuovi > 200:
        a.max_nuovi = 200                      # senza sintesi non servono 450 parole
    # ATTENZIONE all'alias: il filtro vive dentro la sottoquery, dove la tabella si
    # chiama `k`. Scrivendo `j.` si punta alla riga ESTERNA dell'UPDATE — la query
    # resta valida, non da' errore, e non restituisce mai niente (18/09/2026).
    dove = "upper(k.country) = ANY(%s)" if not a.fuori_europa else "(k.country IS NULL OR NOT (upper(k.country) = ANY(%s)))"
    sql = SQL.format(dove=dove)
    st = {"viste": 0, "modello": 0, "gemelle": 0, "scritte": 0, "errori": 0, "senza_testo": 0,
          "ripassate": 0, "riscritte": 0, "muti": 0, "buttafuori": 0, "inventate": 0}
    t0 = time.time()
    giri = 0
    print(f"2b: {'sole tecnologie' if a.solo_tecnologie else 'tecnologie e sintesi'}"
          f"{f' + ripasso sotto {a.soglia:+.4f}, una infornata ogni {a.ogni_quante}' if a.ripasso else ''}", flush=True)
    with psycopg.connect(os.environ["ATS_DATABASE_URL"], autocommit=True) as c:
        while True:
            # Il ripasso non puo' aspettare che la coda delle tecnologie sia vuota:
            # l'arretrato e' di milioni di offerte, quel momento non arriva mai e il
            # ripasso non partirebbe (19/09/2026). Si alterna: una infornata di
            # ripasso ogni OGNI_QUANTE di tecnologie, piu' tutte quelle che entrano
            # nel tempo morto quando la coda principale e' davvero vuota.
            giri += 1
            if a.ripasso and a.ogni_quante and giri % a.ogni_quante == 0:
                if infornata_ripasso(c, a, gram_rip, st):
                    dt = max(time.time() - t0, 1e-6)
                    print(f"2b ripasso: viste {st['ripassate']} | riscritte {st['riscritte']} "
                          f"| errori {st['errori']} | muti {st['muti']} "
                          f"| {int(86400*st['riscritte']/dt):,}/giorno", flush=True)
                    if not a.continuo: break
                    continue
            righe = c.execute(sql, (list(EUROPA), a.limite)).fetchall()
            if not righe:
                # coda principale vuota: allora il ripasso si prende tutto il tempo
                if a.ripasso and infornata_ripasso(c, a, gram_rip, st):
                    dt = max(time.time() - t0, 1e-6)
                    print(f"2b ripasso: viste {st['ripassate']} | riscritte {st['riscritte']} "
                          f"| errori {st['errori']} | {int(86400*st['riscritte']/dt):,}/giorno", flush=True)
                    if not a.continuo: break
                    continue
                print("2b: niente da fare, aspetto", flush=True)
                if not a.continuo: break
                time.sleep(120); continue
            lavoro = []; tutti = []; saltate = []
            for jid, titolo, sede, paese, grezzo, famiglia in righe:
                st["viste"] += 1; tutti.append(jid)
                testo = pulito(grezzo)
                if len(testo) < 300:
                    st["senza_testo"] += 1
                    # niente impronta: una riga d'esito non deve finire fra le gemelle
                    saltate.append((jid, None, None, MODELLO + "+saltato:senza-testo", None))
                    continue
                if a.solo_tecnologie and famiglia in FUORI:
                    # lista VUOTA, non NULL: e' una risposta, non un buco. Ma il
                    # modello dice «+famiglia-esclusa» e non «nivult-2b», perche'
                    # questa lista l'ha decisa una regola, non il modello: fra il 5%
                    # e il 21% di queste offerte una tecnologia ce l'ha davvero, e
                    # se un giorno cambiamo idea sappiamo esattamente quali righe
                    # tornare a leggere.
                    st["buttafuori"] += 1
                    saltate.append((jid, Jsonb([]), None, MODELLO + "+famiglia-esclusa", None))
                    continue
                lavoro.append((jid, titolo, sede, paese, testo, impronta(testo)))
            if not lavoro:
                if not a.dry_run:
                    if saltate:
                        with c.cursor() as cur:
                            cur.executemany(SQL_INSERISCI, saltate)
                    c.execute("UPDATE ats_jobs SET estratto_2b_at = now(), preso_2b_at = NULL "
                          "WHERE id = ANY(%s)", (tutti,))
                if a.continuo: continue
                break
            # 1. le gemelle: stesso testo gia' letto dal modello
            impronte = list({x[5] for x in lavoro})
            copie = {h: (tec, sin) for h, tec, sin in c.execute(
                "SELECT DISTINCT ON (testo_hash) testo_hash, tecnologie, sintesi FROM estrazioni_v2b "
                "WHERE testo_hash = ANY(%s) ORDER BY testo_hash, creato_at", (impronte,)).fetchall()}
            da_fare = [x for x in lavoro if x[5] not in copie]
            gemelle = [x for x in lavoro if x[5] in copie]
            st["gemelle"] += len(gemelle)
            # 2. il modello, in parallelo
            def una(x):
                jid, titolo, sede, paese, testo, h = x
                return x, chiedi(a.url, sistema, prompt(titolo, sede, paese, testo), gram, a.max_nuovi)
            scritte = []
            for g in gemelle:
                tec, sin = copie[g[5]]
                scritte.append((g[0], Jsonb(tec), sin, MODELLO + "+gemella", g[5]))
            if da_fare:
                with cf.ThreadPoolExecutor(a.par) as ex: risposte = list(ex.map(una, da_fare))
                for x, grezza in risposte:
                    st["modello"] += 1
                    if str(grezza).startswith("__ERRORE__"):
                        # il server non ha risposto: non e' colpa dell'annuncio.
                        # Non si marca e non si scrive nulla: tornera' in coda.
                        st["muti"] += 1
                        tutti.remove(x[0])
                        continue
                    v = valida(estrai_json(grezza))
                    # In sole-tecnologie la sintesi manca per costruzione, e una lista
                    # di tecnologie VUOTA e' una risposta legittima: moltissimi annunci
                    # non ne citano nessuna. Contarla come errore riempirebbe l'archivio
                    # di finti fallimenti e farebbe scattare l'interruttore a vuoto.
                    vuoto = (not v) if a.solo_tecnologie else \
                            (not v or (not v["tecnologie"] and not v["sintesi"]))
                    if vuoto:
                        st["errori"] += 1
                        saltate.append((x[0], None, None, MODELLO + "+saltato:illeggibile", None))
                        continue
                    # Via i nomi che l'annuncio non contiene: sono invenzioni del
                    # modello, e un elenco che si svuota del tutto e' un esito
                    # legittimo (l'annuncio non nominava tecnologie vere).
                    v["tecnologie"], inventate = ancoraggio.filtra(x[4], v["tecnologie"])
                    st["inventate"] += inventate
                    scritte.append((x[0], Jsonb(v["tecnologie"]), v["sintesi"],
                                    MODELLO + ("+tec" if a.solo_tecnologie else ""), x[5]))
            if not a.dry_run:
                if scritte or saltate:
                    with c.cursor() as cur:
                        if scritte: cur.executemany(SQL_INSERISCI, scritte)
                        # L'esito di chi e' stato saltato: senza questa riga l'offerta
                        # resta segnata come fatta e non torna mai piu' (17/09/2026).
                        if saltate: cur.executemany(SQL_INSERISCI, saltate)
                    st["scritte"] += len(scritte)
                # Marcate TUTTE le righe viste, comprese quelle senza testo e quelle
                # che il modello non ha saputo leggere: altrimenti tornerebbero a ogni giro.
                c.execute("UPDATE ats_jobs SET estratto_2b_at = now(), preso_2b_at = NULL "
                          "WHERE id = ANY(%s)", (tutti,))
            dt = max(time.time() - t0, 1e-6)
            # Si stampa il ritmo sulle offerte VISTE, non sulle scritte dal modello:
            # da quando c'e' il buttafuori quasi meta' delle offerte si chiude senza
            # chiamare il modello, e contare solo le scritte faceva sembrare il Mac
            # la meta' di quello che e' (25.000 invece di 45.000 al giorno).
            print(f"2b: viste {st['viste']} | modello {st['modello']} | gemelle {st['gemelle']} | scritte {st['scritte']} "
                  f"| errori {st['errori']} | muti {st['muti']} | buttafuori {st['buttafuori']} | senza testo {st['senza_testo']} "
                  f"| inventate {st['inventate']} "
                  f"| {int(86400*st['viste']/dt):,} offerte/giorno (di cui {int(86400*st['modello']/dt):,} al modello)", flush=True)
            if st["modello"] >= 300 and st["errori"] / max(st["modello"], 1) > 0.10:
                print("INTERRUTTORE: oltre il 10% di risposte inutilizzabili, mi fermo", flush=True); return 3
            if st["muti"] >= 40 and st["muti"] / max(st["modello"], 1) > 0.5:
                print("INTERRUTTORE: il server non risponde, mi fermo e lascio riavviare", flush=True); return 4
            if not a.continuo and st["viste"] >= a.limite: break
            if a.dry_run and st["viste"] >= a.limite: break
    return 0

if __name__ == "__main__":
    sys.exit(main())
