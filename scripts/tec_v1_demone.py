"""La testa tecnologie di v1 in produzione, sul N5.

Sostituisce il 2B sul Mac mini per le tecnologie: 218.000 annunci al giorno
contro 36.000, e sul golden F1 74,5% contro 65,4%. Il 2B resta acceso finche'
non siamo sicuri — le due uscite stanno in tabelle diverse e si confrontano.

COSA FA DIVERSAMENTE DA UN GENERATIVO. La testa MARCA parole del testo, quindi
non puo' inventare: ogni nome che scrive e' un pezzo dell'annuncio. Il filtro
sulle invenzioni (ancoraggio.filtra) qui non serve — sarebbe sempre vero.
Il rovescio: i nomi escono come li scrive l'annuncio («excel», non «Microsoft
Excel»). La normalizzazione sta a valle, negli alias, e si puo' cambiare senza
rifare l'estrazione.

  ATS_DATABASE_URL=... python tec_v1_demone.py [--continuo] [--limite N]
"""
from __future__ import annotations
import argparse
import hashlib
import importlib.util
import os
import pathlib
import sys
import time

import psycopg
import torch
from psycopg.types.json import Jsonb
from transformers import AutoModelForTokenClassification, AutoTokenizer

_sp = importlib.util.spec_from_file_location(
    "ancoraggio", str(pathlib.Path(__file__).resolve().parent / "ancoraggio.py"))
ancoraggio = importlib.util.module_from_spec(_sp)
_sp.loader.exec_module(ancoraggio)

_sf = importlib.util.spec_from_file_location(
    "finestre", str(pathlib.Path(__file__).resolve().parent / "finestre.py"))
_finestre_mod = importlib.util.module_from_spec(_sf)
_sf.loader.exec_module(_finestre_mod)
finestre = _finestre_mod.finestre

MODELLO = os.environ.get("MODELLO_TEC", "/opt/nivult/modelli/tec-v1")
NOME = os.environ.get("NOME_TEC", "tec-v1-ck02500")
# La soglia scelta sul golden: 0,30 da' il miglior F1 (93,0% precisione,
# 62,1% richiamo). Si alza per vendere un dato piu' pulito: a 0,70 la
# precisione e' 98,6% e il richiamo 48,4%.
SOGLIA = float(os.environ.get("SOGLIA_TEC", "0.30"))
# Il freno: la Radeon 890M prende memoria dalla RAM di sistema e se si riempie
# la macchina si incastra (16/09: carico 356, undici ore). Sopra questa quota
# non si aggiunge benzina.
TETTO_GRAFICA = float(os.environ.get("TETTO_GRAFICA", "0.70"))
# Sul Mac mini (8 GB unificati) sotto questa soglia si aspetta: e' la macchina
# di casa, non deve annaspare per noi.
# 0,4 GB e non 1,2: a misurare col modello GIA' caricato (2,5 GB), una soglia
# alta si autoblocca — il demone frena per una memoria che occupa lui stesso e
# non riparte mai (provato il 20/09). Questo e' un freno d'emergenza, non di
# routine: sotto 0,4 GB liberi macOS sta davvero soffrendo.
MIN_LIBERA_GB = float(os.environ.get("MIN_LIBERA_GB", "0.4"))

CAMPI_TESTO = ("description", "content", "descriptionHtml", "descriptionPlain", "externalDescription",
               "jobDescription", "job_description", "Job_Description", "body", "content_html",
               "description_html", "descriptionBody", "text", "ShortDescriptionStr")

