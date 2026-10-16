"""Local, read/write test scheduler for Albatros AI.

The module never writes to DSM or Q-Service.  It stores technicians and test
appointments only in the SAFE project's SQLite database.
"""
from __future__ import annotations

from datetime import datetime, timedelta
import calendar as calendar_module
import json
import math
import os
import re
import sqlite3
from pathlib import Path


SKILL_LABELS = {
    "revizie": "Revizii / întreținere",
    "frane": "Frâne",
    "suspensie_directie": "Suspensie / direcție",
    "ambreiaj": "Ambreiaj",
    "distributie": "Distribuție",
    "motor": "Motor",
    "diagnoza": "Diagnoză",
    "electrica": "Electrică",
    "climatizare": "Climatizare",
    "geometrie": "Geometrie roți",
    "tinichigerie": "Tinichigerie / reparații caroserie",
    "vopsitorie": "Vopsitorie",
    "pregatire_vopsitorie": "Pregătire pentru vopsire",
    "accesorii_caroserie": "Uși, geamuri, broaște, faruri și stopuri",
    "radiatoare": "Radiatoare AC, răcire și încălzire",
}

DEPARTMENT_LABELS = {
    "mecanica": "Mecanică",
    "tinichigerie_vopsitorie": "Tinichigerie–Vopsitorie",
}

BODYSHOP_SKILLS = {
    "tinichigerie", "vopsitorie", "pregatire_vopsitorie",
    "accesorii_caroserie", "radiatoare",
}

SKILL_DEFAULT_HOURS = {
    "revizie": 2.5, "frane": 2.0, "suspensie_directie": 3.0,
    "ambreiaj": 5.0, "distributie": 4.0, "motor": 4.0,
    "diagnoza": 1.0, "electrica": 2.0, "climatizare": 2.0, "geometrie": 1.0,
    "tinichigerie": 4.0, "vopsitorie": 5.0, "pregatire_vopsitorie": 3.0,
    "accesorii_caroserie": 2.0, "radiatoare": 3.0,
}

SKILL_PART_TERMS = {
    "revizie": [(r"revizie", "revizie"), (r"ulei", "ulei"), (r"filtr", "filtre")],
    "frane": [(r"disc", "discuri"), (r"placut", "plăcuțe"), (r"etrier", "etrier"),
              (r"sabot", "saboți"), (r"senzor.*uzur", "senzor uzură")],
    "suspensie_directie": [(r"amortiz", "amortizoare"), (r"bielet", "bielete"),
                            (r"pivot", "pivoți"), (r"bascul|brat", "brațe/bascule"),
                            (r"cap.*bara", "capete de bară"), (r"bucsa", "bucșe"),
                            (r"rulment.*sarc", "rulmenți sarcină"), (r"arc", "arcuri")],
    "ambreiaj": [(r"ambreiaj", "ambreiaj"), (r"volanta", "volantă")],
    "distributie": [(r"distribut", "distribuție"), (r"curea.*dint", "curea distribuție")],
    "motor": [(r"motor", "motor"), (r"inject", "injecție"), (r"turb", "turbină")],
    "diagnoza": [(r"diagno", "diagnoză"), (r"constata", "constatare"),
                  (r"tronc|zgomot", "verificare zgomot")],
    "electrica": [(r"electric", "electrică"), (r"alternator", "alternator"),
                   (r"electromotor", "electromotor"), (r"bater", "baterie")],
    "climatizare": [(r"clima|aer conditionat|freon", "climatizare")],
    "geometrie": [(r"geometr|reglaj directie", "reglaj direcție")],
    "tinichigerie": [(r"tinich|caroser|indrept|sud|prag|lonjeron|aripa|portier|bara", "reparații caroserie")],
    "vopsitorie": [(r"vops|revops|lacui", "vopsitorie")],
    "pregatire_vopsitorie": [(r"pregat.*vops|chitui|slefui|grundui", "pregătire vopsitorie")],
    "accesorii_caroserie": [
        (r"cablu.*(?:deschidere|us)|(?:deschidere|us).*cablu", "cablu deschidere ușă"),
        (r"(?:nu\s+se\s+deschid|deschidere).*us|us.*(?:nu\s+se\s+deschid|deschidere)",
         "mecanism deschidere ușă"),
        (r"broasc", "broască ușă"),
        (r"incuiet", "încuietoare ușă"),
        (r"macara.*geam|geam.*macara", "macara geam"),
        (r"fata.*us", "față de ușă"),
        (r"\bfar(?:uri)?\b", "far"),
        (r"\bstop(?:uri)?\b", "stop"),
    ],
    "radiatoare": [(r"radiator|condensator", "radiatoare")],
}


