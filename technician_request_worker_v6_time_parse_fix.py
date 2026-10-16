from playwright.sync_api import sync_playwright
from robot_cdp_health import ensure_autototal_responsive
from pathlib import Path
from difflib import SequenceMatcher
import sys
import json
import re
import math
import unicodedata
import asyncio
import os
from worker_run_guard import write_worker_result
import time
from urllib.request import urlopen
import websockets
from automotive_vocabulary import expand_technical_request

CDP_URL = "http://127.0.0.1:9222"
HOURLY_RATE_RON = 200.0
ALBATROS_BUFFER_HOURS = 0.4
BOOKING_EXTRA_HOURS = 1.0
REVISION_BOOKING_EXTRA_HOURS = 0.5

# Praguri de siguranță.
MIN_MATCH_SCORE = 0.48
MIN_MARGIN_TO_SECOND = 0.06

# Limbaj client / jargon / termeni uzuali -> operație canonică de căutat în Haynes.
# Nu încercăm să memorăm fiecare propoziție posibilă; asociem semantic prin alias-uri.
OPERATION_LIBRARY = [
    {
        "id": "brake_pads_front",
        "canonical": "inlocuire placute de frana punte fata",
        "haynes_queries": [
            "inlocuire placute de frana fata",
            "inlocuire placute frana fata",
        ],
        "aliases": [
            "placute fata", "placute frana fata", "schimb placute fata",
            "schimbat placute fata", "frane fata", "frana fata",
            "front brake pads", "replace front brake pads", "front pads",
            "bremsbelage vorne", "bremsklotze vorne",
            "plaquettes avant", "plaquettes de frein avant",
        ],
    },
    {
        "id": "brake_pads_rear",
        "canonical": "inlocuire placute de frana punte spate",
        "aliases": [
            "placute spate", "placute frana spate", "schimb placute spate",
            "schimbat placute spate", "frane spate", "frana spate",
            "rear brake pads", "replace rear brake pads", "rear pads",
            "bremsbelage hinten", "bremsklotze hinten",
            "plaquettes arriere", "plaquettes de frein arriere",
        ],
    },
    {
        "id": "brake_discs_front",
        "canonical": "inlocuire discuri de frana punte fata",
        "haynes_queries": [
            "inlocuire ambelor discuri de frana fata",
            "inlocuire discuri de frana fata",
        ],
        "aliases": [
            "discuri fata", "discuri frana fata", "schimb discuri fata",
            "front brake discs", "front rotors", "replace front brake discs",
            "bremsscheiben vorne", "disques frein avant",
        ],
    },
    {
        "id": "brake_discs_rear",
        "canonical": "inlocuire discuri de frana punte spate",
        "aliases": [
            "discuri spate", "discuri frana spate", "schimb discuri spate",
            "rear brake discs", "rear rotors", "replace rear brake discs",
            "bremsscheiben hinten", "disques frein arriere",
        ],
    },
    {
        "id": "brake_caliper_rear",
        "canonical": "inlocuire etrier de frana punte spate",
        "haynes_queries": [
            "etrier de frana spate stanga inlocuire",
            "etrier de frana punte spate inlocuire",
        ],
        "aliases": [
            "etrier spate", "etrier frana spate", "etrier stanga spate",
            "rear brake caliper", "rear left brake caliper",
            "bremssattel hinten", "etrier de frein arriere",
        ],
    },
    {
        "id": "timing_belt",
        "canonical": "inlocuire set curea de distributie si pompa de lichid de racire",
        "aliases": [
            "distributie", "kit distributie", "curea distributie",
            "schimb distributie", "distributie cu pompa apa", "kit distributie pompa apa",
            "timing belt", "timing belt kit", "timing belt water pump",
            "zahnriemen", "zahnriemensatz", "courroie distribution",
        ],
    },
    {
        "id": "timing_chain",
        "canonical": "inlocuire lant de distributie",
        "aliases": [
            "lant distributie", "kit lant distributie", "schimb lant distributie",
            "timing chain", "timing chain kit", "steuerkette",
            "chaine distribution",
        ],
    },
    {
        "id": "engine_oil",
        "canonical": "schimb ulei motor",
        "haynes_queries": [
            "schimb ulei motor",
            "inlocuire ulei motor",
            "ulei motor",
            "golire si umplere ulei motor",
            "scurgere si umplere ulei motor",
            "schimb ulei motor si filtru de ulei",
            "ulei motor si filtru de ulei",
        ],
        "aliases": [
            "schimb ulei", "ulei motor", "revizie ulei",
            "oil change", "engine oil change", "motorol wechsel", "olwechsel",
            "vidange moteur",
        ],
    },
    {
        "id": "oil_filter",
        "canonical": "inlocuire filtru ulei",
        "aliases": [
            "filtru ulei", "schimb filtru ulei", "oil filter",
            "oil filter replacement", "olfilter", "filtre huile",
        ],
    },
    {
        "id": "air_filter",
        "canonical": "inlocuire filtru aer",
        "aliases": [
            "filtru aer", "schimb filtru aer", "air filter",
            "luftfilter", "filtre air",
        ],
    },
    {
        "id": "cabin_filter",
        "canonical": "inlocuire filtru habitaclu",
        "aliases": [
            "filtru habitaclu", "filtru polen", "filtru clima",
            "cabin filter", "pollen filter", "innenraumfilter",
            "filtre habitacle",
        ],
    },
    {
        "id": "fuel_filter",
        "canonical": "inlocuire filtru combustibil",
        "aliases": [
            "filtru motorina", "filtru benzina", "filtru combustibil",
            "fuel filter", "kraftstofffilter", "filtre carburant",
        ],
    },
    {
        "id": "spark_plugs",
        "canonical": "inlocuire bujii",
        "aliases": [
            "bujii", "schimb bujii", "spark plugs", "spark plug replacement",
            "zundkerzen", "bougies",
        ],
    },
    {
        "id": "glow_plugs",
        "canonical": "inlocuire bujii incandescente",
        "aliases": [
            "bujii incandescente", "schimb bujii incandescente",
            "glow plugs", "gluhkerzen", "bougies prechauffage",
        ],
    },
    {
        "id": "clutch",
        "canonical": "inlocuire ambreiaj",
        "aliases": [
            "ambreiaj", "kit ambreiaj", "schimb ambreiaj",
            "clutch", "clutch kit", "kupplung", "embrayage",
        ],
    },
    {
        "id": "flywheel",
        "canonical": "inlocuire volant",
        "aliases": [
            "volanta", "volant", "schimb volanta", "dual mass flywheel",
            "flywheel", "zweimassenschwungrad", "volant moteur",
        ],
    },
    {
        "id": "shock_front",
        "canonical": "inlocuire amortizor punte fata",
        "aliases": [
            "amortizor fata", "amortizoare fata", "schimb amortizoare fata",
            "front shock absorber", "front strut", "stossdampfer vorne",
            "amortisseur avant",
        ],
    },
    {
        "id": "shock_rear",
        "canonical": "inlocuire amortizor punte spate",
        "aliases": [
            "amortizor spate", "amortizoare spate", "schimb amortizoare spate",
            "rear shock absorber", "rear strut", "stossdampfer hinten",
            "amortisseur arriere",
        ],
    },
    {
        "id": "wheel_bearing_front",
        "canonical": "inlocuire rulment roata fata",
        "aliases": [
            "rulment fata", "rulment roata fata", "butuc fata",
            "front wheel bearing", "radlager vorne", "roulement roue avant",
        ],
    },
    {
        "id": "wheel_bearing_rear",
        "canonical": "inlocuire rulment roata spate",
        "aliases": [
            "rulment spate", "rulment roata spate", "butuc spate",
            "rear wheel bearing", "radlager hinten", "roulement roue arriere",
        ],
    },
    {
        "id": "alternator",
        "canonical": "inlocuire alternator",
        "aliases": [
            "alternator", "schimb alternator", "generator",
            "replace alternator", "lichtmaschine", "alternateur",
        ],
    },
    {
        "id": "starter",
        "canonical": "inlocuire demaror",
        "aliases": [
            "demaror", "electromotor", "schimb demaror",
            "starter motor", "anlasser", "demarreur",
        ],
    },
    {
        "id": "water_pump",
        "canonical": "inlocuire pompa lichid de racire",
        "aliases": [
            "pompa apa", "pompa de apa", "pompa lichid racire",
            "water pump", "wasserpumpe", "pompe a eau",
        ],
    },
    {
        "id": "thermostat",
        "canonical": "inlocuire termostat",
        "aliases": [
            "termostat", "schimb termostat", "thermostat",
        ],
    },
    {
        "id": "radiator",
        "canonical": "inlocuire radiator lichid de racire",
        "aliases": [
            "radiator apa", "radiator racire", "schimb radiator",
            "coolant radiator", "kuhler", "radiateur",
        ],
    },
    {
        "id": "ac_compressor",
        "canonical": "inlocuire compresor aer conditionat",
        "aliases": [
            "compresor ac", "compresor clima", "compresor aer conditionat",
            "ac compressor", "klimakompressor", "compresseur climatisation",
        ],
    },
    {
        "id": "battery",
        "canonical": "inlocuire baterie",
        "aliases": [
            "baterie", "acumulator", "schimb baterie",
            "battery replacement", "batterie", "batterie remplacement",
        ],
    },
    {
        "id": "drive_belt",
        "canonical": "inlocuire curea accesorii",
        "aliases": [
            "curea accesorii", "cureaua accesorii", "cureaua de accesorii",
            "curea alternator", "curea auxiliara",
            "serpentine belt", "auxiliary belt", "keilrippenriemen",
            "courroie accessoire",
        ],
    },
    {
        "id": "lower_arm_front",
        "canonical": "inlocuire brat suspensie fata",
        "aliases": [
            "brat fata", "brate fata", "brat suspensie fata", "bascula fata",
            "brat suspensie", "bascula", "bascula stanga", "bascula dreapta",
            "front control arm", "querlenker vorne", "bras suspension avant",
        ],
    },
    {
        "id": "tie_rod",
        "canonical": "inlocuire capat de bara directie",
        "haynes_queries": ["capat de bara directie inlocuire", "articulatie bara de directie inlocuire"],
        "aliases": [
            "capat bara", "cap bara", "cap de bara", "capete de bara",
            "tie rod end", "spurstangenkopf", "rotule direction",
        ],
    },
    {
        "id": "axial_tie_rod", "canonical": "inlocuire bieleta axiala directie",
        "haynes_queries": ["bieleta axiala de directie inlocuire", "bara axiala de directie inlocuire", "articulatie axiala inlocuire"],
        "aliases": ["bieleta directie", "bieleta axiala", "bara axiala", "axial directie", "inner tie rod", "axial rod", "axialgelenk", "biellette axiale"],
    },
    {
        "id": "ball_joint_front", "canonical": "inlocuire pivot suspensie fata",
        "haynes_queries": ["pivot suspensie fata inlocuire", "articulatie sferica suspensie fata inlocuire", "rotula suspensie fata inlocuire"],
        "aliases": ["pivot", "pivoti", "pivot fata", "pivoti fata", "rotula suspensie", "ball joint", "suspension ball joint", "traggelenk", "rotule suspension"],
    },
    {
        "id": "stabilizer_link_front", "canonical": "inlocuire bieleta bara stabilizatoare fata",
        "haynes_queries": ["bieleta bara stabilizatoare fata inlocuire", "tija antiruliu fata inlocuire", "articulatie bara stabilizatoare inlocuire"],
        "aliases": ["bieleta stabilizatoare", "bieleta bara stabilizatoare", "bieleta antiruliu", "tija antiruliu", "stabilizer link", "sway bar link", "koppelstange", "biellette barre stabilisatrice"],
    },
    {
        "id": "wheel_alignment_front", "canonical": "reglaj geometrie directie fata",
        "haynes_queries": ["reglaj geometrie directie", "reglaj convergenta roti fata", "aliniere roti fata"],
        "aliases": ["reglaj directie", "geometrie directie", "geometrie roti", "reglaj geometrie", "reglaj convergenta", "wheel alignment", "tracking adjustment", "spur einstellen"],
    },
]

