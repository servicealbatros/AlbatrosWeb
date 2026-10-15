"""Shared original engine-oil policy; approvals alone are not OEM branding."""
import re
import unicodedata


def normalized(value):
    return unicodedata.normalize('NFKD', str(value or '')).encode('ascii', 'ignore').decode().upper()


def original_oil_brands(vehicle):
    text = normalized(vehicle)
    groups = (
        (r'^(?:VW|VOLKSWAGEN|AUDI|SKODA|SEAT|CUPRA)\b', ('VW', 'VOLKSWAGEN', 'AUDI', 'SKODA', 'SEAT', 'CUPRA', 'VAG')),
        (r'^MERCEDES(?:[- ]BENZ)?\b', ('MERCEDES', 'MERCEDES-BENZ')),
    )
    for pattern, brands in groups:
        if re.search(pattern, text):
            return brands
    for brand in ('BMW', 'MINI', 'FORD', 'TOYOTA', 'LEXUS', 'HONDA', 'MAZDA', 'NISSAN', 'RENAULT', 'DACIA', 'PEUGEOT', 'CITROEN', 'OPEL', 'FIAT', 'VOLVO', 'HYUNDAI', 'KIA', 'SUZUKI', 'MITSUBISHI', 'SUBARU', 'IVECO', 'JAGUAR', 'LAND ROVER', 'PORSCHE'):
        if re.match(r'^' + re.escape(brand) + r'\b', text):
            return (brand,)
    return ()


def is_original_engine_oil(product, vehicle):
    brands = original_oil_brands(vehicle)
    if not brands:
        return False
    heading = normalized(product.get('title') or product.get('description')).split('|', 1)[0].strip()
    brand = normalized(product.get('brand')).strip()
    if any(re.match(r'^(?:OE\s+)?' + re.escape(value) + r'\b', heading) for value in brands):
        return True
    # A supplier's explicit manufacturer field is acceptable, but an OEM
    # approval buried in the description of Castrol/Motul is not.
    return brand in brands and not re.match(r'^(?:CASTROL|MOTUL|TEXACO|HAVOLINE|LIQUI|MOBIL|SHELL|TOTAL|FUCHS)\b', heading)
