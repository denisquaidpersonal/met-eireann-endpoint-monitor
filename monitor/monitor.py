"""Twice-daily checks for the app's active public HTTP request families.

Uses only Python's standard library so the GitHub runner needs no paid service,
package installation, or credentials beyond its built-in repository token.
"""

from __future__ import annotations

import argparse
import concurrent.futures
import html
import json
import re
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path


MET = "https://www.met.ie/Open_Data/"
REGIONS = ("Dublin", "Leinster", "Munster", "Connacht", "Ulster")
SUPPLEMENT_REGIONS = ("Leinster", "Munster", "Connacht", "Ulster")
COUNTY_CODES = {
    "Carlow": "EI01", "Cavan": "EI02", "Clare": "EI03", "Cork": "EI04",
    "Donegal": "EI06", "Dublin": "EI07", "Galway": "EI10", "Kerry": "EI11",
    "Kildare": "EI12", "Kilkenny": "EI13", "Laois": "EI14", "Leitrim": "EI15",
    "Limerick": "EI16", "Longford": "EI18", "Louth": "EI19", "Mayo": "EI20",
    "Meath": "EI21", "Monaghan": "EI22", "Offaly": "EI23", "Roscommon": "EI24",
    "Sligo": "EI25", "Tipperary": "EI26", "Waterford": "EI27",
    "Westmeath": "EI29", "Wexford": "EI30", "Wicklow": "EI31",
}
ARCHIVE_URL = "https://www.met.ie/latest-reports/recent-rainfall-radar/24-hour-rainfall-radar"
RAINFALL_URL = "https://gdal.met.ie/api/maps/rainfall"
POINT_URL = "http://openaccess.pf.api.met.ie/metno-wdb2ts/locationforecast?lat=53.3498&long=-6.2603"
MAX_BODY = 6 * 1024 * 1024
TIMEOUT = 25


@dataclass(frozen=True)
class Check:
    id: str
    label: str
    url: str
    kind: str
    source: str = ""


def catalogue() -> list[Check]:
    checks = [
        Check("national", "National forecast", MET + "json/National.json", "forecast"),
        Check("outlook", "National outlook", MET + "json/Outlook.json", "forecast"),
        Check("tonight", "Tonight station forecast", MET + "xml/web-3Dayforecast.xml", "xml"),
        Check("observations", "Current observations", MET + "xml/obs_present.xml", "xml"),
        Check("point", "Point forecast · Dublin", POINT_URL, "xml"),
        Check("warnings-inland", "Inland warnings", MET + "json/warning_IRELAND.json", "warnings"),
        Check("rainfall", "Rainfall frame list", RAINFALL_URL, "frames"),
        Check("rainfall-tile", "Rainfall image tile", "", "image", "rainfall"),
        Check("archive", "24-hour radar archive", ARCHIVE_URL, "archive"),
        Check("archive-image", "24-hour archive image", "", "image", "archive"),
    ]
    for region in REGIONS:
        key = region.lower()
        checks.append(Check(f"regional-{key}", f"{region} forecast", MET + f"json/{region}.json", "forecast"))
    for region in SUPPLEMENT_REGIONS:
        checks.append(Check(f"supplement-{region.lower()}", f"{region} pollen/UV text", MET + f"xml/x{region}.xml", "xml"))
    for county, code in COUNTY_CODES.items():
        checks.append(Check(f"warning-{code.lower()}", f"{county} warnings", MET + f"json/warning_{code}.json", "warnings"))
    return checks


def sanitize_warning_json(data: bytes) -> bytes:
    """Mirror MetEireannAPIClient's tolerance for raw controls in JSON strings."""
    result = bytearray()
    inside = escaped = False
    replacements = {8: b"\\b", 9: b"\\t", 10: b"\\n", 12: b"\\f", 13: b"\\r"}
    for byte in data:
        if inside:
            if escaped:
                result.append(byte)
                escaped = False
            elif byte == 92:
                result.append(byte)
                escaped = True
            elif byte == 34:
                result.append(byte)
                inside = False
            elif byte < 32:
                result.extend(replacements.get(byte, f"\\u00{byte:02X}".encode()))
            else:
                result.append(byte)
        else:
            result.append(byte)
            if byte == 34:
                inside = True
    return bytes(result)


