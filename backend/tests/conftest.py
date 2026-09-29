from test_indexing import env  # noqa: F401


def authenticated_app(kb):
    from app.main import create_app
    from app.indexing import document_id
    from test_security import PASSWORD, sign_in, pdf_bytes
    from pathlib import Path

    app = create_app(kb.settings, kb=kb)
    app.state.security.create_user("admin", PASSWORD, "admin", bootstrap=True)
    _, manifests = kb.store.read()
    for path in kb.source.inventory():
        target = Path(kb.settings.documents_path) / path
        data = (
            target.read_bytes()
            if target.is_file()
            else pdf_bytes("Synthetic API fixture.")
        )
        app.state.documents.record(path, data, actor=None)
        m = manifests.get(path)
        if m:
            with app.state.security.transaction() as db:
                db.execute(
                    "UPDATE documents SET indexed_hash=current_hash,revision=? WHERE id=?",
                    (m["revision"], document_id(path)),
                )
    return app, sign_in(app)
