import { Source } from "./SourcePreview";

export type Mode = "fast" | "reflection" | "search";
export type Availability = Record<Mode, { available: boolean; reason: string }>;
export type Result = {
  mode: Mode;
  kind: "answer" | "search";
  answer: string;
  grounded: boolean;
  claims: { text: string; source_ids: string[] }[];
  safety_notice: string;
  sources: (Source & { url: string })[];
};
export const modes: { value: Mode; label: string; description: string }[] = [
  {
    value: "fast",
    label: "Rapide",
    description:
      "Réponse directe à partir des documents, sans raisonnement lorsque le modèle le permet.",
  },
  {
    value: "reflection",
    label: "Réflexion",
    description:
      "Plus de temps pour examiner et rapprocher plusieurs passages. Les citations restent vérifiées.",
  },
  {
    value: "search",
    label: "Recherche seulement",
    description:
      "Consulter les passages retrouvés, sans génération de réponse.",
  },
];
export const initialAvailability: Availability = {
  fast: { available: false, reason: "Vérification du modèle local…" },
  reflection: { available: false, reason: "Vérification du modèle local…" },
  search: { available: true, reason: "" },
};

export function ModeSelector({
  value,
  onChange,
  availability,
  busy,
}: {
  value: Mode;
  onChange: (mode: Mode) => void;
  availability: Availability;
  busy: boolean;
}) {
  return (
    <fieldset className="response-modes" disabled={busy}>
      <legend>Mode de réponse</legend>
      <div className="mode-options">
        {modes.map((mode) => (
          <label
            className={`mode-option ${value === mode.value ? "active" : ""}`}
            key={mode.value}
          >
            <span className="mode-name">
              <input
                type="radio"
                name="mode"
                value={mode.value}
                checked={value === mode.value}
                disabled={!availability[mode.value].available}
                onChange={() => onChange(mode.value)}
                aria-label={mode.label}
                aria-describedby={`mode-help-${mode.value}`}
              />
              {mode.label}
            </span>
            <span className="mode-description" id={`mode-help-${mode.value}`}>
              {mode.description}
              {!availability[mode.value].available && (
                <span className="mode-unavailable">
                  {availability[mode.value].reason}
                </span>
              )}
            </span>
          </label>
        ))}
      </div>
    </fieldset>
  );
}

export function ResponseCard({
  result,
  question,
  deeper = false,
  onPreview,
}: {
  result: Result;
  question: string;
  deeper?: boolean;
  onPreview: (source: Source) => void;
}) {
  const search = result.kind === "search";
  const label = search
    ? "Résultats documentaires"
    : `${deeper ? "Réponse approfondie" : "Réponse"} · ${result.mode === "reflection" ? "Réflexion" : "Rapide"}`;
  const prefix = deeper ? "deeper" : "initial";
  return (
    <section className="panel result" aria-label={label}>
      <span className={search || !result.grounded ? "badge warning" : "badge"}>
        {search
          ? "Recherche · sans génération"
          : result.grounded
            ? "Citations vérifiées"
            : "Information absente"}
      </span>
      <h3>{label}</h3>
      <p className="result-question">{question}</p>
      {search ? (
        <p>
          {result.sources.length
            ? "Ces passages sont des résultats de recherche. Ils ne constituent pas une réponse validée."
            : "Aucun passage accessible retrouvé."}
        </p>
      ) : result.claims?.length ? (
        <ul className="answer">
          {result.claims.map((claim, i) => (
            <li key={i}>
              {claim.text}{" "}
              {claim.source_ids.map((id) => (
                <a
                  key={id}
                  href={`#${prefix}-source-${id}`}
                  aria-label={`Citation ${result.sources.findIndex((s) => s.source_id === id) + 1}`}
                >
                  [{result.sources.findIndex((s) => s.source_id === id) + 1}]
                </a>
              ))}
            </li>
          ))}
        </ul>
      ) : (
        <p className="answer">{result.answer}</p>
      )}
      <p className="muted">{result.safety_notice}</p>
      {!!result.sources.length && (
        <>
          <h3>{search ? "Passages retrouvés" : "Sources"}</h3>
          <div className="sources">
            {result.sources.map((s) => (
              <article key={s.source_id} id={`${prefix}-source-${s.source_id}`}>
                <a href={s.url} target="_blank" rel="noreferrer">
                  {s.document} — page {s.page}
                </a>
                <p>{s.excerpt}</p>
                <button onClick={() => onPreview(s)}>
                  Ouvrir la source · p. {s.page}
                </button>
              </article>
            ))}
          </div>
        </>
      )}
    </section>
  );
}
