import type { Page } from "@playwright/test"
import { expect, test } from "@playwright/test"

const gameId = "aaf7123e-4260-4c61-86ea-b18ea7be8a11"

function editorialGame(
  title: string,
  id = gameId,
  metadata: { region?: string; edition?: string } = {},
) {
  return {
    id,
    title,
    platform: "SNES",
    attributes: { title, platform: "SNES", genre: "Aventura", ...metadata },
    origin_by_attribute: {},
    source: "test-fixture",
    source_record_id: id,
    version: 1,
    verified_at: "2026-10-01T12:00:00+00:00",
    cover_attribution: "Capa demonstrativa",
    cover_url: `/api/v1/catalog/games/${id}/box-art`,
  }
}

function offer(mode: "purchase" | "rental" = "purchase") {
  return {
    id:
      mode === "purchase"
        ? "f1679124-65fe-4e99-8d45-6bc671234abc"
        : "e1679124-65fe-4e99-8d45-6bc671234abc",
    mode,
    price_minor: mode === "purchase" ? 4990 : 990,
    currency: "BRL",
    condition_summary: "Condição demonstrativa Sandbox.",
    available_units: 2,
    demo_rank: 1,
    sandbox: true,
  }
}

function offeredGame(
  title: string,
  id = gameId,
  metadata: { region?: string; edition?: string } = {},
) {
  return {
    ...editorialGame(title, id, metadata),
    offers: [offer("purchase"), offer("rental")],
  }
}

function listResponse(items: unknown[], next_cursor: string | null = null) {
  return { items, next_cursor, commerce_status: "available" }
}

async function mockVersion(page: Page) {
  await page.route("**/api/v1/system/version", (route) =>
    route.fulfill({
      json: {
        app_version: "test-version",
        correlation_id: "1f4bfe4d-6a71-4d78-97d2-d4c481cc7bd7",
      },
    }),
  )
}

test("Home apresenta populares e plataformas com fatos Sandbox visíveis", async ({
  page,
}) => {
  await mockVersion(page)
  await page.route("**/api/v1/catalog/facets", (route) =>
    route.fulfill({ json: { platforms: ["SNES"], genres: ["Aventura"] } }),
  )
  await page.route("**/api/v1/catalog/games?*", (route) => {
    const requestUrl = new URL(route.request().url())
    const items =
      requestUrl.searchParams.get("sort") === "demo_popular"
        ? [offeredGame("Jogo popular")]
        : []
    return route.fulfill({ json: listResponse(items) })
  })

  await page.goto("/")

  const popularHeading = page.getByRole("heading", {
    name: "Populares disponíveis agora",
  })
  const platformsHeading = page.getByRole("heading", { name: "Plataformas" })
  await expect(popularHeading).toBeVisible()
  await expect(platformsHeading).toBeVisible()
  await expect(
    page.getByRole("heading", { name: "Jogo popular" }),
  ).toBeVisible()
  await expect(page.getByText(/49,90/)).toBeVisible()
  const popularCard = page
    .getByRole("link")
    .filter({ has: page.getByRole("heading", { name: "Jogo popular" }) })
  await expect(
    popularCard
      .getByText("Condição demonstrativa Sandbox.", { exact: true })
      .first(),
  ).toBeVisible()
  await expect(
    page.getByRole("link", { name: "SNES", exact: true }),
  ).toHaveAttribute("href", "/catalog?platform=SNES")

  const popularBeforePlatforms = await popularHeading.evaluate((element) =>
    Boolean(
      element.compareDocumentPosition(
        document.querySelector("#platform-title")!,
      ) & Node.DOCUMENT_POSITION_FOLLOWING,
    ),
  )
  expect(popularBeforePlatforms).toBe(true)
  const popularBox = await popularHeading.boundingBox()
  expect(popularBox?.y).toBeLessThan(page.viewportSize()!.height)
})