# Expresii care nu sunt operații de manoperă și trebuie ignorate.
NOISE_PHRASES = [
    "doar manopera", "numai manopera", "piese client", "piesele clientului",
    "piesele sunt furnizate de client", "clientul aduce piesele",
    "oferta", "solicita oferta", "pret", "cat costa", "va rog",
]

def normalize(text: str) -> str:
    text = (text or "").lower().strip()
    text = unicodedata.normalize("NFKD", text)
    text = "".join(c for c in text if not unicodedata.combining(c))
    text = text.replace("&", " si ")
    text = re.sub(r"[^a-z0-9]+", " ", text)
    return " ".join(text.split())

def clean_request(text: str) -> str:
    s = normalize(expand_technical_request(text))
    for old, new in {
        "inlocuirea": "inlocuire",
        "discurilor": "discuri",
        "placutelor": "placute",
        "franelor": "frane",
    }.items():
        s = re.sub(rf"\b{old}\b", new, s)
    s = re.sub(r"\bstg\b", "stanga", s)
    s = re.sub(r"\bdr\b", "dreapta", s)
    for phrase in NOISE_PHRASES:
        s = s.replace(normalize(phrase), " ")
    return " ".join(s.split())

def token_score(a: str, b: str) -> float:
    aa = normalize(a)
    bb = normalize(b)
    aset = set(aa.split())
    bset = set(bb.split())
    if not aset or not bset:
        return 0.0

    coverage = len(aset & bset) / len(aset)
    reverse_coverage = len(aset & bset) / len(bset)
    sequence = SequenceMatcher(None, aa, bb).ratio()

    # Favorizăm acoperirea cuvintelor operației, dar păstrăm toleranță la formulări/greșeli.
    return (coverage * 0.55) + (reverse_coverage * 0.20) + (sequence * 0.25)

def request_contains_alias(request: str, alias: str) -> float:
    r = normalize(request)
    a = normalize(alias)
    if not a:
        return 0.0
    if a in r:
        return 1.0
    return token_score(a, r)

