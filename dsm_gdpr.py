"""Native DSM GDPR links. Never sign, send DSM SMS, or create transactions here."""
import hashlib
import json
import re
from datetime import datetime, timezone
from urllib.parse import urlsplit

from dsm_pre_reception import call
from dsm_open_orders_readonly import _evaluate

DOCUMENT = 'statement_of_agreement'  # DSM: Acord GDPR Client, not service_offer.
SIGNED = {'signed', 'complete', 'completed'}


def signing_link(value):
    """Only the vendor's native HTTPS link, never an uploaded PDF or chat URL."""
    if (not isinstance(value, str) or not 1 <= len(value) <= 2048 or '\\' in value
            or any(c.isspace() or ord(c) < 32 for c in value)):
        raise ValueError('Link GDPR invalid.')
    parsed = urlsplit(value)
    host = (parsed.hostname or '').lower()
    trusted = any(host == root or host.endswith('.' + root)
                  for root in ('servicemanager.q-service.ro', 'dsmpro.ro'))
    if (parsed.scheme != 'https' or not trusted or parsed.username or parsed.password
            or parsed.port not in (None, 443) or parsed.path.lower().endswith('.pdf')):
        raise ValueError('Linkul GDPR nu este un link de semnare DSM permis.')
    return value


def identity(data):
    fields = [str(data.get(key) or '').strip() for key in
              ('contact_id', 'vin', 'client_name', 'client_address', 'phone')]
    return hashlib.sha256(json.dumps(fields, ensure_ascii=False).encode()).hexdigest()


def signature(payload, kind):
    states = payload.get('sms_or_qrcode_sended') or {}
    if not isinstance(states, dict):
        return ''
    value = states.get(str(kind) + '_' + DOCUMENT, states.get(DOCUMENT, ''))
    if isinstance(value, dict):
        value = next((value[k] for k in ('record_status__code', 'sms_feedback', 'status', 'state')
                      if value.get(k)), '')
    return str(value or '').strip().lower()


async def generate(ident):
    if not re.fullmatch(r'[1-9]\d*', str(ident)):
        raise ValueError('ID tranzacție GDPR invalid.')
    # Route/document verified against DSM's qrcode-document handler. This
    # generates a native signing link; it does not send SMS or sign the form.
    path = '/special/transaction/generate_qr_code/' + str(ident) + '/' + DOCUMENT
    return await _evaluate('''(async()=>{
      if(location.protocol!=='https:'||!['s.servicemanager.q-service.ro','apps.dsmpro.ro','appm.dsmpro.ro'].includes(location.hostname))throw Error('Domeniu DSM nepermis');
      const r=await fetch(__PATH__,{method:'POST',credentials:'same-origin',headers:{Accept:'application/json','X-CSRF-TOKEN':document.querySelector('meta[name="csrf-token"]')?.content||''},signal:AbortSignal.timeout(12000)});
      if(!r.ok||r.url.includes('/login'))throw Error('Generare GDPR incertă');
      return JSON.stringify(await r.json());})()'''.replace('__PATH__', json.dumps(path)))


