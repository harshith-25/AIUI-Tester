/**
 * Lighthouse Runner — Standalone Node.js script
 *
 * Usage: node lighthouse-runner.mjs <url> <outputDir> <reportId> [categories...]
 *
 * Runs a real Google Lighthouse audit using headless Chrome and writes
 * both JSON and HTML reports to the output directory.
 * Prints a JSON summary to stdout for the calling process to parse.
 *
 * Also captures all failed/problematic network requests via CDP
 * (404s, timeouts, DNS failures, blocked requests, stalled downloads, slow resources, etc.)
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

const categories = categoryArgs.length > 0 ? categoryArgs : ['performance'];

// Thresholds
const SLOW_RESOURCE_MS = 30000;   // 30s = slow resource
const STALL_TIMEOUT_MS = 60000;   // 60s = stalled / timed out
const PAGE_SETTLE_MS = 15000;     // Wait 15s after load event for late resources

async function run() {
  let chrome;
  try {
    mkdirSync(outputDir, { recursive: true });

    chrome = await chromeLauncher.launch({
      chromeFlags: [
        '--headless', '--no-sandbox', '--disable-gpu',
        '--disable-dev-shm-usage', '--disable-software-rasterizer',
        '--disable-extensions', '--disable-background-networking',
        '--disable-background-timer-throttling',
        '--disable-backgrounding-occluded-windows',
        '--disable-breakpad', '--disable-client-side-phishing-detection',
        '--disable-component-update', '--disable-default-apps',
        '--disable-domain-reliability',
        '--disable-features=AudioServiceOutOfProcess',
        '--disable-hang-monitor', '--disable-ipc-flooding-protection',
        '--disable-popup-blocking', '--disable-prompt-on-repost',
        '--disable-renderer-backgrounding', '--disable-sync',
        '--disable-translate', '--force-color-profile=srgb',
        '--hide-scrollbars', '--ignore-gpu-blocklist',
        '--metrics-recording-only', '--mute-audio',
        '--no-first-run', '--no-default-browser-check',
        '--no-pings', '--no-zygote',
        '--safebrowsing-disable-auto-update',
        '--use-gl=swiftshader',
        '--js-flags=--max-old-space-size=512',
      ]
    });

    // ── CDP Full Lifecycle Network Monitoring ───────────────────────
    const CDP = await import('chrome-remote-interface');
    const cdpClient = await CDP.default({ port: chrome.port });
    const { Network, Page, Runtime } = cdpClient;

    // Full lifecycle tracking per request
    const requests = new Map(); // requestId → full info
    const failedRequests = [];

    Network.requestWillBeSent(({ requestId, request, type }) => {
      requests.set(requestId, {
        url: request.url,
        method: request.method,
        startTimeMs: Date.now(),
        resourceType: type || 'Other',
        // Lifecycle flags
        gotResponse: false,
        finished: false,
        failed: false,
        // Response data
        statusCode: null,
        mimeType: 'unknown',
        // Timing
        responseTimeMs: null,
        endTimeMs: null,
        // Error info
        errorText: null,
        blockedReason: null,
      });
    });

    Network.responseReceived(({ requestId, response }) => {
      const entry = requests.get(requestId);
      if (!entry) return;
      entry.gotResponse = true;
      entry.statusCode = response.status;
      entry.mimeType = response.mimeType || 'unknown';
      entry.responseTimeMs = Date.now();
    });

    Network.loadingFinished(({ requestId, encodedDataLength }) => {
      const entry = requests.get(requestId);
      if (!entry) return;
      entry.finished = true;
      entry.endTimeMs = Date.now();
      entry.encodedSize = encodedDataLength || 0;
    });

    Network.loadingFailed(({ requestId, errorText, blockedReason, type }) => {
      const entry = requests.get(requestId);
      if (!entry) return;
      entry.failed = true;
      entry.endTimeMs = Date.now();
      entry.errorText = errorText || '';
      entry.blockedReason = blockedReason || null;
      if (type) entry.resourceType = type;
    });

    await Network.enable();
    await Page.enable();
    await Runtime.enable();

    // Navigate and wait for full page load
    await Page.navigate({ url });
    try { await Page.loadEventFired(); } catch (_) { /* continue */ }

    // Scroll down to trigger lazy-loaded resources (e.g. heavy images)
    try {
      await Runtime.evaluate({
        expression: `
          new Promise((resolve) => {
            let totalHeight = 0;
            let distance = 500;
            let timer = setInterval(() => {
              window.scrollBy(0, distance);
              totalHeight += distance;
              // Stop if we reach bottom or scroll 15000px max to prevent infinite loops
              if (totalHeight >= document.body.scrollHeight || totalHeight > 15000) {
                clearInterval(timer);
                resolve();
              }
            }, 150);
          });
        `,
        awaitPromise: true,
      });
    } catch (e) {
      console.error("Scroll failed", e);
    }

    // Wait for late-loading resources (JS-injected images, lazy-load, etc.)
    await new Promise(resolve => setTimeout(resolve, PAGE_SETTLE_MS));

    // ── Analyze all tracked requests ────────────────────────────────
    const nowMs = Date.now();
    const allUrls = Array.from(requests.values()).map(r => r.url);
    writeFileSync(join(outputDir, `${reportId}.all-requests.json`), JSON.stringify(allUrls, null, 2));

    for (const [reqId, entry] of requests) {
      // Skip data URIs, chrome-extension, about:blank etc.
      if (!entry.url || entry.url.startsWith('data:') ||
          entry.url.startsWith('chrome-extension:') ||
          entry.url.startsWith('about:')) {
        continue;
      }

      const elapsedMs = nowMs - entry.startTimeMs;

      // Case 1: Network-level failure (DNS, blocked, refused, SSL, etc.)
      if (entry.failed) {
        let errorType = entry.errorText || 'Network Error';
        let statusCode = entry.gotResponse ? String(entry.statusCode) : 'failed';

        if (entry.blockedReason) {
          errorType = `Blocked: ${entry.blockedReason}`;
          if (!entry.gotResponse) statusCode = 'blocked';
        } else if (/dns/i.test(entry.errorText)) {
          errorType = 'DNS Resolution Failed';
          if (!entry.gotResponse) statusCode = 'dns_error';
        } else if (/timed?\s*out/i.test(entry.errorText)) {
          errorType = 'Connection Timeout';
          if (!entry.gotResponse) statusCode = 'timeout';
        } else if (/refused/i.test(entry.errorText)) {
          errorType = 'Connection Refused';
          if (!entry.gotResponse) statusCode = 'conn_refused';
        } else if (/reset/i.test(entry.errorText)) {
          errorType = 'Connection Reset';
          if (!entry.gotResponse) statusCode = 'conn_reset';
        } else if (/ssl|cert/i.test(entry.errorText)) {
          errorType = 'SSL/Certificate Error';
          if (!entry.gotResponse) statusCode = 'ssl_error';
        } else if (/aborted/i.test(entry.errorText)) {
          // If we got a response and THEN aborted, it means the download stalled/failed mid-way
          errorType = entry.gotResponse ? `Download Aborted/Stalled (${Math.round(elapsedMs/1000)}s)` : 'Request Aborted';
          if (!entry.gotResponse) statusCode = 'aborted';
        } else if (/ERR_BLOCKED/i.test(entry.errorText)) {
          errorType = entry.errorText;
          if (!entry.gotResponse) statusCode = 'blocked';
        }

        // If it was a stalled download that timed out, clarify the error type
        if (entry.gotResponse && elapsedMs >= STALL_TIMEOUT_MS) {
           errorType = `Download Stalled & Failed (>${Math.round(STALL_TIMEOUT_MS/1000)}s)`;
        }

        failedRequests.push({
          url: entry.url,
          statusCode,
          mimeType: entry.mimeType || entry.resourceType || 'unknown',
          errorType,
        });
        continue;
      }

      // Case 2: HTTP error status (4xx, 5xx)
      if (entry.gotResponse && entry.statusCode >= 400) {
        const status = entry.statusCode;
        let errorType = `HTTP ${status}`;
        if (status === 404) errorType = 'Not Found';
        else if (status === 403) errorType = 'Forbidden';
        else if (status === 500) errorType = 'Internal Server Error';
        else if (status === 502) errorType = 'Bad Gateway';
        else if (status === 503) errorType = 'Service Unavailable';
        else if (status >= 400 && status < 500) errorType = `Client Error (${status})`;
        else if (status >= 500) errorType = `Server Error (${status})`;

        failedRequests.push({
          url: entry.url,
          statusCode: String(status),
          mimeType: entry.mimeType,
          errorType,
        });
        continue;
      }

      // Case 3: Got response headers (e.g. 200) but body NEVER finished downloading
      if (entry.gotResponse && !entry.finished && !entry.failed) {
        const downloadMs = nowMs - entry.responseTimeMs;
        failedRequests.push({
          url: entry.url,
          statusCode: String(entry.statusCode || 'stalled'),
          mimeType: entry.mimeType,
          errorType: downloadMs >= STALL_TIMEOUT_MS
            ? `Download Stalled (>${Math.round(STALL_TIMEOUT_MS/1000)}s, body never completed)`
            : `Download Incomplete (${Math.round(downloadMs/1000)}s elapsed, body not received)`,
        });
        continue;
      }

      // Case 4: Never got any response at all (no headers, no failure event)
      if (!entry.gotResponse && !entry.finished && !entry.failed) {
        failedRequests.push({
          url: entry.url,
          statusCode: 'timeout',
          mimeType: entry.resourceType || 'unknown',
          errorType: `No Response (${Math.round(elapsedMs/1000)}s, server never responded)`,
        });
        continue;
      }

      // Case 5: Completed but took way too long (slow resource)
      if (entry.finished && entry.endTimeMs && entry.startTimeMs) {
        const durationMs = entry.endTimeMs - entry.startTimeMs;
        if (durationMs > SLOW_RESOURCE_MS) {
          failedRequests.push({
            url: entry.url,
            statusCode: String(entry.statusCode || 200),
            mimeType: entry.mimeType,
            errorType: `Slow Resource (${(durationMs/1000).toFixed(1)}s to fully load)`,
          });
        }
      }
    }

    // Disconnect CDP so Lighthouse can use the port
    await cdpClient.close();

    // ── Run Lighthouse Audit ────────────────────────────────────────
    const options = {
      logLevel: 'info',
      output: ['json', 'html'],
      onlyCategories: categories,
      port: chrome.port,
      settings: {
        formFactor: 'desktop',
        screenEmulation: {
          mobile: false, width: 1350, height: 940,
          deviceScaleFactor: 1, disabled: false,
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

    // ── Cross-reference with Lighthouse's network-requests audit ───
    const lhr = result.lhr;
    const networkAudit = lhr.audits?.['network-requests'];
    if (networkAudit?.details?.items) {
      const existingUrls = new Set(failedRequests.map(f => f.url));

      for (const item of networkAudit.details.items) {
        const reqUrl = item.url;
        if (!reqUrl || reqUrl.startsWith('data:') || existingUrls.has(reqUrl)) continue;

        const status = item.statusCode;

        // HTTP errors from Lighthouse's perspective
        if (status && status >= 400) {
          let errorType = `HTTP ${status}`;
          if (status === 404) errorType = 'Not Found';
          else if (status === 403) errorType = 'Forbidden';
          else if (status >= 500) errorType = `Server Error (${status})`;

          failedRequests.push({
            url: reqUrl,
            statusCode: String(status),
            mimeType: item.mimeType || 'unknown',
            errorType,
          });
          existingUrls.add(reqUrl);
          continue;
        }

        // Incomplete resources detected by Lighthouse
        if (item.finished === false) {
          failedRequests.push({
            url: reqUrl,
            statusCode: String(status || 'incomplete'),
            mimeType: item.mimeType || 'unknown',
            errorType: 'Incomplete Load (detected by Lighthouse)',
          });
          existingUrls.add(reqUrl);
          continue;
        }

        // Zero-size resources that should have content
        if (item.transferSize === 0 && item.resourceSize === 0 &&
            status && status >= 200 && status < 300 &&
            item.resourceType !== 'Ping' && item.resourceType !== 'Preflight') {
          failedRequests.push({
            url: reqUrl,
            statusCode: String(status),
            mimeType: item.mimeType || 'unknown',
            errorType: 'Empty Response (0 bytes transferred)',
          });
          existingUrls.add(reqUrl);
        }
      }
    }

    // Deduplicate by URL (keep first occurrence)
    const seenUrls = new Set();
    const uniqueFailedRequests = [];
    for (const req of failedRequests) {
      if (!seenUrls.has(req.url)) {
        seenUrls.add(req.url);
        uniqueFailedRequests.push(req);
      }
    }

    // ── Build Summary ───────────────────────────────────────────────
    const summary = {
      status: 'success',
      reportId,
      score: null,
      metrics: {},
      categories: {},
      failedRequests: uniqueFailedRequests,
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

    // Write failed requests CSV
    if (uniqueFailedRequests.length > 0) {
      const escapeField = (val) => {
        const s = String(val);
        if (s.includes(',') || s.includes('"') || s.includes('\n')) {
          return `"${s.replace(/"/g, '""')}"`;
        }
        return s;
      };
      const csvHeader = 'URL,Status Code,MIME Type,Error Type';
      const csvRows = uniqueFailedRequests.map(r =>
        [escapeField(r.url), escapeField(r.statusCode),
         escapeField(r.mimeType), escapeField(r.errorType)].join(',')
      );
      writeFileSync(join(outputDir, `${reportId}.failed-requests.csv`),
                     [csvHeader, ...csvRows].join('\n'));
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
      try { await chrome.kill(); } catch (_) { }
    }
  }
}

run();