# La coda si PRENOTA, non si legge e basta: due operai che leggessero la stessa
# coda riceverebbero le stesse righe. SKIP LOCKED perche' chi arriva secondo
# salti quelle gia' prese invece di aspettarle; la prenotazione SCADE, cosi' se
# un operaio muore a meta' le sue righe tornano libere da sole.
# Il cancello sul testo e' quello imparato il 19/09: non si prenota cio' che non
# si puo' ancora leggere, o lo si marca «fatto» un giorno prima che il
# raccoglitore scarichi la pagina, e quell'annuncio e' perso per sempre.
SQL = """
UPDATE ats_jobs j SET preso_tec_at = now()
 WHERE j.id IN (
   SELECT k.id FROM ats_jobs k
    WHERE k.expired_at IS NULL
      AND k.tec_v1_at IS NULL
      AND (k.preso_tec_at IS NULL OR k.preso_tec_at < now() - interval '{scadenza} minutes')
      AND (EXISTS (SELECT 1 FROM unnest(ARRAY[{campi_k}]) v WHERE length(v) >= 300)
           OR k.created_at < now() - interval '{giorni} days')
    ORDER BY k.posted_at DESC NULLS LAST
    LIMIT %s
    FOR UPDATE SKIP LOCKED)
RETURNING j.id, j.title,
       coalesce((SELECT v FROM unnest(ARRAY[{campi}]) v WHERE length(v) >= 300 LIMIT 1), '')
""".format(campi=", ".join(f"j.raw->>'{c}'" for c in CAMPI_TESTO),
           campi_k=", ".join(f"k.raw->>'{c}'" for c in CAMPI_TESTO),
           scadenza=int(os.environ.get("SCADENZA_PRESA", "20")),
           giorni=int(os.environ.get("GIORNI_ATTESA_TESTO", "7")))

# Un solo unnest con tutte le colonne: due unnest separati da una virgola
# sono un PRODOTTO INCROCIATO, e N righe diventano N al quadrato — con lo
# stesso job_id due volte, che ON CONFLICT rifiuta (provato il 20/09).
SQL_SCRIVI = ("INSERT INTO tecnologie_v1 (job_id, tecnologie, quante, testo_hash, modello, soglia) "
              "SELECT t.a, t.b, t.c, t.d, %s::text, %s::real "
              "  FROM unnest(%s::uuid[], %s::jsonb[], %s::int[], %s::text[]) AS t(a,b,c,d) "
              "ON CONFLICT (job_id) DO UPDATE SET tecnologie = excluded.tecnologie, "
              "quante = excluded.quante, modello = excluded.modello, "
              "soglia = excluded.soglia, testo_hash = excluded.testo_hash, creato_at = now()")


def grafica_piena() -> float:
    """Quanto e' piena la scheda, da 0 a 1. Zero dove non si puo' sapere."""
    try:
        b = pathlib.Path("/sys/class/drm/card0/device")
        u = int((b / "mem_info_gtt_used").read_text())
        t = int((b / "mem_info_gtt_total").read_text())
        return u / max(t, 1)
    except Exception:
        return 0.0


def memoria_libera_gb() -> float:
    """Sul Mac mini la memoria e' unificata e sono 8 GB in tutto: il modello ne
    vuole ~2,5 e il resto e' il Mac di casa di Giuseppe. Il freno sta QUI, fra un
    lotto e l'altro, non solo nel ciclo che lancia il demone: quello controlla
    una volta sola, e il demone gira in --continuo e non esce mai — lo stesso
    errore fatto con la pausa notturna di mT5 il 20/09.
    Ritorna un numero grande dove non si applica (Linux), cosi' non frena nulla."""
    if not sys.platform.startswith("darwin"):
        return 999.0
    try:
        import subprocess
        fuori = subprocess.run(["vm_stat"], capture_output=True, text=True, timeout=5).stdout
        pagine = {}
        for riga in fuori.splitlines():
            if ":" in riga:
                k, v = riga.split(":", 1)
                pagine[k.strip()] = int(v.strip().rstrip("."), 10) if v.strip().rstrip(".").isdigit() else 0
        libere = pagine.get("Pages free", 0) + pagine.get("Pages inactive", 0)
        return libere * 16384 / 1073741824
    except Exception:
        return 999.0


def impronta(testo: str) -> str:
    return hashlib.sha1(testo.encode("utf-8", "ignore")).hexdigest()


