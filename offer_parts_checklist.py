"""Editable quote lines: an unknown price stays unknown, never free."""
from html import escape
import math
import hashlib
import re
import unicodedata


def is_workshop_consumable(row):
    code = re.sub(r'[^A-Z0-9]', '', str(row.get('code') or '').upper())
    if code in {'83190404517', '83230441070', '83238854223'}:
        return True
    text = unicodedata.normalize('NFKD', str(row.get('description') or '')).encode('ascii', 'ignore').decode().lower()
    return bool(re.search(r'\betansant\w*|\bloctite\s*5970\b|\bpast[ae]\b.{0,35}(?:etans|montaj|injector|lubrif)|\b(?:lavet\w*|carp[ae])\b|\bdegresant\w*|\bspray\b.{0,20}curat', text))


def material_key(row):
    value = (str(row.get('code') or '').upper().replace(' ', '') or str(row.get('description') or '').strip().casefold())
    return hashlib.sha256(value.encode()).hexdigest()[:20]


def split_workshop_materials(rows, internal, previous_rows=(), stock_request=None):
    """Only a server-recorded mechanic request can authorize a consumable charge."""
    authorized = {material_key(row): row['stock_request'] for row in previous_rows
                  if is_workshop_consumable(row) and isinstance(row.get('stock_request'), dict) and row['stock_request'].get('technician')
                  and row['stock_request'].get('requested_at')}
    if stock_request:
        authorized[stock_request['item_key']] = stock_request
    waiting = {material_key(row): row for row in normalize_lines(internal)}
    billable = []
    for row in normalize_lines(rows):
        key = material_key(row)
        row.pop('stock_request', None)
        if is_workshop_consumable(row):
            if key not in authorized:
                waiting[key] = row
                continue
            row['stock_request'] = authorized[key]
            # Changing the charged quantity requires recording a new actual request.
            if row['quantity'] != authorized[key]['quantity']:
                raise ValueError('Cantitatea consumabilului trebuie să corespundă cererii din magazie.')
            waiting.pop(key, None)
        billable.append(row)
    return billable, list(waiting.values())


def normalize_lines(rows):
    if not isinstance(rows, list) or len(rows) > 120:
        raise ValueError('Lista trebuie să conțină cel mult 120 de poziții.')
    output = []
    for row in rows:
        if not isinstance(row, dict) or not str(row.get('description') or '').strip():
            raise ValueError('Fiecare poziție trebuie să aibă o denumire.')
        item = {key: str(row.get(key) or '').strip()[:limit] for key, limit in
                [('description', 240), ('code', 120), ('note', 600), ('source', 160)]}
        for field, upper in [('quantity', 999), ('unit_price_ron', 1_000_000)]:
            value = row.get(field)
            value = None if value in (None, '') else float(value)
            if value is not None and (not math.isfinite(value) or value < 0 or value > upper
                                      or (field == 'quantity' and value == 0)):
                raise ValueError('Cantitate sau preț invalid pentru ' + item['description'])
            item[field] = value
        if row.get('confirmed') not in (None, True, False):
            raise ValueError('Verificarea piesei trebuie să fie o valoare booleană.')
        item['confirmed'] = row.get('confirmed') is True
        if isinstance(row.get('stock_request'), dict):
            item['stock_request'] = dict(row['stock_request'])
        if item['confirmed'] and (not item['code'] or item['quantity'] is None or item['unit_price_ron'] is None):
            raise ValueError('Confirmarea necesită cod, cantitate și preț: ' + item['description'])
        output.append(item)
    return output


