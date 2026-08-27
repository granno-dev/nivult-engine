"""Il catalogo completo delle piattaforme ATS europee.

Questo è il fondamento: senza sapere QUALI sono, COME identificarle dagli URL
e COME estrarre lo slug dell'azienda, non possiamo né costruire l'indice né
gli adapter. Ogni piattaforma ha:

  id                il nome interno ('greenhouse', 'workday', …)
  name              il nome commerciale
  url_pattern       il pattern che identifica un URL di questa piattaforma
  slug_extraction   come estrarre lo slug dell'azienda dall'URL
  api_type          'json_get', 'json_post', 'html', 'unknown'
  api_endpoint      l'endpoint per leggere le offerte (se noto)
  cc_search         il pattern da cercare in Common Crawl per scoprire aziende
  market            dove è usato principalmente
  priority          1=critico, 2=importante, 3=nice-to-have

Per aggiungere una nuova piattaforma basta aggiungerla qui e il sistema
la conosce: discovery, eventualmente adapter, tutto parte da qui.
"""

# Il catalogo. Ordinato per priorità: prima le piattaforme con più aziende
# europee e API facili, poi quelle difficili.
REGISTRY = [
    # ── PIATTAFORME CON API JSON PUBBLICA (facili, subito utilizzabili) ──
    {
        "id": "greenhouse",
        "name": "Greenhouse",
        "url_pattern": r"job-boards\.(?:eu\.)?greenhouse\.io/([^/]+)",
        "api_type": "json_get",
        "api_endpoint": "https://boards-api.greenhouse.io/v1/boards/{slug}/jobs",
        "cc_search": "job-boards.greenhouse.io*",
        "market": "Tech, scale-up, global",
        "priority": 1,
    },
    {
        "id": "smartrecruiters",
        "name": "SmartRecruiters",
        "url_pattern": r"jobs\.smartrecruiters\.com/([^/]+)",
        "api_type": "json_get",
        "api_endpoint": "https://api.smartrecruiters.com/v1/companies/{slug}/postings",
        "cc_search": "jobs.smartrecruiters.com*",
        "market": "Grandi aziende, Europa e USA",
        "priority": 1,
    },
    {
        "id": "lever",
        "name": "Lever",
        "url_pattern": r"jobs\.lever\.co/([^/]+)",
        "api_type": "json_get",
        "api_endpoint": "https://api.lever.co/v0/postings/{slug}?mode=json",
        "cc_search": "jobs.lever.co*",
        "market": "Tech, startup",
        "priority": 1,
    },
    {
        "id": "recruitee",
        "name": "Recruitee",
        "url_pattern": r"https://([^/]+)\.recruitee\.com",
        "api_type": "json_get",
        "api_endpoint": "https://{slug}.recruitee.com/api/offers/",
        "cc_search": "*.recruitee.com*",
        "market": "PMI europee",
        "priority": 1,
    },
    {
        "id": "ashby",
        "name": "Ashby",
        "url_pattern": r"jobs\.ashbyhq\.com/([^/.]+)",
        "api_type": "json_get",
        "api_endpoint": "https://api.ashbyhq.com/posting-api/job-board/{slug}",
        "cc_search": "jobs.ashbyhq.com*",
        "market": "Startup europee",
        "priority": 1,
    },
    {
        "id": "workable",
        "name": "Workable",
        "url_pattern": r"apply\.workable\.com/([^/]+)",
        "api_type": "json_get",
        "api_endpoint": "https://{slug}.workable.com/api/v1/jobs",
        "cc_search": "apply.workable.com*",
        "market": "PMI, global",
        "priority": 1,
    },

    # ── PIATTAFORME ENTERPRISE (grandi aziende, API più complesse) ──
    {
        "id": "workday",
        "name": "Workday",
        "url_pattern": r"([^/]+)\.wd\d+\.myworkdayjobs\.com",
        "api_type": "json_post",
        "api_endpoint": "https://{slug}.{wd_server}.myworkdayjobs.com/wday/cxs/{slug}/{wd_instance}/jobs",
        "cc_search": "*.myworkdayjobs.com*",
        "market": "Grandi imprese, Europa",
        "priority": 1,
    },
    {
        "id": "successfactors",
        "name": "SAP SuccessFactors",
        "url_pattern": r"([^/]+)\.sapsf\.com",
        "api_type": "headless",
        "api_endpoint": "https://career*.sapsf.com/career?company={slug}",
        "cc_search": "*.sapsf.com*",
        "market": "Grandi imprese tedesche e francesi",
        "priority": 2,
    },
    {
        "id": "icims",
        "name": "iCIMS",
        "url_pattern": r"([^/]+)\.icims\.com",
        "api_type": "headless",
        "api_endpoint": "https://{slug}.icims.com/jobs/",
        "cc_search": "*.icims.com*",
        "market": "Enterprise, global",
        "priority": 2,
    },
    {
        "id": "taleo",
        "name": "Oracle Taleo",
        "url_pattern": r"([^/]+)\.taleo\.net",
        "api_type": "headless_difficult",
        "api_endpoint": "https://{slug}.taleo.net/careersection/",
        "cc_search": "*.taleo.net*",
        "market": "Enterprise legacy",
        "priority": 3,
    },
    {
        "id": "cornerstone",
        "name": "Cornerstone OnDemand",
        "url_pattern": r"([^/]+)\.csod\.com",
        "api_type": "headless_difficult",
        "api_endpoint": "https://{slug}.csod.com/ats/careersite/",
        "cc_search": "*.csod.com*",
        "market": "Enterprise",
        "priority": 3,
    },

    # ── PIATTAFORME EUROPEE REGIONALI ──
    {
        "id": "teamtailor",
        "name": "Teamtailor",
        "url_pattern": r"([^/]+)\.teamtailor\.com",
        "api_type": "html",
        "api_endpoint": "https://{slug}.teamtailor.com/jobs",
        "cc_search": "*.teamtailor.com*",
        "market": "Nordici, Germania, Olanda",
        "priority": 2,
    },
    {
        "id": "personio",
        "name": "Personio",
        "url_pattern": r"([^/]+)\.jobs\.personio\.com",
        "api_type": "headless",
        "api_endpoint": "https://{slug}.jobs.personio.com",
        "cc_search": "*.jobs.personio.com*",
        "market": "DACH (Germania, Austria, Svizzera)",
        "priority": 2,
    },
    {
        "id": "werecruit",
        "name": "WeRecruit",
        "url_pattern": r"werecruit\.io/[a-z]{2}/([^/]+)",
        "api_type": "headless",
        "api_endpoint": "https://careers.werecruit.io/{country}/{slug}",
        "cc_search": "careers.werecruit.io*",
        "market": "Francia",
        "priority": 2,
    },
    {
        "id": "softgarden",
        "name": "Softgarden",
        "url_pattern": r"([^/]+)\.softgarden\.com",
        "api_type": "headless",
        "api_endpoint": "https://{slug}.softgarden.com/jobs",
        "cc_search": "*.softgarden.com*",
        "market": "Germania",
        "priority": 2,
    },
    {
        "id": "join",
        "name": "Join.com",
        "url_pattern": r"([^/]+)\.join\.com",
        "api_type": "html_parse",
        "api_endpoint": "https://{slug}.join.com/jobs",
        "cc_search": "*.join.com*",
        "market": "Germania",
        "priority": 3,
    },
    {
        "id": "welcometothejungle",
        "name": "Welcome to the Jungle",
        "url_pattern": r"([^/]+)\.welcometothejungle\.com",
        "api_type": "html",
        "api_endpoint": "https://{slug}.welcometothejungle.com/jobs",
        "cc_search": "*.welcometothejungle.com*",
        "market": "Francia",
        "priority": 3,
    },
    {
        "id": "talentsoft",
        "name": "TalentSoft",
        "url_pattern": r"([^/]+)\.talentsoft\.com",
        "api_type": "html",
        "api_endpoint": "https://{slug}.talentsoft.com",
        "cc_search": "*.talentsoft.com*",
        "market": "Francia",
        "priority": 3,
    },
    {
        "id": "homerun",
        "name": "Homerun",
        "url_pattern": r"([^/]+)\.homerun\.co",
        "api_type": "html_parse",
        "api_endpoint": "https://{slug}.homerun.co/jobs",
        "cc_search": "*.homerun.co*",
        "market": "Paesi Bassi",
        "priority": 3,
    },
    {
        "id": "pinpoint",
        "name": "Pinpoint",
        "url_pattern": r"([^/]+)\.pinpointhq\.com",
        "api_type": "json_get",
        "api_endpoint": "https://{slug}.pinpointhq.com/jobs",
        "cc_search": "*.pinpointhq.com*",
        "market": "UK, Irlanda",
        "priority": 3,
    },
    {
        "id": "jobteaser",
        "name": "JobTeaser",
        "url_pattern": r"([^/]+)\.jobteaser\.com",
        "api_type": "html",
        "api_endpoint": "https://{slug}.jobteaser.com",
        "cc_search": "*.jobteaser.com*",
        "market": "Francia, stage e graduate",
        "priority": 3,
    },
    {
        "id": "zohorecruit",
        "name": "Zoho Recruit",
        "url_pattern": r"([^/]+)\.zohorecruit\.eu",
        "api_type": "headless",
        "api_endpoint": "https://{slug}.zohorecruit.eu/jobs",
        "cc_search": "*.zohorecruit.eu*",
        "market": "PMI, globale",
        "priority": 2,
    },

    # ── PIATTAFORME GLOBALI MID-MARKET ──
    {
        "id": "bamboohr",
        "name": "BambooHR",
        "url_pattern": r"([^/]+)\.bamboohr\.com/careers",
        "api_type": "json_get",
        "api_endpoint": "https://{slug}.bamboohr.com/careers/list",
        "cc_search": "*.bamboohr.com/careers*",
        "market": "PMI, USA e UK",
        "priority": 3,
    },
    {
        "id": "jazzhr",
        "name": "JazzHR",
        "url_pattern": r"([^/]+)\.applytojob\.com",
        "api_type": "html_parse",
        "api_endpoint": "https://{slug}.applytojob.com",
        "cc_search": "*.applytojob.com*",
        "market": "PMI, USA",
        "priority": 3,
    },
    {
        "id": "breezy",
        "name": "Breezy HR",
        "url_pattern": r"([^/]+)\.breezy\.hr",
        "api_type": "json_get",
        "api_endpoint": "https://{slug}.breezy.hr/json",
        "cc_search": "*.breezy.hr*",
        "market": "Startup, global",
        "priority": 3,
    },
    {
        "id": "freshteam",
        "name": "Freshteam (Freshworks)",
        "url_pattern": r"([^/]+)\.freshteam\.com",
        "api_type": "html",
        "api_endpoint": "https://{slug}.freshteam.com/jobs",
        "cc_search": "*.freshteam.com*",
        "market": "PMI, India/EU",
        "priority": 3,
    },
    {
        "id": "manatal",
        "name": "Manatal",
        "url_pattern": r"([^/]+)\.manatal\.com",
        "api_type": "html",
        "api_endpoint": "https://{slug}.manatal.com/jobs",
        "cc_search": "*.manatal.com*",
        "market": "Asia/EU",
        "priority": 3,
    },
    {
        "id": "recruiterbox",
        "name": "Recruiterbox",
        "url_pattern": r"([^/]+)\.recruiterbox\.com",
        "api_type": "html",
        "api_endpoint": "https://{slug}.recruiterbox.com",
        "cc_search": "*.recruiterbox.com*",
        "market": "PMI, global",
        "priority": 3,
    },
    {
        "id": "comeet",
        "name": "Comeet",
        "url_pattern": r"([^/]+)\.comeet\.co",
        "api_type": "html",
        "api_endpoint": "https://{slug}.comeet.co/jobs",
        "cc_search": "*.comeet.co*",
        "market": "Tech",
        "priority": 3,
    },
    {
        "id": "jobadder",
        "name": "JobAdder",
        "url_pattern": r"([^/]+)\.jobadder\.com",
        "api_type": "html",
        "api_endpoint": "https://{slug}.jobadder.com/jobs",
        "cc_search": "*.jobadder.com*",
        "market": "Australia/EU",
        "priority": 3,
    },
    {
        "id": "pageup",
        "name": "PageUp",
        "url_pattern": r"([^/]+)\.pageuppeople\.com",
        "api_type": "headless_difficult",
        "api_endpoint": "https://{slug}.pageuppeople.com",
        "cc_search": "*.pageuppeople.com*",
        "market": "Enterprise, UK/EU",
        "priority": 3,
    },

    # ── PIATTAFORME AI/MODERNE ──
    {
        "id": "phenom",
        "name": "Phenom People",
        "url_pattern": r"([^/]+)\.phenompeople\.com",
        "api_type": "html",
        "api_endpoint": "https://{slug}.phenompeople.com",
        "cc_search": "*.phenompeople.com*",
        "market": "Enterprise, AI-driven",
        "priority": 3,
    },
    {
        "id": "eightfold",
        "name": "Eightfold AI",
        "url_pattern": r"([^/]+)\.eightfold\.ai",
        "api_type": "html",
        "api_endpoint": "https://{slug}.eightfold.ai/careers",
        "cc_search": "*.eightfold.ai*",
        "market": "Enterprise, AI",
        "priority": 3,
    },
    {
        "id": "beamery",
        "name": "Beamery",
        "url_pattern": r"([^/]+)\.beamery\.com",
        "api_type": "html",
        "api_endpoint": "https://{slug}.beamery.com/careers",
        "cc_search": "*.beamery.com*",
        "market": "Enterprise, talent CRM",
        "priority": 3,
    },
    {
        "id": "avature",
        "name": "Avature",
        "url_pattern": r"([^/]+)\.avature\.net",
        "api_type": "html",
        "api_endpoint": "https://{slug}.avature.net/careers",
        "cc_search": "*.avature.net*",
        "market": "Enterprise",
        "priority": 3,
    },
    {
        "id": "hirevue",
        "name": "HireVue",
        "url_pattern": r"([^/]+)\.hirevue\.com",
        "api_type": "html",
        "api_endpoint": "https://{slug}.hirevue.com/careers",
        "cc_search": "*.hirevue.com*",
        "market": "Enterprise, video interviewing",
        "priority": 3,
    },
    {
        "id": "vidcruiter",
        "name": "VidCruiter",
        "url_pattern": r"([^/]+)\.vidcruiter\.com",
        "api_type": "html",
        "api_endpoint": "https://{slug}.vidcruiter.com",
        "cc_search": "*.vidcruiter.com*",
        "market": "Enterprise",
        "priority": 3,
    },
]


def tutte_le_piattaforme() -> list[dict]:
    return REGISTRY


def per_priorita(priorita: int) -> list[dict]:
    """Le piattaforme con questa priorità o meglio."""
    return [p for p in REGISTRY if p["priority"] <= priorita]


def per_api_type(api_type: str) -> list[dict]:
    """Le piattaforme con questo tipo di API."""
    return [p for p in REGISTRY if p["api_type"] == api_type]


def conta() -> dict:
    """Statistiche del catalogo."""
    per_tipo: dict[str, int] = {}
    for p in REGISTRY:
        per_tipo[p["api_type"]] = per_tipo.get(p["api_type"], 0) + 1
    return {
        "totale": len(REGISTRY),
        "con_api_json": len(per_api_type("json_get")) + len(per_api_type("json_post")),
        "con_api_html": len(per_api_type("html")),
        "per_priorita": {
            "1_critico": len([p for p in REGISTRY if p["priority"] == 1]),
            "2_importante": len([p for p in REGISTRY if p["priority"] == 2]),
            "3_opzionale": len([p for p in REGISTRY if p["priority"] == 3]),
        },
        "per_tipo": per_tipo,
    }
