import { StrictMode, useEffect, useState } from "react"
import ReactDOM from "react-dom/client"
import "./index.css"

type VersionPayload = { app_version: string; correlation_id: string }

function App() {
  const fallback = import.meta.env.VITE_APP_VERSION ?? "dev"
  const [version, setVersion] = useState(fallback)
  useEffect(() => {
    fetch(`${import.meta.env.VITE_API_URL ?? ""}/api/v1/system/version`)
      .then(async (response) => {
        if (!response.ok) throw new Error("Version endpoint failed")
        return response.json() as Promise<VersionPayload>
      })
      .then((payload) => {
        if (
          typeof payload.app_version !== "string" ||
          !payload.app_version.trim()
        ) {
          throw new Error("Invalid version payload")
        }
        setVersion(payload.app_version)
      })
      .catch(() => setVersion(fallback))
  }, [])
  return (
    <>
      <a className="skip-link" href="#content">
        Pular para o conteúdo
      </a>
      <aside className="sandbox" aria-label="Ambiente de demonstração">
        <strong>Sandbox</strong>
        <span>Experiência demonstrativa. Nenhuma compra real.</span>
      </aside>
      <header className="site-header">
        <a className="brand" href="/" aria-label="RetroVault, página inicial">
          RETRO<span>VAULT</span>
        </a>
        <span className="status">Base operacional pronta</span>
      </header>
      <main id="content">
        <section className="hero" aria-labelledby="hero-title">
          <p className="eyebrow">ARQUIVO DIGITAL · SANDBOX</p>
          <h1 id="hero-title">
            Clássicos preservados.
            <br />
            <span>Fatos verificados.</span>
          </h1>
          <p className="lede">
            A fundação técnica da RetroVault está no ar. Catálogo, busca e
            comércio serão liberados somente quando seus dados e contratos
            estiverem governados.
          </p>
          <section className="facts" aria-label="Estado da plataforma">
            <article>
              <strong>API v1</strong>
              <span>Contrato versionado</span>
            </article>
            <article>
              <strong>Sandbox</strong>
              <span>Dados locais e sintéticos</span>
            </article>
            <article>
              <strong>{version}</strong>
              <span>Versão da aplicação</span>
            </article>
          </section>
        </section>
        <section className="notice" aria-labelledby="notice-title">
          <span aria-hidden="true">▣</span>
          <div>
            <h2 id="notice-title">Arquivo em preparação</h2>
            <p>
              Nenhum jogo, preço ou disponibilidade é exibido antes da
              publicação governada.
            </p>
          </div>
        </section>
      </main>
      <footer>
        <span>RetroVault Sandbox</span>
        <span>APP_VERSION {version}</span>
      </footer>
    </>
  )
}

ReactDOM.createRoot(document.getElementById("root")!).render(
  <StrictMode>
    <App />
  </StrictMode>,
)
