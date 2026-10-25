"""Vocabular românesc popular -> termeni tehnici folosiți la ofertare.

Textul clientului rămâne neschimbat în cerere. Funcția de extindere adaugă
denumirile canonice doar în traseele interne de identificare și manoperă.
"""
from __future__ import annotations

import re
import unicodedata


TECHNICAL_VOCABULARY = (
    ("bucșă braț suspensie față poziția spate", (
        "bucsa bascula fata partea din spate", "bucșă basculă față partea din spate",
        "bucsa bascula fata parte spate", "bucsa spate bascula fata",
    )),
    ("bucșă braț suspensie față poziția față", (
        "bucsa bascula fata partea din fata", "bucșă basculă față partea din față",
        "bucsa bascula fata parte fata", "bucsa fata bascula fata",
    )),
    ("braț suspensie superior punte față", (
        "bascula superioara fata", "basculă superioară față", "brat superior fata", "braț superior față",
    )),
    ("braț suspensie superior punte spate", (
        "bascula superioara spate", "basculă superioară spate", "brat superior spate", "braț superior spate",
    )),
    ("braț suspensie", ("bascula", "basculă", "trapez", "brat suspensie", "braț suspensie")),
    ("bucșă braț suspensie", ("bucsa bascula", "bucșă basculă", "silentbloc bascula", "silent block bascula")),
    ("pivot suspensie", ("pivot", "pivot bascula", "rotula suspensie", "rotulă suspensie")),
    ("capăt de bară direcție", (
        "cap bara", "cap bară", "capat bara", "capăt bară", "capul de bara", "capul de bară",
        "cap bieleta directie", "cap bieletă direcție", "cap de bieleta directie", "cap de bieletă direcție",
        "capul de bieleta directie", "capul de bieletă direcție", "rotula directie", "rotulă direcție",
    )),
    ("bieletă axială direcție", ("bieleta directie", "bieletă direcție", "bieleta axiala", "bieletă axială", "bara axiala", "bară axială")),
    ("bieletă bară stabilizatoare", (
        "bieleta antiruliu", "bieletă antiruliu", "bieleta stabilizatoare", "bieletă stabilizatoare",
        "bieleta bar stab", "bieletă bară stab", "bileta bara stabilizatoare",
        "bilete bara stabilizatoare", "bilete bară stabilizatoare",
    )),
    ("bucșă bară stabilizatoare", ("bucsa stabilizatoare", "bucșă stabilizatoare", "bucsa bara", "bucșă bară", "bucsi bara stab", "bucși bară stab", "bucsa bara stab", "bucșă bară stab")),
    ("amortizor", ("telescop", "telescoape", "amortizor", "amortizoare")),
    ("flanșă amortizor", ("flansa amortizor", "flanșă amortizor", "flansa telescop", "flanșă telescop")),
    ("rulment sarcină amortizor", ("rulment sarcina", "rulment sarcină", "rulment flansa", "rulment flanșă")),
    ("arc suspensie", ("arc suspensie", "arc elicoidal")),
    ("set plăcuțe frână spate", ("placute spate", "plăcuțe spate", "pastile frana spate", "pastile frână spate")),
    ("set plăcuțe frână față", ("placute fata", "plăcuțe față", "pastile frana fata", "pastile frână față")),
    ("set plăcuțe frână", ("placute", "plăcuțe", "pastile frana", "pastile frână")),
    ("senzor uzură plăcuțe frână", (
        "senzor uzura placute", "senzor uzură plăcuțe", "senzor placute frana",
        "senzor plăcuțe frână", "martor uzura placute", "martor uzură plăcuțe",
    )),
    ("disc frână față", ("discuri fata", "disc fata")),
    ("disc frână spate", ("discuri spate", "disc spate")),
    ("disc frână", ("discuri", "discuri frana", "discuri frână")),
    ("etrier frână spate", (
        "etrier spate", "etrier frana spate", "etrier frână spate",
        "etrier punte spate", "etrier punte spate stanga", "etrier punte spate stângă",
        "etrier spate stanga", "etrier spate stângă", "etrier stg spate",
        "etrier stanga spate", "etrier stângă spate",
    )),
    ("etrier frână față", ("etrier fata", "etrier față", "etrier frana fata", "etrier frână față")),
    ("etrier frână", ("etrier", "etrier frana", "etrier frână")),
    ("saboți frână", ("saboti", "saboți", "ferodouri spate")),
    ("kit arcuri saboți frână", (
        "arcuri saboti", "arcuri saboți", "arculete", "arculețe", "arcuțe",
        "arculete saboti", "arculețe saboți", "arcutele sabotilor", "arcuțele saboților",
        "set arculete saboti", "set arculețe saboți", "kit arcuri saboti",
    )),
    ("cablu frână de parcare", ("cablu frana mana", "cablu frână mână", "cablu frana de mana")),
    ("lichid de frână", ("lichid frana", "lichid frână")),
    ("rulment roată", ("rulment roata", "rulment roată", "butuc roata", "butuc roată")),
    ("articulație planetară", ("cap planetara", "cap planetară", "articulatie planetara", "articulație planetară")),
    ("arbore planetar", ("planetara", "planetară", "semiaxa", "semiaxă")),
    ("burduf planetară", ("burduf planetara", "burduf planetară")),
    ("rulment suport arbore cardanic", (
        "rulment cardan", "rulment cardanic", "rulment intermediar cardan",
        "rulment suport cardan", "lagar cardan", "lagăr cardan",
    )),
    ("arbore cardanic", ("cardan", "arbore cardan", "arbore cardanic")),
    ("bucșă suport diferențial", (
        "bucsa grup", "bucșă grup", "bucsi grup", "bucși grup",
        "tampon grup", "suport grup", "bucsa diferential", "bucșă diferențial",
    )),
    ("kit ambreiaj", ("ambreiaj", "kit ambreiaj", "placa disc rulment")),
    ("volantă cu masă dublă", ("volanta", "volantă", "volanta dubla", "volantă dublă")),
    ("kit distribuție pe lanț", ("distributie pe lant", "distribuție pe lanț", "lant de distributie", "lanț de distribuție", "lanturi de distributie", "lanțuri de distribuție", "distributie lant", "timing chain")),
    ("kit distribuție", ("distributie", "distribuție", "kit distributie", "kit distribuție")),
    ("kit curea accesorii complet: curea, role, întinzător și pompă de apă", (
        "kit curea accesorii", "kit curea de accesorii", "kit accesorii",
        "kit curea acessorii", "kit curea de acessorii", "kit acessorii",
        "kit curea auxiliara", "kit curea auxiliară",
    )),
    ("curea accesorii", (
        "curea accesorii", "curea de accesorii", "cureaua accesorii", "cureaua de accesorii",
        "curea alternator", "curea auxiliara", "curea auxiliară",
        "curea acessorii", "curea de acessorii", "cureaua acessorii",
    )),
    ("rolă întinzătoare", ("rola intinzatoare", "rolă întinzătoare", "intinzator curea", "întinzător curea")),
    ("rolă ghidare curea accesorii", ("rola curea accesorii", "rolă curea accesorii", "rola ghidare curea", "rolă ghidare curea")),
    ("pompă de apă", ("pompa apa", "pompă apă", "pompa de apa", "pompă de apă")),
    ("filtru habitaclu", ("filtru polen", "filtru habitaclu", "filtru cabina", "filtru cabină")),
    ("filtru combustibil", ("filtru motorina", "filtru motorină", "filtru benzina", "filtru benzină")),
    ("bușon golire baie ulei", ("buson baie", "bușon baie", "surub baie ulei", "șurub baie ulei")),
    ("reglaj geometrie direcție", ("reglaj directie", "reglaj direcție", "geometrie", "unghiuri roti", "unghiuri roți")),
)


