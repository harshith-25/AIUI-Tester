/**
 * Lighthouse Runner — Standalone Node.js script
 *
 * Usage: node lighthouse-runner.mjs <url> <outputDir> <reportId> [categories...]
 *
 * FIXES vs previous version:
 *   1. Lighthouse runs FIRST on a fresh Chrome tab — CDP monitoring runs AFTER.
 *   2. Removed --no-zygote: crashes renderer when combined with --no-sandbox.
 *   3. Removed --js-flags=--max-old-space-size=2048 from chromeFlags (Node flag).
 *   4. Added --memory-pressure-off and --renderer-process-limit=2.
 *   5. maxWaitForLoad raised to 60 000 ms.
 *   6. PAGE_SETTLE_MS reduced to 5 000 ms (CDP pass only).
 *   7. CDP pass wrapped in try/catch — non-fatal.
 *   8. process.exit(0) removed so finally always runs chrome.kill().
 *
 * CSV FIXES (this revision):
 *   A. Deduplication now happens BEFORE the CSV is written, so the file and
 *      the JSON summary are always consistent.
 *   B. CSV field escaping uses a proper RFC-4180 implementation — handles
 *      commas in query-string URLs, double-quotes, and newlines in error text.
 *   C. The CSV is always written when there are failed requests, regardless of
 *      whether the CDP pass ran. The Lighthouse-only path also writes it.
 *   D. The summary JSON includes a `failedRequestsCsvWritten` boolean so the
 *      Python caller doesn't need to probe the filesystem.
 *   E. Empty-response heuristic tightened: only flag 0-byte responses for
 *      resource types that should have a body (not Ping/Preflight/Redirect).
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
const SLOW_RESOURCE_MS = 30000;  // 30 s = slow resource
const STALL_TIMEOUT_MS = 60000;  // 60 s = stalled / timed out
const PAGE_SETTLE_MS = 5000;  // 5 s settle after load (CDP pass only)

// Resource types that legitimately have 0-byte bodies and should not be
// flagged as empty responses.
const BODYLESS_TYPES = new Set([
  'Ping', 'Preflight', 'CSPViolationReport',
]);

// ── RFC-4180 CSV helpers ────────────────────────────────────────────────────

/**
 * Escape a single CSV field per RFC 4180:
 *   - If the value contains a comma, double-quote, or newline → wrap in
 *     double-quotes and escape any internal double-quotes by doubling them.
 *   - Otherwise return the value as-is (no unnecessary quoting).
 *
 * This correctly handles URLs like:
 *   https://example.com/search?q=foo,bar&sort=asc
 * which would otherwise split across columns with the old simple escaper.
 */
