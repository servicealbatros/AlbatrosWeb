"""Read-only DSM vehicle lookup through the authenticated robot Edge profile.

The integration deliberately exposes only two DSM reads:
1. search_transactions_home (search by registration number)
2. opening an existing transaction page to read its already stored data

It has no DSM write/update/delete/order/payment method.
"""

from __future__ import annotations

import asyncio
import json
import re
import time
from dataclasses import dataclass
from urllib.request import urlopen
from urllib.parse import urlparse

import websockets


CDP_HTTP = "http://127.0.0.1:9222"
DSM_HOST = "apps.dsmpro.ro"
DSM_LEGACY_HOST = "s.servicemanager.q-service.ro"
DSM_HOSTS = (DSM_HOST, "appm.dsmpro.ro", DSM_LEGACY_HOST)
DSM_BASE = f"https://{DSM_HOST}"
DSM_SEARCH_PATH = "/special/home/search_transactions"


class DsmReadOnlyError(RuntimeError):
    pass


def normalize_registration(value: str) -> str:
    plate = re.sub(r"[^A-Z0-9]", "", str(value or "").upper())
    if not 5 <= len(plate) <= 10:
        raise ValueError("Număr de înmatriculare invalid.")
    return plate


async def _call(ws, call_id: int, method: str, params: dict | None = None) -> dict:
    try:
        await asyncio.wait_for(
            ws.send(json.dumps({"id": call_id, "method": method, "params": params or {}})),
            timeout=5,
        )
    except asyncio.TimeoutError as exc:
        raise DsmReadOnlyError(f"DSM/CDP nu a acceptat comanda {method} în 5 secunde.") from exc
    while True:
        try:
            raw = await asyncio.wait_for(ws.recv(), timeout=20)
        except asyncio.TimeoutError as exc:
            raise DsmReadOnlyError(f"DSM/CDP nu a răspuns la {method} în 20 de secunde.") from exc
        message = json.loads(raw)
        if message.get("id") == call_id:
            if message.get("error"):
                raise DsmReadOnlyError(str(message["error"]))
            return message


def _targets() -> list[dict]:
    try:
        return json.load(urlopen(f"{CDP_HTTP}/json", timeout=3))
    except Exception as exc:
        raise DsmReadOnlyError("Browserul dedicat Albatros nu este disponibil pe portul 9222.") from exc


def _home_target() -> dict:
    targets = [
        item for item in _targets()
        if item.get("type") == "page"
        and urlparse(str(item.get("url") or "")).hostname in DSM_HOSTS
    ]
    if not targets:
        raise DsmReadOnlyError("DSM nu este deschis sau sesiunea robotului nu este autentificată.")
    targets.sort(key=lambda item: 0 if "/special/" in str(item.get("url") or "") else 1)
    return next((item for item in targets if "/special/home/" in item.get("url", "")), targets[0])


