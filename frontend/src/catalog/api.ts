import type {
  GameFacetsResponse,
  GameListResponse,
  GameResponse,
} from "../client"

const apiBase = (import.meta.env.VITE_API_URL ?? "").replace(/\/$/, "")
const cachePrefix = "retrovault:published-catalog:"

export type EditorialGame = Omit<GameResponse, "offers">
export type CatalogSnapshot = {
  items: EditorialGame[]
  savedAt: string
}

export function apiUrl(path: string): string {
  return `${apiBase}${path}`
}

async function getJson<T>(path: string, signal?: AbortSignal): Promise<T> {
  const controller = new AbortController()
  const abortFromCaller = () => controller.abort()
  if (signal?.aborted) controller.abort()
  else signal?.addEventListener("abort", abortFromCaller, { once: true })
  const timeout = window.setTimeout(() => controller.abort(), 10_000)
  try {
    const response = await fetch(apiUrl(path), { signal: controller.signal })
    if (!response.ok) {
      throw new Error(`catalog_request_${response.status}`)
    }
    return (await response.json()) as T
  } finally {
    window.clearTimeout(timeout)
    signal?.removeEventListener("abort", abortFromCaller)
  }
}

export function getCatalogPage(
  params: URLSearchParams,
  signal?: AbortSignal,
): Promise<GameListResponse> {
  const query = new URLSearchParams(params)
  query.set("limit", query.get("limit") ?? "20")
  return getJson<GameListResponse>(
    `/api/v1/catalog/games?${query.toString()}`,
    signal,
  )
}

export function getPopularGames(
  signal?: AbortSignal,
): Promise<GameListResponse> {
  return getCatalogPage(
    new URLSearchParams({
      limit: "3",
      availability: "available",
      sort: "demo_popular",
    }),
    signal,
  )
}

export function getCatalogFacets(
  signal?: AbortSignal,
): Promise<GameFacetsResponse> {
  return getJson<GameFacetsResponse>("/api/v1/catalog/facets", signal)
}

function editorialOnly(game: GameResponse): EditorialGame {
  const copy = { ...game }
  delete copy.offers
  return copy
}

export function writeCatalogSnapshot(key: string, games: GameResponse[]): void {
  try {
    const snapshot: CatalogSnapshot = {
      items: games.map(editorialOnly),
      savedAt: new Date().toISOString(),
    }
    window.localStorage.setItem(
      `${cachePrefix}${key}`,
      JSON.stringify(snapshot),
    )
  } catch {
    // Storage can be unavailable; the current API response remains usable.
  }
}

export function readCatalogSnapshot(key: string): CatalogSnapshot | null {
  try {
    const serialized = window.localStorage.getItem(`${cachePrefix}${key}`)
    if (!serialized) return null
    const value = JSON.parse(serialized) as CatalogSnapshot
    if (
      !Array.isArray(value.items) ||
      typeof value.savedAt !== "string" ||
      !value.items.every(isEditorialGame)
    ) {
      return null
    }
    return value
  } catch {
    return null
  }
}

function isEditorialGame(value: unknown): value is EditorialGame {
  if (typeof value !== "object" || value === null || Array.isArray(value)) {
    return false
  }
  const game = value as Partial<EditorialGame>
  return (
    typeof game.id === "string" &&
    typeof game.title === "string" &&
    typeof game.platform === "string" &&
    typeof game.cover_url === "string" &&
    game.cover_url.startsWith("/api/v1/catalog/games/") &&
    typeof game.attributes === "object" &&
    game.attributes !== null &&
    !Array.isArray(game.attributes)
  )
}

export function catalogSnapshotKey(params: URLSearchParams): string {
  const keyParams = new URLSearchParams(params)
  keyParams.delete("cursor")
  return keyParams.toString() || "default"
}
