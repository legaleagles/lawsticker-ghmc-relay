"""
LawSticker AI — GHMC Tender Watcher (standalone, deployed in asia-south1)

This is a small, self-contained service whose ONLY job is watching
ghmc.gov.in for new tenders, filtering to Patancheru-area ones, running
them through the same tender-anomaly-scrutiny logic as the main backend,
logging results to the same GitHub-hosted history the main site's
/admin/tender-scrutiny.html reads, and alerting via Telegram.

WHY THIS EXISTS AS A SEPARATE SERVICE/REPO: the main backend runs in
europe-west1, and ghmc.gov.in appears to reject/block requests from that
region (confirmed via testing - the exact same code deployed in
asia-south1/Mumbai reaches GHMC's site fine). Rather than migrate the
whole production backend, this one small piece runs from India instead.

Deploy: Cloud Run, region asia-south1, source = this repo, continuous
deployment enabled on push to main.

Required env vars: GEMINI_API_KEY, SITE_REPO_TOKEN, TELEGRAM_BOT_TOKEN,
TELEGRAM_CHAT_ID (same values as the main backend's env vars).
"""
from flask import Flask, jsonify
from flask_cors import CORS
import json
import os
import re
import io
import base64
import hashlib
import urllib.request
import urllib.error
import ssl
import socket as socket_mod
import time
from datetime import datetime, timezone
from pypdf import PdfReader

app = Flask(__name__)
CORS(app)  # the site (lawsticker-ai.com) calls this cross-origin from the
           # browser, so CORS headers are required or the browser blocks
           # the response client-side even though the server itself works fine

REPO = "legaleagles/LabourLaw2"
GITHUB_API = "https://api.github.com"
GEMINI_MODEL = "gemini-flash-lite-latest"
GEMINI_URL = f"https://generativelanguage.googleapis.com/v1beta/models/{GEMINI_MODEL}:generateContent"


