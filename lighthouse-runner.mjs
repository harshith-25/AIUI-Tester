/**
 * Lighthouse Runner — Standalone Node.js script
 *
 * Usage: node lighthouse-runner.mjs <url> <outputDir> <reportId> [categories...]
 *
 * FIXES vs previous version:
 *   1. Lighthouse runs FIRST on a fresh Chrome tab — CDP monitoring runs AFTER.
 *      Previously CDP loaded the full page before Lighthouse, exhausting memory
 *      and causing TARGET_CRASHED / rc=-6 on heavy pages.
 *   2. Removed --no-zygote: combined with --no-sandbox on Linux this causes
 *      renderer crashes (SIGABRT). Not needed when --no-sandbox is present.
 *   3. Removed --js-flags=--max-old-space-size=2048 from chromeFlags: that is
 *      a Node.js V8 flag and has no effect when passed to Chrome.
 *   4. Added --memory-pressure-off and --renderer-process-limit=2 to give the
 *      renderer more headroom on memory-constrained servers.
 *   5. maxWaitForLoad raised to 60 000 ms so heavy pages don't time out.
 *   6. PAGE_SETTLE_MS reduced from 15 000 ms to 5 000 ms (CDP pass only).
 *   7. CDP pass wrapped in try/catch — a failure there is non-fatal and must
 *      not abort the Lighthouse results that already succeeded.
 *   8. process.exit(0) removed from after run() so the finally block always
 *      executes chrome.kill() before the process exits.
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

async function run() {
  let chrome;
  try {
    mkdirSync(outputDir, { recursive: true });

    // ── Launch Chrome ───────────────────────────────────────────────
    // Removed: --no-zygote  (crashes renderer when combined with --no-sandbox)
    // Removed: --js-flags=* (Node flag; meaningless when passed to Chrome)
    // Added:   --memory-pressure-off, --renderer-process-limit=2
    chrome = await chromeLauncher.launch({
      chromeFlags: [
        '--headless',
        '--no-sandbox',
        '--disable-gpu',
        '--disable-dev-shm-usage',          // prevents /dev/shm OOM crashes
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
        '--memory-pressure-off',            // don't discard renderer under load
        '--renderer-process-limit=2',       // cap renderer processes to save RAM
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
        maxWaitForLoad: 60000,   // raised: heavy pages need the extra time
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
    // Runs as a second lightweight page load purely to collect network data.
    // Wrapped in try/catch — a crash here must not discard LH results above.
    const failedRequests = [];

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
          contentLength: 0,
          receivedLength: 0,
        });
      });

      Network.responseReceived(({ requestId, response }) => {
        const entry = requests.get(requestId);
        if (!entry) return;
        entry.gotResponse = true;
        entry.statusCode = response.status;
        entry.mimeType = response.mimeType || 'unknown';
        entry.responseTimeMs = Date.now();
        
        // Capture content-length if present
        const cl = response.headers?.['content-length'] || response.headers?.['Content-Length'];
        if (cl) entry.contentLength = parseInt(cl, 10);
      });

      Network.dataReceived(({ requestId, dataLength }) => {
        const entry = requests.get(requestId);
        if (entry) entry.receivedLength += dataLength;
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

      // Brief settle for late-loading resources
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
            statusCode = 'timeout';
          }

          failedRequests.push({
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

          failedRequests.push({
            url: entry.url,
            statusCode: String(status),
            mimeType: entry.mimeType,
            errorType,
          });
          continue;
        }

        // Case 3: Headers received but body never completed or partial
        const isPartial = entry.finished && entry.contentLength > 0 && entry.receivedLength < entry.contentLength;
        if ((entry.gotResponse && !entry.finished && !entry.failed) || isPartial) {
          const downloadMs = nowMs - entry.responseTimeMs;
          const isStalled = downloadMs >= STALL_TIMEOUT_MS;
          
          let err = `Download Incomplete (${Math.round(downloadMs / 1000)}s elapsed, body not received)`;
          if (isStalled) err = `Download Stalled (>${Math.round(STALL_TIMEOUT_MS / 1000)}s, body never completed)`;
          if (isPartial) err = `Partial Download (Received ${entry.receivedLength} of ${entry.contentLength} bytes)`;

          failedRequests.push({
            url: entry.url,
            statusCode: isStalled ? 'timeout' : 'failed',
            mimeType: entry.mimeType,
            errorType: err,
          });
          continue;
        }

        // Case 4: No response at all
        if (!entry.gotResponse && !entry.finished && !entry.failed) {
          failedRequests.push({
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
            failedRequests.push({
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
    if (networkAudit?.details?.items) {
      const existingUrls = new Set(failedRequests.map(f => f.url));

      for (const item of networkAudit.details.items) {
        const reqUrl = item.url;
        if (!reqUrl || reqUrl.startsWith('data:') || existingUrls.has(reqUrl)) continue;

        const status = item.statusCode;

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

        if (item.finished === false) {
          failedRequests.push({
            url: reqUrl,
            statusCode: (status === -1) ? 'failed' : String(status || 'incomplete'),
            mimeType: item.mimeType || 'unknown',
            errorType: 'Incomplete Load (detected by Lighthouse)',
          });
          existingUrls.add(reqUrl);
          continue;
        }

        if (
          item.transferSize === 0 && item.resourceSize === 0 &&
          status && status >= 200 && status < 300 &&
          item.resourceType !== 'Ping' && item.resourceType !== 'Preflight'
        ) {
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

    // ── 4. Build Summary ────────────────────────────────────────────
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

    // Write failed requests CSV
    if (uniqueFailedRequests.length > 0) {
      const escapeField = (val) => {
        const s = String(val);
        return (s.includes(',') || s.includes('"') || s.includes('\n'))
          ? `"${s.replace(/"/g, '""')}"`
          : s;
      };
      const csvHeader = 'URL,Status Code,MIME Type,Error Type';
      const csvRows = uniqueFailedRequests.map(r =>
        [
          escapeField(r.url),
          escapeField(r.statusCode),
          escapeField(r.mimeType),
          escapeField(r.errorType),
        ].join(',')
      );
      writeFileSync(
        join(outputDir, `${reportId}.failed-requests.csv`),
        [csvHeader, ...csvRows].join('\n')
      );
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