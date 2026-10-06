import type {
  GameFacetsResponse,
  GameListResponse,
  GameResponse,
} from "../client"

const apiBase = (import.meta.env.VITE_API_URL ?? "").replace(/\/$/, "")
const cachePrefix = "retrovault:published-catalog:"

export type EditorialGame = Omit<GameResponse, "offers" | "commerce_status">
export type CatalogSnapshot = {
  items: EditorialGame[]
  savedAt: string
}

export function apiUrl(path: string): string {
  return `${apiBase}${path}`
}

export class CatalogApiError extends Error {
  constructor(
    message: string,
    readonly status: number,
  ) {
    super(message)
  }
}

async function getJson<T>(
  path: string,
  signal?: AbortSignal,
  cache?: RequestCache,
): Promise<T> {
  const controller = new AbortController()
  const abortFromCaller = () => controller.abort()
  if (signal?.aborted) controller.abort()
  else signal?.addEventListener("abort", abortFromCaller, { once: true })
  const timeout = window.setTimeout(() => controller.abort(), 10_000)
  try {
    const response = await fetch(apiUrl(path), {
      signal: controller.signal,
      cache,
    })
    if (!response.ok) {
      throw new CatalogApiError(
        `catalog_request_${response.status}`,
        response.status,
      )
    }
    return (await response.json()) as T
  } finally {
    window.clearTimeout(timeout)
    signal?.removeEventListener("abort", abortFromCaller)
  }
}

export function getGameDetails(
  gameId: string,
  signal?: AbortSignal,
): Promise<GameResponse> {
  return getJson<GameResponse>(
    `/api/v1/catalog/games/${encodeURIComponent(gameId)}`,
    signal,
    "no-store",
  )
}

export type PixelEntryResponse = {
  reference: string | null
  expires_at: string | null
  whatsapp_url: string
  web_whatsapp_url: string
}

export async function createPixelEntry(
  gameId?: string,
): Promise<PixelEntryResponse> {
  const controller = new AbortController()
  const timeout = window.setTimeout(() => controller.abort(), 10_000)
  try {
    const response = await fetch(
      apiUrl("/api/v1/concierge/context-references"),
      {
        method: "POST",
        cache: "no-store",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(gameId ? { game_id: gameId } : {}),
        signal: controller.signal,
      },
    )
    if (!response.ok) {
      throw new CatalogApiError(
        `pixel_entry_request_${response.status}`,
        response.status,
      )
    }
    return (await response.json()) as PixelEntryResponse
  } finally {
    window.clearTimeout(timeout)
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
  const { offers, commerce_status, ...editorial } = game
  void offers
  void commerce_status
  return editorial
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
