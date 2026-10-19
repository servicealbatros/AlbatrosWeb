import os
import sqlite3
from pathlib import Path
import json
import threading
from datetime import datetime
from uuid import uuid4
from automotive_vocabulary import expand_technical_request

DATA_DIR=Path(os.getenv("ALBATROS_DATA_DIR", Path(__file__).parent)).expanduser().resolve()
DATA_DIR.mkdir(parents=True,exist_ok=True)
DB_PATH=DATA_DIR / "service_requests.db"
ROBOT_JOBS_FILE=DATA_DIR / "robot_offer_jobs.json"
ROBOT_JOBS_LOCK=threading.RLock()

def _load_robot_jobs():
    with ROBOT_JOBS_LOCK:
        if not ROBOT_JOBS_FILE.exists():
            return []
        try:
            data=json.loads(ROBOT_JOBS_FILE.read_text(encoding="utf-8"))
            return data if isinstance(data,list) else []
        except Exception:
            return []

def _save_robot_jobs(jobs):
    with ROBOT_JOBS_LOCK:
        temporary=ROBOT_JOBS_FILE.with_suffix(".json.tmp")
        temporary.write_text(json.dumps(jobs,ensure_ascii=False,indent=2),encoding="utf-8")
        temporary.replace(ROBOT_JOBS_FILE)

def queue_offer_job(request_id, data):
    vin=str(data.get("vin") or "").strip().upper()
    symptom=str(data.get("symptom") or "").strip()

    if not vin or not symptom:
        return None

    with ROBOT_JOBS_LOCK:
        jobs=_load_robot_jobs()
        now=datetime.now().isoformat(timespec="seconds")

    # MOD MANUAL:
    # Daca exista deja un job pentru aceeasi cerere, apasarea butonului
    # "Cauta piesa in Inter Cars" inseamna RESTART explicit.
    # Este important dupa update/reload de extensie, cand un job vechi poate
    # ramane RUNNING pe server, dar extensia noua nu-l mai are in storage.
        for job in jobs:
            if str(job.get("request_id")) != str(request_id):
                continue

        # O oferta deja aprobata nu este reluata accidental.
            if job.get("status") == "APPROVED":
                continue

            if str(job.get('vin') or '').upper() != vin:
                for field in ('history_parts','dsm_history_parts','dsm_history_labors','history_transaction_id','vehicle_hints'):
                    job.pop(field, None)
            job["previous_run_id"] = job.get("id")
            job["id"] = uuid4().hex
            job["vin"] = vin
            job["customer_request"] = symptom
            job["technical_request"] = expand_technical_request(symptom)
            job["customer_name"] = data.get("customer_name")
            job["customer_phone"] = data.get("phone")
            job["vehicle_hints"] = data.get("vehicle_hints") or {}
            job["source_vin"] = data.get("source_vin")
            job["catalog_vin_note"] = data.get("catalog_vin_note")
            for history_field in ("history_parts", "history_transaction_id",
                                  "dsm_history_parts", "dsm_history_labors"):
                if data.get(history_field) is not None:
                    job[history_field] = data.get(history_field)
            job["status"] = "QUEUED"
            job["stage"] = "manual_restart_from_dashboard"
            job["attempt_count"] = 0
            job["updated_at"] = now
            for field in ("error", "message", "worker_id", "lock_token", "lease_expires_at", "last_progress_at", "last_heartbeat_at", "claimed_at",
                          "cross_help_attempted", "cross_help_source"):
                job.pop(field, None)

            _save_robot_jobs(jobs)

            print(
            f"ALBATROS ROBOT REQUEUE: request=#{request_id}, "
            f"job={job.get('id')}, vin={vin}, query={symptom}"
            )
            return job

    # Niciun job anterior: cream unul nou.
        job={
        "id": uuid4().hex,
        "request_id": int(request_id),
        "vin": vin,
        "customer_request": symptom,
        "technical_request": expand_technical_request(symptom),
        "customer_name": data.get("customer_name"),
        "customer_phone": data.get("phone"),
        "vehicle_hints": data.get("vehicle_hints") or {},
        "source_vin": data.get("source_vin"),
        "catalog_vin_note": data.get("catalog_vin_note"),
        "history_parts": data.get("history_parts") or [],
        "history_transaction_id": data.get("history_transaction_id"),
        "dsm_history_parts": data.get("dsm_history_parts") or [],
        "dsm_history_labors": data.get("dsm_history_labors") or [],
        "status": "QUEUED",
        "stage": "manual_start_from_dashboard",
        "created_at": now,
            "updated_at": now,
            "attempt_count": 0,
        }

        jobs.append(job)
        _save_robot_jobs(jobs)

        print(
        f"ALBATROS ROBOT QUEUED: request=#{request_id}, "
        f"job={job.get('id')}, vin={vin}, query={symptom}"
        )

        return job