async def _search_transactions_once(plate: str) -> list[dict]:
    target = _home_target()
    expression = f"""
        fetch({json.dumps(DSM_SEARCH_PATH)}, {{
          method: 'POST',
          headers: {{
            'Accept': 'application/json',
            'Content-Type': 'application/x-www-form-urlencoded; charset=UTF-8',
            'X-Requested-With': 'XMLHttpRequest',
            'X-CSRF-TOKEN': document.querySelector('meta[name="csrf-token"]')?.content || ''
          }},
          body: new URLSearchParams({{
            search_transaction: {json.dumps(plate)},
            offset: '__OFFSET__', page: '__PAGE__', limit: '100'
          }})
        }}).then(async response => JSON.stringify({{
          status: response.status,
          body: await response.text()
        }}))
    """
    rows = []
    seen_ids = set()
    async with websockets.connect(
        target["webSocketDebuggerUrl"], max_size=8_000_000,
        open_timeout=5, close_timeout=3,
    ) as ws:
        page = 1
        offset = 0
        while True:
            response = await _call(ws, page, "Runtime.evaluate", {
                "expression": expression.replace('__OFFSET__', str(offset)).replace('__PAGE__', str(page)),
                "awaitPromise": True,
                "returnByValue": True,
            })
            value = response.get("result", {}).get("result", {}).get("value")
            if not value:
                raise DsmReadOnlyError("DSM nu a returnat răspuns la căutare.")
            envelope = json.loads(value)
            if envelope.get("status") != 200:
                raise DsmReadOnlyError(f"DSM a răspuns cu status {envelope.get('status')}.")
            payload = json.loads(envelope.get("body") or "[]")
            batch = payload if isinstance(payload, list) else (payload.get("transactions") or payload.get("data") or [])
            new_rows = [row for row in batch if str(row.get('transaction_id') or row.get('id')) not in seen_ids]
            if not new_rows and batch:
                raise DsmReadOnlyError('DSM repetă pagina de istoric; istoricul complet nu poate fi confirmat.')
            for row in new_rows:
                seen_ids.add(str(row.get('transaction_id') or row.get('id')))
            rows.extend(new_rows)
            # Endpoint-ul actual returnează lista integrală ca array. Versiunile
            # paginate anunță explicit totalul sau ultima pagină.
            if isinstance(payload, list):
                break
            total = payload.get('total')
            last_page = payload.get('last_page')
            has_more = (total is not None and len(rows) < int(total)) or (last_page is not None and page < int(last_page)) or bool(payload.get('has_more') or payload.get('next_page_url'))
            if not has_more:
                break
            if not batch:
                raise DsmReadOnlyError('DSM a returnat un istoric incomplet.')
            offset += len(batch)
            page += 1
    exact = []
    for row in rows:
        try:
            if normalize_registration(row.get('car_no') or row.get('car_number') or '') == plate:
                exact.append(row)
        except ValueError:
            continue
    return exact


async def _search_transactions(plate: str) -> list[dict]:
    try:
        return await _search_transactions_once(plate)
    except DsmReadOnlyError:
        await asyncio.sleep(0.5)
        return await _search_transactions_once(plate)


