#!/usr/bin/env python3
"""Prova il lettore dei backup grandi con CMS sintetici, senza dati reali."""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _openssl_con_stream() -> str:
    configurato = os.environ.get("OPENSSL")
    candidati = [configurato] if configurato else [
        shutil.which("openssl"), "/opt/homebrew/bin/openssl",
        "/usr/local/bin/openssl"]
    visti = set()
    for candidato in candidati:
        if not candidato or candidato in visti:
            continue
        visti.add(candidato)
        try:
            r = subprocess.run([candidato, "cms", "-help"],
                               capture_output=True, text=True, check=False)
        except OSError:
            continue
        if "-stream" in r.stdout + r.stderr:
            return candidato
    raise SystemExit("Serve OpenSSL con supporto CMS -stream; "
                     "imposta OPENSSL al percorso corretto")


OPENSSL = _openssl_con_stream()


def _esegui(args: list[str], **kwargs) -> subprocess.CompletedProcess:
    return subprocess.run(args, capture_output=True, check=True, **kwargs)


def main() -> int:
    with tempfile.TemporaryDirectory(prefix="nivult-backup-stream-") as tmp:
        base = Path(tmp)
        key = base / "chiave.pem"
        cert = base / "certificato.pem"
        _esegui([OPENSSL, "req", "-x509", "-newkey", "rsa:2048", "-nodes",
                 "-keyout", str(key), "-out", str(cert), "-days", "1",
                 "-subj", "/CN=nivult-test-backup"])
        for nome, originale in (
                ("vuoto", b""),
                ("blocchi", b"BEGIN;\n" + os.urandom(100_003)
                 + b"\x00\r\nCOMMIT;\n")):
            sorgente = base / f"{nome}.bin"
            cifrato = base / f"{nome}.cms"
            sorgente.write_bytes(originale)
            _esegui([OPENSSL, "cms", "-encrypt", "-aes-256-cbc", "-binary",
                     "-stream", "-outform", "DER", "-recip", str(cert),
                     "-in", str(sorgente), "-out", str(cifrato)])
            comando = [sys.executable, str(ROOT / "scripts" / "decifra_backup.py"),
                       "--key", str(key), "--openssl", OPENSSL]
            r = subprocess.run(comando, input=cifrato.read_bytes(),
                               capture_output=True, check=False, timeout=60)
            if r.returncode or r.stdout != originale:
                print(f"FAIL decifratura {nome}: exit={r.returncode}, "
                      f"byte={len(r.stdout)}/{len(originale)}")
                return 1
            print(f"  ok    CMS {nome}: decifratura fedele")
            r = subprocess.run(comando, input=cifrato.read_bytes()[:-5],
                               capture_output=True, check=False, timeout=60)
            if r.returncode == 0:
                print(f"FAIL CMS {nome} troncato accettato")
                return 1
            print(f"  ok    CMS {nome} troncato rifiutato")
    print("4 controlli ok, 0 falliti")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
