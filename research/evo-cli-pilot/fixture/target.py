"""Synthetic integer-list parser; no Wisp imports or data."""
def parse(text):
    if not text.strip():
        return []
    return [int(piece.strip()) for piece in text.split(',')]