def archive_images(data: bytes) -> list[str]:
    page = data.decode("utf-8")
    match = re.search(r'<long-range-radar\s+maps="([^"]+)"\s+times="([^"]+)"\s+legend="[^"]*"', page, re.S)
    if not match:
        raise ValueError("archive frame component missing")
    maps = json.loads(html.unescape(match.group(1)))
    times = json.loads(html.unescape(match.group(2)))
    if not isinstance(maps, list) or not maps or len(maps) != len(times):
        raise ValueError("archive frame list missing or inconsistent")
    if not all(isinstance(name, str) and re.fullmatch(r"[A-Za-z0-9_.-]+\.png", name) for name in maps):
        raise ValueError("invalid archive image name")
    return maps


def validate(kind: str, data: bytes, content_type: str) -> object:
    if not data:
        raise ValueError("empty response")
    if kind == "image":
        if not content_type.lower().startswith("image/"):
            raise ValueError(f"expected image, received {content_type or 'unknown content type'}")
        if not (data.startswith(b"\x89PNG\r\n\x1a\n") or data.startswith(b"\xff\xd8\xff") or data.startswith(b"RIFF") or data.startswith(b"GIF8")):
            raise ValueError("unrecognized image data")
        return None
    if kind == "xml":
        ET.fromstring(data)
        return None
    if kind == "archive":
        return archive_images(data)
    parsed = json.loads(sanitize_warning_json(data) if kind == "warnings" else data)
    if kind == "warnings":
        if not isinstance(parsed, list):
            raise ValueError("warning response is not an array")
    elif kind == "forecast":
        if not isinstance(parsed, dict) or not isinstance(parsed.get("forecasts"), list) or not parsed["forecasts"]:
            raise ValueError("forecast array missing")
    elif kind == "frames":
        if not isinstance(parsed, list) or not parsed or not all(isinstance(frame, dict) and all(k in frame for k in ("src", "modifiedTime", "server")) for frame in parsed):
            raise ValueError("rainfall frames missing or malformed")
    elif kind == "uv":
        if not isinstance(parsed, dict) or not isinstance(parsed.get("hourly"), dict) or not isinstance(parsed["hourly"].get("uv_index"), list) or not parsed["hourly"]["uv_index"]:
            raise ValueError("UV forecast missing")
    elif not isinstance(parsed, (dict, list)) or not parsed:
        raise ValueError("JSON object or array missing")
    return parsed


def request(check: Check) -> tuple[dict, object]:
    started = time.monotonic()
    response_code = None
    url = check.url
    retried = False

    def fetch(target: str, headers: dict[str, str]) -> tuple[int, str, bytes]:
        req = urllib.request.Request(target, headers=headers)
        with urllib.request.urlopen(req, timeout=TIMEOUT) as response:
            status = response.status
            content_type = response.headers.get("Content-Type", "")
            data = response.read(MAX_BODY + 1)
        if not 200 <= status < 300:
            raise urllib.error.HTTPError(target, status, "unexpected HTTP status", {}, None)
        return status, content_type, data

    try:
        headers = {"Accept": "application/xml" if check.kind == "xml" else "*/*", "User-Agent": "MetEireannDarkSky-EndpointMonitor/1.0"}
        try:
            response_code, content_type, data = fetch(url, headers)
        except urllib.error.HTTPError as exc:
            if exc.code != 404 or not url.startswith(MET + "json/"):
                raise
            # A different CDN edge can cache a missing JSON file while Irish
            # browsers receive it. Retry once with a fresh cache key.
            retried = True
            separator = "&" if "?" in url else "?"
            retry_url = f"{url}{separator}monitor={time.time_ns()}"
            retry_headers = {"Accept": "application/json,*/*", "Cache-Control": "no-cache", "User-Agent": "Mozilla/5.0 (compatible; MetEireannDarkSky-EndpointMonitor/1.0)"}
            response_code, content_type, data = fetch(retry_url, retry_headers)
        if len(data) > MAX_BODY:
            raise ValueError("response exceeds 6 MB")
        parsed = validate(check.kind, data, content_type)
        error = None
    except urllib.error.HTTPError as exc:
        response_code = exc.code
        error = f"HTTP {exc.code}"
        parsed = None
    except Exception as exc:
        error = f"{type(exc).__name__}: {exc}"
        parsed = None
    return ({"ok": error is None, "status": response_code, "ms": round((time.monotonic() - started) * 1000), "error": error, "url": url, "retried": retried}, parsed)


