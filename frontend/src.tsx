import { FormEvent, useCallback, useEffect, useRef, useState } from "react";
import { createRoot } from "react-dom/client";
import "./styles.css";
import { Documents as LibraryDocuments } from "./Documents";
import "./library.css";
import { SourcePreview, Source } from "./SourcePreview";
import {
  Availability,
  initialAvailability,
  Mode,
  ModeSelector,
  ResponseCard,
  Result,
} from "./ResponseModes";

type User = {
  id: number;
  username: string;
  role: string;
  active: boolean;
  groups: number[];
};
type Group = { id: number; name: string };
type Doc = {
  id: string;
  path: string;
  status: string;
  size: number;
  pages: number;
  groups: number[];
  current_hash: string;
  indexed_at: string | null;
};
type Session = { user: User | null; csrf: string; access_version: number };
type API = <T>(path: string, method?: string, body?: unknown) => Promise<T>;
type Run = (fn: () => Promise<void>) => Promise<void>;
type Panel = { api: API; run: Run; busy: boolean };
const roles: Record<string, string> = {
  reader: "Lecteur",
  manager: "Gestionnaire documentaire",
  admin: "Administrateur",
};
const message = (e: unknown) =>
  e instanceof Error ? e.message : "Une erreur est survenue.";
const date = (s: string | null) =>
  s ? new Date(s).toLocaleString("fr-CA") : "Jamais";
