import type { FormEvent } from "react"
import { StrictMode, useCallback, useEffect, useRef, useState } from "react"
import ReactDOM from "react-dom/client"
import type { EditorialGame } from "./catalog/api"
import {
  apiUrl,
  CatalogApiError,
  catalogSnapshotKey,
  createPixelEntry,
  getCatalogFacets,
  getCatalogPage,
  getGameDetails,
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
    <a
      className="game-card"
      aria-label={`${game.title}, ${game.platform}`}
      href={`/games/${game.id}`}
    >
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
        {(game.attributes.region || game.attributes.edition) && (
          <p className="game-edition">
            {game.attributes.region ? `Região: ${game.attributes.region}` : ""}
            {game.attributes.region && game.attributes.edition ? " · " : ""}
            {game.attributes.edition
              ? `Edição: ${game.attributes.edition}`
              : ""}
          </p>
        )}
        <OfferFacts
          offers={"offers" in game ? game.offers : undefined}
          commerceUnavailable={commerceUnavailable}
        />
      </div>
    </a>
  )
}

function PixelEntry({ gameId }: { gameId?: string }) {
  const [loading, setLoading] = useState(false)
  const [entry, setEntry] = useState<Awaited<
    ReturnType<typeof createPixelEntry>
  > | null>(null)
  const [message, setMessage] = useState("")
  const [error, setError] = useState(false)
  const headingId = gameId ? "pixel-game-title" : "pixel-home-title"

  useEffect(() => {
    if (!entry?.expires_at) return
    const expiresAt = Date.parse(entry.expires_at)
    if (!Number.isFinite(expiresAt)) return
    const delay = expiresAt - Date.now()
    if (delay <= 0) {
      setEntry(null)
      setError(true)
      setMessage(
        "A referência expirou. Prepare uma nova conversa para continuar.",
      )
      return
    }
    const timeout = window.setTimeout(() => {
      setEntry(null)
      setError(true)
      setMessage(
        "A referência expirou. Prepare uma nova conversa para continuar.",
      )
    }, delay)
    return () => window.clearTimeout(timeout)
  }, [entry])

  async function prepareEntry() {
    setLoading(true)
    setEntry(null)
    setError(false)
    setMessage("Preparando a conversa com Pixel.")
    try {
      const nextEntry = await createPixelEntry(gameId)
      setEntry(nextEntry)
      setMessage(
        nextEntry.reference
          ? "Contexto do jogo preparado. Escolha como abrir o WhatsApp."
          : "Conversa preparada. Escolha como abrir o WhatsApp.",
      )
    } catch (cause) {
      const unavailable =
        cause instanceof CatalogApiError && cause.status === 503
      setError(true)
      setMessage(
        unavailable
          ? "A conversa com Pixel está indisponível agora. Tente novamente mais tarde."
          : cause instanceof CatalogApiError && cause.status === 404
            ? "Este jogo não está mais publicado. Volte ao catálogo para escolher outro."
            : "Não foi possível preparar a conversa. Seu contexto continua nesta página; tente novamente.",
      )
    } finally {
      setLoading(false)
    }
  }

  return (
    <section className="pixel-entry" aria-labelledby={headingId}>
      <div>
        <p className="eyebrow">ATENDIMENTO NO WHATSAPP</p>
        <h2 id={headingId}>Converse com Pixel</h2>
        <p>
          {gameId
            ? "Tire dúvidas sobre este jogo com o contexto já preparado."
            : "Peça ajuda para encontrar um jogo no acervo publicado."}
        </p>
      </div>
      <button
        className="button-primary"
        type="button"
        onClick={prepareEntry}
        disabled={loading}
      >
        Preparar conversa com Pixel
      </button>
      {message && (
        <p
          className={error ? "pixel-status pixel-error" : "pixel-status"}
          role={error ? "alert" : "status"}
          aria-live={error ? "assertive" : "polite"}
        >
          {message}
        </p>
      )}
      {entry && (
        <fieldset className="pixel-links">
          <legend className="visually-hidden">
            Opções para abrir a conversa
          </legend>
          <a
            className="button-secondary"
            href={entry.whatsapp_url}
            target="_blank"
            rel="noopener noreferrer"
          >
            Continuar no WhatsApp
          </a>
          <a
            className="text-link"
            href={entry.web_whatsapp_url}
            target="_blank"
            rel="noopener noreferrer"
          >
            Usar WhatsApp Web (abre uma nova guia)
          </a>
        </fieldset>
      )}
      {error && (
        <button
          className="button-secondary"
          type="button"
          onClick={prepareEntry}
          disabled={loading}
        >
          Tentar novamente
        </button>
      )}
    </section>
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
  const homeRequestSequence = useRef(0)
  const homeController = useRef<AbortController | null>(null)

  const loadHome = useCallback(() => {
    const sequence = ++homeRequestSequence.current
    homeController.current?.abort()
    const controller = new AbortController()
    homeController.current = controller
    const isCurrent = () =>
      sequence === homeRequestSequence.current && !controller.signal.aborted
    setPopularState("loading")
    setPopularMessage("Carregando jogos populares disponíveis.")
    void getPopularGames(controller.signal)
      .then((page) => {
        if (!isCurrent()) return
        setPopular(page.items)
        setPopularState("ready")
        setPopularMessage(
          page.items.length
            ? `Jogos populares carregados: ${page.items.length}.`
            : "Ainda não há jogos publicados com unidades disponíveis.",
        )
      })
      .catch(() => {
        if (!isCurrent()) return
        setPopular([])
        setPopularState("error")
        setPopularMessage(
          "Não foi possível confirmar quais jogos estão disponíveis agora. Tente novamente.",
        )
      })
    setFacetsState("loading")
    void getCatalogFacets(controller.signal)
      .then((facets) => {
        if (!isCurrent()) return
        setPlatforms(facets.platforms)
        setFacetsState("ready")
      })
      .catch(() => {
        if (!isCurrent()) return
        setPlatforms([])
        setFacetsState("error")
      })
  }, [])

  useEffect(() => {
    loadHome()
    return () => {
      homeRequestSequence.current += 1
      homeController.current?.abort()
      homeController.current = null
    }
  }, [loadHome])

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

      <PixelEntry />

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

function GameOffers({
  game,
  refreshing,
  refreshError,
  onRefresh,
}: {
  game: GameResponse
  refreshing: boolean
  refreshError: boolean
  onRefresh: () => void
}) {
  const offers = game.offers ?? []
  const availableOffers = offers.filter((offer) => offer.available_units > 0)

  return (
    <section className="detail-offers" aria-labelledby="offer-title">
      <div className="detail-section-heading">
        <div>
          <p className="eyebrow">COMMERCE · DADOS ATUAIS</p>
          <h2 id="offer-title">Ofertas e unidades físicas</h2>
        </div>
        <button
          className="button-secondary"
          type="button"
          onClick={onRefresh}
          disabled={refreshing}
          aria-busy={refreshing}
        >
          Atualizar disponibilidade
        </button>
      </div>

      {refreshError || game.commerce_status === "unavailable" ? (
        <div className="commerce-banner" role="status">
          <strong>
            {refreshError
              ? "Não foi possível atualizar os detalhes do jogo."
              : "Dados comerciais indisponíveis."}
          </strong>{" "}
          Não é possível confirmar condição, preço ou disponibilidade agora.
          Tente atualizar mais tarde.
        </div>
      ) : availableOffers.length === 0 ? (
        <div className="state-panel detail-unavailable" role="status">
          <p>
            Nenhuma unidade física está disponível agora. Não há compra ou
            aluguel para iniciar.
          </p>
        </div>
      ) : (
        <div className="detail-offer-list">
          {offers.map((offer) => (
            <article className="detail-offer" key={offer.id}>
              <div className="offer-overview">
                <div>
                  <p className="offer-mode">
                    {modeLabel(offer.mode)}
                    {offer.sandbox ? " · Sandbox" : ""}
                  </p>
                  <h3>{offer.sku_code ? `SKU ${offer.sku_code}` : "SKU"}</h3>
                </div>
                <strong className="detail-price">{formatPrice(offer)}</strong>
              </div>
              <p
                className={
                  offer.available_units > 0
                    ? "availability available"
                    : "availability unavailable"
                }
              >
                {offer.available_units > 0
                  ? `${offer.available_units} ${
                      offer.available_units === 1
                        ? "unidade disponível"
                        : "unidades disponíveis"
                    }`
                  : "Indisponível no momento"}
              </p>
              {offer.units.length > 0 ? (
                <div className="physical-unit-list">
                  {offer.units.map((unit, index) => (
                    <section
                      className="physical-unit"
                      key={`${offer.id}-${index}`}
                    >
                      <h4>Unidade física {index + 1}</h4>
                      <dl className="unit-facts">
                        <div className="unit-fact">
                          <dt>Condição</dt>
                          <dd>
                            {unit.condition_summary.trim() || "Não informado"}
                          </dd>
                        </div>
                        <div className="unit-fact">
                          <dt>Defeitos conhecidos</dt>
                          <dd>
                            {unit.defects == null
                              ? "Não informado"
                              : unit.defects.length
                                ? unit.defects.join("; ")
                                : "Nenhum defeito conhecido registrado"}
                          </dd>
                        </div>
                        <div className="unit-fact">
                          <dt>Itens inclusos</dt>
                          <dd>
                            {unit.included_items == null
                              ? "Não informado"
                              : unit.included_items.length
                                ? unit.included_items.join(", ")
                                : "Nenhum item adicional registrado"}
                          </dd>
                        </div>
                      </dl>
                    </section>
                  ))}
                </div>
              ) : offer.available_units > 0 ? (
                <div className="physical-unit-list">
                  <section className="physical-unit">
                    <h4>Detalhes da unidade</h4>
                    <dl className="unit-facts">
                      <div className="unit-fact">
                        <dt>Condição</dt>
                        <dd>{offer.condition_summary || "Não informado"}</dd>
                      </div>
                      <div className="unit-fact">
                        <dt>Defeitos conhecidos</dt>
                        <dd>Não informado</dd>
                      </div>
                      <div className="unit-fact">
                        <dt>Itens inclusos</dt>
                        <dd>Não informado</dd>
                      </div>
                    </dl>
                  </section>
                </div>
              ) : null}
            </article>
          ))}
        </div>
      )}
    </section>
  )
}

function GameDetailPage({ gameId }: { gameId: string }) {
  const [game, setGame] = useState<GameResponse | null>(null)
  const gameRef = useRef<GameResponse | null>(null)
  const refreshSequence = useRef(0)
  const [pageState, setPageState] = useState<
    "loading" | "ready" | "error" | "missing"
  >("loading")
  const [refreshing, setRefreshing] = useState(false)
  const [refreshError, setRefreshError] = useState(false)
  const [announcement, setAnnouncement] = useState("")

  const refresh = useCallback(
    async (signal?: AbortSignal) => {
      const sequence = ++refreshSequence.current
      setRefreshing(true)
      try {
        const nextGame = await getGameDetails(gameId, signal)
        if (signal?.aborted || sequence !== refreshSequence.current) return
        setRefreshError(false)
        const previous = gameRef.current
        const previousCommerce = previous
          ? JSON.stringify({
              status: previous.commerce_status,
              offers: previous.offers,
            })
          : null
        const nextCommerce = JSON.stringify({
          status: nextGame.commerce_status,
          offers: nextGame.offers,
        })
        gameRef.current = nextGame
        setGame(nextGame)
        setPageState("ready")
        setAnnouncement(
          previousCommerce !== null && previousCommerce !== nextCommerce
            ? "As condições comerciais deste jogo foram atualizadas."
            : "",
        )
      } catch (cause) {
        if (signal?.aborted || sequence !== refreshSequence.current) return
        if (cause instanceof CatalogApiError && cause.status === 404) {
          setRefreshError(false)
          gameRef.current = null
          setGame(null)
          setPageState("missing")
          setAnnouncement("Este jogo não está mais publicado.")
        } else if (gameRef.current) {
          setRefreshError(true)
          const withoutCommercialFacts: GameResponse = {
            ...gameRef.current,
            commerce_status: "unavailable",
            offers: undefined,
          }
          gameRef.current = withoutCommercialFacts
          setGame(withoutCommercialFacts)
          setAnnouncement(
            "Não foi possível atualizar os detalhes do jogo. Preço e disponibilidade foram ocultados até uma nova consulta.",
          )
        } else {
          setPageState("error")
          setAnnouncement(
            "Não foi possível carregar os detalhes do jogo. Verifique sua conexão e tente novamente.",
          )
        }
      } finally {
        if (sequence === refreshSequence.current) setRefreshing(false)
      }
    },
    [gameId],
  )

  useEffect(() => {
    const controller = new AbortController()
    void refresh(controller.signal)
    const interval = window.setInterval(() => void refresh(), 30_000)
    return () => {
      refreshSequence.current += 1
      controller.abort()
      window.clearInterval(interval)
    }
  }, [refresh])

  if (pageState === "loading") {
    return (
      <main id="content" className="detail-page">
        <section
          className="state-message"
          aria-label="Detalhes do jogo"
          aria-busy="true"
          role="status"
        >
          Carregando os detalhes do jogo publicado…
        </section>
      </main>
    )
  }

  if (pageState === "error" || pageState === "missing" || !game) {
    return (
      <main id="content" className="detail-page">
        <section className="detail-state" aria-labelledby="detail-error-title">
          <p className="eyebrow">VISÃO GERAL DO JOGO</p>
          <h1 id="detail-error-title">
            {pageState === "missing"
              ? "Jogo não encontrado."
              : "Não foi possível carregar."}
          </h1>
          <p role={pageState === "missing" ? "status" : "alert"}>
            {announcement || "Tente novamente ou volte ao catálogo publicado."}
          </p>
          <div className="detail-actions">
            <button
              className="button-secondary"
              type="button"
              onClick={() => void refresh()}
              disabled={refreshing}
            >
              {refreshing ? "Carregando…" : "Tentar novamente"}
            </button>
            <a className="button-primary" href="/catalog">
              Voltar ao catálogo
            </a>
          </div>
          <PixelEntry />
        </section>
      </main>
    )
  }

  const region = game.attributes.region
  const edition = game.attributes.edition
  const description = game.attributes.description

  return (
    <main id="content" className="detail-page">
      <article className="game-detail" aria-labelledby="game-detail-title">
        <a className="back-link" href="/catalog">
          ← Voltar ao catálogo
        </a>
        <div className="detail-overview">
          <div className="detail-cover">
            <img src={apiUrl(game.cover_url)} alt={`Capa de ${game.title}`} />
            <span className="published-badge">Jogo publicado</span>
          </div>
          <section
            className="detail-editorial"
            aria-label="Dados editoriais do jogo"
          >
            <p className="eyebrow">JOGO PUBLICADO · CATÁLOGO</p>
            <h1 id="game-detail-title">{game.title}</h1>
            <p className="detail-platform">{game.platform}</p>
            {(region || edition) && (
              <p className="detail-edition">
                {region ? `Região: ${region}` : ""}
                {region && edition ? " · " : ""}
                {edition ? `Edição: ${edition}` : ""}
              </p>
            )}
            <p className="detail-description">
              {description || "Descrição editorial não informada."}
            </p>
          </section>
        </div>

        <p className="sandbox-note">
          <strong>Sandbox.</strong> Valores e disponibilidades são
          demonstrativos e podem mudar.
        </p>
        <p className="visually-hidden" role="status" aria-live="polite">
          {refreshing ? "Atualizando os dados comerciais." : announcement}
        </p>
        <GameOffers
          game={game}
          refreshing={refreshing}
          refreshError={refreshError}
          onRefresh={() => void refresh()}
        />
        <PixelEntry gameId={game.id} />
      </article>
    </main>
  )
}

function initialFilters(): URLSearchParams {
  const allowedKeys = ["q", "platform", "genre", "availability", "cursor"]
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

function searchLength(value: string): number {
  return Array.from(value).length
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
  const [draftSearch, setDraftSearch] = useState(() => params.get("q") ?? "")
  const [draftPlatform, setDraftPlatform] = useState(
    () => params.get("platform") ?? "",
  )
  const [draftGenre, setDraftGenre] = useState(() => params.get("genre") ?? "")
  const [draftAvailability, setDraftAvailability] = useState(
    () => params.get("availability") ?? "",
  )
  const [catalog, setCatalog] = useState<CatalogState>(emptyCatalog)
  const [loadingMore, setLoadingMore] = useState(false)
  const [searchError, setSearchError] = useState("")
  const catalogRef = useRef(catalog)
  const requestSequence = useRef(0)

  useEffect(() => {
    catalogRef.current = catalog
  }, [catalog])

  const load = useCallback(
    async (query: URLSearchParams, append = false, signal?: AbortSignal) => {
      const requestedSearch = query.get("q")
      if (
        requestedSearch !== null &&
        (searchLength(requestedSearch.trim()) < 2 ||
          searchLength(requestedSearch) > 100)
      ) {
        setSearchError(
          searchLength(requestedSearch.trim()) < 2
            ? "Digite pelo menos 2 caracteres para buscar."
            : "Use no máximo 100 caracteres na busca.",
        )
        setCatalog({
          games: [],
          nextCursor: null,
          state: "ready",
          commerceUnavailable: false,
          stale: false,
          message: "Corrija o termo de busca para ver os resultados.",
        })
        return
      }
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
            : query.has("q")
              ? `Nenhum título publicado corresponde a “${query.get("q")}”.`
              : "Nenhum jogo publicado corresponde a estes filtros.",
          paginationError: undefined,
        })
        if (
          !query.has("availability") &&
          !query.has("cursor") &&
          !query.has("q")
        )
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
        if (
          !append &&
          !query.has("availability") &&
          !query.has("cursor") &&
          !query.has("q")
        ) {
          const snapshot = readCatalogSnapshot(catalogSnapshotKey(query))
          if (snapshot?.items.length) {
            setCatalog({
              games: snapshot.items.map((item) => ({
                ...item,
                commerce_status: "unavailable",
              })),
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
    setDraftSearch(params.get("q") ?? "")
    setDraftPlatform(params.get("platform") ?? "")
    setDraftGenre(params.get("genre") ?? "")
    setDraftAvailability(params.get("availability") ?? "")
    const search = params.get("q")
    setSearchError(
      search !== null && searchLength(search.trim()) < 2
        ? "Digite pelo menos 2 caracteres para buscar."
        : search !== null && searchLength(search) > 100
          ? "Use no máximo 100 caracteres na busca."
          : "",
    )
  }, [params])

  function submitFilters(event: FormEvent<HTMLFormElement>) {
    event.preventDefault()
    const searchTerm = draftSearch.trim()
    if (
      searchTerm &&
      (searchLength(searchTerm) < 2 || searchLength(searchTerm) > 100)
    ) {
      setSearchError(
        searchLength(searchTerm) < 2
          ? "Digite pelo menos 2 caracteres para buscar."
          : "Use no máximo 100 caracteres na busca.",
      )
      return
    }
    requestSequence.current += 1
    setSearchError("")
    const next = new URLSearchParams()
    if (searchTerm) next.set("q", searchTerm)
    if (draftPlatform.trim()) next.set("platform", draftPlatform.trim())
    if (draftGenre.trim()) next.set("genre", draftGenre.trim())
    if (draftAvailability) next.set("availability", draftAvailability)
    const queryString = next.toString()
    window.history.pushState(
      {},
      "",
      `/catalog${queryString ? `?${queryString}` : ""}`,
    )
    setParams(next)
  }

  function clearFilters() {
    requestSequence.current += 1
    setDraftSearch("")
    window.history.pushState({}, "", "/catalog")
    setParams(new URLSearchParams())
  }

  const selectedPlatform = params.get("platform") ?? ""
  const selectedGenre = params.get("genre") ?? ""
  const selectedAvailability = params.get("availability") ?? ""
  const selectedSearch = params.get("q") ?? ""

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
        <search
          className="catalog-search"
          aria-label="Busca e filtros do catálogo"
        >
          <form className="filter-panel" onSubmit={submitFilters}>
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
                  !selectedSearch &&
                  !draftSearch.trim() &&
                  !draftPlatform.trim() &&
                  !draftGenre.trim() &&
                  !draftAvailability
                }
              >
                Limpar
              </button>
            </div>
            <label htmlFor="filter-search">Buscar título</label>
            <input
              id="filter-search"
              type="search"
              value={draftSearch}
              aria-invalid={searchError ? true : undefined}
              aria-describedby={`catalog-search-hint${searchError ? " catalog-search-error" : ""}`}
              onChange={(event) => {
                setDraftSearch(event.target.value)
                setSearchError("")
              }}
            />
            <p id="catalog-search-hint" className="field-hint">
              Digite de 2 a 100 caracteres. A busca considera acentos, pontuação
              e pequenas variações de grafia.
            </p>
            {searchError && (
              <p
                id="catalog-search-error"
                className="field-error"
                role="status"
              >
                {searchError}
              </p>
            )}
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
            <p className="field-hint filter-hint">
              Busca e filtros também ficam salvos no endereço desta página.
            </p>
            <button className="button-primary apply-filters" type="submit">
              Buscar jogos
            </button>
          </form>
        </search>

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
              <h2 id="results-title">
                {selectedSearch
                  ? `Resultados para “${selectedSearch}”`
                  : "Jogos publicados"}
              </h2>
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
              {(selectedSearch ||
                selectedPlatform ||
                selectedGenre ||
                selectedAvailability) && (
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
  const detailMatch = path.match(/^\/games\/([^/]+)$/)
  const page =
    path === "/catalog" ? (
      <CatalogPage />
    ) : detailMatch ? (
      <GameDetailPage gameId={detailMatch[1]} />
    ) : path === "/" ? (
      <HomePage />
    ) : (
      <main id="content" className="detail-page">
        <section className="detail-state" aria-labelledby="not-found-title">
          <h1 id="not-found-title">Página não encontrada.</h1>
          <a className="button-primary" href="/catalog">
            Abrir catálogo
          </a>
        </section>
      </main>
    )
  const [headerSearch, setHeaderSearch] = useState("")
  const [headerSearchError, setHeaderSearchError] = useState("")

  function submitHeaderSearch(event: FormEvent<HTMLFormElement>) {
    event.preventDefault()
    const input = event.currentTarget.elements.namedItem("q")
    if (!(input instanceof HTMLInputElement)) return

    const searchTerm = input.value.trim()
    const length = searchLength(searchTerm)
    if (length < 2 || length > 100) {
      const message =
        length < 2
          ? "Digite pelo menos 2 caracteres para buscar."
          : "Use no máximo 100 caracteres na busca."
      setHeaderSearchError(message)
      input.setCustomValidity(message)
      input.reportValidity()
      return
    }

    input.setCustomValidity("")
    setHeaderSearchError("")
    const next = new URLSearchParams({ q: searchTerm })
    if (path === "/catalog") {
      const platform = new URLSearchParams(window.location.search).get(
        "platform",
      )
      if (platform?.trim()) next.set("platform", platform.trim())
    }
    window.history.pushState({}, "", `/catalog?${next.toString()}`)
    window.dispatchEvent(new PopStateEvent("popstate"))
  }

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
        <search className="header-search" aria-label="Buscar no catálogo">
          <form onSubmit={submitHeaderSearch}>
            <label className="visually-hidden" htmlFor="header-search">
              Buscar título no catálogo
            </label>
            <input
              id="header-search"
              name="q"
              type="search"
              value={headerSearch}
              placeholder="Buscar título"
              aria-invalid={headerSearchError ? true : undefined}
              aria-describedby={`header-search-hint${headerSearchError ? " header-search-error" : ""}`}
              onChange={(event) => {
                setHeaderSearch(event.target.value)
                setHeaderSearchError("")
                event.currentTarget.setCustomValidity("")
              }}
            />
            <span id="header-search-hint" className="visually-hidden">
              Digite de 2 a 100 caracteres.
            </span>
            {headerSearchError && (
              <span
                id="header-search-error"
                className="visually-hidden"
                role="status"
              >
                {headerSearchError}
              </span>
            )}
            <button className="button-primary" type="submit">
              Buscar
            </button>
          </form>
        </search>
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
