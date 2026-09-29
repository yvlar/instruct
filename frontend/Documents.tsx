import { ChangeEvent, FormEvent, useEffect, useRef, useState } from "react";
import { message, request } from "./api";
import { SourcePreview, Source } from "./SourcePreview";

type State = "pending" | "running" | "available" | "modified" | "error";
type Document = {
  id: string;
  document: string;
  name: string;
  folder: string;
  size: number | null;
  pages: number | null;
  indexed_at: string | null;
  file_hash: string | null;
  version: string | null;
  state: State;
  error: string | null;
  retired: boolean;
  replacement_pending: boolean;
};
type Job = {
  id: string;
  kind: string;
  document: string | null;
  state: string;
  stage: string;
  processed: number;
  total: number | null;
  current_document: string | null;
  error: string | null;
};
type Listing = {
  items: Document[];
  total: number;
  page: number;
  page_size: number;
  jobs: Job[];
  max_pdf_bytes: number;
};
const labels: Record<State, string> = {
  pending: "À indexer",
  running: "En cours",
  available: "Disponible",
  modified: "Modification détectée",
  error: "Erreur",
};
const date = (value: string | null) =>
  value
    ? new Date(value).toLocaleString("fr-CA", {
        dateStyle: "medium",
        timeStyle: "short",
      })
    : "Pas encore indexé";
const bytes = (value: number | null) =>
  value === null
    ? "Taille inconnue"
    : value < 1024 * 1024
      ? `${Math.ceil(value / 1024)} Ko`
      : `${(value / (1024 * 1024)).toFixed(1)} Mo`;