async def _read_transaction(transaction_id: str) -> dict:
    if not str(transaction_id).isdigit():
        raise DsmReadOnlyError("ID DSM invalid.")
    parsed = urlparse(_home_target()['url'])
    transaction_base = f'{parsed.scheme}://{parsed.netloc}'
    browser_ws = json.load(urlopen(f"{CDP_HTTP}/json/version", timeout=3))["webSocketDebuggerUrl"]
    target_id = None
    try:
        async with websockets.connect(browser_ws) as browser:
            created = await _call(browser, 1, "Target.createTarget", {
                "url": f"{transaction_base}/special/transaction/index/{transaction_id}",
                "background": True,
            })
            target_id = created["result"]["targetId"]

        deadline = time.monotonic() + 15
        target = None
        while time.monotonic() < deadline:
            target = next((item for item in _targets() if item.get("id") == target_id), None)
            if target:
                break
            await asyncio.sleep(0.25)
        if not target:
            raise DsmReadOnlyError("Fila temporară DSM nu a pornit.")

        expression = """(async () => {
          const h = window.transaction_head || {};
          const rows = window.transaction_rows || {};
          if (!h.transaction_id) return 'null';
          const postJson = async (url, data) => {
            try {
              const response = await fetch(url, {
                method: 'POST',
                headers: {
                  'Content-Type': 'application/x-www-form-urlencoded; charset=UTF-8',
                  'X-Requested-With': 'XMLHttpRequest'
                },
                body: new URLSearchParams(data)
              });
              return response.ok ? await response.json() : {};
            } catch (_) { return {}; }
          };
          const [client, car] = await Promise.all([
            h.contact_id ? postJson('/special/contact/get_client', {
              contact_id: h.contact_id, transaction_id: h.transaction_id
            }) : {},
            h.car_id ? postJson('/special/car/get_car', {car_id: h.car_id}) : {}
          ]);
          const phones = Array.isArray(client.mobile) ? client.mobile : [];
          const preferredPhone = phones.find(item => item && item.checked === 'checked' && item.val)
            || phones.find(item => item && item.val) || {};
          const mapRows = (group, kind) => Object.values(group || {}).map(row => ({
            row_id: String(row.id || ''),
            kind,
            code: String(row.autopart_code || row.labor_code || ''),
            manufacturer: String(row.manufacturer_code || ''),
            name: String(row.autopart_name || row.labor_name || ''),
            quantity: Number(row.qty || 0),
            unit: String(row.uom || ''),
            hours: kind === 'labor' ? Number(row.qty || 0) : null
          }));
          return JSON.stringify({
            transaction_id: String(h.transaction_id),
            document_code: String(document.body.innerText.match(/TRANZACTIE\\s+([^\\n]+)/i)?.[1] || ''),
            registration: String(h.car_no || ''),
            vin: String(h.car_body_series || '').toUpperCase(),
            registered_car_vin: String(car.car_body_series || '').toUpperCase(),
            vehicle: String(h.car_model_val || [car.car_brand__name, car.car_model__name]
              .filter(Boolean).map(value => String(value).trim()).join(' ')),
            mileage_km: Number(h.transaction_car_km || 0) || null,
            service_date: String(h.transaction_start || ''),
            description: String(h.transaction_description || ''),
            status: String(h.status || ''),
            dsm_car_id: String(h.car_id || ''),
            customer_name: String(client.contact_name || h.client_name || ''),
            customer_phone: String(preferredPhone.val || h.client_phone || ''),
            year: Number(car.car_manufacture_year || 0) || null,
            engine_or_variant: String(car.car_engine || ''),
            power_kw: Number(car.car_horsepower || 0) || null,
            displacement_cc: Number(car.car_engine_capacity || 0) || null,
            haynes_submodel_id: h.car_haynes_submodel_id || null,
            tecrmi_type_id: h.car_tecrmi_type_id || null,
            tecdoc_type_id: h.car_tecdoc_type_id || null,
            parts: mapRows(rows.autoparts, 'part'),
            labors: mapRows(rows.labors, 'labor')
          });
        })()"""
        async with websockets.connect(target["webSocketDebuggerUrl"], max_size=16_000_000) as ws:
            value = None
            while time.monotonic() < deadline:
                result = await _call(ws, 2, "Runtime.evaluate", {
                    "expression": expression,
                    "awaitPromise": True,
                    "returnByValue": True,
                })
                raw = result.get("result", {}).get("result", {}).get("value")
                value = json.loads(raw) if raw else None
                if value:
                    return value
                await asyncio.sleep(0.5)
        raise DsmReadOnlyError(f"Devizul DSM {transaction_id} nu s-a încărcat la timp.")
    finally:
        if target_id:
            try:
                async with websockets.connect(browser_ws) as browser:
                    await _call(browser, 99, "Target.closeTarget", {"targetId": target_id})
            except Exception:
                pass