def detect_operations(request: str) -> list[dict]:
    cleaned = clean_request(request)
    matches = []

    for op in OPERATION_LIBRARY:
        scored_aliases = []
        for alias in [op["canonical"], *op["aliases"]]:
            scored_aliases.append((request_contains_alias(cleaned, alias), alias))

        scored_aliases.sort(reverse=True, key=lambda x: x[0])
        best_score, best_alias = scored_aliases[0]

        # Pentru expresii foarte scurte cerem un scor mai mare.
        alias_tokens = len(normalize(best_alias).split())
        threshold = 0.78 if alias_tokens <= 2 else 0.62

        if best_score >= threshold:
            matches.append({
                "id": op["id"],
                "canonical": op["canonical"],
                "haynes_queries": op.get("haynes_queries") or [op["canonical"]],
                "matched_alias": best_alias,
                "intent_score": round(best_score, 3),
            })

    # Evităm duplicatele.
    unique = {}
    for item in matches:
        prev = unique.get(item["id"])
        if not prev or item["intent_score"] > prev["intent_score"]:
            unique[item["id"]] = item

    result = list(unique.values())

    s = normalize(cleaned)

    # Reguli pentru revizie / "toate filtrele".
    # Clientul nu trebuie să enumere filtru ulei + aer + habitaclu + combustibil.
    all_filters_phrases = (
        "revizie",
        "toate filtrele",
        "toate filtre",
        "filtrele toate",
        "all filters",
        "alle filter",
        "tous les filtres",
        "revizie completa",
        "revizie de filtre",
        "revizie filtre",
        "ulei si filtre",
        "filtre si ulei",
        "schimb ulei si filtre",
        "service complet",
        "full service",
    )
    wants_all_filters = any(normalize(p) in s for p in all_filters_phrases)

    if wants_all_filters:
        for opid in ("oil_filter", "air_filter", "cabin_filter", "fuel_filter"):
            if not any(x["id"] == opid for x in result):
                op = next(x for x in OPERATION_LIBRARY if x["id"] == opid)
                result.append({
                    "id": opid,
                    "canonical": op["canonical"],
                    "haynes_queries": op.get("haynes_queries") or [op["canonical"]],
                    "matched_alias": "toate filtrele",
                    "intent_score": 1.0,
                })

    # Dacă cererea spune revizie completă / schimb ulei + filtre, includem și uleiul.
    service_phrases = (
        "revizie",
        "schimb ulei",
        "ulei si filtre",
        "filtre si ulei",
        "ulei filtre",
        "revizie completa",
        "service complet",
        "oil and filters",
        "oil filters",
        "full service",
    )
    if any(normalize(p) in s for p in service_phrases):
        if not any(x["id"] == "engine_oil" for x in result):
            op = next(x for x in OPERATION_LIBRARY if x["id"] == "engine_oil")
            result.append({
                "id": "engine_oil",
                "canonical": op["canonical"],
                "haynes_queries": op.get("haynes_queries") or [op["canonical"]],
                "matched_alias": "schimb ulei / revizie",
                "intent_score": 1.0,
            })

    # Read the axle next to each component.  A global "față" elsewhere in a
    # long sentence must not turn "discuri spate" into discs on both axles.
    def component_axles(pattern: str) -> set[str]:
        axes = set()
        shared_group = re.search(
            r'\b(?:discur\w*|placut\w*)\s+(?:(?:de\s+)?frana\s+)?(?:si\s+)?'
            r'(?:discur\w*|placut\w*)\s+(?:(?:de\s+)?frana\s+)?'
            r'(fata|spate)\s+(?:si\s+)?(fata|spate)\b', s)
        if shared_group and re.search(pattern, shared_group.group(0)):
            axes.update('rear' if axle == 'spate' else 'front' for axle in shared_group.groups())
        for match in re.finditer(pattern, s):
            shared = re.match(r'\s+(?:(?:de\s+)?frana\s+)?(fata|anterior|spate|posterior)\s+(?:si\s+)?(fata|anterior|spate|posterior)\b', s[match.end():])
            if shared:
                axes.update('rear' if axle in {'spate','posterior'} else 'front' for axle in shared.groups())
                continue
            nearby = []
            lo, hi = max(0, match.start() - 24), min(len(s), match.end() + 34)
            for axle in re.finditer(r"\b(fata|anterior|spate|posterior)\b", s[lo:hi]):
                absolute_start, absolute_end = lo + axle.start(), lo + axle.end()
                distance = (match.start() - absolute_end if absolute_end <= match.start()
                            else absolute_start - match.end() if absolute_start >= match.end() else 0)
                nearby.append((max(0, distance), axle.group(1)))
            if nearby:
                word = min(nearby, key=lambda item: item[0])[1]
                axes.add("rear" if word in {"spate", "posterior"} else "front")
        return axes

    # Use the client's original sentence here. `clean_request` appends generic
    # vocabulary hints without axle information, which must not influence it.
    axis_text = s
    original_request = re.split(r"\|\s*TERMENI\s+TEHNICI\s*:", request, maxsplit=1, flags=re.I)[0]
    s = normalize(original_request)
    requested_axes = {
        "brake_pads": component_axles(r"placut\w*|pastil\w*"),
        "brake_discs": component_axles(r"\bdiscur\w*|\bdisc\w*"),
    }
    s = axis_text
    for family_name, axes in requested_axes.items():
        for axle in axes:
            opid = f"{family_name}_{axle}"
            if not any(x["id"] == opid for x in result):
                op = next(x for x in OPERATION_LIBRARY if x["id"] == opid)
                result.append({
                    "id": opid,
                    "canonical": op["canonical"],
                    "haynes_queries": op.get("haynes_queries") or [op["canonical"]],
                    "matched_alias": "poziție citită lângă piesă",
                    "intent_score": 1.0,
                })
        if axes:
            allowed_ids = {f"{family_name}_{axle}" for axle in axes}
            result = [row for row in result
                      if not row["id"].startswith(f"{family_name}_") or row["id"] in allowed_ids]

    ambiguous_front_arms = (
        bool(re.search(r"\b(?:brate|bratele|toate)\b", s))
        and bool(re.search(r"\bbrat(?:e|ele)?\b|\bbascul", s))
        and not any(term in s for term in (
            "superior", "inferior", "transversal", "longitudinal", "stanga", "dreapta",
            "bucsa brat", "bucsa bascula", "bascula stanga", "bascula dreapta",
        ))
    )
    if ambiguous_front_arms:
        result = [row for row in result if row["id"] != "lower_arm_front"]

    # Dacă avem o operație specifică, eliminăm operații mai generale care ar dubla timpul.
    ids = {x["id"] for x in result}
    if "timing_belt" in ids and "water_pump" in ids:
        result = [x for x in result if x["id"] != "water_pump"]

    return sorted(result, key=lambda x: x["intent_score"], reverse=True)

def find_haynes_page(browser):
    for context in browser.contexts:
        for pg in context.pages:
            if "workshopdata.com" in pg.url.lower():
                return pg
    return None

def find_intercars_page(browser):
    for context in browser.contexts:
        for pg in context.pages:
            if "intercars.eu" in pg.url.lower():
                return pg
    return None

def load_offer_products(path_text: str) -> list[dict]:
    if not path_text:
        return []
    try:
        data = json.loads(Path(path_text).read_text(encoding="utf-8"))
        return data.get("products") or []
    except (OSError, ValueError, AttributeError):
        return []

def intercars_product_for_operation(products: list[dict], operation_id: str) -> dict | None:
    needles = {
        "brake_discs_front": ("disc", "fata"),
        "brake_discs_rear": ("disc", "spate"),
        "brake_pads_front": ("placute", "fata"),
        "brake_pads_rear": ("placute", "spate"),
        "brake_caliper_rear": ("etrier", "spate"),
        "shock_front": ("amortizor", "fata"),
        "shock_rear": ("amortizor", "spate"),
        "lower_arm_front": ("brat", "fata"),
        "tie_rod": ("capat", "bara"),
        "axial_tie_rod": ("bieleta", "directie"),
        "ball_joint_front": ("pivot",),
        "stabilizer_link_front": ("bieleta", "stabilizatoare"),
    }.get(operation_id)
    if not needles:
        return None
    for product in products:
        requested = normalize(product.get("requested_item") or "")
        description = normalize(product.get("description") or product.get("name") or "")
        # For shocks, use only the actual damper page. Mounts, bearings and
        # protection kits have their own catalogue times and must not seed the
        # replacement-time lookup for the damper itself.
        if operation_id in {"shock_front", "shock_rear"} and any(
                marker in description for marker in
                ("rulment", "flansa", "suport", "protectie", "praf", "burduf", "tampon", "surub")):
            continue
        if all(needle in requested for needle in needles) and product.get("product_url"):
            return product
    return None

def intercars_snapshot_for_operation(page, product: dict) -> list[str]:
    page.goto(product["product_url"], wait_until="domcontentloaded", timeout=30000)
    page.wait_for_timeout(1000)
    tabs = page.locator('a.js-clk-haynes-trigger, [data-test="tabs-item"]')
    clicked = False
    for i in range(tabs.count()):
        tab = tabs.nth(i)
        try:
            if "TIMPI DE REPARATIE" in normalize(tab.inner_text(timeout=500)).upper().replace("Ț", "T"):
                if tab.is_visible():
                    tab.click()
                    clicked = True
                    break
        except Exception:
            continue
    if not clicked:
        # Pagina de listă folosește diacritice și altă structură.
        labels = page.get_by_text(re.compile(r"Timpii? de repara", re.I))
        for i in range(labels.count()):
            try:
                if labels.nth(i).is_visible():
                    labels.nth(i).click()
                    clicked = True
                    break
            except Exception:
                continue
    if not clicked:
        return []
    # The Haynes panel is loaded asynchronously.  On a warm browser 800 ms
    # may work, but a clean test regularly needs several seconds.
    try:
        page.wait_for_function(
            "() => { const panel=document.querySelector('.tabs__content.repair-times.is-active'); "
            "return panel && /\\d{1,2}:\\d{2}\\s*\\/\\s*\\d+[,.]\\d+\\s*h/i.test(panel.innerText||''); }",
            timeout=12000,
        )
    except Exception:
        page.wait_for_timeout(1200)
    return haynes_text_snapshot(page)


