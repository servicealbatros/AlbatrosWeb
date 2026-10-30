"""Calendar-year document refresh policy for production pre-reception.

Document evidence and reuse of DSM data are separate authorizations. Reusing a
record must never claim that a registration certificate was photographed.
"""
from datetime import date, datetime
from zoneinfo import ZoneInfo

ROMANIA = ZoneInfo('Europe/Bucharest')
VERSION = 1


def today():
    return datetime.now(ROMANIA).date()


def saved_date(value):
    if isinstance(value, datetime):
        return value.astimezone(ROMANIA).date() if value.tzinfo else value.date()
    if isinstance(value, date):
        return value
    value = str(value or '').strip()
    if not value:
        return None
    try:
        return saved_date(datetime.fromisoformat(value))
    except ValueError:
        for pattern in ('%d.%m.%Y', '%d.%m.%Y %H:%M', '%d.%m.%Y %H:%M:%S'):
            try:
                return datetime.strptime(value, pattern).date()
            except ValueError:
                pass
    return None


def anniversary(day):
    try:
        return day.replace(year=day.year + 1)
    except ValueError:
        return day.replace(year=day.year + 1, day=28)


def evaluate(*, vehicle_exists, last_visit=None, checked_on=None, history_complete=True):
    current = checked_on or today()
    visit = saved_date(last_visit)
    result = dict(version=VERSION, checked_on=current.isoformat(), vehicle_exists=vehicle_exists,
                  last_visit=visit.isoformat() if visit else None)
    if vehicle_exists is False:
        return dict(result, decision='request_document', reason='vehicle_not_found')
    if not vehicle_exists or not history_complete or visit is None or visit > current:
        return dict(result, decision='review', reason='last_visit_unverified')
    due = anniversary(visit)
    return dict(result, next_update=due.isoformat(),
                decision='request_document' if current >= due else 'reuse_dsm',
                reason='annual_update' if current >= due else 'recent_visit')


def can_reuse(data, *, checked_on=None):
    policy = data.get('document_policy') or {}
    if (not data.get('production') or data.get('manual_test') or data.get('document_refresh_requested')
            or policy.get('version') != VERSION or policy.get('decision') != 'reuse_dsm'
            or policy.get('vin') != data.get('vin') or policy.get('car_id') != data.get('car_id')):
        return False
    return evaluate(vehicle_exists=policy.get('vehicle_exists'), last_visit=policy.get('last_visit'),
                    checked_on=checked_on)['decision'] == 'reuse_dsm'


def preparation_ready(data):
    return ((can_reuse(data) and data.get('document_source') == 'dsm')
            or all(data.get(key) for key in ('document_verified', 'document_current', 'document_fields_confirmed')))


def load_stored_vehicle(data):
    """Copy only real saved fields; do not invent missing ITP/owner/year data."""
    previous = data.get('previous_dsm') or {}
    expiry = saved_date(previous.get('car_itp_expires_date'))
    year = str(previous.get('car_manufacture_year') or '')
    data['itp'] = expiry.isoformat() if expiry else None
    data['manufacture_year'] = int(year) if year.isdigit() and 1886 <= int(year) <= today().year + 1 else None
    data['document_source'] = 'dsm'
    data['itp_source'] = 'dsm'
