import { FormEvent, useRef, useState } from "react";
import { createRoot } from "react-dom/client";
import { Documents } from "./Documents";
import { Source, SourcePreview } from "./SourcePreview";
import { message, request } from "./api";
import "./styles.css";

type Result = { answer: string; sources: Source[]; grounded: boolean };
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
          {result && (
            <section className="result">
              <span
                className={
                  result.grounded ? "badge available" : "badge pending"
                }
              >
                {result.grounded ? "Passages retrouvés" : "Information absente"}
              </span>
              <h3>Réponse</h3>
              <p className="answer">{result.answer}</p>
              {!!result.sources.length && (
                <>
                  <h3>Sources</h3>
                  <div className="sources">
                    {result.sources.map((s, i) => (
                      <article key={i}>
                        <div>
                          <strong>{s.document}</strong>
                          <span>
                            Page {s.page} · pertinence{" "}
                            {Math.round(s.score * 100)} %
                          </span>
                          <p>{s.excerpt}</p>
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
        </>
      )}
      <footer>
        Vérifiez toujours la version officielle de l’instruction avant
        d’exécuter une procédure.
      </footer>
      {source && (
        <SourcePreview source={source} onClose={() => setSource(null)} />
      )}
    </main>
  );
}
createRoot(document.getElementById("root")!).render(<App />);
