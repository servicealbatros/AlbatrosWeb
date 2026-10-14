"""Read-only AutoTotal VIN identification worker."""
import asyncio
from datetime import datetime
import json
import math
import os
from pathlib import Path
import re
import sys
import time
import unicodedata
from urllib.request import urlopen

import websockets
from worker_run_guard import write_worker_result
from robot_cdp_health import ensure_autototal_responsive
from automotive_vocabulary import canonical_terms, expand_technical_request
from oe_english_vocabulary import OE_ENGLISH_PARTS, english_oe_terms

CDP_HTTP = "http://127.0.0.1:9222"
BASE = Path(__file__).resolve().parent
DATA_DIR = Path(os.getenv("ALBATROS_DATA_DIR", BASE / "data")).resolve()


# The AutoTotal frontend no longer always exposes axios on ``window``.  Keep
# catalogue calls working across frontend releases by falling back to the
# browser's authenticated, same-origin fetch implementation.
AUTOTOTAL_POST_HELPER_JS = r"""async (url, payload) => {
  if (globalThis.axios && typeof globalThis.axios.post === 'function') {
    return await globalThis.axios.post(url, payload);
  }
  const response = await fetch(url, {
    method: 'POST',
    credentials: 'same-origin',
    headers: {
      'Accept': 'application/json, text/plain, */*',
      'Content-Type': 'application/json'
    },
    body: JSON.stringify(payload || {})
  });
  const raw = await response.text();
  let data = {};
  try { data = raw ? JSON.parse(raw) : {}; }
  catch (_) { throw new Error(`AutoTotal a răspuns invalid (${response.status}).`); }
  if (!response.ok) throw new Error(data?.message || `AutoTotal HTTP ${response.status}`);
  return {data};
}"""


def _post_helper_js() -> str:
    return AUTOTOTAL_POST_HELPER_JS


