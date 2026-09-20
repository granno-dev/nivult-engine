"""Quanto inventano le sintesi di mT5.

Un voto da 1 a 5 dato da un giudice dice se la sintesi e' *bella*. Per venderla
serve sapere se e' *vera*, e questo si verifica senza giudice: un numero o un
nome proprio che nella sintesi c'e' e nell'annuncio no, e' inventato. Stesso
principio dell'ancoraggio delle tecnologie — si punta il dito sul testo.

Cosa si guarda, per riga:
  numeri      cifre della sintesi assenti dalla fonte (salari, anni, indirizzi)
  nomi        parole con la maiuscola assenti dalla fonte (aziende, citta')
  troncata    non finisce con punteggiatura: il modello e' stato interrotto
  lingua      la sintesi non e' nella lingua dell'annuncio
  ripete      un pezzo di 5 parole compare due volte: degenerazione
  fuori       meno di 20 o piu' di 200 parole (la regola del demone)

I FALSI ALLARMI CHE HO DOVUTO TOGLIERE, o il numero non vuol dire niente:
  - la fonte e' il corpo PIU' titolo, luogo, paese e nome azienda: stanno in
    colonne separate, non nel testo, e la sintesi li cita legittimamente;
  - mT5 spazia male («EH &S Engineer», «J YSK»): si confronta a caratteri
    schiacciati, senza spazi e senza punteggiatura;
  - la prima parola di ogni frase ha la maiuscola per grammatica, non perche'
    e' un nome: si salta;
  - tedesco e svedese scrivono i sostantivi comuni con la maiuscola: per
    quelle lingue i nomi non si contano, resterebbe solo rumore;
  - un numero scritto in lettere nell'annuncio e in cifre nella sintesi non e'
    un'invenzione, ma non so distinguerlo: per questo i numeri sotto il 13 e
    gli anni fra 2019 e 2030 li lascio stare, sono quasi sempre quelli.

Resta una misura PRUDENTE al contrario: conta solo cio' che sa provare, quindi
il vero tasso e' almeno questo, mai meno.
"""
from __future__ import annotations
import os
import re
import sys
import unicodedata

import json

# Il campione arriva da psql dentro il container (una riga JSON per offerta):
# il DSN dal file di shell si rompe sulla password, e per una misura non serve
# un altro modo di autenticarsi — serve il campione.
CAMPIONE = os.environ.get("CAMPIONE", "/tmp/campione-sintesi.jsonl")
QUANTE = int(sys.argv[1]) if len(sys.argv) > 1 else 4000

CAMPI = ("description", "content", "descriptionHtml", "descriptionPlain", "externalDescription",
         "jobDescription", "job_description", "Job_Description", "body", "content_html",
         "description_html", "descriptionBody", "text", "ShortDescriptionStr")

SQL = """
SELECT s.sintesi, s.fiducia, j.title, coalesce(j.location,'')||' '||coalesce(j.city,'')||' '||coalesce(j.country,''),
       coalesce(j.lang,''), coalesce(p.name,''),
       coalesce((SELECT v FROM unnest(ARRAY[{campi}]) v WHERE length(v) >= 300 LIMIT 1), '')
  FROM sintesi_mt5 s
  JOIN ats_jobs j ON j.id = s.job_id
  LEFT JOIN ats_platforms p ON p.id = j.platform_id
 WHERE s.sintesi IS NOT NULL
 ORDER BY random() LIMIT %s
""".format(campi=", ".join(f"j.raw->>'{c}'" for c in CAMPI))

_TAG = re.compile(r"<[^>]+>")
# lingue che scrivono i sostantivi comuni con la maiuscola: i nomi non si contano
MAIUSCOLE_OVUNQUE = {"de", "sv", "da", "no", "nb", "nn", "lb"}


def schiaccia(t: str) -> str:
    """Tutto minuscolo, senza accenti ne' spazi ne' punteggiatura.

    mT5 spazia a caso («EH &S», «J YSK»): schiacciando, «EH&S» e «EH &S»
    diventano la stessa cosa e il confronto smette di mentire.
    """
    t = unicodedata.normalize("NFKD", t.lower())
    return "".join(c for c in t if c.isalnum())


NUMERO = re.compile(r"\d[\d.,]*")
PAROLA = re.compile(r"\b[A-ZÀ-ÖØ-Þ][\wÀ-ÖØ-öø-ÿ&.'-]{2,}")
FINE_FRASE = re.compile(r"[.!?:;]\s*$")