def derived_url(check: Check, source_data: object) -> str:
    if check.source == "rainfall":
        if not isinstance(source_data, list) or not source_data:
            raise ValueError("rainfall frame list unavailable")
        frame = source_data[-1]
        server = frame["server"]
        if urllib.parse.urlparse(server).hostname != "gdal.met.ie":
            raise ValueError("unexpected rainfall tile host")
        return f"{server.rstrip('/')}/api/maps/rainfall/{frame['src']}/30/21/6/{frame['modifiedTime']}"
    if check.source == "archive":
        if not isinstance(source_data, list) or not source_data:
            raise ValueError("archive image list unavailable")
        return f"https://www.met.ie/images/radar/{source_data[-1]}"
    raise ValueError("unknown derived check")


def run_checks(checks: list[Check], fail_id: str = "") -> dict[str, dict]:
    by_id = {check.id: check for check in checks}
    if len(by_id) != len(checks):
        raise ValueError("duplicate check ID")
    results: dict[str, dict] = {}
    payloads: dict[str, object] = {}
    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
        futures = {pool.submit(request, check): check for check in checks if not check.source}
        for future in concurrent.futures.as_completed(futures):
            check = futures[future]
            results[check.id], payloads[check.id] = future.result()
    for check in checks:
        if not check.source:
            continue
        try:
            dynamic = Check(check.id, check.label, derived_url(check, payloads.get(check.source)), check.kind)
            results[check.id], _ = request(dynamic)
        except Exception as exc:
            results[check.id] = {"ok": False, "status": None, "ms": None, "error": f"Dependency: {exc}", "url": ""}
    if fail_id:
        results[fail_id] = {**results[fail_id], "ok": False, "error": "Injected test failure"}
    return results


def append_history(history: dict, results: dict, at: str) -> dict:
    runs = history.get("runs", [])
    if not isinstance(runs, list):
        raise ValueError("history runs must be an array")
    return {"version": 1, "updated_at": at, "runs": ([*runs, {"at": at, "results": results}])[-20:]}


def current_history(history: dict, checks: list[Check]) -> dict:
    """Keep screen-active checks and correct previously accepted HTTP 404s."""
    active_ids = {check.id for check in checks}
    runs = []
    for run in history.get("runs", []):
        results = {}
        for check_id, result in run["results"].items():
            if check_id not in active_ids:
                continue
            if result.get("status") == 404:
                result = {**result, "ok": False, "error": "HTTP 404"}
            results[check_id] = result
        runs.append({"at": run["at"], "results": results})
    return {**history, "runs": runs}


