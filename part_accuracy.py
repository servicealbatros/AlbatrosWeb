"""Deterministic fitment confidence for supplier results.

The score is an evidence gate used by the dashboard.  A score of 99 means
the candidate may be prepared automatically; it is not presented as a
statistical guarantee.  Any identity, axle or side conflict blocks it.
"""

from __future__ import annotations

import re
import unicodedata


def _plain(value) -> str:
    text = unicodedata.normalize("NFKD", str(value or "").casefold())
    return re.sub(r"\s+", " ", text.encode("ascii", "ignore").decode("ascii")).strip()


def _code(value) -> str:
    return re.sub(r"[^A-Z0-9]", "", str(value or "").upper())


def _part_key(product: dict) -> str:
    text = _plain(product.get("requested_item") or product.get("description"))
    families = (
        ("etrier", "brake_caliper"), ("placut", "brake_pads"),
        ("cap de bara", "tie_rod_end"), ("articulatie directie", "tie_rod_end"),
        ("bascul", "control_arm"), ("brat suspensie", "control_arm"),
        ("amortiz", "shock"), ("filtru ulei", "oil_filter"),
        ("filtru aer", "air_filter"), ("filtru combustibil", "fuel_filter"),
        ("filtru habitaclu", "cabin_filter"),
    )
    return next((key for token, key in families if token in text), text)


def _position_conflict(product: dict) -> str | None:
    wanted = _plain(product.get("requested_item"))
    description = _plain(product.get("description") or product.get("name"))
    position = _plain(product.get("position"))
    side = _plain(product.get("side"))
    requested_rear = "spate" in wanted or position == "spate"
    requested_front = "fata" in wanted or position == "fata"
    requested_left = any(x in wanted for x in (" stg", "stanga")) or side == "stanga"
    requested_right = any(x in wanted for x in (" dr", "dreapta")) or side == "dreapta"
    if requested_rear and "fata" in description and "spate" not in description:
        return "conflict punte: cerut spate, rezultat față"
    if requested_front and "spate" in description and "fata" not in description:
        return "conflict punte: cerut față, rezultat spate"
    if requested_left and "dreapta" in description and "stanga" not in description:
        return "conflict parte: cerut stânga, rezultat dreapta"
    if requested_right and "stanga" in description and "dreapta" not in description:
        return "conflict parte: cerut dreapta, rezultat stânga"
    return None


def annotate_part_accuracy(products: list[dict], identity_review: dict | None = None) -> list[dict]:
    """Return product copies annotated with evidence, score and auto eligibility."""
    identity_review = identity_review or {}
    identity_conflict = identity_review.get("status") == "needs_confirmation"
    sources_by_candidate: dict[tuple[str, str], set[str]] = {}
    for product in products or []:
        reference = _code(product.get("manufacturer_ref") or product.get("reference"))
        if not reference:
            continue
        key = (_part_key(product), reference)
        sources_by_candidate.setdefault(key, set()).add(_plain(product.get("source")))

    annotated = []
    for original in products or []:
        product = dict(original)
        reasons: list[str] = []
        conflict = _position_conflict(product)
        reference = _code(product.get("manufacturer_ref") or product.get("reference"))
        source_count = len(sources_by_candidate.get((_part_key(product), reference), set()))

        if conflict:
            score = 0
            reasons.append(conflict)
        elif identity_conflict:
            score = 49
            reasons.append("identitatea vehiculului are un conflict între surse")
        elif product.get("dsm_history_match"):
            score = 99
            reasons.append("același cod a fost montat anterior pe același VIN în DSM")
        elif product.get("vin_exact_oe_cross_reference") and product.get("oe_code"):
            score = 99
            reasons.append("echivalență pornită din cod OE decodat exact după VIN")
        elif source_count >= 2:
            score = 99
            reasons.append("același cod și aceeași piesă sunt confirmate de minimum două surse")
        elif product.get("oe_code"):
            score = 94
            reasons.append("există referință OE, dar lipsește a doua confirmare independentă")
        else:
            score = 80
            reasons.append("rezultat dintr-un singur catalog, fără legătură OE demonstrată")

        product["accuracy_score"] = score
        product["accuracy_band"] = "CONFIRMAT_99" if score >= 99 else ("RESPINS" if score == 0 else "VERIFICARE")
        product["auto_selection_eligible"] = score >= 99 and not conflict and not identity_conflict
        product["accuracy_reasons"] = reasons
        product["independent_source_count"] = source_count
        annotated.append(product)
    return annotated

