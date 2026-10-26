"""Read-only DSM appointment import into the local Albatros scheduler.

DSM is only navigated and read through the authenticated dedicated browser.
This module has no DSM create/update/delete operation.
"""
from __future__ import annotations

import asyncio
from contextlib import closing
import json
import re
import sqlite3
import time
import unicodedata
from datetime import date, datetime, timedelta
from pathlib import Path
from urllib.request import urlopen
from urllib.parse import urlparse

import websockets


CDP_HTTP = "http://127.0.0.1:9222"
DSM_HOST = "apps.dsmpro.ro"
DSM_LEGACY_HOST = "s.servicemanager.q-service.ro"
DSM_HOSTS = (DSM_HOST, "appm.dsmpro.ro", DSM_LEGACY_HOST)
DSM_SCHEDULE_URL = f"https://{DSM_HOST}/special/appointments/index"


class DsmScheduleError(RuntimeError):
    pass


def _fold(value: str) -> str:
    text = unicodedata.normalize("NFKD", str(value or ""))
    text = "".join(ch for ch in text if not unicodedata.combining(ch)).upper()
    text = re.sub(r"\([^)]*\)", " ", text)
    return re.sub(r"[^A-Z0-9]+", "", text)


def _plain_fold(value: str) -> str:
    text = unicodedata.normalize("NFKD", str(value or ""))
    return "".join(ch for ch in text if not unicodedata.combining(ch)).upper()


def _daily_schedule_slice(start_at: str, end_at: str, target_date: date) -> tuple[str, str, bool]:
    """Clip a DSM appointment to one workshop day.

    DSM legitimately displays multi-day jobs on every covered date.  The local
    scheduler stores one daily block so the job is visible and included in the
    mechanic's message on each day, without creating an 80-hour calendar card.
    """
    start = datetime.fromisoformat(str(start_at))
    end = datetime.fromisoformat(str(end_at))
    if end <= start or not (start.date() <= target_date <= end.date()):
        raise DsmScheduleError(
            f"Intervalul DSM {start_at}–{end_at} nu acoperă ziua {target_date.isoformat()}."
        )
    opening = datetime.combine(target_date, datetime.min.time()).replace(hour=8, minute=30)
    closing = opening.replace(hour=17, minute=0)
    daily_start = max(start, opening)
    daily_end = min(end, closing)
    if daily_end <= daily_start:
        raise DsmScheduleError(
            f"Intervalul DSM {start_at}–{end_at} nu are ore lucrătoare în {target_date.isoformat()}."
        )
    return (
        daily_start.isoformat(timespec="minutes"),
        daily_end.isoformat(timespec="minutes"),
        start.date() != end.date(),
    )


def _targets() -> list[dict]:
    try:
        return json.load(urlopen(f"{CDP_HTTP}/json/list", timeout=3))
    except Exception as exc:
        raise DsmScheduleError("Browserul dedicat Albatros nu răspunde pe portul 9222.") from exc


def _dsm_target() -> dict:
    pages = [
        row for row in _targets()
        if row.get("type") == "page"
        and urlparse(str(row.get("url") or "")).hostname in DSM_HOSTS
    ]
    if not pages:
        raise DsmScheduleError("Fila DSM nu este deschisă în browserul robotului.")
    pages.sort(key=lambda row: (0 if '/special/' in str(row.get('url') or '') else 1,
                                0 if '/appointments/' in str(row.get('url') or '') else 1))
    if '/login' in urlparse(str(pages[0].get('url') or '')).path:
        raise DsmScheduleError('Autentificare DSM necesară. Conectați-vă în fila DSM existentă, apoi reluați importul.')
    return pages[0]


