"""Editable quote lines: an unknown price stays unknown, never free."""
from html import escape
import math


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
        if item['confirmed'] and (not item['code'] or item['quantity'] is None or item['unit_price_ron'] is None):
            raise ValueError('Confirmarea necesită cod, cantitate și preț: ' + item['description'])
        output.append(item)
    return output


def pending_lines(rows):
    return [row['description'] for row in normalize_lines(rows)
            if not row['confirmed'] or row['quantity'] is None or row['unit_price_ron'] is None or not row['code']]


def subtotal(rows):
    return round(sum((r['quantity'] or 0) * (r['unit_price_ron'] or 0) for r in normalize_lines(rows)), 2)


def render_lines(rows):
    if not rows:
        return ''
    body = []
    for item in normalize_lines(rows):
        e = lambda key: escape(str(item.get(key) or ''), quote=True)
        number = lambda key: '' if item[key] is None else str(item[key])
        body.append(f'''<tr class="quote-part-row" data-source="{e('source')}">
          <td><input class="quote-description" aria-label="Denumire piesă" value="{e('description')}">
          <small>{e('note')}</small><input class="quote-note" type="hidden" value="{e('note')}"></td>
          <td><input class="quote-code" aria-label="Cod piesă" value="{e('code')}"></td>
          <td><input class="quote-quantity" aria-label="Cantitate piesă" type="number" min="0.001" step="any" value="{number('quantity')}" placeholder="De confirmat"></td>
          <td><input class="quote-price" aria-label="Preț unitar piesă" type="number" min="0" step="0.01" value="{number('unit_price_ron')}" placeholder="De confirmat"></td>
          <td><input class="quote-confirmed" aria-label="Piesă verificată" type="checkbox" {'checked' if item['confirmed'] else ''}></td></tr>''')
    return '''<div class="card" id="quote_parts_checklist"><h2>Necesar de piese — verificare și prețuri</h2>
      <p>Prețurile goale sunt neconfirmate. Totalul este parțial până la verificarea tuturor pozițiilor.
      Uleiul se completează separat în secțiunea dedicată și se facturează o singură dată.</p>
      <div style="overflow:auto"><table><thead><tr><th>Piesă / observații</th><th>Cod</th><th>Cantitate</th><th>Preț unitar RON</th><th>Verificat</th></tr></thead><tbody>''' + ''.join(body) + '</tbody></table></div></div>'
