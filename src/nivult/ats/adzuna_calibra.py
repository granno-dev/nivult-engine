"""Calibrazione: le NOSTRE stime salariali contro il mercato Adzuna.

Uso interno di controllo qualita' (i numeri Adzuna non si rivendono):
per ogni cella grossa del benchmark si chiede l'istogramma Adzuna di un
mestiere rappresentativo della famiglia e si confronta la mediana. Se
lo scostamento e' sistematico, lo sapremo PRIMA dei clienti.
"""
import re, time, httpx, psycopg

env = open("/opt/nivult/engine/.env").read()
AID = re.search(r"^ADZUNA_APP_ID=(.*)$", env, re.M).group(1).strip()
AKEY = re.search(r"^ADZUNA_APP_KEY=(.*)$", env, re.M).group(1).strip()
pw = re.search(r"^POSTGRES_PASSWORD=(.*)$", open("/opt/nivult/.env").read(), re.M).group(1).strip()
c = psycopg.connect(f"postgresql://nivult:{pw}@127.0.0.1:5432/nivult_ats")

MESTIERE = {
    "Engineering": "engineer", "Healthcare": "nurse",
    "Software": "software developer", "Sales": "sales executive",
    "Finance & Accounting": "accountant", "Logistics": "logistics",
    "Education": "teacher", "Manufacturing": "production operator",
    "Management & Leadership": "operations manager",
    "Trades": "electrician", "Customer Service & Support": "customer service",
    "Transportation": "driver",
}
PAESI_ADZUNA = {"US": "us", "GB": "gb", "CA": "ca", "DE": "de",
                "FR": "fr", "IT": "it", "ES": "es", "NL": "nl",
                "AT": "at", "PL": "pl", "AU": "au"}

def mediana_da_istogramma(h: dict) -> float | None:
    if not h: return None
    fasce = sorted((int(k), v) for k, v in h.items())
    tot = sum(v for _, v in fasce)
    if tot < 100: return None
    acc = 0
    for i, (soglia, n) in enumerate(fasce):
        acc += n
        if acc >= tot / 2:
            larghezza = (fasce[i+1][0] - soglia) if i+1 < len(fasce) else 20000
            return soglia + larghezza / 2
    return None

cli = httpx.Client(timeout=20)
print(f"{'cella':<34} {'nostra p50':>10} {'adzuna~':>9} {'scarto':>7}")
scarti = []
for paese, famiglia, p50, n in c.execute("""
    SELECT country, family, p50, n FROM stipendi_benchmark
     WHERE seniority='' ORDER BY n DESC LIMIT 25""").fetchall():
    cc = PAESI_ADZUNA.get(paese); q = MESTIERE.get(famiglia)
    if not cc or not q: continue
    try:
        r = cli.get(f"https://api.adzuna.com/v1/api/jobs/{cc}/histogram",
                    params={"app_id": AID, "app_key": AKEY, "what": q})
        med = mediana_da_istogramma((r.json() or {}).get("histogram") or {})
    except Exception:
        med = None
    if med:
        scarto = 100 * (p50 - med) / med
        scarti.append(scarto)
        print(f"{paese} {famiglia[:28]:<30} {p50:>10} {int(med):>9} {scarto:>+6.0f}%")
    time.sleep(1)
if scarti:
    scarti.sort()
    print(f"\nscarto mediano: {scarti[len(scarti)//2]:+.0f}% su {len(scarti)} celle")
