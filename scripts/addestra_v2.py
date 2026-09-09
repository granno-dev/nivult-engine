#!/usr/bin/env python3
"""nivult-v2: fine-tuning LoRA a 16 bit di un Qwen sul dataset SFT, con Unsloth.

    python addestra_v2.py --modello Qwen/Qwen3.5-9B --train sft-train.jsonl.gz \\
        --out /content/drive/MyDrive/nivult-v2/9b --epoche 2 --bs 8 --max-len 2048

Stesse regole di addestra_v1: il checkpoint porta la FIRMA del dataset e, se
non coincide, si mette da parte e si riparte da zero (e' cosi' che l'anteprima
di v1 era stata spacciata per addestramento); il rapporto dice `passi_previsti`,
`passi_fatti` e `addestramento_completo`. Alla fine salva SOLO gli adattatori
LoRA (pochi MB) piu' il tokenizer: la fusione e la quantizzazione in GGUF per
il N5 le fa `esporta_v2.sh` dopo l'esame.

Il modello: Qwen3.5-9B (denso, sta largo in una H100) oppure Qwen3.6-35B-A3B
(MoE: LoRA 16 bit in ~74 GB, senza addestrare il router). Unsloth sconsiglia
QLoRA a 4 bit sui Qwen 3.5+: qui i pesi restano a 16 bit.
"""
from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import os
import time


