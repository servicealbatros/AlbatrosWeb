"""Independent AutoTotal TecRMI labour-time worker.

This process deliberately writes to a different output file from HaynesPro so
the two catalogues can run at the same time and neither can overwrite the
other one's result.
"""
from pathlib import Path
from datetime import datetime
import asyncio
import os
import sys

from labour_vehicle import identity_path, hints_path, read_identity

from technician_request_worker_v6_time_parse_fix import (
    _autototal_repair_times,
    _save_ready_labour,
    _wait_for_autototal_identification,
    detect_operations,
    save,
)


async def standalone_identity(request_id, vin, request_text, output, offer_result_path):
    """Identify the VIN ourselves; never wait for or create a parts job."""
    from autototal_request_worker import run as identify
    from worker_run_guard import write_worker_result
    target = identity_path(output)
    previous = Path(offer_result_path).with_name(f'request_{request_id}_autototal.json')
    cached = read_identity(previous, request_id, vin)
    if ((cached.get('vehicle') or {}).get('autototal_type_id')
            and cached.get('catalog_status') in {'tecdoc_vehicle_ready', 'tecdoc_ready'}):
        write_worker_result(target, cached, os.getenv('ALBATROS_WORKER_RUN_ID', ''))
        return read_identity(target, request_id, vin)
    budget = max(60, int(os.getenv('ALBATROS_LABOUR_TIMEOUT_SECONDS', '240')))
    await asyncio.wait_for(identify(request_id, vin, request_text, target,
                                  hints_path(output), identification_only=True), timeout=min(90, budget * .4))
    return read_identity(target, request_id, vin)


def main() -> None:
    if len(sys.argv) < 6:
        print("Usage: tecrmi_request_worker.py REQUEST_ID VIN REQUEST_TEXT OUTPUT_JSON OFFER_RESULT_JSON")
        raise SystemExit(2)

    request_id = int(sys.argv[1])
    vin = sys.argv[2].strip()
    request_text = sys.argv[3].strip()
    output = Path(sys.argv[4])
    offer_result_path = sys.argv[5].strip()
    standalone = '--standalone' in sys.argv[6:]
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

    identity_file = None
    try:
        if standalone:
            identity = asyncio.run(standalone_identity(request_id, vin, request_text, output, offer_result_path))
            identity_file = identity_path(output)
            identified = bool((identity.get('vehicle') or {}).get('autototal_type_id'))
        else:
            budget = max(60, int(os.getenv('ALBATROS_LABOUR_TIMEOUT_SECONDS', '240')))
            identified = _wait_for_autototal_identification(request_id, offer_result_path,
                timeout_seconds=max(1, budget - 120), expected_vin=vin)
    except Exception:
        save(output, {'status':'error', 'request_id':request_id, 'vin':vin, 'symptom':request_text,
                      'detected_operations':operations, 'source':'AutoTotal TecRMI',
                      'message':'Identificarea independentă AutoTotal nu s-a încheiat; verifică sesiunea și datele vehiculului.'})
        return
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
            "message": (str(identity.get('message') or 'AutoTotal nu a identificat o variantă TecDoc unică.') if standalone
                        else "AutoTotal nu a publicat tipul TecDoc exact în intervalul permis; nu am interogat TecRMI pe o variantă nesigură."),
            "source": "AutoTotal TecRMI / timpi reparație",
        })
        return
    try:
        save(output, {'status':'running', 'request_id':request_id, 'vin':vin, 'symptom':request_text,
                      'started_at':datetime.now().isoformat(timespec='seconds'),
                      'detected_operations':operations, 'phase':'reading_labour',
                      'source':'AutoTotal TecRMI', 'message':'Vehicul identificat; TecRMI citește timpii operațiilor solicitate.'})
        async def search():
            budget = max(60, int(os.getenv('ALBATROS_LABOUR_TIMEOUT_SECONDS', '240')))
            return await asyncio.wait_for(_autototal_repair_times(
                request_id, offer_result_path, operations, identity_file=identity_file, expected_vin=vin), timeout=min(90, budget * .4))
        result = asyncio.run(search())
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