async def _call(ws, call_id: int, method: str, params: dict | None = None) -> dict:
    await asyncio.wait_for(
        ws.send(json.dumps({"id": call_id, "method": method, "params": params or {}})), timeout=5
    )
    while True:
        message = json.loads(await asyncio.wait_for(ws.recv(), timeout=20))
        pending = getattr(ws, '_dsm_pending_reads', None)
        if pending is not None:
            params = message.get('params') or {}
            if message.get('method') == 'Network.requestWillBeSent' and params.get('type') in {'Fetch', 'XHR'}:
                pending.add(params['requestId'])
                ws._dsm_last_read_activity = time.monotonic()
            elif message.get('method') in {'Network.loadingFinished', 'Network.loadingFailed'} and params.get('requestId') in pending:
                pending.discard(params['requestId'])
                ws._dsm_last_read_activity = time.monotonic()
        if message.get("id") == call_id:
            if message.get("error"):
                raise DsmScheduleError(str(message["error"]))
            return message


async def _eval(ws, call_id: int, expression: str):
    result = await _call(ws, call_id, "Runtime.evaluate", {
        "expression": expression, "awaitPromise": True, "returnByValue": True,
    })
    return result.get("result", {}).get("result", {}).get("value")


async def _wait_ready(ws, call_id: int) -> None:
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        ready = await _eval(ws, call_id, "document.readyState + '|' + location.pathname")
        if '/login' in str(ready):
            raise DsmScheduleError('Autentificare DSM necesară. Conectați-vă în fila DSM existentă, apoi reluați importul.')
        if any(
            str(ready).startswith(f"complete|{path}")
            for path in (
                "/special/appointments/index",
                "/index.php/special/appointments/index",
            )
        ):
            await asyncio.sleep(1)
            return
        await asyncio.sleep(0.25)
    raise DsmScheduleError("Pagina Programări DSM nu s-a încărcat în 20 de secunde.")


_schedule_read_lock = asyncio.Lock()


from browser_resources import browser_operation

@browser_operation
async def read_dsm_schedule(target_date: date) -> list[dict]:
    async with _schedule_read_lock:
        return await _read_dsm_schedule_existing_tab(target_date)


async def _read_dsm_schedule_existing_tab(target_date: date) -> list[dict]:
    """Return mechanical appointments for one day without writing to DSM."""
    target = _dsm_target()
    original_url = str(target['url'])
    parsed = urlparse(original_url)
    prefix = '/index.php' if '/index.php/' in parsed.path else ''
    schedule_url = f'{parsed.scheme}://{parsed.netloc}{prefix}/special/appointments/index'
    navigated = False
    try:
        async with websockets.connect(
            target["webSocketDebuggerUrl"],
            open_timeout=5, close_timeout=3, max_size=16_000_000,
        ) as ws:
            ws._dsm_pending_reads = set()
            ws._dsm_last_read_activity = time.monotonic()
            await _call(ws, 0, 'Network.enable')
            if urlparse(original_url).path != urlparse(schedule_url).path:
                await _call(ws, 1, 'Page.navigate', {'url': schedule_url})
                navigated = True
            await _wait_ready(ws, 2)
            wanted = target_date.isoformat()
            for offset in range(11):
                shown = await _eval(
                    ws, 100 + offset,
                    "document.querySelector('.fc-col-header-cell.fc-resource')?.getAttribute('data-date') || ''",
                )
                if shown == wanted:
                    break
                if not shown:
                    await asyncio.sleep(0.5)
                    continue
                action = "next" if shown < wanted else "prev"
                clicked = await _eval(
                    ws, 200 + offset,
                    f"(() => {{const e=document.querySelector('[data-appointments-day-action=\"{action}\"]'); if(!e)return false; e.click(); return true;}})()",
                )
                if not clicked:
                    raise DsmScheduleError("DSM nu permite schimbarea zilei în programator.")
                await asyncio.sleep(0.8)
            else:
                raise DsmScheduleError(f"DSM nu a ajuns la data {wanted} în intervalul maxim de 10 zile.")

            expression = """(() => {
              const root=document.getElementById('appointments-day-calendar');
              const calendar=root?.__k?.props?.children?.[0]?.props?.emitter?.thisContext;
              if(!calendar || typeof calendar.getEvents!=='function') return JSON.stringify({error:'Calendarul DSM nu este disponibil; importul nu a fost executat.'});
              const resources=[...document.querySelectorAll('.fc-col-header-cell.fc-resource')].map(e=>({
                id:e.getAttribute('data-resource-id'), name:(e.innerText||'').trim()
              }));
              const events=calendar.getEvents().map(event=>{
                const extra=event.extendedProps||{};
                return {
                  id:String(event.id||''),
                  title:String(event.title||''),
                  start:String(event.startStr||'').slice(0,19),
                  end:String(event.endStr||'').slice(0,19),
                  resource_id:String(event.getResources?.()[0]?.id||extra.appointment_resource_id||''),
                  appointment_code:String(extra.appointment_code||''),
                  registration:String(extra.car_number||''),
                  operations:String(extra.description||extra.work_summary||extra.services||''),
                  services:String(extra.services||''),
                  status:String(extra.appointment_status||'')
                };
              });
              const day=calendar.getDate?.();
              const view_date=day ? day.getFullYear()+'-'+String(day.getMonth()+1).padStart(2,'0')+'-'+String(day.getDate()).padStart(2,'0') : '';
              const loading=Object.values(calendar.getCurrentData?.()?.eventSources||{}).some(x=>x.isFetching);
              return JSON.stringify({resources,events,view_date,loading});
            })()"""
            return await _wait_valid_schedule_snapshot(ws, expression, target_date)
    finally:
        if navigated:
            try:
                async with websockets.connect(target['webSocketDebuggerUrl'], open_timeout=5, close_timeout=3) as ws:
                    current = await _eval(ws, 900, 'location.href')
                    if '/appointments/' in str(current):
                        await _call(ws, 901, 'Page.navigate', {'url': original_url})
                        # Page.navigate acknowledges before the tab has finished
                        # loading. Starting the next day's import in that gap
                        # can make CDP temporarily report no DSM tab.
                        await _wait_restored_page(ws, original_url)
            except Exception:
                pass