def voci(testo: str, pr, offsets, soglia: float) -> list[str]:
    """Da BIO ai nomi. L'etichetta «inizio» APRE una voce nuova anche se la
    precedente non si e' chiusa: senza, due tecnologie vicine si incollano in
    una parola inesistente («undwirst» al posto di «Lucanet», 17/09)."""
    fuori, ini, fin = set(), None, None
    for k, (oi, of) in enumerate(offsets):
        if of <= oi:
            if ini is not None:
                fuori.add(testo[ini:fin]); ini = None
            continue
        p = pr[k]
        inizio = float(p[1]) >= soglia and float(p[1]) >= float(p[2])
        dentro = float(p[1] + p[2]) >= soglia
        if inizio and ini is not None:
            fuori.add(testo[ini:fin]); ini, fin = oi, of
        elif inizio or (dentro and ini is not None):
            if ini is None:
                ini = oi
            fin = of
        elif ini is not None:
            fuori.add(testo[ini:fin]); ini = None
    if ini is not None:
        fuori.add(testo[ini:fin])
    return sorted({s for s in (x.strip(" ,;:.()[]/\n\t") for x in fuori) if 1 < len(s) <= 40})


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--continuo", action="store_true")
    ap.add_argument("--limite", type=int, default=512, help="righe prenotate per infornata")
    ap.add_argument("--lotto", type=int, default=16,
                    help="8 sul Mac mini: dimezza il picco di memoria e basta comunque")
    ap.add_argument("--max-len", type=int, default=1024)
    a = ap.parse_args()

    # Tre macchine, tre acceleratori: CUDA sul pod a noleggio, ROCm sul N5
    # (torch lo chiama «cuda» lo stesso), MPS sul Mac mini M2. Su CPU
    # gira ma va dieci volte piu' piano: e' il guasto dell'11/09, quando
    # la scheda del N5 non veniva vista e nessuno se ne accorgeva.
    dev = ("cuda" if torch.cuda.is_available()
           else "mps" if torch.backends.mps.is_available() else "cpu")
    tok = AutoTokenizer.from_pretrained(MODELLO)
    mod = AutoModelForTokenClassification.from_pretrained(MODELLO).to(dev).eval()
    print(f"tec-v1: {NOME} su {dev} | soglia {SOGLIA} | lotto {a.lotto}", flush=True)
    if dev == "cpu":
        print("ATTENZIONE: gira su CPU. La scheda non e' vista: e' il guasto dell'11/09.",
              flush=True)

    st = {"viste": 0, "scritte": 0, "gemelle": 0, "senza_testo": 0, "nomi": 0, "vuote": 0}
    t0 = time.time()
    with psycopg.connect(os.environ["ATS_DATABASE_URL"], autocommit=True) as c:
        while True:
            q = grafica_piena()
            if q > TETTO_GRAFICA:
                print(f"FRENO: memoria grafica al {100*q:.0f}%, aspetto", flush=True)
                time.sleep(60)
                continue
            libera = memoria_libera_gb()
            if libera < MIN_LIBERA_GB:
                print(f"FRENO: solo {libera:.1f} GB liberi, aspetto", flush=True)
                time.sleep(90)
                continue
            righe = c.execute(SQL, (a.limite,)).fetchall()
            if not righe:
                print("tec-v1: niente da fare", flush=True)
                if not a.continuo:
                    break
                time.sleep(120)
                continue

            lavoro, tutti = [], []
            for jid, titolo, grezzo in righe:
                st["viste"] += 1
                tutti.append(jid)
                testo = ancoraggio.pulito(grezzo)
                if len(testo) < 300:
                    st["senza_testo"] += 1
                    continue
                # NIENTE TAGLIO A MANO. Il testo entra intero e piu' sotto viene letto a
                # pezzi: a 1024 token il 22,5% degli annunci veniva letto a
                # meta', e su quelli si perdeva il 27% del testo — la fine,
                # dove i requisiti elencano gli strumenti (20/09/2026).
                lavoro.append((jid, f"{titolo or ''}\n{testo}"))

            # gemelle: stesso testo, stessa risposta. Si chiede al database
            # invece che al modello, e su un magazzino di 2,7 milioni con molti
            # annunci ripubblicati e' una quota che si sente.
            scritte = []
            if lavoro:
                impronte = [impronta(t) for _, t in lavoro]
                gia = dict(c.execute(
                    "SELECT DISTINCT ON (testo_hash) testo_hash, tecnologie FROM tecnologie_v1 "
                    "WHERE testo_hash = ANY(%s) AND modello = %s", (impronte, NOME)).fetchall())
                da_fare = [(j, t, h) for (j, t), h in zip(lavoro, impronte) if h not in gia]
                for (j, _), h in zip(lavoro, impronte):
                    if h in gia:
                        st["gemelle"] += 1
                        scritte.append((j, Jsonb(gia[h]), len(gia[h]), h))

                # i lotti si formano per lunghezza simile: il tokenizzatore
                # riempie fino al piu' lungo del lotto, e mescolare un annuncio
                # da 1.600 token con quindici da 200 fa pagare 1.600 a tutti.
                # OGNI ANNUNCIO DIVENTA UNA O PIU' FINESTRE, e il lotto si forma
                # su quelle. Un annuncio lungo costa piu' di uno corto, che e'
                # giusto: prima costava uguale perche' lo leggevamo a meta'.
                pezzi = []
                for jid, testo, h in da_fare:
                    for k, f in enumerate(finestre(tok, testo, a.max_len)):
                        pezzi.append((jid, f, h, k))
                st["finestre"] = st.get("finestre", 0) + len(pezzi)
                st["spezzati"] = st.get("spezzati", 0) + sum(
                    1 for _, _, _, k in pezzi if k == 1)

                trovate = {j: [] for j, _, _ in da_fare}
                pezzi.sort(key=lambda x: len(x[1]))
                with torch.inference_mode():
                    for i in range(0, len(pezzi), a.lotto):
                        gruppo = pezzi[i:i + a.lotto]
                        enc = tok([t for _, t, _, _ in gruppo], truncation=True,
                                  max_length=a.max_len, padding=True,
                                  return_offsets_mapping=True, return_tensors="pt")
                        off = enc.pop("offset_mapping")
                        pr = torch.softmax(
                            mod(**{k: v.to(dev) for k, v in enc.items()}).logits, -1).cpu()
                        for j2, (jid, finestra, h, _) in enumerate(gruppo):
                            for n in voci(finestra, pr[j2], off[j2].tolist(), SOGLIA):
                                if n not in trovate[jid]:
                                    trovate[jid].append(n)

                for jid, testo, h in da_fare:
                    tec = trovate[jid]
                    st["nomi"] += len(tec)
                    st["vuote"] += not tec
                    scritte.append((jid, Jsonb(tec), len(tec), h))

            # MPS non restituisce la memoria fra un lotto e l'altro se non glielo
            # si chiede: su una macchina da 8 GB la differenza si sente, e
            # svuotare la cache costa millisecondi.
            if dev == "mps":
                torch.mps.empty_cache()

            if scritte:
                c.execute(SQL_SCRIVI, (NOME, SOGLIA,
                                       [x[0] for x in scritte], [x[1] for x in scritte],
                                       [x[2] for x in scritte], [x[3] for x in scritte]))
                st["scritte"] += len(scritte)
            # Si marcano TUTTE le righe viste, anche quelle senza testo: se no si
            # ripresentano a ogni giro e il demone non avanza mai.
            c.execute("UPDATE ats_jobs SET tec_v1_at = now() WHERE id = ANY(%s::uuid[])", (tutti,))

            dt = max(time.time() - t0, 1)
            print(f"tec-v1: viste {st['viste']} | scritte {st['scritte']} | "
                  f"gemelle {st['gemelle']} | senza testo {st['senza_testo']} | "
                  f"vuote {st['vuote']} | nomi {st['nomi']} | "
                  f"{int(86400*st['viste']/dt):,} offerte/giorno", flush=True)
            if not a.continuo and st["viste"] >= a.limite:
                break
    return 0


if __name__ == "__main__":
    sys.exit(main())