function csvField(value) {
  const s = value == null ? '' : String(value);
  // Must quote if contains comma, double-quote, CR, or LF
  if (s.includes(',') || s.includes('"') || s.includes('\r') || s.includes('\n')) {
    return '"' + s.replace(/"/g, '""') + '"';
  }
  return s;
}

/** Build a single CSV row from an array of values. */
function csvRow(fields) {
  return fields.map(csvField).join(',');
}

/**
 * Write the failed-requests CSV.
 * Separated into its own function so it can be called from both the CDP path
 * and the Lighthouse-only fallback path without code duplication.
 */
function writeFailedCsv(outputDir, reportId, requests) {
  if (requests.length === 0) return false;

  const header = csvRow(['URL', 'Status Code', 'MIME Type', 'Error Type']);
  const rows = requests.map(r => csvRow([r.url, r.statusCode, r.mimeType, r.errorType]));
  const content = [header, ...rows].join('\n');

  writeFileSync(join(outputDir, `${reportId}.failed-requests.csv`), content, 'utf8');
  return true;
}

// ── Deduplication helper ────────────────────────────────────────────────────

/**
 * Deduplicate an array of failed-request objects by URL, keeping the first
 * occurrence (CDP failures take precedence over Lighthouse cross-reference).
 */
function deduplicateByUrl(requests) {
  const seen = new Set();
  const out = [];
  for (const req of requests) {
    if (!seen.has(req.url)) {
      seen.add(req.url);
      out.push(req);
    }
  }
  return out;
}

// ── Main ────────────────────────────────────────────────────────────────────

async function run() {
  let chrome;
  try {
    mkdirSync(outputDir, { recursive: true });

    // ── Launch Chrome ───────────────────────────────────────────────
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
        '--safebrowsing-disable-auto-update',
        '--use-gl=swiftshader',
        '--memory-pressure-off',
        '--renderer-process-limit=2',
      ]
    });

    // ── 1. Run Lighthouse FIRST (fresh tab, no prior page load) ────
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
        maxWaitForLoad: 60000,
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

    // ── 2. CDP Network Monitoring (AFTER Lighthouse, best-effort) ──
    const cdpFailures = [];

    try {
      const CDP = await import('chrome-remote-interface');
      const cdpClient = await CDP.default({ port: chrome.port });
      const { Network, Page, Runtime } = cdpClient;

      const requests = new Map();

      Network.requestWillBeSent(({ requestId, request, type }) => {
        requests.set(requestId, {
          url: request.url,
          method: request.method,
          startTimeMs: Date.now(),
          resourceType: type || 'Other',
          gotResponse: false,
          finished: false,
          failed: false,
          statusCode: null,
          mimeType: 'unknown',
          responseTimeMs: null,
          endTimeMs: null,
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

      await Page.navigate({ url });
      try { await Page.loadEventFired(); } catch (_) { /* continue on timeout */ }

      // Scroll to trigger lazy-loaded resources
      try {
        await Runtime.evaluate({
          expression: `
            new Promise((resolve) => {
              let totalHeight = 0;
              const distance = 500;
              const timer = setInterval(() => {
                window.scrollBy(0, distance);
                totalHeight += distance;
                if (totalHeight >= document.body.scrollHeight || totalHeight > 15000) {
                  clearInterval(timer);
                  resolve();
                }
              }, 150);
            });
          `,
          awaitPromise: true,
          timeout: 10000,
        });
      } catch (e) {
        console.error('[CDP] Scroll failed (non-fatal):', e.message);
      }

      await new Promise(resolve => setTimeout(resolve, PAGE_SETTLE_MS));

      const nowMs = Date.now();
      const allUrls = Array.from(requests.values()).map(r => r.url);
      writeFileSync(
        join(outputDir, `${reportId}.all-requests.json`),
        JSON.stringify(allUrls, null, 2)
      );

      for (const [, entry] of requests) {
        if (
          !entry.url ||
          entry.url.startsWith('data:') ||
          entry.url.startsWith('chrome-extension:') ||
          entry.url.startsWith('about:')
        ) continue;

        const elapsedMs = nowMs - entry.startTimeMs;

        // Case 1: Network-level failure
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
            errorType = entry.gotResponse
              ? `Download Aborted/Stalled (${Math.round(elapsedMs / 1000)}s)`
              : 'Request Aborted';
            if (!entry.gotResponse) statusCode = 'aborted';
          } else if (/ERR_BLOCKED/i.test(entry.errorText)) {
            errorType = entry.errorText;
            if (!entry.gotResponse) statusCode = 'blocked';
          }

          if (entry.gotResponse && elapsedMs >= STALL_TIMEOUT_MS) {
            errorType = `Download Stalled & Failed (>${Math.round(STALL_TIMEOUT_MS / 1000)}s)`;
          }

          cdpFailures.push({
            url: entry.url,
            statusCode,
            mimeType: entry.mimeType || entry.resourceType || 'unknown',
            errorType,
          });
          continue;
        }

        // Case 2: HTTP 4xx / 5xx
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

          cdpFailures.push({
            url: entry.url,
            statusCode: String(status),
            mimeType: entry.mimeType,
            errorType,
          });
          continue;
        }

        // Case 3: Headers received but body never completed
        if (entry.gotResponse && !entry.finished && !entry.failed) {
          const downloadMs = nowMs - entry.responseTimeMs;
          cdpFailures.push({
            url: entry.url,
            statusCode: String(entry.statusCode || 'stalled'),
            mimeType: entry.mimeType,
            errorType: downloadMs >= STALL_TIMEOUT_MS
              ? `Download Stalled (>${Math.round(STALL_TIMEOUT_MS / 1000)}s, body never completed)`
              : `Download Incomplete (${Math.round(downloadMs / 1000)}s elapsed, body not received)`,
          });
          continue;
        }

        // Case 4: No response at all
        if (!entry.gotResponse && !entry.finished && !entry.failed) {
          cdpFailures.push({
            url: entry.url,
            statusCode: 'timeout',
            mimeType: entry.resourceType || 'unknown',
            errorType: `No Response (${Math.round(elapsedMs / 1000)}s, server never responded)`,
          });
          continue;
        }

        // Case 5: Completed but very slow
        if (entry.finished && entry.endTimeMs && entry.startTimeMs) {
          const durationMs = entry.endTimeMs - entry.startTimeMs;
          if (durationMs > SLOW_RESOURCE_MS) {
            cdpFailures.push({
              url: entry.url,
              statusCode: String(entry.statusCode || 200),
              mimeType: entry.mimeType,
              errorType: `Slow Resource (${(durationMs / 1000).toFixed(1)}s to fully load)`,
            });
          }
        }
      }

      await cdpClient.close();

    } catch (cdpErr) {
      console.error('[CDP] Network monitoring pass failed (non-fatal):', cdpErr.message);
    }

    // ── 3. Cross-reference with Lighthouse's network-requests audit ─
    const lhr = result.lhr;
    const networkAudit = lhr.audits?.['network-requests'];

    // Build a set from CDP failures first so LH cross-ref can skip dupes
    const cdpUrlSet = new Set(cdpFailures.map(f => f.url));
    const lhFailures = [];

    if (networkAudit?.details?.items) {
      for (const item of networkAudit.details.items) {
        const reqUrl = item.url;
        if (!reqUrl || reqUrl.startsWith('data:') || cdpUrlSet.has(reqUrl)) continue;

        const status = item.statusCode;

        if (status && status >= 400) {
          let errorType = `HTTP ${status}`;
          if (status === 404) errorType = 'Not Found';
          else if (status === 403) errorType = 'Forbidden';
          else if (status >= 500) errorType = `Server Error (${status})`;
          lhFailures.push({
            url: reqUrl,
            statusCode: String(status),
            mimeType: item.mimeType || 'unknown',
            errorType,
          });
          continue;
        }

        if (item.finished === false) {
          lhFailures.push({
            url: reqUrl,
            statusCode: String(status || 'incomplete'),
            mimeType: item.mimeType || 'unknown',
            errorType: 'Incomplete Load (detected by Lighthouse)',
          });
          continue;
        }

        // Tightened empty-response check: skip bodyless resource types
        if (
          item.transferSize === 0 && item.resourceSize === 0 &&
          status && status >= 200 && status < 300 &&
          !BODYLESS_TYPES.has(item.resourceType)
        ) {
          lhFailures.push({
            url: reqUrl,
            statusCode: String(status),
            mimeType: item.mimeType || 'unknown',
            errorType: 'Empty Response (0 bytes transferred)',
          });
        }
      }
    }

    // ── 4. Merge + deduplicate BEFORE writing CSV ───────────────────
    // CDP failures take precedence (more detail); LH failures fill gaps.
    // Deduplication is done here so both the CSV and the JSON summary
    // reflect exactly the same set of records.
    const allFailures = deduplicateByUrl([...cdpFailures, ...lhFailures]);

    // ── 5. Write failed-requests CSV ────────────────────────────────
    const csvWritten = writeFailedCsv(outputDir, reportId, allFailures);

    // ── 6. Build Summary ────────────────────────────────────────────
    const summary = {
      status: 'success',
      reportId,
      score: null,
      metrics: {},
      categories: {},
      failedRequests: allFailures,
      failedRequestsCsvWritten: csvWritten,
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
      if (metricsItems?.length > 0) {
        const loadTime = metricsItems[0].observedLoad;
        if (loadTime != null) {
          summary.metrics.pageLoadTime = {
            value: Math.round(loadTime),
            displayValue: `${(loadTime / 1000).toFixed(1)} s`,
            score: null,
          };
        }
      }
    } catch (_) { /* non-fatal */ }

    for (const [catId, cat] of Object.entries(lhr.categories || {})) {
      summary.categories[catId] = {
        score: cat.score !== null && cat.score !== undefined
          ? Math.round(cat.score * 100)
          : null,
        title: cat.title,
      };
    }

    process.stdout.write(JSON.stringify(summary));

  } catch (error) {
    process.stderr.write(JSON.stringify({
      status: 'error',
      message: error.message || String(error),
    }));
    process.exitCode = 1;
  } finally {
    if (chrome) {
      try { await chrome.kill(); } catch (_) { /* ignore */ }
    }
  }
}

run();