def init_db():
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("""CREATE TABLE IF NOT EXISTS service_requests (id INTEGER PRIMARY KEY AUTOINCREMENT, session_id TEXT NOT NULL, customer_name TEXT, phone TEXT, registration TEXT, vin TEXT, vehicle TEXT, mileage TEXT, symptom TEXT NOT NULL, warning_lights TEXT, drivable TEXT, recent_work TEXT, category TEXT, priority TEXT, technical_labour_time REAL, albatros_expected_time REAL, booking_time REAL, labour_source TEXT, labour_operation TEXT, status TEXT DEFAULT 'nou', created_at DATETIME DEFAULT CURRENT_TIMESTAMP)""")
        existing={r[1] for r in conn.execute("PRAGMA table_info(service_requests)").fetchall()}
        for col,typ in {"technical_labour_time":"REAL","albatros_expected_time":"REAL","booking_time":"REAL","labour_source":"TEXT","labour_operation":"TEXT",
                        "request_type":"TEXT","requested_operations":"TEXT","parts_needed":"INTEGER DEFAULT 1","labour_needed":"INTEGER DEFAULT 1",
                        "source":"TEXT DEFAULT 'whatsapp'","conversation_id":"TEXT"}.items():
            if col not in existing: conn.execute(f"ALTER TABLE service_requests ADD COLUMN {col} {typ}")
        conn.execute("""CREATE TABLE IF NOT EXISTS labour_times (id INTEGER PRIMARY KEY AUTOINCREMENT, vin TEXT NOT NULL, operation TEXT NOT NULL, technical_hours REAL NOT NULL, historical_hours REAL, source TEXT NOT NULL DEFAULT 'manual', source_reference TEXT, created_at DATETIME DEFAULT CURRENT_TIMESTAMP)""")
        conn.commit()