def wait_for_offer_products(path_text: str, timeout_seconds: float = 600.0) -> dict:
    """Wait for the parts worker, so labour never races the Inter Cars VIN flow."""
    if not path_text:
        return {}
    path = Path(path_text)
    deadline = time.monotonic() + timeout_seconds
    last = {}
    while time.monotonic() < deadline:
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(value, dict):
                last = value
                if value.get("status") in {
                    "ready", "partial", "error", "needs_vehicle_data", "failed"
                }:
                    return value
                if value.get("products") and value.get("status") == "running":
                    # Products are partial; keep waiting for all requested groups.
                    pass
        except Exception:
            pass
        time.sleep(0.5)
    return last


def intercars_revision_maintenance(page, offer_result_path: str, request_id: int,
                                   vin: str, operations: list[dict]) -> dict | None:
    """Read revision labour and oil capacity from Inter Cars integrated Haynes."""
    revision_ids = {"engine_oil", "oil_filter", "air_filter", "cabin_filter", "fuel_filter"}
    requested = {op.get("id") for op in operations}
    # Haynes maintenance also covers filter-only revisions. Requiring an
    # explicit engine-oil intent made requests such as "revizie de filtre"
    # skip the integrated maintenance flow and finish without labour times.
    if not requested or not requested.issubset(revision_ids):
        return None
    try:
        offer = json.loads(Path(offer_result_path).read_text(encoding="utf-8"))
        vehicle_url = str(((offer.get("vehicle") or {}).get("url")) or "")
    except Exception:
        return None
    if "intercars.eu" not in vehicle_url:
        return None

    page.goto(vehicle_url, wait_until="domcontentloaded", timeout=30000)
    page.wait_for_timeout(1200)
    opened = page.evaluate("""() => {
        const link = [...document.querySelectorAll('a.vehicleinfo__addmaintenance')]
            .find(element => element.offsetParent);
        if (!link) return false;
        link.click();
        return true;
    }""")
    if not opened:
        return None
    popup = page.locator(".js-maintenance-service-popup")
    popup.wait_for(state="visible", timeout=20000)
    selected = page.evaluate("""() => {
        const popup = [...document.querySelectorAll('.js-maintenance-service-popup')]
            .find(element => element.offsetParent);
        if (!popup) return false;
        // Different manufacturers name the same maintenance operation
        // differently. BMW uses "Sistem de întreținere ... ulei de motor"
        // instead of the generic "Service de ulei" label.
        const label = [...popup.querySelectorAll('label')]
            .find(element => /(Service de ulei|ulei de motor)/i.test(element.innerText || ''));
        const radio = label && label.querySelector('input[type=radio]');
        const submit = popup.querySelector('.js-maintenance-service-popup-submit');
        if (!radio || !submit) return false;
        radio.checked = true;
        radio.dispatchEvent(new Event('input', {bubbles: true}));
        radio.dispatchEvent(new Event('change', {bubbles: true}));
        submit.classList.remove('is-disabled');
        submit.click();
        return true;
    }""")
    if not selected:
        return None
    page.wait_for_url(re.compile(r"create-maintenance-offer"), timeout=30000)
    page.locator("form.js-offer-edit [name=title]").fill(f"Albatros cerere {request_id}")
    page.locator("form.js-offer-edit [name=vehicleVin]").fill(vin)
    page.locator("form.js-offer-edit button[type=submit]").click()
    page.wait_for_url(re.compile(r"edit-maintenance-service"), timeout=45000)
    page.wait_for_function(
        "() => document.body && /Timp standard/i.test(document.body.innerText) "
        "&& /Carter motor, inclusiv filtru/i.test(document.body.innerText)",
        timeout=45000,
    )

    motor_text = page.locator("body").inner_text(timeout=10000)
    # Cabin filter lives under the Interior accordion; expanding it closes the
    # Motor accordion, so preserve both snapshots before parsing.
    try:
        page.get_by_text("Interior", exact=True).last.click(timeout=5000)
        page.wait_for_timeout(700)
    except Exception:
        pass
    interior_text = page.locator("body").inner_text(timeout=10000)
    text = motor_text + "\n" + interior_text
    normalized_text = normalize(text)

    def number(pattern: str, default: float = 0.0) -> float:
        match = re.search(pattern, text, re.I | re.S)
        return float(match.group(1).replace(",", ".")) if match else default

    base_hours = number(r"Timp standard\s+(\d+(?:[,.]\d+)?)")
    oil_liters = number(r"Carter motor, inclusiv filtru\s+(\d+(?:[,.]\d+)?)\s*\(l\)")
    fuel_hours = number(r"Sarcina suplimentar[aă]:\s*Înlocuire filtru de carburant\s*\+\s*(\d+(?:[,.]\d+)?)\s*h")
    air_hours = number(r"Sarcina suplimentar[aă]:\s*Înlocuire filtru de aer\s*\+\s*(\d+(?:[,.]\d+)?)\s*h")
    if base_hours <= 0 or oil_liters <= 0:
        return None

    resolved = []
    unresolved = []
    for op in operations:
        op_id = op.get("id")
        row = dict(op)
        if op_id == "engine_oil":
            row.update(haynes_operation="Service ulei (indicator service)", technical_hours=base_hours)
        elif op_id == "oil_filter" and "filtru de ulei" in normalized_text:
            if "engine_oil" in requested:
                row.update(haynes_operation="Înlocuire filtru ulei — inclus în serviciul standard",
                           technical_hours=0.0, included_in="engine_oil")
            else:
                row.update(haynes_operation="Înlocuire filtru ulei — serviciul standard",
                           technical_hours=base_hours)
        elif op_id == "cabin_filter" and ("filtru de praf si polen" in normalized_text or "filtru aer habitaclu" in normalized_text):
            row.update(haynes_operation="Înlocuire filtru habitaclu — inclus în serviciul standard",
                       technical_hours=0.0,
                       included_in="engine_oil" if "engine_oil" in requested else "standard_service")
        elif op_id == "fuel_filter" and fuel_hours > 0:
            row.update(haynes_operation="Sarcină suplimentară: înlocuire filtru carburant",
                       technical_hours=fuel_hours)
        elif op_id == "air_filter" and air_hours > 0:
            row.update(haynes_operation="Sarcină suplimentară: înlocuire filtru aer",
                       technical_hours=air_hours)
        else:
            unresolved.append(op)
            continue
        resolved.append(row)
    return {"resolved": resolved, "unresolved": unresolved,
            "engine_oil_liters": oil_liters, "engine_oil_specification": "",
            "engine_oil_options": []}

def score_haynes(query: str, text: str) -> float:
    q = normalize(query)
    t = normalize(text)
    qset = set(q.split())
    tset = set(t.split())
    if not qset or not tset:
        return 0.0

    coverage = len(qset & tset) / len(qset)
    sequence = SequenceMatcher(None, q, t).ratio()

    # Cuvinte tehnice importante.
    important = {
        "fata", "spate", "punte", "placute", "discuri", "distributie",
        "pompa", "ambreiaj", "volant", "ulei", "filtru", "amortizor",
        "rulment", "alternator", "demaror", "termostat", "radiator",
    }
    qimp = qset & important
    imp_score = (len(qimp & tset) / len(qimp)) if qimp else 1.0

    return (coverage * 0.60) + (sequence * 0.20) + (imp_score * 0.20)

def haynes_text_snapshot(page) -> list[str]:
    """
    Citește pagina Haynes o singură dată.
    Evită mii de apeluri Playwright pe fiecare div/tr, care făceau workerul lent.
    """
    try:
        body_text = page.locator("body").inner_text(timeout=8000)
    except Exception:
        body_text = ""

    raw_lines = [" ".join(line.split()) for line in body_text.splitlines()]
    raw_lines = [line for line in raw_lines if line]

    # Haynes poate despărți codul/operația/timpul pe 2 linii.
    # Păstrăm atât liniile individuale, cât și ferestre de 2 și 3 linii.
    candidates = list(raw_lines)

    for size in (2, 3):
        for i in range(0, max(len(raw_lines) - size + 1, 0)):
            merged = " ".join(raw_lines[i:i+size])
            if len(merged) <= 420:
                candidates.append(merged)

    # Eliminăm duplicatele fără a strica ordinea.
    seen = set()
    result = []
    for line in candidates:
        key = normalize(line)
        if key and key not in seen:
            seen.add(key)
            result.append(line)

    return result


