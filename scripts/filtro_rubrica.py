"""Toglie le voci che la rubrica esclude gia' a parole.

Un maestro grande legge la rubrica e la viola lo stesso. Misurato il 20/09/2026
su 104 annunci: delle 31 voci sbagliate di gpt-oss-120b, quasi meta' cadono in
categorie che il messaggio di sistema elenca una per una — certificazioni
personali (BLS, ACLS, PALS, NRP), benefit con un nome proprio (Wagestream, BHN
rewards), i browser nominati per candidarsi (Chrome, Firefox), le categorie di
patente (LGV).

Quelle non servono un modello migliore: servono una regola. Un filtro
deterministico non discute, non costa niente e non cambia idea fra un giro e
l'altro — ed e' lo stesso principio dell'ancoraggio, che tiene solo cio' che si
puo' puntare col dito.

COSA NON FA. Non giudica se una parola «suona tecnica»: toglie solo cio' che sta
in un elenco scritto, con la ragione accanto. Tutto il resto passa. Un filtro
che comincia a indovinare diventa un secondo modello da misurare, e allora tanto
vale misurare il primo.

  from filtro_rubrica import filtra_rubrica
  tenuti, tolti = filtra_rubrica(["Excel", "BLS", "Wagestream"])
  # -> (["Excel"], [("BLS", "abilitazione personale"), ...])
"""
from __future__ import annotations
import re

# (ragione, espressione). L'ordine non conta: si scarta al primo che combacia.
REGOLE: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("abilitazione personale", re.compile(
        r"(?i)^(bls|acls|pals|nrp|cpr|aed/cpr|cpr/aed|first aid|primo soccorso|"
        r"basic (cardiac )?life support|advanced cardiac life support|"
        r"pediatric advanced life support|neonatal resuscitation|"
        r"h2s alive|csts|fall protection|fimo|fco|afgsu|rpps|cceps|"
        r"osha \d+|ase|caate|npi|dbs|cdl(-a)?|lgv|hgv|"
        r"large goods vehicle.*|heavy goods vehicle.*|"
        r"patente [a-e]\b.*|permis [a-e]{1,2}\b.*|b-k[oö]rkort.*|f[uü]hrerschein.*)$")),
    ("benefit o welfare", re.compile(
        r"(?i)^(wagestream|dailypay|noom|skinio|lifemart|aflac|blue light card|"
        r"benefits@work|bhn( rewards)?( platform)?|perkbox|cycle2work|"
        r"sharesave|401\s?\(?k\)?|employee assistance program(me)?|eap)$")),
    ("software per candidarsi", re.compile(
        r"(?i)^(google chrome|chrome|mozilla firefox|firefox|safari|microsoft edge|edge|"
        r"myworkday|candidate home|taleo|icims portal)$")),
    ("legge o regime di approvazione", re.compile(
        r"(?i)^(gdpr|hipaa|sox|ccpa|osha|obra|clia|ncqa|fda|ce|ukca|"
        r"care act( \d+)?|mental capacity act( \d+)?|section \d+|"
        r"executive order \d+|directive \d+)$")),
    ("titolo di studio", re.compile(
        r"(?i)^(ged|high school diploma|bachelor'?s?( degree)?|master'?s?( degree)?|"
        r"laurea.*|diploma.*|bac( pro)?|cap|bep|nvq( level \d)?)$")),
    ("lingua naturale", re.compile(
        r"(?i)^(english|inglese|italian|italiano|french|français|francese|german|"
        r"deutsch|tedesco|spanish|español|spagnolo|swedish|svenska|svedese|"
        r"dutch|nederlands|olandese|portuguese|português|polish|polski)$")),
)


def filtra_rubrica(voci) -> tuple[list[str], list[tuple[str, str]]]:
    """Torna (tenute, [(voce, ragione), ...])."""
    tenute, tolte = [], []
    for v in voci:
        s = str(v).strip().strip(" .,;:()[]")
        if not s:
            continue
        for ragione, patt in REGOLE:
            if patt.match(s):
                tolte.append((s, ragione))
                break
        else:
            tenute.append(s)
    return tenute, tolte


if __name__ == "__main__":
    prova = ["Excel", "BLS", "Wagestream", "Google Chrome", "GDPR", "Bachelor's degree",
             "MIG Welding", "LGV", "Notifier", "English", "AED", "Python"]
    tenute, tolte = filtra_rubrica(prova)
    print("tenute:", tenute)
    for v, r in tolte:
        print(f"  tolta  {v:<22} {r}")
