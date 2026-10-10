import { expect, test } from "@playwright/test"

const id = "aaf7123e-4260-4c61-86ea-b18ea7be8a11"
const detail = (
  availability: "none" | "purchase" | "rental" | "both" = "none",
) => ({
  id,
  title: "Chrono Trigger",
  platform: "SNES",
  attributes: {},
  origin_by_attribute: {},
  source: "fixture",
  source_record_id: id,
  version: 1,
  verified_at: "2026-10-10T12:00:00Z",
  cover_attribution: "Sandbox",
  cover_url: `/api/v1/catalog/games/${id}/box-art`,
  commerce_status: "available",
  offers:
    availability === "none"
      ? []
      : [
          ...(availability !== "rental"
            ? [
                {
                  id: `${id}-purchase`,
                  mode: "purchase",
                  price_minor: 4990,
                  currency: "BRL",
                  condition_summary: "Boa",
                  available_units: 1,
                  demo_rank: 1,
                  sandbox: true,
                  sku_code: "SKU-P",
                  units: [],
                },
              ]
            : []),
          ...(availability !== "purchase"
            ? [
                {
                  id: `${id}-rental`,
                  mode: "rental",
                  price_minor: 990,
                  currency: "BRL",
                  condition_summary: "Boa",
                  available_units: 1,
                  demo_rank: 2,
                  sandbox: true,
                  sku_code: "SKU-R",
                  units: [],
                },
              ]
            : []),
        ],
})

for (const scenario of [
  "keyboard and channel consent",
  "stock changed before notice link opens",
  "rental unavailable while purchase remains available",
  "purchase unavailable while rental remains available",
  "Commerce failure does not offer automatic interest",
]) {
  test(scenario, async ({ page }) => {
    let availability: "none" | "purchase" | "rental" | "both" =
      scenario === "stock changed before notice link opens"
        ? "both"
        : scenario.includes("purchase unavailable")
          ? "rental"
          : scenario.includes("rental unavailable")
            ? "purchase"
            : "none"
    await page.route("**/api/v1/system/version", (route) =>
      route.fulfill({ json: { app_version: "test", correlation_id: id } }),
    )
    await page.route(`**/api/v1/catalog/games/${id}`, (route) =>
      route.fulfill({
        json: scenario.includes("Commerce failure")
          ? { ...detail(), commerce_status: "unavailable" }
          : detail(availability),
      }),
    )
    await page.route("**/api/v1/concierge/context-references", (route) =>
      route.fulfill({
        json: {
          reference: "opaque",
          expires_at: new Date(Date.now() + 1800_000).toISOString(),
          telegram_url: "https://t.me/pixel?start=opaque",
        },
      }),
    )
    await page.goto(`/games/${id}`)
    const panel = page.locator(".demand-panel")
    if (scenario.includes("Commerce failure")) {
      await expect(
        page.getByText("Dados comerciais indisponíveis."),
      ).toBeVisible()
      await expect(panel).toHaveCount(0)
    } else if (scenario === "stock changed before notice link opens") {
      await expect(panel).toHaveCount(0)
      availability = "none"
      await page
        .getByRole("button", { name: "Atualizar disponibilidade" })
        .click()
      await expect(panel).toBeVisible()
      await expect(panel).toContainText(
        "Não há reserva, prioridade, garantia de aquisição ou prazo",
      )
    } else if (scenario.includes("remains available")) {
      await expect(panel).toBeVisible()
      await expect(
        panel.getByLabel("Modalidade de interesse").locator("option"),
      ).toHaveCount(1)
      await expect(panel.getByLabel("Modalidade de interesse")).toHaveValue(
        scenario.includes("purchase unavailable") ? "purchase" : "rental",
      )
      await expect(panel).toContainText(
        scenario.includes("purchase unavailable") ? "| compra" : "| aluguel",
      )
    } else {
      await expect(panel).toBeVisible()
      await expect(
        page.getByRole("button", { name: "Preparar conversa com Pixel" }),
      ).toHaveCount(1)
      await expect(panel).toContainText(
        "/demanda Chrono Trigger | SNES | compra",
      )
      await panel.getByLabel("Modalidade de interesse").focus()
      await page.keyboard.press("ArrowDown")
      await expect(panel).toContainText("| aluguel")
      await page.keyboard.press("Tab")
      await page.keyboard.press("Enter")
      await expect(
        panel.getByRole("link", { name: "Continuar no Telegram" }),
      ).toHaveAttribute("href", "https://t.me/pixel?start=opaque")
      await panel.getByRole("button", { name: "Continuar pela Web" }).focus()
      await page.keyboard.press("Enter")
      await expect(panel).toContainText(
        "Para cadastrar ou cancelar um aviso, continue no Telegram",
      )
      await expect(panel).toContainText("/cancelar_demanda")
    }
  })
}
