import type { FormEvent } from "react"
import { StrictMode, useCallback, useEffect, useRef, useState } from "react"
import ReactDOM from "react-dom/client"
import type { EditorialGame } from "./catalog/api"
import {
  apiUrl,
  catalogSnapshotKey,
  getCatalogFacets,
  getCatalogPage,
  getPopularGames,
  readCatalogSnapshot,
  writeCatalogSnapshot,
} from "./catalog/api"
import type { GameResponse, OfferResponse } from "./client"
import "./index.css"

type VersionPayload = { app_version: string; correlation_id: string }
type LoadState = "loading" | "ready" | "error"
type CatalogState = {
  games: GameResponse[]
  nextCursor: string | null
  state: LoadState
  commerceUnavailable: boolean
  stale: boolean
  message: string
  paginationError?: string
}

const emptyCatalog: CatalogState = {
  games: [],
  nextCursor: null,
  state: "loading",
  commerceUnavailable: false,
  stale: false,
  message: "Carregando jogos publicados.",
}

function useVersion() {
  const fallback = import.meta.env.VITE_APP_VERSION ?? "dev"
  const [version, setVersion] = useState(fallback)
  useEffect(() => {
    const controller = new AbortController()
    fetch(apiUrl("/api/v1/system/version"), { signal: controller.signal })
      .then(async (response) => {
        if (!response.ok) throw new Error("version_request_failed")
        return (await response.json()) as VersionPayload
      })
      .then((payload) => {
        if (
          typeof payload.app_version === "string" &&
          payload.app_version.trim()
        ) {
          setVersion(payload.app_version)
        }
      })
      .catch(() => setVersion(fallback))
    return () => controller.abort()
  }, [])
  return version
}

function useLocationPath() {
  const [path, setPath] = useState(() => window.location.pathname)
  useEffect(() => {
    const update = () => setPath(window.location.pathname)
    window.addEventListener("popstate", update)
    return () => window.removeEventListener("popstate", update)
  }, [])
  return path
}

function formatPrice(offer: OfferResponse): string {
  return new Intl.NumberFormat("pt-BR", {
    style: "currency",
    currency: offer.currency,
  }).format(offer.price_minor / 100)
}

function modeLabel(mode: OfferResponse["mode"]): string {
  return mode === "purchase" ? "Compra" : "Aluguel"
}

function OfferFacts({
  offers,
  commerceUnavailable,
}: {
  offers?: OfferResponse[] | null
  commerceUnavailable?: boolean
}) {
  if (commerceUnavailable) {
    return (
      <p className="commerce-note">
        Preço e disponibilidade temporariamente indisponíveis.
      </p>
    )
  }
  const currentOffers = offers ?? []
  const available = currentOffers.filter((offer) => offer.available_units > 0)
  if (available.length === 0) {
    return <p className="availability unavailable">Indisponível no momento</p>
  }
  return (
    <div className="offer-list">
      {available.map((offer) => (
        <div className="offer-row" key={offer.id}>
          <span className="offer-mode">{modeLabel(offer.mode)} · Sandbox</span>
          <strong>{formatPrice(offer)}</strong>
          <span className="availability available">Disponível</span>
          <span className="condition">{offer.condition_summary}</span>
        </div>
      ))}
    </div>
  )
}

function GameCard({
  game,
  commerceUnavailable = false,
}: {
  game: GameResponse | EditorialGame
  commerceUnavailable?: boolean
}) {
  const genre = game.attributes.genre
  return (
    <article className="game-card">
      <div className="cover-wrap">
        <img
          src={apiUrl(game.cover_url)}
          alt={`Capa de ${game.title}`}
          loading="lazy"
        />
        <span className="published-badge">Jogo publicado</span>
      </div>
      <div className="game-card-body">
        <p className="game-platform">
          {game.platform}
          {genre ? ` · ${genre}` : ""}
        </p>
        <h3>{game.title}</h3>
        <OfferFacts
          offers={"offers" in game ? game.offers : undefined}
          commerceUnavailable={commerceUnavailable}
        />
      </div>
    </article>
  )
}

