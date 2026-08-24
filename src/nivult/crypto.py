"""Cifratura a busta per i CV: AES-256-GCM, DEK per file, KEK nell'ambiente.

Ogni CV ha la sua chiave di dati (DEK) casuale; la DEK viene avvolta con la
chiave master (KEK) che vive solo in variabile d'ambiente sul server. Hetzner
riceve byte opachi: non ha né la KEK né le DEK, e non può leggere nulla.

Il motivo della busta è la rotazione: cambiare KEK significa riavvolgere DEK
di poche decine di byte, non riscaricare e ricifrare ogni CV. kek_version in
user_cvs dice con quale generazione è avvolta ciascuna DEK, così la rotazione
sarà incrementale invece che atomica.

Il contratto con lo schema della 0009:
  nonce        12 byte  — il nonce GCM dei DATI
  auth_tag     16 byte  — il tag GCM dei DATI (i dati cifrati lo portano
                          appeso: lì sta la verità, qui la fotografia)
  encrypted_dek >= 32   — nonce di avvolgimento (12) + DEK cifrata e firmata
                          (32 + 16 = 48): totale 60 byte
"""

from __future__ import annotations

import os
from dataclasses import dataclass

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

DEK_BYTES = 32      # AES-256
NONCE_BYTES = 12    # GCM standard
TAG_BYTES = 16


class BustaCifrata(bytes):
    """I dati cifrati: nonce || ciphertext || tag, autosufficienti."""


@dataclass(slots=True)
class EsitoCifratura:
    dati: BustaCifrata          # da mandare all'object storage, così com'è
    encrypted_dek: bytes        # da mettere in user_cvs
    nonce: bytes                # da mettere in user_cvs
    auth_tag: bytes             # da mettere in user_cvs


def _kek() -> bytes:
    kek = os.environ.get("CV_KEK", "")
    try:
        raw = bytes.fromhex(kek)
    except ValueError:
        raw = b""
    if len(raw) != DEK_BYTES:
        raise RuntimeError("CV_KEK assente o malformata: servono 64 caratteri esadecimali "
                           "(la chiave non entra mai nel database, solo nell'ambiente)")
    return raw


def cifra(testo: bytes) -> EsitoCifratura:
    kek = _kek()
    dek = os.urandom(DEK_BYTES)
    nonce = os.urandom(NONCE_BYTES)
    ct = AESGCM(dek).encrypt(nonce, testo, None)          # ciphertext || tag
    # La DEK si avvolge a sua volta: nonce di avvolgimento in testa, così il
    # valore è autosufficiente e lo schema non ha bisogno di una colonna in più.
    wrap_nonce = os.urandom(NONCE_BYTES)
    encrypted_dek = wrap_nonce + AESGCM(kek).encrypt(wrap_nonce, dek, None)
    return EsitoCifratura(BustaCifrata(nonce + ct), encrypted_dek,
                          nonce, ct[-TAG_BYTES:])


def decifra(dati: BustaCifrata | bytes, encrypted_dek: bytes) -> bytes:
    kek = _kek()
    wrap_nonce, dek_ct = encrypted_dek[:NONCE_BYTES], encrypted_dek[NONCE_BYTES:]
    dek = AESGCM(kek).decrypt(wrap_nonce, dek_ct, None)
    nonce, ct = dati[:NONCE_BYTES], dati[NONCE_BYTES:]
    return AESGCM(dek).decrypt(nonce, ct, None)