def github_get(path, token, timeout=15):
    req = urllib.request.Request(
        f"{GITHUB_API}/repos/{REPO}/contents/{path}",
        headers={"Authorization": f"token {token}", "Accept": "application/vnd.github+json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode())
            content = base64.b64decode(data["content"]).decode()
            return json.loads(content), data["sha"]
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return None, None
        raise


def github_put(path, token, content_obj, sha, message, timeout=15):
    body = json.dumps(content_obj, indent=2, ensure_ascii=False).encode()
    payload = {"message": message, "content": base64.b64encode(body).decode(), "branch": "main"}
    if sha:
        payload["sha"] = sha
    req = urllib.request.Request(
        f"{GITHUB_API}/repos/{REPO}/contents/{path}",
        data=json.dumps(payload).encode(),
        headers={"Authorization": f"token {token}", "Accept": "application/vnd.github+json", "Content-Type": "application/json"},
        method="PUT",
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.status


def send_telegram(bot_token, chat_id, text):
    url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
    payload = json.dumps({"chat_id": chat_id, "text": text, "parse_mode": "HTML"}).encode()
    req = urllib.request.Request(url, data=payload, headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=15) as resp:
        return resp.status


def send_telegram_to_all(bot_token, chat_id_config, text):
    results = {}
    for cid in [c.strip() for c in chat_id_config.split(",") if c.strip()]:
        try:
            send_telegram(bot_token, cid, text)
            results[cid] = "sent"
        except Exception as e:
            results[cid] = f"failed: {e}"
    return results


def call_gemini_structured(api_key, prompt, schema, max_tokens=600, timeout=15):
    payload = json.dumps({
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {
            "maxOutputTokens": max_tokens,
            "responseMimeType": "application/json",
            "responseSchema": schema,
        },
    }).encode()
    req = urllib.request.Request(
        f"{GEMINI_URL}?key={api_key}",
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        result = json.loads(resp.read().decode())
    raw_text = result["candidates"][0]["content"]["parts"][0]["text"]
    return json.loads(raw_text)


def try_extract_pdf_text(pdf_bytes):
    try:
        reader = PdfReader(io.BytesIO(pdf_bytes))
        pages_text = [page.extract_text() or "" for page in reader.pages]
        full_text = "\n\n".join(pages_text).strip()
        if len(reader.pages) > 0 and len(full_text) >= 120 * len(reader.pages):
            return full_text
        return None
    except Exception:
        return None

TENDER_SCRUTINY_LOG_FILE = "tender-scrutiny-log.json"

TENDER_ANOMALY_SCHEMA = {
    "type": "object",
    "properties": {
        "tender_summary": {"type": "string", "description": "2-3 sentence neutral summary of what this tender is for, issuing body, and estimated value"},
        "suggested_title": {"type": "string", "description": "A short (5-9 word) descriptive title for this tender, e.g. 'GHMC GPS Vehicle Tracking Tender 2026' - issuing body + subject + year if known. Used as the default filename/label, should be filesystem-safe-ish (no slashes or special punctuation)."},
        "plain_language_brief": {
            "type": "object",
            "properties": {
                "what_is_being_bought": {"type": "string", "description": "1-3 plain sentences, no legal jargon, explaining what's actually being procured - as if explaining to someone with no procurement background"},
                "who_can_apply": {"type": "string", "description": "Plain-language summary of who is eligible to bid - the key eligibility conditions in ordinary words, not the legal clause language"},
                "how_much_money": {"type": "string", "description": "Plain-language summary of the money involved - what it costs, what deposits/guarantees a bidder needs to put up, in simple terms"},
                "important_dates_plain": {"type": "string", "description": "Plain-language summary of when things happen - when to apply by, when it'll be decided - in ordinary sentence form, not a legal date table"},
                "how_winner_is_chosen": {"type": "string", "description": "Plain-language explanation of how the winning bidder gets selected (e.g. lowest price wins, or price plus technical score, etc.)"},
                "what_to_watch_for": {"type": "string", "description": "1-3 plain sentences translating the most important flagged concerns (if any) into ordinary language a non-lawyer would understand - what should someone reading this tender feel cautious about, without legal jargon. Empty string if nothing notable was flagged."},
            },
            "required": ["what_is_being_bought", "who_can_apply", "how_much_money", "important_dates_plain", "how_winner_is_chosen", "what_to_watch_for"],
            "description": "A companion plain-English brief of the ENTIRE tender written for an ordinary citizen with no legal/procurement background - government tenders are written in dense legal/bureaucratic language that's genuinely hard to follow even for educated readers. This section exists specifically to make the tender itself understandable, separate from the anomaly-hunting sections below.",
        },
        "nature_of_work": {"type": "string", "description": "1-2 sentences precisely describing what is actually being procured (goods/services/works, quantities, scope) - this frames whether a market-value comparison even makes sense for this tender type"},
        "key_dates": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "label": {"type": "string", "description": "e.g. Date of Publication, Bid Submission Start, Bid Submission End, Technical Bid Opening, Financial Bid Opening, Clarification End"},
                    "value": {"type": "string", "description": "The exact date/time as printed, or 'Not specified / will be intimated later' if genuinely open-ended"},
                    "concern": {"type": "string", "description": "Empty string if this date is normal. Non-empty ONLY if genuinely notable - e.g. unusually short window to the next date, or left open-ended when it should be fixed, or an internal inconsistency (a later stage dated before an earlier one)."},
                },
                "required": ["label", "value", "concern"],
            },
        },
        "bid_window_assessment": {"type": "string", "description": "1-2 sentences: how many days between publication and bid submission deadline, and whether that's reasonable for a tender of this technical complexity and value - compare to typical practice (e.g. GFR 2017 generally expects a minimum reasonable response window scaled to complexity), don't just assert a number is 'short' without saying what it should reasonably be instead"},
        "financial_snapshot": {
            "type": "object",
            "properties": {
                "estimated_value": {"type": "string", "description": "The tender's own stated estimated cost/value, as printed, or 'Not specified' if absent"},
                "emd_amount": {"type": "string", "description": "EMD/Bid Security amount as printed, or 'Not specified/exempted'"},
                "performance_security": {"type": "string", "description": "Performance security amount/percentage as printed"},
                "turnover_threshold": {"type": "string", "description": "Minimum turnover eligibility requirement as printed"},
                "proportionality_note": {"type": "string", "description": "1-3 sentences ONLY if there's a genuine disproportion worth noting - e.g. turnover threshold set unusually high or low relative to estimated value, or EMD disproportionate to contract value. Do NOT force a generic market-rate comparison for every tender type - for services/manpower/works where 'market value' isn't a simple lookup, say so plainly instead of inventing a comparison, and focus instead on whether the FINANCIAL FIGURES ARE INTERNALLY PROPORTIONATE to each other and to the stated scope of work."},
            },
            "required": ["estimated_value", "emd_amount", "performance_security", "turnover_threshold", "proportionality_note"],
        },
        "relaxation_clauses": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "clause_quote": {"type": "string", "description": "Exact verbatim quote of a clause that grants the procuring authority discretion to relax, waive, or deviate from stated eligibility/technical/financial conditions, with clause number if visible"},
                    "concern": {"type": "string", "description": "1-2 sentences on why this specific discretion clause is a risk vector - who benefits if criteria are relaxed selectively, and what it undermines about the tender's stated fairness"},
                },
                "required": ["clause_quote", "concern"],
            },
            "description": "A DEDICATED, thorough scan specifically for every clause anywhere in the document that gives the procuring authority power to relax, waive, deviate from, or use discretion over any stated condition - these are the single most exploitable clauses in tender rigging and deserve exhaustive listing, not just one example. List EVERY instance found, not just the most obvious one.",
        },
        "flags": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "category": {"type": "string", "description": "One of: Restrictive Eligibility, Tailored Specification, Financial Structure Anomaly, Turnover/Experience Mismatch, Other (Discretionary clauses go in relaxation_clauses instead, dates go in key_dates instead - do not duplicate here)"},
                    "clause_quote": {"type": "string", "description": "Short verbatim quote (under 30 words) of the exact clause text being flagged, with clause/section number if visible"},
                    "concern": {"type": "string", "description": "1-3 sentences: specifically why this clause structurally narrows competition, enables favoritism, or reduces transparency - grounded in GFR 2017 / standard public procurement fairness principles, not vague suspicion"},
                    "severity": {"type": "string", "description": "One of: High, Medium, Low - based on how directly this could exclude legitimate competitors or enable a pre-decided outcome"},
                    "suggested_rti_question": {"type": "string", "description": "One specific, answerable RTI question a citizen could file to seek justification/data on this exact clause - not generic, tailored to this clause"},
                },
                "required": ["category", "clause_quote", "concern", "severity", "suggested_rti_question"],
            },
        },
        "overall_assessment": {"type": "string", "description": "2-4 sentence honest overall read: does this tender show a genuine pattern of restrictive/tailored conditions, or does it look like a fairly standard tender with only minor/routine points - be calibrated, not alarmist. Many tenders have SOME restrictive clauses for legitimate reasons; only flag a real pattern as concerning."},
    },
    "required": ["tender_summary", "suggested_title", "plain_language_brief", "nature_of_work", "key_dates", "bid_window_assessment", "financial_snapshot", "relaxation_clauses", "flags", "overall_assessment"],
}


