import React, {FormEvent, useState} from "react";
import {createRoot} from "react-dom/client";
import "./styles.css";

type Source={document:string,page:number,excerpt:string,score:number};
type Result={answer:string,sources:Source[],grounded:boolean};
const API=import.meta.env.VITE_API_URL ?? "http://localhost:8000";

function App(){
  const [question,setQuestion]=useState(""); const [result,setResult]=useState<Result|null>(null);
  const [busy,setBusy]=useState(false); const [error,setError]=useState("");
  async function ask(e:FormEvent){e.preventDefault();setBusy(true);setError("");setResult(null);
    try{const r=await fetch(`${API}/api/ask`,{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({question})});
      if(!r.ok)throw new Error((await r.json()).detail??"Erreur");setResult(await r.json());}
    catch(e){setError(e instanceof Error?e.message:"Erreur inconnue");}finally{setBusy(false)}}
  return <main><header><span className="mark">I</span><div><h1>Instruct IA</h1><p>Assistant local d’instructions de travail</p></div></header>
    <section className="hero"><p className="eyebrow">RECHERCHE DOCUMENTAIRE SÉCURISÉE</p><h2>Comment puis-je vous aider?</h2>
      <form onSubmit={ask}><textarea value={question} onChange={e=>setQuestion(e.target.value)} placeholder="Ex. Quelle est la procédure de démarrage de la calandre?" minLength={3} required/><button disabled={busy}>{busy?"Recherche…":"Rechercher"}</button></form></section>
    {error&&<p className="error">{error}</p>}{result&&<section className="result"><span className={result.grounded?"badge":"badge warning"}>{result.grounded?"Réponse documentée":"Information absente"}</span><h3>Réponse</h3><p className="answer">{result.answer}</p>
      {!!result.sources.length&&<><h3>Sources</h3><div className="sources">{result.sources.map((s,i)=><article key={i}><strong>{s.document}</strong><span>Page {s.page} · pertinence {Math.round(s.score*100)} %</span><p>{s.excerpt}…</p></article>)}</div></>}</section>}
    <footer>⚠ Vérifiez toujours la version officielle de l’instruction avant d’exécuter une procédure.</footer></main>}
createRoot(document.getElementById("root")!).render(<App/>);