function SearchPreview() {
  return (
    <search className="search-preview">
      <form onSubmit={(event) => event.preventDefault()}>
        <label htmlFor="home-search">Encontre um clássico</label>
        <div className="search-controls">
          <input
            id="home-search"
            type="search"
            placeholder="A busca por títulos chega em breve"
            disabled
            aria-describedby="search-hint"
          />
          <button type="submit" disabled aria-label="Buscar jogos">
            Buscar
          </button>
        </div>
        <span id="search-hint" className="field-hint">
          A busca direta será liberada na próxima etapa.
        </span>
      </form>
    </search>
  )
}

function HomePage() {
  const [popular, setPopular] = useState<GameResponse[]>([])
  const [popularState, setPopularState] = useState<LoadState>("loading")
  const [popularMessage, setPopularMessage] = useState(
    "Carregando jogos populares disponíveis.",
  )
  const [platforms, setPlatforms] = useState<string[]>([])
  const [facetsState, setFacetsState] = useState<LoadState>("loading")

  const loadHome = useCallback(() => {
    const controller = new AbortController()
    setPopularState("loading")
    setPopularMessage("Carregando jogos populares disponíveis.")
    void getPopularGames(controller.signal)
      .then((page) => {
        setPopular(page.items)
        setPopularState("ready")
        setPopularMessage(
          page.items.length
            ? `Jogos populares carregados: ${page.items.length}.`
            : "Ainda não há jogos publicados com unidades disponíveis.",
        )
      })
      .catch(() => {
        setPopular([])
        setPopularState("error")
        setPopularMessage(
          "Não foi possível confirmar quais jogos estão disponíveis agora. Tente novamente.",
        )
      })
    setFacetsState("loading")
    void getCatalogFacets(controller.signal)
      .then((facets) => {
        setPlatforms(facets.platforms)
        setFacetsState("ready")
      })
      .catch(() => {
        setPlatforms([])
        setFacetsState("error")
      })
    return () => controller.abort()
  }, [])

  useEffect(() => loadHome(), [loadHome])

  return (
    <main id="content" className="home-page">
      <section className="hero" aria-labelledby="hero-title">
        <p className="eyebrow">ARQUIVO DIGITAL · NEON ARCADE</p>
        <h1 id="hero-title">
          Clássicos preservados<span>.</span>
        </h1>
        <p className="lede">
          Encontre jogos que marcaram época e descubra o que está disponível no
          acervo.
        </p>
        <SearchPreview />
      </section>

      <section
        className="popular-section"
        aria-labelledby="popular-title"
        aria-busy={popularState === "loading"}
      >
        <div className="section-heading">
          <div>
            <p className="eyebrow">RANKING DEMONSTRATIVO</p>
            <h2 id="popular-title">Populares disponíveis agora</h2>
            <p className="section-copy">
              Seleção de demonstração entre jogos publicados com unidades
              disponíveis.
            </p>
          </div>
          <a className="text-link" href="/catalog?availability=available">
            Explorar disponíveis <span aria-hidden="true">→</span>
          </a>
        </div>
        {popularState === "loading" ? (
          <div className="game-grid home-grid" aria-hidden="true">
            {[1, 2, 3].map((item) => (
              <div className="game-skeleton" key={item}>
                <span />
                <span />
                <span />
              </div>
            ))}
          </div>
        ) : popular.length ? (
          <div className="game-grid home-grid">
            {popular.map((game) => (
              <GameCard key={game.id} game={game} />
            ))}
          </div>
        ) : (
          <div
            className={`state-panel ${popularState === "error" ? "state-error" : ""}`}
            role="status"
          >
            <p>{popularMessage}</p>
            {popularState === "error" && (
              <button
                className="button-secondary"
                type="button"
                onClick={loadHome}
              >
                Tentar novamente
              </button>
            )}
            {popularState === "ready" && (
              <a className="button-secondary" href="/catalog">
                Abrir catálogo
              </a>
            )}
          </div>
        )}
        <p className="visually-hidden" role="status" aria-live="polite">
          {popularState === "ready" && popular.length > 0 ? popularMessage : ""}
        </p>
      </section>

      <section className="home-platforms" aria-labelledby="platform-title">
        <div className="section-heading compact-heading">
          <div>
            <p className="eyebrow">EXPLORE POR GERAÇÃO</p>
            <h2 id="platform-title">Plataformas</h2>
          </div>
          <a className="text-link" href="/catalog">
            Ver catálogo completo <span aria-hidden="true">→</span>
          </a>
        </div>
        {facetsState === "loading" ? (
          <p className="state-message" role="status" aria-busy="true">
            Carregando plataformas publicadas…
          </p>
        ) : facetsState === "error" ? (
          <p className="state-message" role="status">
            Não foi possível carregar as plataformas. Explore o{" "}
            <a href="/catalog">catálogo</a>.
          </p>
        ) : platforms.length ? (
          <ul className="platform-list">
            {platforms.map((platform) => (
              <li key={platform}>
                <a
                  className="platform-chip"
                  href={`/catalog?${new URLSearchParams({ platform })}`}
                >
                  {platform}
                </a>
              </li>
            ))}
          </ul>
        ) : (
          <p className="state-message" role="status">
            As plataformas aparecerão aqui quando houver jogos publicados.
          </p>
        )}
      </section>
    </main>
  )
}

