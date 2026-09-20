"""Il dataset prima di pagare la GPU: dieci controlli, nessuno dei quali si fida.

Un addestramento costa soldi e ore, e un dataset sbagliato non lo dice: la
perdita scende lo stesso, il modello esce, e ci si accorge dell'errore
all'esame — quando si e' gia' speso. Qui si guarda PRIMA, e si guarda tutto,
anche le cose che «sicuramente sono a posto».

I controlli, e cosa romperebbe ciascuno se fallisse:

  A  struttura        una riga malformata fa saltare l'addestramento a meta'
  B  doppioni         lo stesso annuncio ripetuto pesa piu' degli altri
  C  contaminazione   il dev dentro il train = esame che misura la memoria
  D  numeri           il difetto che stiamo curando: deve essere a zero
  E  nomi propri      la stessa malattia dei numeri, mai misurata prima
  F  lunghezza        il messaggio di sistema promette 40-120 parole
  G  lingua           «nella lingua dell'annuncio» e' una promessa del prodotto
  H  degenerazione    frasi troncate o ripetute che il modello imparerebbe
  I  formato          l'ingresso deve essere IDENTICO a quello di produzione
  J  distribuzione    per sapere su cosa il modello sara' bravo e su cosa no

Nessun controllo qui «aggiusta»: dicono soltanto. Aggiustare al buio e' il modo
di far sparire un sintomo lasciando la causa.
"""
from __future__ import annotations
import argparse
import collections
import gzip
import hashlib
import json
import os
import re
import sys
import unicodedata

from transformers import AutoTokenizer

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from sintesi_ancorata import numeri_fuori, solo_cifre                  # noqa: E402

PAROLA = re.compile(r"\b[A-ZÀ-ÖØ-Þ][\wÀ-ÖØ-öø-ÿ&.'-]{2,}")
FINE = re.compile(r"[.!?:;]\s*$")
MAIUSCOLE_OVUNQUE = {"de", "sv", "da", "no", "nb", "nn", "lb"}
PREFISSO = re.compile(r"^Titolo: (.*)\nSede: (.*)\nLingua dell'annuncio: (.*?)\n\n", re.S)


def schiaccia(t: str) -> str:
    t = unicodedata.normalize("NFKD", (t or "").lower())
    return "".join(c for c in t if c.isalnum())


def impronta(t: str) -> str:
    return hashlib.sha1(re.sub(r"\s+", " ", (t or "").strip()).encode()).hexdigest()


def ripete(s: str) -> bool:
    p = s.lower().split()
    visti = set()
    for i in range(len(p) - 4):
        g = " ".join(p[i:i + 5])
        if g in visti:
            return True
        visti.add(g)
    return False


def lingua_di(t: str) -> str | None:
    try:
        from langdetect import detect, DetectorFactory
        DetectorFactory.seed = 0
        return detect(t)
    except Exception:                                                  # noqa: BLE001
        return None