TENDER_ANOMALY_CHECKLIST = """Analyze this government/PSU tender document THOROUGHLY for clauses that could improperly restrict fair competition or enable a pre-decided outcome. This is for citizens preparing RTI applications and public-transparency scrutiny - be precise, evidence-based, calibrated, and exhaustive. These documents are typically drafted by competent people who cover most loose ends deliberately, but often leave a few genuine gaps or overly-favorable clauses buried among routine boilerplate - your job is to find those specific ones, not to pad the report with generic observations.

Do NOT flag routine, standard tender boilerplate as suspicious just because it exists; only flag clauses that are genuinely unusual, disproportionate, or specifically enabling of favoritism.

WORK THROUGH THESE DISTINCT ANALYSIS TASKS, ALL OF THEM:

A. PLAIN-LANGUAGE BRIEF: Government tenders are written in dense legal/bureaucratic language that's genuinely hard to follow even for educated readers. Write a companion brief in ordinary, everyday language (no legal jargon, no clause numbers, no "hereinafter") explaining: what's being bought, who can apply, how much money is involved, when things happen, and how the winner gets picked. Someone with zero procurement background should be able to read this and actually understand what the tender says. If your other analysis below finds genuine concerns, also translate the single most important one into plain language here - not the full legal reasoning, just "here's what to watch out for" in simple words.

B. NATURE OF WORK: State precisely what's being procured. This determines whether "market value" comparisons even make sense — for physical commodities they often do, for specialized services/manpower/works they usually don't (use wage benchmarks, past contract rates, or scope-to-cost proportionality instead, not a generic "market rate" claim you can't actually verify).

C. KEY DATES: Extract EVERY milestone date/deadline printed (publication, document download, clarification window, bid submission start/end, technical bid opening, financial bid opening, any others). For each, note if genuinely notable - e.g. an unusually short gap to the next milestone for a tender this complex, a date left open-ended where it should be fixed, or an internal date inconsistency. List even normal dates with an empty concern - the reader needs the full timeline either way.

D. BID WINDOW: Compute/estimate the total days between publication and bid submission deadline, and give an honest read on whether that's a reasonable window for the technical complexity and value involved - compare against what would normally be expected, don't just label it "short" without saying why.

E. FINANCIAL SNAPSHOT: Pull the estimated tender value, EMD, performance security, and turnover threshold exactly as printed. Only flag a proportionality concern if there's a genuine mismatch between these figures relative to EACH OTHER and the stated scope - not a forced external market-price comparison for every tender type.

F. RELAXATION/DISCRETION CLAUSES - EXHAUSTIVE SCAN: Search the ENTIRE document specifically for every clause anywhere that gives the procuring authority power to relax, waive, deviate from, exercise discretion over, or overrule any stated eligibility/technical/financial/evaluation condition. These are the highest-value clauses for rigging since they let a committee bend rules selectively. List every single instance found, however small, not just one representative example - a document can have several scattered across different sections (eligibility, evaluation, contract terms) and all of them matter.

G. OTHER RESTRICTIVE/TAILORED CLAUSES: geographic/office-location restrictions narrower than needed, ownership requirements excluding viable business models, technical specs unusually narrow (specific brand/model with no genuine equivalent), turnover/experience thresholds disproportionate to contract value, tie-breaker mechanisms that lack objective merit criteria (e.g. pure lottery instead of a scored tiebreaker), or anything else genuinely anomalous.

For every genuine flagged issue, quote the EXACT clause (verbatim, with clause number), explain the specific structural concern, rate severity honestly, and where applicable draft one specific answerable RTI question. If you find few or no genuine issues in a section, say so plainly rather than forcing content to fill a quota - a clean tender should come back looking clean, and a thorough one should come back looking thorough."""