function initialFilters(): URLSearchParams {
  const allowedKeys = ["platform", "genre", "availability", "cursor"]
  const lastValues = new Map<string, string>()
  for (const [key, value] of new URLSearchParams(window.location.search)) {
    if (allowedKeys.includes(key)) lastValues.set(key, value)
  }
  const params = new URLSearchParams()
  for (const key of allowedKeys) {
    const value = lastValues.get(key)
    if (value !== undefined) params.set(key, value)
  }
  return params
}

function canonicalizeFilters(params: URLSearchParams) {
  const query = params.toString()
  const canonicalUrl = `/catalog${query ? `?${query}` : ""}`
  if (`${window.location.pathname}${window.location.search}` !== canonicalUrl) {
    window.history.replaceState({}, "", canonicalUrl)
  }
}

function CatalogPage() {
  const [params, setParams] = useState(initialFilters)
  const [draftPlatform, setDraftPlatform] = useState(
    () => params.get("platform") ?? "",
  )
  const [draftGenre, setDraftGenre] = useState(() => params.get("genre") ?? "")
  const [draftAvailability, setDraftAvailability] = useState(
    () => params.get("availability") ?? "",
  )
  const [catalog, setCatalog] = useState<CatalogState>(emptyCatalog)
  const [loadingMore, setLoadingMore] = useState(false)
  const catalogRef = useRef(catalog)
  const requestSequence = useRef(0)

  useEffect(() => {
    catalogRef.current = catalog
  }, [catalog])

  const load = useCallback(
    async (query: URLSearchParams, append = false, signal?: AbortSignal) => {
      const requestId = append
        ? requestSequence.current
        : ++requestSequence.current
      const current = catalogRef.current
      setCatalog((current) => ({
        ...current,
        state: append ? current.state : "loading",
        message: "Carregando jogos publicados.",
        stale: append ? current.stale : false,
        ...(append
          ? { paginationError: undefined }
          : {
              games: [],
              nextCursor: null,
              commerceUnavailable: false,
              paginationError: undefined,
            }),
      }))
      try {
        const request = new URLSearchParams(query)
        if (append && current.nextCursor)
          request.set("cursor", current.nextCursor)
        const page = await getCatalogPage(request, signal)
        if (requestSequence.current !== requestId) return
        const games = append ? [...current.games, ...page.items] : page.items
        setCatalog({
          games,
          nextCursor: page.next_cursor,
          state: "ready",
          commerceUnavailable: page.commerce_status === "unavailable",
          stale: false,
          message: games.length
            ? `${games.length} ${games.length === 1 ? "jogo publicado carregado" : "jogos publicados carregados"}.`
            : "Nenhum jogo publicado corresponde a estes filtros.",
          paginationError: undefined,
        })
        if (!query.has("availability") && !query.has("cursor"))
          writeCatalogSnapshot(catalogSnapshotKey(query), games)
      } catch {
        if (signal?.aborted || requestSequence.current !== requestId) return
        if (append) {
          setCatalog((current) => ({
            ...current,
            state: "ready",
            paginationError:
              "Não foi possível carregar mais jogos. Tente novamente.",
          }))
          return
        }
        if (!append && !query.has("availability") && !query.has("cursor")) {
          const snapshot = readCatalogSnapshot(catalogSnapshotKey(query))
          if (snapshot?.items.length) {
            setCatalog({
              games: snapshot.items,
              nextCursor: null,
              state: "ready",
              commerceUnavailable: true,
              stale: true,
              message: `Exibindo dados editoriais salvos em ${new Date(snapshot.savedAt).toLocaleString("pt-BR")}. Preços e disponibilidade não foram armazenados.`,
            })
            return
          }
        }
        setCatalog((current) => ({
          ...current,
          state: "error",
          commerceUnavailable: false,
          stale: false,
          message: query.has("availability")
            ? "Não foi possível confirmar a disponibilidade para este filtro. Tente novamente ou remova o filtro."
            : "Não foi possível carregar o catálogo. Verifique sua conexão e tente novamente.",
        }))
      }
    },
    [],
  )

  useEffect(() => canonicalizeFilters(params), [params])

  useEffect(() => {
    const controller = new AbortController()
    void load(params, false, controller.signal)
    return () => controller.abort()
  }, [load, params])

  useEffect(() => {
    const onPopState = () => {
      requestSequence.current += 1
      const next = initialFilters()
      canonicalizeFilters(next)
      setParams(next)
    }
    window.addEventListener("popstate", onPopState)
    return () => window.removeEventListener("popstate", onPopState)
  }, [])

  useEffect(() => {
    setDraftPlatform(params.get("platform") ?? "")
    setDraftGenre(params.get("genre") ?? "")
    setDraftAvailability(params.get("availability") ?? "")
  }, [params])

  function submitFilters(event: FormEvent<HTMLFormElement>) {
    event.preventDefault()
    requestSequence.current += 1
    const next = new URLSearchParams()
    if (draftPlatform.trim()) next.set("platform", draftPlatform.trim())
    if (draftGenre.trim()) next.set("genre", draftGenre.trim())
    if (draftAvailability) next.set("availability", draftAvailability)
    const query = next.toString()
    window.history.pushState({}, "", `/catalog${query ? `?${query}` : ""}`)
    setParams(next)
  }

  function clearFilters() {
    requestSequence.current += 1
    window.history.pushState({}, "", "/catalog")
    setParams(new URLSearchParams())
  }

  const selectedPlatform = params.get("platform") ?? ""
  const selectedGenre = params.get("genre") ?? ""
  const selectedAvailability = params.get("availability") ?? ""

  return (
    <main id="content" className="catalog-page">
      <section className="catalog-intro" aria-labelledby="catalog-title">
        <p className="eyebrow">ACERVO PUBLICADO</p>
        <h1 id="catalog-title">
          Catálogo<span>.</span>
        </h1>
        <p className="lede">
          Explore jogos revisados e publicados. Os dados comerciais são
          demonstrativos e podem mudar.
        </p>
      </section>

      <section className="catalog-layout" aria-label="Exploração do catálogo">
        <form
          className="filter-panel"
          aria-label="Filtros do catálogo"
          onSubmit={submitFilters}
        >
          <div className="filter-heading">
            <h2>Filtrar jogos</h2>
            <button
              type="button"
              className="clear-filters"
              onClick={clearFilters}
              disabled={
                !selectedPlatform &&
                !selectedGenre &&
                !selectedAvailability &&
                !draftPlatform.trim() &&
                !draftGenre.trim() &&
                !draftAvailability
              }
            >
              Limpar
            </button>
          </div>
          <label htmlFor="filter-platform">Plataforma</label>
          <input
            id="filter-platform"
            type="text"
            value={draftPlatform}
            placeholder="Ex.: PlayStation"
            onChange={(event) => setDraftPlatform(event.target.value)}
          />
          <label htmlFor="filter-genre">Gênero</label>
          <input
            id="filter-genre"
            type="text"
            value={draftGenre}
            placeholder="Ex.: Aventura"
            onChange={(event) => setDraftGenre(event.target.value)}
          />
          <label htmlFor="filter-availability">Disponibilidade</label>
          <select
            id="filter-availability"
            value={draftAvailability}
            onChange={(event) => setDraftAvailability(event.target.value)}
          >
            <option value="">Todas</option>
            <option value="available">Disponíveis agora</option>
            <option value="unavailable">Indisponíveis</option>
          </select>
          <p className="field-hint">
            Os filtros também ficam salvos no endereço desta página.
          </p>
          <button className="button-primary apply-filters" type="submit">
            Aplicar filtros
          </button>
        </form>

        <section
          className="catalog-results"
          aria-labelledby="results-title"
          aria-busy={catalog.state === "loading" || loadingMore}
        >
          <p className="visually-hidden" role="status" aria-live="polite">
            {catalog.state === "ready" && catalog.games.length > 0
              ? catalog.message
              : ""}
          </p>
          <div className="results-heading">
            <div>
              <h2 id="results-title">Jogos publicados</h2>
              {catalog.state === "ready" && (
                <p>
                  {catalog.games.length}{" "}
                  {catalog.games.length === 1 ? "jogo" : "jogos"}
                  {catalog.stale ? " · dados desatualizados" : ""}
                </p>
              )}
            </div>
          </div>
          {catalog.commerceUnavailable && (
            <div className="commerce-banner" role="status">
              <strong>Dados comerciais indisponíveis.</strong> Valores e
              estoques não são exibidos até a consulta voltar.
            </div>
          )}
          {catalog.state === "loading" ? (
            <>
              <p className="state-message" role="status" aria-busy="true">
                {catalog.message}
              </p>
              <div className="game-grid catalog-grid" aria-hidden="true">
                {[1, 2, 3, 4].map((item) => (
                  <div className="game-skeleton" key={item}>
                    <span />
                    <span />
                    <span />
                  </div>
                ))}
              </div>
            </>
          ) : catalog.state === "error" ? (
            <div className="state-panel state-error" role="alert">
              <p>{catalog.message}</p>
              <button
                className="button-secondary"
                type="button"
                onClick={() => void load(params)}
              >
                Tentar novamente
              </button>
            </div>
          ) : catalog.games.length ? (
            <>
              {catalog.stale && (
                <div className="stale-note">
                  <p role="status">{catalog.message}</p>
                  <button
                    className="button-secondary"
                    type="button"
                    onClick={() => void load(params)}
                  >
                    Tentar atualizar
                  </button>
                </div>
              )}
              <div className="game-grid catalog-grid">
                {catalog.games.map((game) => (
                  <GameCard
                    key={game.id}
                    game={game}
                    commerceUnavailable={catalog.commerceUnavailable}
                  />
                ))}
              </div>
              {catalog.paginationError && (
                <p className="state-message" role="status">
                  {catalog.paginationError}
                </p>
              )}
              {catalog.nextCursor && !catalog.stale && (
                <div className="load-more-wrap">
                  <button
                    className="button-secondary"
                    type="button"
                    disabled={loadingMore}
                    onClick={async () => {
                      setLoadingMore(true)
                      const next = new URLSearchParams(params)
                      next.set("cursor", catalog.nextCursor!)
                      const query = next.toString()
                      window.history.pushState({}, "", `/catalog?${query}`)
                      await load(next, true)
                      setLoadingMore(false)
                    }}
                  >
                    {loadingMore ? "Carregando…" : "Carregar mais jogos"}
                  </button>
                </div>
              )}
            </>
          ) : (
            <div className="state-panel" role="status">
              <p>{catalog.message}</p>
              {(selectedPlatform || selectedGenre || selectedAvailability) && (
                <button
                  className="button-secondary"
                  type="button"
                  onClick={clearFilters}
                >
                  Limpar filtros
                </button>
              )}
            </div>
          )}
        </section>
      </section>
    </main>
  )
}

function App() {
  const version = useVersion()
  const path = useLocationPath()
  const page = path === "/catalog" ? <CatalogPage /> : <HomePage />

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
        <nav aria-label="Navegação principal">
          <a aria-current={path === "/" ? "page" : undefined} href="/">
            Descobrir
          </a>
          <a
            aria-current={path === "/catalog" ? "page" : undefined}
            href="/catalog"
          >
            Catálogo
          </a>
        </nav>
        <span className="status">Acervo demonstrativo</span>
      </header>
      {page}
      <footer>
        <span>RetroVault Sandbox · Dados demonstrativos</span>
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