async def prepare(data, checkpoint, *, check_only=False):
    """Persist before generating, revalidate identity before sharing/checking.

    A native appointment ID is never a transaction ID. Without an explicitly
    associated offer/order, defer until the normal confirmed writer returns an
    order. Existing paper/file flags alone are not proof of remote signing.
    """
    # GDPR belongs to the identified client, independently of the vehicle's
    # annual certificate refresh. A new owner can bring a recently seen car.
    if not data.get('production'):
        return {}
    previous = data.get('gdpr') or {}
    if (previous.get('state') == 'declined' and not check_only
            or previous.get('reason') == 'existing_agreement_requires_review'):
        return previous  # No automatic regeneration or reminder after refusal.
    binding = identity(data)
    if previous.get('binding') and previous['binding'] != binding:
        data['gdpr'] = {**previous, 'state': 'review', 'reason': 'client_data_changed'}
        checkpoint()
        return data['gdpr']
    ident = str(previous.get('transaction_id') or (data.get('order') or {}).get('id')
                or data.get('approved_existing_order_id') or data.get('appointment_command_id') or '')
    expected_kind = '2'
    if previous.get('transaction_id'):
        expected_kind = previous.get('transaction_type')
    elif not ident and data.get('source_explicit') and data.get('source_type') in {'1', '2'}:
        ident = str(data.get('source_transaction_id') or '')
        expected_kind = data['source_type']
    if not ident:
        data['gdpr'] = {'state': 'awaiting_order', 'reason': 'no_bound_transaction'}
        checkpoint()
        return data['gdpr']
    if not re.fullmatch(r'[1-9]\d*', ident):
        raise ValueError('Asociere GDPR invalidă.')
    payload = await call('/special/transaction/get_transaction/' + ident)
    head = payload.get('transaction') or {}
    if (str(head.get('transaction_head__id')) != ident
            or str(head.get('transaction_head__type')) != expected_kind
            or expected_kind not in {'1', '2'}
            or str(head.get('contact_id')) != str(data.get('contact_id'))
            or str(head.get('car_id')) != str(data.get('car_id'))
            or not re.fullmatch(r'[A-HJ-NPR-Z0-9]{17}', str(data.get('vin') or ''))
            or str(head.get('car_body_series') or '').upper() != data['vin']):
        raise ValueError('Documentul GDPR nu poate fi asociat sigur clientului și mașinii.')
    record = {**previous, 'binding': binding, 'transaction_id': ident, 'transaction_type': expected_kind}
    native_signature = signature(payload, expected_kind)
    file_flag = (payload.get('electronic') or {}).get('has_gdpr')
    if native_signature in SIGNED and file_flag not in (True, 1, '1'):
        # The transaction might retain an old signing event after its client
        # changed. Require the current client's GDPR file flag as well.
        record.update(state='review', reason='signature_file_disagreement')
    elif native_signature in SIGNED:
        record.update(state='signed', verified_at=datetime.now(timezone.utc).isoformat(),
                      verification_source='dsm.sms_or_qrcode_sended.' + expected_kind + '_' + DOCUMENT)
        record.pop('link', None)
        record.pop('reason', None)
    elif previous.get('state') == 'review':
        return previous  # Read-only signature recovery is allowed; regeneration is not.
    elif previous.get('state') == 'declined':
        record.update(state='declined')
    elif head.get('transaction_head__status') not in {'working', ''}:
        record.update(state='review', reason='transaction_not_active')
    elif previous.get('state') == 'generating':
        record.update(state='review', reason='generation_uncertain')
    elif previous.get('link'):
        record.update(state='pending', link=signing_link(previous['link']))
        record.pop('verified_at', None)
        record.pop('verification_source', None)
    elif previous.get('state') == 'signed':
        record.update(state='review', reason='signature_no_longer_confirmed')
    elif file_flag in (True, 1, '1'):
        # DSM may contain an uploaded paper agreement. Neither presume it is
        # unsigned and resend nor claim its signature has been verified.
        record.update(state='review', reason='uploaded_agreement_requires_review')
    elif file_flag not in (False, 0, '0'):
        record.update(state='review', reason='agreement_presence_unverified')
    elif native_signature:
        record.update(state='review', reason='existing_request_requires_review')
    elif check_only:
        record.update(state='review', reason='no_signing_link')
    elif (payload.get('electronic') or {}).get('has_qrcode') not in (True, 1, '1'):
        record.update(state='review', reason='native_signing_unavailable')
    else:
        record.update(state='generating')
        data['gdpr'] = record
        checkpoint()  # A timeout/restart may follow successful DSM generation.
        try:
            response = await generate(ident)
            if str(response.get('status')) != '1':
                raise ValueError('DSM nu a confirmat generarea linkului GDPR.')
            record.update(state='pending', link=signing_link(response.get('link')))
        except Exception:
            record.update(state='review', reason='generation_uncertain')
    data['gdpr'] = record
    checkpoint()
    return record
