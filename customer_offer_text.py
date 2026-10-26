"""Customer wording only; preserve technical values and internal source records."""
import re


_SOURCE = r'\b(?:Auto[\s-]*Total|Inter[\s-]*Cars|eOriginal|TecRMI|Haynes(?:Pro)?|DSM|Q[\s-]*Service(?:\s+Manager)?)\b'
_SOURCE_URL = (r'https?://[^\s<>]*(?:eoriginal\.ro|autototal\.ro|intercars\.eu|'
               r'workshopdata\.com|tecalliance\.net|servicemanager\.q-service\.ro)[^\s<>]*')


def customer_offer_text(value: str) -> str:
    """Strip our integration names, without changing brands, prices or approvals."""
    text = str(value or '')
    # The supplier catalogue relays the automaker's requirements; it does not
    # establish an oil approval or the permitted viscosity itself.
    text = re.sub(
        r'conform\s+(?:opțiunilor|optiunilor|cerințelor|cerintelor|specificațiilor|specificatiilor)\s+'
        + _SOURCE + r'(?:\s+pentru\s+VIN)?',
        'conform cerințelor producătorului auto', text, flags=re.I)
    text = re.sub(r'(?im)^\s*(?:surs[ăa]|surse|furnizor|catalog(?:ul)? tehnic)\s*:[^\n]*' + _SOURCE + r'[^\n]*', '', text)
    text = re.sub(_SOURCE_URL, '', text, flags=re.I)
    text = re.sub(r'\b(?:din|prin|de la|în|in)\s+(?:catalog(?:ul)?\s+)?' + _SOURCE, '', text, flags=re.I)
    text = re.sub(_SOURCE, '', text, flags=re.I)
    lines = [re.sub(r'[ \t]+([,;:.])', r'\1', re.sub(r'[ \t]+', ' ', line)).strip(' \t|—;/')
             for line in text.splitlines()]
    return re.sub(r'\n{3,}', '\n\n', '\n'.join(lines)).strip()


def customer_oil_specification(value: str) -> str:
    """The label belongs to the automaker; keep qualifications and alternatives."""
    return customer_offer_text(value)
