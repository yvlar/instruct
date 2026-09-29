import { useEffect, useRef, useState } from "react";
import { API, message, request } from "./api";

export type Source = {
  source_id?: string;
  passage_id?: string;
  revision?: string;
  fingerprint?: string;
  document: string;
  page: number;
  excerpt: string;
  score: number;
  document_id?: string;
  version?: string;
};

export function SourcePreview({
  source,
  onClose,
}: {
  source: Source;
  onClose: () => void;
}) {
  const dialog = useRef<HTMLDialogElement>(null);
  const [url, setUrl] = useState("");
  const [error, setError] = useState("");
  useEffect(() => {
    dialog.current?.showModal();
    const controller = new AbortController();
    if (!source.document_id || !source.version)
      setError(
        "Cette ancienne source ne contient pas de version vérifiable. Synchronisez les documents puis relancez la question.",
      );
    else
      request<{ url: string }>(
        `/api/documents/${source.document_id}/source?version=${encodeURIComponent(source.version)}&page=${source.page}`,
        { signal: controller.signal },
      )
        .then((data) => setUrl(`${API}${data.url}`))
        .catch((e) => {
          if (!controller.signal.aborted) setError(message(e));
        });
    return () => controller.abort();
  }, [source]);
  return (
    <dialog
      ref={dialog}
      className="source-dialog"
      onCancel={onClose}
      aria-labelledby="source-title"
    >
      <div className="dialog-header">
        <div>
          <p className="eyebrow">SOURCE DOCUMENTAIRE</p>
          <h2 id="source-title">{source.document}</h2>
        </div>
        <button className="secondary" onClick={onClose}>
          Fermer
        </button>
      </div>
      <p>
        <strong>Page {source.page}</strong> · Version technique exacte de la
        source
      </p>
      <p className="muted">
        Le lecteur PDF peut ignorer le numéro de page sur mobile. Ouvrez ou
        téléchargez le PDF et consultez la page {source.page}.
      </p>
      {error ? (
        <p className="notice error" role="alert">
          {error}
        </p>
      ) : url ? (
        <>
          <div className="actions">
            <a
              className="button"
              href={`${url}#page=${source.page}`}
              target="_blank"
              rel="noreferrer"
            >
              Ouvrir le PDF · page {source.page}
            </a>
            <a className="button secondary" href={`${url}&download=true`}>
              Télécharger le PDF
            </a>
          </div>
          <iframe
            title={`PDF ${source.document}, page ${source.page}`}
            src={`${url}#page=${source.page}`}
          />
        </>
      ) : (
        <p role="status">Vérification de la version…</p>
      )}
    </dialog>
  );
}