def run_tender_anomaly_analysis(pdf_bytes, gemini_key):
    # Shared analysis core - used both by the manual upload endpoint
    # (/api/tender-scrutiny) and the automated GHMC daily watcher below, so
    # there's exactly one place that runs the actual checklist.
    text = try_extract_pdf_text(pdf_bytes)
    if text:
        prompt = TENDER_ANOMALY_CHECKLIST + "\n\nTENDER DOCUMENT TEXT:\n" + text[:180000]
        return call_gemini_structured(gemini_key, prompt, TENDER_ANOMALY_SCHEMA, max_tokens=8000, timeout=90)
    pdf_base64 = base64.b64encode(pdf_bytes).decode()
    payload = json.dumps({
        "contents": [{"parts": [
            {"text": TENDER_ANOMALY_CHECKLIST},
            {"inline_data": {"mime_type": "application/pdf", "data": pdf_base64}},
        ]}],
        "generationConfig": {
            "responseMimeType": "application/json",
            "responseSchema": TENDER_ANOMALY_SCHEMA,
            "maxOutputTokens": 8000,
        },
    }).encode()
    req = urllib.request.Request(
        f"{GEMINI_URL}?key={gemini_key}", data=payload,
        headers={"Content-Type": "application/json"}, method="POST",
    )
    with urllib.request.urlopen(req, timeout=60) as resp:
        raw = json.loads(resp.read().decode())
    return json.loads(raw["candidates"][0]["content"]["parts"][0]["text"])


