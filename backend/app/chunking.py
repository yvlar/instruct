import re


def chunk_text(text: str, size: int = 1400, overlap: int = 250) -> list[str]:
    """Normalize text and split it into overlapping, sentence-aware chunks."""
    clean = re.sub(r"\s+", " ", text).strip()
    if not clean:
        return []

    chunks: list[str] = []
    start = 0
    while start < len(clean):
        end = min(start + size, len(clean))
        if end < len(clean):
            boundary = clean.rfind(". ", start, end)
            if boundary > start + size // 2:
                end = boundary + 1
        chunks.append(clean[start:end])
        if end == len(clean):
            break
        start = max(start + 1, end - overlap)
    return chunks