async def _wait_restored_page(ws, original_url: str) -> None:
    deadline = time.monotonic() + 15
    wanted = urlparse(original_url)
    while time.monotonic() < deadline:
        state = await _eval(ws, 902, "JSON.stringify({url:location.href,ready:document.readyState})")
        data = json.loads(state or "{}")
        current = urlparse(str(data.get("url") or ""))
        if (current.hostname == wanted.hostname and current.path == wanted.path
                and data.get("ready") == "complete"):
            return
        await asyncio.sleep(0.25)
    raise DsmScheduleError("Fila DSM nu a terminat revenirea la pagina inițială.")


async def _wait_valid_schedule_snapshot(ws, expression, target_date):
    deadline = time.monotonic() + 20
    previous = None
    error = DsmScheduleError('Calendarul DSM încă se încarcă.')
    while time.monotonic() < deadline:
        snapshot = json.loads(await _eval(ws, 300, expression) or '{}')
        settled = not getattr(ws, '_dsm_pending_reads', set()) and time.monotonic() - getattr(ws, '_dsm_last_read_activity', 0) >= 0.5
        try:
            if snapshot.get('error'):
                raise DsmScheduleError(snapshot['error'])
            if 'events' not in snapshot or 'resources' not in snapshot:
                raise DsmScheduleError('Snapshot DSM incomplet; importul a fost oprit.')
            if snapshot.get('view_date') != target_date.isoformat() or snapshot.get('loading') or not settled:
                raise DsmScheduleError('Calendarul DSM încă se încarcă pentru ziua solicitată.')
            # FullCalendar's event store can retain cached events outside the
            # visible day. Wait for reads to settle, then import only overlaps.
            begin = datetime.combine(target_date, datetime.min.time())
            end = begin + timedelta(days=1)
            visible = []
            for event in snapshot['events']:
                try:
                    starts = datetime.fromisoformat(event['start'])
                    ends = datetime.fromisoformat(event['end'])
                except (ValueError, KeyError):
                    raise DsmScheduleError('Programare DSM cu interval invalid.')
                if ends <= starts:
                    raise DsmScheduleError('Programare DSM cu durată invalidă.')
                if starts < end and ends > begin:
                    visible.append(event)
            rows = _appointments_from_snapshot({**snapshot, 'events': visible}, target_date)
            if snapshot == previous:
                return rows
            previous = snapshot
        except DsmScheduleError as exc:
            error = exc
            previous = None
        await asyncio.sleep(0.5)
    raise error