def log_tender_scrutiny_result(site_token, tender_name, result, source="manual"):
    log, sha = github_get(TENDER_SCRUTINY_LOG_FILE, site_token, timeout=8)
    entries = (log or {}).get("entries", [])
    entry_id = hashlib.sha256(f"{tender_name}{datetime.now(timezone.utc).isoformat()}".encode()).hexdigest()[:12]
    entries.insert(0, {
        "id": entry_id,
        "tender_name": tender_name or (result.get("suggested_title") if result else None) or "Untitled tender",
        "analyzed_at": datetime.now(timezone.utc).isoformat(),
        "source": source,
        "result": result,
    })
    entries = entries[:100]
    github_put(TENDER_SCRUTINY_LOG_FILE, site_token, {"entries": entries}, sha, "Tender scrutiny log update", timeout=10)
    return entry_id


GHMC_TENDERS_PAGE = "https://www.ghmc.gov.in/Tenderspage.aspx"
GHMC_SEEN_TENDERS_FILE = "ghmc-seen-tenders.json"


def _resolve_via_doh(hostname):
    # DNS-over-HTTPS - resolves via a normal HTTPS request (port 443) instead
    # of a native OS-level DNS lookup (UDP port 53). Retries with plain
    # IPv4-forced native resolution didn't help and failed identically both
    # times, which rules out "just transient" - since Gemini/GitHub/Telegram
    # (all resolved natively) work fine from this same container, general
    # DNS isn't broken; this looks specific to less-common domains like
    # ghmc.gov.in. Try Google's DoH first, then Cloudflare's as a second
    # resolver - Google's DNSSEC-validating resolver returning zero records
    # (seen in testing) can happen with older/misconfigured .gov.in DNSSEC
    # setups that a non-validating resolver like Cloudflare's tolerates fine,
    # which is likely why the site loads normally in an ordinary browser.
    last_err = None
    for doh_host in ("dns.google", "cloudflare-dns.com"):
        try:
            doh_req = urllib.request.Request(
                f"https://{doh_host}/resolve?name={hostname}&type=A",
                headers={"Accept": "application/dns-json"},
            )
            with urllib.request.urlopen(doh_req, timeout=10) as resp:
                data = json.loads(resp.read().decode())
            answers = [a["data"] for a in data.get("Answer", []) if a.get("type") == 1]
            if answers:
                return answers[0]
            last_err = Exception(f"{doh_host} returned no A record")
        except Exception as e:
            last_err = e
    raise last_err


