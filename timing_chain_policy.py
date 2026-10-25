"""Workshop scope for confirmed timing-chain replacement, without VIN guesses."""
import re
import unicodedata


def plain(value):
    return re.sub(r'\s+', ' ', unicodedata.normalize('NFKD', str(value or '')).encode('ascii', 'ignore').decode().lower()).strip()


def chain_replacement(value):
    text = plain(value).split('| termeni tehnici:', 1)[0].split('| regula distributie:', 1)[0]
    # A transfer-case chain and diagnosis alone are different jobs.
    if re.search(r'cuti[aei]+\s+de\s+distributie|transfer case', text):
        return False
    if re.search(r'\b(?:verific\w*|diagnostic\w*|constat\w*)\b', text) and not re.search(r'\b(?:inlocui\w*|schimb\w*)\b', text):
        return False
    return bool(re.search(r'\b(?:lant(?:ul|uri|urile)?\s+(?:de\s+)?distributie|distributi\w*\s+(?:pe\s+|cu\s+)?lant\w*|timing chain)\b', text))


def intake_cleaning(value):
    text = plain(value).split('| regula distributie:', 1)[0]
    return bool(re.search(r'curat\w*.{0,35}(?:admisie|galeri)|(?:admisie|galeri).{0,35}curat\w*', text)
                and not re.search(r'(?:fara|nu\s+(?:se\s+)?(?:cere|doresc|vreau))\s+curat', text))


def expand_chain_scope(value):
    original = str(value or '').strip()
    if not chain_replacement(original) or '| REGULA DISTRIBUTIE:' in original:
        return original
    additions = ('kit distribuție pe lanț; revizie completă ulei motor original și filtre: '
                 'filtru ulei, filtru aer, filtru habitaclu, filtru combustibil dacă este servisabil; '
                 'șaibe de foc injectoare; garnituri capac chiulasă; simering palier și simering distribuție')
    if intake_cleaning(original):
        additions += '; garnituri galerie admisie'
    return original + ' | REGULA DISTRIBUTIE: ' + additions


def default_checklist(value):
    if not chain_replacement(value):
        return []
    names = ['Kit distribuție pe lanț — conținut verificat după VIN', 'Filtru ulei',
             'Filtru aer', 'Filtru habitaclu', 'Filtru combustibil, dacă este servisabil',
             'Șaibe de foc injectoare', 'Garnituri capac chiulasă', 'Simering palier',
             'Simering distribuție — fără dublarea aceluiași reper',
             'Șuruburi și etanșări de înlocuit conform procedurii']
    if intake_cleaning(value):
        names.append('Garnituri galerie admisie pentru curățarea solicitată')
    return [dict(description=name, code='', quantity=None, unit_price_ron=None,
                 confirmed=False, note='Regulă Albatros; identificare și preț de confirmat după VIN.') for name in names]
