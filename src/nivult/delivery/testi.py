"""I testi delle email, nelle nove lingue del sito.

Un dizionario per lingua, chiavi identiche: `t(locale)` ritorna quello
giusto, con l'inglese come ripiego per una lingua che non conosciamo — che
con il CHECK su users.locale non dovrebbe succedere mai, ma un'email deve
sempre poter partire.

Scritte a mano e non generate: sono poche, si leggono in un colpo d'occhio,
e un errore qui finisce nella casella di qualcuno. Le motivazioni delle
offerte NON stanno qui: quelle le genera GLM direttamente nella lingua
dell'utente (vedi la rubrica in matching/llm.py).

I mesi stanno qui e non in `locale.setlocale` perché quella dipende dai
locale installati sul sistema, e un server minimale ne ha pochi: un
dizionario non ha dipendenze e non ha sorprese.
"""

from __future__ import annotations

MESI = {
    "en": ["January", "February", "March", "April", "May", "June", "July",
           "August", "September", "October", "November", "December"],
    "it": ["gennaio", "febbraio", "marzo", "aprile", "maggio", "giugno",
           "luglio", "agosto", "settembre", "ottobre", "novembre", "dicembre"],
    "fr": ["janvier", "février", "mars", "avril", "mai", "juin", "juillet",
           "août", "septembre", "octobre", "novembre", "décembre"],
    "de": ["Januar", "Februar", "März", "April", "Mai", "Juni", "Juli",
           "August", "September", "Oktober", "November", "Dezember"],
    "es": ["enero", "febrero", "marzo", "abril", "mayo", "junio", "julio",
           "agosto", "septiembre", "octubre", "noviembre", "diciembre"],
    "pt": ["janeiro", "fevereiro", "março", "abril", "maio", "junho", "julho",
           "agosto", "setembro", "outubro", "novembro", "dezembro"],
    "nl": ["januari", "februari", "maart", "april", "mei", "juni", "juli",
           "augustus", "september", "oktober", "november", "december"],
    "pl": ["stycznia", "lutego", "marca", "kwietnia", "maja", "czerwca",
           "lipca", "sierpnia", "września", "października", "listopada",
           "grudnia"],
    "sv": ["januari", "februari", "mars", "april", "maj", "juni", "juli",
           "augusti", "september", "oktober", "november", "december"],
}

# Il nome della lingua per la rubrica di GLM: in inglese, perché la rubrica
# è in una lingua sola e chiede "one sentence in {language}".
LINGUA_PER_GLM = {
    "en": "English", "it": "Italian", "fr": "French", "de": "German",
    "es": "Spanish", "pt": "European Portuguese", "nl": "Dutch",
    "pl": "Polish", "sv": "Swedish",
}

