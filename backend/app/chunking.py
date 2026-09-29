import re

STEP = re.compile(r"^\s*(?:\d+[.)]\s+|[-•]\s+)")


def _split(text, size, overlap):
    start = 0
    while start < len(text):
        end = min(start + size, len(text))
        if end < len(text):
            boundary = max(
                text.rfind("\n", start + size // 2, end),
                text.rfind(". ", start + size // 2, end),
                text.rfind(" ", start + size // 2, end),
            )
            if boundary > start:
                end = boundary + 1
        yield text[start:end].strip()
        if end == len(text):
            break
        start = max(start + 1, end - overlap)


def chunk_text(text: str, size: int = 1400, overlap: int = 250) -> list[str]:
    """Keep page-local paragraphs/steps and repeat short headings as context.

    Overlap applies only inside an oversized paragraph or step, never across pages
    or distinct numbered steps. Newlines remain visible to both readers and models.
    """
    if size <= 0 or not 0 <= overlap < size:
        raise ValueError("Invalid chunk size/overlap")
    lines = [re.sub(r"[^\S\n]+", " ", line).strip() for line in text.splitlines()]
    blocks, current = [], []
    for line in lines:
        if not line or STEP.match(line):
            if current:
                blocks.append("\n".join(current))
            current = []
        if line:
            current.append(line)
    if current:
        blocks.append("\n".join(current))
    result, heading, heading_used = [], "", False
    for block in blocks:
        first, _, rest = block.partition("\n")
        # Conservative heading heuristic; PDF block boundaries supply blank lines.
        is_heading = (
            len(first) <= min(90, size // 3)
            and not STEP.match(first)
            and not re.search(r"[.,;!?]$", first)
        )
        if is_heading:
            if heading and not heading_used:
                result.append(heading)
            heading, heading_used = first, False
            if not rest:
                continue
            block = rest
        prefix = heading + "\n\n" if heading else ""
        capacity = size - len(prefix)
        for part in _split(block, capacity, min(overlap, capacity // 4)):
            if part:
                result.append(prefix + part)
                heading_used = True
    if heading and not heading_used:
        result.append(heading)
    return result
