import { useEffect, useState } from "react";

type Health = {
  status: string;
  database?: string;
};

const apiBase = import.meta.env.VITE_API_BASE_URL ?? "http://localhost:8000";

function Status({ label, value }: { label: string; value: string }) {
  const healthy = value === "Online" || value === "Ready";
  return (
    <div className="status-row">
      <span>{label}</span>
      <span className={healthy ? "status healthy" : "status pending"}>
        <i />
        {value}
      </span>
    </div>
  );
}

export default function App() {
  const [apiStatus, setApiStatus] = useState("Checking");
  const [databaseStatus, setDatabaseStatus] = useState("Checking");

  useEffect(() => {
    const checkHealth = async () => {
      try {
        const response = await fetch(`${apiBase}/health/ready`);
        const health = (await response.json()) as Health;
        setApiStatus(response.ok ? "Online" : "Unavailable");
        setDatabaseStatus(health.database === "ok" ? "Ready" : "Unavailable");
      } catch {
        setApiStatus("Unavailable");
        setDatabaseStatus("Unknown");
      }
    };

    void checkHealth();
    const interval = window.setInterval(() => void checkHealth(), 10000);
    return () => window.clearInterval(interval);
  }, []);

  return (
    <main className="shell">
      <header className="topbar">
        <a className="brand" href="/">
          <span className="brand-mark">A</span>
          <span>AI Agent <strong>Studio</strong></span>
        </a>
        <span className="environment">M0 · DEVELOPMENT</span>
      </header>

      <section className="hero">
        <p className="eyebrow">AGENT PLATFORM</p>
        <h1>Build, orchestrate<br /><span>and govern AI agents.</span></h1>
        <p className="intro">
          A modular workspace for model routing, workflow orchestration and
          policy-governed tool execution.
        </p>
        <div className="hero-actions">
          <a className="primary-button" href={`${apiBase}/docs`}>Explore API <span>↗</span></a>
          <a className="secondary-button" href="https://github.com/Jasonzhu12397/AI-Agent-Stack">
            Source repository <span>↗</span>
          </a>
        </div>
      </section>

      <section className="overview">
        <div className="section-heading">
          <div>
            <p className="eyebrow">SYSTEM OVERVIEW</p>
            <h2>Platform status</h2>
          </div>
          <span className="live-label"><i /> LIVE CHECK</span>
        </div>
        <div className="status-card">
          <Status label="Platform API" value={apiStatus} />
          <Status label="PostgreSQL" value={databaseStatus} />
          <div className="status-row">
            <span>Worker</span>
            <span className="status pending"><i /> Heartbeat service</span>
          </div>
        </div>
      </section>

      <section className="milestones">
        <p className="eyebrow">DELIVERY ROADMAP</p>
        <div className="milestone-grid">
          {[
            ["M0", "Platform foundation", "API, Worker, storage and developer environment"],
            ["M1", "Agent runtime", "Agent versions, asynchronous runs and tool routing"],
            ["M2", "Workflow engine", "Visual DAG authoring and validated execution"],
            ["M3", "Human approval", "Policy decisions and hash-bound approvals"],
          ].map(([code, title, detail], index) => (
            <article className="milestone" key={code}>
              <span className={index === 0 ? "milestone-code current" : "milestone-code"}>{code}</span>
              <h3>{title}</h3>
              <p>{detail}</p>
              <span className="milestone-state">{index === 0 ? "IN PROGRESS" : "PLANNED"}</span>
            </article>
          ))}
        </div>
      </section>

      <footer>
        <span>AI Agent Studio <span className="muted">·</span> M0 foundation</span>
        <span>Policy first. Observable by design.</span>
      </footer>
    </main>
  );
}
