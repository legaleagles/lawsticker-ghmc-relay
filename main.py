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

# ghmc.gov.in's own listing carries no Patancheru-area data at all (confirmed
# via testing - it's a small unrelated "General quotations" table). Real
# Patancheru/Ameenpur-circle tenders live on the state eProcurement system,
# which tenderdetail.com mirrors into a plain static page under the GHMC
# authority tag (still true even after GHMC's Feb-2026 three-way split into
# GHMC/Cyberabad Municipal Corporation/Malkajgiri - aggregator tagging hasn't
# fully caught up to that yet, so filtering by place-name in the title is
# more reliable right now than filtering by authority name).
TENDERDETAIL_GHMC_PAGES = [
    f"https://www.tenderdetail.com/government-tenders/tenders-for-greater-hyderabad-municipal-corporation/{n}?agid=3036"
    for n in (1, 2, 3)
]
PATANCHERU_AREA_KEYWORDS = (
    "patancheru", "pattancheru", "patancheruvu", "ameenpur", "ramachandrapuram",
    "rc puram", "r.c.puram", "beeramguda", "bollaram", "bollarum", "tellapur",
    "muthangi", "circle-45", "circle-46", "circle-47", "circle 45", "circle 46",
    "circle 47", "circle-49", "circle 49", "slpz", "serilingampally",
)


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
    return body  # raw bytes - caller decodes if it's text (HTML), or uses as-is if binary (PDF)