test("Catálogo sincroniza filtros com a URL e mostra ofertas nos resultados", async ({
  page,
}) => {
  await mockVersion(page)
  let filteredRequest: URL | undefined
  await page.route("**/api/v1/catalog/games?*", (route) => {
    const requestUrl = new URL(route.request().url())
    const isFiltered =
      requestUrl.searchParams.get("platform") === "SNES" &&
      requestUrl.searchParams.get("availability") === "available"
    if (isFiltered) filteredRequest = requestUrl
    return route.fulfill({
      json: listResponse(isFiltered ? [offeredGame("Jogo filtrado")] : []),
    })
  })

  await page.goto("/catalog")
  await page.getByLabel("Plataforma").fill("SNES")
  await page.getByLabel("Disponibilidade").selectOption("available")
  await page.getByRole("button", { name: "Buscar jogos" }).click()

  await expect(page).toHaveURL(
    /\/catalog\?platform=SNES&availability=available$/,
  )
  await expect(
    page.getByRole("heading", { name: "Jogo filtrado" }),
  ).toBeVisible()
  await expect(page.getByText(/49,90/)).toBeVisible()
  const filteredCard = page
    .getByRole("link")
    .filter({ has: page.getByRole("heading", { name: "Jogo filtrado" }) })
  await expect(
    filteredCard
      .getByText("Condição demonstrativa Sandbox.", { exact: true })
      .first(),
  ).toBeVisible()
  await expect(
    filteredCard.getByText("Disponível", { exact: true }).first(),
  ).toBeVisible()
  expect(filteredRequest?.searchParams.get("platform")).toBe("SNES")
  expect(filteredRequest?.searchParams.get("availability")).toBe("available")
})

test("Catálogo explica resultado vazio e permite limpar filtros", async ({
  page,
}) => {
  await mockVersion(page)
  await page.route("**/api/v1/catalog/games?*", (route) =>
    route.fulfill({ json: listResponse([]) }),
  )

  await page.goto("/catalog?genre=Sem%20resultados")

  await expect(
    page.getByText("Nenhum jogo publicado corresponde a estes filtros."),
  ).toBeVisible()
  await page.getByRole("button", { name: "Limpar", exact: true }).click()
  await expect(page).toHaveURL("/catalog")
})

test("Catálogo permite tentar novamente ao exibir snapshot offline", async ({
  page,
}) => {
  await mockVersion(page)
  const snapshotGame = editorialGame("Jogo salvo")
  const currentGame = offeredGame("Jogo atualizado")
  await page.addInitScript(
    ({ key, snapshot }) => {
      localStorage.setItem(key, JSON.stringify(snapshot))
    },
    {
      key: "retrovault:published-catalog:default",
      snapshot: {
        items: [snapshotGame],
        savedAt: "2026-10-01T12:00:00.000Z",
      },
    },
  )
  let allowRecovery = false
  await page.route("**/api/v1/catalog/games?*", (route) => {
    if (!allowRecovery) {
      return route.fulfill({
        status: 503,
        contentType: "application/problem+json",
        body: JSON.stringify({ status: 503, code: "commerce_unavailable" }),
      })
    }
    return route.fulfill({ json: listResponse([currentGame]) })
  })

  await page.goto("/catalog")
  await expect(
    page
      .locator(".stale-note")
      .getByText(/Exibindo dados editoriais salvos em/),
  ).toBeVisible()
  await expect(page.getByRole("heading", { name: "Jogo salvo" })).toBeVisible()
  await expect(
    page.getByText("Preço e disponibilidade temporariamente indisponíveis."),
  ).toBeVisible()
  allowRecovery = true
  await page.getByRole("button", { name: "Tentar atualizar" }).click()

  await expect(
    page.getByRole("heading", { name: "Jogo atualizado" }),
  ).toBeVisible()
  await expect(page.getByText(/49,90/)).toBeVisible()
  await expect(page.locator(".stale-note")).toHaveCount(0)
})

test("Busca do cabeçalho preserva termo e plataforma e mostra região e edição", async ({
  page,
}) => {
  await mockVersion(page)
  let requestedSearch: URL | undefined
  await page.route("**/api/v1/catalog/games?*", (route) => {
    const requestUrl = new URL(route.request().url())
    if (requestUrl.searchParams.has("q")) requestedSearch = requestUrl
    const matches =
      requestUrl.searchParams.get("q") === "Pokemon-Stadium" &&
      (!requestUrl.searchParams.has("platform") ||
        requestUrl.searchParams.get("platform") === "SNES")
    return route.fulfill({
      json: listResponse(
        matches
          ? [
              offeredGame("Pokemon Stadium", gameId, {
                region: "NTSC-J",
                edition: "Player's Choice",
              }),
            ]
          : [],
      ),
    })
  })

  await page.goto("/catalog?platform=SNES")
  await page.getByLabel("Buscar título no catálogo").fill("Pokemon-Stadium")
  await page.getByLabel("Buscar título no catálogo").press("Enter")

  await expect(page).toHaveURL(/\/catalog\?q=Pokemon-Stadium&platform=SNES$/)
  await expect(
    page.getByRole("heading", { name: "Resultados para “Pokemon-Stadium”" }),
  ).toBeVisible()
  const result = page.getByRole("link", {
    name: "Pokemon Stadium, SNES",
  })
  await expect(result.getByText("SNES · Aventura")).toBeVisible()
  await expect(
    result.getByText("Região: NTSC-J · Edição: Player's Choice"),
  ).toBeVisible()

  await page.getByLabel("Plataforma").fill("SNES")
  await page.getByRole("button", { name: "Buscar jogos" }).click()
  await expect(page).toHaveURL(/\/catalog\?q=Pokemon-Stadium&platform=SNES$/)
  expect(requestedSearch?.searchParams.get("q")).toBe("Pokemon-Stadium")
  expect(requestedSearch?.searchParams.get("platform")).toBe("SNES")
})