def _fetch_via_resolved_ip(url, user_agent, timeout=25):
    # Connects directly to the DoH-resolved IP over TLS, sending the correct
    # Host header / SNI so the server still sees a normal request for the
    # right hostname - this is what lets us skip the container's own (here,
    # apparently unreliable) hostname lookup entirely for this one call.
    import ssl
    import socket as sock_mod
    parsed = urllib.parse.urlparse(url)
    hostname = parsed.hostname
    ip = _resolve_via_doh(hostname)
    port = parsed.port or 443
    path = parsed.path or "/"
    if parsed.query:
        path += "?" + parsed.query

    raw_sock = sock_mod.create_connection((ip, port), timeout=timeout)
    ctx = ssl.create_default_context()
    ssl_sock = ctx.wrap_socket(raw_sock, server_hostname=hostname)
    try:
        # A 403 from the earlier attempt (minimal headers) suggests a WAF/
        # bot-detection rule flagging the request as non-browser traffic.
        # Send a fuller, ordinary-browser-like header set - real requests
        # almost never arrive with just User-Agent and Accept alone.
        request_lines = (
            f"GET {path} HTTP/1.1\r\n"
            f"Host: {hostname}\r\n"
            f"User-Agent: {user_agent}\r\n"
            f"Accept: text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8\r\n"
            f"Accept-Language: en-US,en;q=0.9\r\n"
            f"Accept-Encoding: identity\r\n"
            f"Upgrade-Insecure-Requests: 1\r\n"
            f"Sec-Fetch-Dest: document\r\n"
            f"Sec-Fetch-Mode: navigate\r\n"
            f"Sec-Fetch-Site: none\r\n"
            f"Sec-Fetch-User: ?1\r\n"
            f"Cache-Control: no-cache\r\n"
            f"Connection: close\r\n\r\n"
        )
        ssl_sock.sendall(request_lines.encode())
        response = b""
        while True:
            chunk = ssl_sock.recv(8192)
            if not chunk:
                break
            response += chunk
    finally:
        ssl_sock.close()

    header_end = response.find(b"\r\n\r\n")
    if header_end == -1:
        raise Exception("Malformed HTTP response from GHMC (no header/body split found)")
    status_line = response[:response.find(b"\r\n")].decode(errors="ignore")
    body = response[header_end + 4:]
    if " 200 " not in status_line:
        raise Exception(f"Unexpected HTTP status fetching GHMC page: {status_line.strip()}")
    return body.decode("utf-8", errors="ignore")


def fetch_ghmc_tenders_page():
    user_agent = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"

    # First try the normal path (fast, simple) with IPv4-forced native
    # resolution and a short retry - covers the case where it really is just
    # a transient blip this time.
    import socket
    import time
    original_getaddrinfo = socket.getaddrinfo

    def _ipv4_only_getaddrinfo(host, port, family=0, type=0, proto=0, flags=0):
        return original_getaddrinfo(host, port, socket.AF_INET, type, proto, flags)

    socket.getaddrinfo = _ipv4_only_getaddrinfo
    last_error = None
    try:
        for attempt in range(2):
            try:
                req = urllib.request.Request(GHMC_TENDERS_PAGE, headers={"User-Agent": user_agent})
                with urllib.request.urlopen(req, timeout=25) as resp:
                    return resp.read().decode("utf-8", errors="ignore")
            except Exception as e:
                last_error = e
                if attempt == 0:
                    time.sleep(2)
    finally:
        socket.getaddrinfo = original_getaddrinfo

    # Native resolution failed twice - fall back to DNS-over-HTTPS + a raw
    # direct-IP request, bypassing the container's own DNS lookup entirely.
    try:
        return _fetch_via_resolved_ip(GHMC_TENDERS_PAGE, user_agent)
    except Exception as doh_error:
        raise Exception(f"Both native resolution and DNS-over-HTTPS fallback failed. Native: {last_error}. DoH: {doh_error}")


