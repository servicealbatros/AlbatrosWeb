"""Separate drivetrain lubrication from an engine-oil service; never infer xDrive."""
import re
from timing_chain_policy import plain, chain_replacement


def drivetrain_operations(value):
    text = plain(value).split('| termeni tehnici:', 1)[0]
    if not re.search(r'ulei|lubrif|fluid|atf|dtf|oil', text):
        return []
    names = []
    automatic = bool(re.search(r'transmisi\w* automat|cuti[ae]\w*(?: de)? (?:viteze )?automat|ga8hp|8hp\d', text))
    if automatic:
        if re.search(r'\b(?:baie|baii|filtru|filtrul)\b', text):
            names.append(('automatic_transmission_service', 'Unitate formată din baia de ulei pentru cutia de viteze automată și filtru înlocuire'))
        else:
            names.append(('automatic_transmission_oil', 'Ulei transmisie automată înlocuire'))
    if re.search(r'cuti[ae]\w*(?: de)? transfer|\batc\s*13\b|ulei cuti[ae]\w*(?: de)? distribut', text):
        names.append(('transfer_case_oil', 'Ulei cutia de distribuție înlocuire'))
    differential = re.search(r'diferent\w*|grup(?:ul|uri|urile)?\b', text)
    if differential:
        tail = text[differential.start():]
        for axle, pattern, label in [('front', r'\bfata\b|anterior', 'punte față'),
                                     ('rear', r'\bspate\b|posterior', 'osie spate')]:
            if re.search(pattern, tail):
                names.append((f'differential_oil_{axle}', f'Ulei diferențial {label} înlocuire'))
    return [dict(id=key, canonical=name, haynes_queries=[name], matched_alias=name, intent_score=1.0)
            for key, name in names]


def engine_service_requested(value):
    text = plain(value).split('| termeni tehnici:', 1)[0]
    if chain_replacement(value) or re.search(r'ulei(?: de)? motor|engine oil|revizie motor|filtr\w* (?:de )?(?:aer|habitaclu|polen|combustibil|motorina)', text):
        return True
    if drivetrain_operations(value) or re.search(r'\bcuti[ae]\w*|\btransmisi\w*|\bdiferent\w*|\bgrup(?:ul|uri|urile)?\b|\batf\b|\bdtf\b', text):
        return False
    return 'revizie' in text or 'filtr' in text or ('ulei' in text and 'schimb' in text)
