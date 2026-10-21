"""Independent AutoTotal TecRMI labour-time worker.

This process deliberately writes to a different output file from HaynesPro so
the two catalogues can run at the same time and neither can overwrite the
other one's result.
"""
from pathlib import Path
import asyncio
import sys

from technician_request_worker_v6_time_parse_fix import (
    _autototal_repair_times,
    _save_ready_labour,
    _wait_for_autototal_identification,
    detect_operations,
    save,
)


def main() -> None:
    if len(sys.argv) < 6:
        print("Usage: tecrmi_request_worker.py REQUEST_ID VIN REQUEST_TEXT OUTPUT_JSON OFFER_RESULT_JSON")
        raise SystemExit(2)

    request_id = int(sys.argv[1])
    vin = sys.argv[2].strip()
    request_text = sys.argv[3].strip()
    output = Path(sys.argv[4])
    offer_result_path = sys.argv[5].strip()
    operations = detect_operations(request_text)

    if not operations:
        save(output, {
            "status": "needs_verification",
            "request_id": request_id,
            "vin": vin,
            "symptom": request_text,
            "detected_operations": [],
            "message": "TecRMI nu a putut asocia solicitarea cu o operație tehnică standard.",
            "source": "AutoTotal TecRMI / timpi reparație",
        })
        return

    identified = _wait_for_autototal_identification(request_id, offer_result_path)
    if not identified:
        save(output, {
            "status": "unavailable",
            "request_id": request_id,
            "vin": vin,
            "symptom": request_text,
            "detected_operations": operations,
            "resolved_operations": [],
            "unresolved_operations": operations,
            "engine_oil_liters": 0,
            "engine_oil_specification": "",
            "engine_oil_options": [],
            "message": "AutoTotal nu a publicat tipul TecDoc exact în intervalul permis; nu am interogat TecRMI pe o variantă nesigură.",
            "source": "AutoTotal TecRMI / timpi reparație",
        })
        return
    try:
        result = asyncio.run(_autototal_repair_times(request_id, offer_result_path, operations))
    except Exception as exc:
        save(output, {
            "status": "error",
            "request_id": request_id,
            "vin": vin,
            "symptom": request_text,
            "detected_operations": operations,
            "message": f"TecRMI nu a putut citi timpii din AutoTotal: {exc}",
            "source": "AutoTotal TecRMI / timpi reparație",
        })
        return

    if result and result.get("resolved") and not result.get("unresolved"):
        _save_ready_labour(
            output, request_id, vin, request_text, operations,
            result["resolved"], "AutoTotal TecRMI / timpi reparație", result,
        )
        return

    save(output, {
        "status": "needs_verification" if result else "unavailable",
        "request_id": request_id,
        "vin": vin,
        "symptom": request_text,
        "detected_operations": operations,
        "resolved_operations": (result or {}).get("resolved") or [],
        "unresolved_operations": (result or {}).get("unresolved") or operations,
        "engine_oil_liters": (result or {}).get("engine_oil_liters") or 0,
        "engine_oil_specification": (result or {}).get("engine_oil_specification") or "",
        "engine_oil_options": (result or {}).get("engine_oil_options") or [],
        "message": (
            "TecRMI a găsit numai o parte dintre operații; rezultatul HaynesPro poate completa traseul."
            if result else
            "TecRMI nu este disponibil pentru identificarea AutoTotal a acestui VIN; HaynesPro continuă independent."
        ),
        "source": "AutoTotal TecRMI / timpi reparație",
    })


if __name__ == "__main__":
    from browser_resources import browser_activity_sync
    with browser_activity_sync():
        main()