TESTI: dict[str, dict[str, str]] = {
    "en": {
        "canale_ripiego_oggetto": 'Your digest is coming by email again',
        "canale_ripiego_testo": 'We could not deliver to Telegram ({motivo}), so your digest is going back to email. You can reconnect Telegram any time from your panel on nivult.com.',
        "oggetto_uno": "Nivult — 1 job for you ({data})",
        "oggetto_molte": "Nivult — {n} jobs for you ({data})",
        "digest_del": "Digest of {data}",
        "pubblicata": "posted {data}",
        "agenzia": "recruitment agency",
        "datore_non_dichiarato": "Employer not disclosed",
        "candidatura_diretta": "Direct application",
        "via": "Via {fonte}",
        "apri": "Open the posting",
        "aderenza": "Match",
        "piede": "You receive this digest because you subscribed to Nivult. "
                 "Manage your preferences at nivult.com.",
        "piede_testo": "You receive this digest because you subscribed to Nivult.\n"
                       "Manage your preferences at nivult.com",
        "ml_oggetto": "Your Nivult sign-in link",
        "ml_corpo": "Sign in to Nivult with this link (valid 15 minutes):",
        "ml_entra": "Sign in to Nivult",
        "ml_ignora": "The link is valid for 15 minutes. "
                     "If you did not request it, ignore this email.",
        "unita_mese": "/month", "unita_anno": "/year", "unita_ora": "/hour",
        "unita_giorno": "/day", "unita_settimana": "/week",
    },
    "it": {
        "canale_ripiego_oggetto": 'Il tuo digest torna via email',
        "canale_ripiego_testo": 'Non siamo riusciti a consegnare su Telegram ({motivo}), quindi il digest torna via email. Puoi ricollegare Telegram quando vuoi dal pannello su nivult.com.',
        "oggetto_uno": "Nivult — 1 offerta per te ({data})",
        "oggetto_molte": "Nivult — {n} offerte per te ({data})",
        "digest_del": "Digest del {data}",
        "pubblicata": "pubblicata il {data}",
        "agenzia": "agenzia di selezione",
        "datore_non_dichiarato": "Datore non dichiarato",
        "candidatura_diretta": "Candidatura diretta",
        "via": "Via {fonte}",
        "apri": "Apri l'offerta",
        "aderenza": "Aderenza",
        "piede": "Ricevi questo digest perché sei iscritto a Nivult. "
                 "Le preferenze si gestiscono su nivult.com.",
        "piede_testo": "Ricevi questo digest perché sei iscritto a Nivult.\n"
                       "Le preferenze si gestiscono su nivult.com",
        "ml_oggetto": "Il tuo accesso a Nivult",
        "ml_corpo": "Entra in Nivult con questo link (vale 15 minuti):",
        "ml_entra": "Entra in Nivult",
        "ml_ignora": "Il link vale 15 minuti. Se non l'hai chiesto tu, "
                     "ignora questa email.",
        "unita_mese": "/mese", "unita_anno": "/anno", "unita_ora": "/ora",
        "unita_giorno": "/giorno", "unita_settimana": "/settimana",
    },
    "fr": {
        "canale_ripiego_oggetto": 'Votre digest revient par e-mail',
        "canale_ripiego_testo": 'La livraison sur Telegram a échoué ({motivo}), votre digest repasse donc par e-mail. Vous pouvez reconnecter Telegram à tout moment depuis votre espace sur nivult.com.',
        "oggetto_uno": "Nivult — 1 offre pour vous ({data})",
        "oggetto_molte": "Nivult — {n} offres pour vous ({data})",
        "digest_del": "Digest du {data}",
        "pubblicata": "publiée le {data}",
        "agenzia": "cabinet de recrutement",
        "datore_non_dichiarato": "Employeur non communiqué",
        "candidatura_diretta": "Candidature directe",
        "via": "Via {fonte}",
        "apri": "Voir l'offre",
        "aderenza": "Adéquation",
        "piede": "Vous recevez ce digest car vous êtes inscrit à Nivult. "
                 "Gérez vos préférences sur nivult.com.",
        "piede_testo": "Vous recevez ce digest car vous êtes inscrit à Nivult.\n"
                       "Gérez vos préférences sur nivult.com",
        "ml_oggetto": "Votre lien de connexion Nivult",
        "ml_corpo": "Connectez-vous à Nivult avec ce lien (valable 15 minutes) :",
        "ml_entra": "Se connecter à Nivult",
        "ml_ignora": "Le lien est valable 15 minutes. Si vous ne l'avez pas "
                     "demandé, ignorez cet email.",
        "unita_mese": "/mois", "unita_anno": "/an", "unita_ora": "/heure",
        "unita_giorno": "/jour", "unita_settimana": "/semaine",
    },
    "de": {
        "canale_ripiego_oggetto": 'Dein Digest kommt wieder per E-Mail',
        "canale_ripiego_testo": 'Die Zustellung über Telegram hat nicht geklappt ({motivo}), daher kommt dein Digest wieder per E-Mail. Du kannst Telegram jederzeit in deinem Bereich auf nivult.com neu verbinden.',
        "oggetto_uno": "Nivult — 1 Job für dich ({data})",
        "oggetto_molte": "Nivult — {n} Jobs für dich ({data})",
        "digest_del": "Digest vom {data}",
        "pubblicata": "veröffentlicht am {data}",
        "agenzia": "Personalvermittlung",
        "datore_non_dichiarato": "Arbeitgeber nicht genannt",
        "candidatura_diretta": "Direktbewerbung",
        "via": "Über {fonte}",
        "apri": "Stelle ansehen",
        "aderenza": "Passung",
        "piede": "Du erhältst diesen Digest, weil du Nivult abonniert hast. "
                 "Einstellungen verwaltest du auf nivult.com.",
        "piede_testo": "Du erhältst diesen Digest, weil du Nivult abonniert hast.\n"
                       "Einstellungen verwaltest du auf nivult.com",
        "ml_oggetto": "Dein Nivult-Anmeldelink",
        "ml_corpo": "Melde dich bei Nivult mit diesem Link an (15 Minuten gültig):",
        "ml_entra": "Bei Nivult anmelden",
        "ml_ignora": "Der Link ist 15 Minuten gültig. Wenn du ihn nicht "
                     "angefordert hast, ignoriere diese E-Mail.",
        "unita_mese": "/Monat", "unita_anno": "/Jahr", "unita_ora": "/Stunde",
        "unita_giorno": "/Tag", "unita_settimana": "/Woche",
    },
    "es": {
        "canale_ripiego_oggetto": 'Tu resumen vuelve por correo',
        "canale_ripiego_testo": 'No pudimos entregarlo en Telegram ({motivo}), así que tu resumen vuelve por correo. Puedes reconectar Telegram cuando quieras desde tu panel en nivult.com.',
        "oggetto_uno": "Nivult — 1 oferta para ti ({data})",
        "oggetto_molte": "Nivult — {n} ofertas para ti ({data})",
        "digest_del": "Digest del {data}",
        "pubblicata": "publicada el {data}",
        "agenzia": "agencia de selección",
        "datore_non_dichiarato": "Empresa no indicada",
        "candidatura_diretta": "Candidatura directa",
        "via": "Vía {fonte}",
        "apri": "Ver la oferta",
        "aderenza": "Afinidad",
        "piede": "Recibes este digest porque estás suscrito a Nivult. "
                 "Gestiona tus preferencias en nivult.com.",
        "piede_testo": "Recibes este digest porque estás suscrito a Nivult.\n"
                       "Gestiona tus preferencias en nivult.com",
        "ml_oggetto": "Tu enlace de acceso a Nivult",
        "ml_corpo": "Entra en Nivult con este enlace (válido 15 minutos):",
        "ml_entra": "Entrar en Nivult",
        "ml_ignora": "El enlace es válido durante 15 minutos. Si no lo has "
                     "pedido tú, ignora este correo.",
        "unita_mese": "/mes", "unita_anno": "/año", "unita_ora": "/hora",
        "unita_giorno": "/día", "unita_settimana": "/semana",
    },
    "pt": {
        "canale_ripiego_oggetto": 'O teu resumo volta por email',
        "canale_ripiego_testo": 'Não conseguimos entregar no Telegram ({motivo}), por isso o teu resumo volta por email. Podes voltar a ligar o Telegram quando quiseres no teu painel em nivult.com.',
        "oggetto_uno": "Nivult — 1 vaga para si ({data})",
        "oggetto_molte": "Nivult — {n} vagas para si ({data})",
        "digest_del": "Digest de {data}",
        "pubblicata": "publicada a {data}",
        "agenzia": "agência de recrutamento",
        "datore_non_dichiarato": "Empregador não divulgado",
        "candidatura_diretta": "Candidatura direta",
        "via": "Via {fonte}",
        "apri": "Ver a vaga",
        "aderenza": "Adequação",
        "piede": "Recebe este digest porque está inscrito na Nivult. "
                 "Gira as suas preferências em nivult.com.",
        "piede_testo": "Recebe este digest porque está inscrito na Nivult.\n"
                       "Gira as suas preferências em nivult.com",
        "ml_oggetto": "O seu link de acesso à Nivult",
        "ml_corpo": "Entre na Nivult com este link (válido por 15 minutos):",
        "ml_entra": "Entrar na Nivult",
        "ml_ignora": "O link é válido por 15 minutos. Se não o pediu, "
                     "ignore este email.",
        "unita_mese": "/mês", "unita_anno": "/ano", "unita_ora": "/hora",
        "unita_giorno": "/dia", "unita_settimana": "/semana",
    },
    "nl": {
        "canale_ripiego_oggetto": 'Je digest komt weer per e-mail',
        "canale_ripiego_testo": 'Bezorgen via Telegram lukte niet ({motivo}), dus je digest gaat weer per e-mail. Je kunt Telegram op elk moment opnieuw koppelen in je paneel op nivult.com.',
        "oggetto_uno": "Nivult — 1 baan voor je ({data})",
        "oggetto_molte": "Nivult — {n} banen voor je ({data})",
        "digest_del": "Digest van {data}",
        "pubblicata": "geplaatst op {data}",
        "agenzia": "wervingsbureau",
        "datore_non_dichiarato": "Werkgever niet vermeld",
        "candidatura_diretta": "Rechtstreeks solliciteren",
        "via": "Via {fonte}",
        "apri": "Bekijk de vacature",
        "aderenza": "Match",
        "piede": "Je ontvangt deze digest omdat je bent aangemeld bij Nivult. "
                 "Beheer je voorkeuren op nivult.com.",
        "piede_testo": "Je ontvangt deze digest omdat je bent aangemeld bij Nivult.\n"
                       "Beheer je voorkeuren op nivult.com",
        "ml_oggetto": "Je Nivult-inloglink",
        "ml_corpo": "Log in bij Nivult met deze link (15 minuten geldig):",
        "ml_entra": "Inloggen bij Nivult",
        "ml_ignora": "De link is 15 minuten geldig. Heb je hem niet "
                     "aangevraagd, negeer dan deze e-mail.",
        "unita_mese": "/maand", "unita_anno": "/jaar", "unita_ora": "/uur",
        "unita_giorno": "/dag", "unita_settimana": "/week",
    },
    "pl": {
        "canale_ripiego_oggetto": 'Twój digest wraca na e-mail',
        "canale_ripiego_testo": 'Nie udało się dostarczyć przez Telegram ({motivo}), więc digest wraca na e-mail. Telegram możesz podłączyć ponownie w dowolnej chwili w panelu na nivult.com.',
        "oggetto_uno": "Nivult — 1 oferta dla Ciebie ({data})",
        "oggetto_molte": "Nivult — {n} ofert dla Ciebie ({data})",
        "oggetto_poche": "Nivult — {n} oferty dla Ciebie ({data})",
        "digest_del": "Digest z {data}",
        "pubblicata": "opublikowana {data}",
        "agenzia": "agencja rekrutacyjna",
        "datore_non_dichiarato": "Pracodawca nieujawniony",
        "candidatura_diretta": "Aplikacja bezpośrednia",
        "via": "Przez {fonte}",
        "apri": "Zobacz ofertę",
        "aderenza": "Dopasowanie",
        "piede": "Otrzymujesz ten digest, ponieważ masz subskrypcję Nivult. "
                 "Preferencje zmienisz na nivult.com.",
        "piede_testo": "Otrzymujesz ten digest, ponieważ masz subskrypcję Nivult.\n"
                       "Preferencje zmienisz na nivult.com",
        "ml_oggetto": "Twój link logowania do Nivult",
        "ml_corpo": "Zaloguj się do Nivult tym linkiem (ważny 15 minut):",
        "ml_entra": "Zaloguj się do Nivult",
        "ml_ignora": "Link jest ważny 15 minut. Jeśli to nie Ty go "
                     "zamawiałeś, zignoruj tę wiadomość.",
        "unita_mese": "/mies.", "unita_anno": "/rok", "unita_ora": "/godz.",
        "unita_giorno": "/dzień", "unita_settimana": "/tydz.",
    },
    "sv": {
        "canale_ripiego_oggetto": 'Din sammanfattning kommer via e-post igen',
        "canale_ripiego_testo": 'Vi kunde inte leverera via Telegram ({motivo}), så din sammanfattning går via e-post igen. Du kan koppla Telegram på nytt när du vill från din panel på nivult.com.',
        "oggetto_uno": "Nivult — 1 jobb till dig ({data})",
        "oggetto_molte": "Nivult — {n} jobb till dig ({data})",
        "digest_del": "Digest {data}",
        "pubblicata": "publicerad {data}",
        "agenzia": "rekryteringsbyrå",
        "datore_non_dichiarato": "Arbetsgivare ej angiven",
        "candidatura_diretta": "Ansök direkt",
        "via": "Via {fonte}",
        "apri": "Se annonsen",
        "aderenza": "Matchning",
        "piede": "Du får det här digestet eftersom du prenumererar på Nivult. "
                 "Hantera dina inställningar på nivult.com.",
        "piede_testo": "Du får det här digestet eftersom du prenumererar på Nivult.\n"
                       "Hantera dina inställningar på nivult.com",
        "ml_oggetto": "Din inloggningslänk till Nivult",
        "ml_corpo": "Logga in på Nivult med den här länken (giltig i 15 minuter):",
        "ml_entra": "Logga in på Nivult",
        "ml_ignora": "Länken är giltig i 15 minuter. Om du inte begärde den "
                     "kan du ignorera det här mejlet.",
        "unita_mese": "/mån", "unita_anno": "/år", "unita_ora": "/tim",
        "unita_giorno": "/dag", "unita_settimana": "/vecka",
    },
}


# `oggetto_poche` esiste solo dove la lingua lo distingue (il polacco):
# altrove ricade sul plurale normale, e il controllo delle chiavi lo sa.
for _d in TESTI.values():
    _d.setdefault("oggetto_poche", _d["oggetto_molte"])


def t(locale: str) -> dict[str, str]:
    return TESTI.get(locale, TESTI["en"])


def mesi(locale: str) -> list[str]:
    return MESI.get(locale, MESI["en"])
