import { execFileSync } from "node:child_process"
import { mkdirSync, writeFileSync } from "node:fs"
import { dirname, resolve } from "node:path"
import { chromium, type Page } from "@playwright/test"

declare global {
  interface Window {
    __retroVaultLargestContentfulPaint?: number | null
  }
}

const SAMPLE_COUNT = 30
const WARMUP_COUNT = 1
const VIEWPORT = { width: 1365, height: 900 }
const TARGET_MS = 2500
const DEFAULT_FRONTEND_URL = "http://127.0.0.1:4173"
const DEFAULT_API_URL = "http://127.0.0.1:8000"

type Sample = { navigation: number; lcp_ms: number | null; error?: string }
type RouteReport = {
  route: string
  warmup_navigations: number
  sample_count: number
  successful_samples: number
  missing_samples: number
  p75_ms: number | null
  local_threshold_ms: number
  status: "inconclusive" | "within_local_threshold" | "above_local_threshold"
  samples: Sample[]
}

function requireLoopback(value: string, label: string): URL {
  const url = new URL(value)
  if (!new Set(["127.0.0.1", "localhost", "[::1]"]).has(url.hostname)) {
    throw new Error(`${label}_must_be_loopback`)
  }
  return url
}

function percentile75(values: number[]): number | null {
  if (values.length === 0) return null
  const ordered = [...values].sort((left, right) => left - right)
  return ordered[Math.ceil(ordered.length * 0.75) - 1]
}

async function waitForRenderedPage(page: Page, url: string): Promise<void> {
  await page.goto(url, { waitUntil: "networkidle", timeout: 30_000 })
  await page.evaluate(async () => {
    await document.fonts.ready
    await new Promise<void>((resolveFrame) =>
      requestAnimationFrame(() => requestAnimationFrame(() => resolveFrame())),
    )
  })
  await page.waitForTimeout(300)
}