def known_dsm_vehicle(registration):
    """Return a vehicle only when every imported record for a plate agrees on VIN."""
    plate="".join(ch for ch in str(registration or "").upper() if ch.isalnum())
    if not plate:
        return None
    try:
        history=json.loads((DATA_DIR / "dsm_history_test.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    rows=history if isinstance(history,list) else [history]
    matching=[r for r in rows if isinstance(r,dict) and
              "".join(ch for ch in str(r.get("registration") or "").upper() if ch.isalnum())==plate]
    vins={str(r.get("vin") or "").upper() for r in matching}
    if len(vins)!=1 or not next(iter(vins)):
        return None
    return sorted(matching,key=lambda r:(str(r.get("service_date") or ""),str(r.get("transaction_id") or "")))[-1]

def create_request(data):
    data=dict(data)
    data.setdefault("request_type", data.get("category") or "SERVICE")
    data.setdefault("requested_operations", json.dumps([data.get("symptom")], ensure_ascii=False) if data.get("symptom") else "[]")
    data.setdefault("parts_needed", 1)
    data.setdefault("labour_needed", 1)
    data.setdefault("source", "whatsapp")
    data.setdefault("conversation_id", data.get("session_id"))
    known=known_dsm_vehicle(data.get("registration"))
    if known and not data.get("vin"):
        data["vin"]=known["vin"]
        for field,key in (("vehicle","vehicle"),("mileage","mileage_km"),("customer_name","customer_name")):
            if not data.get(field) and known.get(key):
                data[field]=str(known[key])
    fields=["session_id","customer_name","phone","registration","vin","vehicle","mileage","symptom","warning_lights","drivable","recent_work","category","priority","technical_labour_time","albatros_expected_time","booking_time","labour_source","labour_operation","request_type","requested_operations","parts_needed","labour_needed","source","conversation_id"]
    vals=[data.get(f) for f in fields]
    with sqlite3.connect(DB_PATH) as conn:
        conn.row_factory=sqlite3.Row
        session_id=str(data.get("session_id") or "").strip()
        registration="".join(ch for ch in str(data.get("registration") or "").upper() if ch.isalnum())
        if session_id:
            previous=conn.execute(
                "SELECT * FROM service_requests WHERE session_id=? ORDER BY id DESC LIMIT 1",
                (session_id,),
            ).fetchone()
            if previous:
                previous_plate="".join(ch for ch in str(previous["registration"] or "").upper() if ch.isalnum())
                same_vehicle=not registration or not previous_plate or registration==previous_plate
                if same_vehicle and str(previous["status"] or "nou").lower() in {"nou", "în lucru", "in lucru"}:
                    updates={field:data[field] for field in fields if field not in {"session_id", "phone"} and data.get(field) is not None}
                    if updates:
                        conn.execute(
                            "UPDATE service_requests SET " + ",".join(f"{field}=?" for field in updates) + " WHERE id=?",
                            [*updates.values(), previous["id"]],
                        )
                        conn.commit()
                    print(f"ALBATROS CERERE ACTUALIZATA #{previous['id']} pentru sesiunea {session_id}")
                    return int(previous["id"])
        cur=conn.execute(
            f"INSERT INTO service_requests ({','.join(fields)}) VALUES ({','.join('?' for _ in fields)})",
            vals
        )
        conn.commit()
        request_id=int(cur.lastrowid)

    # MOD MANUAL:
    # Salvarea cererii NU mai porneste robotul Inter Cars.
    # Robotul intra in coada doar cand consilierul apasa "Cauta piesa"
    # din Dashboard / pagina ofertei.
    print(f"ALBATROS MANUAL MODE - CERERE SALVATA #{request_id}; robotul NU a pornit automat.")
    return request_id

def list_requests(limit=50, include_dsm_accepted=False):
    with sqlite3.connect(DB_PATH) as conn:
        conn.row_factory=sqlite3.Row
        if include_dsm_accepted:
            rows = conn.execute("SELECT * FROM service_requests WHERE source='dsm_accepted_offer' OR id IN (SELECT id FROM service_requests ORDER BY id DESC LIMIT ?) ORDER BY id DESC", (limit,)).fetchall()
        else:
            rows = conn.execute("SELECT * FROM service_requests ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        return [dict(r) for r in rows]

def save_labour_time(data):
    with sqlite3.connect(DB_PATH) as conn:
        cur=conn.execute("INSERT INTO labour_times (vin,operation,technical_hours,historical_hours,source,source_reference) VALUES (?,?,?,?,?,?)",(data['vin'],data['operation'],data['technical_hours'],data.get('historical_hours'),data.get('source','manual'),data.get('source_reference')))
        conn.commit()
        return int(cur.lastrowid)

def find_labour_time(vin,operation):
    vin=vin.strip().upper(); operation=operation.strip().lower()
    with sqlite3.connect(DB_PATH) as conn:
        conn.row_factory=sqlite3.Row
        row=conn.execute("SELECT * FROM labour_times WHERE UPPER(vin)=? AND LOWER(operation)=? ORDER BY id DESC LIMIT 1",(vin,operation)).fetchone()
        if not row:
            row=conn.execute("SELECT * FROM labour_times WHERE UPPER(vin)=? AND (LOWER(operation) LIKE ? OR ? LIKE '%' || LOWER(operation) || '%') ORDER BY id DESC LIMIT 1",(vin,f'%{operation}%',operation)).fetchone()
        return dict(row) if row else None
