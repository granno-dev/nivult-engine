"""nivult-v0: il classificatore locale (famiglia + seniority).
Carica una volta, predice in millisecondi sulla CPU, e dice QUANTO e'
sicuro: sotto soglia, il chiamante scala all'LLM. Zero rete: la base
XLM-R e' ricostruita dalla config e i pesi arrivano tutti da pesi.pt."""
from __future__ import annotations
import json, torch, torch.nn as nn
from transformers import AutoTokenizer, AutoConfig, AutoModel

CARTELLA = "/opt/nivult/nivult-modello-v0"

class _Rete(nn.Module):
    def __init__(self, cfg, n_fam, n_sen):
        super().__init__()
        self.enc = AutoModel.from_config(cfg)
        h = cfg.hidden_size
        self.fam = nn.Linear(h, n_fam)
        self.sen = nn.Linear(h, n_sen)
    def forward(self, **b):
        out = self.enc(**b).last_hidden_state[:, 0]
        return self.fam(out), self.sen(out)

class ModelloLocale:
    def __init__(self, cartella: str = CARTELLA):
        meta = json.load(open(f"{cartella}/config.json"))
        self.famiglie = meta["famiglie"]
        self.seniority = meta["seniority"]
        self.tok = AutoTokenizer.from_pretrained(f"{cartella}/tokenizer")
        cfg = AutoConfig.from_pretrained(meta["base"])
        self.rete = _Rete(cfg, len(self.famiglie), len(self.seniority))
        sd = torch.load(f"{cartella}/pesi.pt", map_location="cpu")
        self.rete.load_state_dict(sd)
        self.rete.eval()
        torch.set_num_threads(2)   # convive coi demoni, non li affama

    @torch.no_grad()
    def predici(self, testi: list[str]):
        """[(famiglia, conf_fam, seniority, conf_sen), ...]"""
        enc = self.tok(testi, truncation=True, max_length=192,
                       padding=True, return_tensors="pt")
        lf, ls = self.rete(**enc)
        pf = torch.softmax(lf, -1); ps = torch.softmax(ls, -1)
        out = []
        for i in range(len(testi)):
            cf, if_ = pf[i].max(0); cs, is_ = ps[i].max(0)
            out.append((self.famiglie[if_], float(cf),
                        self.seniority[is_], float(cs)))
        return out