def init_scheduler_db(db_path: Path) -> None:
    with sqlite3.connect(db_path) as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS scheduler_technicians (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL UNIQUE,
                color TEXT NOT NULL DEFAULT '#2563eb',
                active INTEGER NOT NULL DEFAULT 1,
                work_start TEXT NOT NULL DEFAULT '08:30',
                work_end TEXT NOT NULL DEFAULT '17:00',
                skills_json TEXT NOT NULL DEFAULT '{}',
                notes TEXT,
                phone TEXT,
                department TEXT NOT NULL DEFAULT 'mecanica',
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS scheduler_appointments (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                request_id INTEGER,
                technician_id INTEGER,
                start_at TEXT NOT NULL,
                end_at TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'programat',
                customer_name TEXT,
                phone TEXT,
                registration TEXT,
                vehicle TEXT,
                job_number TEXT,
                continued_from_id INTEGER,
                completion_note TEXT,
                operations TEXT NOT NULL,
                estimated_hours REAL NOT NULL,
                notes TEXT,
                source TEXT NOT NULL DEFAULT 'albatros_safe_local',
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                department TEXT NOT NULL DEFAULT 'mecanica',
                dsm_original_operations TEXT,
                dsm_original_start_at TEXT,
                dsm_original_end_at TEXT,
                FOREIGN KEY(technician_id) REFERENCES scheduler_technicians(id)
            )
        """)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_scheduler_range ON scheduler_appointments(start_at,end_at)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_scheduler_technician ON scheduler_appointments(technician_id,start_at)")
        existing = {row[1] for row in conn.execute("PRAGMA table_info(scheduler_appointments)").fetchall()}
        if "job_number" not in existing:
            conn.execute("ALTER TABLE scheduler_appointments ADD COLUMN job_number TEXT")
        if "continued_from_id" not in existing:
            conn.execute("ALTER TABLE scheduler_appointments ADD COLUMN continued_from_id INTEGER")
        if "completion_note" not in existing:
            conn.execute("ALTER TABLE scheduler_appointments ADD COLUMN completion_note TEXT")
        if "dsm_appointment_id" not in existing:
            conn.execute("ALTER TABLE scheduler_appointments ADD COLUMN dsm_appointment_id TEXT")
        conn.execute("""
            CREATE UNIQUE INDEX IF NOT EXISTS idx_scheduler_dsm_appointment
            ON scheduler_appointments(dsm_appointment_id)
            WHERE dsm_appointment_id IS NOT NULL AND dsm_appointment_id!=''
        """)
        technician_columns = {row[1] for row in conn.execute("PRAGMA table_info(scheduler_technicians)").fetchall()}
        if "phone" not in technician_columns:
            conn.execute("ALTER TABLE scheduler_technicians ADD COLUMN phone TEXT")
        if "department" not in technician_columns:
            conn.execute("ALTER TABLE scheduler_technicians ADD COLUMN department TEXT NOT NULL DEFAULT 'mecanica'")
        if "department" not in existing:
            conn.execute("ALTER TABLE scheduler_appointments ADD COLUMN department TEXT NOT NULL DEFAULT 'mecanica'")
        if "dsm_original_operations" not in existing:
            conn.execute("ALTER TABLE scheduler_appointments ADD COLUMN dsm_original_operations TEXT")
        if "dsm_original_start_at" not in existing:
            conn.execute("ALTER TABLE scheduler_appointments ADD COLUMN dsm_original_start_at TEXT")
        if "dsm_original_end_at" not in existing:
            conn.execute("ALTER TABLE scheduler_appointments ADD COLUMN dsm_original_end_at TEXT")
        conn.execute("""
            UPDATE scheduler_appointments
            SET dsm_original_operations=COALESCE(dsm_original_operations,operations),
                dsm_original_start_at=COALESCE(dsm_original_start_at,start_at),
                dsm_original_end_at=COALESCE(dsm_original_end_at,end_at)
            WHERE source LIKE 'dsm%' AND source!='dsm_capacity_split_local'
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS scheduler_notifications (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                appointment_id INTEGER,
                request_id INTEGER,
                phone TEXT,
                message TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending_review',
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
        """)
        notification_columns = {row[1] for row in conn.execute("PRAGMA table_info(scheduler_notifications)").fetchall()}
        if "sent_at" not in notification_columns:
            conn.execute("ALTER TABLE scheduler_notifications ADD COLUMN sent_at TEXT")
        if "last_error" not in notification_columns:
            conn.execute("ALTER TABLE scheduler_notifications ADD COLUMN last_error TEXT")
        if "auto_send_after" not in notification_columns:
            conn.execute("ALTER TABLE scheduler_notifications ADD COLUMN auto_send_after TEXT")
        if "attempt_count" not in notification_columns:
            conn.execute("ALTER TABLE scheduler_notifications ADD COLUMN attempt_count INTEGER NOT NULL DEFAULT 0")
        conn.execute("UPDATE scheduler_technicians SET work_start='08:30' WHERE work_start='08:00'")
        conn.execute("""
            CREATE TABLE IF NOT EXISTS scheduler_mechanic_messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                technician_id INTEGER NOT NULL,
                appointment_id INTEGER,
                sender TEXT NOT NULL,
                message TEXT NOT NULL,
                action TEXT,
                status TEXT NOT NULL DEFAULT 'received',
                is_beta_test INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY(technician_id) REFERENCES scheduler_technicians(id),
                FOREIGN KEY(appointment_id) REFERENCES scheduler_appointments(id)
            )
        """)
        mechanic_columns = {row[1] for row in conn.execute("PRAGMA table_info(scheduler_mechanic_messages)").fetchall()}
        if "is_beta_test" not in mechanic_columns:
            conn.execute("ALTER TABLE scheduler_mechanic_messages ADD COLUMN is_beta_test INTEGER NOT NULL DEFAULT 0")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_mechanic_messages_sender ON scheduler_mechanic_messages(sender,status,created_at)")
        conn.execute("""
            CREATE TABLE IF NOT EXISTS dashboard_yard_orders (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                source_key TEXT NOT NULL UNIQUE,
                job_number TEXT NOT NULL,
                opened_at TEXT,
                status TEXT NOT NULL DEFAULT 'in_curte',
                service_request_id INTEGER,
                customer_name TEXT,
                phone TEXT,
                registration TEXT,
                vin TEXT,
                vehicle TEXT,
                operations TEXT,
                notes TEXT,
                source TEXT NOT NULL DEFAULT 'dsm_readonly',
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
        """)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_yard_orders_status ON dashboard_yard_orders(status,updated_at)")
        conn.commit()


def _row(row) -> dict:
    result = dict(row)
    if "skills_json" in result:
        try:
            result["skills"] = json.loads(result.pop("skills_json") or "{}")
        except (TypeError, ValueError):
            result["skills"] = {}
    return result


def list_technicians(db_path: Path, include_inactive: bool = False) -> list[dict]:
    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        where = "" if include_inactive else "WHERE active=1"
        return [_row(row) for row in conn.execute(
            f"SELECT * FROM scheduler_technicians {where} ORDER BY active DESC,name"
        ).fetchall()]


def save_technician(db_path: Path, payload: dict, technician_id: int | None = None) -> dict:
    name = str(payload.get("name") or "").strip()
    if not name:
        raise ValueError("Numele mecanicului este obligatoriu.")
    skills = {
        key: max(0, min(5, int(value or 0)))
        for key, value in (payload.get("skills") or {}).items()
        if key in SKILL_LABELS
    }
    department = str(payload.get("department") or "mecanica").strip()
    if department not in DEPARTMENT_LABELS:
        department = "mecanica"
    values = (
        name,
        str(payload.get("phone") or "").strip(),
        str(payload.get("color") or "#2563eb"),
        1 if payload.get("active", True) else 0,
        str(payload.get("work_start") or "08:30"),
        str(payload.get("work_end") or "17:00"),
        json.dumps(skills, ensure_ascii=False),
        str(payload.get("notes") or "").strip(),
        department,
    )
    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        if technician_id:
            conn.execute("""
                UPDATE scheduler_technicians
                SET name=?,phone=?,color=?,active=?,work_start=?,work_end=?,skills_json=?,notes=?,department=?,updated_at=CURRENT_TIMESTAMP
                WHERE id=?
            """, (*values, technician_id))
            row_id = technician_id
        else:
            cur = conn.execute("""
                INSERT INTO scheduler_technicians
                (name,phone,color,active,work_start,work_end,skills_json,notes,department)
                VALUES (?,?,?,?,?,?,?,?,?)
            """, values)
            row_id = int(cur.lastrowid)
        conn.commit()
        row = conn.execute("SELECT * FROM scheduler_technicians WHERE id=?", (row_id,)).fetchone()
        if not row:
            raise ValueError("Mecanicul nu a fost găsit.")
        return _row(row)


def classify_operations(text: str) -> list[str]:
    value = str(text or "").casefold()
    value = "".join(ch for ch in __import__("unicodedata").normalize("NFKD", value)
                    if not __import__("unicodedata").combining(ch))
    patterns = {
        "revizie": r"revizie|ulei|filtr|intretinere",
        "frane": r"fran|placut|disc|etrier|sabot|incinge.*roat|roat.*incinge",
        "suspensie_directie": r"suspens|directi|bielet|pivot|bascul|amortiz|cap de bara|bucsa|arc|rulment.*rot|verificat.*rulment|probleme.*cardan|rulment.*cardan|grup spate|planetar",
        "ambreiaj": r"ambreiaj|volanta",
        "distributie": r"distribut|curea dintata|\blant(?:uri)?\b",
        "motor": r"motor|chiulas|inject|turb|compres|pierderi antigel|pierderi motorina|fum|galopeaz|\begr\b|vas.*expansi|pompa.*vacc?um|dpf",
        "diagnoza": r"diagno|martor|eroare|constatare|tronc|trosn|zgomot|nu porneste|nu mai trage|se limiteaza|check|proba.*drum|trepidat",
        "electrica": r"electric|alternator|electromotor|bater|senzor|\breleu\b|semnaliz",
        "climatizare": r"clima|aer conditionat|freon|compresor ac",
        "geometrie": r"geometr|reglaj directie|unghi.*roti|roti strambe",
        "tinichigerie": r"tinich|reparat.*caroser|caroser.*(?:repar|lovit|indrept)|indrept|sud(?:at|ura)?|prag|lonjeron|aripa|portier|bara.*(?:fata|spate)|capota|plafon|de aranjat|aranajt|reglat.*usa|usa.*(?:nu.*inchide|de reglat)",
        "vopsitorie": r"vops|revops|lacui",
        "pregatire_vopsitorie": r"pregat.*vops|chitui|slefui|grundui",
        "accesorii_caroserie": r"broasc|incuiet|macara.*geam|geam.*macara|fata.*us|maner.*us|cablu.*(?:deschidere|us)|(?:deschidere|us).*cablu|(?:nu\s+se\s+deschid|deschidere).*us|us.*(?:nu\s+se\s+deschid|deschidere)|\bfar(?:uri)?\b|\bstop(?:uri)?\b",
        "radiatoare": r"radiator|condensator",
    }
    found = [key for key, pattern in patterns.items() if re.search(pattern, value)]
    if re.search(r"bara\s+(?:de\s+)?stabiliz", value) and "tinichigerie" in found:
        found.remove("tinichigerie")
    if ("vopsitorie" in found and "tinichigerie" in found
            and not re.search(r"tinich|indrept|repar|inloc|schimb|lovit|sud", value)):
        found.remove("tinichigerie")
    if "vopsitorie" in found and "pregatire_vopsitorie" not in found:
        found.append("pregatire_vopsitorie")
    if "motor" in found:
        without_service_terms = re.sub(
            r"ulei(?: de)? motor|filtr\w*(?: de)? ulei(?: de)? motor", "", value
        )
        if not re.search(
            r"\bmotor\b|chiulas|inject|turb|compres|\begr\b|vas.*expansi|"
            r"pompa.*vacc?um|dpf|pierderi antigel|pierderi motorina",
            without_service_terms,
        ):
            found.remove("motor")
    if "radiatoare" in found and "climatizare" in found:
        found.remove("climatizare")
    return found or ["diagnoza"]


def department_for_operations(text: str) -> str:
    skills = classify_operations(text)
    body = any(skill in BODYSHOP_SKILLS for skill in skills)
    mechanical = any(skill not in BODYSHOP_SKILLS for skill in skills)
    return "tinichigerie_vopsitorie" if body and not mechanical else "mecanica"


def describe_skill_work(skill: str, text: str) -> str:
    value = str(text or "").casefold()
    value = "".join(ch for ch in __import__("unicodedata").normalize("NFKD", value)
                    if not __import__("unicodedata").combining(ch))
    parts = []
    for pattern, label in SKILL_PART_TERMS.get(skill, []):
        if re.search(pattern, value) and label not in parts:
            parts.append(label)
    detail = ", ".join(parts) if parts else SKILL_LABELS.get(skill, skill)
    return f"{SKILL_LABELS.get(skill, skill)}: {detail}"


def _assignment_score(technician: dict, skill: str) -> int:
    level = int((technician.get("skills") or {}).get(skill) or 0)
    notes = str(technician.get("notes") or "").casefold()
    notes = "".join(ch for ch in __import__("unicodedata").normalize("NFKD", notes)
                    if not __import__("unicodedata").combining(ch))
    score = level * 100
    if skill in {"revizie", "frane"} and "nu pe lucrari usoare" in notes:
        score -= 250
    if skill in {"revizie", "frane"} and "doar daca nu are programari" in notes:
        score -= 100
    return score


def _technician_allowed_for_work(technician: dict, skills: list[str], text: str) -> bool:
    """Apply hard profile restrictions that competence scores cannot override."""
    name = str(technician.get("name") or "").strip().casefold()
    normalized = str(text or "").casefold()
    normalized = "".join(
        ch for ch in __import__("unicodedata").normalize("NFKD", normalized)
        if not __import__("unicodedata").combining(ch)
    )
    if name == "mihai":
        if set(skills) != {"diagnoza"}:
            return False
        return bool(re.search(
            r"proba.*drum|diagno.*dific|trepid|vibr|peste\s*\d+.*(?:ora|km)|"
            r"la\s*\d+.*ora|intermit|se opreste|turat",
            normalized,
        ))
    return True


def _validate_technician_assignment(db_path: Path, technician_id: int | None,
                                    operations: str) -> None:
    """Reject manual/drag assignments that contradict a technician's hard profile."""
    if not technician_id:
        return
    technician = next(
        (item for item in list_technicians(db_path, include_inactive=True)
         if int(item.get("id") or 0) == int(technician_id)),
        None,
    )
    if not technician:
        raise ValueError("Mecanicul selectat nu există.")
    skills = classify_operations(operations)
    configured = technician.get("skills") or {}
    missing = [skill for skill in skills if int(configured.get(skill) or 0) <= 0]
    allowed = _technician_allowed_for_work(technician, skills, operations)
    if missing or not allowed:
        labels = ", ".join(SKILL_LABELS.get(skill, skill) for skill in missing or skills)
        raise ValueError(
            f"{technician.get('name') or 'Mecanicul'} nu poate primi această lucrare "
            f"conform fișei sale ({labels})."
        )


def _split_operation_groups(text: str) -> list[dict]:
    """Split one DSM description into mechanical/bodyshop work blocks."""
    fragments = [item.strip(" -") for item in re.split(r"(?:\r?\n|;|//|,)", str(text or ""))]
    groups: list[dict] = []
    metadata = re.compile(r"^(?:piese?\s+)?comandat(?:e)?$|^oferta\s+deschisa$", re.I)
    for fragment in fragments:
        if not fragment:
            continue
        if metadata.match(fragment):
            if groups:
                groups[-1]["operations"].append(fragment)
            continue
        skills = classify_operations(fragment)
        body = any(skill in BODYSHOP_SKILLS for skill in skills)
        department = "tinichigerie_vopsitorie" if body else "mecanica"
        if groups and groups[-1]["department"] == department:
            groups[-1]["operations"].append(fragment)
            for skill in skills:
                if skill not in groups[-1]["skills"]:
                    groups[-1]["skills"].append(skill)
        else:
            groups.append({
                "department": department,
                "operations": [fragment],
                "skills": list(skills),
            })
    for group in groups:
        group["operations"] = ", ".join(group["operations"])
    return groups or [{
        "department": department_for_operations(text),
        "operations": str(text or "").strip() or "Programare DSM",
        "skills": classify_operations(text),
    }]


def _parse_local(value: str) -> datetime:
    return datetime.fromisoformat(str(value).replace("Z", "+00:00")).replace(tzinfo=None)


def _validate_shop_hours(start: datetime, end: datetime) -> None:
    if start.date() != end.date():
        raise ValueError("Programarea trebuie să înceapă și să se termine în aceeași zi.")
    opening = start.replace(hour=8, minute=30, second=0, microsecond=0)
    closing = start.replace(hour=17, minute=0, second=0, microsecond=0)
    if start < opening or end > closing:
        raise ValueError("Programarea trebuie să fie în intervalul 08:30–17:00.")


def list_appointments(db_path: Path, start: str, end: str) -> list[dict]:
    start_dt, end_dt = _parse_local(start), _parse_local(end)
    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute("""
            SELECT a.*,t.name technician_name,t.color technician_color
            FROM scheduler_appointments a
            LEFT JOIN scheduler_technicians t ON t.id=a.technician_id
            WHERE a.end_at>? AND a.start_at<? AND a.status!='anulat'
            ORDER BY a.start_at,a.technician_id
        """, (start_dt.isoformat(timespec="minutes"), end_dt.isoformat(timespec="minutes"))).fetchall()
        return [dict(row) for row in rows]


def get_appointment(db_path: Path, appointment_id: int) -> dict | None:
    conn = sqlite3.connect(db_path)
    try:
        conn.row_factory = sqlite3.Row
        row = conn.execute("""
            SELECT a.*,t.name technician_name,t.color technician_color
            FROM scheduler_appointments a
            LEFT JOIN scheduler_technicians t ON t.id=a.technician_id
            WHERE a.id=?
        """, (appointment_id,)).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def list_yard_capacity_exercise(db_path: Path) -> list[dict]:
    """Return every temporary yard-capacity block, across all exercise dates."""
    conn = sqlite3.connect(db_path)
    try:
        conn.row_factory = sqlite3.Row
        rows = conn.execute("""
            SELECT a.*,t.name technician_name,t.color technician_color
            FROM scheduler_appointments a
            LEFT JOIN scheduler_technicians t ON t.id=a.technician_id
            WHERE a.source='yard_capacity_exercise' AND a.status!='anulat'
            ORDER BY a.start_at,COALESCE(t.name,'ZZZ Nerepartizat'),a.registration
        """).fetchall()
        return [dict(row) for row in rows]
    finally:
        conn.close()


def clear_yard_capacity_exercise(db_path: Path) -> dict:
    """Delete only temporary capacity blocks and restore the local yard queue."""
    conn = sqlite3.connect(db_path)
    try:
        conn.execute("BEGIN IMMEDIATE")
        deleted = conn.execute(
            "DELETE FROM scheduler_appointments WHERE source='yard_capacity_exercise'"
        ).rowcount
        restored = conn.execute("""
            UPDATE dashboard_yard_orders
            SET status='in_curte',updated_at=CURRENT_TIMESTAMP
            WHERE status='programat_exercitiu'
        """).rowcount
        remaining_real = conn.execute("""
            SELECT COUNT(*) FROM scheduler_appointments
            WHERE source!='yard_capacity_exercise' AND status!='anulat'
        """).fetchone()[0]
        conn.commit()
        return {
            "ok": True,
            "deleted_exercise_blocks": int(deleted or 0),
            "restored_yard_orders": int(restored or 0),
            "remaining_real_appointments": int(remaining_real or 0),
        }
    finally:
        conn.close()


def month_capacity(db_path: Path, year: int, month: int, department: str = "mecanica") -> dict:
    """Return workshop utilization for every day in one calendar month."""
    if not 2020 <= int(year) <= 2100 or not 1 <= int(month) <= 12:
        raise ValueError("Luna sau anul calendarului este invalid.")
    first = datetime(int(year), int(month), 1)
    next_month = (datetime(int(year) + 1, 1, 1) if month == 12
                  else datetime(int(year), int(month) + 1, 1))
    if department not in DEPARTMENT_LABELS:
        department = "mecanica"
    technicians = [item for item in list_technicians(db_path)
                   if str(item.get("department") or "mecanica") == department]
    daily_capacity = 0.0
    for technician in technicians:
        work_start = datetime.strptime(str(technician.get("work_start") or "08:30"), "%H:%M")
        work_end = datetime.strptime(str(technician.get("work_end") or "17:00"), "%H:%M")
        daily_capacity += max(0.0, (work_end - work_start).total_seconds() / 3600)
    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        appointments = [dict(row) for row in conn.execute("""
            SELECT start_at,end_at FROM scheduler_appointments
            WHERE status!='anulat' AND start_at>=? AND start_at<? AND department=?
        """, (first.isoformat(timespec="minutes"), next_month.isoformat(timespec="minutes"), department)).fetchall()]
    booked_by_day: dict[str, float] = {}
    for appointment in appointments:
        start = _parse_local(appointment["start_at"])
        end = _parse_local(appointment["end_at"])
        key = start.date().isoformat()
        booked_by_day[key] = booked_by_day.get(key, 0.0) + max(
            0.0, (end - start).total_seconds() / 3600
        )
    days = []
    day_count = calendar_module.monthrange(int(year), int(month))[1]
    for day_number in range(1, day_count + 1):
        day = datetime(int(year), int(month), day_number)
        key = day.date().isoformat()
        booked = round(booked_by_day.get(key, 0.0), 2)
        available = round(daily_capacity, 2) if day.weekday() < 5 else 0.0
        percent = round((booked / available) * 100, 1) if available > 0 else (101.0 if booked > 0 else 0.0)
        level = "closed" if day.weekday() >= 5 and booked <= 0 else (
            "red" if percent > 100 else "yellow" if percent > 70 else "green"
        )
        days.append({"date": key, "day": day_number, "booked_hours": booked,
                     "capacity_hours": available, "percent": percent, "level": level})
    return {"year": int(year), "month": int(month), "technician_count": len(technicians),
            "daily_capacity_hours": round(daily_capacity, 2), "days": days}


def list_pending_notifications(db_path: Path) -> list[dict]:
    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        return [dict(row) for row in conn.execute("""
            SELECT * FROM scheduler_notifications
            WHERE status IN ('pending_review','failed') ORDER BY id DESC LIMIT 100
        """).fetchall()]


def sync_dsm_open_orders(db_path: Path, orders: list[dict], authoritative: bool = False) -> dict:
    """Mirror DSM open orders into the dashboard yard queue, never the scheduler."""
    result = {"created": 0, "updated": 0, "closed": 0, "skipped_scheduled": 0, "invalid": 0}
    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        scheduled_plates = {
            re.sub(r"[^A-Z0-9]", "", str(row[0] or "").upper())
            for row in conn.execute("""
                SELECT registration FROM scheduler_appointments
                WHERE status NOT IN ('anulat','finalizat')
                  AND (substr(end_at,1,10)>=date('now','localtime') OR status IN ('in_lucru','pornit','neterminat'))
                  AND COALESCE(registration,'')!=''
            """).fetchall()
        }
        for raw in orders:
            job_number = str(raw.get("job_number") or raw.get("transaction_id") or "").strip()
            if not job_number:
                result["invalid"] += 1
                continue
            if not re.match(r"^(?:APP|PHDEV|ALB|#)", job_number, re.I):
                job_number = f"APP-{job_number}"
            status = str(raw.get("status") or "open").strip().casefold()
            source_key = f"dsm:{job_number.upper()}"
            existing = conn.execute(
                "SELECT * FROM dashboard_yard_orders WHERE source_key=?", (source_key,)
            ).fetchone()
            if status in {"closed", "finalizat", "inchis", "închis", "facturat", "cancelled", "anulat"}:
                if existing:
                    conn.execute("UPDATE dashboard_yard_orders SET status='inchis_dsm',updated_at=CURRENT_TIMESTAMP WHERE id=?", (existing["id"],))
                    result["closed"] += 1
                continue
            plate_key = re.sub(r"[^A-Z0-9]", "", str(raw.get("registration") or "").upper())
            if bool(raw.get("has_appointment")) or (plate_key and plate_key in scheduled_plates):
                if existing:
                    conn.execute(
                        "UPDATE dashboard_yard_orders SET status='programat',updated_at=CURRENT_TIMESTAMP WHERE id=?",
                        (existing["id"],),
                    )
                result["skipped_scheduled"] += 1
                continue
            opened_at = str(raw.get("opened_at") or raw.get("date") or datetime.now().isoformat(timespec="minutes"))
            values = (
                str(raw.get("customer_name") or "").strip(), str(raw.get("phone") or "").strip(),
                str(raw.get("registration") or "").strip().upper(), str(raw.get("vehicle") or "").strip(),
                str(raw.get("operations") or "Constatare / comandă DSM deschisă").strip(),
                "Comandă DSM deschisă fără programare; necesită ofertare în dashboard.",
            )
            if existing:
                conn.execute("""
                    UPDATE dashboard_yard_orders
                    SET opened_at=?,customer_name=?,phone=?,registration=?,vin=?,vehicle=?,operations=?,notes=?,
                        status=CASE
                            WHEN status='sters_local' THEN status
                            WHEN service_request_id IS NULL THEN 'in_curte'
                            WHEN status IN ('programat','programat_exercitiu','inchis_dsm') THEN 'in_ofertare'
                            ELSE status
                        END,
                        updated_at=CURRENT_TIMESTAMP
                    WHERE id=?
                """, (opened_at, values[0], values[1], values[2], str(raw.get("vin") or "").strip().upper(),
                      values[3], values[4], values[5], existing["id"]))
                result["updated"] += 1
            else:
                conn.execute("""
                    INSERT INTO dashboard_yard_orders
                    (source_key,job_number,opened_at,status,customer_name,phone,registration,vin,vehicle,operations,notes,source)
                    VALUES (?,?,?,'in_curte',?,?,?,?,?,?,?,'dsm_readonly')
                """, (
                    source_key, job_number, opened_at, values[0], values[1], values[2],
                    str(raw.get("vin") or "").strip().upper(), values[3], values[4], values[5],
                ))
                result["created"] += 1
        if authoritative:
            active_keys={f"dsm:{str(r.get('job_number') or r.get('transaction_id') or '').strip().upper()}" for r in orders}
            existing=conn.execute("SELECT id,source_key FROM dashboard_yard_orders WHERE source='dsm_readonly' AND status IN ('in_curte','in_ofertare')").fetchall()
            for item in existing:
                if item['source_key'] not in active_keys:
                    conn.execute("UPDATE dashboard_yard_orders SET status='inchis_dsm',updated_at=CURRENT_TIMESTAMP WHERE id=?",(item['id'],))
                    result['closed']+=1
        conn.commit()
    return result


def list_yard_orders(db_path: Path) -> list[dict]:
    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        return [dict(row) for row in conn.execute("""
            SELECT * FROM dashboard_yard_orders
            WHERE status IN ('in_curte','in_ofertare')
            ORDER BY CASE status WHEN 'in_curte' THEN 0 ELSE 1 END,
                     COALESCE(opened_at,created_at),id
        """).fetchall()]


def link_yard_order_to_request(db_path: Path, yard_order_id: int, request_id: int) -> dict:
    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute("SELECT * FROM dashboard_yard_orders WHERE id=?", (yard_order_id,)).fetchone()
        if not row:
            raise ValueError("Mașina din curte nu mai există în listă.")
        if row["service_request_id"] and int(row["service_request_id"]) != int(request_id):
            raise ValueError("Mașina are deja o cerere de ofertare.")
        conn.execute("""
            UPDATE dashboard_yard_orders
            SET service_request_id=?,status='in_ofertare',updated_at=CURRENT_TIMESTAMP WHERE id=?
        """, (request_id, yard_order_id))
        conn.commit()
        return dict(conn.execute("SELECT * FROM dashboard_yard_orders WHERE id=?", (yard_order_id,)).fetchone())


def dismiss_yard_order(db_path: Path, yard_order_id: int) -> bool:
    """Hide an open DSM order locally without changing anything in DSM."""
    with sqlite3.connect(db_path) as conn:
        cursor = conn.execute("""
            UPDATE dashboard_yard_orders
            SET status='sters_local',updated_at=CURRENT_TIMESTAMP
            WHERE id=? AND status IN ('in_curte','in_ofertare')
        """, (yard_order_id,))
        conn.commit()
        return cursor.rowcount == 1


def accepted_requests(db_path: Path) -> list[dict]:
    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute("""
            SELECT r.* FROM service_requests r
            WHERE LOWER(COALESCE(r.status,'')) NOT IN ('anulat','finalizat','respins','sters')
              AND NOT EXISTS (
                SELECT 1 FROM scheduler_appointments a
                WHERE a.request_id=r.id AND a.status!='anulat'
              )
            ORDER BY CASE WHEN LOWER(COALESCE(r.priority,''))='urgent' THEN 0 ELSE 1 END,r.id DESC
            LIMIT 40
        """).fetchall()
        return [dict(row) for row in rows]


def cancel_waiting_request(db_path: Path, request_id: int) -> dict:
    """Remove an unscheduled dashboard request from the scheduling queue.

    The request and its history are retained.  A request with an active
    appointment must be cancelled from the appointment dialog instead, so a
    waiting-card action can never silently remove calendar blocks.
    """
    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        request = conn.execute(
            "SELECT id,status FROM service_requests WHERE id=?", (request_id,)
        ).fetchone()
        if not request:
            raise ValueError("Cererea nu a fost găsită.")
        active = conn.execute(
            """SELECT 1 FROM scheduler_appointments
               WHERE request_id=? AND status!='anulat' LIMIT 1""",
            (request_id,),
        ).fetchone()
        if active:
            raise ValueError(
                "Cererea are deja o programare activă. Scoate programarea din calendar."
            )
        conn.execute(
            "UPDATE service_requests SET status='anulat' WHERE id=?", (request_id,)
        )
        conn.commit()
    return {
        "request_id": int(request_id),
        "status": "anulat",
        "history_preserved": True,
    }


def request_by_id(db_path: Path, request_id: int) -> dict | None:
    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute("SELECT * FROM service_requests WHERE id=?", (request_id,)).fetchone()
        return dict(row) if row else None


def _conflicts(db_path: Path, technician_id: int, start: datetime, end: datetime,
               exclude_id: int | None = None) -> bool:
    query = """
        SELECT 1 FROM scheduler_appointments
        WHERE technician_id=? AND status!='anulat' AND end_at>? AND start_at<?
    """
    params: list = [technician_id, start.isoformat(timespec="minutes"), end.isoformat(timespec="minutes")]
    if exclude_id:
        query += " AND id!=?"
        params.append(exclude_id)
    with sqlite3.connect(db_path) as conn:
        return conn.execute(query + " LIMIT 1", params).fetchone() is not None


def suggest_appointment(db_path: Path, request_id: int, duration_hours: float = 0.0,
                        earliest: str | None = None) -> dict:
    request = request_by_id(db_path, request_id)
    if not request:
        raise ValueError("Cererea nu a fost găsită.")
    duration = max(0.5, math.ceil(float(duration_hours or request.get("booking_time") or 1.0) * 2) / 2)
    skills_needed = classify_operations(request.get("symptom") or "")
    department = department_for_operations(request.get("symptom") or "")
    technicians = [item for item in list_technicians(db_path)
                   if str(item.get("department") or "mecanica") == department]
    ranked = []
    for technician in technicians:
        skills = technician.get("skills") or {}
        if not skills:
            continue
        if not _technician_allowed_for_work(
            technician, skills_needed, request.get("symptom") or ""
        ):
            continue
        levels = [int(skills.get(skill) or 0) for skill in skills_needed]
        if any(level <= 0 for level in levels):
            continue
        ranked.append((sum(levels), min(levels), technician))
    ranked.sort(key=lambda item: (-item[0], -item[1], item[2]["name"]))
    if not ranked:
        return {
            "status": "needs_technician_profile",
            "request_id": request_id,
            "duration_hours": duration,
            "required_skills": skills_needed,
            "message": "Nu există încă un mecanic configurat pentru toate competențele acestei lucrări.",
        }

    cursor = _parse_local(earliest) if earliest else datetime.now().replace(second=0, microsecond=0)
    cursor += timedelta(minutes=(30 - cursor.minute % 30) % 30)
    for day_offset in range(22):
        day = (cursor + timedelta(days=day_offset)).date()
        if day.weekday() >= 5:
            continue
        for _, _, technician in ranked:
            work_start = datetime.combine(day, datetime.strptime(technician["work_start"], "%H:%M").time())
            work_end = datetime.combine(day, datetime.strptime(technician["work_end"], "%H:%M").time())
            slot = max(cursor, work_start) if day == cursor.date() else work_start
            slot += timedelta(minutes=(30 - slot.minute % 30) % 30)
            while slot + timedelta(hours=duration) <= work_end:
                end = slot + timedelta(hours=duration)
                if not _conflicts(db_path, technician["id"], slot, end):
                    return {
                        "status": "suggested",
                        "request_id": request_id,
                        "technician_id": technician["id"],
                        "technician_name": technician["name"],
                        "start_at": slot.isoformat(timespec="minutes"),
                        "end_at": end.isoformat(timespec="minutes"),
                        "duration_hours": duration,
                        "required_skills": skills_needed,
                        "department": department,
                        "score": sum(int((technician.get("skills") or {}).get(skill) or 0) for skill in skills_needed),
                        "message": "Propunere locală; programarea se salvează numai după confirmarea consilierului.",
                    }
                slot += timedelta(minutes=30)
    return {"status": "no_slot", "request_id": request_id, "duration_hours": duration,
            "required_skills": skills_needed, "message": "Nu există interval liber în următoarele 22 de zile."}


def _first_slot_for_technician(db_path: Path, technician: dict | None, cursor: datetime,
                               duration_hours: float,
                               extra_busy: list[tuple[datetime, datetime]] | None = None,
                               request_id: int | None = None,
                               max_jobs_per_day: int = 2) -> tuple[datetime, datetime]:
    duration = timedelta(hours=duration_hours)
    for day_offset in range(32):
        day = (cursor + timedelta(days=day_offset)).date()
        if day.weekday() >= 5:
            continue
        start_text = (technician or {}).get("work_start") or "08:30"
        end_text = (technician or {}).get("work_end") or "17:00"
        work_start = datetime.combine(day, datetime.strptime(start_text, "%H:%M").time())
        work_end = datetime.combine(day, datetime.strptime(end_text, "%H:%M").time())
        if technician and max_jobs_per_day > 0:
            with sqlite3.connect(db_path) as conn:
                params: list = [technician["id"], day.isoformat()]
                exclude = ""
                if request_id:
                    exclude = " AND (request_id IS NULL OR request_id!=?)"
                    params.append(request_id)
                job_count = int(conn.execute(f"""
                    SELECT COUNT(DISTINCT COALESCE(CAST(request_id AS TEXT),'manual-'||id))
                    FROM scheduler_appointments
                    WHERE technician_id=? AND date(start_at)=? AND status!='anulat'{exclude}
                """, params).fetchone()[0] or 0)
            if job_count >= max_jobs_per_day:
                continue
        slot = max(cursor, work_start) if day == cursor.date() else work_start
        slot += timedelta(minutes=(30 - slot.minute % 30) % 30)
        while slot + duration <= work_end:
            end = slot + duration
            virtual_conflict = any(busy_end > slot and busy_start < end
                                   for busy_start, busy_end in (extra_busy or []))
            if not virtual_conflict and (not technician or not _conflicts(db_path, technician["id"], slot, end)):
                return slot, end
            slot += timedelta(minutes=30)
    raise ValueError("Nu există interval disponibil în următoarele 32 de zile.")


def _required_skill_level(skill: str, text: str) -> int:
    base = {"revizie": 2, "frane": 3, "suspensie_directie": 3, "ambreiaj": 4,
            "distributie": 4, "motor": 4, "diagnoza": 3, "electrica": 4,
            "climatizare": 3, "geometrie": 3, "tinichigerie": 4,
            "vopsitorie": 4, "pregatire_vopsitorie": 3,
            "accesorii_caroserie": 3, "radiatoare": 3}.get(skill, 3)
    normalized = str(text or "").casefold()
    normalized = "".join(ch for ch in __import__("unicodedata").normalize("NFKD", normalized)
                         if not __import__("unicodedata").combining(ch))
    if skill == "suspensie_directie" and re.search(r"toate.*brat|brat.*toate|punte|caseta", normalized):
        return max(base, 4)
    if skill == "frane" and re.search(r"etrier|conduct|pompa", normalized):
        return max(base, 4)
    if skill == "distributie" and re.search(
        r"\blant(?:uri)?\b|axe(?:lor)?\s+cu\s+came|garantie", normalized
    ):
        return 5
    return base


PRIORITY_EXPERT_NAMES = {"VALI", "GEORGE"}


def _is_priority_expert(technician: dict) -> bool:
    """Identify the scarce all-round experts whose time is reserved for hard work."""
    name = str(technician.get("name") or "").strip().upper()
    return name in PRIORITY_EXPERT_NAMES


def _specialty_priority(technician: dict, skills: list[str], text: str = "") -> int:
    """Lower is better; route distribution work by make/model specialists."""
    name = str(technician.get("name") or "").strip().upper()
    if "distributie" in skills:
        normalized = str(text or "").casefold()
        normalized = "".join(
            ch for ch in __import__("unicodedata").normalize("NFKD", normalized)
            if not __import__("unicodedata").combining(ch)
        )
        if re.search(r"\bbmw\b", normalized):
            return 0 if name in {"ADELIN", "CATALIN"} else (1 if name == "VALI" else 2)
        if re.search(r"\biveco\b", normalized):
            return 0 if name == "GEORGE" else (1 if name == "VALI" else 2)
        if re.search(r"\bboxer\b|peugeot\s+boxer", normalized):
            return 0 if name in {"GEORGE", "CATALIN"} else (1 if name == "VALI" else 2)
        if name == "VALI":
            return 0
        if name == "GEORGE":
            return 1
        return 2
    return 0 if _is_priority_expert(technician) else 1


def _skill_level_satisfies(technician: dict, skill: str, required: int,
                           text: str) -> bool:
    level = int((technician.get("skills") or {}).get(skill) or 0)
    if level >= required:
        return True
    name = str(technician.get("name") or "").strip().upper()
    normalized = str(text or "").casefold()
    normalized = "".join(
        ch for ch in __import__("unicodedata").normalize("NFKD", normalized)
        if not __import__("unicodedata").combining(ch)
    )
    # Explicit workshop profile: Adelin performs distribution work only on BMW.
    return skill == "distributie" and name == "ADELIN" and level > 0 and bool(
        re.search(r"\bbmw\b", normalized)
    )


def _scheduled_hours(db_path: Path, technician_id: int, week_of: datetime) -> float:
    monday = (week_of - timedelta(days=week_of.weekday())).replace(hour=0, minute=0, second=0, microsecond=0)
    next_week = monday + timedelta(days=7)
    with sqlite3.connect(db_path) as conn:
        row = conn.execute("""
            SELECT COALESCE(SUM(estimated_hours),0) FROM scheduler_appointments
            WHERE technician_id=? AND status!='anulat' AND start_at>=? AND start_at<?
        """, (technician_id, monday.isoformat(timespec="minutes"),
              next_week.isoformat(timespec="minutes"))).fetchone()
    return float(row[0] or 0)


def plan_request_assignments(db_path: Path, request_id: int, earliest: str | None = None,
                             duration_hours: float = 0.0) -> dict:
    request = request_by_id(db_path, request_id)
    if not request:
        raise ValueError("Cererea nu a fost găsită.")
    work_text = " ".join(str(request.get(key) or "") for key in ("vehicle", "symptom"))
    skills = classify_operations(work_text)
    department = department_for_operations(request.get("symptom") or "")
    has_body = any(skill in BODYSHOP_SKILLS for skill in skills)
    has_mechanical = any(skill not in BODYSHOP_SKILLS for skill in skills)
    technicians = list_technicians(db_path)
    if not (has_body and has_mechanical):
        technicians = [item for item in technicians
                       if str(item.get("department") or "mecanica") == department]
    requested_total = float(duration_hours or request.get("booking_time") or 0)
    default_total = sum(SKILL_DEFAULT_HOURS.get(skill, 1.0) for skill in skills) or 1.0
    cursor = _parse_local(earliest) if earliest else datetime.now().replace(second=0, microsecond=0)
    cursor += timedelta(minutes=(30 - cursor.minute % 30) % 30)
    skill_hours = {}
    required_levels = {}
    for skill in skills:
        base_hours = SKILL_DEFAULT_HOURS.get(skill, 1.0)
        duration = (requested_total * base_hours / default_total) if requested_total > 0 else base_hours
        skill_hours[skill] = max(0.5, math.ceil(duration * 2) / 2)
        required_levels[skill] = _required_skill_level(skill, work_text)

    def covered_by(technician: dict) -> list[str]:
        covered = [skill for skill in skills
                   if _skill_level_satisfies(
                       technician, skill, required_levels[skill], work_text
                   )]
        return covered if _technician_allowed_for_work(
            technician, covered, work_text
        ) else []

    primary = None
    if technicians:
        ranked_primary = []
        for technician in technicians:
            covered = covered_by(technician)
            if not covered:
                continue
            load = _scheduled_hours(db_path, technician["id"], cursor)
            surplus = sum(int((technician.get("skills") or {}).get(skill) or 0) - required_levels[skill]
                          for skill in covered)
            complex_request = any(required_levels[skill] >= 4 for skill in covered)
            expert_penalty = int(
                not complex_request and load > 0 and _is_priority_expert(technician)
            )
            complex_priority = (_specialty_priority(technician, covered, work_text)
                                if complex_request else 0)
            covered_hours = sum(skill_hours[skill] for skill in covered)
            try:
                candidate_start, _ = _first_slot_for_technician(
                    db_path, technician, cursor, covered_hours, request_id=request_id
                )
            except ValueError:
                candidate_start = cursor + timedelta(days=365)
            ranked_primary.append(((complex_priority, expert_penalty, -len(covered), load, surplus,
                                    technician["name"]), technician, covered, candidate_start))
        if ranked_primary:
            choice = min(ranked_primary, key=lambda item: item[0])
            if "distributie" in skills:
                best_specialty = min(item[0][0] for item in ranked_primary)
                preferred = [item for item in ranked_primary if item[0][0] == best_specialty]
                preferred_start = min(item[3] for item in preferred)
                if preferred_start >= cursor + timedelta(days=7):
                    earlier = [item for item in ranked_primary if item[3] < preferred_start]
                    if earlier:
                        choice = min(earlier, key=lambda item: (item[3], item[0]))
            _, primary, primary_skills, _ = choice
        else:
            primary_skills = []
    else:
        primary_skills = []

    groups: list[dict] = []
    if primary_skills:
        groups.append({"technician": primary, "skills": primary_skills, "role": "principal"})
    uncovered = [skill for skill in skills if skill not in primary_skills]
    specialist_groups: dict[int | None, dict] = {}
    for skill in uncovered:
        candidates = [t for t in technicians
                      if _skill_level_satisfies(t, skill, required_levels[skill], work_text)
                      and _technician_allowed_for_work(t, [skill], work_text)]
        if not candidates:
            candidates = [t for t in technicians
                          if int((t.get("skills") or {}).get(skill) or 0) > 0
                          and _technician_allowed_for_work(t, [skill], work_text)]
        if candidates:
            specialist = min(candidates, key=lambda t: (
                _scheduled_hours(db_path, t["id"], cursor),
                abs(int((t.get("skills") or {}).get(skill) or 0) - required_levels[skill]),
                -_assignment_score(t, skill), t["name"],
            ))
        else:
            specialist = None
        key = (specialist or {}).get("id")
        group = specialist_groups.setdefault(key, {"technician": specialist, "skills": [], "role": "suplimentar"})
        group["skills"].append(skill)
    groups.extend(specialist_groups.values())

    assignments = []
    next_group_cursor = cursor
    for group in groups:
        technician = group["technician"]
        total_hours = sum(skill_hours[skill] for skill in group["skills"])
        remaining = total_hours
        group_cursor = next_group_cursor
        chunk_index = 0
        while remaining > 0:
            work_start = (technician or {}).get("work_start") or "08:30"
            work_end = (technician or {}).get("work_end") or "17:00"
            daily_capacity = (
                datetime.strptime(work_end, "%H:%M") - datetime.strptime(work_start, "%H:%M")
            ).total_seconds() / 3600
            chunk = min(remaining, daily_capacity)
            start, end = _first_slot_for_technician(
                db_path, technician, group_cursor, chunk, request_id=request_id
            )
            chunk_index += 1
            operations = " + ".join(
                describe_skill_work(skill, request.get("symptom") or "") for skill in group["skills"]
            )
            if chunk_index > 1:
                operations += " — continuare"
            assignments.append({
                "technician_id": (technician or {}).get("id"),
                "technician_name": (technician or {}).get("name") or "Nerepartizat",
                "skills": group["skills"],
                "role": group["role"],
                "operations": operations,
                "duration_hours": chunk,
                "start_at": start.isoformat(timespec="minutes"),
                "end_at": end.isoformat(timespec="minutes"),
            })
            remaining = round(remaining - chunk, 2)
            group_cursor = end
        next_group_cursor = max(next_group_cursor, group_cursor)
    distinct_technicians = {(item.get("technician_id") or 0) for item in assignments}
    return {"request": request, "assignments": assignments,
            "split": len(distinct_technicians) > 1,
            "primary_technician_id": (primary or {}).get("id"),
            "department": department}


def auto_schedule_request(db_path: Path, request_id: int, earliest: str | None = None,
                          duration_hours: float = 0.0) -> dict:
    plan = plan_request_assignments(db_path, request_id, earliest, duration_hours)
    request = plan["request"]
    created = []
    for index, assignment in enumerate(plan["assignments"], 1):
        created.append(create_appointment(db_path, {
            "request_id": request_id,
            "technician_id": assignment["technician_id"],
            "start_at": assignment["start_at"],
            "end_at": assignment["end_at"],
            "registration": request.get("registration") or "",
            "vehicle": request.get("vehicle") or "",
            "job_number": f"#{request_id}",
            "operations": assignment["operations"],
            "notes": (f"Rol: {assignment.get('role') or 'principal'}. "
                      f"Partea {index}/{len(plan['assignments'])} din aceeași cerere. "
                      f"Cererea completă: {request.get('symptom') or ''}"),
        }))
    return {"status": "scheduled", "request_id": request_id, "split": plan["split"],
            "appointments": created}


def rebalance_schedule(db_path: Path, week_start: str) -> dict:
    monday = _parse_local(f"{week_start}T00:00")
    monday -= timedelta(days=monday.weekday())
    next_week = monday + timedelta(days=7)
    with sqlite3.connect(db_path) as conn:
        request_ids = [int(row[0]) for row in conn.execute("""
            SELECT DISTINCT request_id FROM scheduler_appointments
            WHERE request_id IS NOT NULL AND status!='anulat' AND start_at>=? AND start_at<?
        """, (monday.isoformat(timespec="minutes"), next_week.isoformat(timespec="minutes"))).fetchall()]
    requests = [request_by_id(db_path, request_id) for request_id in request_ids]
    requests = [request for request in requests if request]
    requests.sort(key=lambda request: (
        -len(classify_operations(request.get("symptom") or "")),
        -sum(SKILL_DEFAULT_HOURS.get(skill, 1.0)
             for skill in classify_operations(request.get("symptom") or "")),
        -int(request["id"]),
    ))
    with sqlite3.connect(db_path) as conn:
        if request_ids:
            placeholders = ",".join("?" for _ in request_ids)
            conn.execute(f"DELETE FROM scheduler_appointments WHERE request_id IN ({placeholders})", request_ids)
            conn.commit()
    results = []
    earliest = monday.replace(hour=8, minute=30).isoformat(timespec="minutes")
    for request in requests:
        results.append(auto_schedule_request(db_path, int(request["id"]), earliest))
    appointments = [item for result in results for item in result["appointments"]]
    return {"status": "rebalanced", "request_count": len(results),
            "appointment_count": len(appointments), "appointments": appointments}


def distribute_dsm_appointments_by_capacity(
    db_path: Path, week_start: str, department: str = "mecanica"
) -> dict:
    """Split mixed DSM work and redistribute local blocks by real capability."""
    if department != "all" and department not in DEPARTMENT_LABELS:
        raise ValueError("Secția selectată este invalidă.")
    monday = _parse_local(f"{week_start}T00:00")
    monday -= timedelta(days=monday.weekday())
    end = monday + timedelta(days=7)
    technicians = list_technicians(db_path)
    if department != "all":
        technicians = [item for item in technicians
                       if str(item.get("department") or "mecanica") == department]
    if not technicians:
        raise ValueError("Nu există angajați activi configurați în această secție.")

    conn = sqlite3.connect(db_path)
    try:
        conn.row_factory = sqlite3.Row
        conn.execute("""
            DELETE FROM scheduler_appointments
            WHERE source='dsm_capacity_split_local' AND start_at>=? AND start_at<?
        """, (monday.isoformat(timespec="minutes"), end.isoformat(timespec="minutes")))
        target_sql = """
            SELECT * FROM scheduler_appointments
            WHERE status!='anulat'
              AND source IN ('dsm_schedule_readonly','dsm_readonly_import')
              AND COALESCE(dsm_original_start_at,start_at)>=?
              AND COALESCE(dsm_original_start_at,start_at)<?
        """
        target_params: list = [monday.isoformat(timespec="minutes"), end.isoformat(timespec="minutes")]
        if department != "all":
            target_sql += " AND department=?"
            target_params.append(department)
        target_sql += " ORDER BY COALESCE(dsm_original_start_at,start_at),id"
        targets = [dict(row) for row in conn.execute(target_sql, target_params).fetchall()]

        def target_order(item: dict) -> tuple:
            original_start = _parse_local(item.get("dsm_original_start_at") or item["start_at"])
            operations = str(item.get("dsm_original_operations") or item.get("operations") or "")
            context = " ".join((str(item.get("vehicle") or ""), operations))
            skills = classify_operations(context)
            complexity = max(
                (_required_skill_level(skill, context) for skill in skills),
                default=0,
            )
            # Reserve scarce expert capacity first on each day.  Routine work is
            # fitted around it, even when the routine DSM block starts earlier.
            return original_start.date(), -complexity, original_start.time(), int(item["id"])

        targets.sort(key=target_order)
        target_ids = {int(item["id"]) for item in targets}
        fixed = [dict(row) for row in conn.execute("""
            SELECT * FROM scheduler_appointments
            WHERE status!='anulat' AND start_at<? AND end_at>?
        """, (end.isoformat(timespec="minutes"), monday.isoformat(timespec="minutes"))).fetchall()
                 if int(row["id"]) not in target_ids]

        busy: dict[int, list[tuple[datetime, datetime]]] = {
            int(item["id"]): [] for item in technicians
        }
        for fixed_item in fixed:
            tech_id = int(fixed_item.get("technician_id") or 0)
            if tech_id in busy:
                busy[tech_id].append((_parse_local(fixed_item["start_at"]), _parse_local(fixed_item["end_at"])))

        moved = unchanged = unresolved = split_count = blocks_created = 0
        decisions: list[dict] = []
        for item in targets:
            original_start = _parse_local(item.get("dsm_original_start_at") or item["start_at"])
            original_end = _parse_local(item.get("dsm_original_end_at") or item["end_at"])
            operations = str(item.get("dsm_original_operations") or item.get("operations") or "").strip()
            vehicle_context = str(item.get("vehicle") or "").strip()
            groups = _split_operation_groups(operations)
            if department != "all":
                groups = [group for group in groups if group["department"] == department]
            if not groups:
                continue
            if len(groups) > 1:
                split_count += 1

            total_units = max(len(groups), int(round((original_end - original_start).total_seconds() / 1800)))
            weights = [sum(SKILL_DEFAULT_HOURS.get(skill, 1.0) for skill in group["skills"]) or 1.0
                       for group in groups]
            units = [1 for _ in groups]
            for _ in range(total_units - len(groups)):
                group_index = max(range(len(groups)), key=lambda i: weights[i] / (units[i] + 1))
                units[group_index] += 1

            cursor = original_start
            for index, (group, unit_count) in enumerate(zip(groups, units)):
                block_start = cursor
                block_end = original_end if index == len(groups) - 1 else cursor + timedelta(minutes=30 * unit_count)
                cursor = block_end
                group_techs = [tech for tech in technicians
                               if str(tech.get("department") or "mecanica") == group["department"]]
                work_context = " ".join((vehicle_context, group["operations"]))
                required = {skill: _required_skill_level(skill, work_context)
                            for skill in group["skills"]}

                def available(technician: dict) -> bool:
                    opening = datetime.combine(block_start.date(), datetime.strptime(
                        technician.get("work_start") or "08:30", "%H:%M").time())
                    closing = datetime.combine(block_start.date(), datetime.strptime(
                        technician.get("work_end") or "17:00", "%H:%M").time())
                    if block_start < opening or block_end > closing:
                        return False
                    return not any(other_end > block_start and other_start < block_end
                                   for other_start, other_end in busy[int(technician["id"])])

                qualified = [tech for tech in group_techs
                             if available(tech)
                             and _technician_allowed_for_work(tech, group["skills"], work_context)
                             and all(_skill_level_satisfies(tech, skill, level, work_context)
                                     for skill, level in required.items())]
                if not qualified:
                    qualified = [tech for tech in group_techs
                                 if available(tech)
                                 and _technician_allowed_for_work(tech, group["skills"], work_context)
                                 and all(int((tech.get("skills") or {}).get(skill) or 0) > 0
                                         for skill in group["skills"])]

                def load_key(technician: dict) -> tuple:
                    tech_id = int(technician["id"])
                    daily = sum((b - a).total_seconds() / 3600 for a, b in busy[tech_id]
                                if a.date() == block_start.date())
                    weekly = sum((b - a).total_seconds() / 3600 for a, b in busy[tech_id])
                    competence = sum(int((technician.get("skills") or {}).get(skill) or 0)
                                     for skill in group["skills"])
                    complex_work = any(level >= 4 for level in required.values())
                    expert_penalty = int(
                        not complex_work and daily > 0 and _is_priority_expert(technician)
                    )
                    complex_priority = (_specialty_priority(
                        technician, group["skills"], work_context
                    )
                                        if complex_work else 0)
                    return (complex_priority, expert_penalty, daily, weekly, -competence,
                            str(technician.get("name") or ""))

                original_id = int(item.get("technician_id") or 0)
                original = next(
                    (tech for tech in qualified if int(tech.get("id") or 0) == original_id),
                    None,
                )
                expert_work = any(level >= 4 for level in required.values())
                distribution_work = "distributie" in group["skills"]
                best_specialty = min(
                    (_specialty_priority(tech, group["skills"], work_context)
                     for tech in qualified),
                    default=99,
                )
                # For expert work, retain the DSM technician when that technician
                # still satisfies every hard skill requirement and is available.
                # Load balancing must not downgrade a specialist assignment.
                preserve_original_expert = bool(
                    expert_work and original and _is_priority_expert(original)
                    and not (
                        distribution_work
                        and _specialty_priority(original, group["skills"], work_context)
                        > best_specialty
                    )
                )
                selected = original if preserve_original_expert else (
                    min(qualified, key=load_key) if qualified else None
                )
                selected_id = int(selected["id"]) if selected else None
                if selected_id:
                    busy[selected_id].append((block_start, block_end))
                else:
                    unresolved += 1

                duration = round((block_end - block_start).total_seconds() / 3600, 2)
                old_id = int(item.get("technician_id") or 0)
                if index == 0:
                    conn.execute("""
                        UPDATE scheduler_appointments
                        SET technician_id=?,start_at=?,end_at=?,operations=?,estimated_hours=?,
                            department=?,updated_at=CURRENT_TIMESTAMP WHERE id=?
                    """, (selected_id, block_start.isoformat(timespec="minutes"),
                          block_end.isoformat(timespec="minutes"), group["operations"], duration,
                          group["department"], int(item["id"])))
                else:
                    conn.execute("""
                        INSERT INTO scheduler_appointments
                        (request_id,technician_id,start_at,end_at,status,customer_name,phone,registration,
                         vehicle,job_number,continued_from_id,operations,estimated_hours,notes,source,department,
                         dsm_original_operations,dsm_original_start_at,dsm_original_end_at)
                        VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                    """, (item.get("request_id"), selected_id,
                          block_start.isoformat(timespec="minutes"), block_end.isoformat(timespec="minutes"),
                          item.get("status") or "programat", item.get("customer_name"), item.get("phone"),
                          item.get("registration"), item.get("vehicle"), item.get("job_number"), int(item["id"]),
                          group["operations"], duration,
                          "Împărțire locală după competențe; programarea DSM nu a fost modificată.",
                          "dsm_capacity_split_local", group["department"], operations,
                          original_start.isoformat(timespec="minutes"), original_end.isoformat(timespec="minutes")))
                    blocks_created += 1

                state = "unassigned" if not selected else ("moved" if selected_id != old_id else "unchanged")
                if selected:
                    if state == "moved":
                        moved += 1
                    else:
                        unchanged += 1
                decisions.append({
                    "appointment_id": int(item["id"]), "job_number": item.get("job_number"),
                    "registration": item.get("registration"), "status": state,
                    "technician": (selected or {}).get("name"), "skills": group["skills"],
                    "department": group["department"], "operations": group["operations"],
                })
        conn.commit()
        return {
            "ok": unresolved == 0,
            "department": department,
            "week_start": monday.date().isoformat(),
            "appointment_count": len(targets),
            "blocks_created": blocks_created,
            "split_count": split_count,
            "moved": moved,
            "unchanged": unchanged,
            "unresolved": unresolved,
            "decisions": decisions,
            "dsm_unchanged": True,
        }
    finally:
        conn.close()


def create_appointment(db_path: Path, payload: dict) -> dict:
    request_id = int(payload.get("request_id") or 0) or None
    request = request_by_id(db_path, request_id) if request_id else None
    technician_id = int(payload.get("technician_id") or 0) or None
    start, end = _parse_local(payload.get("start_at")), _parse_local(payload.get("end_at"))
    if end <= start:
        raise ValueError("Ora de final trebuie să fie după ora de început.")
    _validate_shop_hours(start, end)
    if technician_id and _conflicts(db_path, technician_id, start, end):
        raise ValueError("Mecanicul are deja o programare în acest interval.")
    operations = str(payload.get("operations") or (request or {}).get("symptom") or "").strip()
    if not operations:
        raise ValueError("Lucrarea este obligatorie.")
    _validate_technician_assignment(db_path, technician_id, operations)
    duration = round((end - start).total_seconds() / 3600, 2)
    department = str(payload.get("department") or "").strip()
    if technician_id:
        with sqlite3.connect(db_path) as lookup:
            tech = lookup.execute("SELECT department FROM scheduler_technicians WHERE id=?", (technician_id,)).fetchone()
        if tech:
            department = str(tech[0] or "mecanica")
    if department not in DEPARTMENT_LABELS:
        department = department_for_operations(operations)
    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        cur = conn.execute("""
            INSERT INTO scheduler_appointments
            (request_id,technician_id,start_at,end_at,status,customer_name,phone,registration,vehicle,
             job_number,operations,estimated_hours,notes,department)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """, (
            request_id, technician_id, start.isoformat(timespec="minutes"), end.isoformat(timespec="minutes"),
            "programat", payload.get("customer_name") or (request or {}).get("customer_name"),
            payload.get("phone") or (request or {}).get("phone"),
            payload.get("registration") or (request or {}).get("registration"),
            payload.get("vehicle") or (request or {}).get("vehicle"),
            str(payload.get("job_number") or (f"#{request_id}" if request_id else "")).strip(),
            operations, duration,
            str(payload.get("notes") or "").strip(),
            department,
        ))
        appointment_id = int(cur.lastrowid)
        if request_id:
            conn.execute("UPDATE service_requests SET status='programat' WHERE id=?", (request_id,))
        conn.commit()
        row = conn.execute("""
            SELECT a.*,t.name technician_name,t.color technician_color
            FROM scheduler_appointments a LEFT JOIN scheduler_technicians t ON t.id=a.technician_id
            WHERE a.id=?
        """, (appointment_id,)).fetchone()
        return dict(row)


def update_appointment(db_path: Path, appointment_id: int, payload: dict) -> dict:
    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        current = conn.execute(
            "SELECT * FROM scheduler_appointments WHERE id=?", (appointment_id,)
        ).fetchone()
    if not current:
        raise ValueError("Programarea nu a fost găsită.")
    current = dict(current)
    technician_id = payload.get("technician_id", current.get("technician_id"))
    technician_id = int(technician_id or 0) or None
    start = _parse_local(payload.get("start_at") or current["start_at"])
    end = _parse_local(payload.get("end_at") or current["end_at"])
    if end <= start:
        raise ValueError("Ora de final trebuie să fie după ora de început.")
    _validate_shop_hours(start, end)
    if technician_id and _conflicts(db_path, technician_id, start, end, exclude_id=appointment_id):
        raise ValueError("Mecanicul are deja o programare în acest interval.")
    duration = round((end - start).total_seconds() / 3600, 2)
    operations = str(payload.get("operations", current.get("operations")) or "").strip()
    _validate_technician_assignment(db_path, technician_id, operations)
    department = str(payload.get("department") or current.get("department") or "").strip()
    if technician_id:
        with sqlite3.connect(db_path) as lookup:
            tech = lookup.execute("SELECT department FROM scheduler_technicians WHERE id=?", (technician_id,)).fetchone()
        if tech:
            department = str(tech[0] or "mecanica")
    if department not in DEPARTMENT_LABELS:
        department = department_for_operations(operations)
    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        conn.execute("""
            UPDATE scheduler_appointments
            SET technician_id=?,start_at=?,end_at=?,estimated_hours=?,
                registration=?,vehicle=?,job_number=?,operations=?,notes=?,department=?,
                source='local_schedule_override',updated_at=CURRENT_TIMESTAMP
            WHERE id=?
        """, (
            technician_id, start.isoformat(timespec="minutes"), end.isoformat(timespec="minutes"), duration,
            payload.get("registration", current.get("registration")),
            payload.get("vehicle", current.get("vehicle")),
            payload.get("job_number", current.get("job_number")),
            operations,
            payload.get("notes", current.get("notes")), department, appointment_id,
        ))
        conn.commit()
        row = conn.execute("""
            SELECT a.*,t.name technician_name,t.color technician_color
            FROM scheduler_appointments a LEFT JOIN scheduler_technicians t ON t.id=a.technician_id
            WHERE a.id=?
        """, (appointment_id,)).fetchone()
        return dict(row)


def claim_appointment(db_path: Path, appointment_id: int, technician_id: int, note: str = '') -> dict:
    """Transfer the same local booking atomically; keep DSM and other jobs intact."""
    conn = sqlite3.connect(db_path)
    try:
        conn.row_factory = sqlite3.Row
        conn.execute('BEGIN IMMEDIATE')
        row = conn.execute('SELECT * FROM scheduler_appointments WHERE id=?', (appointment_id,)).fetchone()
        tech = conn.execute('SELECT * FROM scheduler_technicians WHERE id=? AND active=1', (technician_id,)).fetchone()
        if not row or not tech:
            raise ValueError('Programarea sau mecanicul activ nu a fost găsit.')
        current, tech = dict(row), dict(tech)
        if int(current.get('technician_id') or 0) == technician_id:
            return {'appointment':current, 'changed':False, 'slot_changed':False}
        if current['status'] in {'anulat','finalizat'}:
            raise ValueError('Programarea este anulată sau finalizată.')
        start, end = _parse_local(current['start_at']), _parse_local(current['end_at'])
        old_start = start
        duration = (end-start).total_seconds()/3600
        if duration <= 0:
            raise ValueError('Durata programării nu este validă.')
        if start.date() < datetime.now().date() or _conflicts(db_path, technician_id, start, end, exclude_id=appointment_id):
            cursor = max(start, datetime.now().replace(second=0,microsecond=0))
            start, end = _first_slot_for_technician(db_path, tech, cursor, duration, max_jobs_per_day=0)
        audit = f"Preluată de {tech['name']} de la mecanicul #{current.get('technician_id') or 'nerepartizat'}. {note}".strip()
        if start != old_start:
            audit += f" Interval anterior: {current['start_at']}–{current['end_at']}."
        notes = '\n'.join(filter(None,(str(current.get('notes') or ''),audit)))
        conn.execute("""UPDATE scheduler_appointments SET technician_id=?,department=?,start_at=?,end_at=?,
            notes=?,source='mechanic_takeover',updated_at=CURRENT_TIMESTAMP WHERE id=?""",
            (technician_id,tech.get('department') or 'mecanica',start.isoformat(timespec='minutes'),end.isoformat(timespec='minutes'),notes,appointment_id))
        conn.commit()
        updated = dict(conn.execute('SELECT * FROM scheduler_appointments WHERE id=?',(appointment_id,)).fetchone())
        return {'appointment':updated,'changed':True,'slot_changed':start!=old_start,'from_technician_id':current.get('technician_id')}
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def cancel_appointment(db_path: Path, appointment_id: int, return_to_waiting: bool = False) -> dict:
    """Cancel locally without deleting history; optionally return the linked request to scheduling."""
    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        current = conn.execute("SELECT * FROM scheduler_appointments WHERE id=?", (appointment_id,)).fetchone()
        if not current:
            raise ValueError("Programarea nu a fost găsită.")
        request_id = int(current["request_id"] or 0) or None
        if return_to_waiting and request_id:
            cur = conn.execute("""
                UPDATE scheduler_appointments SET status='anulat',updated_at=CURRENT_TIMESTAMP
                WHERE request_id=? AND status!='anulat'
            """, (request_id,))
            conn.execute("UPDATE service_requests SET status='acceptat' WHERE id=?", (request_id,))
            cancelled_count = int(cur.rowcount or 0)
        else:
            conn.execute("UPDATE scheduler_appointments SET status='anulat',updated_at=CURRENT_TIMESTAMP WHERE id=?", (appointment_id,))
            cancelled_count = 1
        conn.commit()
        return {"status": "cancelled", "appointment_id": appointment_id, "request_id": request_id,
                "returned_to_waiting": bool(return_to_waiting and request_id),
                "cancelled_count": cancelled_count}


def _next_workday_start(value: datetime, start_text: str = "08:30") -> datetime:
    day = value.date() + timedelta(days=1)
    while day.weekday() >= 5:
        day += timedelta(days=1)
    return datetime.combine(day, datetime.strptime(start_text, "%H:%M").time())


def _client_greeting(customer_name: str | None) -> str:
    name = str(customer_name or "").strip()
    return f"Bună ziua, {name}!" if name else "Bună ziua!"


def _unfinished_vehicle_message(appointment: dict, remaining_hours: float,
                                continuation_start: datetime) -> str:
    vehicle = appointment.get("registration") or appointment.get("vehicle") or "mașina dumneavoastră"
    hours = f"{remaining_hours:g}".replace(".", ",")
    return (
        f"{_client_greeting(appointment.get('customer_name'))}\n\n"
        f"Pentru a finaliza corect lucrarea la {vehicle}, avem nevoie de aproximativ "
        f"{hours} ore suplimentare. Continuăm lucrarea în "
        f"{continuation_start.strftime('%d.%m.%Y')}, de la ora {continuation_start.strftime('%H:%M')}.\n\n"
        "Vă anunțăm din timp pentru a vă putea organiza. Consilierul urmărește lucrarea și vă "
        "contactează dacă estimarea se modifică. Vă mulțumim pentru înțelegere."
    )


def _shifted_appointment_message(appointment: dict, old_start: datetime,
                                 new_start: datetime) -> str:
    vehicle = appointment.get("registration") or appointment.get("vehicle") or "mașina dumneavoastră"
    return (
        f"{_client_greeting(appointment.get('customer_name'))}\n\n"
        f"Vă informăm din timp că programarea pentru {vehicle}, stabilită inițial pentru "
        f"{old_start.strftime('%d.%m.%Y la %H:%M')}, se mută la "
        f"{new_start.strftime('%d.%m.%Y la %H:%M')}.\n\n"
        "O lucrare aflată înaintea programării dumneavoastră a necesitat timp suplimentar, iar această "
        "întârziere decalează ora de intrare în service. Ne cerem scuze pentru această modificare. "
        "Am stabilit noul interval astfel încât mașina dumneavoastră să beneficieze de timpul complet "
        "de lucru și de calitatea necesară intervenției. "
        "Dacă noul interval nu este potrivit, răspundeți cu «ALT INTERVAL», iar consilierul vă va contacta. "
        "Vă mulțumim pentru înțelegere."
    )


def _fit_after(cursor: datetime, duration_hours: float, start_text: str, end_text: str) -> tuple[datetime, datetime]:
    duration = timedelta(hours=duration_hours)
    while True:
        while cursor.weekday() >= 5:
            cursor = _next_workday_start(cursor, start_text)
        opening = datetime.combine(cursor.date(), datetime.strptime(start_text, "%H:%M").time())
        closing = datetime.combine(cursor.date(), datetime.strptime(end_text, "%H:%M").time())
        start = max(cursor, opening)
        start += timedelta(minutes=(30 - start.minute % 30) % 30)
        if start + duration <= closing:
            return start, start + duration
        cursor = _next_workday_start(cursor, start_text)


def mark_appointment_incomplete(db_path: Path, appointment_id: int, remaining_hours: float,
                                note: str = "") -> dict:
    remaining = max(0.5, math.ceil(float(remaining_hours or 0) * 2) / 2)
    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        appointment = conn.execute("""
            SELECT a.*,t.name technician_name,t.work_start,t.work_end
            FROM scheduler_appointments a
            LEFT JOIN scheduler_technicians t ON t.id=a.technician_id WHERE a.id=?
        """, (appointment_id,)).fetchone()
        if not appointment:
            raise ValueError("Programarea nu a fost găsită.")
        appointment = dict(appointment)
        if not appointment.get("technician_id"):
            raise ValueError("Lucrarea trebuie repartizată unui mecanic înainte de continuare.")
        start_text = appointment.get("work_start") or "08:30"
        end_text = appointment.get("work_end") or "17:00"
        daily_capacity = (datetime.strptime(end_text, "%H:%M") -
                          datetime.strptime(start_text, "%H:%M")).total_seconds() / 3600
        cursor = _next_workday_start(_parse_local(appointment["end_at"]), start_text)
        continuation_segments = []
        left = remaining
        while left > 0:
            chunk = min(left, daily_capacity)
            start, end = _fit_after(cursor, chunk, start_text, end_text)
            continuation_segments.append((start, end, chunk))
            left = round(left - chunk, 2)
            cursor = end if left <= 0 else _next_workday_start(end, start_text)

        cascade_cursor = continuation_segments[-1][1]
        future = [dict(row) for row in conn.execute("""
            SELECT * FROM scheduler_appointments
            WHERE technician_id=? AND id!=? AND status!='anulat' AND start_at>=?
            ORDER BY start_at,id
        """, (appointment["technician_id"], appointment_id,
              continuation_segments[0][0].isoformat(timespec="minutes"))).fetchall()]
        shifted = []
        for item in future:
            old_start, old_end = _parse_local(item["start_at"]), _parse_local(item["end_at"])
            duration = (old_end - old_start).total_seconds() / 3600
            if old_start >= cascade_cursor:
                new_start, new_end = old_start, old_end
            else:
                new_start, new_end = _fit_after(cascade_cursor, duration, start_text, end_text)
            if new_start != old_start:
                shifted.append((item, old_start, new_start, new_end))
            cascade_cursor = max(cascade_cursor, new_end)

        conn.execute("""
            UPDATE scheduler_appointments
            SET status='neterminat',completion_note=?,updated_at=CURRENT_TIMESTAMP WHERE id=?
        """, (str(note or "").strip(), appointment_id))
        continuation_ids = []
        for index, (start, end, chunk) in enumerate(continuation_segments, 1):
            cur = conn.execute("""
                INSERT INTO scheduler_appointments
                (request_id,technician_id,start_at,end_at,status,customer_name,phone,registration,vehicle,
                 job_number,continued_from_id,operations,estimated_hours,notes)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """, (
                appointment.get("request_id"), appointment["technician_id"],
                start.isoformat(timespec="minutes"), end.isoformat(timespec="minutes"), "programat",
                appointment.get("customer_name"), appointment.get("phone"), appointment.get("registration"),
                appointment.get("vehicle"), appointment.get("job_number"), appointment_id,
                f"{appointment.get('operations') or 'Lucrare'} — continuare {index}/{len(continuation_segments)}",
                chunk, f"Continuare automată. Motiv: {str(note or '').strip()}",
            ))
            continuation_ids.append(int(cur.lastrowid))
        notifications = []
        grace_minutes = max(1, int(os.getenv("DELAY_NOTIFICATION_GRACE_MINUTES", "30")))
        auto_send_after = (datetime.now() + timedelta(minutes=grace_minutes)).isoformat(timespec="seconds")
        if str(appointment.get("phone") or "").strip():
            continuation_start = continuation_segments[0][0]
            current_message = _unfinished_vehicle_message(
                appointment, remaining, continuation_start
            )
            conn.execute("""
                UPDATE scheduler_notifications
                SET status='superseded'
                WHERE appointment_id=? AND status='pending_review'
            """, (appointment_id,))
            ncur = conn.execute("""
                INSERT INTO scheduler_notifications
                (appointment_id,request_id,phone,message,auto_send_after)
                VALUES (?,?,?,?,?)
            """, (appointment_id, appointment.get("request_id"), appointment.get("phone"),
                  current_message, auto_send_after))
            notifications.append({"id": int(ncur.lastrowid), "appointment_id": appointment_id,
                                  "phone": appointment.get("phone"), "message": current_message,
                                  "status": "pending_review", "auto_send_after": auto_send_after,
                                  "recipient_kind": "delayed_vehicle"})
        for item, old_start, new_start, new_end in shifted:
            conn.execute("""
                UPDATE scheduler_appointments SET start_at=?,end_at=?,updated_at=CURRENT_TIMESTAMP WHERE id=?
            """, (new_start.isoformat(timespec="minutes"), new_end.isoformat(timespec="minutes"), item["id"]))
            message = _shifted_appointment_message(item, old_start, new_start)
            conn.execute("""
                UPDATE scheduler_notifications
                SET status='superseded'
                WHERE appointment_id=? AND status='pending_review'
            """, (item["id"],))
            ncur = conn.execute("""
                INSERT INTO scheduler_notifications
                (appointment_id,request_id,phone,message,auto_send_after)
                VALUES (?,?,?,?,?)
            """, (item["id"], item.get("request_id"), item.get("phone"), message, auto_send_after))
            notifications.append({"id": int(ncur.lastrowid), "appointment_id": item["id"],
                                  "phone": item.get("phone"), "message": message,
                                  "status": "pending_review", "auto_send_after": auto_send_after,
                                  "recipient_kind": "shifted_customer"})
        conn.commit()
    return {"status": "continued", "appointment_id": appointment_id,
            "continuation_ids": continuation_ids, "shifted_count": len(shifted),
            "notifications": notifications}
