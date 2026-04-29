/**
 * Lighthouse Runner — Standalone Node.js script
 *
 * Usage: node lighthouse-runner.mjs <url> <outputDir> <reportId> [categories...]
 *
 * Runs a real Google Lighthouse audit using headless Chrome and writes
 * both JSON and HTML reports to the output directory.
 * Prints a JSON summary to stdout for the calling process to parse.
 */

import lighthouse from 'lighthouse';
import * as chromeLauncher from 'chrome-launcher';
import { writeFileSync, mkdirSync } from 'fs';
import { join } from 'path';

const [, , url, outputDir, reportId, ...categoryArgs] = process.argv;

if (!url || !outputDir || !reportId) {
  process.stderr.write(JSON.stringify({
    status: 'error',
    message: 'Usage: node lighthouse-runner.mjs <url> <outputDir> <reportId> [categories...]'
  }));
  process.exit(1);
}

const categories = categoryArgs.length > 0
  ? categoryArgs
  : ['performance'];

async function run() {
  let chrome;
  try {
    mkdirSync(outputDir, { recursive: true });

    chrome = await chromeLauncher.launch({
      chromeFlags: [
        '--headless',
        '--no-sandbox',
        '--disable-gpu',
        '--disable-dev-shm-usage',
        '--disable-software-rasterizer',
        '--disable-extensions',
        '--disable-background-networking',
        '--disable-background-timer-throttling',
        '--disable-backgrounding-occluded-windows',
        '--disable-breakpad',
        '--disable-client-side-phishing-detection',
        '--disable-component-update',
        '--disable-default-apps',
        '--disable-domain-reliability',
        '--disable-features=AudioServiceOutOfProcess',
        '--disable-hang-monitor',
        '--disable-ipc-flooding-protection',
        '--disable-popup-blocking',
        '--disable-prompt-on-repost',
        '--disable-renderer-backgrounding',
        '--disable-sync',
        '--disable-translate',
        '--force-color-profile=srgb',
        '--hide-scrollbars',
        '--ignore-gpu-blocklist',
        '--metrics-recording-only',
        '--mute-audio',
        '--no-first-run',
        '--no-default-browser-check',
        '--no-pings',
        '--no-zygote',
        '--safebrowsing-disable-auto-update',
        '--use-gl=swiftshader',
        '--js-flags=--max-old-space-size=512',
      ]
    });

    const options = {
      logLevel: 'info',
      output: ['json', 'html'],
      onlyCategories: categories,
      port: chrome.port,
      settings: {
        formFactor: 'desktop',
        screenEmulation: {
          mobile: false,
          width: 1350,
          height: 940,
          deviceScaleFactor: 1,
          disabled: false,
        },
        maxWaitForLoad: 45000,
        maxWaitForFcp: 30000,
        throttlingMethod: 'simulate',
      },
    };

    const result = await lighthouse(url, options);

    if (!result || !result.lhr) {
      throw new Error('Lighthouse returned empty result');
    }

    const [jsonReport, htmlReport] = result.report;

    writeFileSync(join(outputDir, `${reportId}.report.json`), jsonReport);
    writeFileSync(join(outputDir, `${reportId}.report.html`), htmlReport);

    const lhr = result.lhr;
    const summary = {
      status: 'success',
      reportId,
      score: null,
      metrics: {},
      categories: {},
    };

    const perfScore = lhr.categories?.performance?.score;
    if (perfScore !== null && perfScore !== undefined) {
      summary.score = Math.round(perfScore * 100);
    }

    const metricAudits = {
      'first-contentful-paint': 'fcp',
      'largest-contentful-paint': 'lcp',
      'cumulative-layout-shift': 'cls',
      'total-blocking-time': 'tbt',
      'speed-index': 'si',
      'interactive': 'tti',
      'server-response-time': 'ttfb',
    };

    for (const [auditId, key] of Object.entries(metricAudits)) {
      const audit = lhr.audits?.[auditId];
      if (audit) {
        summary.metrics[key] = {
          value: audit.numericValue,
          displayValue: audit.displayValue || '',
          score: audit.score,
        };
      }
    }

    try {
      const metricsItems = lhr.audits?.['metrics']?.details?.items;
      if (metricsItems && metricsItems.length > 0) {
        const observed = metricsItems[0];
        const loadTime = observed.observedLoad;
        if (loadTime != null) {
          summary.metrics.pageLoadTime = {
            value: Math.round(loadTime),
            displayValue: `${(loadTime / 1000).toFixed(1)} s`,
            score: null,
          };
        }
      }
    } catch (_) { }

    for (const [catId, cat] of Object.entries(lhr.categories || {})) {
      summary.categories[catId] = {
        score: cat.score !== null && cat.score !== undefined ? Math.round(cat.score * 100) : null,
        title: cat.title,
      };
    }

    process.stdout.write(JSON.stringify(summary));

  } catch (error) {
    process.stderr.write(JSON.stringify({
      status: 'error',
      message: error.message || String(error)
    }));
    process.exit(1);
  } finally {
    if (chrome) {
      try {
        await chrome.kill();
      } catch (_) { }
    }
  }
}

run();