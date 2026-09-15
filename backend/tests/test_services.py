from app.services import chunk_text


def test_empty_text():
    assert chunk_text("  \n ") == []


def test_chunks_keep_content():
    text = "Première étape. " * 200
    chunks = chunk_text(text, size=100, overlap=10)
    assert len(chunks) > 1
    assert all(chunk for chunk in chunks)

