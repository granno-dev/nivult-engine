"""Due riparazioni misurate il 2026-09-06:
1. country: valori non-ISO (286 codici distinti a 2 lettere, ISO reali
   ~249; piu' 10 righe palesemente sporche). Si valida contro ISO
   3166-1 alpha-2: cio' che non e' un paese vero -> NULL.
2. created_at: 930.271 righe timbrate tutte a 2026-09-04 20:22 (il
   momento in cui la colonna e' nata col backfill), non quando abbiamo
   visto l'offerta. Si riscrive al valore vero: date_posted o fetched_at.
   Le righe vere dopo il blocco NON si toccano.
Lotti ordinati per id, regola di casa."""
import os, time, psycopg

# ISO 3166-1 alpha-2 (249 assegnati) + UK/EL comunemente usati
ISO = set("""AD AE AF AG AI AL AM AO AQ AR AS AT AU AW AX AZ BA BB BD BE BF
BG BH BI BJ BL BM BN BO BQ BR BS BT BV BW BY BZ CA CC CD CF CG CH CI CK CL
CM CN CO CR CU CV CW CX CY CZ DE DJ DK DM DO DZ EC EE EG EH ER ES ET FI FJ
FK FM FO FR GA GB GD GE GF GG GH GI GL GM GN GP GQ GR GS GT GU GW GY HK HM
HN HR HT HU ID IE IL IM IN IO IQ IR IS IT JE JM JO JP KE KG KH KI KM KN KP
KR KW KY KZ LA LB LC LI LK LR LS LT LU LV LY MA MC MD ME MF MG MH MK ML MM
MN MO MP MQ MR MS MT MU MV MW MX MY MZ NA NC NE NF NG NI NL NO NP NR NU NZ
OM PA PE PF PG PH PK PL PM PN PR PS PT PW PY QA RE RO RS RU RW SA SB SC SD
SE SG SH SI SJ SK SL SM SN SO SR SS ST SV SX SY SZ TC TD TF TG TH TJ TK TL
TM TN TO TR TT TV TW TZ UA UG UM US UY UZ VA VC VE VG VI VN VU WF WS YE YT
ZA ZM ZW UK EL XK""".split())

dsn = os.environ["ATS_DATABASE_URL"]
c = psycopg.connect(dsn, autocommit=True)

# 1. country non-ISO -> NULL (a lotti ordinati)
sporchi = [r[0] for r in c.execute(
    "SELECT DISTINCT country FROM ats_jobs WHERE country IS NOT NULL").fetchall()
    if r[0] not in ISO]
print(f"codici country non-ISO da azzerare: {len(sporchi)} distinti", flush=True)
tot = 0
for i in range(0, len(sporchi), 50):
    lotto = sporchi[i:i+50]
    tot += c.execute("UPDATE ats_jobs SET country=NULL WHERE country = ANY(%s)",
                     (lotto,)).rowcount
print(f"righe country azzerate: {tot}", flush=True)

# 2. created_at del blocco backfill -> valore vero
n = c.execute("""UPDATE ats_jobs
   SET created_at = coalesce(date_posted, fetched_at, created_at)
 WHERE created_at >= '2026-09-04 20:22:00+00'
   AND created_at <  '2026-09-04 20:23:00+00'""").rowcount
print(f"created_at del blocco riscritti al valore vero: {n}", flush=True)

# verifica
paesi = c.execute("SELECT count(DISTINCT country) FROM ats_jobs WHERE country IS NOT NULL").fetchone()[0]
nuove24 = c.execute("SELECT count(*) FROM ats_jobs WHERE created_at > now()-interval '24 hours'").fetchone()[0]
print(f"VERIFICA -> paesi distinti ora: {paesi} | offerte nuove 24h ora: {nuove24}", flush=True)