function Groups({
  groups,
  value,
  set,
}: {
  groups: Group[];
  value: number[];
  set: (v: number[]) => void;
}) {
  return (
    <fieldset>
      <legend>Groupes autorisés</legend>
      {groups.length ? (
        groups.map((g) => (
          <label className="check" key={g.id}>
            <input
              type="checkbox"
              checked={value.includes(g.id)}
              onChange={(e) =>
                set(
                  e.target.checked
                    ? [...value, g.id]
                    : value.filter((id) => id !== g.id),
                )
              }
            />
            {g.name}
          </label>
        ))
      ) : (
        <p>Aucun groupe disponible.</p>
      )}
    </fieldset>
  );
}
function App() {
  const [session, setSession] = useState<Session | null>(null);
  const ref = useRef<Session | null>(null);
  const [ready, setReady] = useState(false),
    [tab, setTab] = useState("ask"),
    [error, setError] = useState(""),
    [notice, setNotice] = useState(""),
    [busy, setBusy] = useState(false);
  const [result, setResult] = useState<Result | null>(null),
    [revision, setRevision] = useState(0);
  const [deeper, setDeeper] = useState<Result | null>(null);
  const [resultQuestion, setResultQuestion] = useState("");
  const [mode, setMode] = useState<Mode>("fast");
  const [availability, setAvailability] =
    useState<Availability>(initialAvailability);
  const [loadingMode, setLoadingMode] = useState<Mode | null>(null);
  const requestInFlight = useRef(false);
  const [preview, setPreview] = useState<Source | null>(null);
  const epoch = useRef(0);
  const applySession = useCallback((next: Session) => {
    if (
      ref.current?.user?.id !== next.user?.id ||
      ref.current?.access_version !== next.access_version
    ) {
      epoch.current++;
      setResult(null);
      setDeeper(null);
      setPreview(null);
      setRevision((r) => r + 1);
    }
    ref.current = next;
    setSession(next);
  }, []);
  const api: API = useCallback(
    async <T,>(path: string, method = "GET", body?: unknown): Promise<T> => {
      const form = body instanceof FormData;
      const headers: Record<string, string> = {};
      if (method !== "GET") headers["X-CSRF-Token"] = ref.current?.csrf ?? "";
      if (body && !form) headers["Content-Type"] = "application/json";
      const response = await fetch(path, {
        method,
        credentials: "same-origin",
        cache: "no-store",
        headers,
        body: body ? (form ? body : JSON.stringify(body)) : undefined,
      });
      const data = await response.json();
      if (!response.ok) {
        if (response.status === 401) {
          epoch.current++;
          setResult(null);
          setDeeper(null);
          setPreview(null);
          ref.current = ref.current ? { ...ref.current, user: null } : null;
          setSession(ref.current);
        }
        throw new Error(
          typeof data.detail === "string"
            ? data.detail
            : "Demande invalide. Vérifiez les champs.",
        );
      }
      return data;
    },
    [],
  );
  const refresh = useCallback(async () => {
    try {
      applySession(await api<Session>("/api/auth/session"));
    } catch (e) {
      setError(message(e));
    } finally {
      setReady(true);
    }
  }, [api, applySession]);
  useEffect(() => {
    void refresh();
    const timer = setInterval(() => void refresh(), 15000);
    return () => clearInterval(timer);
  }, [refresh]);
  const run: Run = useCallback(async (fn) => {
    setError("");
    setNotice("");
    setBusy(true);
    try {
      await fn();
    } catch (e) {
      setError(message(e));
    } finally {
      setBusy(false);
    }
  }, []);
  async function login(e: FormEvent<HTMLFormElement>) {
    e.preventDefault();
    const form = e.currentTarget;
    const data = Object.fromEntries(new FormData(form));
    await run(async () => {
      applySession(await api<Session>("/api/auth/login", "POST", data));
      setTab("ask");
      form.reset();
    });
  }
  async function logout() {
    await run(async () => {
      await api("/api/auth/logout", "POST");
      epoch.current++;
      setResult(null);
      setDeeper(null);
      setPreview(null);
      setSession(null);
      ref.current = null;
      await refresh();
    });
  }
  useEffect(() => {
    if (!session?.user) {
      setAvailability(initialAvailability);
      setMode("fast");
      return;
    }
    let cancelled = false;
    void api<{ modes: Availability }>("/api/response-modes")
      .then((data) => {
        if (!cancelled) setAvailability(data.modes);
      })
      .catch(() => {
        if (!cancelled)
          setAvailability({
            fast: {
              available: false,
              reason:
                "Modèle indisponible. Rechargez la page après vérification.",
            },
            reflection: {
              available: false,
              reason:
                "Modèle indisponible. Rechargez la page après vérification.",
            },
            search: { available: true, reason: "" },
          });
      });
    return () => {
      cancelled = true;
    };
  }, [api, session?.user?.id]);
  async function submitQuestion(
    question: string,
    requestedMode: Mode,
    deepen = false,
  ) {
    if (requestInFlight.current || !availability[requestedMode].available)
      return;
    requestInFlight.current = true;
    const ticket = epoch.current;
    setLoadingMode(requestedMode);
    try {
      await run(async () => {
        if (!deepen) {
          setResult(null);
          setDeeper(null);
        }
        setPreview(null);
        let r: Result;
        try {
          r = await api<Result>("/api/ask", "POST", {
            question,
            mode: requestedMode,
          });
        } catch (error) {
          // A model may reject a previously detected mode after being replaced.
          // Refresh cached capability state; never retry generation automatically.
          try {
            const data = await api<{ modes: Availability }>(
              "/api/response-modes",
            );
            if (ticket === epoch.current) setAvailability(data.modes);
          } catch {
            /* Preserve the original request error. */
          }
          throw error;
        }
        if (ticket === epoch.current) {
          if (deepen) setDeeper(r);
          else {
            setResult(r);
            setResultQuestion(question);
          }
        }
      });
    } finally {
      requestInFlight.current = false;
      setLoadingMode(null);
    }
  }
  async function ask(e: FormEvent<HTMLFormElement>) {
    e.preventDefault();
    await submitQuestion(
      String(new FormData(e.currentTarget).get("question")),
      mode,
    );
  }
  const user = session?.user;
  return (
    <main>
      <header>
        <span className="mark">I</span>
        <div>
          <h1>Instruct IA</h1>
          <p>Vos instructions. Sur votre réseau.</p>
        </div>
        {user && (
          <div className="identity">
            <span>
              {user.username} · {roles[user.role]}
            </span>
            <button className="secondary" onClick={logout} disabled={busy}>
              Déconnexion
            </button>
          </div>
        )}
      </header>
      {error && (
        <p className="error" role="alert">
          {error}
        </p>
      )}
      {notice && (
        <p className="notice" role="status">
          {notice}
        </p>
      )}
      {!ready ? (
        <p>Chargement…</p>
      ) : !user ? (
        <section className="login panel">
          <p className="eyebrow">ESPACE DE TRAVAIL LOCAL</p>
          <h2>Connexion</h2>
          <p>Accédez aux documents autorisés pour votre équipe.</p>
          <form onSubmit={login}>
            <label>
              Identifiant
              <input
                name="username"
                autoComplete="username"
                minLength={3}
                maxLength={64}
                required
              />
            </label>
            <label>
              Mot de passe
              <input
                name="password"
                type="password"
                autoComplete="current-password"
                maxLength={256}
                required
              />
            </label>
            <button disabled={busy}>
              {busy ? "Connexion…" : "Se connecter"}
            </button>
          </form>
          <p className="muted">
            Pour obtenir un compte ou récupérer votre accès, contactez votre
            administrateur.
          </p>
        </section>
      ) : (
        <>
          {preview && (
            <div className="library">
              <SourcePreview
                source={preview}
                onClose={() => setPreview(null)}
              />
            </div>
          )}
          <nav aria-label="Sections">
            {[
              ["ask", "Recherche"],
              ["documents", "Documents"],
              ["account", "Mon compte"],
              ...(user.role === "admin"
                ? [
                    ["admin", "Administration"],
                    ["library", "Gestion avancée"],
                  ]
                : []),
            ].map(([key, label]) => (
              <button
                key={key}
                className={tab === key ? "selected" : "secondary"}
                onClick={() => {
                  setTab(key);
                  setError("");
                  setNotice("");
                }}
              >
                {label}
              </button>
            ))}
          </nav>
          {tab === "ask" && (
            <>
              <section className="hero">
                <p className="eyebrow">VOTRE PÉRIMÈTRE DOCUMENTAIRE</p>
                <h2>Comment puis-je vous aider?</h2>
                <form onSubmit={ask}>
                  <label className="sr-only" htmlFor="question">
                    Votre question
                  </label>
                  <textarea
                    id="question"
                    name="question"
                    placeholder="Ex. Quelle est la procédure de démarrage de la calandre?"
                    minLength={3}
                    maxLength={1000}
                    required
                  />
                  <ModeSelector
                    value={mode}
                    onChange={setMode}
                    availability={availability}
                    busy={busy || loadingMode !== null}
                  />
                  <button
                    disabled={
                      busy ||
                      loadingMode !== null ||
                      !availability[mode].available
                    }
                  >
                    Rechercher
                  </button>
                </form>
              </section>
              <div role="status" aria-live="polite" className="response-status">
                {loadingMode === "reflection"
                  ? "Réflexion en cours…"
                  : loadingMode === "fast"
                    ? "Réponse en cours…"
                    : loadingMode === "search"
                      ? "Recherche documentaire en cours…"
                      : ""}
              </div>
              {result && (
                <>
                  <ResponseCard
                    result={result}
                    question={resultQuestion}
                    onPreview={setPreview}
                  />
                  {result.mode === "fast" &&
                    result.kind === "answer" &&
                    availability.reflection.available &&
                    !deeper && (
                      <div className="deepen-actions">
                        <button
                          className="secondary"
                          disabled={busy || loadingMode !== null}
                          onClick={() =>
                            void submitQuestion(
                              resultQuestion,
                              "reflection",
                              true,
                            )
                          }
                        >
                          Approfondir
                        </button>
                        <p className="muted">
                          Relance la recherche pour cette question avec vos
                          droits actuels, puis examine les passages en mode
                          Réflexion.
                        </p>
                      </div>
                    )}
                </>
              )}
              {deeper && (
                <ResponseCard
                  result={deeper}
                  question={resultQuestion}
                  deeper
                  onPreview={setPreview}
                />
              )}
            </>
          )}
          {tab === "library" && user.role === "admin" && (
            <div className="library">
              <LibraryDocuments
                onChange={() => {
                  setRevision((r) => r + 1);
                  setResult(null);
                  setDeeper(null);
                  setPreview(null);
                }}
              />
            </div>
          )}
          {tab === "documents" && (
            <Documents
              key={`${user.id}-${revision}`}
              api={api}
              user={user}
              run={run}
              busy={busy}
            />
          )}
          {tab === "account" && (
            <section className="panel">
              <h2>Mon compte</h2>
              <p>Le changement de mot de passe ferme toutes vos sessions.</p>
              <form
                className="stack"
                onSubmit={(e) => {
                  e.preventDefault();
                  const form = e.currentTarget;
                  const data = Object.fromEntries(new FormData(form));
                  void run(async () => {
                    await api("/api/auth/password", "POST", data);
                    form.reset();
                    await refresh();
                    setNotice("Mot de passe changé. Reconnectez-vous.");
                  });
                }}
              >
                <label>
                  Mot de passe actuel
                  <input
                    name="current"
                    type="password"
                    autoComplete="current-password"
                    maxLength={256}
                    required
                  />
                </label>
                <label>
                  Nouveau mot de passe
                  <input
                    name="password"
                    type="password"
                    autoComplete="new-password"
                    minLength={12}
                    maxLength={256}
                    required
                  />
                </label>
                <button disabled={busy}>Changer le mot de passe</button>
              </form>
            </section>
          )}
          {tab === "admin" && user.role === "admin" && (
            <Administration api={api} run={run} busy={busy} />
          )}
        </>
      )}
      <footer>
        Vérifiez toujours la version officielle de l’instruction avant
        d’exécuter une procédure.
      </footer>
    </main>
  );
}
function Documents({ api, user, run, busy }: Panel & { user: User }) {
  const [docs, setDocs] = useState<Doc[]>([]),
    [groups, setGroups] = useState<Group[]>([]),
    [selected, setSelected] = useState<number[]>([]),
    [loading, setLoading] = useState(true);
  const load = useCallback(async () => {
    const [d, g] = await Promise.all([
      api<Doc[]>("/api/documents"),
      api<Group[]>("/api/groups"),
    ]);
    setDocs(d);
    setGroups(g);
    setLoading(false);
  }, [api]);
  useEffect(() => {
    void run(load);
  }, [load, run]);
  return (
    <section>
      <div className="section-title">
        <div>
          <h2>Documents</h2>
          <p>Documents accessibles à votre compte.</p>
        </div>
        {user.role === "admin" && (
          <button
            disabled={busy}
            onClick={() =>
              run(async () => {
                await api("/api/ingest", "POST");
                await load();
              })
            }
          >
            Synchroniser le dossier local
          </button>
        )}
      </div>
      {user.role !== "reader" && (
        <details className="panel">
          <summary>Ajouter ou remplacer un PDF</summary>
          <form
            className="stack"
            onSubmit={(e) => {
              e.preventDefault();
              const form = e.currentTarget;
              const data = new FormData(form);
              selected.forEach((g) => data.append("groups", String(g)));
              void run(async () => {
                await api("/api/documents", "POST", data);
                form.reset();
                await load();
              });
            }}
          >
            <label>
              Chemin relatif, avec le nom du PDF
              <input
                name="path"
                placeholder="maintenance/calandre.pdf"
                required
              />
            </label>
            <label>
              Fichier PDF
              <input
                name="file"
                type="file"
                accept="application/pdf,.pdf"
                required
              />
            </label>
            <Groups groups={groups} value={selected} set={setSelected} />
            <p className="muted">
              Un nouveau document exige au moins un groupe. Un remplacement
              conserve les droits existants. Lancez ensuite l’indexation.
            </p>
            <button disabled={busy}>Enregistrer le PDF</button>
          </form>
        </details>
      )}
      {loading ? (
        <p>Chargement des documents…</p>
      ) : !docs.length ? (
        <p className="panel">
          Aucun document autorisé. L’administrateur doit attribuer les droits
          des documents importés.
        </p>
      ) : (
        docs.map((d) => (
          <article className="panel" key={d.id}>
            <div className="section-title">
              <h3>{d.path}</h3>
              <span className="badge">{d.status}</span>
            </div>
            <p className="muted">
              {d.pages} pages · {Math.ceil(d.size / 1024)} Ko · Indexation :{" "}
              {date(d.indexed_at)}
            </p>
            <div className="actions">
              <a
                href={`/api/documents/${d.id}/file`}
                target="_blank"
                rel="noreferrer"
              >
                Ouvrir le PDF
              </a>
              {user.role !== "reader" && (
                <>
                  <button
                    disabled={busy}
                    onClick={() =>
                      run(async () => {
                        await api(`/api/documents/${d.id}/sync`, "POST");
                        await load();
                      })
                    }
                  >
                    Indexer
                  </button>
                  <button
                    className="danger"
                    disabled={busy}
                    onClick={() => {
                      if (
                        confirm(
                          `Retirer ${d.path} de la recherche et de la consultation?`,
                        )
                      )
                        void run(async () => {
                          await api(`/api/documents/${d.id}`, "DELETE");
                          await load();
                        });
                    }}
                  >
                    Retirer
                  </button>
                </>
              )}
            </div>
            <details>
              <summary>Détails et versions</summary>
              <p className="hash">SHA-256 : {d.current_hash}</p>
              <Versions ident={d.id} api={api} run={run} />
            </details>
            {user.role === "admin" && (
              <DocumentGrants
                doc={d}
                groups={groups}
                api={api}
                run={run}
                busy={busy}
                reload={load}
              />
            )}
          </article>
        ))
      )}
    </section>
  );
}
function Versions({ api, ident, run }: { api: API; ident: string; run: Run }) {
  const [items, setItems] = useState<{ hash: string; created_at: string }[]>(
    [],
  );
  return (
    <div>
      <button
        className="secondary"
        onClick={() =>
          run(async () =>
            setItems(await api(`/api/documents/${ident}/versions`)),
          )
        }
      >
        Charger les versions conservées
      </button>
      {items.map((v) => (
        <p key={v.hash}>
          <a
            href={`/api/documents/${ident}/file?version=${v.hash}`}
            target="_blank"
            rel="noreferrer"
          >
            {date(v.created_at)} · {v.hash.slice(0, 12)}
          </a>
        </p>
      ))}
    </div>
  );
}
function DocumentGrants({
  doc,
  groups,
  api,
  run,
  busy,
  reload,
}: Panel & { doc: Doc; groups: Group[]; reload: () => Promise<void> }) {
  const [value, setValue] = useState(doc.groups);
  return (
    <details>
      <summary>Autorisations</summary>
      <Groups groups={groups} value={value} set={setValue} />
      <button
        disabled={busy}
        onClick={() =>
          run(async () => {
            await api(`/api/admin/documents/${doc.id}/groups`, "PUT", {
              groups: value,
            });
            await reload();
          })
        }
      >
        Enregistrer les droits
      </button>
    </details>
  );
}
function Administration({ api, run, busy }: Panel) {
  const [users, setUsers] = useState<User[]>([]),
    [groups, setGroups] = useState<Group[]>([]),
    [selected, setSelected] = useState<number[]>([]),
    [retention, setRetention] = useState(90),
    [offset, setOffset] = useState(0);
  const [audit, setAudit] = useState<{
    total: number;
    items: {
      id: number;
      at: string;
      actor: number | null;
      action: string;
      resource: string;
      result: string;
    }[];
  }>({ total: 0, items: [] });
  const load = useCallback(async () => {
    const [u, g, c] = await Promise.all([
      api<User[]>("/api/admin/users"),
      api<Group[]>("/api/groups"),
      api<{ audit_retention_days: number }>("/api/admin/configuration"),
    ]);
    setUsers(u);
    setGroups(g);
    setRetention(c.audit_retention_days);
  }, [api]);
  useEffect(() => {
    void run(load);
  }, [load, run]);
  useEffect(() => {
    void run(async () =>
      setAudit(await api(`/api/admin/audit?offset=${offset}&limit=25`)),
    );
  }, [api, offset, run]);
  return (
    <section>
      <h2>Administration</h2>
      <div className="admin-grid">
        <section className="panel">
          <h3>Créer un compte</h3>
          <form
            className="stack"
            onSubmit={(e) => {
              e.preventDefault();
              const form = e.currentTarget;
              const data = Object.fromEntries(new FormData(form));
              void run(async () => {
                await api("/api/admin/users", "POST", {
                  ...data,
                  groups: selected,
                });
                form.reset();
                setSelected([]);
                await load();
              });
            }}
          >
            <label>
              Identifiant
              <input
                name="username"
                autoComplete="off"
                minLength={3}
                maxLength={64}
                pattern="[a-z0-9][a-z0-9._-]{2,63}"
                required
              />
            </label>
            <label>
              Mot de passe initial
              <input
                name="password"
                type="password"
                autoComplete="new-password"
                minLength={12}
                maxLength={256}
                required
              />
            </label>
            <label>
              Rôle
              <select name="role">
                {Object.entries(roles).map(([key, label]) => (
                  <option key={key} value={key}>
                    {label}
                  </option>
                ))}
              </select>
            </label>
            <Groups groups={groups} value={selected} set={setSelected} />
            <button disabled={busy}>Créer le compte</button>
          </form>
        </section>
        <section className="panel">
          <h3>Groupes</h3>
          <form
            className="stack"
            onSubmit={(e) => {
              e.preventDefault();
              const form = e.currentTarget;
              const name = new FormData(form).get("name");
              void run(async () => {
                await api("/api/admin/groups", "POST", { name });
                form.reset();
                await load();
              });
            }}
          >
            <label>
              Nom du groupe
              <input name="name" maxLength={80} required />
            </label>
            <button disabled={busy}>Créer le groupe</button>
          </form>
          {groups.map((g) => (
            <form
              key={g.id}
              className="group-row"
              onSubmit={(e) => {
                e.preventDefault();
                const name = new FormData(e.currentTarget).get("name");
                void run(async () => {
                  await api(`/api/admin/groups/${g.id}`, "PUT", { name });
                  await load();
                });
              }}
            >
              <label className="sr-only" htmlFor={`group-${g.id}`}>
                Nom du groupe {g.name}
              </label>
              <input
                id={`group-${g.id}`}
                name="name"
                defaultValue={g.name}
                required
              />
              <button className="secondary" disabled={busy}>
                Renommer
              </button>
              <button
                type="button"
                className="danger"
                disabled={busy}
                onClick={() => {
                  if (
                    confirm(
                      `Supprimer le groupe ${g.name} et ses autorisations?`,
                    )
                  )
                    void run(async () => {
                      await api(`/api/admin/groups/${g.id}`, "DELETE");
                      await load();
                    });
                }}
              >
                Supprimer
              </button>
            </form>
          ))}
        </section>
      </div>
      <section className="panel">
        <h3>Comptes existants</h3>
        {users.map((u) => (
          <UserEditor
            key={`${u.id}-${JSON.stringify(u)}`}
            user={u}
            groups={groups}
            api={api}
            run={run}
            busy={busy}
            reload={load}
          />
        ))}
      </section>
      <section className="panel">
        <h3>Journal d’audit</h3>
        <form
          className="actions"
          onSubmit={(e) => {
            e.preventDefault();
            void run(async () => {
              await api("/api/admin/configuration", "PUT", {
                audit_retention_days: retention,
              });
              setAudit(await api(`/api/admin/audit?offset=${offset}&limit=25`));
            });
          }}
        >
          <label>
            Conservation (jours)
            <input
              type="number"
              value={retention}
              onChange={(e) => setRetention(Number(e.target.value))}
              min={1}
              max={3650}
              required
            />
          </label>
          <button disabled={busy}>Enregistrer la conservation</button>
        </form>
        <div className="table-scroll">
          <table>
            <thead>
              <tr>
                <th>Date</th>
                <th>Acteur</th>
                <th>Action</th>
                <th>Ressource</th>
                <th>Résultat</th>
              </tr>
            </thead>
            <tbody>
              {audit.items.map((a) => (
                <tr key={a.id}>
                  <td>{date(a.at)}</td>
                  <td>{a.actor ?? "Local / inconnu"}</td>
                  <td>{a.action}</td>
                  <td className="hash">{a.resource}</td>
                  <td>{a.result}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
        <div className="actions">
          <button
            className="secondary"
            disabled={busy || offset === 0}
            onClick={() => setOffset(Math.max(0, offset - 25))}
          >
            Précédent
          </button>
          <span>
            {audit.total ? offset + 1 : 0}–{Math.min(offset + 25, audit.total)}{" "}
            / {audit.total}
          </span>
          <button
            className="secondary"
            disabled={busy || offset + 25 >= audit.total}
            onClick={() => setOffset(offset + 25)}
          >
            Suivant
          </button>
          <button
            className="secondary"
            disabled={busy}
            onClick={() =>
              run(async () =>
                setAudit(
                  await api(`/api/admin/audit?offset=${offset}&limit=25`),
                ),
              )
            }
          >
            Actualiser le journal
          </button>
        </div>
      </section>
    </section>
  );
}
function UserEditor({
  user,
  groups,
  api,
  run,
  busy,
  reload,
}: Panel & { user: User; groups: Group[]; reload: () => Promise<void> }) {
  const [role, setRole] = useState(user.role),
    [active, setActive] = useState(!!user.active),
    [value, setValue] = useState(user.groups);
  return (
    <details>
      <summary>
        {user.username} · {roles[user.role]} ·{" "}
        {user.active ? "Actif" : "Désactivé"}
      </summary>
      <div className="stack">
        <label>
          Rôle
          <select value={role} onChange={(e) => setRole(e.target.value)}>
            {Object.entries(roles).map(([key, label]) => (
              <option key={key} value={key}>
                {label}
              </option>
            ))}
          </select>
        </label>
        <label className="check">
          <input
            type="checkbox"
            checked={active}
            onChange={(e) => setActive(e.target.checked)}
          />
          Compte actif
        </label>
        <Groups groups={groups} value={value} set={setValue} />
        <button
          disabled={busy}
          onClick={() =>
            run(async () => {
              await api(`/api/admin/users/${user.id}`, "PUT", {
                role,
                active,
                groups: value,
              });
              await reload();
            })
          }
        >
          Enregistrer et fermer ses sessions
        </button>
        <form
          className="stack"
          onSubmit={(e) => {
            e.preventDefault();
            const form = e.currentTarget;
            const password = new FormData(form).get("password");
            void run(async () => {
              await api(`/api/admin/users/${user.id}/password`, "POST", {
                password,
              });
              form.reset();
            });
          }}
        >
          <label>
            Nouveau mot de passe
            <input
              name="password"
              type="password"
              autoComplete="new-password"
              minLength={12}
              maxLength={256}
              required
            />
          </label>
          <button className="secondary" disabled={busy}>
            Réinitialiser et fermer ses sessions
          </button>
        </form>
      </div>
    </details>
  );
}
createRoot(document.getElementById("root")!).render(<App />);
