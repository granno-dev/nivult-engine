"""Object storage S3 per i CV cifrati: byte opachi, dentro e fuori.

Il bucket è di Hetzner (stesso datacenter del database): l'endpoint è
compatibile S3 e si parla con boto3. Le chiavi degli oggetti sono
user_cvs.storage_key; la cancellazione GDPR raccoglie quelle chiavi prima di
cancellare le righe, e le rimuove da qui.
"""

from __future__ import annotations

import os
from functools import lru_cache

import boto3
from botocore.client import Config


@lru_cache(maxsize=1)
def _client():
    endpoint = os.environ.get("S3_ENDPOINT")
    access = os.environ.get("S3_ACCESS_KEY")
    secret = os.environ.get("S3_SECRET_KEY")
    if not (endpoint and access and secret):
        raise RuntimeError("Object storage non configurato: servono S3_ENDPOINT, "
                           "S3_ACCESS_KEY e S3_SECRET_KEY nell'ambiente")
    return boto3.client(
        "s3", endpoint_url=endpoint, aws_access_key_id=access,
        aws_secret_access_key=secret,
        # Il nostro traffico è fatto di file piccoli e operazioni rare:
        # niente ricerche, niente multipart.
        config=Config(signature_version="s3v4", retries={"max_attempts": 3}))


def bucket() -> str:
    return os.environ.get("S3_BUCKET", "nivult-cvs")


def salva(chiave: str, dati: bytes) -> None:
    _client().put_object(Bucket=bucket(), Key=chiave, Body=dati)


def leggi(chiave: str) -> bytes:
    r = _client().get_object(Bucket=bucket(), Key=chiave)
    return r["Body"].read()


def elimina(chiave: str) -> None:
    _client().delete_object(Bucket=bucket(), Key=chiave)


def esiste(chiave: str) -> bool:
    try:
        _client().head_object(Bucket=bucket(), Key=chiave)
        return True
    except _client().exceptions.ClientError:
        return False
