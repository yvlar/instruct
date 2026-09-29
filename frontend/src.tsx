import { FormEvent, useRef, useState } from "react";
import { createRoot } from "react-dom/client";
import { Documents } from "./Documents";
import { Source, SourcePreview } from "./SourcePreview";
import { message, request } from "./api";
import "./styles.css";

type Result = {
  answer: string;
  sources: Source[];
  grounded: boolean;
  claims: { text: string; source_ids: string[] }[];
  safety_notice: string;
};
function App() {
  const answerEpoch = useRef(0);
  const [screen, setScreen] = useState("questions");
  const [question, setQuestion] = useState("");
  const [result, setResult] = useState<Result | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [source, setSource] = useState<Source | null>(null);
  async function ask(event: FormEvent) {
    event.preventDefault();
    const epoch = ++answerEpoch.current;
    setBusy(true);
    setError("");
    setResult(null);
    try {
      const answer = await request<Result>("/api/ask", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ question }),
      });
      if (epoch === answerEpoch.current) setResult(answer);
    } catch (e) {
      setError(message(e));
    } finally {
      setBusy(false);
    }
  }
  return (
    <main>
      <header className="app-header">
        <div className="brand">
          <span className="mark">I</span>
          <div>
            <h1>Instruct IA</h1>
            <p>Vos instructions, à portée de main.</p>
          </div>
        </div>
        <nav aria-label="Navigation principale">
          <button
            className={screen === "questions" ? "nav active" : "nav"}
            aria-current={screen === "questions" ? "page" : undefined}
            onClick={() => setScreen("questions")}
          >
            Questions
          </button>
          <button
            className={screen === "documents" ? "nav active" : "nav"}
            aria-current={screen === "documents" ? "page" : undefined}
            onClick={() => setScreen("documents")}
          >
            Documents
          </button>
        </nav>
      </header>
      {screen === "documents" ? (
        <Documents
          onChange={() => {
            answerEpoch.current++;
            setResult(null);
            setSource(null);
          }}
        />
      ) : (
        <>
          <section className="hero">
            <p className="eyebrow">ASSISTANT LOCAL D’INSTRUCTIONS DE TRAVAIL</p>
            <h2>Comment puis-je vous aider?</h2>
            <p className="muted">
              Posez votre question. Retrouvez les passages dans vos documents.
            </p>
            <form className="question-form" onSubmit={ask}>
              <label className="visually-hidden" htmlFor="question">
                Votre question
              </label>
              <textarea
                id="question"
                value={question}
                onChange={(e) => setQuestion(e.target.value)}
                placeholder="Ex. Quelle est la procédure de démarrage de la calandre?"
                minLength={3}
                maxLength={1000}
                required
              />
              <button disabled={busy}>
                {busy ? "Recherche…" : "Rechercher →"}
              </button>
            </form>
            <button
              className="text-button"
              onClick={() => setScreen("documents")}
            >
              Gérer mes documents →
            </button>
          </section>
          {error && (
            <p className="notice error" role="alert">
              {error}
            </p>
          )}
          <div aria-live="polite" aria-busy={busy}>
            {result && (
              <section className="result">
                <span
                  className={
                    result.grounded ? "badge available" : "badge pending"
                  }
                >
                  {result.grounded
                    ? "Extraits vérifiés"
                    : "Information absente"}
                </span>
                <h3>Réponse</h3>
                {result.grounded && result.claims.length > 0 ? (
                  <>
                    <p className="citation-notice">
                      Chaque extrait est relié à son passage source. Vérifiez
                      qu’il répond à votre situation et lisez les conditions qui
                      l’entourent.
                    </p>
                    <ul className="claims">
                      {result.claims.map((claim, index) => (
                        <li key={index}>
                          <span className="answer">{claim.text}</span>{" "}
                          {claim.source_ids.map((id) => {
                            const sourceIndex = result.sources.findIndex(
                              (s) => s.source_id === id,
                            );
                            const cited = result.sources[sourceIndex];
                            return (
                              cited && (
                                <a
                                  className="citation"
                                  key={id}
                                  href={`#source-${id}`}
                                  aria-label={`Source ${sourceIndex + 1} : ${cited.document}, page ${cited.page}`}
                                >
                                  [{sourceIndex + 1}]
                                </a>
                              )
                            );
                          })}
                        </li>
                      ))}
                    </ul>
                  </>
                ) : (
                  <p className="answer">{result.answer}</p>
                )}
                {!!result.sources.length && (
                  <>
                    <h3>Passages utilisés</h3>
                    <div className="sources">
                      {result.sources.map((s, i) => (
                        <article
                          key={s.source_id ?? i}
                          id={`source-${s.source_id}`}
                          tabIndex={-1}
                        >
                          <div>
                            <strong>{s.document}</strong>
                            <span>Page {s.page}</span>
                            <blockquote>{s.excerpt}</blockquote>
                            <details>
                              <summary>
                                Score de classement hybride :{" "}
                                {s.score.toFixed(3)}
                              </summary>
                              <p>
                                Ce score combine les classements des recherches
                                sémantique et lexicale. Ce n’est ni une
                                probabilité de vérité, ni une validation de la
                                réponse.
                              </p>
                            </details>
                          </div>
                          <button
                            className="secondary"
                            onClick={() => setSource(s)}
                          >
                            Ouvrir la source · p. {s.page}
                          </button>
                        </article>
                      ))}
                    </div>
                  </>
                )}
              </section>
            )}
          </div>
        </>
      )}
      <footer>
        {result?.safety_notice ??
          "Vérifiez toujours la version officielle de l’instruction avant d’exécuter une procédure. Cet assistant de recherche documentaire n’est pas une autorité en matière de sécurité industrielle."}
      </footer>
      {source && (
        <SourcePreview source={source} onClose={() => setSource(null)} />
      )}
    </main>
  );
}
createRoot(document.getElementById("root")!).render(<App />);