export function Documents({ onChange }: { onChange: () => void }) {
  const [data, setData] = useState<Listing | null>(null);
  const [query, setQuery] = useState("");
  const [status, setStatus] = useState("");
  const [page, setPage] = useState(1);
  const [refresh, setRefresh] = useState(0);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const [busy, setBusy] = useState(false);
  const [file, setFile] = useState<File | null>(null);
  const [name, setName] = useState("");
  const [folder, setFolder] = useState("");
  const [replacement, setReplacement] = useState<Document | null>(null);
  const [collision, setCollision] = useState<Document | null>(null);
  const [removal, setRemoval] = useState<Document | null>(null);
  const [preview, setPreview] = useState<Source | null>(null);
  const [dragging, setDragging] = useState(false);
  const input = useRef<HTMLInputElement>(null);
  const removeDialog = useRef<HTMLDialogElement>(null);
  const uploadPanel = useRef<HTMLElement>(null);
  const active =
    data?.jobs.some((job) => ["queued", "running"].includes(job.state)) ??
    false;
  const disabled = busy || active;
  const pages = data ? Math.max(1, Math.ceil(data.total / data.page_size)) : 1;

  useEffect(() => {
    let timer: ReturnType<typeof setTimeout>;
    const controller = new AbortController();
    async function load() {
      try {
        const next = await request<Listing>(
          `/api/library/documents?q=${encodeURIComponent(query)}&status=${status}&page=${page}`,
          { signal: controller.signal },
        );
        if (!controller.signal.aborted) {
          setData(next);
          setPage((p) =>
            Math.min(p, Math.max(1, Math.ceil(next.total / next.page_size))),
          );
          setError("");
        }
      } catch (e) {
        if (!controller.signal.aborted) setError(message(e));
      }
      if (!controller.signal.aborted) timer = setTimeout(load, 2000);
    }
    void load();
    return () => {
      controller.abort();
      clearTimeout(timer);
    };
  }, [query, status, page, refresh]);
  useEffect(() => {
    if (removal) removeDialog.current?.showModal();
  }, [removal]);

  function choose(selected: File | undefined) {
    if (!selected) return;
    setFile(selected);
    setName(replacement?.name ?? selected.name);
    setNotice("");
    setCollision(null);
  }
  function resetUpload() {
    setFile(null);
    setName("");
    setReplacement(null);
    setCollision(null);
    if (input.current) input.current.value = "";
  }
  async function action(path: string) {
    setBusy(true);
    setNotice("");
    try {
      await request(path, { method: "POST" });
      onChange();
      setRefresh((v) => v + 1);
      setRemoval(null);
    } catch (e) {
      setNotice(message(e));
    } finally {
      setBusy(false);
    }
  }
  async function upload(event: FormEvent) {
    event.preventDefault();
    if (!file) return;
    setBusy(true);
    setNotice("");
    setCollision(null);
    try {
      const params = new URLSearchParams({ name, folder });
      if (replacement) params.set("replace_id", replacement.id);
      await request(`/api/library/documents?${params}`, {
        method: "PUT",
        headers: { "Content-Type": "application/pdf" },
        body: file,
      });
      setNotice(
        replacement
          ? "Remplacement reçu. Lancez l’indexation pour le valider. La version précédente reste conservée."
          : "PDF ajouté. Lancez son indexation pour le rendre disponible à la recherche.",
      );
      resetUpload();
      onChange();
      setRefresh((v) => v + 1);
      setPage(1);
    } catch (e) {
      setNotice(message(e));
      if (message(e).includes("Ce nom existe")) {
        try {
          const found = await request<Listing>(
            `/api/library/documents?q=${encodeURIComponent(name)}&page_size=100`,
          );
          setCollision(
            found.items.find(
              (d) => d.document === (folder ? `${folder}/${name}` : name),
            ) ?? null,
          );
        } catch {
          /* existing error remains visible */
        }
      }
    } finally {
      setBusy(false);
    }
  }
  function beginReplacement(doc: Document) {
    setReplacement(doc);
    setName(doc.name);
    setFolder(doc.folder);
    setFile(null);
    setCollision(null);
    setNotice("");
    uploadPanel.current?.scrollIntoView({
      behavior: "smooth",
      block: "center",
    });
    input.current?.click();
  }
  return (
    <>
      <section className="page-heading">
        <div>
          <p className="eyebrow">VOTRE BASE DE CONNAISSANCES</p>
          <h2>Documents</h2>
          <p className="muted">
            Ajoutez vos PDF et gardez les instructions de recherche à jour.
          </p>
        </div>
        <button
          className="secondary"
          disabled={disabled}
          onClick={() => void action("/api/library/documents/sync")}
        >
          ↻ Synchroniser les documents
        </button>
      </section>
      <section
        ref={uploadPanel}
        className={`upload-panel ${dragging ? "dragging" : ""}`}
        onDragOver={(e) => {
          e.preventDefault();
          if (!disabled) setDragging(true);
        }}
        onDragLeave={() => setDragging(false)}
        onDrop={(e) => {
          e.preventDefault();
          setDragging(false);
          if (!disabled) choose(e.dataTransfer.files[0]);
        }}
        aria-label="Ajout de PDF"
      >
        <div className="upload-icon" aria-hidden="true">
          ↑
        </div>
        <div className="upload-copy">
          <h3>
            {replacement
              ? `Remplacer ${replacement.name}`
              : "Une nouvelle instruction à partager?"}
          </h3>
          <p>
            Glissez un PDF ici ou choisissez un fichier. Un fichier à la fois,
            jusqu’à {Math.floor((data?.max_pdf_bytes ?? 52428800) / 1048576)}{" "}
            Mo.
          </p>
        </div>
        <button disabled={disabled} onClick={() => input.current?.click()}>
          {replacement ? "Choisir le nouveau PDF" : "Ajouter un PDF"}
        </button>
        <input
          ref={input}
          className="visually-hidden"
          type="file"
          accept=".pdf,application/pdf"
          aria-label="Fichier PDF"
          disabled={disabled}
          onChange={(e: ChangeEvent<HTMLInputElement>) =>
            choose(e.target.files?.[0])
          }
        />
        {(file || replacement) && (
          <form className="upload-form" onSubmit={upload}>
            {file && (
              <p className="file-summary">
                {file.name} · {bytes(file.size)}
              </p>
            )}
            <label>
              Nom du PDF
              <input
                value={name}
                disabled={!!replacement}
                required
                maxLength={120}
                onChange={(e) => setName(e.target.value)}
              />
            </label>
            <label>
              Dossier relatif
              <input
                value={folder}
                disabled={!!replacement}
                placeholder="Ex. maintenance"
                maxLength={240}
                onChange={(e) => setFolder(e.target.value)}
              />
            </label>
            {replacement && (
              <p className="muted">
                Le PDF actuel reste utilisable jusqu’à la validation de
                l’indexation du remplacement.
              </p>
            )}
            <div className="actions">
              <button disabled={!file || disabled} type="submit">
                {busy
                  ? "Envoi…"
                  : replacement
                    ? "Confirmer le remplacement"
                    : "Enregistrer le PDF"}
              </button>
              <button
                type="button"
                className="secondary"
                disabled={busy}
                onClick={resetUpload}
              >
                Annuler
              </button>
            </div>
          </form>
        )}
      </section>
      {notice && (
        <div className="notice" role="status">
          {notice}
          {collision && (
            <div className="actions">
              <button
                className="secondary"
                disabled={disabled}
                onClick={() => {
                  setReplacement(collision);
                  setCollision(null);
                }}
              >
                Remplacer le document existant
              </button>
              <span>ou changez le nom ci-dessus.</span>
            </div>
          )}
        </div>
      )}
      {data?.jobs.slice(0, 1).map((job) => (
        <section
          className={`job-panel ${job.state}`}
          key={job.id}
          aria-live="polite"
        >
          <div>
            <strong>{job.stage}</strong>
            <p>
              {job.total !== null
                ? `${job.processed} document${job.processed > 1 ? "s" : ""} traité${job.processed > 1 ? "s" : ""} sur ${job.total}`
                : "Préparation de l’inventaire"}
              {job.current_document && ` · ${job.current_document}`}
            </p>
            {job.error && <p>{job.error}</p>}
          </div>
          {["failed", "interrupted"].includes(job.state) && (
            <button
              className="secondary"
              disabled={disabled}
              onClick={() => void action(`/api/library/document-jobs/${job.id}/retry`)}
            >
              Réessayer la tâche
            </button>
          )}
        </section>
      ))}
      <section className="document-library" aria-label="Liste des documents">
        <div className="library-top">
          <h3>
            Vos documents <span className="count">{data?.total ?? "—"}</span>
          </h3>
          <span className="local-label">● Stockage local</span>
        </div>
        <div className="filters">
          <label className="search-label">
            <span className="visually-hidden">Rechercher un document</span>
            <input
              type="search"
              value={query}
              placeholder="Rechercher un nom ou un dossier…"
              onChange={(e) => {
                setQuery(e.target.value);
                setPage(1);
              }}
            />
          </label>
          <label>
            <span className="visually-hidden">Filtrer par état</span>
            <select
              value={status}
              onChange={(e) => {
                setStatus(e.target.value);
                setPage(1);
              }}
            >
              <option value="">Tous les états</option>
              {Object.entries(labels).map(([value, label]) => (
                <option value={value} key={value}>
                  {label}
                </option>
              ))}
            </select>
          </label>
        </div>
        {error && (
          <p className="notice error" role="alert">
            {error}{" "}
            <button
              className="secondary"
              onClick={() => setRefresh((v) => v + 1)}
            >
              Actualiser
            </button>
          </p>
        )}
        {!data && !error && (
          <p className="empty" role="status">
            Chargement des documents…
          </p>
        )}
        {data && !data.items.length && (
          <div className="empty">
            <h3>
              {query || status
                ? "Aucun document ne correspond"
                : "Votre base documentaire est prête à accueillir ses premiers PDF"}
            </h3>
            <p>
              {query || status
                ? "Modifiez votre recherche ou le filtre."
                : "Ajoutez un PDF, puis cliquez sur Indexer. Vos fichiers restent sur cette machine."}
            </p>
          </div>
        )}
        {!!data?.items.length && (
          <table className="documents-table">
            <thead>
              <tr>
                <th>Document</th>
                <th>État</th>
                <th>Dernière indexation</th>
                <th>Actions</th>
              </tr>
            </thead>
            <tbody>
              {data.items.map((doc) => (
                <tr key={doc.id}>
                  <td>
                    <div className="document-name">
                      <span className="pdf-icon" aria-hidden="true">
                        PDF
                      </span>
                      <div>
                        <strong>{doc.name}</strong>
                        <span className="muted folder">
                          {doc.folder || "Dossier principal"}
                        </span>
                        <span className="file-meta">
                          {bytes(doc.size)} ·{" "}
                          {doc.pages === null
                            ? "Pages inconnues"
                            : `${doc.pages} page${doc.pages > 1 ? "s" : ""}`}
                        </span>
                      </div>
                    </div>
                    <details>
                      <summary>Détails techniques</summary>
                      <p>
                        Empreinte SHA-256 du fichier :{" "}
                        <code>{doc.file_hash ?? "Inconnue"}</code>
                      </p>
                      <p>
                        Version indexée : <code>{doc.version ?? "Aucune"}</code>
                      </p>
                      <p>
                        Cette empreinte identifie le contenu du fichier. Elle ne
                        représente ni une révision officielle de procédure ni
                        une approbation métier.
                      </p>
                    </details>
                    {doc.replacement_pending && (
                      <p className="row-note">
                        Remplacement en attente de validation.
                      </p>
                    )}
                    {doc.error && <p className="row-error">{doc.error}</p>}
                  </td>
                  <td>
                    <span className={`badge ${doc.state}`}>
                      {labels[doc.state]}
                    </span>
                  </td>
                  <td className="index-date">{date(doc.indexed_at)}</td>
                  <td>
                    <div className="row-actions">
                      {!doc.retired && (
                        <>
                          <button
                            disabled={disabled}
                            onClick={() =>
                              void action(`/api/library/documents/${doc.id}/index`)
                            }
                          >
                            {doc.state === "error"
                              ? "Réessayer"
                              : doc.state === "available"
                                ? "Réindexer"
                                : "Indexer"}
                          </button>
                          <button
                            className="text-button"
                            disabled={disabled || doc.replacement_pending}
                            onClick={() => beginReplacement(doc)}
                          >
                            Remplacer
                          </button>
                          {doc.version && (
                            <button
                              className="text-button"
                              onClick={() =>
                                setPreview({
                                  document: doc.document,
                                  document_id: doc.id,
                                  version: doc.version!,
                                  page: 1,
                                  excerpt: "",
                                  score: 0,
                                })
                              }
                            >
                              Ouvrir
                            </button>
                          )}
                        </>
                      )}
                      <button
                        className="text-button danger"
                        disabled={disabled}
                        onClick={() => setRemoval(doc)}
                      >
                        {doc.retired ? "Réessayer le retrait" : "Retirer"}
                      </button>
                    </div>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
        {data && pages > 1 && (
          <nav className="pagination" aria-label="Pagination des documents">
            <button
              className="secondary"
              disabled={page === 1}
              onClick={() => setPage((p) => p - 1)}
            >
              Précédent
            </button>
            <span>
              Page {page} sur {pages}
            </span>
            <button
              className="secondary"
              disabled={page >= pages}
              onClick={() => setPage((p) => p + 1)}
            >
              Suivant
            </button>
          </nav>
        )}
      </section>
      <p className="technical-note">
        Les PDF inchangés sont ignorés pendant la synchronisation. L’indexation
        se fait sur cette machine; les questions peuvent être temporairement
        indisponibles pendant le traitement.
      </p>
      {removal && (
        <dialog
          ref={removeDialog}
          className="confirm-dialog"
          onCancel={() => setRemoval(null)}
          aria-labelledby="remove-title"
        >
          <h2 id="remove-title">Retirer ce document?</h2>
          <p>
            <strong>{removal.document}</strong>
          </p>
          <p>
            Le fichier sera conservé dans une archive locale, hors du dossier
            indexé. Ses passages seront retirés des index actifs et ne
            reviendront pas lors de la prochaine synchronisation.
          </p>
          <p>
            Les anciennes versions PDF conservées restent consultables depuis
            leurs sources. Le retrait sera confirmé seulement après la fin du
            traitement.
          </p>
          <div className="actions">
            <button
              className="danger-button"
              disabled={disabled}
              onClick={() => void action(`/api/library/documents/${removal.id}/remove`)}
            >
              Confirmer le retrait
            </button>
            <button
              className="secondary"
              disabled={busy}
              onClick={() => setRemoval(null)}
            >
              Annuler
            </button>
          </div>
          {notice && <p role="alert">{notice}</p>}
        </dialog>
      )}
      {preview && (
        <SourcePreview source={preview} onClose={() => setPreview(null)} />
      )}
    </>
  );
}