def render_internal_materials(rows):
    if not rows:
        return ''
    items = []
    for row in normalize_lines(rows):
        key = material_key(row)
        name = escape(row['description'])
        code = escape(row['code']) or 'Cod de confirmat'
        items.append(f'''<li style="margin:16px 0"><strong>{name}</strong> · {code}
          <details id="stock_{key}"><summary>Înregistrează cererea mecanicului din magazie</summary>
          <div class="stock-request-fields">
          <label>Mecanic<input class="stock-technician" required maxlength="120"></label>
          <label>Cantitate solicitată<input class="stock-quantity" type="number" min="0.001" step="any" required></label>
          <label>Preț unitar RON<input class="stock-price" type="number" min="0" step="0.01" required></label>
          <label>Referință cerere / bon magazie<input class="stock-reference" maxlength="160"></label>
          </div>
          <button type="button" onclick="recordWorkshopRequest('{key}')">Adaugă în deviz cererea efectivă</button>
          </details></li>''')
    return '''<div class="card" id="internal_workshop_materials"><style>
      #internal_workshop_materials summary{cursor:pointer;color:#1d4ed8;margin:8px 0}
      #internal_workshop_materials .stock-request-fields{display:grid;grid-template-columns:repeat(4,minmax(120px,1fr));gap:12px;margin:12px 0}
      #internal_workshop_materials label{display:flex;flex-direction:column;gap:6px;font-size:14px}
      #internal_workshop_materials input{width:100%;box-sizing:border-box;padding:9px;border:1px solid #cbd5e1;border-radius:6px}
      #internal_workshop_materials button{background:#1d4ed8;color:white;border:0;border-radius:7px;padding:10px 14px;cursor:pointer}
      @media(max-width:750px){#internal_workshop_materials .stock-request-fields{grid-template-columns:1fr 1fr}}
      </style><h2>Consumabile de atelier — necesar intern</h2>
      <p>Nu sunt incluse în oferta inițială sau în total. Se adaugă în deviz numai după cererea efectivă
      a mecanicului către magazie, cu mecanic și cantitate înregistrate. Nu se solicită automat din magazie.</p><ul>''' + ''.join(items) + '</ul></div>'


def line_gaps(rows):
    result = []
    for row in normalize_lines(rows):
        missing = [label for field, label in [('code', 'cod piesă'), ('quantity', 'cantitate'),
                                               ('unit_price_ron', 'preț unitar')]
                   if row[field] in (None, '')]
        if not row['confirmed']:
            missing.append('verificarea consilierului')
        if missing:
            result.append(dict(description=row['description'], missing=missing))
    return result


def pending_lines(rows):
    return [row['description'] for row in line_gaps(rows)]


def subtotal(rows):
    return round(sum((r['quantity'] or 0) * (r['unit_price_ron'] or 0) for r in normalize_lines(rows)), 2)


def render_lines(rows, *, engine_oil_separate=True):
    if not rows:
        return ''
    body = []
    for item in normalize_lines(rows):
        e = lambda key: escape(str(item.get(key) or ''), quote=True)
        number = lambda key: '' if item[key] is None else str(item[key])
        stock = item.get('stock_request') or {}
        stock_note = escape(f"Cerut din magazie de {stock.get('technician')} la {stock.get('requested_at')}; {stock.get('reference') or ''}") if stock else ''
        gaps = line_gaps([item])
        review_note = ('Lipsește: ' + ', '.join(gaps[0]['missing']) if gaps else 'Verificată de consilier')
        body.append(f'''<tr class="quote-part-row" data-source="{e('source')}">
          <td><input class="quote-description" aria-label="Denumire piesă" value="{e('description')}">
          <small class="quote-review-status" style="color:{'#a12e2e' if gaps else '#13784c'}">{escape(review_note)}</small>
          <small>{e('note')}</small><small>{stock_note}</small><input class="quote-note" type="hidden" value="{e('note')}"></td>
          <td><input class="quote-code" aria-label="Cod piesă" value="{e('code')}"></td>
          <td><input class="quote-quantity" aria-label="Cantitate piesă" type="number" min="0.001" step="any" value="{number('quantity')}" placeholder="De confirmat"></td>
          <td><input class="quote-price" aria-label="Preț unitar piesă" type="number" min="0" step="0.01" value="{number('unit_price_ron')}" placeholder="De confirmat"></td>
          <td><input class="quote-confirmed" aria-label="Piesă verificată" type="checkbox" {'checked' if item['confirmed'] else ''}></td></tr>''')
    oil_note = ('Uleiul de motor se completează separat în secțiunea dedicată și se facturează o singură dată.'
                if engine_oil_separate else 'Uleiurile transmisiei sunt pe poziții distincte, pentru fiecare ansamblu.')
    return '''<div class="card" id="quote_parts_checklist"><style>
      #quote_parts_checklist table{min-width:800px}#quote_parts_checklist td:first-child{width:44%}
      #quote_parts_checklist input:not([type=checkbox]){width:100%;box-sizing:border-box;padding:7px;border:1px solid #cbd5e1;border-radius:6px}
      #quote_parts_checklist small{display:block;margin-top:5px;line-height:1.45;color:#475569}
      </style><h2>Necesar de piese — verificare și prețuri</h2>
      <p>Prețurile goale sunt neconfirmate. Totalul este parțial până la verificarea tuturor pozițiilor.
      ''' + oil_note + '''</p>
      <div style="overflow:auto"><table><thead><tr><th>Piesă / observații</th><th>Cod</th><th>Cantitate</th><th>Preț unitar RON</th><th>Verificat</th></tr></thead><tbody>''' + ''.join(body) + '</tbody></table></div></div>'
