"""Quote arithmetic: labour is stored net; customer amounts include labour VAT.

Supplier part/oil prices already include VAT and must not be taxed again.
Legacy labour_price_ron values use the same net basis as hours * hourly_rate_ron.
"""
from decimal import Decimal, ROUND_HALF_UP
import os

CENT = Decimal('0.01')


def _number(value):
    number = Decimal(str(value or 0))
    if not number.is_finite() or number < 0:
        raise ValueError('Valoarea trebuie să fie pozitivă și finită.')
    return number


def labour_vat_percent():
    rate = _number(os.getenv('ALBATROS_LABOUR_VAT_PERCENT', '21'))
    if rate > 100:
        raise ValueError('Cota TVA pentru manoperă este invalidă.')
    return float(rate.quantize(CENT, rounding=ROUND_HALF_UP))


def labour_pricing(net, *, vat_percent=None):
    rate = _number(labour_vat_percent() if vat_percent is None else vat_percent)
    if rate > 100:
        raise ValueError('Cota TVA pentru manoperă este invalidă.')
    net = _number(net).quantize(CENT, rounding=ROUND_HALF_UP)
    tax = (net * rate / 100).quantize(CENT, rounding=ROUND_HALF_UP)
    return {'labour_price_ron': float(net), 'labour_price_basis': 'net',
            'labour_vat_percent': float(rate), 'labour_vat_ron': float(tax),
            'labour_price_with_vat_ron': float(net + tax)}


def quote_pricing(draft):
    labour = labour_pricing(draft.get('labour_price_ron'), vat_percent=draft.get('labour_vat_percent'))
    parts = (_number(draft.get('parts_price_ron'))
             + _number(draft.get('oil_unit_price_ron')) * _number(draft.get('oil_liters'))
             + (_number(draft.get('drain_plug_price_ron')) if draft.get('drain_plug_selected') else 0))
    parts = parts.quantize(CENT, rounding=ROUND_HALF_UP)
    return {**labour, 'parts_total_ron': float(parts),
            'total_ron': float(parts + Decimal(str(labour['labour_price_with_vat_ron'])))}
