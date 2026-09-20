#!/bin/bash
# Ferma TUTTO quello della testa tecnologie sul Mac mini, e ne riavvia UNO.
#
# Il 20/09 ho lasciato tre cicli in piedi senza accorgermene: uccidevo il ciclo
# bash ma non il python figlio, che restava vivo col suo modello in memoria,
# e il riavvio ne aggiungeva un altro. Tre modelli da 2,5 GB su una macchina da
# 8: lo swap e' salito a 6,3 GB e il Mac ha cominciato ad annaspare.
#
# Qui si uccidono prima i figli e poi i padri, si VERIFICA che non resti niente,
# e solo allora si riavvia. Il motivo di ricerca e' spezzato perche' altrimenti
# troverebbe questo stesso script.
set -u
cd "$HOME/nivult" || exit 1

conta() { pgrep -f "tec_v1""_demone.py|nivult-mini""-tec.sh" 2>/dev/null | wc -l | tr -d " "; }

echo "prima: $(conta) processi"
for giro in 1 2 3; do
  vivi=$(pgrep -f "tec_v1""_demone.py" 2>/dev/null; pgrep -f "nivult-mini""-tec.sh" 2>/dev/null)
  [ -z "$vivi" ] && break
  # prima i python (i figli), poi i bash (i padri): al contrario il padre
  # rilancia il figlio mentre lo stiamo uccidendo
  for p in $(pgrep -f "tec_v1""_demone.py" 2>/dev/null); do kill "$p" 2>/dev/null; done
  sleep 2
  for p in $(pgrep -f "nivult-mini""-tec.sh" 2>/dev/null); do kill "$p" 2>/dev/null; done
  sleep 2
  [ "$giro" = 3 ] && {
    for p in $(pgrep -f "tec_v1""_demone.py|nivult-mini""-tec.sh" 2>/dev/null); do
      kill -9 "$p" 2>/dev/null
    done
  }
done
sleep 2
resta=$(conta)
echo "dopo lo spegnimento: $resta processi"
[ "$resta" != "0" ] && { echo "NON e' pulito, non riavvio: guarda a mano"; exit 1; }

vm_stat | awk '/Pages free/{f=$3} /Pages inactive/{i=$3} END{printf "memoria libera: %.1f GB\n", (f+i)*16384/1073741824}'
sysctl -n vm.swapusage

nohup bash "$HOME/nivult/nivult-mini-tec.sh" > /dev/null 2>&1 &
sleep 3
echo "riavviato: $(conta) processi (attesi 2: il ciclo e il suo python)"
