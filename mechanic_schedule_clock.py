"""Romanian business-day clock, independent of server timezone and DSM import."""
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

ROMANIA = ZoneInfo('Europe/Bucharest')

def local_time(now):
    return now.replace(tzinfo=ROMANIA) if now.tzinfo is None else now.astimezone(ROMANIA)

def due_day(now, clock=(8, 0)):
    now = local_time(now)
    due = now.replace(hour=clock[0], minute=clock[1], second=0, microsecond=0)
    return now.date() if now.weekday() < 5 and due <= now < due + timedelta(minutes=20) else None

def next_send(now, clock=(8, 0)):
    now = local_time(now)
    due = now.replace(hour=clock[0], minute=clock[1], second=0, microsecond=0)
    if due <= now:
        due += timedelta(days=1)
    while due.weekday() >= 5:
        due += timedelta(days=1)
    return due