def fetch_ghmc_url_hardened(url, timeout=25):
    # Generalized version of the page-fetch hardening below - same
    # native+IPv4+retry then DNS-over-HTTPS fallback, but works for ANY
    # ghmc.gov.in URL (the tenders list page OR an individual tender's PDF),
    # returning raw bytes so binary content isn't corrupted by text decoding.
    # This replaces a separate, weaker fetch that individual PDF downloads
    # were using (bare "lawsticker-ai-cron/1.0" User-Agent, no DoH fallback,
    # no retry) - likely why PDF fetches were failing even when the tenders
    # list itself fetched fine.
    user_agent = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
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
                req = urllib.request.Request(url, headers={
                    "User-Agent": user_agent,
                    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,application/pdf,*/*;q=0.8",
                    "Accept-Language": "en-US,en;q=0.9",
                })
                with urllib.request.urlopen(req, timeout=timeout) as resp:
                    return resp.read()
            except Exception as e:
                last_error = e
                if attempt == 0:
                    time.sleep(2)
    finally:
        socket.getaddrinfo = original_getaddrinfo

    try:
        return _fetch_via_resolved_ip(url, user_agent, timeout=timeout)
    except Exception as doh_error:
        raise Exception(f"Both native resolution and DNS-over-HTTPS fallback failed. Native: {last_error}. DoH: {doh_error}")


def fetch_ghmc_tenders_page():
    return fetch_ghmc_url_hardened(GHMC_TENDERS_PAGE).decode("utf-8", errors="ignore")


def parse_ghmc_tender_rows(html):
    # GHMC's tenders table is plain server-rendered HTML (not JS-rendered),
    # each row has the work name plus one or more links. Parsed with regex
    # rather than a full HTML parser dependency, since the structure is a
    # simple repeating <tr> table.
    rows = []
    row_pattern = re.compile(r'<tr[^>]*>(.*?)</tr>', re.S | re.I)
    link_pattern = re.compile(r'<a[^>]+href="([^"]+)"[^>]*>(.*?)</a>', re.S | re.I)
    cell_pattern = re.compile(r'<td[^>]*>(.*?)</td>', re.S | re.I)
    # A real, genuinely-fetchable document link - not the row's OTHER links
    # (view-detail postbacks, pagination controls, etc.) which live testing
    # showed are javascript:__doPostBack(...) triggers, not real URLs. Only
    # trust something that looks like an actual file/document path.
    real_doc_pattern = re.compile(r'\.(pdf|doc|docx|xls|xlsx)(\?|$)|/documents?/', re.I)

    for row_html in row_pattern.findall(html):
        cells = cell_pattern.findall(row_html)
        if len(cells) < 3:
            continue
        work_name = re.sub(r'<[^>]+>', '', cells[1] if len(cells) > 1 else "").strip()
        work_name = re.sub(r'\s+', ' ', work_name)
        if not work_name or work_name.lower() in ("name of the work", "t.type"):
            continue

        # Check EVERY link in the row (not just the first) for a genuine
        # document link - a row can have several links (detail postback,
        # download postback, an actual file link) and the first one found
        # isn't necessarily the real document.
        doc_url = None
        for href, _link_text in link_pattern.findall(row_html):
            href = href.strip()
            if href.lower().startswith("javascript:") or href.lower().startswith("#"):
                continue
            if not real_doc_pattern.search(href):
                continue
            doc_url = href if href.startswith("http") else "https://www.ghmc.gov.in/" + href.lstrip("/")
            break

        # Per explicit request: only surface tenders that actually have a
        # real, downloadable document attached - skip everything else
        # rather than showing a broken/no-document entry.
        if not doc_url:
            continue

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


def fetch_tenderdetail_page(url):
    # tenderdetail.com is a normal, non-.gov.in domain - no reason to expect
    # the same DNS quirk ghmc.gov.in hit, so a plain retry (no DoH fallback)
    # is used here rather than dragging in that whole mechanism pre-emptively.
    user_agent = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36"
    last_error = None
    for attempt in range(2):
        try:
            req = urllib.request.Request(url, headers={
                "User-Agent": user_agent,
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            })
            with urllib.request.urlopen(req, timeout=25) as resp:
                return resp.read().decode("utf-8", errors="ignore")
        except Exception as e:
            last_error = e
            if attempt == 0:
                time.sleep(2)
    raise last_error


def parse_tenderdetail_rows(html):
    # Anchored on the one thing that's certain to be stable: tenderdetail.com's
    # detail-page URL pattern /Indian-Tenders/TenderNotice/{id}/{hash}. Title,
    # deadline, value, and doc-availability are pulled from the text between
    # one such link and the next, via looser sub-patterns so small markup
    # changes don't break the whole parse.
    rows = []
    link_pattern = re.compile(
        r'href="(/Indian-Tenders/TenderNotice/(\d+)/([a-f0-9]+))"[^>]*>(.*?)</a>', re.S | re.I
    )
    matches = list(link_pattern.finditer(html))
    for i, m in enumerate(matches):
        detail_path, tender_id, tender_hash, link_text = m.groups()
        title = re.sub(r'<[^>]+>', '', link_text).strip()
        title = re.sub(r'\s+', ' ', title)
        if not title or len(title) < 8:
            continue
        window_end = matches[i + 1].start() if i + 1 < len(matches) else min(len(html), m.end() + 1500)
        block = html[m.end():window_end]
        block_text = re.sub(r'<[^>]+>', ' ', block)
        block_text = re.sub(r'\s+', ' ', block_text)

        deadline_match = re.search(r'Closes\s+([A-Za-z]+ \d{1,2},\s*\d{4})', block_text)
        deadline = deadline_match.group(1) if deadline_match else None
        value_match = re.search(r'₹\s*([\d,\.]+\s*(?:Crore|Lakh|Lac)?|Ref\.?\s*Document)', block_text, re.I)
        value = value_match.group(1).strip() if value_match else None
        has_real_doc = bool(re.search(r'Tender Document', block_text, re.I))
        scanned_only = bool(re.search(r'Scan Images', block_text, re.I))

        rows.append({
            "id": tender_id,
            "title": title,
            "deadline": deadline,
            "value": value,
            "has_tender_document": has_real_doc,
            "scanned_images_only": scanned_only,
            "detail_url": "https://www.tenderdetail.com" + detail_path,
        })
    seen_ids = set()
    deduped = []
    for r in rows:
        if r["id"] in seen_ids:
            continue
        seen_ids.add(r["id"])
        deduped.append(r)
    return deduped


def parse_tenderdetail_detail_page(html):
    # Extracts the richer fields visible on a single tender's detail page
    # (not the PDF - these are shown on the page itself, ungated). Labels
    # and values sit close together in the markup; matched loosely (any
    # tags between label and value) so small structural changes don't break
    # the whole parse. Returns None per field it can't find rather than
    # failing the whole extraction - partial detail is still useful.
    def find_value(label_pattern, text, max_gap=200):
        m = re.search(label_pattern + r'.{0,' + str(max_gap) + r'}?</[^>]+>\s*<[^>]+>\s*([^<]{1,120})', text, re.S | re.I)
        return m.group(1).strip() if m else None

    text = html
    result = {}
    result["tender_no"] = find_value(r'Tender\s*No\b', text)
    result["publish_date"] = find_value(r'Publish\s*Date\b', text)
    result["submission_date"] = find_value(r'Submission\s*Date\b', text)
    result["tender_value"] = find_value(r'Tender\s*Value\b', text)
    result["tender_fee"] = find_value(r'Tender\s*Fee\b', text)
    result["emd"] = find_value(r'\bEMD\b(?!\s*Exemption)', text)
    result["emd_exemption"] = find_value(r'EMD\s*Exemption\b|Exemption\b', text)
    result["competition_type"] = find_value(r'Competition\s*Type\b', text)
    result["bidding_type"] = find_value(r'Bidding\s*Type\b', text)
    result["city"] = find_value(r'\bCity\b', text)
    result["state"] = find_value(r'\bState\b', text)
    result["authority_name"] = find_value(r'Authority\s*Name\b', text)

    # Corrigendum table - each row is a date + optional new-submission-date;
    # frequency/recency of corrigendums is itself a useful signal (repeated
    # amendments can indicate spec or eligibility problems in the original
    # tender), even without reading the PDF.
    corrigendum_dates = re.findall(r'(\d{1,2}-[A-Za-z]{3}-\d{4})', text)
    result["corrigendum_count"] = max(0, len(set(corrigendum_dates)) - 1)  # rough - first date pair is usually publish, not a corrigendum
    result["has_corrigendum"] = bool(re.search(r'Corrigendum-1|Corrigendum\s*Issued', text, re.I))

    found_count = sum(1 for v in result.values() if v not in (None, False, 0))
    return result, found_count


@app.route('/api/ghmc-tender-detail', methods=['GET'])
def ghmc_tender_detail():
    # Fetches the richer, still-free fields on a single tender's OWN detail
    # page (tender number, EMD, dates, competition type, corrigendum
    # history) - not the gated PDF, just what tenderdetail.com shows on the
    # page itself. Called per-tender from the frontend (on demand), not
    # bulk, to avoid hammering their site with 105 extra fetches per list
    # refresh.
    from flask import request
    detail_url = request.args.get("url", "")
    if not detail_url.startswith("https://www.tenderdetail.com/Indian-Tenders/TenderNotice/"):
        return jsonify({"ok": False, "error": "url must be a tenderdetail.com TenderNotice link"}), 400
    try:
        html = fetch_tenderdetail_page(detail_url)
    except Exception as e:
        return jsonify({"ok": False, "error": f"Could not fetch detail page: {str(e)[:300]}"}), 502

    detail, found_count = parse_tenderdetail_detail_page(html)
    response = {"ok": True, "detail": detail}
    if found_count < 3:
        # Parser found almost nothing - same diagnostic pattern as the list
        # endpoint, since this parser was also written without being able
        # to see this domain's raw HTML from this sandbox's own network.
        response["debug_html_len"] = len(html)
        response["debug_sample"] = html[:1200]
    return jsonify(response)


@app.route('/api/ghmc-fetch-doc-test', methods=['GET'])
def ghmc_fetch_doc_test():
    # Lets a specific document URL be tested directly (e.g. one shown by
    # /api/ghmc-tenders-list) without waiting for the cron job to pick it up
    # - useful for confirming the hardened fetch actually retrieves a real
    # PDF for a given tender's doc_url.
    from flask import request
    url = request.args.get("url", "")
    if not url.startswith("https://www.ghmc.gov.in/") and not url.startswith("https://ghmc.gov.in/"):
        return jsonify({"ok": False, "error": "url must be a ghmc.gov.in link"}), 400
    try:
        data = fetch_ghmc_url_hardened(url)
        is_pdf = data.startswith(b"%PDF")
        return jsonify({"ok": True, "is_pdf": is_pdf, "byte_count": len(data)})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)[:500]})


@app.route('/api/ghmc-tenders-list', methods=['GET'])
def ghmc_tenders_list():
    # Public - GHMC's own tender listing has no Patancheru-area data at all
    # (see comment on TENDERDETAIL_GHMC_PAGES above), so this pulls from
    # tenderdetail.com's GHMC-authority listing across all 3 pages instead.
    # Returns the FULL scanned list (not pre-filtered) with an
    # is_patancheru_area flag per row, so the frontend can filter by area
    # and/or a free-text keyword (e.g. "school", "hospital") independently.
    all_rows = []
    fetch_errors = []
    debug_sample = None
    debug_html_len = None
    for page_url in TENDERDETAIL_GHMC_PAGES:
        try:
            html = fetch_tenderdetail_page(page_url)
            page_rows = parse_tenderdetail_rows(html)
            all_rows.extend(page_rows)
            if debug_sample is None:
                debug_html_len = len(html)
                debug_sample = html[:800]
        except Exception as e:
            fetch_errors.append(str(e)[:200])

    if not all_rows and fetch_errors:
        return jsonify({"ok": False, "error": f"Could not fetch tender listing: {'; '.join(fetch_errors)}"}), 500

    for r in all_rows:
        r["is_patancheru_area"] = any(k in r["title"].lower() for k in PATANCHERU_AREA_KEYWORDS)

    return jsonify({
        "ok": True,
        "tenders": all_rows,
        "total_scanned": len(all_rows),
        "partial_fetch_errors": fetch_errors or None,
        "debug_html_len": debug_html_len if not all_rows else None,
        "debug_sample": debug_sample if not all_rows else None,
    })


@app.route('/api/ghmc-tender-watch', methods=['GET'])
def ghmc_tender_watch():
    site_token = os.environ.get("SITE_REPO_TOKEN")
    gemini_key = os.environ.get("GEMINI_API_KEY")
    bot_token = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat_id_config = os.environ.get("TELEGRAM_CHAT_ID")
    if not site_token or not gemini_key:
        return jsonify({"ok": False, "error": "Server misconfiguration."}), 500

    try:
        all_rows = []
        fetch_errors = []
        for page_url in TENDERDETAIL_GHMC_PAGES:
            try:
                html = fetch_tenderdetail_page(page_url)
                all_rows.extend(parse_tenderdetail_rows(html))
            except Exception as e:
                fetch_errors.append(str(e)[:200])
        if not all_rows and fetch_errors:
            return jsonify({"ok": False, "error": f"Could not fetch tender listing: {'; '.join(fetch_errors)}"}), 500
    except Exception as e:
        return jsonify({"ok": False, "error": f"Could not fetch/parse tender listing: {str(e)[:300]}"}), 500

    try:
        seen_data, seen_sha = github_get(GHMC_SEEN_TENDERS_FILE, site_token, timeout=8)
    except Exception:
        seen_data, seen_sha = None, None
    first_run = seen_data is None
    seen_ids = set((seen_data or {}).get("seen_ids", []))

    rows = all_rows
    new_rows = [r for r in rows if r["id"] not in seen_ids]

    # Place-name keywords, not "GHMC" as an authority - see comment on
    # PATANCHERU_AREA_KEYWORDS above. Everything on the page still gets
    # marked "seen" below regardless of this filter, so non-matching
    # tenders are correctly never re-checked, just never alerted on.
    relevant_new_rows = [r for r in new_rows if any(k in r["title"].lower() for k in PATANCHERU_AREA_KEYWORDS)]

    alerted = []

    # No free PDF is available from this source (tenderdetail.com lead-gates
    # the actual document behind a name/phone/OTP form) - so this can no
    # longer auto-run the clause-level anomaly analysis the way it did
    # against ghmc.gov.in's now-abandoned listing. It still alerts on every
    # new matching tender with full metadata + a direct link, so nothing
    # slips by unnoticed; full Scrutiny stays a manual next step until/unless
    # a paid data source with real document access is set up.
    MAX_PER_RUN = 10

    if not first_run and bot_token and chat_id_config:
        for row in relevant_new_rows[:MAX_PER_RUN]:
            doc_note = "📄 Document available" if row["has_tender_document"] else ("🖼️ Scanned images only" if row["scanned_images_only"] else "⚠️ No document listed")
            msg = (
                f"📋 <b>New Patancheru-area tender</b>\n"
                f"{row['title'][:250]}\n"
                f"Deadline: {row['deadline'] or 'not listed'} · Value: {row['value'] or 'Ref. Document'}\n"
                f"{doc_note}\n"
                f"View & download: {row['detail_url']}"
            )
            try:
                send_telegram_to_all(bot_token, chat_id_config, msg[:4000])
                alerted.append(row["title"][:200])
            except Exception:
                continue

    all_current_ids = [r["id"] for r in rows]
    merged = list(dict.fromkeys(all_current_ids + list(seen_ids)))[:1000]
    try:
        github_put(GHMC_SEEN_TENDERS_FILE, site_token, {"seen_ids": merged}, seen_sha, "Update seen tender ids", timeout=10)
    except Exception:
        pass

    return jsonify({"ok": True, "total_scanned": len(rows), "new_found": len(new_rows), "new_matching_area": len(relevant_new_rows), "alerted_this_run": len(alerted), "first_run": first_run})

@app.route('/', methods=['GET'])
def health():
    return jsonify({"ok": True, "service": "lawsticker-ghmc-relay", "routes": [
        "/api/ghmc-connectivity-test", "/api/ghmc-tenders-list", "/api/ghmc-tender-watch", "/api/ghmc-fetch-doc-test", "/api/ghmc-tender-detail",
        "(note: ghmc-tenders-list and ghmc-tender-watch now source from tenderdetail.com, not ghmc.gov.in)",
    ]})


if __name__ == '__main__':
    app.run(host='0.0.0.0', port=int(os.environ.get('PORT', 8080)))
