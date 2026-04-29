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

const [,, url, outputDir, reportId, ...categoryArgs] = process.argv;

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
    // Ensure output directory exists
    mkdirSync(outputDir, { recursive: true });

    // Launch headless Chrome
    chrome = await chromeLauncher.launch({
      chromeFlags: [
        '--headless=new',
        '--no-sandbox',
        '--disable-gpu',
        '--disable-dev-shm-usage',
        '--user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
        '--disable-blink-features=AutomationControlled',
        '--js-flags="--max-old-space-size=4096"',
        '--disable-features=IsolateOrigins,site-per-process',
        '--disk-cache-size=1',
      ]
    });

    const options = {
      logLevel: 'info',
      output: ['json', 'html'],
      onlyCategories: categories,
      port: chrome.port,
      settings: {
        maxWaitForLoad: 90000,
        skipAudits: [
          'full-page-screenshot', 
          'screenshot-thumbnails', 
          'final-screenshot',
          'largest-contentful-paint-element', 
          'layout-shifts', 
          'lcp-lazy-loaded', 
          'non-composited-animations', 
          'prioritize-lcp-image',
          'cls-culprits-insight',
          'document-latency-insight',
          'dom-size-insight',
          'font-display-insight',
          'forced-reflow-insight',
          'image-delivery-insight',
          'inp-breakdown-insight',
          'lcp-breakdown-insight',
          'lcp-discovery-insight',
          'network-dependency-tree-insight',
          'render-blocking-insight',
          'third-parties-insight',
          'viewport-insight',
          'optimized-images',
          'uses-text-compression',
          'unminified-css',
          'unused-css-rules',
          'render-blocking-resources',
          'bf-cache',
          'image-size-responsive',
          'offscreen-images',
          'uses-responsive-images'
        ],
        formFactor: 'desktop',
        screenEmulation: {
          mobile: false,
          width: 1920,
          height: 1080,
          deviceScaleFactor: 1,
          disabled: false,
        },
        throttlingMethod: 'provided',
        disableStorageReset: true,
      },
    };

    const result = await lighthouse(url, options);

    if (!result || !result.lhr) {
      throw new Error('Lighthouse returned empty result');
    }

    const [jsonReport, htmlReport] = result.report;

    // Write report files
    writeFileSync(join(outputDir, `${reportId}.report.json`), jsonReport);
    writeFileSync(join(outputDir, `${reportId}.report.html`), htmlReport);

    // Build summary for stdout
    const lhr = result.lhr;
    const summary = {
      status: 'success',
      reportId,
      score: null,
      metrics: {},
      categories: {},
    };

    // Performance score
    const perfScore = lhr.categories?.performance?.score;
    if (perfScore !== null && perfScore !== undefined) {
      summary.score = Math.round(perfScore * 100);
    }

    // Extract audit metrics
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

    // Extract Full Page Load Time from Lighthouse's own navigation timing.
    // lhr.audits.metrics.details.items[0] contains observedLoad (loadEventEnd)
    // which is the real "Full Page Load Time" as measured by the browser.
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
    } catch (_) {
      // Non-critical — if observedLoad is missing, skip it
    }

    // Extract category scores
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
      } catch (_) {
        // Ignore cleanup errors (Windows EPERM on temp dir is non-critical)
      }
    }
  }
}

run();