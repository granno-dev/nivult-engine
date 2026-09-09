"""Banco di prova del lettore di salari: casi scritti a mano, nessun database.
Ogni riga e' un annuncio reale ridotto all'osso, con la risposta attesa."""
from nivult.ats.salari import parse_testo

CASI = [
    ("Verdien een salaris tussen €3.900 en €5.000 per maand", "NL", (3900, 5000, "EUR", "month")),
    ("Met een salaris tussen €4.411,16 en €5.237,70 per maand", "NL", (4411.16, 5237.70, "EUR", "month")),
    ("Salary $65,000 - $75,000 per year", "US", (65000, 75000, "USD", "year")),
    ("Pay Range USD $16.10 - USD $19.25 /Hr.", "US", (16.10, 19.25, "USD", "hour")),
    ("Le salaire est de 12,02 € brut de l'heure", "FR", (12.02, 12.02, "EUR", "hour")),
    ("Salary : £17.50–£24.00 per hour PAYE", "GB", (17.50, 24.00, "GBP", "hour")),
    ("Gehalt zwischen 45.000 € und 55.000 € pro Jahr", "DE", (45000, 55000, "EUR", "year")),
    ("Retribuzione: 1.400 € lordi al mese", "IT", (1400, 1400, "EUR", "month")),
    ("Compensation: $80,000 CAD annually", "CA", (80000, 80000, "CAD", "year")),
    ("Lön 35 000 kr per månad", "SE", (35000, 35000, "SEK", "month")),
    ("Salaire: 2 000 € à 2 500 € par mois", "FR", (2000, 2500, "EUR", "month")),
    ("base salary between $70,000 and $85,000 per year", "US", (70000, 85000, "USD", "year")),
    ("Salaire entre 30 000 € et 35 000 € par an", "FR", (30000, 35000, "EUR", "year")),
    ("We offer a salary of $50,000 per year and 3 weeks of holiday", "US", (50000, 50000, "USD", "year")),
    # e i casi in cui deve TACERE
    ("Competitive salary and great benefits", "US", None),
    ("We work 38 hours per week", "IT", None),
    ("Our revenue grew to $50 million per year", "US", None),
]

ko = 0
for testo, paese, atteso in CASI:
    r = parse_testo(testo, paese)
    got = (round(float(r[0]), 2), round(float(r[1]), 2), r[2], r[3]) if r else None
    exp = (round(float(atteso[0]), 2), round(float(atteso[1]), 2), atteso[2], atteso[3]) if atteso else None
    ok = got == exp
    ko += 0 if ok else 1
    print(("  ok  " if ok else "  KO  ") + f"{str(got):36s} atteso {str(exp):36s} | {testo[:52]}")
print(f"\n{len(CASI) - ko}/{len(CASI)} passano")
raise SystemExit(1 if ko else 0)