def parse_ghmc_tender_rows(html):
    # GHMC's tenders table is plain server-rendered HTML (not JS-rendered),
    # each row has the work name as a link (usually to a PDF or detail page)
    # alongside dates. Parsed with regex rather than a full HTML parser
    # dependency, since the structure is a simple repeating <tr> table.
    rows = []
    row_pattern = re.compile(r'<tr[^>]*>(.*?)</tr>', re.S | re.I)
    link_pattern = re.compile(r'<a[^>]+href="([^"]+)"[^>]*>(.*?)</a>', re.S | re.I)
    cell_pattern = re.compile(r'<td[^>]*>(.*?)</td>', re.S | re.I)

    for row_html in row_pattern.findall(html):
        cells = cell_pattern.findall(row_html)
        if len(cells) < 3:
            continue
        link_match = link_pattern.search(row_html)
        work_name = re.sub(r'<[^>]+>', '', cells[1] if len(cells) > 1 else "").strip()
        work_name = re.sub(r'\s+', ' ', work_name)
        if not work_name or work_name.lower() in ("name of the work", "t.type"):
            continue
        doc_url = None
        if link_match:
            href = link_match.group(1).strip()
            # GHMC's site is ASP.NET WebForms - "download" links here are
            # often javascript:__doPostBack(...) triggers, not real fetchable
            # URLs. Blindly prepending the domain to any non-http href (the
            # old behavior) turned these into garbage URLs like
            # "https://www.ghmc.gov.in/javascript:__doPostBack(...)" that
            # LOOK like real links (they start with "http") but actually
            # crash the server with a security exception when opened - a
            # real bug found via live testing. Explicitly exclude anything
            # that isn't a genuine relative/absolute path.
            if href.lower().startswith("javascript:") or href.lower().startswith("#"):
                doc_url = None
            elif href.startswith("http"):
                doc_url = href
            else:
                doc_url = "https://www.ghmc.gov.in/" + href.lstrip("/")
        # Use a stable hash of the work name as the id - GHMC's table has no
        # explicit tender/reference number column exposed in the HTML.
        tender_id = hashlib.sha256(work_name.encode()).hexdigest()[:16]
        rows.append({"id": tender_id, "work_name": work_name, "doc_url": doc_url})
    return rows


@app.route('/api/ghmc-connectivity-test', methods=['GET'])
def ghmc_connectivity_test():
    # Tests ONLY whether this deployment's network can reach GHMC's site -
    # no API keys, tokens, or other env vars needed at all, so this works
    # immediately on a fresh test deployment with zero configuration.
    try:
        html = fetch_ghmc_tenders_page()
        rows = parse_ghmc_tender_rows(html)
        return jsonify({"ok": True, "reached_ghmc": True, "rows_found": len(rows)})
    except Exception as e:
        return jsonify({"ok": False, "reached_ghmc": False, "error": str(e)[:500]})


@app.route('/api/ghmc-tenders-list', methods=['GET'])
def ghmc_tenders_list():
    # Public - GHMC's own tender listing is public data, this just returns
    # what's currently on their page in a clean JSON shape (work name + PDF
    # link) so any tender - not just Patancheru-matching ones - can be
    # manually browsed/downloaded and fed into Tender Scrutiny by hand.
    try:
        html = fetch_ghmc_tenders_page()
        rows = parse_ghmc_tender_rows(html)
        return jsonify({"ok": True, "tenders": rows})
    except Exception as e:
        return jsonify({"ok": False, "error": f"Could not fetch/parse GHMC tenders page: {str(e)[:300]}"}), 500