async def lookup_vehicle_async(registration: str, max_transactions: int = 5) -> dict:
    """Read every returned transaction for this plate.

    max_transactions remains accepted for callers written before full-history
    lookup; it no longer truncates the vehicle's history.
    """
    plate = normalize_registration(registration)
    from dsm_history_cache import lookup_registration
    cached = lookup_registration(plate)
    if cached:
        return cached
    search_rows = await _search_transactions(plate)
    if not search_rows:
        return {"found": False, "registration": plate, "source": "DSM read-only", "transactions": []}

    selected = []
    selected_ids = set()
    for row in search_rows:
        transaction_id = str(row.get('transaction_id') or '')
        if transaction_id in selected_ids:
            continue
        selected.append(row)
        selected_ids.add(transaction_id)
    details = []
    for row in selected:
        try:
            detail = await _read_transaction(str(row.get("transaction_id") or ""))
            detail["vehicle"] = detail.get("vehicle") or " ".join(
                str(row.get(key) or "").strip() for key in ("car_brand__name", "car_model__name")
            ).strip()
            detail["record_date"] = row.get("record_date")
            details.append(detail)
        except DsmReadOnlyError as exc:
            details.append({
                "transaction_id": str(row.get("transaction_id") or ""),
                "read_error": str(exc),
            })

    history_summary = {
        "history_total": len(selected),
        "history_read": sum(not item.get("read_error") for item in details),
        "history_complete": all(not item.get("read_error") for item in details),
    }

    vins = {item.get("vin") for item in details if re.fullmatch(r"[A-HJ-NPR-Z0-9]{17}", item.get("vin") or "")}
    other_vehicle_history = []
    current = details[0] if details else {}
    current_vin = str(current.get('registered_car_vin') or '')
    # Numărul poate fi reutilizat pe altă mașină. Fișa auto actuală confirmă
    # VIN-ul devizului recent; istoricul altui VIN rămâne exclus din ofertare.
    if re.fullmatch(r'[A-HJ-NPR-Z0-9]{17}', current_vin) and current.get('vin') == current_vin:
        other_vehicle_history = [item for item in details if item.get('vin') and item.get('vin') != current_vin]
        details = [item for item in details if item.get('vin') == current_vin or item.get('read_error')]
        vins = {current_vin}
    history_summary['other_vehicle_history'] = other_vehicle_history
    if len(vins) != 1:
        return {
            "found": True,
            "confirmed": False,
            "registration": plate,
            "reason": "VIN lipsă sau neconcordant între devize; necesită verificare umană.",
            "source": "DSM read-only",
            "transactions": details,
            **history_summary,
        }
    latest = next(item for item in details if item.get("vin") in vins)
    mileage = next((item.get("mileage_km") for item in details
                    if item.get("vin") in vins and item.get("mileage_km") is not None), None)
    customer_name = next((str(item.get("customer_name") or "").strip() for item in details
                          if item.get("vin") in vins and str(item.get("customer_name") or "").strip()), "")
    customer_phone = next((str(item.get("customer_phone") or "").strip() for item in details
                           if item.get("vin") in vins and str(item.get("customer_phone") or "").strip()), "")
    year = next((item.get("year") for item in details
                 if item.get("vin") in vins and item.get("year")), None)
    engine_or_variant = next((str(item.get("engine_or_variant") or "").strip() for item in details
                              if item.get("vin") in vins and str(item.get("engine_or_variant") or "").strip()), "")
    power_kw = next((item.get("power_kw") for item in details
                     if item.get("vin") in vins and item.get("power_kw")), None)
    displacement_cc = next((item.get("displacement_cc") for item in details
                            if item.get("vin") in vins and item.get("displacement_cc")), None)
    return {
        "found": True,
        "confirmed": True,
        "registration": plate,
        "vin": next(iter(vins)),
        "vehicle": latest.get("vehicle") or "",
        "mileage_km": mileage,
        "customer_name": customer_name,
        "customer_phone": customer_phone,
        "year": year,
        "engine_or_variant": engine_or_variant,
        "power_kw": power_kw,
        "displacement_cc": displacement_cc,
        "dsm_car_id": latest.get("dsm_car_id"),
        "haynes_submodel_id": latest.get("haynes_submodel_id"),
        "tecrmi_type_id": latest.get("tecrmi_type_id"),
        "tecdoc_type_id": latest.get("tecdoc_type_id"),
        "source": "DSM read-only",
        "transactions": details,
        **history_summary,
    }


def lookup_vehicle(registration: str, max_transactions: int = 5) -> dict:
    return asyncio.run(lookup_vehicle_async(registration, max_transactions=max_transactions))


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Caută numai pentru citire o mașină în DSM.")
    parser.add_argument("registration")
    parser.add_argument("--max-transactions", type=int, default=5)
    args = parser.parse_args()
    print(json.dumps(lookup_vehicle(args.registration, args.max_transactions), ensure_ascii=False, indent=2))