def _resource_department(name: str) -> str:
    label = _plain_fold(name)
    return "tinichigerie_vopsitorie" if any(term in label for term in ("TINICH", "VOPS")) else "mecanica"


def _appointments_from_snapshot(snapshot: dict, target_date: date) -> list[dict]:
    resource_names = {
        str(row.get("id") or ""): str(row.get("name") or "")
        for row in snapshot.get("resources") or []
    }
    appointments: list[dict] = []
    for event in snapshot.get("events") or []:
        mechanic = resource_names.get(str(event.get("resource_id") or ""), "")
        title = str(event.get("title") or "").strip()
        references = [part.strip() for part in title.split("/") if part.strip()]
        appointment_code = str(event.get("appointment_code") or "").strip() or next(
            (part for part in references if part.upper().startswith("APP-")), ""
        )
        if not appointment_code:
            event_id = str(event.get("id") or "").strip()
            if not event_id:
                raise DsmScheduleError(f"Programarea DSM {title!r} nu are identificator.")
            appointment_code = "DSM-ID-" + event_id
        raw_start = str(event.get("start") or "").strip()
        raw_end = str(event.get("end") or "").strip()
        if not raw_start or not raw_end:
            raise DsmScheduleError(f"Programarea DSM {appointment_code} nu are interval complet.")
        daily_start, daily_end, multi_day = _daily_schedule_slice(raw_start, raw_end, target_date)
        operations = str(event.get("operations") or "").strip() or "Programare DSM"
        services = [item.strip() for item in str(event.get("services") or "").split(",") if item.strip()]
        appointments.append({
            "dsm_appointment_id": f"{appointment_code}@{target_date.isoformat()}" if multi_day else appointment_code,
            "job_number": " / ".join(references) or appointment_code,
            "mechanic": re.sub(r"\([^)]*\)", "", mechanic).strip(),
            "registration": str(event.get("registration") or "").strip(),
            "department": _resource_department(mechanic),
            "status": "anulat" if _plain_fold(str(event.get("status") or "")) in {"CANCELED", "CANCELLED", "ANULAT"} else "programat",
            "customer_name": "",
            "phone": "",
            "start_at": daily_start,
            "end_at": daily_end,
            "operations": operations,
            "vehicle": "",
            "services": services,
        })
    codes = [str(row.get("dsm_appointment_id") or "").upper() for row in appointments]
    if len(codes) != len(set(codes)):
        raise DsmScheduleError(
            f"Import DSM duplicat: {len(appointments)} programări, {len(set(codes))} coduri unice."
        )
    for row in appointments:
        if not str(row.get("start_at") or "").startswith(target_date.isoformat()):
            raise DsmScheduleError(
                f"Programarea {row.get('dsm_appointment_id')} are data internă "
                f"{row.get('start_at') or 'lipsă'}, nu aparține zilei {target_date.isoformat()}."
            )
    return appointments