def extract_candidates_from_snapshot(snapshot: list[str], query: str) -> list[dict]:
    candidates = []

    for txt in snapshot:
        if len(txt) < 10 or len(txt) > 420:
            continue

        normalized_text = normalize(txt)
        # Product cards contain production intervals such as 06.04-06.15.
        # They are not labour rows and must never become 6.15 hours.
        if "se potriveste" in normalized_text or "anii de productie" in normalized_text:
            continue

        hour_matches = re.findall(
            r'\b\d{1,2}:\d{2}\s*/\s*(\d{1,2}[,.]\d{2})\s*h\b', txt, flags=re.I
        )
        direct_matches = re.findall(r'\+\s*(\d{1,2}[,.]\d{2})\s*$', txt)
        if hour_matches:
            time_text = hour_matches[-1]
        elif direct_matches:
            time_text = direct_matches[-1]
        else:
            continue

        sc = score_haynes(query, txt)
        if sc < MIN_MATCH_SCORE:
            continue

        # În Haynes, pe unele linii apare mai întâi un număr de grup/secțiune
        # (ex. "10,00 Filtru de combustibil") și abia la final timpul real
        # al operației (ex. "+ 1,10"). Pentru operațiile de reparație,
        # timpul util este ultimul număr zecimal din linie/fragment.
        candidates.append({
            "score": sc,
            "operation": txt,
            "technical_hours": float(time_text.replace(",", ".")),
            "time_text": time_text,
            # Rândurile reale de operație Haynes se termină cu butonul "+" și timpul.
            # Ferestrele de context pot lipi timpul rândului vecin și nu sunt sigure.
            "direct_operation_row": bool(hour_matches or direct_matches),
        })

    unique = {}
    for item in candidates:
        key = normalize(item["operation"])
        if key not in unique or item["score"] > unique[key]["score"]:
            unique[key] = item

    return sorted(unique.values(), key=lambda x: x["score"], reverse=True)

def extract_candidates_for_operation(snapshot: list[str], op: dict) -> list[dict]:
    queries = op.get("haynes_queries") or [op.get("canonical") or ""]
    merged = {}

    for query in queries:
        for item in extract_candidates_from_snapshot(snapshot, query):
            key = normalize(item["operation"])
            enriched = dict(item)
            enriched["matched_query"] = query

            if key not in merged or enriched["score"] > merged[key]["score"]:
                merged[key] = enriched

    result = list(merged.values())
    direct_rows = [item for item in result if item.get("direct_operation_row")]
    if direct_rows:
        result = direct_rows
    return sorted(result, key=lambda x: x["score"], reverse=True)


def choose_safe_candidate(candidates: list[dict], operation_id: str = "") -> dict | None:
    if not candidates:
        return None

    # Inter Cars afișează separat stânga, dreapta și operația completă pe punte.
    # Pentru o cerere generică de plăcuțe/discuri alegem numai operația completă,
    # ca să nu ofertăm accidental timpul unei singure roți.
    whole_axle_rules = {
        "brake_pads_front": lambda t: "placute" in t and "fata" in t and not any(x in t for x in ("stanga", "dreapta", "spate", "tuturor")),
        "brake_pads_rear": lambda t: "placute" in t and "spate" in t and not any(x in t for x in ("stanga", "dreapta", "fata", "tuturor")),
        "brake_discs_front": lambda t: "ambelor" in t and "discur" in t and "fata" in t,
        "brake_discs_rear": lambda t: "ambelor" in t and "discur" in t and "spate" in t,
        "shock_front": lambda t: "ambelor" in t and "amortiz" in t and "fata" in t,
        "shock_rear": lambda t: "ambelor" in t and "amortiz" in t and "spate" in t,
        "stabilizer_link_front": lambda t: (
            any(word in t for word in ("ambelor", "ambele"))
            and any(word in t for word in ("bielet", "tija", "articul"))
            and any(word in t for word in ("stabiliz", "antiruliu"))
            and "fata" in t
        ),
        "tie_rod": lambda t: ("bara" in t and "directie" in t
                                and not any(x in t for x in ("spate", "posterioara", "posterior"))),
        "lower_arm_front": lambda t: (any(x in t for x in ("brat", "bascula"))
                                       and not any(x in t for x in ("spate", "posterioara", "posterior"))),
        "brake_caliper_rear": lambda t: "etrier" in t and "spate" in t,
    }
    rule = whole_axle_rules.get(operation_id)
    if rule:
        exact = [item for item in candidates if rule(normalize(item.get("operation") or ""))]
        if exact:
            candidates = exact
        elif operation_id in {"tie_rod", "lower_arm_front", "brake_caliper_rear"}:
            return None

    best = candidates[0]
    second = candidates[1] if len(candidates) > 1 else None

    if best["score"] < MIN_MATCH_SCORE:
        return None

    # Dacă două variante sunt aproape la scor, nu alegem automat.
    if second and (best["score"] - second["score"]) < MIN_MARGIN_TO_SECOND:
        # Acceptăm totuși dacă textul operației este aproape identic și timpul este același.
        same_time = abs(best["technical_hours"] - second["technical_hours"]) < 0.01
        if not same_time:
            return None

    return best


def tecrmi_row_matches_request(operation: dict, row_text: str) -> bool:
    """Reject TecRMI rows for the wrong axle or number of sides."""
    operation_id = str(operation.get("id") or "")
    text = normalize(row_text)
    requested = normalize(" ".join(str(operation.get(key) or "")
                                   for key in ("canonical", "matched_alias")))
    # Fuzzy similarity alone is unsafe for workshop operations (for example,
    # "bieleta" previously matched "ventilatie").  Require the physical part
    # and the requested axle to be present in the TecRMI description.
    strict_terms = {
        "brake_pads_front": lambda t: "placut" in t and "fran" in t and "fata" in t,
        "brake_pads_rear": lambda t: "placut" in t and "fran" in t and "spate" in t,
        "brake_discs_front": lambda t: "disc" in t and "fran" in t and "fata" in t,
        "brake_discs_rear": lambda t: "disc" in t and "fran" in t and "spate" in t,
        "shock_front": lambda t: ("amortiz" in t or "socuri" in t) and "fata" in t,
        "shock_rear": lambda t: ("amortiz" in t or "socuri" in t) and "spate" in t,
        "stabilizer_link_front": lambda t: (
            any(word in t for word in ("stabiliz", "antiruliu"))
            and any(word in t for word in ("bielet", "tija", "articul"))
            and "fata" in t
        ),
        "drive_belt": lambda t: (
            "curea" in t
            and any(word in t for word in ("accesor", "agregat", "auxiliar", "antrenare"))
            and not any(word in t for word in ("rola", "intinzator", "tension"))
        ),
        "oil_filter": lambda t: "filtru" in t and "ulei" in t,
        "air_filter": lambda t: "filtru" in t and "aer" in t,
        "fuel_filter": lambda t: (
            "filtru" in t
            and any(word in t for word in ("combust", "motorina", "carburant"))
            # A routine filter replacement must not become a fuel-pump/module
            # replacement on petrol vehicles with an integrated filter.
            and not any(word in t for word in (
                "pompa de combustibil", "pompa combustibil", "unitate formata",
                "modul combustibil", "ansamblu pompa",
            ))
        ),
        "cabin_filter": lambda t: "filtr" in t and any(word in t for word in ("habitaclu", "polen", "cabina", "spatiului")),
        "engine_oil": lambda t: "ulei" in t and "motor" in t,
    }
    rule = strict_terms.get(operation_id)
    if rule and not rule(text):
        return False
    # Disc and shock requests in the dashboard refer to the pair on the axle.
    # A one-side catalogue row must never be billed as the complete operation.
    if operation_id in {"brake_discs_front", "brake_discs_rear", "shock_front", "shock_rear"}:
        if any(side in text for side in ("stanga", "dreapta")) and not any(
            both in text for both in ("ambele", "ambelor", "toate", "set")
        ):
            return False
    if operation_id == "stabilizer_link_front" and not any(
        side in requested for side in ("stanga", "dreapta")
    ):
        if any(side in text for side in ("stanga", "dreapta")) and not any(
            both in text for both in ("ambele", "ambelor")
        ):
            return False
    if operation_id in {"tie_rod", "axial_tie_rod", "lower_arm_front"}:
        if any(token in text for token in ("spate", "posterioara", "posterior")):
            return False
    if operation_id == "brake_caliper_rear":
        if "spate" not in text:
            return False
        if "stanga" in requested:
            return "stanga" in text and not any(token in text for token in ("ambele", "ambelor"))
        if "dreapta" in requested:
            return "dreapta" in text and not any(token in text for token in ("ambele", "ambelor"))
    if operation_id == "wheel_alignment_front":
        return "spate" not in text and "posterio" not in text
    return True