def numeri_inventati(sintesi: str, fonte_piatta: str) -> list[str]:
    fuori = []
    for m in NUMERO.finditer(sintesi):
        grezzo = m.group(0).rstrip(".,")
        secco = grezzo.replace(".", "").replace(",", "")
        if not secco:
            continue
        n = int(secco) if secco.isdigit() else None
        # i piccoli e gli anni: quasi sempre scritti in lettere nella fonte
        if n is not None and (n < 13 or 2019 <= n <= 2030):
            continue
        if schiaccia(grezzo) not in fonte_piatta and schiaccia(secco) not in fonte_piatta:
            fuori.append(grezzo)
    return fuori


def nomi_inventati(sintesi: str, fonte_piatta: str) -> list[str]:
    fuori = []
    # la prima parola di ogni frase ha la maiuscola per grammatica: si salta
    inizi = {0}
    for m in re.finditer(r"[.!?:;]\s+", sintesi):
        inizi.add(m.end())
    for m in PAROLA.finditer(sintesi):
        if m.start() in inizi:
            continue
        p = m.group(0).rstrip(".,")
        if schiaccia(p) not in fonte_piatta:
            fuori.append(p)
    return fuori


def ripete(sintesi: str) -> bool:
    p = sintesi.lower().split()
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
    except Exception:                                              # noqa: BLE001
        return None


def main() -> None:
    righe = []
    with open(CAMPIONE, encoding="utf-8") as f:
        for r in f:
            r = r.strip()
            if not r:
                continue
            d = json.loads(r)
            righe.append((d["sintesi"], d.get("fiducia"), d.get("titolo") or "",
                          d.get("luogo") or "", d.get("lang") or "",
                          d.get("piattaforma") or "", d.get("corpo") or ""))
            if len(righe) >= QUANTE:
                break

    st = {k: 0 for k in ("righe", "numeri", "nomi", "troncata", "lingua", "ripete", "fuori", "pulita")}
    esempi: dict[str, list[str]] = {"numeri": [], "nomi": [], "lingua": []}
    n_lingua_controllata = 0
    for sintesi, _fid, titolo, luogo, lang, azienda, corpo in righe:
        st["righe"] += 1
        fonte = " ".join((titolo or "", luogo or "", azienda or "", _TAG.sub(" ", corpo or "")))
        piatta = schiaccia(fonte)

        num = numeri_inventati(sintesi, piatta)
        nom = [] if lang in MAIUSCOLE_OVUNQUE else nomi_inventati(sintesi, piatta)
        tro = not FINE_FRASE.search(sintesi)
        rip = ripete(sintesi)
        par = len(sintesi.split())
        fuo = par < 20 or par > 200

        if num:
            st["numeri"] += 1
            if len(esempi["numeri"]) < 8:
                esempi["numeri"].append(f"{num[:3]}  ← {sintesi[:90]}")
        if nom:
            st["nomi"] += 1
            if len(esempi["nomi"]) < 8:
                esempi["nomi"].append(f"{nom[:3]}  ← {sintesi[:90]}")
        if tro:
            st["troncata"] += 1
        if rip:
            st["ripete"] += 1
        if fuo:
            st["fuori"] += 1

        if lang and len(sintesi) > 60:
            n_lingua_controllata += 1
            vista = lingua_di(sintesi)
            if vista and vista.split("-")[0] != lang.split("-")[0].split("_")[0]:
                st["lingua"] += 1
                if len(esempi["lingua"]) < 6:
                    esempi["lingua"].append(f"annuncio {lang}, sintesi {vista}: {sintesi[:80]}")

        if not (num or nom or tro or rip or fuo):
            st["pulita"] += 1

    n = st["righe"] or 1
    print(f"=== {n} sintesi mT5 prese a caso\n")
    ordine = (("numeri", "un numero che nell'annuncio non c'e'"),
              ("nomi", "un nome proprio che nell'annuncio non c'e'"),
              ("troncata", "finisce a meta' frase"),
              ("ripete", "ripete cinque parole di fila"),
              ("fuori", "fuori misura (sotto 20 o sopra 200 parole)"))
    for k, che in ordine:
        print(f"  {st[k] * 100 / n:5.1f}%  ({st[k]:5d})  {che}")
    if n_lingua_controllata:
        print(f"  {st['lingua'] * 100 / n_lingua_controllata:5.1f}%  ({st['lingua']:5d})  "
              f"non e' nella lingua dell'annuncio (su {n_lingua_controllata} controllate)")
    print(f"\n  {st['pulita'] * 100 / n:5.1f}%  ({st['pulita']:5d})  senza nessun difetto di questa lista")

    for k, v in esempi.items():
        if v:
            print(f"\n--- esempi «{k}»:")
            for r in v:
                print(f"    {r}")


if __name__ == "__main__":
    main()
