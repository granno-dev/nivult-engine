"""Quale alternativa della lista blocca besix.com? Un filtro che scarta aziende
vere costa copertura in silenzio, ed e' peggio di uno che ne fa passare qualcuna."""
import importlib.util, re
spec = importlib.util.spec_from_file_location("cd", "/opt/nivult/caccia_domini.py")
m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)

pat = m.NON_AZIENDA.pattern
# si smonta la regex nelle sue alternative e si prova una per una
alt = re.split(r"\|", pat.replace("(?:", "").replace(")", "").replace("(", ""))
for d in ("besix.com", "cvshealth.com", "drmax.eu", "broadcom.com", "sthree.com", "theodo.com"):
    colpevoli = [a for a in alt if a and re.search(a, d, re.I)]
    print(f"  {d:<20}{'BLOCCATO da: ' + ', '.join(colpevoli) if colpevoli else 'passa'}")
