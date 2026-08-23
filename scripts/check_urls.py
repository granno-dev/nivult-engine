#!/usr/bin/env python3
"""Verifica della canonicalizzazione degli URL e della classificazione dei link.

    python scripts/check_urls.py

Non tocca il database e non usa la rete: è pura logica, e va provata da sola.
Qui si decide la deduplica dura — `canonical_url` è UNIQUE, quindi ogni errore
in queste funzioni o fonde offerte diverse o duplica la stessa.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from nivult.ingestion.urls import (  # noqa: E402
    canonicalize, classify_link, normalize_title, registrable_domain,
)

PASSED: list[str] = []
FAILED: list[str] = []


def eq(label: str, got, expected) -> None:
    if got == expected:
        PASSED.append(label); print(f"  ok    {label}")
    else:
        FAILED.append(label); print(f"  FAIL  {label}\n          atteso: {expected!r}\n          ottenuto: {got!r}")


def same(label: str, a: str, b: str) -> None:
    ca, cb = canonicalize(a), canonicalize(b)
    if ca == cb:
        PASSED.append(label); print(f"  ok    {label}  -> {ca}")
    else:
        FAILED.append(label); print(f"  FAIL  {label}\n          {ca}\n          {cb}")


def differ(label: str, a: str, b: str) -> None:
    ca, cb = canonicalize(a), canonicalize(b)
    if ca != cb:
        PASSED.append(label); print(f"  ok    {label}")
    else:
        FAILED.append(label); print(f"  FAIL  {label} — collidono entrambi su {ca}")


def raises(label: str, url: str) -> None:
    try:
        canonicalize(url)
    except ValueError:
        PASSED.append(label); print(f"  ok    {label}")
        return
    FAILED.append(label); print(f"  FAIL  {label} — accettato invece di essere rifiutato")


def main() -> int:
    print("canonicalizzazione — cose che DEVONO coincidere")
    same("schema e host in maiuscolo", "HTTPS://Acme.Example/Jobs/1", "https://acme.example/Jobs/1")
    same("www o non www", "https://www.acme.example/jobs/1", "https://acme.example/jobs/1")
    same("slash finale", "https://acme.example/jobs/1/", "https://acme.example/jobs/1")
    same("fragment", "https://acme.example/jobs/1#apply", "https://acme.example/jobs/1")
    same("porta esplicita di default", "https://acme.example:443/jobs/1", "https://acme.example/jobs/1")
    same("parametri utm", "https://acme.example/jobs/1?utm_source=x&utm_campaign=y",
         "https://acme.example/jobs/1")
    same("gclid e fbclid", "https://acme.example/jobs/1?gclid=abc&fbclid=def",
         "https://acme.example/jobs/1")
    same("ordine dei parametri", "https://acme.example/jobs?b=2&a=1",
         "https://acme.example/jobs?a=1&b=2")
    same("misto: tracciamento più parametro utile",
         "https://acme.example/j?jobId=77&utm_source=li", "https://acme.example/j?jobId=77")

    print("\ncanonicalizzazione — cose che NON devono coincidere")
    # Il motivo della denylist invece della allowlist: se buttassimo tutti i
    # parametri, queste due offerte diverse diventerebbero la stessa riga.
    differ("due offerte distinte dallo stesso parametro di query",
           "https://acme.example/apply?jobId=77", "https://acme.example/apply?jobId=88")
    differ("Greenhouse: offerte distinte da gh_jid",
           "https://boards.acme.example/x?gh_jid=1", "https://boards.acme.example/x?gh_jid=2")
    differ("path diversi", "https://acme.example/jobs/1", "https://acme.example/jobs/2")
    differ("host diversi", "https://acme.example/j/1", "https://other.example/j/1")
    differ("porta non standard", "https://acme.example:8443/j", "https://acme.example/j")

    print("\ncanonicalizzazione — input da rifiutare")
    raises("stringa vuota", "")
    raises("solo spazi", "   ")
    raises("schema mancante", "acme.example/jobs/1")
    raises("mailto", "mailto:jobs@acme.example")
    raises("javascript", "javascript:alert(1)")
    raises("host assente", "https:///jobs/1")

    print("\nclassificazione del link")
    eq("career site sconosciuto -> career_site",
       classify_link("https://careers.acme.example/j/1"), "career_site")
    eq("France Travail -> national_agency",
       classify_link("https://candidat.francetravail.fr/offres/recherche/detail/123"),
       "national_agency")
    eq("Bundesagentur -> national_agency",
       classify_link("https://www.arbeitsagentur.de/jobsuche/jobdetail/abc"), "national_agency")
    eq("sottodominio di un'agenzia -> national_agency",
       classify_link("https://jobboerse.arbeitsagentur.de/x"), "national_agency")
    eq("LinkedIn -> job_board", classify_link("https://www.linkedin.com/jobs/view/1"), "job_board")
    eq("Indeed -> job_board", classify_link("https://it.indeed.com/viewjob?jk=1"), "job_board")
    # Un dominio che CONTIENE il nome di un aggregatore non è quell'aggregatore.
    eq("non-linkedin.example non è LinkedIn",
       classify_link("https://not-linkedin.example/jobs/1"), "career_site")
    eq("linkedin.example.com non è LinkedIn",
       classify_link("https://linkedin.example.com/jobs/1"), "career_site")

    print("\nsupporto")
    eq("dominio registrabile", registrable_domain("https://www.Acme.Example/x"), "acme.example")
    eq("titolo normalizzato", normalize_title("  Senior   HR   Manager \n"), "senior hr manager")

    print(f"\n{len(PASSED)} superati, {len(FAILED)} falliti")
    if FAILED:
        return 1
    print("OK: la deduplica dura si comporta come deve")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
