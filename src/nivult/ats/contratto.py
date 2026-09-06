"""Il tipo di contratto nel NOSTRO vocabolario, qualunque cosa dica la
piattaforma. Misurato il 2026-09-06: 38.000 offerte attive portavano il
valore grezzo dell'ATS — «FullTime», «Tempo pieno», «FULL_TIME»,
«Permanent», «Working student» — e nessun filtro le trovava.

Regola della rubrica: si LEGGE, non si presume. «Permanent», «Regular»,
«Employee», «Tempo indeterminato» dicono che il posto e' stabile, non
quante ore: restano NULL invece di diventare full_time per abitudine.
"""
from __future__ import annotations

import re

VOCABOLARIO = ("full_time", "part_time", "contract", "temporary",
               "internship", "apprenticeship")

# (pattern sul valore grezzo normalizzato, valore nostro); il primo che
# combacia vince, quindi i piu' specifici stanno prima
_REGOLE = (
    (r"apprendist|apprentice|ausbildung|alternance|lehr", "apprenticeship"),
    (r"intern|stage|tirocin|praktik|trainee|working student|werkstudent|student", "internship"),
    (r"tempor|interim|somministr|zeitarbeit|befristet|fixed term|cdd|seasonal|saison|casual|per diem|prn|on call|aushilfe|side job|minijob", "temporary"),
    (r"contract|freelance|consulen|collaboraz|agente|rappresentante|selbst|independ|cons\b|partita iva|self.?employ", "contract"),
    (r"part|teilzeit|tempo parziale|parcial|mi-temps|deeltijd|deltid", "part_time"),
    (r"full|vollzeit|tempo pieno|completo|plein|voltijd|heltid|fulltime", "full_time"),
)
_RX = [(re.compile(p, re.I), v) for p, v in _REGOLE]


def normalizza(valore: str | None) -> str | None:
    """«FullTime» -> full_time, «Tempo pieno» -> full_time, «Permanent» -> None."""
    if not valore or not isinstance(valore, str):
        return None
    v = valore.strip()
    if v in VOCABOLARIO:
        return v
    t = re.sub(r"[_\-]+", " ", v).lower()
    for rx, nostro in _RX:
        if rx.search(t):
            return nostro
    return None


if __name__ == "__main__":
    for x in ("FullTime", "Tempo pieno", "FULL_TIME", "Permanent", "Part time",
              "PartTime", "Tempo indeterminato", "OTHER", "Intern", "Employee",
              "Permanent Full Time", "Stage", "Apprendistato", "Working student",
              "Collaborazione/consulenza", "Agente/rappresentante", "Parttime-Temporary",
              "['TEMPORARY', 'FULL_TIME']", "Fixed Term", "Contractor Full-Time", "CONS",
              "Side job", "Regular", None):
        print(f"{x!r:32s} -> {normalizza(x)}")
