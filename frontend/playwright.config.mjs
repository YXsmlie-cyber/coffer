import { defineConfig } from '@playwright/test'

export default defineConfig({
  testDir: './browser-tests',
  outputDir: '../data/maintenance/browser-results',
  timeout: 30000,
  fullyParallel: false,
  workers: 1,
  use: {
    baseURL: 'http://127.0.0.1:4175',
    timezoneId: 'Asia/Shanghai',
    trace: 'retain-on-failure', screenshot: 'only-on-failure',
  },
  projects: [
    { name: 'desktop', use: { viewport: { width: 1440, height: 1000 } } },
    { name: 'mobile', use: { viewport: { width: 390, height: 844 }, isMobile: true, hasTouch: true } },
  ],
  webServer: {
    command: `npm run ${process.env.PH115_READONLY_QA ? 'preview' : 'dev'} -- --host 127.0.0.1 --port 4175 --strictPort`,
    url: 'http://127.0.0.1:4175', reuseExistingServer: false,
  },
})
