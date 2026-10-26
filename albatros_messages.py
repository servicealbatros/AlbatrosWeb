"""Bundle branded, accessible messages into authenticated dashboard pages."""
from functools import lru_cache
from pathlib import Path


@lru_cache(maxsize=1)
def controller():
    return (Path(__file__).parent/'static'/'albatros_messages.js').read_bytes()


def install_messages(html: bytes) -> bytes:
    if b'data-albatros-ai-messages' in html:
        return html
    script = b'<script data-albatros-ai-messages>' + controller() + b'</script>'
    index = html.lower().find(b'</head>')
    if index >= 0:
        return html[:index] + script + html[index:]
    index = html.lower().find(b'<script')
    if index >= 0:
        return html[:index] + script + html[index:]
    return html + script