def save(path: Path, data: dict):
    return write_worker_result(path, data, os.getenv('ALBATROS_WORKER_RUN_ID'))

async def _cdp_call(ws, ident, method, params=None):
    await ws.send(json.dumps({"id": ident, "method": method, "params": params or {}}))
    while True:
        reply = json.loads(await ws.recv())
        if reply.get("id") == ident:
            return reply

async def _haynes_drivetrain_conflict(request_id: int, offer_result_path: str) -> dict | None:
    """Read the Inter Cars maintenance qualifier without saving an offer."""
    if not offer_result_path:
        return None
    offer_path = Path(offer_result_path)
    auto_path = offer_path.with_name(f"request_{request_id}_autototal.json")
    try:
        auto = json.loads(auto_path.read_text(encoding="utf-8"))
        drivetrain = str((auto.get("vehicle") or {}).get("drivetrain") or "").lower()
        offer = json.loads(offer_path.read_text(encoding="utf-8"))
        vehicle_url = str(((offer.get("vehicle") or {}).get("url")) or "")
    except Exception:
        return None
    if drivetrain not in {"4x2", "4x4"} or "intercars.eu" not in vehicle_url:
        return None
    targets = json.load(urlopen(f"{CDP_URL}/json", timeout=5))
    target = next((x for x in targets if x.get("type") == "page" and "intercars.eu" in x.get("url", "")), None)
    if not target:
        return None
    async with websockets.connect(target["webSocketDebuggerUrl"], max_size=16_000_000) as ws:
        await _cdp_call(ws, 1, "Page.navigate", {"url": vehicle_url})
        await asyncio.sleep(4)
        click = r'''(()=>{const x=[...document.querySelectorAll('a.vehicleinfo__addmaintenance')].find(x=>x.offsetParent);if(!x)return false;x.click();return true})()'''
        await _cdp_call(ws, 2, "Runtime.evaluate", {"expression": click, "returnByValue": True})
        qualifier = ""
        deadline = time.monotonic() + 20
        ident = 10
        while time.monotonic() < deadline:
            await asyncio.sleep(1)
            expr = r'''(()=>{const x=[...document.querySelectorAll('.js-maintenance-service-popup')].find(x=>x.offsetParent);return x?(x.innerText||''):''})()'''
            reply = await _cdp_call(ws, ident, "Runtime.evaluate", {"expression": expr, "returnByValue": True})
            ident += 1
            qualifier = str(reply.get("result", {}).get("result", {}).get("value") or "")
            if qualifier:
                break
        shown_4x4 = bool(re.search(r"\b(?:4WD|4X4|4MATIC|AWD)\b", qualifier, re.I))
        shown_4x2 = bool(re.search(r"\b(?:4X2|2WD|RWD|FWD)\b", qualifier, re.I))
        if drivetrain == "4x2" and shown_4x4 and not shown_4x2:
            return {"autototal": "4x2", "haynes": "4WD", "qualifier": qualifier.strip()[:500]}
        if drivetrain == "4x4" and shown_4x2 and not shown_4x4:
            return {"autototal": "4x4", "haynes": "4x2", "qualifier": qualifier.strip()[:500]}
    return None


async def _autototal_repair_times(request_id: int, offer_result_path: str,
                                  operations: list[dict]) -> dict | None:
    """Read TecRMI repair times from AutoTotal for its VIN-selected exact vehicle."""
    if not offer_result_path:
        return None
    auto_path = Path(offer_result_path).with_name(f"request_{request_id}_autototal.json")
    try:
        auto = json.loads(auto_path.read_text(encoding="utf-8"))
        vehicle = auto.get("vehicle") or {}
        type_id = str(vehicle.get("autototal_type_id") or "")
    except Exception:
        return None
    if not type_id:
        return None
    targets = json.load(urlopen(f"{CDP_URL}/json", timeout=5))
    target = next((x for x in targets if x.get("type") == "page" and "eoriginal.ro" in x.get("url", "")), None)
    if not target:
        return None
    async with websockets.connect(target["webSocketDebuggerUrl"], max_size=32_000_000) as ws:
        tree_expr = """(async()=>JSON.stringify((await axios.post('/car/repair/getRepair',{typeID:%s})).data?.result?.categs||[]))()""" % json.dumps(type_id)
        tree_reply = await _cdp_call(ws, 1000, "Runtime.evaluate", {
            "expression": tree_expr, "awaitPromise": True, "returnByValue": True})
        tree_json = tree_reply.get("result", {}).get("result", {}).get("value") or "[]"
        tree = json.loads(tree_json)
        capacity_expr = """(async()=>JSON.stringify((await axios.post('/car/capacitati-umplere',{typeID:%s})).data?.result?.engOil||{}))()""" % json.dumps(type_id)
        capacity_reply = await _cdp_call(ws, 1001, "Runtime.evaluate", {
            "expression": capacity_expr, "awaitPromise": True, "returnByValue": True})
        engine_oil = json.loads(capacity_reply.get("result", {}).get("result", {}).get("value") or "{}")
        engine_oil_liters = float((engine_oil.get("engine_capacity") or {}).get("value") or 0)
        oil_options = []
        for option in engine_oil.get("engine_oil_options") or []:
            classifications = [str(x.get("value") or "") for x in option.get("classifications") or [] if x.get("value")]
            grades = [str(x.get("value") or "") for x in option.get("grades") or [] if x.get("value")]
            if classifications or grades:
                oil_options.append({"classifications": classifications, "grades": grades})
        preferred_oil = next((x for x in oil_options
                              if "5W-30" in x["grades"] and any("229.51" in c for c in x["classifications"])),
                             oil_options[0] if oil_options else None)
        oil_specification = ""
        if preferred_oil:
            oil_specification = " / ".join(preferred_oil["classifications"])
            if preferred_oil["grades"]:
                oil_specification += " · SAE " + " sau ".join(preferred_oil["grades"])

        leaves = []
        def walk(nodes, path=None, main_id=None, sub_id=None):
            for node in nodes or []:
                text = str(node.get("text") or "")
                current_path = [*(path or []), text] if text else list(path or [])
                # TecRMI child nodes often contain the keys with a null value;
                # preserve the parent's identifiers instead of replacing them.
                current_main = node.get("mainGroupIdField") or main_id
                current_sub = node.get("subGroupIdField") or sub_id
                if node.get("itemMpIdField") is not None and node.get("korIdField") is not None:
                    leaves.append({
                        "text": " > ".join(current_path),
                        "main_id": current_main,
                        "sub_id": current_sub,
                        "kor_id": node.get("korIdField"),
                        "item_id": node.get("itemMpIdField"),
                    })
                walk(node.get("children") or [], current_path, current_main, current_sub)
        walk(tree)

        preferred_text = {
            "engine_oil": "ulei de motor inlocuire",
            "oil_filter": "filtru de ulei de motor inlocuire",
            "air_filter": "filtru de aer inlocuire",
            "fuel_filter": "filtru de combustibil inlocuire",
            "cabin_filter": "set de filtre pentru habitaclu inlocuire",
            "brake_pads_front": "placute de frana fata inlocuire",
            "brake_pads_rear": "placute de frana spate inlocuire",
            "brake_discs_front": "discuri de frana fata inlocuire",
            "brake_discs_rear": "discuri de frana spate inlocuire",
            "shock_front": "amortizor de socuri punte fata ambele laturi inlocuire",
            "shock_rear": "amortizor de socuri punte spate ambele laturi inlocuire",
            "tie_rod": "capat de bara directie inlocuire",
            "axial_tie_rod": "bieleta axiala de directie inlocuire",
            "ball_joint_front": "pivot suspensie fata inlocuire",
            "stabilizer_link_front": "bieleta bara stabilizatoare fata inlocuire",
            "wheel_alignment_front": "reglaj geometrie directie fata",
        }
        resolved = []
        unresolved = []
        call_id = 1100
        for op in operations:
            target_text = preferred_text.get(op["id"], op.get("canonical") or "")
            exact_leaves = [leaf for leaf in leaves
                            if normalize(leaf["text"].split(" > ")[-1]) == normalize(target_text)]
            candidates = exact_leaves or leaves
            # Apply the same semantic guard before fuzzy ranking the TecRMI
            # tree.  This prevents a high character similarity from selecting
            # a different component family.
            semantic_candidates = [leaf for leaf in candidates
                                   if tecrmi_row_matches_request(op, leaf["text"])]
            candidates = semantic_candidates
            if op["id"] == "cabin_filter":
                # "filtru" alone is not sufficient: TecRMI also contains
                # particulate-filter and lambda-sensor operations. Accept only
                # explicit cabin/pollen/passenger-compartment wording.
                candidates = [leaf for leaf in candidates
                              if any(token in normalize(leaf["text"])
                                     for token in ("habitaclu", "polen", "cabina", "spatiului"))
                              and not any(token in normalize(leaf["text"])
                                          for token in ("particule", "lambda", "calamina"))]
            ranked = sorted(
                ((score_haynes(target_text, leaf["text"]), leaf) for leaf in candidates),
                key=lambda pair: pair[0], reverse=True)
            if not ranked or ranked[0][0] < MIN_MATCH_SCORE:
                unresolved.append(op)
                continue
            leaf = ranked[0][1]
            time_expr = """(async()=>JSON.stringify((await axios.post('/car/repair/getRepairTime',{
              typeID:%s,cID:%s,kID:%s,iID:%s
            })).data?.result||[]))()""" % (
                json.dumps(type_id), json.dumps(leaf["main_id"]),
                json.dumps(leaf["kor_id"]), json.dumps(leaf["item_id"]))
            reply = await _cdp_call(ws, call_id, "Runtime.evaluate", {
                "expression": time_expr, "awaitPromise": True, "returnByValue": True})
            call_id += 1
            rows = json.loads(reply.get("result", {}).get("result", {}).get("value") or "[]")
            safe_rows = [
                row for row in rows
                if float(row.get("workTimeField") or 0) > 0
                and tecrmi_row_matches_request(
                    op, row.get("full_text") or row.get("workTextField") or leaf["text"]
                )
            ]
            if not safe_rows:
                unresolved.append(op)
                continue
            # When the catalogue exposes several valid variants, use the
            # largest allowance, as requested by the workshop.
            row = max(safe_rows, key=lambda value: float(value.get("workTimeField") or 0))
            resolved.append({
                **op,
                "haynes_operation": row.get("full_text") or row.get("workTextField") or leaf["text"],
                "technical_hours": float(row["workTimeField"]),
                "haynes_score": round(ranked[0][0], 3),
                "matched_haynes_query": target_text,
                "autototal_work_id": row.get("workIdField"),
            })
        return {"resolved": resolved, "unresolved": unresolved, "type_id": type_id,
                "vehicle": vehicle, "engine_oil_liters": engine_oil_liters,
                "engine_oil_specification": oil_specification,
                "engine_oil_options": oil_options}