def normalize_technical_text(value: str) -> str:
    text = unicodedata.normalize("NFKD", str(value or "").casefold())
    text = "".join(char for char in text if not unicodedata.combining(char))
    return " ".join(re.sub(r"[^a-z0-9]+", " ", text).split())


def canonical_terms(value: str) -> list[str]:
    normalized = normalize_technical_text(value)
    # O singură denumire poate indica ambele punți: „plăcuțe față și spate”.
    def repeat_axles(match):
        noun, first, second = match.groups()
        return f'{noun} {first} {noun} {second}'
    normalized = re.sub(r'\b(placute|discuri|amortizoare)\s+(fata|spate)\s+(?:si\s+)?(fata|spate)\b', repeat_axles, normalized)
    candidates = []
    for order, (canonical, aliases) in enumerate(TECHNICAL_VOCABULARY):
        for alias in aliases:
            candidate = normalize_technical_text(alias)
            pattern = r"(?<![a-z0-9])" + re.escape(candidate).replace(r"\ ", r"\s+") + r"(?![a-z0-9])"
            for match in re.finditer(pattern, normalized):
                candidates.append((-(match.end() - match.start()), order, match.start(), match.end(), canonical))

    # Expresiile specifice câștigă în fața celor generale: „cap planetară”
    # nu devine simultan și „planetară”, iar „bucșă basculă” nu devine și braț.
    occupied = []
    selected = []
    for _, order, start, end, canonical in sorted(candidates):
        if any(start < used_end and end > used_start for used_start, used_end in occupied):
            continue
        occupied.append((start, end))
        selected.append((start, order, canonical))

    found = []
    for _, _, canonical in sorted(selected):
        if canonical not in found:
            found.append(canonical)
    return found


def expand_technical_request(value: str) -> str:
    from timing_chain_policy import expand_chain_scope
    original = expand_chain_scope(value)
    terms = canonical_terms(original)
    if not terms:
        return original
    return original + " | TERMENI TEHNICI: " + "; ".join(terms)
