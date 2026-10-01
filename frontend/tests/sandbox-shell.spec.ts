import AxeBuilder from "@axe-core/playwright"
import { expect, test } from "@playwright/test"

test("sandbox shell is keyboard accessible and versioned", async ({ page }) => {
  await page.route("**/api/v1/system/version", async (route) => {
    await route.fulfill({
      json: {
        app_version: "api-verified-1.1",
        correlation_id: "1f4bfe4d-6a71-4d78-97d2-d4c481cc7bd7",
      },
    })
  })
  await page.goto("/")
  await expect(
    page.getByText("Experiência demonstrativa. Nenhuma compra real."),
  ).toBeVisible()
  await page.keyboard.press("Tab")
  await expect(
    page.getByRole("link", { name: "Pular para o conteúdo" }),
  ).toBeFocused()
  await expect(
    page.getByRole("heading", { name: /Clássicos preservados/ }),
  ).toBeVisible()
  await expect(page.getByRole("contentinfo")).toContainText(
    "APP_VERSION api-verified-1.1",
  )
  const accessibility = await new AxeBuilder({ page }).analyze()
  expect(accessibility.violations).toEqual([])
})

test("shell renderiza a versão real da API no Compose", async ({ page }) => {
  test.skip(!process.env.PLAYWRIGHT_REAL_API, "requer a pilha Compose")
  const versionResponse = page.waitForResponse(
    (response) =>
      new URL(response.url()).pathname === "/api/v1/system/version" &&
      response.request().resourceType() === "fetch",
  )
  await page.goto("/")
  const apiResponse = await versionResponse
  expect(apiResponse.ok()).toBe(true)
  const { app_version } = (await apiResponse.json()) as { app_version: string }

  await expect(page.getByText(app_version, { exact: true })).toBeVisible()
  await expect(page.getByText(`APP_VERSION ${app_version}`)).toBeVisible()
})

test("shell does not overflow at 320 CSS px", async ({ page }) => {
  await page.setViewportSize({ width: 320, height: 720 })
  await page.goto("/")
  const dimensions = await page.evaluate(() => ({
    width: document.documentElement.scrollWidth,
    viewport: document.documentElement.clientWidth,
  }))
  expect(dimensions.width).toBeLessThanOrEqual(dimensions.viewport)
})
