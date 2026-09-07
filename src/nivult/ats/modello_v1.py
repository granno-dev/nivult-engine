"""nivult-v1: il classificatore locale a cinque teste (mmBERT-base).

Famiglia, seniority, contratto, remoto e lingue richieste in una passata,
sul N5 (GPU ROCm se c'e', altrimenti CPU). Carica una volta, predice a
lotti, e per ogni testa dice QUANTO e' sicuro: le soglie stanno in
`config-v1.json` (`soglie_95`: la confidenza sopra la quale l'esame a
mano ha misurato il 95% di precisione; `null` = nessuna soglia raggiunge
il 95%, e allora quella testa si usa solo come ripiego prudente).

La rete e' IDENTICA a quella di addestramento (`addestra_v1.py`, classe
Modello): media mascherata dei token, dropout, una Linear per testa.
Cambiarla qui senza riaddestrare = pesi che non si caricano, o peggio,
che si caricano e predicono a caso.

Il testo in ingresso deve essere formattato COME IN ADDESTRAMENTO:
`titolo | localita`, a capo, descrizione pulita e tagliata a 1.200
caratteri. Un formato diverso in inferenza e' un modello diverso.
"""
from __future__ import annotations

import html
import json
import os
import re

import torch
import torch.nn as nn

CARTELLA = os.environ.get("MODELLO_V1", "/opt/nivult/modelli/nivult-v1")
_TAG = re.compile(r"<[^>]+>")


def pulito(t: str | None) -> str:
    """Come `pulisci` di estrai_dataset_v1: descrizioni salvate come JSON
    ({"text": …}) sciolte, entita' due volte, via i tag, spazi normali."""
    t = t or ""
    s = t.lstrip()
    if s[:1] in "{[":
        try:
            d = json.loads(s)
            if isinstance(d, dict):
                t = str(d.get("text") or d.get("description") or d.get("descriptionPlain") or "")
                if not t:
                    t = " ".join(str(v) for v in d.values() if isinstance(v, str))
            elif isinstance(d, list):
                t = " ".join(str(x) for x in d if isinstance(x, str))
        except ValueError:
            pass
    t = html.unescape(html.unescape(t))
    return re.sub(r"\s+", " ", _TAG.sub(" ", t).replace("\xa0", " ")).strip()


def testo(title: str | None, location: str | None, descrizione: str | None) -> str:
    return f"{title or ''} | {location or ''}\n{pulito(descrizione)[:1200]}"


class _Rete(nn.Module):
    def __init__(self, cfg, teste: dict[str, list[str]], n_lingue: int):
        super().__init__()
        from transformers import AutoModel
        self.enc = AutoModel.from_config(cfg)
        h = self.enc.config.hidden_size
        self.drop = nn.Dropout(0.1)
        self.teste = nn.ModuleDict({t: nn.Linear(h, len(v)) for t, v in teste.items()})
        self.lingue = nn.Linear(h, n_lingue)

    def forward(self, input_ids, attention_mask):
        out = self.enc(input_ids=input_ids, attention_mask=attention_mask).last_hidden_state
        m = attention_mask.unsqueeze(-1).to(out.dtype)
        pooled = (out * m).sum(1) / m.sum(1).clamp(min=1)
        pooled = self.drop(pooled)
        return {t: head(pooled) for t, head in self.teste.items()} | {"lingue": self.lingue(pooled)}


class ModelloV1:
    def __init__(self, cartella: str = CARTELLA, device: str | None = None):
        from transformers import AutoConfig, AutoTokenizer
        meta = json.load(open(f"{cartella}/config-v1.json"))
        self.teste: dict[str, list[str]] = meta["teste"]
        self.lingue: list[str] = meta["lingue"]
        self.max_len = int(meta.get("max_len", 384))
        self.temperature = meta.get("temperature", {})
        self.soglie_95 = meta.get("soglie_95", {})
        self.tok = AutoTokenizer.from_pretrained(cartella)
        # la config della base: salvata accanto ai pesi la prima volta, cosi'
        # dopo non serve piu' la rete
        if os.path.exists(f"{cartella}/config.json"):
            cfg = AutoConfig.from_pretrained(cartella)
        else:
            cfg = AutoConfig.from_pretrained(meta["base"])
            try:
                cfg.save_pretrained(cartella)
            except OSError:
                pass
        self.rete = _Rete(cfg, self.teste, len(self.lingue))
        sd = torch.load(f"{cartella}/pesi.pt", map_location="cpu")
        self.rete.load_state_dict(sd)
        self.rete.eval()
        self.device = device or ("cuda" if torch.cuda.is_available() else "cpu")
        if self.device == "cuda":
            self.rete.half()
        self.rete.to(self.device)
        if self.device == "cpu":
            torch.set_num_threads(4)

    @torch.no_grad()
    def predici(self, testi: list[str]) -> list[dict]:
        """Per ogni testo: {testa: (etichetta, confidenza)} + 'lingue': [(cod, prob)]."""
        enc = self.tok(testi, truncation=True, max_length=self.max_len,
                       padding=True, return_tensors="pt").to(self.device)
        out = self.rete(enc["input_ids"], enc["attention_mask"])
        ris: list[dict] = [{} for _ in testi]
        for t, voc in self.teste.items():
            temp = float(self.temperature.get(t) or 1.0)
            p = torch.softmax(out[t].float() / temp, -1)
            conf, idx = p.max(-1)
            for i in range(len(testi)):
                ris[i][t] = (voc[int(idx[i])], float(conf[i]))
        pl = torch.sigmoid(out["lingue"].float())
        for i in range(len(testi)):
            ris[i]["lingue"] = [(cod, float(pl[i][k])) for k, cod in enumerate(self.lingue)]
        return ris