def leggi(p: str):
    with gzip.open(p, "rt") as f:
        for n, linea in enumerate(f, 1):
            linea = linea.strip()
            if not linea:
                continue
            try:
                yield n, json.loads(linea)
            except Exception as e:                                     # noqa: BLE001
                yield n, {"__rotta__": str(e)}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--train", default="/opt/nivult/sintesi/sintesi-train-pulito.jsonl.gz")
    ap.add_argument("--dev", default="/opt/nivult/sintesi/sintesi-dev-pulito.jsonl.gz")
    ap.add_argument("--base", default=os.environ.get("BASE", "/opt/nivult/mt5"))
    ap.add_argument("--max-in", type=int, default=1024)
    ap.add_argument("--campione-lingua", type=int, default=3000)
    ap.add_argument("--limite", type=int, default=0)
    a = ap.parse_args()

    tok = AutoTokenizer.from_pretrained(a.base)
    guasti: list[str] = []

    st = collections.Counter()
    impronte: collections.Counter[str] = collections.Counter()
    lingue = collections.Counter()
    parole_bersaglio: list[int] = []
    numeri_residui, nomi_residui = [], []
    troncate = ripetute = fuori_misura = prefisso_rotto = 0
    lingua_sbagliata = lingua_controllate = 0
    righe = 0

    for n, d in leggi(a.train):
        if a.limite and righe >= a.limite:
            break
        righe += 1
        if "__rotta__" in d:
            st["json rotto"] += 1
            continue
        m = d.get("messages")
        if not isinstance(m, list) or len(m) != 3:
            st["non tre messaggi"] += 1
            continue
        if [x.get("role") for x in m] != ["system", "user", "assistant"]:
            st["ruoli sbagliati"] += 1
            continue
        ingresso, bersaglio = m[1].get("content") or "", m[2].get("content") or ""
        if not ingresso.strip() or not bersaglio.strip():
            st["campo vuoto"] += 1
            continue
        st["sane"] += 1

        mm = PREFISSO.match(ingresso)
        if not mm:
            prefisso_rotto += 1
            corpo, lang = ingresso, ""
        else:
            corpo, lang = ingresso[mm.end():], (mm.group(3) or "").strip()
        lingue[lang or "?"] += 1
        impronte[impronta(corpo)] += 1

        ids = tok(ingresso, add_special_tokens=False)["input_ids"]
        if len(ids) > a.max_in:
            troncate += 1
            visto = tok.decode(ids[:a.max_in], skip_special_tokens=True)
        else:
            visto = ingresso

        fuori = numeri_fuori(bersaglio, solo_cifre(visto))
        if fuori:
            st["numeri non ancorati"] += 1
            if len(numeri_residui) < 12:
                numeri_residui.append((n, fuori[:3], bersaglio[:110]))

        if lang not in MAIUSCOLE_OVUNQUE:
            piatto = schiaccia(visto)
            inizi = {0} | {x.end() for x in re.finditer(r"[.!?:;]\s+", bersaglio)}
            nomi = [x.group(0).rstrip(".,") for x in PAROLA.finditer(bersaglio)
                    if x.start() not in inizi
                    and schiaccia(x.group(0).rstrip(".,")) not in piatto]
            if nomi:
                st["nomi non ancorati"] += 1
                if len(nomi_residui) < 12:
                    nomi_residui.append((n, nomi[:3], bersaglio[:110]))

        np_ = len(bersaglio.split())
        parole_bersaglio.append(np_)
        if np_ < 40 or np_ > 120:
            fuori_misura += 1

        if not FINE.search(bersaglio):
            st["bersaglio troncato"] += 1
        if ripete(bersaglio):
            ripetute += 1

        if lang and lingua_controllate < a.campione_lingua and len(bersaglio) > 60:
            lingua_controllate += 1
            vista = lingua_di(bersaglio)
            if vista and vista.split("-")[0] != lang.split("-")[0]:
                lingua_sbagliata += 1

        if righe % 25000 == 0:
            print(f"  {righe}...", flush=True)

    sane = st["sane"] or 1

    imp_dev = set()
    righe_dev = 0
    for _, d in leggi(a.dev):
        if "__rotta__" in d:
            continue
        m = d.get("messages")
        if not isinstance(m, list) or len(m) != 3:
            continue
        righe_dev += 1
        ing = m[1].get("content") or ""
        mm = PREFISSO.match(ing)
        imp_dev.add(impronta(ing[mm.end():] if mm else ing))
    comuni = imp_dev & set(impronte)

    print(f"\n=== {righe} righe in {os.path.basename(a.train)}\n")

    print("A  STRUTTURA")
    for k in ("json rotto", "non tre messaggi", "ruoli sbagliati", "campo vuoto"):
        v = st[k]
        print(f"     {k:<24} {v}")
        if v:
            guasti.append(f"{v} righe con «{k}»")
    print(f"     {'sane':<24} {st['sane']}")

    doppi = sum(c - 1 for c in impronte.values() if c > 1)
    print("\nB  DOPPIONI (stesso testo d'annuncio)")
    print(f"     testi distinti {len(impronte)}   copie in piu' {doppi}"
          f"   ({doppi * 100 / sane:.1f}%)")
    if impronte:
        print(f"     il testo piu' ripetuto compare {impronte.most_common(1)[0][1]} volte")
    if doppi * 100 / sane > 5:
        guasti.append(f"{doppi} copie in piu' ({doppi * 100 / sane:.1f}%)")

    print("\nC  CONTAMINAZIONE train/dev")
    print(f"     dev: {righe_dev} righe, {len(imp_dev)} testi distinti")
    print(f"     anche nel train: {len(comuni)}")
    if comuni:
        guasti.append(f"{len(comuni)} annunci del dev sono anche nel train")

    print("\nD  NUMERI non ancorati (il difetto curato)")
    q = st["numeri non ancorati"] * 100 / sane
    print(f"     {st['numeri non ancorati']} righe ({q:.3f}%)")
    for n, f, b in numeri_residui:
        print(f"       riga {n}: {f}  ← {b}")
    if q > 0.1:
        guasti.append(f"{st['numeri non ancorati']} righe con numeri non ancorati")

    print("\nE  NOMI PROPRI non ancorati (mai misurato prima)")
    print(f"     {st['nomi non ancorati']} righe ({st['nomi non ancorati'] * 100 / sane:.1f}%)")
    for n, f, b in nomi_residui:
        print(f"       riga {n}: {f}  ← {b}")

    parole_bersaglio.sort()
    med = parole_bersaglio[len(parole_bersaglio) // 2] if parole_bersaglio else 0
    print("\nF  LUNGHEZZA del bersaglio (il sistema promette 40-120 parole)")
    print(f"     mediana {med}   fuori misura {fuori_misura} ({fuori_misura * 100 / sane:.1f}%)")
    if parole_bersaglio:
        print(f"     minimo {parole_bersaglio[0]}   massimo {parole_bersaglio[-1]}")

    print(f"\nG  LINGUA del bersaglio (campione di {lingua_controllate})")
    if lingua_controllate:
        ql = lingua_sbagliata * 100 / lingua_controllate
        print(f"     diversa da quella dichiarata: {lingua_sbagliata} ({ql:.1f}%)")
        if ql > 5:
            guasti.append(f"{ql:.1f}% dei bersagli non e' nella lingua dell'annuncio")

    print("\nH  DEGENERAZIONE")
    print(f"     senza punteggiatura finale: {st['bersaglio troncato']}"
          f" ({st['bersaglio troncato'] * 100 / sane:.1f}%)")
    print(f"     che ripetono cinque parole: {ripetute} ({ripetute * 100 / sane:.1f}%)")

    print("\nI  FORMATO dell'ingresso")
    print(f"     prefisso «Titolo/Sede/Lingua» assente: {prefisso_rotto}")
    if prefisso_rotto:
        guasti.append(f"{prefisso_rotto} ingressi senza il prefisso di produzione")
    print(f"     troncati a {a.max_in} token: {troncate} ({troncate * 100 / sane:.1f}%)")

    print("\nJ  DISTRIBUZIONE delle lingue")
    for k, v in lingue.most_common(12):
        print(f"     {k or '?':<6} {v:>7}  {v * 100 / sane:>5.1f}%")

    print("\n" + "=" * 60)
    if guasti:
        print("DA GUARDARE PRIMA DI PAGARE LA GPU:")
        for g in guasti:
            print(f"  - {g}")
        return 1
    print("nessun guasto bloccante fra quelli che questo audit sa vedere.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
