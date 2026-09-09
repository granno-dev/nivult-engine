"""La verita' gratis: sulle offerte che hanno GIA' il salario strutturato,
si legge il testo e si confronta. Nessuna etichetta a mano.

Il disaccordo si SPACCA per classe, perche' «diverso» mette insieme cose
che non si curano allo stesso modo: un periodo sbagliato e' un errore
nostro, una valuta diversa a importo identico spesso e' il campo della
fonte a essere sciatto, un importo piu' alto e' il premio letto al posto
della base.

Il campione si DEDUPLICA sul testo: la stessa offerta ripubblicata dieci
volte contava dieci errori e gonfiava il quadro (misurato: 7 errori su 70
erano un solo annuncio francese)."""
import os, hashlib, collections
import psycopg
from nivult.ats.salari import parse_testo
from nivult.ats.testo import SQL_TESTO

c = psycopg.connect(os.environ["ATS_DATABASE_URL"])
q = f"""SELECT salary_min, salary_max, salary_currency, salary_period, country,
               {SQL_TESTO}
          FROM ats_jobs
         WHERE expired_at IS NULL AND salary_min IS NOT NULL
           AND salary_period IS NOT NULL AND ({SQL_TESTO}) IS NOT NULL
         -- campione FISSO, non random(): due versioni del lettore vanno
         -- confrontate sulle STESSE offerte, o la differenza fra 91,9 e
         -- 92,8 e' il sorteggio e non il codice
         ORDER BY md5(id::text) LIMIT %s"""

# Per confrontare periodi diversi. Non servono a scrivere niente nel
# database: servono solo a capire se «2.300 €/mese» e «31.000 €/anno»
# sono la stessa paga detta in due modi (lo sono) o un errore vero.
_ANNO = {"hour": 1720.0, "day": 220.0, "week": 52.0, "month": 12.0, "year": 1.0}

st = collections.Counter()
classi = collections.Counter()
esempi = collections.defaultdict(list)
visti = set()
for mn, mx, cur, per, paese, testo in c.execute(q, (int(os.environ.get("N", "6000")),)):
    h = hashlib.sha1((testo or "")[:3000].encode()).hexdigest()
    if h in visti:
        continue
    visti.add(h)
    r = parse_testo(testo, paese)
    if not r:
        st["non trovato nel testo"] += 1
        continue
    tmn, tmx, tcur, tper, frase = r
    st["trovato"] += 1
    ok_per = (tper == per)
    ok_cur = (tcur == cur) if cur else True
    vicino = abs(float(tmn) - float(mn)) <= max(1.0, float(mn) * 0.10)
    if ok_per and ok_cur and vicino:
        st["GIUSTO"] += 1
        continue
    st["diverso"] += 1
    stessa_paga = (abs(float(tmn) * _ANNO[tper] - float(mn) * _ANNO[per])
                   <= 0.15 * float(mn) * _ANNO[per])
    if not ok_per and stessa_paga and ok_cur:
        k = "unita' diversa, STESSA paga (2.300/mese = 31.000/anno)"
    elif not ok_per:
        k = "periodo diverso"
    elif not ok_cur and vicino and (paese or "US") != "US" and cur == "USD":
        k = "la fonte dice USD fuori dagli USA (sospetta LEI)"
    elif not ok_cur and vicino:
        k = "solo la valuta (importo uguale)"
    elif not ok_cur:
        k = "valuta E importo"
    elif float(tmn) > float(mn):
        k = "importo piu' ALTO (premio letto al posto della base?)"
    else:
        k = "importo piu' BASSO"
    classi[k] += 1
    if len(esempi[k]) < 4:
        esempi[k].append((f"{mn}-{mx} {cur}/{per}", f"{tmn}-{tmx} {tcur}/{tper}", frase[:120]))

print(f"campione: {sum(st.values())} offerte DISTINTE col salario strutturato")
for k, v in st.most_common():
    print(f"  {k:34s} {v}")
tr = st["trovato"]
indulgenti = (classi["unita' diversa, STESSA paga (2.300/mese = 31.000/anno)"]
              + classi["la fonte dice USD fuori dagli USA (sospetta LEI)"])
if tr:
    print(f"\nPRECISIONE severa (tutto quello che non combacia e' errore): "
          f"{100 * st['GIUSTO'] / tr:.1f}%")
    print(f"PRECISIONE sostanziale (la stessa paga in un'altra unita' e la "
          f"valuta che sbaglia la FONTE non contano): "
          f"{100 * (st['GIUSTO'] + indulgenti) / tr:.1f}%")
    print(f"copertura sulle offerte col salario strutturato: "
          f"{100 * tr / max(sum(st.values()), 1):.1f}%")
print("\ndove sbaglia:")
for k, v in classi.most_common():
    print(f"\n  {k}  —  {v} su {st['diverso']}")
    for a, b, f in esempi[k]:
        print(f"      {a:24s} -> {b:24s}")
        print(f"      | {f}")
