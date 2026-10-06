import AxeBuilder from "@axe-core/playwright"
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
    sku_code: `SKU-${mode}`,
    units: [],
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

function detailGame(title: string, offers: unknown[] = [offer("purchase")]) {
  return {
    ...editorialGame(title),
    commerce_status: "available",
    offers,
  }
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

test("Home ignora facetas antigas quando a retentativa termina primeiro", async ({
  page,
}) => {
  await mockVersion(page)
  let retryStarted = false
  let releaseOldFacets: (() => void) | undefined
  const oldFacets = new Promise<void>((resolve) => {
    releaseOldFacets = resolve
  })
  await page.route("**/api/v1/catalog/facets", async (route) => {
    if (!retryStarted) {
      await oldFacets
      try {
        await route.fulfill({ json: { platforms: ["SNES"], genres: [] } })
      } catch {
        // The retry aborts this older request.
      }
      return
    }
    await route.fulfill({ json: { platforms: ["N64"], genres: [] } })
  })
  await page.route("**/api/v1/catalog/games?*", (route) => {
    const url = new URL(route.request().url())
    if (url.searchParams.get("sort") !== "demo_popular") {
      return route.fulfill({ json: listResponse([]) })
    }
    return retryStarted
      ? route.fulfill({ json: listResponse([offeredGame("Novo popular")]) })
      : route.fulfill({ status: 503, body: "" })
  })

  await page.goto("/")
  await page.getByRole("button", { name: "Tentar novamente" }).waitFor()
  retryStarted = true
  await page.getByRole("button", { name: "Tentar novamente" }).click()
  await expect(
    page.getByRole("link", { name: "N64", exact: true }),
  ).toBeVisible()
  await expect(
    page.getByRole("heading", { name: "Novo popular" }),
  ).toBeVisible()
  releaseOldFacets?.()
  await expect(
    page.getByRole("link", { name: "N64", exact: true }),
  ).toBeVisible()
  await expect(
    page.getByRole("link", { name: "SNES", exact: true }),
  ).toHaveCount(0)
})

test("Detalhe mostra ofertas mistas e os fatos das unidades físicas", async ({
  page,
}) => {
  await mockVersion(page)
  const purchase = {
    ...offer("purchase"),
    available_units: 1,
    units: [
      {
        condition_summary: "Muito bom",
        defects: ["Risco no estojo"],
        included_items: ["Cartucho", "Manual"],
      },
    ],
  }
  const rental = { ...offer("rental"), available_units: 0, units: [] }
  await page.route(`**/api/v1/catalog/games/${gameId}`, (route) =>
    route.fulfill({ json: detailGame("Jogo detalhado", [purchase, rental]) }),
  )
  await page.route(`**/api/v1/catalog/games/${gameId}/box-art`, (route) =>
    route.fulfill({ status: 404, body: "" }),
  )

  await page.goto(`/games/${gameId}`)

  await expect(
    page.getByRole("heading", { name: "Jogo detalhado" }),
  ).toBeVisible()
  await expect(page.getByText("1 unidade disponível")).toBeVisible()
  await expect(page.getByText("Indisponível no momento")).toBeVisible()
  await expect(page.getByText("Risco no estojo")).toBeVisible()
  await expect(page.getByText("Cartucho, Manual")).toBeVisible()
})

test("Detalhe atualiza disponibilidade, oculta preços no erro e trata 404", async ({
  page,
}) => {
  await mockVersion(page)
  let failRefresh: "none" | "unavailable" | "missing" = "none"
  await page.route(`**/api/v1/catalog/games/${gameId}`, (route) => {
    if (failRefresh === "unavailable")
      return route.fulfill({ status: 503, body: "" })
    if (failRefresh === "missing")
      return route.fulfill({ status: 404, body: "" })
    return route.fulfill({ json: detailGame("Jogo atualizado") })
  })
  await page.route(`**/api/v1/catalog/games/${gameId}/box-art`, (route) =>
    route.fulfill({ status: 404, body: "" }),
  )

  await page.goto(`/games/${gameId}`)
  await expect(
    page.getByRole("heading", { name: "Jogo atualizado" }),
  ).toBeVisible()
  failRefresh = "unavailable"
  await page.getByRole("button", { name: "Atualizar disponibilidade" }).click()
  await expect(
    page.getByText("Não foi possível atualizar os detalhes do jogo.", {
      exact: true,
    }),
  ).toBeVisible()
  await expect(
    page.getByText(
      /Não é possível confirmar condição, preço ou disponibilidade agora/,
    ),
  ).toBeVisible()
  await expect(page.getByText(/49,90/)).toHaveCount(0)

  failRefresh = "missing"
  await page.getByRole("button", { name: "Atualizar disponibilidade" }).click()
  await expect(
    page.getByRole("heading", { name: "Jogo não encontrado." }),
  ).toBeVisible()
})

test("Detalhe ignora resposta de refresh antiga que chega depois da nova", async ({
  page,
}) => {
  await mockVersion(page)
  await page.addInitScript(() => {
    const original = window.setInterval
    window.setInterval = new Proxy(original, {
      apply(target, thisArg, args: Parameters<typeof window.setInterval>) {
        if (args[1] === 30_000) Reflect.set(window, "__detailRefresh", args[0])
        return Reflect.apply(target, thisArg, args)
      },
    })
  })
  let raceMode = false
  let raceCalls = 0
  let releaseOld: (() => void) | undefined
  const oldResponse = new Promise<void>((resolve) => {
    releaseOld = resolve
  })
  await page.route(`**/api/v1/catalog/games/${gameId}`, async (route) => {
    if (!raceMode) return route.fulfill({ json: detailGame("Jogo", [offer()]) })
    raceCalls += 1
    if (raceCalls === 1) {
      await oldResponse
      return route.fulfill({
        json: detailGame("Jogo", [{ ...offer(), available_units: 0 }]),
      })
    }
    return route.fulfill({
      json: detailGame("Jogo", [{ ...offer(), available_units: 4 }]),
    })
  })
  await page.route(`**/api/v1/catalog/games/${gameId}/box-art`, (route) =>
    route.fulfill({ status: 404, body: "" }),
  )

  await page.goto(`/games/${gameId}`)
  await expect(page.getByText("2 unidades disponíveis")).toBeVisible()
  raceMode = true
  await page.evaluate(() => {
    const refresh = Reflect.get(window, "__detailRefresh")
    if (typeof refresh === "function") refresh()
  })
  await expect.poll(() => raceCalls).toBe(1)
  await page.evaluate(() => {
    const refresh = Reflect.get(window, "__detailRefresh")
    if (typeof refresh === "function") refresh()
  })
  await expect(page.getByText("4 unidades disponíveis")).toBeVisible()
  releaseOld?.()
  await expect(page.getByText("4 unidades disponíveis")).toBeVisible()
  await expect(page.getByText("Indisponível no momento")).toHaveCount(0)
})

test("Pixel prepara referência contextual e oferece os dois destinos WhatsApp", async ({
  page,
}) => {
  await mockVersion(page)
  await page.route(`**/api/v1/catalog/games/${gameId}`, (route) =>
    route.fulfill({ json: detailGame("Jogo para Pixel") }),
  )
  await page.route(`**/api/v1/catalog/games/${gameId}/box-art`, (route) =>
    route.fulfill({ status: 404, body: "" }),
  )
  let body: unknown
  await page.route("**/api/v1/concierge/context-references", async (route) => {
    body = route.request().postDataJSON()
    await route.fulfill({
      status: 201,
      json: {
        reference: "v1.context.signature",
        expires_at: "2099-10-01T12:00:00Z",
        whatsapp_url: "https://wa.me/5500000000000?text=Pixel",
        web_whatsapp_url:
          "https://web.whatsapp.com/send?phone=5500000000000&text=Pixel",
      },
    })
  })

  await page.goto(`/games/${gameId}`)
  await page
    .getByRole("button", { name: "Preparar conversa com Pixel" })
    .click()

  await expect(page.getByText("Contexto do jogo preparado.")).toBeVisible()
  expect(body).toEqual({ game_id: gameId })
  await expect(
    page.getByRole("link", { name: "Continuar no WhatsApp" }),
  ).toHaveAttribute("href", /wa\.me/)
  await expect(
    page.getByRole("link", { name: "Usar WhatsApp Web" }),
  ).toHaveAttribute("href", /web\.whatsapp\.com/)
})

test("Pixel prepara conversa global sem anexar jogo", async ({ page }) => {
  await mockVersion(page)
  await page.route("**/api/v1/catalog/facets", (route) =>
    route.fulfill({ json: { platforms: [], genres: [] } }),
  )
  await page.route("**/api/v1/catalog/games?*", (route) =>
    route.fulfill({ json: listResponse([]) }),
  )
  let body: unknown
  await page.route("**/api/v1/concierge/context-references", async (route) => {
    body = route.request().postDataJSON()
    await route.fulfill({
      status: 201,
      json: {
        reference: null,
        expires_at: null,
        whatsapp_url: "https://wa.me/5500000000000?text=Pixel",
        web_whatsapp_url:
          "https://web.whatsapp.com/send?phone=5500000000000&text=Pixel",
      },
    })
  })

  await page.goto("/")
  await page
    .getByRole("button", { name: "Preparar conversa com Pixel" })
    .click()

  await expect(page.getByText("Conversa preparada.")).toBeVisible()
  expect(body).toEqual({})
})

test("Detalhe mantém acessibilidade automatizada em viewport estreita e forced colors", async ({
  page,
}) => {
  await mockVersion(page)
  await page.setViewportSize({ width: 320, height: 900 })
  await page.route(`**/api/v1/catalog/games/${gameId}`, (route) =>
    route.fulfill({ json: detailGame("Jogo acessível") }),
  )
  await page.route(`**/api/v1/catalog/games/${gameId}/box-art`, (route) =>
    route.fulfill({ status: 404, body: "" }),
  )

  await page.goto(`/games/${gameId}`)
  await expect(
    page.getByRole("heading", { name: "Jogo acessível" }),
  ).toBeVisible()
  await page.keyboard.press("Tab")
  await expect(
    page.getByRole("link", { name: "Pular para o conteúdo" }),
  ).toBeFocused()
  const accessibility = await new AxeBuilder({ page }).analyze()
  expect(accessibility.violations).toEqual([])
  const narrowLayout = await page.evaluate(() => ({
    scrollWidth: document.documentElement.scrollWidth,
    clientWidth: document.documentElement.clientWidth,
  }))
  expect(narrowLayout.scrollWidth).toBeLessThanOrEqual(narrowLayout.clientWidth)
  await page.emulateMedia({ forcedColors: "active" })
  expect(
    await page.evaluate(
      () => window.matchMedia("(forced-colors: active)").matches,
    ),
  ).toBe(true)
  const updateButton = page.getByRole("button", {
    name: "Atualizar disponibilidade",
  })
  await expect(updateButton).toBeVisible()
  const forcedColors = await updateButton.evaluate((button) => {
    const resolveSystemColor = (value: string) => {
      const probe = document.createElement("span")
      probe.style.color = value
      document.body.append(probe)
      const color = getComputedStyle(probe).color
      probe.remove()
      return color
    }
    const style = getComputedStyle(button)
    const availableStatus = document.querySelector(".availability.available")
    return {
      borderColor: style.borderTopColor,
      canvasText: resolveSystemColor("CanvasText"),
      outlineColor: style.outlineColor,
      highlight: resolveSystemColor("Highlight"),
      outlineWidth: style.outlineWidth,
      statusColor: availableStatus
        ? getComputedStyle(availableStatus).color
        : "",
      linkText: resolveSystemColor("LinkText"),
    }
  })
  expect(forcedColors.borderColor).toBe(forcedColors.canvasText)
  expect(forcedColors.statusColor).toBe(forcedColors.linkText)
  await updateButton.focus()
  const focusOutline = await updateButton.evaluate((button) => {
    const style = getComputedStyle(button)
    return { color: style.outlineColor, width: style.outlineWidth }
  })
  expect(focusOutline).toEqual({
    color: forcedColors.highlight,
    width: "3px",
  })
})