test("Paginação da busca mantém o termo e envia o cursor seguinte", async ({
  page,
}) => {
  await mockVersion(page)
  let secondPageRequest: URL | undefined
  await page.route("**/api/v1/catalog/games?*", (route) => {
    const requestUrl = new URL(route.request().url())
    if (requestUrl.searchParams.get("cursor") === "search-page-2") {
      secondPageRequest = requestUrl
      return route.fulfill({
        json: listResponse(
          [
            offeredGame(
              "Sonic resultado 2",
              "baf7123e-4260-4c61-86ea-b18ea7be8a12",
            ),
          ],
          null,
        ),
      })
    }
    return route.fulfill({
      json: listResponse([offeredGame("Sonic resultado 1")], "search-page-2"),
    })
  })

  await page.goto("/catalog?q=sonic")
  await expect(
    page.getByRole("heading", { name: "Sonic resultado 1" }),
  ).toBeVisible()
  await page.getByRole("button", { name: "Carregar mais jogos" }).click()

  await expect(
    page.getByRole("heading", { name: "Sonic resultado 2" }),
  ).toBeVisible()
  await expect(page).toHaveURL(/\/catalog\?q=sonic&cursor=search-page-2$/)
  expect(secondPageRequest?.searchParams.get("q")).toBe("sonic")
  expect(secondPageRequest?.searchParams.get("cursor")).toBe("search-page-2")
})

test("Busca curta preserva o valor e não envia consulta; termo especial pode retornar vazio", async ({
  page,
}) => {
  await mockVersion(page)
  const requestedTerms: string[] = []
  await page.route("**/api/v1/catalog/games?*", (route) => {
    const requestUrl = new URL(route.request().url())
    const term = requestUrl.searchParams.get("q")
    if (term !== null) requestedTerms.push(term)
    return route.fulfill({ json: listResponse([]) })
  })

  await page.goto("/catalog?q=a")
  await expect(page.getByLabel("Buscar título", { exact: true })).toHaveValue(
    "a",
  )
  await expect(
    page.getByText("Digite pelo menos 2 caracteres para buscar."),
  ).toBeVisible()
  await page.getByRole("button", { name: "Buscar jogos" }).click()
  await expect(page).toHaveURL("/catalog?q=a")
  expect(requestedTerms).toEqual([])

  await page.goto("/catalog?q=%27%20OR%201%3D1%20--")
  await expect(
    page.getByText(/Nenhum título publicado corresponde a/),
  ).toBeVisible()
  expect(requestedTerms.length).toBeGreaterThan(0)
  expect(new Set(requestedTerms)).toEqual(new Set(["' OR 1=1 --"]))
})

test("Busca mantém termo e foco lógico diante de falha e oferece retentativa", async ({
  page,
}) => {
  await mockVersion(page)
  let allowRecovery = false
  await page.route("**/api/v1/catalog/games?*", (route) => {
    if (!allowRecovery) {
      return route.fulfill({ status: 503, body: "" })
    }
    return route.fulfill({
      json: listResponse([offeredGame("Jogo recuperado")]),
    })
  })

  await page.goto("/catalog?q=sonic")
  await expect(
    page.getByText(
      "Não foi possível carregar o catálogo. Verifique sua conexão e tente novamente.",
    ),
  ).toBeVisible()
  await expect(page.getByLabel("Buscar título", { exact: true })).toHaveValue(
    "sonic",
  )
  allowRecovery = true
  await page.getByRole("button", { name: "Tentar novamente" }).click()
  await expect(
    page.getByRole("heading", { name: "Jogo recuperado" }),
  ).toBeVisible()
  await expect(page).toHaveURL("/catalog?q=sonic")
})