def _deduplicate_overlapping_operations(resolved: list[dict]) -> list[dict]:
    """Return billable operations without charging overlapping catalogue times twice."""
    result = list(resolved)
    engine_row = next((row for row in result if row.get("id") == "engine_oil"), None)
    filter_row = next((row for row in result if row.get("id") == "oil_filter"), None)
    if engine_row and filter_row:
        engine_hours = float(engine_row.get("technical_hours") or 0)
        filter_hours = float(filter_row.get("technical_hours") or 0)
        # During a revision, draining/refilling the oil and replacing its filter
        # are one overlapping service step. Keep the longer catalogue allowance.
        # On equal values keep the filter row, which is the historic DSM operation.
        remove_id = "oil_filter" if engine_hours > filter_hours else "engine_oil"
        result = [row for row in result if row.get("id") != remove_id]
    ids = {row.get("id") for row in result}
    if {"brake_discs_front", "brake_pads_front"}.issubset(ids):
        result = [row for row in result if row.get("id") != "brake_pads_front"]
    if {"brake_discs_rear", "brake_pads_rear"}.issubset(ids):
        result = [row for row in result if row.get("id") != "brake_pads_rear"]
    return result


def _save_ready_labour(output: Path, request_id: int, vin: str, request_text: str,
                       operations: list[dict], resolved: list[dict], source: str,
                       technical_details: dict | None = None) -> None:
    resolved_for_sum = _deduplicate_overlapping_operations(resolved)
    technical_total = round(sum(x["technical_hours"] for x in resolved_for_sum), 2)
    expected = round(technical_total + ALBATROS_BUFFER_HOURS, 1)
    is_revision = any(x.get("id") in {
        "engine_oil", "oil_filter", "air_filter", "cabin_filter", "fuel_filter"
    } for x in operations)
    booking_hours = round(expected + (
        REVISION_BOOKING_EXTRA_HOURS if is_revision else BOOKING_EXTRA_HOURS
    ), 1)
    details = technical_details or {}
    save(output, {
        "status": "ready", "request_id": request_id, "vin": vin, "symptom": request_text,
        "detected_operations": operations, "resolved_operations": resolved,
        "summed_operations": [x["id"] for x in resolved_for_sum],
        "operation": " + ".join(x["haynes_operation"] for x in resolved),
        "technical_hours": technical_total, "albatros_expected_hours": expected,
        "hourly_rate_ron": HOURLY_RATE_RON,
        "labour_price_ron": round(expected * HOURLY_RATE_RON, 2),
        "booking_hours": booking_hours,
        "source": source,
        "engine_oil_liters": details.get("engine_oil_liters") or 0,
        "engine_oil_specification": details.get("engine_oil_specification") or "",
        "engine_oil_options": details.get("engine_oil_options") or [],
        "message": "Timpii au fost citiți pentru varianta exactă identificată după VIN.",
    })

def _wait_for_autototal_identification(request_id: int, offer_result_path: str,
                                       timeout_seconds: float = 300.0) -> bool:
    """Wait for AutoTotal's exact TecDoc type, not merely an interim status.

    OE-first searches can legitimately take more than 90 seconds.  Returning
    on a partial payload without ``autototal_type_id`` made TecRMI declare
    itself unavailable just before AutoTotal completed identification.
    """
    if not offer_result_path:
        return False
    auto_path = Path(offer_result_path).with_name(f"request_{request_id}_autototal.json")
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        try:
            payload = json.loads(auto_path.read_text(encoding="utf-8"))
            vehicle = payload.get("vehicle") or {}
            if vehicle.get("autototal_type_id"):
                return True
            if payload.get("status") in {
                "error", "unavailable"
            }:
                return False
        except Exception:
            pass
        time.sleep(0.5)
    return False