def _load_vehicle_hints(request_id: int, hints_path: Path | None = None) -> dict:
    # AutoTotal is intentionally started before the Inter Cars job is queued.
    # Therefore the DSM snapshot must be passed directly; reading only the
    # later robot job creates a circular dependency and loses the exact engine.
    if hints_path:
        try:
            hints = json.loads(hints_path.read_text(encoding="utf-8"))
            if isinstance(hints, dict) and hints:
                return hints
        except (OSError, ValueError, TypeError):
            pass
    try:
        jobs = json.loads((DATA_DIR / "robot_offer_jobs.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    for job in jobs if isinstance(jobs, list) else []:
        if int(job.get("request_id") or 0) == int(request_id):
            hints = job.get("vehicle_hints") or {}
            return hints if isinstance(hints, dict) else {}
    return {}


async def _cdp_call(ws, call_id: int, method: str, params: dict | None = None) -> dict:
    await ws.send(json.dumps({"id": call_id, "method": method, "params": params or {}}))
    while True:
        try:
            raw_message = await asyncio.wait_for(ws.recv(), timeout=35)
        except asyncio.TimeoutError as exc:
            raise RuntimeError(
                f"AutoTotal nu a răspuns în 35 secunde la {method}; workerul a fost eliberat controlat."
            ) from exc
        message = json.loads(raw_message)
        if message.get("id") == call_id:
            if message.get("error"):
                raise RuntimeError(str(message["error"]))
            return message


def _autototal_target() -> dict:
    targets = json.load(urlopen(f"{CDP_HTTP}/json", timeout=3))
    pages = [item for item in targets if item.get("type") == "page" and "www3.eoriginal.ro" in item.get("url", "")]
    if not pages:
        raise RuntimeError("Fila AutoTotal nu este deschisă în browserul robotului.")
    return pages[0]


async def _evaluate(ws, call_id: int, expression: str, await_promise: bool = False):
    reply = await _cdp_call(ws, call_id, "Runtime.evaluate", {
        "expression": expression, "awaitPromise": await_promise, "returnByValue": True,
    })
    exception = reply.get("result", {}).get("exceptionDetails")
    if exception:
        details = exception.get("exception", {}) if isinstance(exception, dict) else {}
        description = details.get("description") if isinstance(details, dict) else None
        raise RuntimeError(description or exception.get("text") or "Eroare JavaScript AutoTotal")
    result = reply.get("result", {}).get("result", {})
    if result.get("subtype") == "error":
        raise RuntimeError(result.get("description") or "Eroare JavaScript AutoTotal")
    return result.get("value")


async def _wait_value(ws, expression: str, timeout: float = 20):
    deadline = time.monotonic() + timeout
    call_id = 100
    while time.monotonic() < deadline:
        value = await _evaluate(ws, call_id, expression)
        call_id += 1
        if value:
            return value
        await asyncio.sleep(.4)
    raise RuntimeError("AutoTotal nu a încărcat pagina la timp.")


def save(path: Path, payload: dict) -> None:
    write_worker_result(path, payload, os.getenv('ALBATROS_WORKER_RUN_ID'))


def _json_value(value, default):
    """Accept both JSON strings and CDP values already returned by value."""
    if value in (None, ""):
        return default
    if isinstance(value, (dict, list)):
        return value
    return json.loads(value)


def _ambiguous_front_arms(value: str) -> bool:
    text = unicodedata.normalize("NFKD", str(value or "").casefold()).encode("ascii", "ignore").decode("ascii")
    asks_arm = bool(re.search(r"\bbrat(?:e|ele)?\b|\bbascul", text))
    explicit = any(term in text for term in (
        "superior", "inferior", "transversal", "longitudinal", "stanga", "dreapta",
        "bucsa brat", "bucsa bascula", "bascula stanga", "bascula dreapta",
    ))
    return asks_arm and not explicit and bool(re.search(r"\b(?:brate|bratele|toate)\b", text))


def _load_intercars_candidates(output: Path, query: str) -> list[dict]:
    path = output.with_name(output.name.replace("_autototal.json", "_products.json"))
    if not path.exists():
        return []


FILTER_BRANDS = ("MANN-FILTER", "BOSCH", "MAHLE", "HENGST", "PURFLUX")
MECHANICAL_BRANDS = ("TRW", "DELPHI", "LEMFORDER")
BELT_BRANDS = ("CONTITECH", "DAYCO")
ENGINE_OIL_BRANDS = ("CASTROL", "MOTUL", "TEXACO", "HAVOLINE")
# Prefer the established belt/bearing manufacturers.  The remaining names are
# used only for rollers/tensioners when the preferred set has no application
# for the VIN; the belt itself stays restricted to ContiTech and Dayco.
ACCESSORY_KIT_BRANDS = (
    "INA", "SKF", "GATES", "CONTITECH", "DAYCO",
    "SNR", "FEBI", "SWAG", "BTA", "OPTIMAL",
)
BRAND_PREFIXES = {
    "MANN-FILTER": ("MANN-FILTER",),
    "BOSCH": ("BOSCH",),
    "MAHLE": ("MAHLE ORIGINAL", "MAHLE"),
    "HENGST": ("HENGST FILTER", "HENGST"),
    "PURFLUX": ("PURFLUX",),
    "TRW": ("TRW",),
    "DELPHI": ("DELPHI",),
    "LEMFORDER": ("LEMFÖRDER", "LEMFORDER"),
    "CONTITECH": ("CONTITECH",),
    "DAYCO": ("DAYCO",),
    "GATES": ("GATES",),
    "INA": ("INA",),
    "SKF": ("SKF",),
    "SNR": ("SNR",),
    "FEBI": ("FEBI BILSTEIN", "FEBI"),
    "SWAG": ("SWAG",),
    "BTA": ("BTA",),
    "OPTIMAL": ("OPTIMAL",),
}


def _independent_plan(query: str) -> list[dict]:
    query = expand_technical_request(query)
    text = unicodedata.normalize("NFKD", query.casefold()).encode("ascii", "ignore").decode("ascii")
    plan = []
    if "revizie" in text or "filtre" in text or "filtru" in text:
        plan.extend([
            {"requested_item": "filtru ulei", "search": "filtru ulei", "kind": r"FILTRU\s+ULEI"},
            {"requested_item": "filtru aer", "search": "filtru aer", "kind": r"FILTRU\s+AER"},
            {"requested_item": "filtru combustibil", "search": "filtru combustibil", "kind": r"FILTRU\s+(?:COMBUSTIBIL|MOTORINA|CARBURANT)"},
            {"requested_item": "filtru habitaclu", "search": "filtru habitaclu", "kind": r"FILTRU\s+(?:HABITACLU|POLEN|CABINA)"},
            {
                "requested_item": "bușon baie ulei",
                "search": "șurub de golire baia de ulei",
                "category_search": "lubrifiere",
                "group_search": "șurub de golire baia de ulei",
                "kind": r"(?:SURUB|BUSON).*?(?:GOLIRE|BAIA?\s+DE\s+ULEI)",
                # The exact OE code is resolved from the VIN first. Once the
                # code is known, any same-part aftermarket equivalent may be
                # offered when OE is not available.
                "allow_any_brand": True,
            },
        ])
    if re.search(r"\bpivo(?:t|ti|ți)\b|rotul[aă]\s+suspensie", text, re.I):
        plan.append({"requested_item": "pivot suspensie", "search": "pivot", "kind": r"(?:PIVOT|ARTICULATIE.*SARCINA|ROTULA.*SUSPENSIE)", "brands": MECHANICAL_BRANDS})
    if re.search(r"cap(?:at|ete)?\s+(?:de\s+)?bar[aă]", text, re.I):
        plan.append({"requested_item": "cap de bară direcție", "search": "cap de bara", "kind": r"CAP(?:AT)?\s+(?:DE\s+)?BARA|ARTICULATIE.*DIRECTIE", "brands": MECHANICAL_BRANDS})
    if re.search(r"bielet[aă]\s+(?:axial[aă]|(?:de\s+)?direcție|(?:de\s+)?directie)|bar[aă]\s+axial[aă]", text, re.I):
        plan.append({"requested_item": "bieletă axială direcție", "search": "bieleta directie", "kind": r"BIELETA.*DIRECTIE|BARA.*AXIALA|ARTICULATIE.*AXIALA", "brands": MECHANICAL_BRANDS})
    if re.search(r"bielet[aă].*(?:stabiliz|antiruliu)|(?:stabiliz|antiruliu).*bielet[aă]", text, re.I):
        plan.append({"requested_item": "bieletă bară stabilizatoare", "search": "bieleta antiruliu", "kind": r"BIELETA.*(?:ANTIRULIU|STABILIZ)|TIJA.*ANTIRULIU|SWING\s+SUPPORT", "brands": MECHANICAL_BRANDS})
    if (re.search(r"\bbascul[aă]\b|bra[țt]\s+(?:de\s+)?suspensie", text, re.I)
            and not _ambiguous_front_arms(query)):
        plan.append({"requested_item": "basculă suspensie", "search": "brat suspensie", "kind": r"(?:BRAT|BASCULA).*SUSPENSIE|BRAT.*TRANSVERSAL", "brands": MECHANICAL_BRANDS})
    if re.search(r"pl[aă]cu[țt].*(?:spate|posterior)|(?:spate|posterior).*pl[aă]cu[țt]", text, re.I):
        plan.append({"requested_item": "plăcuțe frână spate", "search": "placute frana", "kind": r"PLACUTE.*FRANA", "brands": ("TEXTAR", "TRW")})
    if re.search(r"pl[aă]cu[țt].*(?:fa[țt][aă]|anterior)|(?:fa[țt][aă]|anterior).*pl[aă]cu[țt]", text, re.I):
        plan.append({"requested_item": "plăcuțe frână față", "search": "placute frana", "kind": r"PLACUTE.*FRANA", "brands": ("TEXTAR", "TRW")})
    if re.search(r"disc(?:uri)?.*(?:fa[țt][aă]|anterior)|(?:fa[țt][aă]|anterior).*disc", text, re.I):
        plan.append({"requested_item": "discuri frână față", "search": "disc frana", "kind": r"DISC(?:URI)?\s+FRANA", "brands": ("TEXTAR", "TRW")})
    if re.search(r"etrier.*(?:spate|posterior)|(?:spate|posterior).*etrier", text, re.I):
        plan.append({"requested_item": "etrier frână spate", "search": "etrier frana", "kind": r"ETRIER.*FRANA", "brands": ("TEXTAR", "TRW")})
    accessory_belt_requested = (
        "curea" in text
        and any(word in text for word in ("accesor", "acessor", "agregat", "auxiliar"))
    )
    accessory_kit_requested = accessory_belt_requested and "kit" in text
    if accessory_belt_requested:
        plan.append({
            "requested_item": "curea accesorii",
            "search": "curea transmisie cu caneluri",
            "kind": r"CUREA\s+TRANSMISIE\s+CU\s+CANELURI",
            "brands": BELT_BRANDS,
        })
    if accessory_kit_requested:
        # AutoTotal exposes every component in a distinct standard-catalogue
        # group.  A search for the belt alone therefore cannot complete a kit.
        plan.extend([
            {
                "requested_item": "kit curea accesorii complet",
                "search": "set curea transmisie cu caneluri",
                "category_search": "curea transmisie cu caneluri",
                "group_search": "set curea transmisie cu caneluri",
                "kind": r"SET\s+CUREA\s+TRANSMISIE\s+CU\s+CANELURI|KIT.*CUREA",
                "brands": ACCESSORY_KIT_BRANDS,
            },
            {
                "requested_item": "rolă ghidare curea accesorii",
                "search": "rola ghidare conducatoare",
                "category_search": "curea transmisie cu caneluri",
                "group_search": "rola ghidare conducatoare",
                "kind": r"ROLA\s+(?:GHIDARE|CONDUCATOARE|CONDUCERE)|ROLA.*CUREA",
                "brands": ACCESSORY_KIT_BRANDS,
            },
            {
                "requested_item": "întinzător curea accesorii",
                "search": "rola intinzatoare",
                "category_search": "curea transmisie cu caneluri",
                "group_search": "rola intinzatoare",
                "kind": r"(?:ROLA\s+INTINZATOARE|INTINZATOR|DISPOZITIV.*INTINDERE)",
                "brands": ACCESSORY_KIT_BRANDS,
            },
            {
                "requested_item": "pompă de apă",
                "search": "pompa apa",
                "category_search": "pompa apa garnitura",
                "group_search": "pompa apa",
                "kind": r"POMPA\s+APA",
                "brands": ACCESSORY_KIT_BRANDS,
            },
        ])
    return plan


async def _discover_candidate(ws, item: dict, call_id: int) -> dict | None:
    raise RuntimeError("_discover_candidate necesită catalogul standard AutoTotal")


def _type_detail(type_row: dict, key_id: int) -> list[str]:
    return [str(x.get("addInfoKeyValueField") or "") for x in type_row.get("typeDetailsField") or []
            if x.get("addInfoKeyIdField") == key_id]


def _select_vehicle_type(types: list[dict], lx_name: str, engine: str, drivetrain: str,
                         year: int | None, power_kw: int | None = None) -> dict | None:
    """Select the TecDoc type using only VIN data returned independently by AutoTotal."""
    # A VIN response with a single TecDoc type is already unambiguous even if
    # the visible page omits engine/year fields (common on PSA/Stellantis).
    if len(types or []) == 1:
        return types[0]
    model_number = (re.search(r"\b(\d{3})\s*(?:KB|CDI)?\b", lx_name or "", re.I) or [None, ""])[1]
    normalized_engine = re.sub(r"[^A-Z0-9]", "", (engine or "").upper())
    ranked = []
    for row in types or []:
        name = str(row.get("typeNameField") or "")
        score = 0
        if model_number and re.search(rf"^\s*{re.escape(model_number)}\b", name):
            score += 100
        engines = _type_detail(row, -4)
        exact_engine = bool(normalized_engine) and any(
            normalized_engine == re.sub(r"[^A-Z0-9]", "", x.upper())
            or normalized_engine.endswith(re.sub(r"[^A-Z0-9]", "", x.upper()))
            or re.sub(r"[^A-Z0-9]", "", x.upper()).endswith(normalized_engine)
            for x in engines
        )
        if exact_engine:
            score += 80
        elif normalized_engine and any(
            normalized_engine[:3] == re.sub(r"[^A-Z0-9]", "", x.upper())[:3]
            for x in engines
            if len(re.sub(r"[^A-Z0-9]", "", x.upper())) >= 3
        ):
            # VIN decoders and TecDoc may use different suffixes for the same
            # engine family (for example BMW N57Z vs N57 D30 B).
            score += 40
        candidate_powers = [int(x) for x in _type_detail(row, 5) if str(x).isdigit()]
        if power_kw is not None and candidate_powers:
            power_delta = min(abs(power_kw - value) for value in candidate_powers)
            # DSM and TecDoc sometimes differ by 1 kW through conversion or
            # rounding. Accept that only when the engine code is an exact VIN match.
            score += 100 if power_delta == 0 or (power_delta == 1 and exact_engine) else -250
        is_van = bool(re.search(r"\bVAN\b", name, re.I))
        vin_says_van = bool(re.search(r"\bVAN\b", lx_name or "", re.I))
        if is_van != vin_says_van:
            score -= 200
        is_4x4 = bool(re.search(r"(?:\b(?:4X4|4WD|AWD|4MATIC|4MOTION)\b|XDRIVE)", name, re.I))
        if drivetrain == "4x2":
            score += 40 if not is_4x4 else -200
        elif drivetrain == "4x4":
            score += 40 if is_4x4 else -100
        starts = [int(x[:4]) for x in _type_detail(row, 3) if re.fullmatch(r"\d{6}", x)]
        ends = [int(x[:4]) for x in _type_detail(row, 4) if re.fullmatch(r"\d{6}", x)]
        if year:
            if starts and year < min(starts):
                score -= 50
            elif ends and year > max(ends):
                score -= 50
            else:
                score += 10
        ranked.append((score, row))
    ranked.sort(key=lambda pair: pair[0], reverse=True)
    return ranked[0][1] if ranked and ranked[0][0] >= 100 else None


def _resolve_unique_power_from_types(types: list[dict], engine: str,
                                     year: int | None = None) -> dict:
    """Resolve kW only when every exact-engine, date-compatible type agrees."""
    normalized_engine = re.sub(r"[^A-Z0-9]", "", str(engine or "").upper())
    if not normalized_engine:
        return {"status": "insufficient_vehicle_data", "power_candidates_kw": []}
    powers: set[int] = set()
    matching_type_ids: list[str] = []
    for row in types or []:
        engines = {
            re.sub(r"[^A-Z0-9]", "", value.upper())
            for value in _type_detail(row, -4) if str(value).strip()
        }
        if normalized_engine not in engines:
            continue
        starts = [int(x[:4]) for x in _type_detail(row, 3) if re.fullmatch(r"\d{6}", x)]
        ends = [int(x[:4]) for x in _type_detail(row, 4) if re.fullmatch(r"\d{6}", x)]
        if year and ((starts and year < min(starts)) or (ends and year > max(ends))):
            continue
        row_powers = {int(x) for x in _type_detail(row, 5) if str(x).isdigit()}
        if not row_powers:
            continue
        powers.update(row_powers)
        matching_type_ids.append(str(row.get("typeIdField") or ""))
    if len(powers) == 1:
        return {
            "status": "resolved",
            "power_kw": next(iter(powers)),
            "engine_code": str(engine or "").upper(),
            "matching_type_ids": matching_type_ids,
            "source": "AutoTotal TecDoc — cod motor exact și putere unică",
        }
    return {
        "status": "ambiguous" if powers else "not_found",
        "engine_code": str(engine or "").upper(),
        "power_candidates_kw": sorted(powers),
        "matching_type_ids": matching_type_ids,
    }


def _oe_vin_targets(query: str) -> list[dict]:
    """Map workshop wording to stable AutoTotal/Laximo VIN catalogue groups."""
    requested_canonicals = canonical_terms(query)
    query = expand_technical_request(query)
    text = unicodedata.normalize("NFKD", query.casefold()).encode("ascii", "ignore").decode("ascii")
    targets = []

    def local_position(part_pattern: str) -> str | None:
        """Read axle only from the words next to this part in a multi-part request."""
        match = re.search(part_pattern, text)
        if not match:
            return None
        start, end = max(0, match.start() - 24), min(len(text), match.end() + 36)
        nearby = text[start:end]
        positions = []
        for position_match in re.finditer(r"\b(fata|anterior|spate|posterior)\b", nearby):
            absolute_start = start + position_match.start()
            absolute_end = start + position_match.end()
            distance = (match.start() - absolute_end if absolute_end <= match.start()
                        else absolute_start - match.end() if absolute_start >= match.end()
                        else 0)
            axle = "front" if position_match.group(1) in {"fata", "anterior"} else "rear"
            positions.append((max(0, distance), absolute_start, axle))
        return min(positions)[2] if positions else None

    accessory_kit_requested = (
        "kit" in text and "curea" in text
        and any(word in text for word in ("accesor", "acessor", "agregat", "auxiliar"))
    )
    if accessory_kit_requested:
        # A kit alias is one customer phrase, but the OE catalogue contains
        # separate VIN rows. Expand it before any aftermarket search so OE
        # codes remain the primary identifiers.
        targets.extend([
            {
                "requested_item": "curea accesorii",
                "group_id": 10531,
                "terms": ("ribbed v-belt", "serpentine belt", "drive belt"),
                "exclude": ("timing",),
                "allowed_brands": ("CONTITECH", "DAYCO", "OE"),
            },
            {
                "requested_item": "întinzător curea accesorii",
                "group_id": 10534,
                "terms": ("belt tensioner", "tensioner pulley", "tensioning device"),
                "exclude": ("timing",),
                "allowed_brands": ACCESSORY_KIT_BRANDS + ("OE",),
            },
            {
                "requested_item": "rolă ghidare curea accesorii",
                # Mercedes places the guide/idler and tensioner in the same
                # auxiliary-drive diagram returned by this VIN group.
                "group_id": 10534,
                "terms": ("idler pulley", "guide pulley", "deflection pulley"),
                "exclude": ("timing",),
                "allowed_brands": ACCESSORY_KIT_BRANDS + ("OE",),
            },
        ])

    if "amortiz" in text:
        shock_position = local_position(r"amortiz\w*")
        rear_only = shock_position == "rear"
        targets.append({"requested_item": "amortizor spate" if rear_only else "amortizor față",
                        "group_id": 10221,
                        "terms": english_oe_terms("amortizor") + ("amortizor",),
                        "exclude": ("tampon", "bucsa", "mount", "bearing", "auxiliary"),
                        "allowed_brands": ("SACHS", "BILSTEIN", "KYB", "OE"),
                        "front": not rear_only, "rear": rear_only})
    if any(term in text for term in ("protectie praf", "burduf", "kit protectie")):
        targets.append({"requested_item": "kit protecție praf amortizor față", "group_id": 10221,
                        "terms": ("burduf", "tub protectie", "tampon limitator"), "front": "fata" in text})
    if "rulment sarcina" in text or "flansa amortiz" in text:
        targets.append({"requested_item": "rulment sarcină față", "group_id": 10471,
                        "terms": ("rulment", "suportul piciorului suspensiei"), "front": "fata" in text})
    if re.search(r"\bpivo(?:t|ti)\b|rotula", text):
        targets.append({"requested_item": "pivot suspensie", "group_id": 10681,
                        "terms": english_oe_terms("pivot suspensie") + ("pivot", "rotula", "articulatie"), "front": True})
    if "bielet" in text and any(term in text for term in ("stabiliz", "antiruliu")):
        link_position = local_position(r"bielet\w*.{0,30}(?:stabiliz|antiruliu)|(?:stabiliz|antiruliu).{0,30}bielet\w*")
        targets.append({"requested_item": "bieletă bară stabilizatoare", "group_id": 10690,
                        # BMW/Laximo calls these parts "Swing support".  Keep
                        # the workshop terms too for other manufacturers.
                        "terms": english_oe_terms("bieletă bară stabilizatoare") + ("bieleta", "tija"),
                        "exclude": ("stabilizer support", "repair kit"),
                        "category_aliases": ("brat/bieleta suspensie, stabilizator", "bieleta antiruliu"),
                        "front": link_position != "rear",
                        "rear": link_position == "rear",
                        "allowed_brands": MECHANICAL_BRANDS + ("OE",)})
    if (re.search(r"\bbascul|\bbrat(?:ele)?(?:\s+(?:de\s+)?suspensie|\s+(?:pe\s+)?fata)?\b", text)
            and "bucsa" not in text and "bara stabiliz" not in text
            and not _ambiguous_front_arms(query)):
        targets.append({"requested_item": "basculă suspensie", "group_id": 10681,
                        "terms": english_oe_terms("braț suspensie") + ("brat transversal", "brat suspensie"),
                        "exclude": ("lager", "bucsa", "suport"),
                        "allowed_brands": MECHANICAL_BRANDS,
                        "front": "spate" not in text,
                        "left": bool(re.search(r"\bstg\b|stanga", text)),
                        "right": bool(re.search(r"\bdr\b|dreapta", text))})
    if re.search(r"cap(?:at)?\s+(?:de\s+)?bara", text):
        targets.append({"requested_item": "cap de bară direcție", "group_id": 10298,
                        "terms": english_oe_terms("capăt de bară direcție") + ("cap bara de directie",),
                        "allowed_brands": MECHANICAL_BRANDS,
                        "left": bool(re.search(r"\bstg\b|stanga", text)),
                        "right": bool(re.search(r"\bdr\b|dreapta", text))})
    # A common workshop formulation is "discuri placute fata spate".  It asks
    # for both axles; choosing only the closest axle silently dropped the rear
    # set from the offer.
    both_brake_axles = bool(re.search(
        r"disc\w*[^,;.]{0,25}placut\w*[^,;.]{0,20}\bfata\b[^,;.]{0,12}\bspate\b",
        text,
    ))
    pad_position = local_position(r"placut\w*") if "placut" in text else None
    if pad_position == "rear" or both_brake_axles:
        targets.append({"requested_item": "plăcuțe frână spate", "group_id": 10130,
                        "terms": ("repair kit, brake pads asbestos-free",), "rear": True,
                        "require_position_context": True,
                        "allowed_brands": ("TEXTAR", "TRW", "OE")})
        targets.append({"requested_item": "senzor uzură plăcuțe spate", "group_id": 10130,
                        "terms": ("brake pad wear sensor", "sensor, brake pad wear"), "rear": True,
                        "require_position_context": True,
                        "allowed_brands": ("TEXTAR", "TRW", "OE")})
    if pad_position == "front" or both_brake_axles:
        targets.append({"requested_item": "plăcuțe frână față", "group_id": 10130,
                        # The BMW "Value Line" unit also exposes a rear set in
                        # the front diagram.  The repair-kit row is axle exact.
                        "terms": ("repair kit, brake pads asbestos-free",),
                        "front": True, "require_position_context": True,
                        "allowed_brands": ("TEXTAR", "TRW", "OE")})
        targets.append({"requested_item": "senzor uzură plăcuțe față", "group_id": 10130,
                        "terms": ("brake pad wear sensor, front",), "front": True,
                        "require_position_context": True,
                        "allowed_brands": ("TEXTAR", "TRW", "OE")})
    disc_position = local_position(r"disc(?:uri)?") if "disc" in text else None
    if disc_position == "front" or both_brake_axles:
        targets.append({"requested_item": "discuri frână față", "group_id": 10130,
                        "terms": ("brake disc",), "front": True,
                        "require_position_context": True,
                        "allowed_brands": ("TEXTAR", "TRW", "OE"),
                        "forced_quantity": 2})
    if disc_position == "rear" or both_brake_axles:
        targets.append({"requested_item": "discuri frână spate", "group_id": 10132,
                        "terms": ("brake disc",), "rear": True,
                        "require_position_context": True,
                        "allowed_brands": ("TEXTAR", "TRW", "OE"),
                        "forced_quantity": 2})
    if re.search(r"etrier.*(?:spate|posterior)|(?:spate|posterior).*etrier", text):
        targets.append({"requested_item": "etrier frână spate", "group_id": 10130,
                        "terms": ("carcasa etrier frana",), "rear": True,
                        "allowed_brands": ("TEXTAR", "TRW", "OE"),
                        "left": bool(re.search(r"\bstg\b|stanga", text)),
                        "right": bool(re.search(r"\bdr\b|dreapta", text))})

    # The remaining workshop vocabulary uses the same conservative OE path.
    # These specs cover every currently supported Romanian canonical term, so
    # adding a client/mechanic alias does not require another browser rule.
    hard_coded = {
        "amortizor", "flanșă amortizor", "rulment sarcină amortizor",
        "pivot suspensie", "bieletă bară stabilizatoare", "braț suspensie",
        "capăt de bară direcție", "set plăcuțe frână spate",
        "set plăcuțe frână față", "etrier frână spate",
    }
    for canonical in requested_canonicals:
        if canonical in hard_coded:
            continue
        spec = OE_ENGLISH_PARTS.get(canonical) or {}
        if not spec.get("group_id") or not spec.get("terms"):
            continue
        front = "față" in canonical or "punte față" in canonical
        rear = "spate" in canonical or "punte spate" in canonical
        targets.append({
            "requested_item": canonical,
            "group_id": int(spec["group_id"]),
            "terms": tuple(spec["terms"]),
            "front": front,
            "rear": rear,
            "require_position_context": bool(front or rear),
        })

    # The standard-search plan is also the complete list of concrete parts
    # understood from this request.  Give every one an OE attempt when the
    # vocabulary has a VIN-catalogue mapping; this prevents a new alias from
    # silently bypassing OE and going straight to TecDoc.
    plan_to_canonical = {
        "filtru ulei": "filtru ulei",
        "filtru aer": "filtru aer",
        "filtru combustibil": "filtru combustibil",
        "filtru habitaclu": "filtru habitaclu",
        "bușon baie ulei": "bușon golire baie ulei",
        "curea accesorii": "curea accesorii",
        "rolă ghidare curea accesorii": "rolă ghidare curea accesorii",
        "întinzător curea accesorii": "rolă întinzătoare",
        "pompă de apă": "pompă de apă",
        "pivot suspensie": "pivot suspensie",
        "cap de bară direcție": "capăt de bară direcție",
        "bieletă axială direcție": "bieletă axială direcție",
        "bieletă bară stabilizatoare": "bieletă bară stabilizatoare",
        "basculă suspensie": "braț suspensie",
    }
    existing = {(_plain(row.get("requested_item")), int(row.get("group_id") or 0)) for row in targets}
    for item in _independent_plan(query):
        requested_item = str(item.get("requested_item") or "")
        canonical = plan_to_canonical.get(requested_item)
        spec = OE_ENGLISH_PARTS.get(canonical or "") or {}
        group_id = int(spec.get("group_id") or 0)
        if not group_id or (_plain(requested_item), group_id) in existing:
            continue
        targets.append({
            "requested_item": requested_item,
            "group_id": group_id,
            "terms": tuple(spec.get("terms") or ()),
            "allowed_brands": (() if item.get("allow_any_brand") else
                               tuple(item.get("brands") or ()) + ("OE",)),
        })
        existing.add((_plain(requested_item), group_id))
    return targets


async def _discover_oe_vin_candidates(ws, identification: dict, vin: str,
                                      query: str, call_id: int = 700) -> list[dict]:
    """Read exact OE codes from the VIN graphical catalogue, without TecDoc."""
    lx_rows = list((identification.get("lx_catalog") or {}).values())
    if not lx_rows:
        return []
    lx = lx_rows[0]
    catalog = str(lx.get("catalog") or "")
    ssd = str(lx.get("ssd") or "")
    if not catalog or not ssd:
        return []
    found, seen = [], set()
    # Several requested parts often share one graphical catalogue group. Read
    # that group once and apply every axle-specific target to the same snapshot;
    # repeated calls were occasionally throttled and produced a partial offer.
    group_cache: dict[int, dict] = {}
    for index, target in enumerate(_oe_vin_targets(query)):
        group_id = int(target["group_id"])
        if group_id not in group_cache:
            payload = {"t_id": "0", "c_id": catalog, "s_id": ssd,
                       "g_id": group_id, "vin": vin}
            expression = "(async()=>{const post=%s;return JSON.stringify((await post('/car/get-lcar-categ',%s)).data?.result||{});})()" % (_post_helper_js(), json.dumps(payload))
            raw = await _evaluate(ws, call_id + index, expression, True)
            group_cache[group_id] = _json_value(raw, {})
        result = group_cache[group_id]
        for group in result.get("groups") or []:
            for unit in group.get("units") or []:
                unit_note = " ".join(str(x.get("value") or "") for x in unit.get("attributes") or [])
                position_context = " ".join((str(group.get("name") or ""), str(unit.get("name") or ""), unit_note))
                unit_norm = unicodedata.normalize("NFKD", position_context.casefold()).encode("ascii", "ignore").decode("ascii")
                position_tokens = {token.strip() for token in unit_norm.split(";")}
                has_front = bool(re.search(r"\b(?:front|fata|anterior)\b", unit_norm))
                has_rear = bool(re.search(r"\b(?:rear|spate|posterior)\b", unit_norm))
                if target.get("front") and has_rear and not has_front:
                    continue
                if target.get("rear") and has_front and not has_rear:
                    continue
                if target.get("require_position_context") and target.get("front") and not has_front:
                    continue
                if target.get("require_position_context") and target.get("rear") and not has_rear:
                    continue
                for detail in unit.get("details") or []:
                    code = re.sub(r"\s+", "", str(detail.get("oem") or "").upper())
                    name = str(detail.get("name") or "").strip()
                    name_norm = unicodedata.normalize("NFKD", name.casefold()).encode("ascii", "ignore").decode("ascii")
                    detail_values = " ".join(str(x.get("value") or "") for x in detail.get("attributes") or [])
                    detail_norm = unicodedata.normalize("NFKD", detail_values.casefold()).encode("ascii", "ignore").decode("ascii")
                    if (not code or not any(term in name_norm for term in target["terms"])
                            or any(term in name_norm for term in target.get("exclude", ()) )):
                        continue
                    if "nu pentru acest model" in (name_norm + " " + detail_norm):
                        continue
                    if any(x.get("key") == "archiveStatus" for x in detail.get("attributes") or []):
                        continue
                    if target.get("left") and "dreapta" in detail_norm and "stanga" not in detail_norm:
                        continue
                    if target.get("right") and "stanga" in detail_norm and "dreapta" not in detail_norm:
                        continue
                    combined_norm = name_norm + " " + detail_norm
                    inferred_left = bool(target.get("left") or re.search(r"\b(?:left|stanga)\b", combined_norm))
                    inferred_right = bool(target.get("right") or re.search(r"\b(?:right|dreapta)\b", combined_norm))
                    key = (target["requested_item"], code)
                    if key in seen:
                        continue
                    seen.add(key)
                    attributes = detail.get("attributes") or []
                    amount = next((x.get("value") for x in attributes
                                   if x.get("key") == "amount"), None)
                    quantity_match = re.search(r"\d+", str(target.get("forced_quantity") or amount or ""))
                    quantity = int(quantity_match.group()) if quantity_match else 1
                    catalog_notes = [str(x.get("value") or "").strip() for x in attributes
                                     if x.get("key") not in {"amount", "archiveStatus"} and str(x.get("value") or "").strip()]
                    found.append({
                        "requested_item": target["requested_item"],
                        "manufacturer_ref": code,
                        "oe_code": code,
                        "brand": "OE",
                        "description": name,
                        "quantity": quantity,
                        "position": "SPATE" if target.get("rear") else ("FATA" if target.get("front") else None),
                        "side": "STANGA" if inferred_left else ("DREAPTA" if inferred_right else None),
                        "allowed_brands": list(target.get("allowed_brands") or ()),
                        "catalog_group_id": target["group_id"],
                        "catalog_unit": unit.get("name"),
                        "vin_exact": True,
                        "identification_confidence": "VIN_EXACT_OE",
                        "requires_human_review": bool(catalog_notes),
                        "catalog_notes": catalog_notes,
                        "match": int(detail.get("match") or 0),
                        "source": "AutoTotal catalog OE după VIN",
                    })
    return sorted(found, key=lambda row: (-int(row.get("match") or 0), row["requested_item"], row["manufacturer_ref"]))


async def _standard_categories(ws, type_id: str, call_id: int) -> dict:
    script = """(async()=>{
      const post=%s;
      const r=await post('/car/getCategs',{typeID:%s});
      return JSON.stringify(r?.data?.result||{});
    })()""" % (_post_helper_js(), json.dumps(str(type_id)))
    return _json_value(await _evaluate(ws, call_id, script, True), {})


async def _standard_vin_types(ws, identification: dict, wanted_model: str,
                              call_id: int = 50) -> list[dict]:
    """Complete a VIN decode through AutoTotal's standard catalogue.

    Some VINs return the exact OE/Laximo vehicle plus TecDoc brand/models, but
    omit ``types`` until the decoded model is selected in Catalog standard.
    """
    td_catalog = identification.get("td_catalog") or {}
    models = td_catalog.get("models") or []
    if not models:
        return []
    wanted = re.sub(r"[^a-z0-9]", "", str(wanted_model or "").casefold())
    ranked = sorted(models, key=lambda row: (
        wanted not in re.sub(r"[^a-z0-9]", "", str(row.get("rangeNameField") or "").casefold()),
        -len(re.sub(r"[^a-z0-9]", "", str(row.get("rangeNameField") or "").casefold())),
    ))
    selected_model = ranked[0] if ranked else None
    model_id = str((selected_model or {}).get("rangeIdField") or "")
    link = str(identification.get("link") or "")
    if not model_id or not link:
        return []
    standard_url = link.replace("#/graphics", "#/standard")
    await _evaluate(ws, call_id, f"location.href={json.dumps(standard_url)}; true")
    await asyncio.sleep(2.5)
    selection = """(()=>{
      const modelId=%s;
      const select=[...document.querySelectorAll('select')]
        .find(node=>[...node.options].some(option=>option.value===modelId));
      if(!select)return false;
      select.value=modelId;
      select.dispatchEvent(new Event('input',{bubbles:true}));
      select.dispatchEvent(new Event('change',{bubbles:true}));
      return true;
    })()""" % json.dumps(model_id)
    if not await _evaluate(ws, call_id + 1, selection, True):
        return []
    try:
        raw = await _wait_value(
            ws,
            "window.td && Array.isArray(window.td.types) && window.td.types.length "
            "? JSON.stringify(window.td.types) : ''",
            15,
        )
        return _json_value(raw, [])
    except (RuntimeError, ValueError):
        return []


def _find_category(categs: list[dict], wanted: str) -> tuple[str, str] | None:
    normalize = lambda value: re.sub(r"[^a-z0-9]", "", str(value).casefold())
    wanted_norm = normalize(wanted)
    for parent in categs or []:
        for child in parent.get("c") or []:
            child_norm = normalize(child.get("t") or "")
            cabin_equivalent = ("filtru" in wanted_norm and "habitaclu" in wanted_norm
                                and "filtru" in child_norm and "habitaclu" in child_norm)
            if (wanted_norm == child_norm or wanted_norm in child_norm
                    or child_norm in wanted_norm or cabin_equivalent):
                return str(parent.get("i") or ""), str(child.get("i") or "")
    return None


def _history_search_candidates(vehicle_context: dict, query: str) -> list[dict]:
    """Exact fitted codes are the safest completion path for vehicles in DSM."""
    text = query.casefold()
    is_revision = any(token in text for token in ("revizie", "filtre", "filtru", "ulei"))
    if not is_revision:
        return []
    rows = []
    for part in vehicle_context.get("_history_parts") or []:
        rows.append({
            "requested_item": part.get("requested_item") or part.get("catalog_name") or "Piesă",
            "description": part.get("catalog_name") or part.get("requested_item") or part.get("part_code"),
            "manufacturer_ref": part.get("part_code"),
            "brand": part.get("brand"),
            "quantity": part.get("quantity") or 1,
            "pack_liters": part.get("pack_liters"),
            "history_exact_code": True,
        })
    for part in vehicle_context.get("_dsm_history_parts") or []:
        name = str(part.get("name") or "")
        normalized = re.sub(r"[^a-z0-9]", "", name.casefold())
        if (("surub" in normalized or "buson" in normalized)
                and ("golire" in normalized or "baieulei" in normalized)):
            rows.append({
                "requested_item": "bușon baie ulei",
                "description": name or "Bușon baie ulei",
                "manufacturer_ref": part.get("code"),
                "brand": part.get("manufacturer"),
                "quantity": part.get("quantity") or 1,
                "history_exact_code": True,
            })
    unique, seen = [], set()
    for row in rows:
        key = re.sub(r"[^A-Z0-9]", "", str(row.get("manufacturer_ref") or "").upper())
        if not key or key in seen or not row.get("brand"):
            continue
        seen.add(key)
        unique.append(row)
    return unique


def _revision_product_kind(value: str) -> str:
    ascii_text = unicodedata.normalize("NFKD", str(value or "")).encode("ascii", "ignore").decode("ascii")
    text = re.sub(r"[^a-z0-9]", "", ascii_text.casefold())
    if "filtru" in text and "ulei" in text:
        return "oil_filter"
    if "filtru" in text and any(x in text for x in ("habitaclu", "polen", "cabina")):
        return "cabin_filter"
    if "filtru" in text and any(x in text for x in ("combustibil", "motorina", "carburant")):
        return "fuel_filter"
    if "filtru" in text and "aer" in text:
        return "air_filter"
    if any(x in text for x in ("buson", "surub")) and any(x in text for x in ("golire", "baieulei")):
        return "oil_drain_plug"
    if "ulei" in text and not any(x in text for x in ("cutie", "transmis", "diferential", "haldex")):
        return "engine_oil"
    if "kit" in text and "curea" in text and any(x in text for x in ("accesor", "acessor", "caneluri")):
        return "accessory_belt_kit"
    if "pompa" in text and "apa" in text:
        return "water_pump"
    if "curea" in text and any(x in text for x in ("accesor", "acessor", "agregat", "auxiliar")):
        if any(x in text for x in ("intinz", "tension")):
            return "accessory_belt_tensioner"
        if any(x in text for x in ("rola", "ghidare", "conducatoare")):
            return "accessory_belt_roller"
        return "accessory_belt"
    return ""


def _required_product_kinds(query: str) -> set[str]:
    plain = unicodedata.normalize("NFKD", str(query or "").casefold()).encode("ascii", "ignore").decode("ascii")
    required: set[str] = set()
    if any(token in plain for token in ("revizie", "filtre", "filtru", "ulei")):
        required.update({"oil_filter", "air_filter", "cabin_filter", "fuel_filter", "engine_oil", "oil_drain_plug"})
    if "kit" in plain and "curea" in plain and any(
            token in plain for token in ("accesor", "acessor", "agregat", "auxiliar")):
        required.update({
            "accessory_belt", "accessory_belt_roller",
            "accessory_belt_tensioner", "water_pump",
        })
    return required


async def _engine_oil_requirements(ws, type_id: str, call_id: int) -> dict:
    """Read the VIN-selected engine capacity and oil approvals from AutoTotal."""
    expression = """(async()=>{const post=%s;return JSON.stringify((await post('/car/capacitati-umplere',{typeID:%s})).data?.result?.engOil||{});})()""" % (_post_helper_js(), json.dumps(str(type_id)))
    try:
        return _json_value(await _evaluate(ws, call_id, expression, True), {})
    except Exception:
        return {}


def _choose_engine_oil_card(cards: list[dict], requirements: dict) -> dict | None:
    """Choose a live oil card matching the OE approval and viscosity."""
    capacity = float((requirements.get("engine_capacity") or {}).get("value") or 0)
    options = requirements.get("engine_oil_options") or []
    approvals = [
        str(row.get("value") or "").upper()
        for option in options for row in option.get("classifications") or []
        if row.get("value")
    ]
    grades = [
        str(row.get("value") or "").upper().replace(" ", "")
        for option in options for row in option.get("grades") or []
        if row.get("value")
    ]
    if capacity <= 0 or not grades:
        return None
    grade = grades[0]
    approval_tokens = [re.sub(r"[^A-Z0-9]", "", value) for value in approvals]

    def approval_matches(compact: str, token: str) -> bool:
        if token in compact:
            return True
        prefix = "".join(re.findall(r"[A-Z]+", token))
        digits = "".join(re.findall(r"\d+", token))
        # Catalogue cards commonly abbreviate "VW 504.00 / 507.00" and omit
        # the second VW prefix while still declaring both approvals.
        return bool(prefix and digits and prefix in compact and digits in compact)
    candidates = []
    for card in cards:
        title = str(card.get("title") or "").strip()
        text = str(card.get("text") or "")
        compact = re.sub(r"[^A-Z0-9]", "", (title + " " + text).upper())
        if re.sub(r"[^A-Z0-9]", "", grade) not in compact:
            continue
        if approval_tokens and not any(approval_matches(compact, token) for token in approval_tokens):
            continue
        pack_match = re.search(r"Ambalare:\s*([0-9]+(?:[.,][0-9]+)?)\s*L", text, re.I)
        price_match = re.search(r"Pret vanzare \(cu TVA\)\s*([0-9]+(?:[.,][0-9]{1,2})?)", text, re.I)
        if not pack_match or not price_match:
            continue
        pack = float(pack_match.group(1).replace(",", "."))
        price = float(price_match.group(1).replace(",", "."))
        if pack <= 0 or pack > 20:
            continue
        quantity = max(1, math.ceil(capacity / pack))
        left = title.split("|", 1)[0].strip()
        code_match = re.search(r"\bOIL\s+(.+)$", left, re.I)
        code = (code_match.group(1).strip() if code_match else left.split()[-1])
        upper_card = (title + " " + text).upper()
        brand = next((value for value in ENGINE_OIL_BRANDS if value in upper_card), "")
        oe_brand = str(requirements.get('oe_brand') or '').upper().strip()
        # Manufacturer must be in the card heading, not just in an approval
        # printed in the description of an aftermarket oil.
        if oe_brand and re.search(r'^(?:OE\s+)?' + re.escape(oe_brand) + r'\b', left.upper()):
            brand = oe_brand
        if requirements.get('oe_only') and brand != oe_brand:
            continue
        # The catalogue sometimes lists Havoline under either the TEXACO or
        # HAVOLINE name.  Keep one stable display brand for the adviser.
        if brand == "HAVOLINE":
            brand = "TEXACO HAVOLINE"
        if not brand:
            continue
        delivery = re.search(r"(Comanda (?:ajunge|express)[^\n]+|Verifica disponibilitatea)", text, re.I)
        candidates.append({
            "requested_item": "ulei motor",
            "description": title,
            "manufacturer_ref": code,
            "supplier_code": f"AT-OIL-{re.sub(r'[^A-Z0-9]', '', code.upper())}",
            "brand": brand,
            "price_ron": price,
            "price": f"{price:.2f} RON".replace(".", ","),
            "availability": delivery.group(1).strip() if delivery else "Disponibilitate afișată în AutoTotal",
            "quantity": quantity,
            "pack_liters": pack,
            "technical_liters": capacity,
            "offered_liters": round(quantity * pack, 2),
            "oil_specification": " / ".join(approvals) + f" · SAE {grade}",
            "verified_from_tecrmi": True,
            "source": "AutoTotal Uleiuri — normă verificată TecRMI",
            "product_url": card.get("url") or f"https://www3.eoriginal.ro/uleiuri/#/?filter_v={grade.replace('-', '')}",
            "discovered_by": "Capacitate și normă OE după VIN",
        })
    if not candidates:
        return None
    brand_rank = {"CASTROL": 0, "MOTUL": 1, "TEXACO": 2, "TEXACO HAVOLINE": 2}
    return min(candidates, key=lambda row: (
        row["offered_liters"] - capacity,
        brand_rank.get(row["brand"], 99),
        row["price_ron"] * row["quantity"],
        row["pack_liters"],
    ))


async def _discover_engine_oil(ws, requirements: dict, call_id: int) -> list[dict]:
    options = requirements.get("engine_oil_options") or []
    grades = [str(row.get("value") or "").upper().replace(" ", "")
              for option in options for row in option.get("grades") or [] if row.get("value")]
    if not grades:
        return []
    grade = grades[0]
    url = f"https://www3.eoriginal.ro/uleiuri/#/?filter_v={grade.replace('-', '')}"
    await _evaluate(ws, call_id, f"location.href={json.dumps(url)}; true")
    await asyncio.sleep(4)
    expression = r"""(()=>JSON.stringify([...document.querySelectorAll('.article-card')].slice(0,100).map(c=>({
      text:c.innerText||'',title:c.querySelector('.card-title')?.innerText||'',url:location.href
    }))))()"""
    try:
        cards = _json_value(await _wait_value(ws, expression, 20), [])
    except RuntimeError:
        return []
    chosen = _choose_engine_oil_card(cards, requirements)
    # The dedicated oil page may initially expose only its promoted brand.
    # Search the approved brands explicitly before declaring the oil missing.
    if not chosen:
        approvals = [
            str(row.get("value") or "")
            for option in options for row in option.get("classifications") or []
            if row.get("value")
        ]
        approval = approvals[0] if approvals else ""
        accumulated = list(cards)
        search_brands = ([requirements.get('oe_brand')] if requirements.get('oe_only') else ("CASTROL", "MOTUL", "HAVOLINE"))
        for offset, brand in enumerate(search_brands, start=1):
            search = " ".join(value for value in (brand, grade, approval) if value)
            script = """(async()=>{
              const post=%s;
              const response=await post('/search/doSearch',{
                search:%s,searchType:0,section:section,ref:'robot-oil',autoSelection:1,useAiSearch:false
              });
              const result=response?.data?.result||{};
              const target=[response?.data?.redirect,result.result,result.redirect]
                .find(value=>typeof value==='string' && value.startsWith('http'))||'';
              if(!target) return '';
              location.href=target; return target;
            })()""" % (_post_helper_js(), json.dumps(search))
            try:
                target = await _evaluate(ws, call_id + offset * 100, script, True)
                if not target:
                    continue
                await asyncio.sleep(3)
                brand_cards = _json_value(await _wait_value(ws, expression, 15), [])
                accumulated.extend(brand_cards)
                chosen = _choose_engine_oil_card(accumulated, requirements)
                if chosen:
                    break
            except Exception as exc:
                print(f"AUTOTOTAL oil search failed brand={brand}: {exc}", flush=True)
    return [chosen] if chosen else []


async def _discover_known_preferred_engine_oil(ws, requirements: dict, call_id: int) -> list[dict]:
    """Exact SKU fallback for an approved oil when brand search has no cards."""
    capacity = float((requirements.get("engine_capacity") or {}).get("value") or 0)
    options = requirements.get("engine_oil_options") or []
    approvals = [str(row.get("value") or "").upper()
                 for option in options for row in option.get("classifications") or []]
    grades = [str(row.get("value") or "").upper().replace(" ", "")
              for option in options for row in option.get("grades") or []]
    compact_approvals = {re.sub(r"[^A-Z0-9]", "", value) for value in approvals}
    if capacity <= 0 or "VW50700" not in compact_approvals or "5W-30" not in grades:
        return []
    rounded_liters = max(1, math.ceil(capacity))
    use_five_liter_pack = rounded_liters % 5 == 0
    pack_liters = 5.0 if use_five_liter_pack else 1.0
    code = "CG530LL/5" if use_five_liter_pack else "CG530LL/1"
    candidate = {
        "requested_item": "ulei motor",
        "description": f"Castrol EDGE 5W-30 Long Life {pack_liters:g}L",
        "manufacturer_ref": code,
        "brand": "CASTROL",
        "quantity": max(1, rounded_liters // int(pack_liters)),
        "pack_liters": pack_liters,
        "technical_liters": capacity,
        "oil_specification": " / ".join(approvals) + " · SAE 5W-30",
        "verified_from_tecrmi": True,
        "history_exact_code": True,
    }
    found = await _search_code(ws, candidate, call_id)
    if not found:
        return []
    found["offered_liters"] = float(candidate["quantity"] * pack_liters)
    found["source"] = "AutoTotal — cod exact ulei aprobat, normă verificată TecRMI"
    found["discovered_by"] = "Normă VW 507.00 și cod exact Castrol EDGE Long Life"
    return [found]


async def _discover_standard_product(ws, item: dict, type_id: str, catalog: dict,
                                     base_url: str, call_id: int) -> list[dict]:
    category_search = item.get("category_search") or item["search"]
    found = _find_category(catalog.get("categs") or [], category_search)
    if not found:
        return []
    main_category, category = found
    sub_script = """(async()=>{
      const post=%s;
      const r=await post('/car/getSubCategs',{typeID:%s,categ:%s});
      return JSON.stringify(r?.data?.result||{});
    })()""" % (_post_helper_js(), json.dumps(str(type_id)), json.dumps(category))
    groups = _json_value(await _evaluate(ws, call_id, sub_script, True), {})
    normalize = lambda value: re.sub(r"[^a-z0-9]", "", str(value).casefold())
    wanted = normalize(item.get("group_search") or item["search"])
    group_id = next((str(key) for key, value in groups.items()
                     if wanted in normalize(value) or normalize(value) in wanted), None)
    if not group_id and wanted == "filtruhabitaclu":
        group_id = next((str(key) for key, value in groups.items()
                         if normalize(value) in {"filtruaerhabitaclu", "filtruhabitaclu"}), None)
    if not group_id:
        return []
    url = f"{base_url}#/standard?ti={type_id}&mcat={main_category}&cat={category}&gr={group_id}"
    await _evaluate(ws, call_id + 1, f"location.href={json.dumps(url)}; true")
    await asyncio.sleep(2)
    cards_expression = r"""(() => {
      const wanted=new RegExp(%s,'i');
      const cards=[...document.querySelectorAll('.article-card')]
        .filter(c=>wanted.test(c.querySelector('.card-title')?.innerText||'') &&
          /Pret vanzare \(cu TVA\)\s*[0-9]/i.test(c.innerText||''));
      return cards.length ? JSON.stringify(cards.slice(0,60).map(c=>({
        text:c.innerText,title:c.querySelector('.card-title')?.innerText||'',url:location.href
      }))) : '';
    })()""" % json.dumps(item["kind"])
    try:
        cards = _json_value(await _wait_value(ws, cards_expression, 25), [])
    except RuntimeError:
        return []
    kind = re.compile(item["kind"], re.I)
    preferred = []
    for card in cards:
        head = (card.get("title") or (card.get("text") or "").splitlines()[0]).strip()
        upper = head.upper()
        allowed_brands = item.get("brands") or FILTER_BRANDS
        brand = next((name for name in allowed_brands if name in upper.replace("Ö", "O")), None)
        if not brand or not kind.search(upper):
            continue
        price_match = re.search(r"Pret vanzare \(cu TVA\)\s*([0-9]+(?:[.,][0-9]{1,2})?)", card.get("text") or "", re.I)
        if not price_match:
            continue
        left = head.split("|")[0].strip()
        rest = left
        for prefix in BRAND_PREFIXES.get(brand, (brand,)):
            rest = re.sub(r"^" + re.escape(prefix) + r"\s+", "", rest, flags=re.I)
        code = re.sub(r"\s*\([^)]*\).*", "", rest).strip()
        if not code:
            continue
        price = float(price_match.group(1).replace(",", "."))
        delivery = re.search(r"(Comanda ajunge[^\n]+|Comanda express[^\n]+|Verifica disponibilitatea)",
                             card.get("text") or "", re.I)
        preferred.append({
            "requested_item": item["requested_item"],
            "description": item["requested_item"],
            "manufacturer_ref": code,
            "supplier_code": f"AT-{brand}-{re.sub(r'[^A-Z0-9]', '', code.upper())}",
            "brand": brand,
            "price_ron": price,
            "price": f"{price:.2f} RON".replace(".", ","),
            "availability": delivery.group(1).strip() if delivery else "Disponibilitate afișată în AutoTotal",
            "quantity": 1,
            "source": "AutoTotal",
            "product_url": card.get("url") or url,
            "discovered_by": "AutoTotal VIN + catalog standard",
        })
    # Keep every preferred-brand alternative visible for human verification.
    # Similar vehicles can legitimately have multiple codes in the same group.
    return sorted(preferred, key=lambda p: (p["price_ron"], p["brand"], p["manufacturer_ref"]))
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        saved_query = str(data.get("query") or "").strip()
        # Wait for the complete Inter Cars result. Reading a RUNNING file here
        # races with the extension and can price only the first code written.
        if data.get("status") != "ready" or (saved_query and saved_query != query.strip()):
            return []
        return [p for p in data.get("products") or [] if p.get("manufacturer_ref")]
    except (OSError, ValueError, TypeError):
        return []


async def _search_code(ws, candidate: dict, call_id: int) -> dict | None:
    code = str(candidate.get("manufacturer_ref") or "").strip()
    brand = str(candidate.get("brand") or "").strip()
    if not code or not brand:
        return None
    await _evaluate(ws, call_id, "location.href='https://www3.eoriginal.ro/#/'; true")
    await asyncio.sleep(1.5)
    script = """(async () => {
      const post=%s;
      const response=await post('/search/doSearch',{
        search:%s,searchType:0,section:section,ref:'robot',autoSelection:1,useAiSearch:false
      });
      const result=response?.data?.result||{};
      const target=[response?.data?.redirect,result.result,result.redirect]
        .find(value=>typeof value==='string' && value.startsWith('http'))||'';
      if(!target) return '';
      location.href=target; return target;
    })()""" % (_post_helper_js(), json.dumps(code))
    target = await _evaluate(ws, call_id, script, True)
    if not target:
        return None
    # Cards render first; stock and sale prices arrive in a second request.
    await asyncio.sleep(3)
    cards_expression = """(() => {
      const cards=[...document.querySelectorAll('.article-card')];
      return cards.length ? JSON.stringify(cards.slice(0,40).map(c=>({
        text:c.innerText,title:c.querySelector('.card-title')?.innerText||'',url:location.href
      }))) : '';
    })()"""
    raw_cards = await _wait_value(ws, cards_expression, 18)
    cards = _json_value(raw_cards, [])
    print(f"AUTOTOTAL code={code} brand={brand} cards={len(cards)}", flush=True)
    wanted = re.sub(r"[^A-Z0-9]", "", code.upper())
    wanted_brand = re.sub(r"[^A-Z0-9]", "", brand.upper())
    candidate_kind = _revision_product_kind(candidate.get("requested_item") or candidate.get("description"))

    def matches_card(head: str) -> bool:
        displayed = re.sub(r"[^A-Z0-9]", "", head.upper())
        if wanted_brand not in displayed:
            return False
        if wanted in displayed:
            return True
        # Some suppliers store their prefix only in DSM (EL567640), while
        # AutoTotal displays the manufacturer plus 567.640.
        wanted_digits = "".join(re.findall(r"\d+", code))
        if candidate_kind == "oil_drain_plug" and len(wanted_digits) >= 5 and wanted_digits in displayed:
            return True
        if candidate_kind == "engine_oil":
            viscosity = re.search(r"\b(\d{1,2}W[- ]?\d{2})\b", code, re.I)
            pack = float(candidate.get("pack_liters") or 0)
            viscosity_ok = bool(viscosity and re.sub(r"[^A-Z0-9]", "", viscosity.group(1).upper()) in displayed)
            pack_ok = not pack or re.search(rf"\b{pack:g}\s*L\b", head, re.I)
            longlife_ok = ("LL" not in code.upper()
                           or "LONGLIFE" in displayed or "LL" in displayed)
            return viscosity_ok and bool(pack_ok) and longlife_ok
        return False
    matched = None
    for card in cards:
        text = card.get("text") or ""
        head = card.get("title") or (text.splitlines()[0] if text else "")
        if matches_card(head):
            matched = (card, text)
            print(f"AUTOTOTAL matched={head}", flush=True)
            break
    if not matched:
        print(
            "AUTOTOTAL no_exact_match " + json.dumps(
                [str(card.get("title") or "") for card in cards[:10]], ensure_ascii=True
            ),
            flush=True,
        )
        return None
    _, text = matched
    # Prices/stocks are populated after the product cards. Re-read the same
    # matching card until that second request has completed.
    for retry in range(8):
        if re.search(r"Pret vanzare \(cu TVA\)\s*[0-9]", text, re.I):
            break
        await asyncio.sleep(1)
        refreshed = _json_value(await _evaluate(ws, call_id + 200 + retry, cards_expression), [])
        for card in refreshed:
            head = card.get("title") or ""
            if matches_card(head):
                matched = (card, card.get("text") or "")
                text = matched[1]
                break
    price_match = re.search(r"Pret vanzare \(cu TVA\)\s*([0-9]+(?:[.,][0-9]{1,2})?)", text, re.I)
    delivery_match = re.search(r"(Comanda ajunge[^\n]+)", text, re.I)
    quantity_match = re.search(r"\bx\s*(\d+)\b", text, re.I)
    if not price_match:
        print(f"AUTOTOTAL price_missing code={code} text={text[:500]!r}", flush=True)
        return None
    price = float(price_match.group(1).replace(",", "."))
    return {
        "requested_item": candidate.get("requested_item") or "Piesă",
        "brand": brand,
        "description": candidate.get("description") or code,
        "manufacturer_ref": code,
        "supplier_code": f"AT-{brand}-{re.sub(r'[^A-Z0-9]', '', code.upper())}",
        "price_ron": price,
        "price": f"{price:.2f} RON".replace(".", ","),
        "availability": delivery_match.group(1).strip() if delivery_match else "Disponibilitate afișată în AutoTotal",
        "quantity": candidate.get("quantity") or (int(quantity_match.group(1)) if quantity_match else 1),
        "pack_liters": candidate.get("pack_liters"),
        "technical_liters": candidate.get("technical_liters"),
        "oil_specification": candidate.get("oil_specification"),
        "verified_from_tecrmi": bool(candidate.get("verified_from_tecrmi")),
        "source": "AutoTotal",
        "product_url": matched[0].get("url") or "https://www3.eoriginal.ro/",
        "discovered_by": "AutoTotal căutare cod exact din istoricul DSM",
        "dsm_history_match": bool(candidate.get("history_exact_code")),
    }


def _plain(value: str) -> str:
    return unicodedata.normalize("NFKD", str(value or "").casefold()).encode("ascii", "ignore").decode("ascii")


def _canonical_offer_brand(brand: str) -> str:
    value = str(brand or "").strip().upper()
    if value in {"OE", "VW", "VW-AUDI", "VOLKSWAGEN", "AUDI", "SKODA", "SEAT"}:
        return "OE"
    return value


def _oe_option_allowed(candidate: dict, title: str, brand: str) -> bool:
    """Keep only true same-part equivalents suitable for the requested axle/side."""
    wanted = _plain(candidate.get("requested_item"))
    product = _plain(title)
    canonical_brand = _canonical_offer_brand(brand)
    allowed = {_canonical_offer_brand(value) for value in candidate.get("allowed_brands") or ()}
    if allowed and canonical_brand not in allowed:
        return False

    wrong_family = ("set reparatie", "set accesorii", "suport etrier", "piston etrier")
    if "bascul" in wanted:
        if not any(value in product for value in ("bascul", "brat suspensie", "brat transversal")):
            return False
        if any(value in product for value in ("bucsa", "lager", "set reparatie")):
            return False
    if "cap de bara" in wanted and not any(value in product for value in ("cap de bara", "articulatie directie")):
        return False
    if "bieleta bara stabilizatoare" in wanted and not any(
            value in product for value in ("bieleta", "antiruliu", "stabiliz", "swing support")):
        return False
    if "placute" in wanted and "placute frana" not in product:
        return False
    if "discuri frana" in wanted and not any(value in product for value in ("disc frana", "discuri frana")):
        return False
    if "curea accesorii" in wanted:
        if "curea transmisie cu caneluri" not in product:
            return False
        if any(value in product for value in ("set curea", "pompa apa", "rola", "intinzator")):
            return False
        # The VIN catalogue supplies the exact belt dimensions (for example
        # 6 DPK X 1817).  Reject visually similar belts from the same generic
        # TecDoc group when their dimensions differ.
        notes = " ".join(str(value) for value in candidate.get("catalog_notes") or ())
        dimension = re.search(r"\b(\d+)\s*DPK\s*X?\s*(\d{3,4})\b", notes, re.I)
        if dimension:
            wanted_dimension = f"{dimension.group(1)}dpk{dimension.group(2)}"
            compact_product = re.sub(r"[^a-z0-9]", "", product)
            if wanted_dimension not in compact_product and "oe " not in product:
                return False
    if "buson baie ulei" in wanted:
        # A search by the VIN-exact OE code may also return sealing rings,
        # repair assortments or unrelated VAG plugs. Keep only the drain plug
        # itself; vehicle-family filtering is applied again in the dashboard.
        if not any(value in product for value in (
                "surub de golire", "buson de golire", "buson baie", "surub obturator",
                "oil drain plug", "screw plug, oil sump")):
            return False
    if "filtru aer" in wanted:
        if not any(value in product for value in ("filtru aer", "filtru de aer", "air filter")):
            return False
        if any(value in product for value in ("carcasa", "housing", "capac filtru")):
            return False
    if "filtru ulei" in wanted:
        if not any(value in product for value in ("filtru ulei", "filtru de ulei", "oil filter", "cartus filtru")):
            return False
        if any(value in product for value in ("suport filtru", "capac filtru", "set garnituri", "housing")):
            return False
        if any(value in product for value in (
                "inel de etansare", "garnitura", "set sortiment", "trusa")):
            return False
    if "etrier" in wanted:
        if "etrier frana" not in product or any(value in product for value in wrong_family):
            return False
        # Căutarea pornește din codul OE exact după VIN. Unele carduri nu
        # repetă puntea în titlu; respingem doar contradicția explicită.
    if candidate.get("position") == "SPATE" and "fata" in product and "spate" not in product:
        return False
    if candidate.get("position") == "FATA" and "spate" in product and "fata" not in product:
        return False
    if candidate.get("side") == "STANGA" and "dreapta" in product and "stanga" not in product:
        return False
    if candidate.get("side") == "DREAPTA" and "stanga" in product and "dreapta" not in product:
        return False
    return True


def _keep_one_oil_drain_plug(products: list[dict]) -> list[dict]:
    """Offer one VIN/OE-confirmed drain plug, preferring the actual OE item."""
    plugs = [row for row in products if _revision_product_kind(
        row.get("requested_item") or row.get("description")) == "oil_drain_plug"]
    if len(plugs) <= 1:
        return products

    def rank(row: dict) -> tuple:
        brand = _canonical_offer_brand(row.get("brand") or "")
        reference = re.sub(r"[^A-Z0-9]", "", str(row.get("manufacturer_ref") or "").upper())
        oe_code = re.sub(r"[^A-Z0-9]", "", str(row.get("oe_code") or "").upper())
        exact_oe = brand == "OE" and reference and reference == oe_code
        available = bool(str(row.get("availability") or "").strip())
        trusted = {"ELRING": 0, "FEBI": 1, "FA1": 2, "METZGER": 3, "TRISCAN": 4, "TOPRAN": 5}
        return (
            0 if exact_oe else 1,
            0 if available else 1,
            trusted.get(str(row.get("brand") or "").upper(), 50),
            float(row.get("price_ron") or 10**9),
        )

    chosen = min(plugs, key=rank)
    return [row for row in products if row not in plugs] + [chosen]


async def _search_oe_code_options(ws, candidate: dict, call_id: int) -> list[dict]:
    """Find priced OE and aftermarket equivalents for one VIN-exact OE code."""
    code = str(candidate.get("oe_code") or candidate.get("manufacturer_ref") or "").strip()
    if not code:
        return []
    await _evaluate(ws, call_id, "location.href='https://www3.eoriginal.ro/#/'; true")
    await asyncio.sleep(1.2)
    script = """(async () => {
      const post=%s;
      const response=await post('/search/doSearch',{
        search:%s,searchType:0,section:section,ref:'robot-oe',autoSelection:1,useAiSearch:false
      });
      const result=response?.data?.result||{};
      const target=[response?.data?.redirect,result.result,result.redirect]
        .find(value=>typeof value==='string' && value.startsWith('http'))||'';
      if(!target) return '';
      location.href=target; return target;
    })()""" % (_post_helper_js(), json.dumps(code))
    target = await _evaluate(ws, call_id, script, True)
    if not target:
        return []
    await asyncio.sleep(3)
    cards_expression = """(() => JSON.stringify([...document.querySelectorAll('.article-card')].slice(0,30).map(c=>({
      text:c.innerText||'', title:c.querySelector('.card-title')?.innerText||'', url:location.href
    }))))()"""
    raw = await _wait_value(ws, cards_expression, 18)
    cards = _json_value(raw, [])
    for retry in range(6):
        if cards and any(re.search(r"Pret vanzare \(cu TVA\)\s*[0-9]", row.get("text") or "", re.I)
                         for row in cards):
            break
        await asyncio.sleep(1)
        cards = _json_value(await _evaluate(ws, call_id + 100 + retry, cards_expression), [])

    known_brands = sorted((
        "KAVO PARTS", "MANN-FILTER", "LEMFORDER", "JAPANPARTS", "BILSTEIN",
        "HARTWINN", "MONROE", "SACHS", "MEYLE", "SKODA", "SEAT", "VOLKSWAGEN",
        "VW-AUDI", "TEXTAR", "AUDI", "OE", "TRW", "DELPHI", "FEBI", "SNR", "KYB", "BOGE", "PURFLUX", "BOSCH", "MAHLE",
    ), key=len, reverse=True)
    wanted_item = str(candidate.get("requested_item") or "Piesă OE")
    wanted_norm = unicodedata.normalize("NFKD", wanted_item.casefold()).encode("ascii", "ignore").decode("ascii")
    options = []
    for card in cards:
        title = str(card.get("title") or "").strip()
        text = str(card.get("text") or "")
        title_norm = unicodedata.normalize("NFKD", title.casefold()).encode("ascii", "ignore").decode("ascii")
        if "amortizor" in wanted_norm and "amortiz" not in title_norm:
            continue
        if "protectie" in wanted_norm and not any(x in title_norm for x in ("burduf", "protect", "tampon", "kit")):
            continue
        if "rulment" in wanted_norm and not any(x in title_norm for x in ("rulment", "flansa", "suport")):
            continue
        price_match = re.search(r"Pret vanzare \(cu TVA\)\s*([0-9]+(?:[.,][0-9]{1,2})?)", text, re.I)
        if not price_match:
            continue
        upper_title = title.upper()
        brand = next((value for value in known_brands if upper_title.startswith(value + " ")), "")
        if not brand:
            brand = upper_title.split(" ", 1)[0]
        if not _oe_option_allowed(candidate, title, brand):
            continue
        display_brand = _canonical_offer_brand(brand)
        if "OE BMW" in upper_title or (brand == "AUTOTOTAL" and " OE " in f" {upper_title} "):
            display_brand = "OE BMW"
        left = re.split(r"\s+\(|\s+\|", title, maxsplit=1)[0]
        reference = left[len(brand):].strip() if left.upper().startswith(brand) else left
        delivery = (re.search(r"(Comanda (?:ajunge|express)[^\n]+|Termen mediu[^\n]+)", text, re.I) or [None, ""])[1]
        price = float(price_match.group(1).replace(",", "."))
        options.append({
            "requested_item": wanted_item,
            "brand": display_brand,
            "description": title,
            "manufacturer_ref": reference,
            "oe_code": code,
            "supplier_code": f"AT-OE-{re.sub(r'[^A-Z0-9]', '', reference.upper())}",
            "price_ron": price,
            "price": f"{price:.2f} RON".replace(".", ","),
            "availability": delivery.strip() or "Disponibilitate afișată în AutoTotal",
            "quantity": candidate.get("quantity") or 1,
            "position": candidate.get("position"),
            "side": candidate.get("side"),
            "source": "AutoTotal — echivalență după cod OE/VIN",
            "product_url": card.get("url") or target,
            "discovered_by": f"Cod OE {code} decodat după VIN",
            "vin_exact_oe_cross_reference": True,
            "identification_confidence": "VIN_OE_CROSS_REFERENCE",
            "requires_human_review": bool(candidate.get("requires_human_review")),
            "catalog_notes": list(candidate.get("catalog_notes") or []),
        })
    return options


async def resolve_power_in_haynes(context, vehicle: dict) -> dict:
    pages = [p for p in context.pages if "workshopdata.com" in p.url]
    page = pages[0] if pages else await context.new_page()
    await page.goto("https://www.workshopdata.com/touch/site/layout/makesOverview",
                    wait_until="domcontentloaded", timeout=25000)
    await page.wait_for_timeout(1200)
    if "wsdLogin" in page.url or await page.get_by_text("Login name:", exact=False).count():
        return {"status": "login_required", "message": "Autentificare Haynes de rezervă necesară."}

    make = str(vehicle.get("make") or "").strip()
    model = str(vehicle.get("model") or "").strip()
    engine = str(vehicle.get("engine_or_variant") or "").strip().upper()
    if not make or not model or not engine:
        return {"status": "insufficient_vehicle_data", "message": "Lipsesc marca, modelul sau codul motor."}

    make_link = page.get_by_text(make, exact=True)
    if not await make_link.count() and make == "VW":
        make_link = page.get_by_text("VOLKSWAGEN", exact=True)
    if not await make_link.count():
        return {"status": "make_not_found", "message": f"Marca {make} nu a fost găsită în Haynes."}
    await make_link.last.click()
    await page.wait_for_timeout(1200)

    model_link = page.get_by_text(model, exact=True)
    if not await model_link.count():
        model_link = page.get_by_text(re.compile(re.escape(model), re.I))
    if not await model_link.count():
        return {"status": "model_not_found", "message": f"Modelul {model} nu a fost găsit în Haynes."}
    await model_link.last.click()
    await page.wait_for_timeout(1200)

    rows = page.locator("tr").filter(has_text=engine)
    row_texts = [await rows.nth(i).inner_text() for i in range(min(await rows.count(), 20))]
    powers = sorted({int(x) for text in row_texts for x in re.findall(r"(\d{2,3})\s*kW", text, re.I)})
    if len(powers) == 1:
        return {"status": "resolved", "power_kw": powers[0], "engine_code": engine,
                "evidence": row_texts[:5], "message": "Puterea a fost confirmată în Haynes după codul motor."}
    return {"status": "ambiguous", "engine_code": engine, "power_candidates_kw": powers,
            "evidence": row_texts[:5], "message": "Haynes nu a furnizat o singură putere pentru codul motor."}


async def run(request_id: int, vin: str, query: str, output: Path,
              hints_path: Path | None = None) -> None:
    base = {"request_id": request_id, "vin": vin, "query": query,
            "source": "AutoTotal", "products": [],
            "updated_at": datetime.now().isoformat(timespec="seconds")}
    try:
        await ensure_autototal_responsive()
        vehicle_hints = _load_vehicle_hints(request_id, hints_path)
        target = _autototal_target()
        async with websockets.connect(target["webSocketDebuggerUrl"], max_size=16_000_000) as ws:
            await _evaluate(ws, 0, "location.href='https://www3.eoriginal.ro/#/'; true")
            await asyncio.sleep(1.5)
            landing_text = str(await _evaluate(
                ws, 1, "document.body ? document.body.innerText.trim() : ''"
            ) or "")
            if re.search(r"\bmentenan(?:ta|ță)\b", landing_text, re.I):
                raise RuntimeError(
                    "AutoTotal este temporar în mentenanță; Inter Cars continuă independent."
                )
            identify = """(async () => {
              const post=%s;
              const response=await post('/search/searchVin',{vin:%s,return_car:1});
              const result=response?.data?.result||{};
              const link=result.car_link||'';
              if(!link) throw new Error('AutoTotal nu a identificat VIN-ul.');
              location.href=link;
              return JSON.stringify({link,td_catalog:result.td_catalog||{},lx_catalog:result.lx_catalog||{}});
            })()""" % (_post_helper_js(), json.dumps(vin))
            identification = _json_value(await _evaluate(ws, 2, identify, True), {})
            await _wait_value(ws, "location.href.includes('/auto/vin/')", 25)
            await asyncio.sleep(1.5)
            text = await _wait_value(ws, "document.body && document.body.innerText", 10)
            vehicle_match = re.search(r"\n(MERCEDES-BENZ|VOLKSWAGEN|VW|AUDI|BMW|FORD|OPEL|RENAULT|DACIA|PEUGEOT|CITROEN|SEAT|SKODA)\s+([^\n\t]+)", text, re.I)
            date_match = re.search(r"\b(\d{2})\.(\d{2})\.(\d{4})\b", text)
            power_match = re.search(r"(?:Putere|Power)\s*:?\s*(\d{2,3})\s*kW", text, re.I)
            lx_rows = list((identification.get("lx_catalog") or {}).values())
            lx_row = lx_rows[0] if lx_rows else {}
            lx_name = str(lx_row.get("name") or "")
            lx_attributes = lx_row.get("attributes") or {}
            engine_attribute = lx_attributes.get("engine") or {}
            aggregates = str(engine_attribute.get("full_value") or engine_attribute.get("value") or "")
            engine_match = re.search(r"\b(\d{3}[.]\d{3})\b", aggregates, re.I)
            if not engine_match:
                engine_match = re.search(r"^\s*([A-Z][A-Z0-9.-]{2,15})\b", aggregates, re.I)
            if not engine_match:
                engine_match = re.search(r"(?:Engine:\s*\d+\s+\d+\s+|ENGINE\s+)([A-Z0-9. -]{3,40})", text, re.I)
            aggregate_power = re.search(r"\b(\d{2,3})\s*kW\b", aggregates, re.I)
            drive_value = str((lx_attributes.get("drive") or {}).get("value") or "")
            vehicle = {
                "make": vehicle_match.group(1).strip() if vehicle_match else None,
                "model": vehicle_match.group(2).strip() if vehicle_match else None,
                "engine_or_variant": (engine_match.group(1).strip() if engine_match else None),
                "year": int(date_match.group(3)) if date_match else None,
                "first_registration": ".".join(date_match.groups()) if date_match else None,
                "power_kw": int(power_match.group(1)) if power_match else (
                    int(aggregate_power.group(1)) if aggregate_power else None
                ),
                "url": await _evaluate(ws, 2, "location.href"),
            }
            if lx_name and (not vehicle.get("model") or "/" in str(vehicle.get("model"))):
                vehicle["model"] = lx_name
            hint_fields_used = []
            # Keep AutoTotal's independently decoded exact identity. DSM can
            # complete missing fields, but a list of possible DSM engine codes
            # must never overwrite the exact AutoTotal engine selected by VIN.
            for field in ("engine_or_variant", "power_kw", "drivetrain"):
                if (vehicle.get(field) in (None, "")
                        and vehicle_hints.get(field) not in (None, "")):
                    vehicle[field] = vehicle_hints[field]
                    hint_fields_used.append(field)
            vehicle["dsm_engine_candidates"] = vehicle_hints.get("engine_or_variant") or ""
            for field in ("make", "model", "year", "first_registration"):
                generic_model = field == "model" and str(vehicle.get(field) or "").strip().casefold() in {"model", "unknown"}
                if (vehicle.get(field) in (None, "") or generic_model) and vehicle_hints.get(field) not in (None, ""):
                    vehicle[field] = vehicle_hints[field]
                    hint_fields_used.append(field)
            vehicle["identity_source"] = (
                "AutoTotal VIN + verificare încrucișată DSM"
                if hint_fields_used else "AutoTotal VIN"
            )
            vehicle["identity_fields_from_dsm"] = hint_fields_used
            identity_text = " ".join(str(vehicle.get(k) or "") for k in ("model", "engine_or_variant"))
            identity_text += " " + lx_name + " " + drive_value
            if re.search(r"\b(?:E?4X2|2WD|RWD|FWD)\b", identity_text, re.I):
                vehicle["drivetrain"] = "4x2"
            elif re.search(r"\b(?:E?4X4|4WD|AWD|4MATIC|4MOTION)\b", identity_text, re.I):
                vehicle["drivetrain"] = "4x4"
            complete = all(vehicle.get(k) not in (None, "") for k in ("make", "model", "year", "engine_or_variant", "power_kw"))
            save(output, {**base, "status": "searching_parts", "vehicle": vehicle,
                          "message": "AutoTotal a identificat vehiculul și caută independent piesele cerute."})
            types = (identification.get("td_catalog") or {}).get("types") or []
            # AutoTotal can return the OE vehicle immediately while its
            # standard TecDoc catalogue is still loading. Retry the VIN decode
            # and model selection before declaring power/TecRMI unavailable.
            # This is the path that resolves an exact engine code (for example
            # CJCD) to its unique kW value without asking the customer.
            for type_attempt in range(3):
                if types:
                    break
                types = await _standard_vin_types(
                    ws,
                    identification,
                    str(vehicle_hints.get("model") or vehicle.get("model") or lx_name),
                    50 + type_attempt * 20,
                )
                if types or type_attempt == 2:
                    break
                await asyncio.sleep(2.0 + type_attempt)
                refresh_script = """(async () => {
                  const post=%s;
                  const response=await post('/search/searchVin',{vin:%s,return_car:1});
                  const result=response?.data?.result||{};
                  return JSON.stringify({link:result.car_link||'',td_catalog:result.td_catalog||{},lx_catalog:result.lx_catalog||{}});
                })()""" % (_post_helper_js(), json.dumps(vin))
                refreshed = _json_value(
                    await _evaluate(ws, 70 + type_attempt, refresh_script, True), {}
                )
                if refreshed.get("link"):
                    identification = refreshed
            power_resolution = {"status": "not_needed"}
            if types and vehicle.get("engine_or_variant") and vehicle.get("power_kw") in (None, ""):
                power_resolution = _resolve_unique_power_from_types(
                    types, str(vehicle.get("engine_or_variant") or ""), vehicle.get("year")
                )
                if power_resolution.get("status") == "resolved":
                    vehicle["power_kw"] = int(power_resolution["power_kw"])
                    vehicle["power_source"] = power_resolution.get("source")
                    complete = all(vehicle.get(k) not in (None, "") for k in (
                        "make", "model", "year", "engine_or_variant", "power_kw"
                    ))
            if not types:
                oe_candidates = await _discover_oe_vin_candidates(ws, identification, vin, query)
                oe_products = []
                for index, candidate in enumerate(oe_candidates):
                    try:
                        oe_products.extend(await _search_oe_code_options(
                            ws, candidate, 900 + index * 200
                        ))
                    except Exception as exc:
                        print(f"AUTOTOTAL OE search failed code={candidate.get('oe_code')}: {exc}", flush=True)
                unique_products, product_keys = [], set()
                for product in oe_products:
                    key = (product.get("requested_item"), product.get("brand"),
                           re.sub(r"[^A-Z0-9]", "", str(product.get("manufacturer_ref") or "").upper()))
                    if key in product_keys:
                        continue
                    product_keys.add(key)
                    unique_products.append(product)
                save(output, {
                    **base,
                    "status": "ready" if unique_products else ("vehicle_ready" if complete else "vehicle_partial"),
                    "vehicle": vehicle,
                    "products": unique_products,
                    "oe_candidates": oe_candidates,
                    "catalog_status": "tecdoc_unavailable",
                    "tecrmi_status": "unavailable",
                    "message": (
                        f"AutoTotal a identificat {len(oe_candidates)} coduri OE după VIN și "
                        f"{len(unique_products)} variante OE/aftermarket cu preț. "
                        "TecRMI rămâne indisponibil pentru acest VIN și nu blochează oferta."
                    ),
                })
                return
            selection_name = " ".join(str(vehicle.get(key) or "") for key in ("make", "model")).strip() or lx_name
            selected_type = _select_vehicle_type(types, selection_name, vehicle.get("engine_or_variant") or "",
                                                 vehicle.get("drivetrain") or "", vehicle.get("year"),
                                                 vehicle.get("power_kw"))
            if not selected_type:
                save(output, {
                    **base,
                    "status": "vehicle_ready" if complete else "vehicle_partial",
                    "vehicle": vehicle,
                    "products": [],
                    "catalog_status": "variant_needs_confirmation",
                    "tecrmi_status": "unavailable",
                    "power_resolution": power_resolution,
                    "message": (
                        "AutoTotal a identificat VIN-ul, dar mai multe variante TecDoc sunt posibile. "
                        "Căutarea continuă independent în Inter Cars, fără alegere automată riscantă."
                    ),
                })
                return
            type_id = str(selected_type.get("typeIdField"))
            vehicle["autototal_type_id"] = type_id
            vehicle["autototal_type_name"] = selected_type.get("typeNameField")
            selected_identity = str(selected_type.get("typeNameField") or "")
            if re.search(r"\b(?:4WD|AWD|4X4|4MATIC|4MOTION)\b", selected_identity, re.I):
                vehicle["drivetrain"] = "4x4"
            elif re.search(r"\b(?:2WD|4X2|RWD|FWD)\b", selected_identity, re.I):
                vehicle["drivetrain"] = "4x2"
            # Publish the exact TecDoc type immediately. TecRMI depends only
            # on this identity and must not wait for the slower OE/product
            # searches to finish.
            save(output, {
                **base,
                "status": "vehicle_ready",
                "vehicle": vehicle,
                "products": [],
                "catalog_status": "tecdoc_vehicle_ready",
                "tecrmi_status": "starting",
                "message": "AutoTotal a identificat tipul TecDoc exact; piesele și timpii continuă în paralel.",
            })
            base_url = str(identification.get("link") or "").split("#", 1)[0]
            await _evaluate(ws, 10, f"location.href={json.dumps(base_url + '#/standard?ti=' + type_id)}; true")
            await asyncio.sleep(2)
            catalog = await _standard_categories(ws, type_id, 11)
            oil_requirements = await _engine_oil_requirements(ws, type_id, 12)
            oil_requirements['oe_brand'] = str(vehicle.get('make') or '')
            oil_requirements['oe_only'] = bool(re.search(r'ulei[^,;\n]*(?:\boe\b|original|origine)', _plain(query)))
            oe_candidates = []
            products = []
            standard_plan = _independent_plan(query)
            oe_targets = _oe_vin_targets(query)
            if oe_targets:
                oe_candidates = await _discover_oe_vin_candidates(ws, identification, vin, query)
                oe_candidates = [candidate for candidate in oe_candidates if _oe_option_allowed(
                    candidate, str(candidate.get("description") or candidate.get("requested_item") or ""), "OE"
                )]
                for index, candidate in enumerate(oe_candidates):
                    try:
                        products.extend(await _search_oe_code_options(
                            ws, candidate, 900 + index * 200
                        ))
                    except Exception as exc:
                        print(f"AUTOTOTAL OE search failed code={candidate.get('oe_code')}: {exc}", flush=True)
            oe_priced_items = {
                _plain(product.get("requested_item")) for product in products
                if product.get("vin_exact_oe_cross_reference")
            }
            for index, item in enumerate(standard_plan):
                # OE-by-VIN cross references are the primary identification
                # path.  Do not mix them with generic category variants for
                # the same requested component.
                if _plain(item.get("requested_item")) in oe_priced_items:
                    continue
                found_products = await _discover_standard_product(
                    ws, item, type_id, catalog, base_url, 30 + index * 10)
                products.extend(found_products)
            # Complete a DSM vehicle with the exact codes fitted previously.
            # This covers items that the standard category tree omits, notably
            # cabin filters, engine oil and the oil-pan drain plug.
            exact_products = []
            exact_candidates = _history_search_candidates(vehicle_hints, query)
            exact_candidates = [candidate for candidate in exact_candidates if _oe_option_allowed(
                candidate, str(candidate.get("description") or candidate.get("requested_item") or ""),
                str(candidate.get("brand") or "OE"),
            )]
            for index, candidate in enumerate(exact_candidates):
                try:
                    found = await _search_code(ws, candidate, 300 + index * 20)
                except Exception:
                    found = None
                if found:
                    exact_products.append(found)
            exact_codes = {
                re.sub(r"[^A-Z0-9]", "", str(p.get("manufacturer_ref") or "").upper())
                for p in exact_products
            }
            products = [p for p in products if re.sub(
                r"[^A-Z0-9]", "", str(p.get("manufacturer_ref") or "").upper()
            ) not in exact_codes] + exact_products
            if ("engine_oil" in _required_product_kinds(query)
                    and not oil_requirements.get('oe_only')
                    and not any(_revision_product_kind(
                        p.get("requested_item") or p.get("description")) == "engine_oil"
                                for p in products)):
                products.extend(await _discover_known_preferred_engine_oil(
                    ws, oil_requirements, 6000))
            if ("engine_oil" in _required_product_kinds(query)
                    and not any(_revision_product_kind(
                        p.get("requested_item") or p.get("description")) == "engine_oil"
                                for p in products)):
                products.extend(await _discover_engine_oil(ws, oil_requirements, 7000))
            products = _keep_one_oil_drain_plug(products)
            required = _required_product_kinds(query)
            found_kinds = {_revision_product_kind(p.get("requested_item") or p.get("description")) for p in products}
            if "accessory_belt_kit" in found_kinds:
                found_kinds.update({"accessory_belt", "accessory_belt_roller", "accessory_belt_tensioner"})
            missing_kinds = sorted(required - found_kinds)
            oe_target_items = {_plain(row.get("requested_item")) for row in oe_targets}
            oe_code_items = {_plain(row.get("requested_item")) for row in oe_candidates}
            oe_priced_items = {
                _plain(row.get("requested_item")) for row in products
                if row.get("vin_exact_oe_cross_reference")
            }
            oe_first_audit = []
            for item in standard_plan:
                name = str(item.get("requested_item") or "")
                normalized = _plain(name)
                if normalized == "kit curea accesorii complet":
                    outcome = "oe_expanded_to_individual_components"
                elif normalized in oe_priced_items:
                    outcome = "oe_priced"
                elif normalized in oe_code_items:
                    outcome = "oe_code_found_tecdoc_fallback"
                elif normalized in oe_target_items:
                    outcome = "oe_not_found_tecdoc_fallback"
                else:
                    outcome = "oe_mapping_missing_tecdoc_fallback"
                oe_first_audit.append({"requested_item": name, "outcome": outcome})
            status = ("ready" if products and not missing_kinds else
                      "partial" if products else
                      ("vehicle_ready" if complete else "vehicle_partial"))
            save(output, {**base, "status": status, "vehicle": vehicle, "products": products,
                          "oe_candidates": oe_candidates,
                          "identification_policy": "OE_VIN_FIRST_THEN_TECDOC_PER_PART",
                          "oe_first_audit": oe_first_audit,
                          "catalog_status": "tecdoc_ready",
                          "tecrmi_status": "ready",
                          "required_product_kinds": sorted(required),
                          "missing_product_kinds": missing_kinds,
                          "haynes_fallback": {"status": "not_started", "message": "Haynes de rezervă se folosește numai manual."},
                          "message": (f"AutoTotal a identificat independent {len(products)} piese și prețuri după VIN."
                                      + (" Lipsesc încă: " + ", ".join(missing_kinds) + "." if missing_kinds else "")
                                      if products else "AutoTotal a identificat vehiculul, dar nu a returnat independent piese cu preț confirmat.")})
    except Exception as exc:
        diagnostic = {**base, "status": "error", "message": str(exc)}
        if "vehicle" in locals():
            diagnostic["vehicle"] = vehicle
        if "vehicle_hints" in locals():
            diagnostic["vehicle_hints"] = vehicle_hints
        save(output, diagnostic)


if __name__ == "__main__":
    hints_arg = Path(sys.argv[5]) if len(sys.argv) > 5 else None
    asyncio.run(run(int(sys.argv[1]), sys.argv[2].upper(), sys.argv[3], Path(sys.argv[4]), hints_arg))