def ensure_automation_tables(db_path: Path) -> None:
    with closing(sqlite3.connect(db_path)) as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS scheduler_automation_runs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                kind TEXT NOT NULL,
                run_date TEXT NOT NULL,
                status TEXT NOT NULL,
                attempts INTEGER NOT NULL DEFAULT 0,
                details TEXT,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(kind,run_date)
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS scheduler_mechanic_schedule_deliveries (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                schedule_date TEXT NOT NULL,
                technician_id INTEGER NOT NULL,
                phone TEXT,
                message_id TEXT,
                status TEXT NOT NULL,
                details TEXT,
                sent_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(schedule_date,technician_id)
            )
        """)
        conn.commit()


def automation_succeeded(db_path: Path, kind: str, run_date: date) -> bool:
    ensure_automation_tables(db_path)
    with closing(sqlite3.connect(db_path)) as conn:
        row = conn.execute(
            "SELECT status FROM scheduler_automation_runs WHERE kind=? AND run_date=?",
            (kind, run_date.isoformat()),
        ).fetchone()
    return bool(row and row[0] == "success")


def automation_attempts(db_path: Path, kind: str, run_date: date) -> int:
    ensure_automation_tables(db_path)
    with closing(sqlite3.connect(db_path)) as conn:
        row = conn.execute(
            "SELECT attempts FROM scheduler_automation_runs WHERE kind=? AND run_date=?",
            (kind, run_date.isoformat()),
        ).fetchone()
    return int(row[0] or 0) if row else 0


def mechanic_schedule_was_sent(db_path: Path, schedule_date: date, technician_id: int) -> bool:
    ensure_automation_tables(db_path)
    with closing(sqlite3.connect(db_path)) as conn:
        row = conn.execute(
            """SELECT status FROM scheduler_mechanic_schedule_deliveries
               WHERE schedule_date=? AND technician_id=?""",
            (schedule_date.isoformat(), int(technician_id)),
        ).fetchone()
    return bool(row and row[0] in {"accepted_by_meta", "sent", "delivered", "read"})


def record_mechanic_schedule_delivery(
    db_path: Path, schedule_date: date, technician_id: int, phone: str,
    message_id: str, status: str, details: object,
) -> None:
    ensure_automation_tables(db_path)
    payload = json.dumps(details, ensure_ascii=False, default=str)
    with closing(sqlite3.connect(db_path)) as conn:
        conn.execute("""
            INSERT INTO scheduler_mechanic_schedule_deliveries
              (schedule_date,technician_id,phone,message_id,status,details)
            VALUES(?,?,?,?,?,?)
            ON CONFLICT(schedule_date,technician_id) DO UPDATE SET
              phone=excluded.phone,message_id=excluded.message_id,status=excluded.status,
              details=excluded.details,sent_at=CURRENT_TIMESTAMP
        """, (schedule_date.isoformat(), int(technician_id), phone, message_id, status, payload))
        conn.commit()


def record_automation_run(db_path: Path, kind: str, run_date: date, status: str, details: object) -> None:
    ensure_automation_tables(db_path)
    payload = json.dumps(details, ensure_ascii=False, default=str)
    with closing(sqlite3.connect(db_path)) as conn:
        conn.execute("""
            INSERT INTO scheduler_automation_runs(kind,run_date,status,attempts,details)
            VALUES(?,?,?,1,?)
            ON CONFLICT(kind,run_date) DO UPDATE SET
              status=excluded.status,attempts=scheduler_automation_runs.attempts+1,
              details=excluded.details,updated_at=CURRENT_TIMESTAMP
        """, (kind, run_date.isoformat(), status, payload))
        conn.commit()


def sync_local_schedule(db_path: Path, rows: list[dict]) -> dict:
    """Idempotently insert/update captured DSM rows in the local database."""
    with closing(sqlite3.connect(db_path)) as conn:
        conn.row_factory = sqlite3.Row
        techs = { _fold(row["name"]): dict(row) for row in conn.execute(
            "SELECT * FROM scheduler_technicians WHERE active=1"
        ).fetchall() }
        inserted = updated = 0
        skipped: list[dict] = []
        warnings: list[dict] = []
        for row in rows:
            mechanic = str(row.get("mechanic") or "")
            tech = techs.get(_fold(mechanic))
            if not tech:
                source_tokens = set(re.findall(r"[A-Z0-9]+", re.sub(r"\([^)]*\)", "", _plain_fold(mechanic))))
                matches = [candidate for candidate in techs.values()
                           if set(re.findall(r"[A-Z0-9]+", _plain_fold(candidate["name"]))).issubset(source_tokens)]
                tech = matches[0] if len(matches) == 1 else None
            if not tech:
                warnings.append({"appointment": row.get("dsm_appointment_id"), "mechanic": mechanic,
                                 "reason": "angajat neasociat; programarea este păstrată în Nerepartizat"})
            dsm_id = str(row.get("dsm_appointment_id") or "").strip()
            if not dsm_id:
                skipped.append({"appointment": "", "mechanic": row.get("mechanic"), "reason": "lipsește codul APP"})
                continue
            start = datetime.fromisoformat(str(row["start_at"])); end = datetime.fromisoformat(str(row["end_at"]))
            duration = round((end - start).total_seconds() / 3600, 2)
            operations = str(row.get("operations") or "Programare DSM")
            start_text = start.isoformat(timespec="minutes")
            end_text = end.isoformat(timespec="minutes")
            existing = conn.execute(
                """SELECT id,source,customer_name,phone,vehicle
                   FROM scheduler_appointments WHERE dsm_appointment_id=?""",
                (dsm_id,),
            ).fetchone()
            # Noul calendar DSM expune direct programarea, dar nu include mereu
            # clientul, telefonul sau descrierea completă a vehiculului. La o
            # resincronizare nu ștergem datele deja cunoscute local.
            customer_name = str(row.get("customer_name") or "").strip()
            phone = str(row.get("phone") or "").strip()
            vehicle = str(row.get("vehicle") or "").strip()
            if existing:
                customer_name = customer_name or str(existing["customer_name"] or "")
                phone = phone or str(existing["phone"] or "")
                vehicle = vehicle or str(existing["vehicle"] or "")
            values = (
                int(tech["id"]) if tech else None, start_text, end_text,
                customer_name, phone,
                str(row.get("registration") or ""), vehicle,
                str(row.get("job_number") or dsm_id), operations,
                duration, "Import automat read-only din programatorul DSM. Angajat DSM: " + mechanic,
                str((tech or {}).get("department") or row.get("department") or "mecanica"), dsm_id,
                operations, start_text, end_text,
            )
            if existing:
                if existing['source'] == 'mechanic_takeover':
                    # A mechanic has explicitly taken responsibility locally.
                    # Refresh DSM evidence/contact data without assigning the
                    # car back to its old mechanic on the next import.
                    conn.execute("""UPDATE scheduler_appointments SET customer_name=?,phone=?,vehicle=?,
                        dsm_original_operations=?,dsm_original_start_at=?,dsm_original_end_at=?,updated_at=CURRENT_TIMESTAMP
                        WHERE id=?""", (customer_name,phone,vehicle,operations,start_text,end_text,int(existing['id'])))
                    updated += 1
                    continue
                conn.execute("""
                    UPDATE scheduler_appointments SET technician_id=?,start_at=?,end_at=?,status=CASE WHEN status='finalizat' THEN status ELSE 'programat' END,
                      customer_name=?,phone=?,registration=?,vehicle=?,job_number=?,operations=?,estimated_hours=?,
                      notes=?,source='dsm_schedule_readonly',department=?,dsm_appointment_id=?,
                      dsm_original_operations=?,dsm_original_start_at=?,dsm_original_end_at=?,updated_at=CURRENT_TIMESTAMP
                    WHERE id=?
                """, values + (int(existing["id"]),))
                updated += 1
            else:
                conn.execute("""
                    INSERT INTO scheduler_appointments
                    (request_id,technician_id,start_at,end_at,status,customer_name,phone,registration,vehicle,
                     job_number,operations,estimated_hours,notes,source,department,dsm_appointment_id,
                     dsm_original_operations,dsm_original_start_at,dsm_original_end_at)
                    VALUES(NULL,?,?,?,'programat',?,?,?,?,?,?,?,?,'dsm_schedule_readonly',?,?,?,?,?)
                """, values)
                inserted += 1
        conn.commit()
    from dsm_accepted_offers import reconcile_existing_bookings
    programmed_ids = reconcile_existing_bookings(db_path)
    return {"inserted": inserted, "updated": updated, "skipped": skipped, "warnings": warnings,
            "captured": len(rows), "programmed_request_ids": programmed_ids}


async def import_dsm_schedule(db_path: Path, target_date: date) -> dict:
    rows = await read_dsm_schedule(target_date)
    result = sync_local_schedule(db_path, rows)
    result["date"] = target_date.isoformat()
    return result