def render_dashboard(history: dict, checks: list[Check]) -> str:
    runs = history.get("runs", [])
    latest = runs[-1]["results"] if runs else {}
    rows = []
    for check in checks:
        result = latest.get(check.id, {})
        status = "UP" if result.get("ok") and result.get("status") != 404 else "DOWN" if result else "PENDING"
        css = status.lower()
        recent = [run["results"].get(check.id) for run in reversed(runs)]
        pips = "".join(f'<span class="pip {"up" if item and item.get("ok") and item.get("status") != 404 else "down"}" title="{html.escape(run["at"])}"></span>' for run, item in zip(reversed(runs), recent))
        detail = html.escape(str(result.get("error") or ("HTTP 404" if result.get("status") == 404 else "200 on retry after 404" if result.get("retried") else "")))
        detail_class = "error recovered" if result.get("ok") and result.get("retried") else "error"
        url = result.get("url") or check.url
        safe_url = html.escape(url, quote=True)
        elapsed = f'{result["ms"]} ms' if result.get("ms") is not None else "—"
        code = str(result.get("status") or "—")
        rows.append(f'<tr><td><a href="{safe_url}" rel="noopener noreferrer">{html.escape(check.label)}</a><small>{html.escape(check.id)}</small></td><td><b class="badge {css}">{status}</b></td><td>{code}</td><td>{elapsed}</td><td class="history">{pips}</td><td class="{detail_class}">{detail}</td></tr>')
    checked = html.escape(history.get("updated_at", "Never"))
    failures = sum(not latest[check.id].get("ok") or latest[check.id].get("status") == 404 for check in checks if check.id in latest)
    headline = f"{failures} failing" if latest else "Awaiting first check"
    return f'''<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><meta http-equiv="refresh" content="300"><title>Weather API status</title><style>
:root{{font-family:system-ui,-apple-system,sans-serif;color:#eaf0f8;background:#0e1623}}body{{margin:0 auto;max-width:1400px;padding:24px}}header{{display:flex;justify-content:space-between;gap:16px;align-items:center;flex-wrap:wrap}}h1{{margin:0;font-size:1.8rem}}p{{color:#a9b9ca}}.summary{{padding:18px 20px;background:#192638;border-radius:14px;margin:22px 0}}.badge{{border-radius:99px;padding:5px 9px;font-size:.75rem}}.up{{background:#164b38;color:#8be9b4}}.down{{background:#632b35;color:#ffd1d5}}.pending{{background:#435067;color:#fff}}table{{width:100%;border-collapse:collapse;background:#172334;border-radius:14px;overflow:hidden}}th,td{{padding:12px;text-align:left;border-bottom:1px solid #304057;vertical-align:middle}}th{{color:#a9b9ca;font-size:.8rem}}th button{{border:0;background:none;color:inherit;font:inherit;font-weight:700;cursor:pointer;padding:0;text-align:left;white-space:nowrap}}th button:hover,th button:focus-visible{{color:#fff;text-decoration:underline}}th[aria-sort="ascending"] button::after{{content:" ▲"}}th[aria-sort="descending"] button::after{{content:" ▼"}}td small{{display:block;color:#8599ae;margin-top:3px}}a{{color:#cde8ff}}.history{{min-width:130px}}.pip{{display:inline-block;width:8px;height:18px;border-radius:3px;margin:1px;background:#435067}}.pip.up{{background:#28ba78}}.pip.down{{background:#ee6876}}.error{{max-width:280px;overflow-wrap:anywhere;color:#ffc2c7;font-size:.85rem}}.error.recovered{{color:#8be9b4}}.stale{{color:#ffcc7a;font-weight:700}}.scroll{{overflow-x:auto}}@media(max-width:700px){{body{{padding:14px}}th,td{{padding:9px;font-size:.84rem}}}}
</style></head><body><header><div><h1>Weather API status</h1><p>Met Éireann feeds used by the app screens · checked around 08:00 and 20:00 Ireland time</p></div><span id="freshness"></span></header><section class="summary"><strong>{headline}</strong><p>Last check: <time id="checked" datetime="{checked}">{checked}</time> · Last 20 checks per endpoint, newest on the left.</p></section><div class="scroll"><table><thead><tr><th scope="col" aria-sort="none"><button type="button" data-sort="name">Endpoint</button></th><th scope="col" aria-sort="none"><button type="button" data-sort="status">Latest</button></th><th scope="col" aria-sort="none"><button type="button" data-sort="http">HTTP</button></th><th scope="col" aria-sort="none"><button type="button" data-sort="time">Time</button></th><th scope="col" aria-sort="none"><button type="button" data-sort="history">Last 20</button></th><th scope="col" aria-sort="none"><button type="button" data-sort="error">Error</button></th></tr></thead><tbody>{''.join(rows)}</tbody></table></div><p>Latest status reflects the most recent scheduled check, not each request made by a phone. HTTP 404 is shown as DOWN for every feed.</p><script>
const time=document.getElementById('checked');const last=Date.parse(time.dateTime);
if(Number.isFinite(last))time.textContent=new Intl.DateTimeFormat('en-IE',{{dateStyle:'medium',timeStyle:'short',timeZone:'Europe/Dublin'}}).format(last)+' Ireland time';
function freshness(){{const stale=!Number.isFinite(last)||Date.now()-last>14*60*60*1000;const el=document.getElementById('freshness');el.textContent=stale?'Checks overdue':'Checks current';el.className=stale?'stale':''}}
freshness();setInterval(freshness,60000);
const body=document.querySelector('tbody');const original=[...body.rows];
const values={{name:row=>row.cells[0].querySelector('a').textContent,status:row=>row.cells[1].textContent.trim(),http:row=>Number.parseInt(row.cells[2].textContent,10),time:row=>Number.parseFloat(row.cells[3].textContent),history:row=>row.cells[4].querySelectorAll('.pip.up').length,error:row=>row.cells[5].textContent.trim()}};
const numeric=new Set(['http','time','history']);const collator=new Intl.Collator('en',{{numeric:true,sensitivity:'base'}});
document.querySelectorAll('th button[data-sort]').forEach(button=>button.addEventListener('click',()=>{{
  const header=button.parentElement;const direction=header.getAttribute('aria-sort')==='ascending'?'descending':'ascending';
  document.querySelectorAll('th[aria-sort]').forEach(item=>item.setAttribute('aria-sort','none'));
  header.setAttribute('aria-sort',direction);const key=button.dataset.sort;
  const sorted=[...original].sort((a,b)=>{{const left=values[key](a),right=values[key](b);
    const missingLeft=numeric.has(key)&&Number.isNaN(left),missingRight=numeric.has(key)&&Number.isNaN(right);
    if(missingLeft||missingRight)return missingLeft===missingRight?original.indexOf(a)-original.indexOf(b):missingLeft?1:-1;
    const order=numeric.has(key)?left-right:collator.compare(left,right);
    return order?(direction==='ascending'?order:-order):original.indexOf(a)-original.indexOf(b);
  }});
  body.replaceChildren(...sorted);
}}));
</script></body></html>'''


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--history", type=Path, required=True)
    parser.add_argument("--site", type=Path, required=True)
    parser.add_argument("--fail-id", default="", help="test failure injection; omit in production")
    args = parser.parse_args()
    checks = catalogue()
    if args.fail_id and args.fail_id not in {check.id for check in checks}:
        parser.error("unknown failure injection ID")
    history = json.loads(args.history.read_text()) if args.history.exists() else {"version": 1, "runs": []}
    history = current_history(history, checks)
    results = run_checks(checks, args.fail_id)
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    history = append_history(history, results, now)
    args.history.parent.mkdir(parents=True, exist_ok=True)
    args.history.write_text(json.dumps(history, indent=2, sort_keys=True) + "\n")
    args.site.mkdir(parents=True, exist_ok=True)
    (args.site / "index.html").write_text(render_dashboard(history, checks))
    (args.site / ".nojekyll").touch()
    failed = [check.label for check in checks if not results[check.id]["ok"]]
    print(f"Checked {len(checks)} endpoints; {len(failed)} failed")
    for label in failed:
        print(f"FAIL: {label}")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
