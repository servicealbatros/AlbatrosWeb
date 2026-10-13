"""Termeni OE englezești folosiți de cataloagele VIN AutoTotal/Laximo.

Cheile coincid cu vocabularul tehnic românesc. ``group_id`` este grupa rapidă
TecDoc/Laximo; termenii sunt intenționat exacți, pentru a nu confunda o piesă
completă cu șuruburile, bucșele sau kiturile aflate în aceeași planșă OE.
"""

OE_ENGLISH_PARTS = {
    "bucșă braț suspensie față poziția spate": {"group_id": 10681, "terms": ("rear rubber mounting", "rear hydrobearing", "rear control arm bushing")},
    "bucșă braț suspensie față poziția față": {"group_id": 10681, "terms": ("front rubber mounting", "front hydrobearing", "front control arm bushing")},
    "braț suspensie superior punte față": {"group_id": 10681, "terms": ("upper control arm", "upper wishbone", "upper traction strut")},
    "braț suspensie superior punte spate": {"group_id": 10681, "terms": ("upper control arm", "upper wishbone", "upper traction strut")},
    "braț suspensie": {"group_id": 10681, "terms": ("control arm", "wishbone", "traction strut")},
    "bucșă braț suspensie": {"group_id": 10681, "terms": ("rubber mounting", "hydrobearing", "control arm bushing", "wishbone bushing")},
    "pivot suspensie": {"group_id": 10681, "terms": ("ball joint", "wheel suspension joint")},
    "capăt de bară direcție": {"group_id": 10298, "terms": ("tie rod end", "steering ball joint", "left tie rod", "right tie rod")},
    "bieletă axială direcție": {"group_id": 10703, "terms": ("axial joint", "inner tie rod", "track rod")},
    "bieletă bară stabilizatoare": {"group_id": 10690, "terms": ("swing support", "stabilizer link", "anti-roll bar link", "sway bar link")},
    "bucșă bară stabilizatoare": {"group_id": 10690, "terms": ("stabilizer rubber mounting", "anti-roll bar bush", "sway bar bushing")},
    "amortizor": {"group_id": 10221, "terms": ("shock absorber", "spring strut", "damper")},
    "flanșă amortizor": {"group_id": 10471, "terms": ("strut mount", "supporting mount", "guide support")},
    "rulment sarcină amortizor": {"group_id": 10471, "terms": ("strut bearing", "support bearing", "thrust bearing")},
    "arc suspensie": {"group_id": 10213, "terms": ("coil spring", "suspension spring")},
    "set plăcuțe frână spate": {"group_id": 10130, "terms": ("repair kit, brake pads", "set of brake pads")},
    "set plăcuțe frână față": {"group_id": 10130, "terms": ("repair kit, brake pads", "set of brake pads")},
    "set plăcuțe frână": {"group_id": 10130, "terms": ("repair kit, brake pads", "set of brake pads")},
    "senzor uzură plăcuțe frână": {"group_id": 10130, "terms": ("brake pad wear sensor", "wear indicator")},
    "disc frână": {"group_id": 10130, "terms": ("brake disc", "brake rotor")},
    "etrier frână spate": {"group_id": 10127, "terms": ("brake caliper", "caliper housing")},
    "etrier frână față": {"group_id": 10127, "terms": ("brake caliper", "caliper housing")},
    "etrier frână": {"group_id": 10127, "terms": ("brake caliper", "caliper housing")},
    "saboți frână": {"group_id": 10131, "terms": ("brake shoe", "repair kit, brake shoe")},
    "kit arcuri saboți frână": {"group_id": 10734, "terms": ("repair kit springs", "brake shoe spring kit", "accessory kit, brake shoes")},
    "cablu frână de parcare": {"group_id": 10735, "terms": ("parking brake cable", "handbrake cable")},
    "lichid de frână": {"group_id": 12308, "terms": ("brake fluid",)},
    "rulment roată": {"group_id": 10679, "terms": ("wheel bearing", "wheel hub")},
    "articulație planetară": {"group_id": 10687, "terms": ("cv joint", "constant velocity joint", "joint kit, drive shaft")},
    "arbore planetar": {"group_id": 10162, "terms": ("drive shaft", "axle shaft", "half shaft")},
    "burduf planetară": {"group_id": 10174, "terms": ("cv boot", "drive shaft boot", "bellow set, drive shaft")},
    "rulment suport arbore cardanic": {"group_id": 10939, "terms": ("propshaft centre bearing", "drive shaft support bearing", "center bearing")},
    "arbore cardanic": {"group_id": 10803, "terms": ("propeller shaft", "cardan shaft", "drive shaft complete")},
    "bucșă suport diferențial": {"group_id": 10515, "terms": ("differential mount", "differential mounting bush", "rubber mounting, differential")},
    "kit ambreiaj": {"group_id": 10151, "terms": ("clutch kit", "repair kit clutch", "clutch set")},
    "volantă cu masă dublă": {"group_id": 10157, "terms": ("dual mass flywheel", "flywheel")},
    "kit distribuție": {"group_id": 10116, "terms": ("timing belt kit", "timing chain kit", "repair kit, timing belt")},
    "curea accesorii": {"group_id": 10531, "terms": ("ribbed v-belt", "serpentine belt", "drive belt")},
    "rolă întinzătoare": {"group_id": 10534, "terms": ("tensioner pulley", "belt tensioner")},
    "rolă ghidare curea accesorii": {"group_id": 10534, "terms": ("idler pulley", "guide pulley", "deflection pulley")},
    "pompă de apă": {"group_id": 10188, "terms": ("water pump", "coolant pump")},
    "filtru ulei": {
        "group_id": 10359,
        "terms": (
            "oil filter", "filter element",
            "filtru de ulei", "cartus filtru", "element filtrant ulei",
        ),
    },
    "filtru aer": {
        "group_id": 10360,
        "terms": ("air filter", "air cleaner element", "filtru aer", "filtru de aer", "cartus filtru aer", "cartus filtru de aer", "element filtrant aer"),
    },
    "filtru habitaclu": {
        "group_id": 10363,
        "terms": ("cabin air filter", "pollen filter", "microfilter", "filtru habitaclu", "filtru praf si polen"),
    },
    "filtru combustibil": {
        "group_id": 10361,
        "terms": ("fuel filter", "filtru combustibil", "filtru carburant", "filtru motorina"),
    },
    "bușon golire baie ulei": {
        "group_id": 10591,
        "terms": (
            "oil drain plug",
            "screw plug, oil sump",
            "screw plug with sealing ring",
            # Denumirea folosită de planșele OE VAG în limba română.
            "surub obturator cu inel de etansare",
        ),
    },
    # Operație de atelier, fără reper OE.
    "reglaj geometrie direcție": {"group_id": None, "terms": ("wheel alignment", "steering geometry")},
}


def english_oe_terms(canonical: str) -> tuple[str, ...]:
    return tuple(OE_ENGLISH_PARTS.get(canonical, {}).get("terms") or ())
