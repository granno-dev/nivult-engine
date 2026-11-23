#!/usr/bin/env python3
"""Decifra un backup CMS grande senza caricarlo tutto in memoria.

OpenSSL non decifra in streaming i CMS prodotti con `cms -encrypt -stream`:
https://github.com/openssl/openssl/issues/26274

Questo lettore accetta soltanto il formato di deploy/backup.sh e
deploy/backup-n5.sh: envelopedData, destinatari RSA, AES-256-CBC e contenuto
indefinito composto da OCTET STRING. RSA e AES restano a OpenSSL/cryptography;
qui si decodifica soltanto il contenitore DER. Il risultato va su stdout.
"""
from __future__ import annotations

import argparse
import io
import os
import shutil
import subprocess
import sys
from typing import BinaryIO

from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

OID_ENVELOPED_DATA = bytes.fromhex("2a864886f70d010703")
OID_DATA = bytes.fromhex("2a864886f70d010701")
OID_RSA = bytes.fromhex("2a864886f70d010101")
OID_AES_256_CBC = bytes.fromhex("60864801650304012a")
MASSIMO_CAMPO = 16 * 1024 * 1024


def _leggi(f: BinaryIO, n: int) -> bytes:
    dati = f.read(n)
    if len(dati) != n:
        raise ValueError("file CMS troncato")
    return dati


def _campo(f: BinaryIO) -> tuple[int, int | None]:
    tag = _leggi(f, 1)[0]
    if tag & 0x1F == 0x1F:
        raise ValueError("tag DER multi-byte non supportato")
    primo = _leggi(f, 1)[0]
    if tag == 0 and primo == 0:
        return tag, 0
    if primo < 128:
        return tag, primo
    cifre = primo & 0x7F
    if cifre == 0:
        if not tag & 0x20:
            raise ValueError("lunghezza indefinita su un campo primitivo")
        return tag, None
    if cifre > 4:
        raise ValueError("lunghezza DER eccessiva")
    lunghezza = int.from_bytes(_leggi(f, cifre), "big")
    if lunghezza > MASSIMO_CAMPO:
        raise ValueError("campo DER oltre il limite di sicurezza")
    return tag, lunghezza


def _atteso(f: BinaryIO, tag: int, indefinito: bool | None = None) -> int | None:
    letto, lunghezza = _campo(f)
    if letto != tag or (indefinito is True and lunghezza is not None) \
            or (indefinito is False and lunghezza is None):
        raise ValueError(f"struttura CMS inattesa: tag {letto:#x}")
    return lunghezza


def _salta(f: BinaryIO) -> None:
    _, lunghezza = _campo(f)
    if lunghezza is None:
        raise ValueError("campo annidato indefinito non supportato")
    _leggi(f, lunghezza)


def _destinatari(dati: bytes) -> list[bytes]:
    f = io.BytesIO(dati)
    chiavi = []
    while f.tell() < len(dati):
        lunghezza = _atteso(f, 0x30, False)
        fine = f.tell() + lunghezza
        _salta(f)
        _salta(f)
        algoritmo = _leggi(f, _atteso(f, 0x30, False))
        with io.BytesIO(algoritmo) as a:
            oid = _leggi(a, _atteso(a, 0x06, False))
        if oid != OID_RSA:
            _salta(f)
            f.seek(fine)
            continue
        chiavi.append(_leggi(f, _atteso(f, 0x04, False)))
        while f.tell() < fine:
            _salta(f)
    return chiavi


def _algoritmo(dati: bytes) -> bytes:
    with io.BytesIO(dati) as f:
        if _leggi(f, _atteso(f, 0x06, False)) != OID_AES_256_CBC:
            raise ValueError("il backup non usa AES-256-CBC")
        iv = _leggi(f, _atteso(f, 0x04, False))
        if len(iv) != 16 or f.read(1):
            raise ValueError("IV o algoritmo CMS non valido")
        return iv


def _chiave_simmetrica(percorso: str, destinatari: list[bytes], openssl: str) -> bytes:
    for cifrata in destinatari:
        r = subprocess.run(
            [openssl, "pkeyutl", "-decrypt", "-inkey", percorso,
             "-pkeyopt", "rsa_padding_mode:pkcs1"],
            input=cifrata, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            check=False)
        if r.returncode == 0 and len(r.stdout) == 32:
            return r.stdout
    raise ValueError("nessun destinatario corrisponde alla chiave privata")


def decifra(ingresso: BinaryIO, uscita: BinaryIO, percorso_chiave: str,
            openssl: str) -> None:
    _atteso(ingresso, 0x30, True)
    if _leggi(ingresso, _atteso(ingresso, 0x06, False)) != OID_ENVELOPED_DATA:
        raise ValueError("non e' un CMS envelopedData")
    _atteso(ingresso, 0xA0, True)
    _atteso(ingresso, 0x30, True)
    _salta(ingresso)
    destinatari = _destinatari(_leggi(ingresso, _atteso(ingresso, 0x31, False)))
    _atteso(ingresso, 0x30, True)
    if _leggi(ingresso, _atteso(ingresso, 0x06, False)) != OID_DATA:
        raise ValueError("contenuto CMS inatteso")
    iv = _algoritmo(_leggi(ingresso, _atteso(ingresso, 0x30, False)))
    _atteso(ingresso, 0xA0, True)
    chiave = _chiave_simmetrica(percorso_chiave, destinatari, openssl)
    decifratore = Cipher(algorithms.AES(chiave), modes.CBC(iv)).decryptor()
    blocchi = 0
    pendente = b""
    while True:
        tag, lunghezza = _campo(ingresso)
        if tag == 0 and lunghezza == 0:
            break
        if tag != 0x04 or lunghezza is None or lunghezza % 16:
            raise ValueError("blocco CMS non allineato o non supportato")
        dati = decifratore.update(_leggi(ingresso, lunghezza))
        uscita.write(pendente)
        pendente = dati
        blocchi += 1
    if not blocchi:
        raise ValueError("contenuto CMS vuoto")
    coda = ingresso.read(65)
    if len(coda) > 64 or len(coda) < 6 or len(coda) % 2 or any(coda):
        raise ValueError("chiusura DER mancante o dati dopo il CMS")
    pendente += decifratore.finalize()
    if not pendente:
        raise ValueError("padding PKCS#7 assente")
    padding = pendente[-1]
    if not 1 <= padding <= 16 or pendente[-padding:] != bytes([padding]) * padding:
        raise ValueError("padding PKCS#7 non valido")
    uscita.write(pendente[:-padding])
    uscita.flush()


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--key", required=True, help="chiave privata PEM, mai copiata")
    ap.add_argument("--openssl", default=os.environ.get("OPENSSL")
                    or shutil.which("openssl") or "openssl")
    args = ap.parse_args(argv)
    try:
        decifra(sys.stdin.buffer, sys.stdout.buffer, args.key, args.openssl)
    except (ValueError, OSError) as exc:
        print(f"decifra_backup: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