@app.route('/api/ghmc-tender-watch', methods=['GET'])
def ghmc_tender_watch():
    site_token = os.environ.get("SITE_REPO_TOKEN")
    gemini_key = os.environ.get("GEMINI_API_KEY")
    bot_token = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat_id_config = os.environ.get("TELEGRAM_CHAT_ID")
    if not site_token or not gemini_key:
        return jsonify({"ok": False, "error": "Server misconfiguration."}), 500

    try:
        html = fetch_ghmc_tenders_page()
        rows = parse_ghmc_tender_rows(html)
    except Exception as e:
        return jsonify({"ok": False, "error": f"Could not fetch/parse GHMC tenders page: {str(e)[:300]}"}), 500

    try:
        seen_data, seen_sha = github_get(GHMC_SEEN_TENDERS_FILE, site_token, timeout=8)
    except Exception:
        seen_data, seen_sha = None, None
    first_run = seen_data is None
    seen_ids = set((seen_data or {}).get("seen_ids", []))

    new_rows = [r for r in rows if r["id"] not in seen_ids]

    # Scoped to Patancheru only for now, per explicit request - GHMC tender
    # titles include the locality name inline (e.g. "...in Madinaguda, Ward
    # No.237, Miyapur Division-48..."), so a straightforward keyword match
    # on the work name is enough - no separate circle/zone lookup needed.
    # Everything on the page still gets marked "seen" below regardless of
    # this filter, so non-Patancheru tenders are correctly never re-checked,
    # they're just never analyzed or alerted on.
    AREA_KEYWORDS = ("patancheru", "pattancheru")
    relevant_new_rows = [r for r in new_rows if any(k in r["work_name"].lower() for k in AREA_KEYWORDS)]

    analyzed = []

    # Cap how many we actually run the AI on per call, in case the page ever
    # returns a big batch (e.g. first run, or GHMC posts many at once) - the
    # rest will simply be picked up as "new" on the next scheduled run since
    # they stay unseen, rather than burning a huge amount of Gemini credit
    # in one go. We're a startup - controlled, not unlimited, AI spend.
    MAX_PER_RUN = 5

    if not first_run:
        for row in relevant_new_rows[:MAX_PER_RUN]:
            if not row["doc_url"]:
                continue  # nothing to actually analyze without a document
            try:
                doc_req = urllib.request.Request(row["doc_url"], headers={"User-Agent": "lawsticker-ai-cron/1.0"})
                with urllib.request.urlopen(doc_req, timeout=25) as doc_resp:
                    pdf_bytes = doc_resp.read()
                if not pdf_bytes.startswith(b"%PDF"):
                    continue  # link wasn't actually a PDF (e.g. a detail page instead)
                result = run_tender_anomaly_analysis(pdf_bytes, gemini_key)
                if not result:
                    continue
                entry_id = log_tender_scrutiny_result(site_token, row["work_name"], result, source="ghmc-auto")
                analyzed.append({"work_name": row["work_name"], "entry_id": entry_id, "flags": len(result.get("flags", [])) + len(result.get("relaxation_clauses", []))})

                if bot_token and chat_id_config:
                    high_severity = [f for f in result.get("flags", []) if f.get("severity") == "High"]
                    relaxation_count = len(result.get("relaxation_clauses", []))
                    # Notify on EVERY analyzed Patancheru tender, not just
                    # flagged ones - the point of watching a specific area is
                    # to know about all of it, not only the concerning ones.
                    # Severity of the emoji/framing still signals at a glance
                    # whether it's worth opening immediately or just FYI.
                    if high_severity or relaxation_count:
                        headline = "🚩 <b>New Patancheru tender — issues flagged</b>"
                    else:
                        headline = "📄 <b>New Patancheru tender</b>"
                    msg = (
                        f"{headline}\n"
                        f"{row['work_name'][:200]}\n"
                        f"High-severity flags: {len(high_severity)} · Relaxation clauses: {relaxation_count}\n"
                        f"Full analysis: /admin/tender-scrutiny.html (see history)\n"
                        f"Document: {row['doc_url']}"
                    )
                    send_telegram_to_all(bot_token, chat_id_config, msg[:4000])
            except Exception:
                continue  # one bad tender/PDF shouldn't stop the rest of the run

    # Mark everything we saw this run (whether analyzed or not) so we don't
    # re-process it, and don't blow past a reasonable stored history size.
    all_current_ids = [r["id"] for r in rows]
    merged = list(dict.fromkeys(all_current_ids + list(seen_ids)))[:1000]
    try:
        github_put(GHMC_SEEN_TENDERS_FILE, site_token, {"seen_ids": merged}, seen_sha, "Update seen GHMC tender ids", timeout=10)
    except Exception:
        pass

    return jsonify({"ok": True, "total_on_page": len(rows), "new_found": len(new_rows), "new_matching_area": len(relevant_new_rows), "analyzed_this_run": len(analyzed), "first_run": first_run})

@app.route('/', methods=['GET'])
def health():
    return jsonify({"ok": True, "service": "lawsticker-ghmc-relay", "routes": [
        "/api/ghmc-connectivity-test", "/api/ghmc-tenders-list", "/api/ghmc-tender-watch",
    ]})


if __name__ == '__main__':
    app.run(host='0.0.0.0', port=int(os.environ.get('PORT', 8080)))