test("Busca do cabeçalho rejeita espaços sem navegar e mantém foco para correção", async ({
  page,
}) => {
  await mockVersion(page)
  const requestedTerms: string[] = []
  await page.route("**/api/v1/catalog/games?*", (route) => {
    const term = new URL(route.request().url()).searchParams.get("q")
    if (term !== null) requestedTerms.push(term)
    return route.fulfill({ json: listResponse([]) })
  })

  await page.goto("/catalog?platform=SNES")
  const search = page.getByLabel("Buscar título no catálogo")
  await search.fill("  ")
  await page.getByRole("button", { name: "Buscar", exact: true }).click()

  await expect(page).toHaveURL("/catalog?platform=SNES")
  await expect(search).toBeFocused()
  await expect(page.locator("#header-search-error")).toHaveText(
    "Digite pelo menos 2 caracteres para buscar.",
  )
  expect(requestedTerms).toEqual([])
})

test("Busca aplica o limite de 100 caracteres Unicode, não unidades UTF-16", async ({
  page,
}) => {
  await mockVersion(page)
  const requestedTerms: string[] = []
  await page.route("**/api/v1/catalog/games?*", (route) => {
    const term = new URL(route.request().url()).searchParams.get("q")
    if (term !== null) requestedTerms.push(term)
    return route.fulfill({ json: listResponse([]) })
  })
  const astralTitle = String.fromCodePoint(0x10400).repeat(51)

  await page.goto(`/catalog?q=${encodeURIComponent(astralTitle)}`)

  await expect(
    page.getByText(/Nenhum título publicado corresponde a/),
  ).toBeVisible()
  expect(new Set(requestedTerms)).toEqual(new Set([astralTitle]))
  await expect(page.locator("#catalog-search-error")).toHaveCount(0)
})

test("Busca longa preserva o texto e orienta a correção", async ({ page }) => {
  await mockVersion(page)
  const requestedTerms: string[] = []
  await page.route("**/api/v1/catalog/games?*", (route) => {
    const term = new URL(route.request().url()).searchParams.get("q")
    if (term !== null) requestedTerms.push(term)
    return route.fulfill({ json: listResponse([]) })
  })
  const longTerm = "x".repeat(101)

  await page.goto("/catalog")
  const catalogSearch = page.getByLabel("Buscar título", { exact: true })
  await catalogSearch.fill(longTerm)
  await page.getByRole("button", { name: "Buscar jogos" }).click()
  await expect(catalogSearch).toHaveValue(longTerm)
  await expect(
    page.getByText("Use no máximo 100 caracteres na busca.").first(),
  ).toBeVisible()
  await expect(page).toHaveURL("/catalog")

  const headerSearch = page.getByLabel("Buscar título no catálogo")
  await headerSearch.fill(longTerm)
  await page.getByRole("button", { name: "Buscar", exact: true }).click()
  await expect(headerSearch).toHaveValue(longTerm)
  await expect(headerSearch).toHaveAttribute("aria-invalid", "true")
  await expect(page.locator("#header-search-error")).toHaveText(
    "Use no máximo 100 caracteres na busca.",
  )
  await expect(page).toHaveURL("/catalog")
  expect(requestedTerms).toEqual([])
})

test("Busca curta não invalida a consulta válida que já está carregando", async ({
  page,
}) => {
  await mockVersion(page)
  let delayedSearches = 0
  await page.route("**/api/v1/catalog/games?*", async (route) => {
    const term = new URL(route.request().url()).searchParams.get("q")
    if (term === "sonic") {
      delayedSearches += 1
      await new Promise((resolve) => setTimeout(resolve, 200))
      try {
        await route.fulfill({
          json: listResponse([offeredGame("Sonic carregado")]),
        })
      } catch {
        // React StrictMode can abort its first development-only request.
      }
      return
    }
    return route.fulfill({ json: listResponse([]) })
  })

  await page.goto("/catalog")
  const search = page.getByLabel("Buscar título", { exact: true })
  await search.fill("sonic")
  await page.getByRole("button", { name: "Buscar jogos" }).click()
  await expect.poll(() => delayedSearches).toBeGreaterThan(0)
  await search.fill("a")
  await page.getByRole("button", { name: "Buscar jogos" }).click()

  await expect(page).toHaveURL("/catalog?q=sonic")
  await expect(
    page.getByRole("heading", { name: "Sonic carregado" }),
  ).toBeVisible()
  await expect(
    page.getByText("Digite pelo menos 2 caracteres para buscar."),
  ).toBeVisible()
})
