import React, {FormEvent, useState} from "react";
import {createRoot} from "react-dom/client";
import "./styles.css";

type Source = {source_id: string; document: string; page: number; excerpt: string; score: number};
type Claim = {text: string; source_ids: string[]};
type Result = {answer: string; sources: Source[]; grounded: boolean; claims: Claim[]; safety_notice: string};
const API = import.meta.env.VITE_API_URL ?? "http://localhost:8000";
const SAFETY_NOTICE = "Vérifiez toujours la version officielle de l’instruction avant d’exécuter une procédure. Cet assistant de recherche documentaire n’est pas une autorité en matière de sécurité industrielle.";

function App() {
  const [question, setQuestion] = useState("");
  const [result, setResult] = useState<Result | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");

  async function ask(e: FormEvent) {
    e.preventDefault();
    setBusy(true);
    setError("");
    setResult(null);
    try {
      const response = await fetch(`${API}/api/ask`, {
        method: "POST", headers: {"Content-Type": "application/json"},
        body: JSON.stringify({question}),
      });
      if (!response.ok) throw new Error((await response.json()).detail ?? "Erreur");
      setResult(await response.json());
    } catch (e) {
      setError(e instanceof Error ? e.message : "Erreur inconnue");
    } finally {
      setBusy(false);
    }
  }

  return <main>
    <header><span className="mark">I</span><div><h1>Instruct IA</h1><p>Assistant local d’instructions de travail</p></div></header>
    <section className="hero">
      <p className="eyebrow">RECHERCHE DOCUMENTAIRE LOCALE</p><h2>Comment puis-je vous aider?</h2>
      <form onSubmit={ask}>
        <label className="sr-only" htmlFor="question">Votre question</label>
        <textarea id="question" value={question} onChange={e => setQuestion(e.target.value)} placeholder="Ex. Quelle est la procédure de démarrage de la calandre?" minLength={3} maxLength={1000} required/>
        <button disabled={busy}>{busy ? "Recherche…" : "Rechercher"}</button>
      </form>
    </section>
    {error && <p className="error" role="alert">{error}</p>}
    <div aria-live="polite" aria-busy={busy}>
      {result && <section className="result">
        <span className={result.grounded ? "badge" : "badge warning"}>{result.grounded ? "Extraits retrouvés" : "Réponse non établie"}</span>
        <h3>Réponse</h3>
        {result.grounded && result.claims.length > 0 ? <>
          <p className="citation-notice">Chaque extrait est relié à son passage source. Vérifiez qu’il répond à votre situation et lisez les conditions qui l’entourent.</p>
          <ul className="claims">{result.claims.map((claim, index) => <li key={index}>
            <span className="answer">{claim.text}</span>{" "}
            {claim.source_ids.map(id => {
              const sourceIndex = result.sources.findIndex(s => s.source_id === id);
              const source = result.sources[sourceIndex];
              return source && <a className="citation" key={id} href={`#source-${id}`} aria-label={`Source ${sourceIndex + 1} : ${source.document}, page ${source.page}`}>[{sourceIndex + 1}]</a>;
            })}
          </li>)}</ul>
        </> : <p className="answer">{result.answer}</p>}
        {!!result.sources.length && <>
          <h3>Passages utilisés</h3>
          <div className="sources">{result.sources.map((source, index) => <article key={source.source_id} id={`source-${source.source_id}`} tabIndex={-1}>
            <strong>[{index + 1}] {source.document}</strong>
            <span>Page {source.page}</span>
            <blockquote>{source.excerpt}</blockquote>
            <details><summary>Similarité de recherche : {source.score.toFixed(3)}</summary>
              <p>Ce score compare les vecteurs de la question et du passage. Ce n’est ni une probabilité de vérité, ni une validation de la réponse.</p>
            </details>
          </article>)}</div>
        </>}
      </section>}
    </div>
    <footer>⚠ {result?.safety_notice ?? SAFETY_NOTICE}</footer>
  </main>;
}
createRoot(document.getElementById("root")!).render(<App/>);