def main():
    if len(sys.argv) < 5:
        print("Usage: technician_request_worker_v6_time_parse_fix.py REQUEST_ID VIN REQUEST_TEXT OUTPUT_JSON")
        raise SystemExit(2)

    request_id = int(sys.argv[1])
    vin = sys.argv[2].strip()
    request_text = sys.argv[3].strip()
    output = Path(sys.argv[4])
    offer_result_path = sys.argv[5].strip() if len(sys.argv) > 5 else ""

    operations = detect_operations(request_text)

    if not operations:
        save(output, {
            "status": "needs_verification",
            "request_id": request_id,
            "vin": vin,
            "symptom": request_text,
            "detected_operations": [],
            "message": "Nu am putut asocia suficient de sigur solicitarea cu o operație tehnică standard.",
            "source": "HaynesPro / WorkshopData",
        })
        return

    try:
        drivetrain_conflict = asyncio.run(_haynes_drivetrain_conflict(request_id, offer_result_path))
    except Exception:
        drivetrain_conflict = None
    if drivetrain_conflict:
        save(output, {
            "status": "needs_verification",
            "request_id": request_id,
            "vin": vin,
            "symptom": request_text,
            "detected_operations": operations,
            "vehicle_conflict": drivetrain_conflict,
            "message": "AutoTotal a identificat vehiculul 4x2, dar Haynes din Inter Cars oferă numai varianta 4WD. Nu am selectat și nu am salvat varianta greșită.",
            "source": "AutoTotal + HaynesPro integrat în Inter Cars",
        })
        return

    # AutoTotal and Inter Cars are asynchronous. If AutoTotal cannot resolve
    # the exact type, Inter Cars still finishes by VIN; wait for that fallback
    # before attempting integrated Haynes maintenance.
    offer_state = wait_for_offer_products(offer_result_path)
    if offer_state.get("status") not in {
            "ready", "partial", "error", "needs_vehicle_data", "failed"}:
        save(output, {
            "status": "error",
            "request_id": request_id,
            "vin": vin,
            "symptom": request_text,
            "detected_operations": operations,
            "message": (
                "Căutarea pieselor nu a ajuns într-o stare finală în intervalul permis; "
                "workerul de timpi nu a atins fila Inter Cars, pentru a evita interferența."
            ),
            "source": "HaynesPro integrat în Inter Cars",
        })
        return

    try:
        asyncio.run(ensure_autototal_responsive())
        with sync_playwright() as p:
            browser = p.chromium.connect_over_cdp(CDP_URL, timeout=15000)
            intercars_page = find_intercars_page(browser)
            products = load_offer_products(offer_result_path)
            revision_ids = {"engine_oil", "oil_filter", "air_filter", "cabin_filter", "fuel_filter"}
            revision_operations = [op for op in operations if op.get("id") in revision_ids]
            maintenance = None
            if intercars_page and revision_operations:
                maintenance = intercars_revision_maintenance(
                    intercars_page, offer_result_path, request_id, vin, revision_operations)
                if (maintenance and not maintenance.get("unresolved")
                        and len(revision_operations) == len(operations)):
                    _save_ready_labour(
                        output, request_id, vin, request_text, operations,
                        maintenance["resolved"],
                        "HaynesPro integrat în Inter Cars — Servicii de mentenanță",
                        maintenance,
                    )
                    return
            operation_snapshots = {}
            if intercars_page and products:
                for op in operations:
                    product = intercars_product_for_operation(products, op["id"])
                    if product:
                        snap = intercars_snapshot_for_operation(intercars_page, product)
                        if snap:
                            operation_snapshots[op["id"]] = snap

            page = find_haynes_page(browser)

            if not operation_snapshots and not page:
                save(output, {
                    "status": "error",
                    "request_id": request_id,
                    "vin": vin,
                    "symptom": request_text,
                    "detected_operations": operations,
                    "message": "Nu am găsit timpii integrați în Inter Cars și nici HaynesPro / WorkshopData deschis în Edge.",
                })
                return

            snapshot = haynes_text_snapshot(page) if page else []

            if not operation_snapshots and not snapshot:
                save(output, {
                    "status": "error",
                    "request_id": request_id,
                    "vin": vin,
                    "symptom": request_text,
                    "detected_operations": operations,
                    "message": "Pagina Haynes este deschisă, dar nu am putut citi conținutul paginii de timpi.",
                    "source": "HaynesPro / WorkshopData",
                })
                return

            resolved = list((maintenance or {}).get("resolved") or [])
            unresolved = list((maintenance or {}).get("unresolved") or [])
            resolved_ids_from_maintenance = {row.get("id") for row in resolved}

            for op in operations:
                if op.get("id") in resolved_ids_from_maintenance:
                    continue
                candidates = extract_candidates_for_operation(operation_snapshots.get(op["id"], snapshot), op)
                chosen = choose_safe_candidate(candidates, op["id"])

                if not chosen:
                    unresolved.append({
                        **op,
                        "alternatives": [
                            {
                                "score": round(x["score"], 3),
                                "operation": x["operation"],
                                "time": x["time_text"],
                            }
                            for x in candidates[:5]
                        ],
                    })
                    continue

                resolved.append({
                    **op,
                    "haynes_operation": chosen["operation"],
                    "technical_hours": chosen["technical_hours"],
                    "haynes_score": round(chosen["score"], 3),
                    "matched_haynes_query": chosen.get("matched_query") or op["canonical"],
                    "alternatives": [
                        {
                            "score": round(x["score"], 3),
                            "operation": x["operation"],
                            "time": x["time_text"],
                        }
                        for x in candidates[:5]
                    ],
                })

            if unresolved:
                save(output, {
                    "status": "needs_verification",
                    "request_id": request_id,
                    "vin": vin,
                    "symptom": request_text,
                    "detected_operations": operations,
                    "resolved_operations": resolved,
                    "unresolved_operations": unresolved,
                    "message": "Am asociat solicitarea, dar cel puțin o operație nu are încă un timp Haynes suficient de sigur.",
                    "source": "HaynesPro integrat în Inter Cars" if operation_snapshots else "HaynesPro / WorkshopData",
                })
                return

            # Calculăm totalul fără dublare evidentă.
            # Exemplu: dacă Haynes dă "schimb ulei motor + filtru ulei" ca o singură operație,
            # nu mai adăugăm încă o dată timpul separat al filtrului de ulei.
            resolved_for_sum = _deduplicate_overlapping_operations(resolved)

            # Înlocuirea ambelor discuri include demontarea/remontarea plăcuțelor
            # de pe aceeași punte. Nu facturăm încă o dată timpul plăcuțelor.
            resolved_ids = {x["id"] for x in resolved}
            if {"brake_discs_front", "brake_pads_front"}.issubset(resolved_ids):
                resolved_for_sum = [x for x in resolved_for_sum if x["id"] != "brake_pads_front"]
            if {"brake_discs_rear", "brake_pads_rear"}.issubset(resolved_ids):
                resolved_for_sum = [x for x in resolved_for_sum if x["id"] != "brake_pads_rear"]

            technical_total = round(sum(x["technical_hours"] for x in resolved_for_sum), 2)

            # Buffer-ul Albatros se aplică o singură dată pe lucrare, nu pe fiecare suboperație.
            expected = round(technical_total + ALBATROS_BUFFER_HOURS, 1)
            labour_price = round(expected * HOURLY_RATE_RON, 2)
            is_revision = any(x.get("id") in {
                "engine_oil", "oil_filter", "air_filter", "cabin_filter", "fuel_filter"
            } for x in operations)
            booking = round(expected + (
                REVISION_BOOKING_EXTRA_HOURS if is_revision else BOOKING_EXTRA_HOURS
            ), 1)

            save(output, {
                "status": "ready",
                "request_id": request_id,
                "vin": vin,
                "symptom": request_text,
                "detected_operations": operations,
                "resolved_operations": resolved,
                "summed_operations": [x["id"] for x in resolved_for_sum],
                "operation": " + ".join(x["haynes_operation"] for x in resolved),
                "technical_hours": technical_total,
                "albatros_expected_hours": expected,
                "hourly_rate_ron": HOURLY_RATE_RON,
                "labour_price_ron": labour_price,
                "booking_hours": booking,
                "engine_oil_liters": (maintenance or {}).get("engine_oil_liters"),
                "engine_oil_specification": (maintenance or {}).get("engine_oil_specification") or "",
                "engine_oil_options": (maintenance or {}).get("engine_oil_options") or [],
                "source": "HaynesPro integrat în Inter Cars" if operation_snapshots else "HaynesPro / WorkshopData",
                "message": "Operațiile au fost asociate din limbaj natural și verificate în Haynes. Timpii independenți au fost însumați fără a inventa suprapuneri.",
            })

    except Exception as exc:
        save(output, {
            "status": "error",
            "request_id": request_id,
            "vin": vin,
            "symptom": request_text,
            "detected_operations": operations,
            "message": f"Robotul Tehnician nu a putut finaliza căutarea: {exc}",
            "source": "HaynesPro / WorkshopData",
        })
        return

if __name__ == "__main__":
    main()
