import urllib.request
import urllib.error
import xml.etree.ElementTree as ET
import subprocess
import json
import csv
from pathlib import Path

SITEMAP_URL = "https://new.conceptsurfaces.com/product-sitemap.xml"
OUTPUT_DIR = Path("bulk_lighthouse_reports")
RUNNER_SCRIPT = "lighthouse-runner.mjs"

def main():
    OUTPUT_DIR.mkdir(exist_ok=True)
    
    print(f"Fetching sitemap from {SITEMAP_URL}...")
    req = urllib.request.Request(
        SITEMAP_URL, 
        headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"}
    )
    
    try:
        with urllib.request.urlopen(req) as response:
            content = response.read()
    except urllib.error.URLError as e:
        print(f"Failed to fetch sitemap: {e}")
        return
        
    # Parse XML Sitemap
    root = ET.fromstring(content)
    
    # Handle standard sitemap namespaces
    ns = {'ns': 'http://www.sitemaps.org/schemas/sitemap/0.9'}
    urls = [loc.text for loc in root.findall('.//ns:loc', ns)]
    
    # Fallback if namespace is different or missing
    if not urls:
        urls = [loc.text for loc in root.findall('.//loc')]

    print(f"Found {len(urls)} URLs in the sitemap.")
    
    results = []
    
    for i, url in enumerate(urls, 1):
        report_id = f"report_{i:03d}"
        print(f"\n[{i}/{len(urls)}] Auditing {url}...")
        
        cmd = [
            "node",
            RUNNER_SCRIPT,
            url,
            str(OUTPUT_DIR),
            report_id,
            "performance",
            "best-practices"
        ]
        
        row = {"URL": url, "Report File": f"{report_id}.report.html", "Status": "", "Score": "", "FCP": "", "LCP": "", "TTFB": ""}
        
        try:
            # 180s timeout per audit
            proc = subprocess.run(cmd, capture_output=True, text=True, timeout=180)
            
            if proc.returncode == 0:
                print(f"  -> Success. Saved to {report_id}.report.html")
                row["Status"] = "Success"
                try:
                    summary = json.loads(proc.stdout.strip())
                    row["Score"] = summary.get("score", "")
                    metrics = summary.get("metrics", {})
                    row["FCP"] = metrics.get("fcp", {}).get("displayValue", "")
                    row["LCP"] = metrics.get("lcp", {}).get("displayValue", "")
                    row["TTFB"] = metrics.get("ttfb", {}).get("displayValue", "")
                    print(f"  -> Score: {row['Score']} | LCP: {row['LCP']}")
                except json.JSONDecodeError:
                    print("  -> Could not parse JSON summary.")
            else:
                err_msg = proc.stderr.strip()
                print(f"  -> Failed: {err_msg[:100]}")
                row["Status"] = f"Failed"
        except subprocess.TimeoutExpired:
            print(f"  -> Timeout (180s)")
            row["Status"] = "Timeout"
        except Exception as e:
            print(f"  -> Error: {e}")
            row["Status"] = f"Error: {e}"
            
        results.append(row)
        
        # Save CSV progressively so data isn't lost if stopped early
        csv_path = OUTPUT_DIR / "summary.csv"
        with open(csv_path, "w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=["URL", "Report File", "Status", "Score", "FCP", "LCP", "TTFB"])
            writer.writeheader()
            writer.writerows(results)

    print(f"\nDone! All reports saved in {OUTPUT_DIR}")
    print(f"A summary of all scores is available at: {OUTPUT_DIR / 'summary.csv'}")

if __name__ == "__main__":
    main()