async function main(): Promise<void> {
  const frontendUrl = requireLoopback(
    process.env.PLAYWRIGHT_BASE_URL ?? DEFAULT_FRONTEND_URL,
    "frontend_url",
  )
  const apiUrl = requireLoopback(
    process.env.LCP_API_URL ?? DEFAULT_API_URL,
    "api_url",
  )
  const catalogResponse = await fetch(
    new URL("/api/v1/catalog/games?limit=1", apiUrl),
    { headers: { Accept: "application/json" } },
  )
  if (!catalogResponse.ok)
    throw new Error(`catalog_http_${catalogResponse.status}`)
  const catalog = (await catalogResponse.json()) as {
    items?: Array<{ id?: string }>
  }
  const gameId = process.env.LCP_GAME_ID ?? catalog.items?.[0]?.id ?? null
  const routes = [
    "/",
    "/catalog",
    ...(gameId ? [`/games/${encodeURIComponent(gameId)}`] : []),
  ]
  const versionResponse = await fetch(
    new URL("/api/v1/system/version", apiUrl),
    {
      headers: { Accept: "application/json" },
    },
  )
  const versionPayload = versionResponse.ok
    ? ((await versionResponse.json()) as { app_version?: string })
    : {}
  const browser = await chromium.launch({ headless: true })
  const context = await browser.newContext({
    viewport: VIEWPORT,
    deviceScaleFactor: 1,
    reducedMotion: "reduce",
    serviceWorkers: "block",
  })
  const routeReports: RouteReport[] = []

  try {
    for (const route of routes) {
      const page = await context.newPage()
      await page.addInitScript(() => {
        window.__retroVaultLargestContentfulPaint = null
        if ("PerformanceObserver" in window) {
          const observer = new PerformanceObserver((list) => {
            const entries = list.getEntries()
            const latest = entries[entries.length - 1]
            if (latest)
              window.__retroVaultLargestContentfulPaint = latest.startTime
          })
          observer.observe({ type: "largest-contentful-paint", buffered: true })
        }
      })
      const targetUrl = new URL(route, frontendUrl).toString()
      for (let warmup = 0; warmup < WARMUP_COUNT; warmup += 1) {
        await waitForRenderedPage(page, targetUrl)
      }
      const samples: Sample[] = []
      for (let navigation = 1; navigation <= SAMPLE_COUNT; navigation += 1) {
        try {
          await waitForRenderedPage(page, targetUrl)
          const lcp = await page.evaluate(
            () => window.__retroVaultLargestContentfulPaint ?? null,
          )
          samples.push({
            navigation,
            lcp_ms: lcp === null ? null : Number(lcp.toFixed(2)),
          })
        } catch (error) {
          samples.push({
            navigation,
            lcp_ms: null,
            error: error instanceof Error ? error.name : "navigation_error",
          })
        }
      }
      await page.close()
      const values = samples.flatMap((sample) =>
        sample.lcp_ms === null ? [] : [sample.lcp_ms],
      )
      const p75 = percentile75(values)
      const complete = values.length === SAMPLE_COUNT
      routeReports.push({
        route,
        warmup_navigations: WARMUP_COUNT,
        sample_count: SAMPLE_COUNT,
        successful_samples: values.length,
        missing_samples: SAMPLE_COUNT - values.length,
        p75_ms: p75,
        local_threshold_ms: TARGET_MS,
        status: !complete
          ? "inconclusive"
          : p75 !== null && p75 <= TARGET_MS
            ? "within_local_threshold"
            : "above_local_threshold",
        samples,
      })
    }
  } finally {
    await context.close()
    await browser.close()
  }

  const report = {
    report_version: 1,
    measured_at: new Date().toISOString(),
    status:
      gameId && routeReports.every((route) => route.status !== "inconclusive")
        ? "complete_local_baseline"
        : "inconclusive",
    scope:
      "Compose local production-build baseline; not production performance evidence.",
    environment: {
      compose_service: "frontend-lcp",
      frontend_url: frontendUrl.origin,
      api_url: apiUrl.origin,
      app_version: versionPayload.app_version ?? "unavailable",
      git_commit:
        process.env.GIT_COMMIT ??
        execFileSync("git", ["rev-parse", "HEAD"], { encoding: "utf8" }).trim(),
      git_worktree_clean:
        execFileSync("git", ["status", "--porcelain"], {
          encoding: "utf8",
        }).trim().length === 0,
      browser: `Chromium ${browser.version()}`,
      viewport_css_px: VIEWPORT,
      device_scale_factor: 1,
      reduced_motion: "reduce",
      warmup_navigations_per_route: WARMUP_COUNT,
      measured_navigations_per_route: SAMPLE_COUNT,
      dataset_label:
        process.env.LCP_DATASET_LABEL ??
        "Catálogo publicado ativo no PostgreSQL Compose local",
      detail_game_id: gameId,
    },
    routes: routeReports,
    limitations: [
      "A medição usa um Compose local e não comprova desempenho de produção.",
      "Cada rota precisa de 30 eventos LCP; evento ausente ou falha de navegação torna sua conclusão inconclusiva.",
      "A página de detalhe é medida somente quando existe um jogo publicado ou LCP_GAME_ID é informado.",
      "O baseline não representa rede, CPU ou variabilidade de usuários reais.",
    ],
  }
  const timestamp = report.measured_at.replaceAll(/[:.]/g, "-")
  const outputPath = resolve(
    process.env.LCP_OUTPUT ??
      `../docs/evidencias/lcp-compose-${timestamp}.json`,
  )
  mkdirSync(dirname(outputPath), { recursive: true })
  writeFileSync(outputPath, `${JSON.stringify(report, null, 2)}\n`, {
    mode: 0o600,
  })
  console.log(
    JSON.stringify(
      {
        report: outputPath,
        status: report.status,
        routes: routeReports.map(
          ({ route, successful_samples, p75_ms, status }) => ({
            route,
            successful_samples,
            p75_ms,
            status,
          }),
        ),
      },
      null,
      2,
    ),
  )
  if (report.status !== "complete_local_baseline") process.exitCode = 2
}

main().catch((error: unknown) => {
  const code = error instanceof Error ? error.message : "lcp_measurement_failed"
  console.error(JSON.stringify({ code }))
  process.exitCode = 2
})