def firma_dataset(percorso: str) -> str:
    h = hashlib.sha1()
    n = 0
    with gzip.open(percorso, "rb") as f:
        while True:
            b = f.read(1 << 20)
            if not b:
                break
            if n == 0:
                h.update(b)
            n += b.count(b"\n")
    return f"{n}:{h.hexdigest()[:12]}"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--modello", default="Qwen/Qwen3.5-9B")
    ap.add_argument("--train", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--epoche", type=float, default=2.0)
    ap.add_argument("--bs", type=int, default=8)
    ap.add_argument("--accumulo", type=int, default=2)
    ap.add_argument("--lr", type=float, default=1e-4)
    ap.add_argument("--max-len", type=int, default=2048)
    ap.add_argument("--rango", type=int, default=32)
    ap.add_argument("--max-righe", type=int, default=None)
    a = ap.parse_args()
    os.makedirs(a.out, exist_ok=True)

    from unsloth import FastLanguageModel
    from datasets import Dataset
    from trl import SFTTrainer, SFTConfig

    firma = firma_dataset(a.train)
    ckpt_dir = os.path.join(a.out, "checkpoint")
    meta_path = os.path.join(a.out, "meta.json")
    riprendi = None

    def _ultimo_checkpoint(cartella: str) -> str | None:
        """L'ultimo checkpoint-N COMPLETO (con trainer_state.json), o None."""
        if not os.path.isdir(cartella):
            return None
        cand = []
        for nome in os.listdir(cartella):
            if nome.startswith("checkpoint-") and nome.split("-")[-1].isdigit() \
                    and os.path.exists(os.path.join(cartella, nome, "trainer_state.json")):
                cand.append((int(nome.split("-")[-1]), os.path.join(cartella, nome)))
        return max(cand)[1] if cand else None

    if os.path.exists(meta_path) and os.path.isdir(ckpt_dir):
        meta = json.load(open(meta_path))
        if meta.get("firma") == firma and meta.get("modello") == a.modello:
            riprendi = _ultimo_checkpoint(ckpt_dir)
            print("riprendo dal checkpoint", riprendi or "(nessuno completo: da zero)", flush=True)
        else:
            print(f"CHECKPOINT DI UN ALTRO DATASET/MODELLO ({meta.get('firma')} vs {firma}): si parte da zero", flush=True)
            os.rename(ckpt_dir, ckpt_dir + ".altro-dataset-" + str(int(time.time())))
    # il rapporto di un giro precedente non deve mai passare per quello di questo
    for vecchio in ("rapporto-addestramento.json",):
        if os.path.exists(os.path.join(a.out, vecchio)):
            os.remove(os.path.join(a.out, vecchio))

    model, tok = FastLanguageModel.from_pretrained(model_name=a.modello, max_seq_length=a.max_len,
                                                   load_in_4bit=False, dtype=None)
    model = FastLanguageModel.get_peft_model(
        model, r=a.rango, lora_alpha=a.rango, lora_dropout=0.0, bias="none",
        target_modules=["q_proj", "k_proj", "v_proj", "o_proj", "gate_proj", "up_proj", "down_proj"],
        use_gradient_checkpointing="unsloth", random_state=7)

    righe = []
    with gzip.open(a.train, "rt") as f:
        for l in f:
            righe.append(json.loads(l)["messages"])
            if a.max_righe and len(righe) >= a.max_righe:
                break
    print("righe di addestramento:", len(righe), "firma", firma, flush=True)
    ds = Dataset.from_dict({"messages": righe}).shuffle(seed=7)

    # La PERDITA SI CALCOLA SOLO SULLA RISPOSTA. Il primo addestramento
    # (08-09/09/2026) passava l'intera conversazione come «text»: la
    # risposta JSON era il 3,8% dei caratteri e il 96% del gradiente andava
    # a imparare a scrivere annunci. Esito: perdita ferma a 0,57, famiglia
    # al 67,5% sulle 280 a mano contro il 90% di v1. Con un dataset
    # prompt/completion trl maschera il prompt e conta solo la risposta.
    # Il prompt e' ESATTAMENTE la stringa che l'esame passa al modello
    # (add_generation_prompt=True, enable_thinking=False): il completion e'
    # cio' che il template aggiunge dopo, calcolato per differenza cosi'
    # da non dipendere da come il template scrive il turno dell'assistente.
    def formatta(batch):
        prompts, completions = [], []
        for m in batch["messages"]:
            kw = {"tokenize": False}
            try:
                pieno = tok.apply_chat_template(m, add_generation_prompt=False, enable_thinking=False, **kw)
                prompt = tok.apply_chat_template(m[:-1], add_generation_prompt=True, enable_thinking=False, **kw)
            except TypeError:   # template senza il parametro enable_thinking
                pieno = tok.apply_chat_template(m, add_generation_prompt=False, **kw)
                prompt = tok.apply_chat_template(m[:-1], add_generation_prompt=True, **kw)
            if not pieno.startswith(prompt):
                raise SystemExit("il template non e' un prefisso di se stesso: prompt e completion non separabili")
            prompts.append(prompt)
            completions.append(pieno[len(prompt):].rstrip("\n"))
        return {"prompt": prompts, "completion": completions}
    ds = ds.map(formatta, batched=True, remove_columns=["messages"])
    es = ds[0]
    print("ESEMPIO prompt (coda):", repr(es["prompt"][-160:]), flush=True)
    print("ESEMPIO completion:", repr(es["completion"][:200]), flush=True)

    passi_per_epoca = len(ds) // (a.bs * a.accumulo)
    passi_previsti = int(passi_per_epoca * a.epoche)
    json.dump({"firma": firma, "modello": a.modello, "passi_previsti": passi_previsti, "righe": len(ds)},
              open(meta_path, "w"))
    base = dict(output_dir=ckpt_dir, per_device_train_batch_size=a.bs, gradient_accumulation_steps=a.accumulo,
                num_train_epochs=a.epoche, learning_rate=a.lr, lr_scheduler_type="cosine", warmup_ratio=0.03,
                logging_steps=25, save_steps=500, save_total_limit=2, bf16=True, optim="adamw_8bit",
                completion_only_loss=True, packing=False, report_to="none", seed=7)
    # packing=False: su questo modello (processor-based) trl lo ignorava
    # comunque, e con la perdita sul solo completion e' meglio esplicito.
    # trl cambia nome ai parametri fra versioni: si prova il nuovo, poi il vecchio
    try:
        cfg = SFTConfig(max_length=a.max_len, **base)
    except TypeError:
        cfg = SFTConfig(max_seq_length=a.max_len, **base)
    try:
        trainer = SFTTrainer(model=model, processing_class=tok, train_dataset=ds, args=cfg)
    except TypeError:
        trainer = SFTTrainer(model=model, tokenizer=tok, train_dataset=ds, args=cfg)
    t0 = time.time()
    esito = trainer.train(resume_from_checkpoint=riprendi)
    passi_fatti = int(trainer.state.global_step)
    completo = passi_fatti >= 0.9 * passi_previsti
    model.save_pretrained(os.path.join(a.out, "lora"))
    tok.save_pretrained(os.path.join(a.out, "lora"))
    rapporto = {"modello": a.modello, "firma_dataset": firma, "righe": len(ds), "passi_previsti": passi_previsti,
                "passi_fatti": passi_fatti, "addestramento_completo": completo,
                "perdita_finale": float(esito.training_loss) if esito else None, "ore": round((time.time() - t0) / 3600, 2)}
    json.dump(rapporto, open(os.path.join(a.out, "rapporto-addestramento.json"), "w"), indent=1)
    print("RAPPORTO:", json.dumps(rapporto), flush=True)
    if not completo:
        print("ATTENZIONE: addestramento NON completo, non fare l'esame come se lo fosse", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
