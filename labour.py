import math
import os
from dataclasses import dataclass
from typing import Optional
from database import find_labour_time, save_labour_time

@dataclass
class LabourTimeResult:
    vin: str
    operation: str
    technical_hours: float
    albatros_expected_hours: float
    booking_hours: float
    source: str
    source_reference: Optional[str] = None

def _round_booking_hours(hours: float) -> float:
    minutes_step = int(os.getenv("BOOKING_ROUND_MINUTES", "30"))
    if minutes_step <= 0:
        minutes_step = 30
    total_minutes = hours * 60
    rounded = math.ceil(total_minutes / minutes_step) * minutes_step
    return rounded / 60

def calculate_booking_time(technical_hours: float, historical_hours: Optional[float] = None):
    if historical_hours and historical_hours > 0:
        expected = historical_hours
    else:
        factor = float(os.getenv("ALBATROS_TIME_FACTOR", "1.00"))
        expected = technical_hours * factor

    buffer_hours = float(os.getenv("BOOKING_BUFFER_HOURS", "1.00"))
    booking_base = expected + buffer_hours

    return round(expected, 2), round(_round_booking_hours(booking_base), 2)
def get_labour_time(vin: str, operation: str) -> Optional[LabourTimeResult]:
    row = find_labour_time(vin, operation)
    if not row:
        return None
    expected, booking = calculate_booking_time(float(row["technical_hours"]), float(row["historical_hours"]) if row.get("historical_hours") else None)
    return LabourTimeResult(vin=vin, operation=row["operation"], technical_hours=float(row["technical_hours"]), albatros_expected_hours=expected, booking_hours=booking, source=row["source"], source_reference=row.get("source_reference"))

def add_manual_labour_time(vin, operation, technical_hours, source="manual", source_reference=None, historical_hours=None):
    return save_labour_time({"vin":vin.strip().upper(),"operation":operation.strip(),"technical_hours":technical_hours,"historical_hours":historical_hours,"source":source,"source_reference":source_reference})
