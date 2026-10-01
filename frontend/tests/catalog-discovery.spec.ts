import type { Page } from "@playwright/test"
import { expect, test } from "@playwright/test"

const gameId = "aaf7123e-4260-4c61-86ea-b18ea7be8a11"

function editorialGame(title: string, id = gameId) {
  return {
    id,
    title,
    platform: "SNES",
    attributes: { title, platform: "SNES", genre: "Aventura" },
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

function offeredGame(title: string, id = gameId) {
  return {
    ...editorialGame(title, id),
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
    .getByRole("article")
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
  await page.getByRole("button", { name: "Aplicar filtros" }).click()

  await expect(page).toHaveURL(
    /\/catalog\?platform=SNES&availability=available$/,
  )
  await expect(
    page.getByRole("heading", { name: "Jogo filtrado" }),
  ).toBeVisible()
  await expect(page.getByText(/49,90/)).toBeVisible()
  const filteredCard = page
    .getByRole("article")
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
