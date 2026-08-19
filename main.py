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


@app.errorhandler(Exception)
def handle_any_uncaught_error(e):
    # Without this, an uncaught exception anywhere returns Flask's default
    # HTML error page, which has no CORS headers - the browser then reports
    # a generic "Network error"/"Failed to fetch" instead of a readable
    # error, making a real 500 look like a connectivity problem. This
    # guarantees every response, even from a bug nobody's caught yet, is
    # valid CORS-safe JSON the frontend can actually display.
    import traceback
    return jsonify({"ok": False, "error": f"Unhandled server error: {str(e)[:300]}", "trace_tail": traceback.format_exc()[-500:]}), 500

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


def send_telegram_document(bot_token, chat_id, pdf_bytes, filename, caption):
    # Telegram's sendDocument needs a real multipart/form-data body - this
    # repo uses urllib (not requests) elsewhere, so the multipart encoding
    # is built by hand here rather than pulling in a new dependency just
    # for one endpoint.
    url = f"https://api.telegram.org/bot{bot_token}/sendDocument"
    boundary = "----lawstickerBoundary" + hashlib.md5(filename.encode()).hexdigest()[:12]
    parts = []
    parts.append(f"--{boundary}\r\nContent-Disposition: form-data; name=\"chat_id\"\r\n\r\n{chat_id}\r\n")
    parts.append(f"--{boundary}\r\nContent-Disposition: form-data; name=\"caption\"\r\n\r\n{caption[:1024]}\r\n")
    parts.append(f"--{boundary}\r\nContent-Disposition: form-data; name=\"parse_mode\"\r\n\r\nHTML\r\n")
    parts.append(f"--{boundary}\r\nContent-Disposition: form-data; name=\"document\"; filename=\"{filename}\"\r\nContent-Type: application/pdf\r\n\r\n")
    body = "".join(parts).encode("utf-8") + bytes(pdf_bytes) + f"\r\n--{boundary}--\r\n".encode("utf-8")
    req = urllib.request.Request(
        url, data=body,
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        return resp.status


def send_telegram_document_to_all(bot_token, chat_id_config, pdf_bytes, filename, caption):
    results = {}
    for cid in [c.strip() for c in chat_id_config.split(",") if c.strip()]:
        try:
            send_telegram_document(bot_token, cid, pdf_bytes, filename, caption)
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
# Permanent, growing archive - unlike GHMC_SEEN_TENDERS_FILE (which is just
# a dedup marker), this stores full tender records (metadata + detail + AI
# review) forever, even after a tender drops off the live listing. Since
# there's no free historical archive to backfill from, this is how real
# history actually accumulates - going forward, one hourly run at a time,
# never discarding what's already been captured.
GHMC_TENDER_ARCHIVE_FILE = "ghmc-tender-archive.json"
GHMC_AUTO_PIPELINE_MAX_PER_RUN = 5  # Gemini + PDF + Telegram cost control per run

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
    "sultanpur", "indresham", "isnapur", "pati village", "kistareddypet",
    "nallavelly", "jp colony patancheru",
)


def matches_area_keywords(title):
    # Word-boundary matching, not naive substring - a plain "in" check on
    # "sultanpur" incorrectly matched "Sultanpura" (a Charminar/old-city
    # locality, nothing to do with Patancheru) since it's a substring of
    # that unrelated name. \b only breaks between a word char and a
    # non-word char, so it correctly excludes "Sultanpura" (word chars on
    # both sides of the boundary) while still matching standalone
    # "Sultanpur". Multi-word keywords like "rc puram" have spaces, which
    # already act as natural word boundaries, so \b around the whole
    # phrase still behaves correctly for those too.
    title_lower = title.lower()
    for k in PATANCHERU_AREA_KEYWORDS:
        if re.search(r'\b' + re.escape(k) + r'\b', title_lower):
            return True
    return False


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

        # Extra fields grabbed from the same block, no additional fetch
        # needed - all best-effort (None if not found), never required.
        days_left_match = re.search(r'(\d+)\s*days?\s*left', block_text, re.I)
        days_left = int(days_left_match.group(1)) if days_left_match else None
        status = "Closed" if re.search(r'\bClosed\b', block_text, re.I) else ("Live" if re.search(r'\bLive\b', block_text, re.I) else None)
        tender_mode = "Offline" if re.search(r'\bOffline\b', block_text, re.I) else ("Online" if re.search(r'\bOnline\b', block_text, re.I) else None)
        # Category sits right before "Local Bodies"/similar department tags
        # on tenderdetail.com listings - captured loosely since exact
        # category taxonomy isn't something to over-fit a regex to.
        category_match = re.search(r'([A-Za-z][A-Za-z &]{3,40})\s*Local Bodies', block_text)
        category = category_match.group(1).strip() if category_match else None

        # A server-computed numeric value (in rupees) so the frontend can
        # sort/filter without re-parsing "7.14 Lakh"/"2.74 Crore" strings
        # itself - one parser, one place, used consistently everywhere.
        value_rupees = None
        if value:
            num_match = re.match(r'([\d,\.]+)', value)
            if num_match:
                try:
                    num = float(num_match.group(1).replace(',', ''))
                    unit = value.lower()
                    if 'crore' in unit:
                        value_rupees = num * 1e7
                    elif 'lakh' in unit or 'lac' in unit:
                        value_rupees = num * 1e5
                    else:
                        value_rupees = num
                except ValueError:
                    value_rupees = None

        rows.append({
            "id": tender_id,
            "title": title,
            "deadline": deadline,
            "value": value,
            "value_rupees": value_rupees,
            "days_left": days_left,
            "status": status,
            "tender_mode": tender_mode,
            "category": category,
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


def compute_quick_flags(title, detail):
    # Server-side Python port of the same deterministic checks already
    # shown client-side on the admin page (EMD ratio, bid window, recall
    # detection, corrigendum, offline submission) - needed here since this
    # runs unattended in the cron pipeline, not in a browser. Kept in sync
    # in spirit with the JS version; any threshold change should be mirrored
    # on both sides if it matters, though these are independent copies.
    flags = []

    def to_rupees(s):
        if not s:
            return None
        m = re.match(r"([\d,\.]+)\s*(Crore|Lakh|Lakhs|Lac)?", s, re.I)
        if not m:
            return None
        try:
            num = float(m.group(1).replace(",", ""))
        except ValueError:
            return None
        unit = (m.group(2) or "").lower()
        if unit.startswith("crore"):
            return num * 1e7
        if unit.startswith("lakh") or unit.startswith("lac"):
            return num * 1e5
        return num

    value_rs = to_rupees(detail.get("tender_value"))
    emd_rs = to_rupees(detail.get("emd"))
    if value_rs and emd_rs:
        pct = (emd_rs / value_rs) * 100
        if pct < 0.5:
            flags.append({"level": "info", "text": f"EMD is unusually low ({pct:.2f}% of tender value)."})
        elif pct > 5:
            flags.append({"level": "info", "text": f"EMD is on the higher side ({pct:.2f}% of tender value)."})
        else:
            flags.append({"level": "ok", "text": f"EMD is {pct:.2f}% of tender value - within the typical 1-5% range."})

    def parse_date(s):
        if not s:
            return None
        for fmt in ("%d %b %Y", "%d-%b-%Y", "%d/%m/%Y", "%Y-%m-%d"):
            try:
                return datetime.strptime(s.strip(), fmt)
            except Exception:
                continue
        return None

    pub = parse_date(detail.get("publish_date"))
    sub = parse_date(detail.get("submission_date"))
    if pub and sub:
        days = (sub - pub).days
        if days <= 3:
            flags.append({"level": "warn", "text": f"Very short bid window - only {days} day(s) between publish and submission."})
        elif days <= 7:
            flags.append({"level": "info", "text": f"Short bid window - {days} days between publish and submission."})
        else:
            flags.append({"level": "ok", "text": f"Bid window is {days} days - a reasonable amount of time to prepare a bid."})

    if re.search(r"\d+\s*(?:st|nd|rd|th)?\s*recall|re-?call", title, re.I):
        m = re.search(r"\d+\s*(?:st|nd|rd|th)?\s*recall", title, re.I)
        label = m.group(0) if m else "recall"
        flags.append({"level": "warn", "text": f"This is a re-issued tender ({label}) - worth checking why the earlier round didn't result in an award."})

    if detail.get("has_corrigendum"):
        flags.append({"level": "info", "text": "This tender has at least one corrigendum (amendment after original publication)."})

    tender_type = (detail.get("tender_type") or "").lower()
    if "offline" in tender_type:
        flags.append({"level": "warn", "text": "This tender uses offline bid submission rather than the standard online e-procurement portal."})

    return flags


def generate_tender_pdf_report(row, detail, quick_flags, ai_review):
    # Server-side PDF generation (fpdf2 - pure Python, no system deps, safe
    # for Cloud Run without Docker changes) since this runs unattended in
    # the cron pipeline, not in a browser where jsPDF could be used instead.
    from fpdf import FPDF

    pdf = FPDF()
    pdf.add_page()
    pdf.set_font("Helvetica", "B", 16)
    pdf.set_text_color(124, 90, 11)
    pdf.multi_cell(0, 9, "Tender Scrutiny Alert Report", new_x="LMARGIN", new_y="NEXT")
    pdf.set_font("Helvetica", "", 9)
    pdf.set_text_color(120, 120, 120)
    pdf.cell(0, 6, f"Generated {datetime.now(timezone.utc).strftime('%d %b %Y, %H:%M UTC')} - automated pipeline, lawsticker-ai.com", new_x="LMARGIN", new_y="NEXT")
    pdf.ln(3)

    pdf.set_text_color(20, 20, 20)
    pdf.set_font("Helvetica", "B", 13)
    pdf.multi_cell(0, 7, row.get("title", "Untitled Tender"), new_x="LMARGIN", new_y="NEXT")
    pdf.ln(2)

    def section_header(text):
        pdf.set_font("Helvetica", "B", 12)
        pdf.set_text_color(124, 90, 11)
        pdf.cell(0, 8, text, new_x="LMARGIN", new_y="NEXT")
        pdf.set_text_color(20, 20, 20)
        pdf.set_font("Helvetica", "", 10)

    section_header("Tender Details")
    detail_rows = [
        ("Tender No", detail.get("tender_no")), ("Tender ID", row.get("id")),
        ("Authority", detail.get("authority_name")), ("Publish Date", detail.get("publish_date")),
        ("Submission Date", detail.get("submission_date")), ("Tender Value", detail.get("tender_value") or row.get("value")),
        ("EMD", detail.get("emd")), ("Competition Type", detail.get("competition_type")),
        ("Tender Type", detail.get("tender_type")), ("Location", ", ".join(filter(None, [detail.get("city"), detail.get("state")]))),
        ("Source Page", row.get("detail_url")),
    ]
    for label, val in detail_rows:
        if not val:
            continue
        pdf.set_font("Helvetica", "B", 10)
        pdf.cell(45, 6, label + ":")
        pdf.set_font("Helvetica", "", 10)
        pdf.multi_cell(0, 6, str(val), new_x="LMARGIN", new_y="NEXT")
    pdf.ln(3)

    if quick_flags:
        section_header("Quick Flags (computed from listed data)")
        for f in quick_flags:
            icon = "[!]" if f["level"] == "warn" else ("[OK]" if f["level"] == "ok" else "[i]")
            pdf.multi_cell(0, 6, f"{icon} {f['text']}", new_x="LMARGIN", new_y="NEXT")
        pdf.ln(3)

    section_header("AI Assessment (metadata only - PDF not reviewed)")
    pdf.multi_cell(0, 6, ai_review.get("assessment", ""), new_x="LMARGIN", new_y="NEXT")
    pdf.ln(2)

    if ai_review.get("flags"):
        pdf.set_font("Helvetica", "B", 10)
        pdf.cell(0, 6, "Flags:", new_x="LMARGIN", new_y="NEXT")
        pdf.set_font("Helvetica", "", 10)
        for f in ai_review["flags"]:
            pdf.multi_cell(0, 6, f"[{f.get('severity','')}] {f.get('point','')}", new_x="LMARGIN", new_y="NEXT")
        pdf.ln(2)

    if ai_review.get("what_the_document_would_show"):
        pdf.set_font("Helvetica", "I", 9)
        pdf.set_text_color(90, 90, 90)
        pdf.multi_cell(0, 6, "What the real document would still need to confirm: " + ai_review["what_the_document_would_show"], new_x="LMARGIN", new_y="NEXT")
        pdf.set_text_color(20, 20, 20)
        pdf.ln(3)

    if ai_review.get("rti_points"):
        section_header("Suggested RTI Questions (RTI Act, 2005)")
        for i, q in enumerate(ai_review["rti_points"], 1):
            pdf.multi_cell(0, 6, f"{i}. {q}", new_x="LMARGIN", new_y="NEXT")
        pdf.ln(3)

    pdf.set_font("Helvetica", "I", 8)
    pdf.set_text_color(120, 120, 120)
    pdf.multi_cell(0, 5, "Compiled automatically from public tender-listing metadata. No tender document (PDF) was reviewed for this report - this is a plausibility summary from listed fields only, not a clause-level fairness audit. Verify all details against the original source before taking any action, including before filing any RTI request.", new_x="LMARGIN", new_y="NEXT")

    return bytes(pdf.output())


def parse_tenderdetail_detail_page(html):
    # Extracts the richer fields visible on a single tender's detail page
    # (not the PDF - these are shown on the page itself, ungated). Labels
    # and values sit close together in the markup; matched loosely (any
    # tags between label and value) so small structural changes don't break
    # the whole parse. Returns None per field it can't find rather than
    # failing the whole extraction - partial detail is still useful.
    def find_value(label_pattern, text, max_gap=200):
        # Wrap in a non-capturing group unconditionally - a bare top-level
        # alternation in label_pattern (e.g. "A|B") would otherwise bind the
        # appended suffix only to the LAST alternative, due to regex
        # operator precedence. That was the actual crash: when only the
        # first alternative matched, group(1) belonged to a branch that
        # never ran, came back None, and .strip() on None raised.
        m = re.search(r'(?:' + label_pattern + r')' + r'.{0,' + str(max_gap) + r'}?</[^>]+>\s*<[^>]+>\s*([^<]{1,120})', text, re.S | re.I)
        if not m or m.group(1) is None:
            return None
        return m.group(1).strip() or None

    text = html
    result = {}
    result["tender_no"] = find_value(r'Tender\s*No\b', text)
    result["publish_date"] = find_value(r'Publish\s*Date\b', text)
    result["submission_date"] = find_value(r'Submission\s*Date\b', text)
    result["tender_value"] = find_value(r'Tender\s*Value\b', text)
    result["tender_fee"] = find_value(r'Tender\s*Fee\b', text)
    result["emd"] = find_value(r'\bEMD\b(?!\s*Exemption)', text)
    result["emd_exemption"] = find_value(r'EMD\s*Exemption\b|\bExemption\b', text)
    result["competition_type"] = find_value(r'Competition\s*Type\b', text)
    result["bidding_type"] = find_value(r'Bidding\s*Type\b', text)
    result["city"] = find_value(r'\bCity\b', text)
    result["state"] = find_value(r'\bState\b', text)
    result["authority_name"] = find_value(r'Authority\s*Name\b', text)
    result["tender_type"] = find_value(r'Tender\s*Type\b', text)  # Online / Offline
    result["quantity"] = find_value(r'\bQuantity\b', text)

    # TDR number and document count sit in distinctive, low-ambiguity spots
    # (a tab label and a reference-number badge) rather than a clean
    # label:value pair, so these get their own small direct patterns instead
    # of going through find_value.
    tdr_match = re.search(r'TDR\s*#\s*(\d+)', text, re.I)
    result["tdr_number"] = tdr_match.group(1) if tdr_match else None
    # Anchored to the tab navigation specifically ("...Timeline Documents 2")
    # since "Documents" alone appears in several unrelated spots on the page
    # (a loose search risks matching the wrong number entirely).
    doc_count_match = re.search(r'Timeline\s*Documents\s*(\d{1,3})\D', text, re.I)
    if not doc_count_match:
        doc_count_match = re.search(r'Documents\s*(\d{1,3})\D', text, re.I)
    result["document_count"] = int(doc_count_match.group(1)) if doc_count_match else None
    result["document_fee_refundable"] = (
        "Non-refundable" if re.search(r'Non-refundable', text, re.I)
        else ("Refundable" if re.search(r'(?<!Non-)Refundable', text, re.I) else None)
    )

    # Corrigendum table - each row is a date + optional new-submission-date;
    # frequency/recency of corrigendums is itself a useful signal (repeated
    # amendments can indicate spec or eligibility problems in the original
    # tender), even without reading the PDF.
    corrigendum_dates = re.findall(r'(\d{1,2}-[A-Za-z]{3}-\d{4})', text)
    result["corrigendum_count"] = max(0, len(set(corrigendum_dates)) - 1)  # rough - first date pair is usually publish, not a corrigendum
    result["has_corrigendum"] = bool(re.search(r'Corrigendum-1|Corrigendum\s*Issued', text, re.I))

    found_count = sum(1 for v in result.values() if v)
    return result, found_count


GHMC_METADATA_REVIEW_SCHEMA = {
    "type": "OBJECT",
    "properties": {
        "assessment": {"type": "STRING", "description": "2-4 sentence plain-language assessment of what the metadata suggests, calibrated to what is actually knowable without the document itself"},
        "flags": {
            "type": "ARRAY",
            "items": {
                "type": "OBJECT",
                "properties": {
                    "severity": {"type": "STRING", "enum": ["Low", "Medium", "High"]},
                    "point": {"type": "STRING", "description": "one specific observation, under 200 characters"},
                },
                "required": ["severity", "point"],
            },
        },
        "what_the_document_would_show": {"type": "STRING", "description": "1-2 sentences on what real clause-level scrutiny would need the PDF to check, that metadata alone cannot"},
        "rti_points": {
            "type": "ARRAY",
            "items": {"type": "STRING"},
            "description": "Only fill this in if there is at least one Medium or High severity flag - specific, concrete RTI (Right to Information Act 2005) questions a citizen could file with the tendering authority to get clarity on the flagged concern(s). Each should be phrased as an actual RTI question (e.g. 'Please provide the reasons recorded for cancelling/re-tendering NIT No. X, and copies of any file notings related to this decision'), not a vague request. Leave empty if there are no Medium/High flags - don't manufacture RTI points for a clean tender.",
        },
    },
    "required": ["assessment", "flags", "what_the_document_would_show", "rti_points"],
}


@app.route('/api/ghmc-tender-metadata-review', methods=['POST'])
def ghmc_tender_metadata_review():
    # A genuinely separate, lighter thing from full PDF-based Tender
    # Scrutiny: this sends ONLY the already-fetched metadata (title, value,
    # EMD, dates, competition type, etc - no document text, since none is
    # available from this source) to Gemini for a plausibility read. The
    # prompt is explicit about this limitation so the model doesn't invent
    # clause-level findings it has no basis for.
    gemini_key = os.environ.get("GEMINI_API_KEY")
    if not gemini_key:
        return jsonify({"ok": False, "error": "Server misconfiguration - missing GEMINI_API_KEY."}), 500

    from flask import request
    body = request.get_json(force=True, silent=True) or {}
    title = (body.get("title") or "").strip()
    detail = body.get("detail") or {}
    if not title:
        return jsonify({"ok": False, "error": "title is required."}), 400

    detail_lines = "\n".join(f"- {k}: {v}" for k, v in detail.items() if v)
    prompt = f"""You are reviewing METADATA ONLY for an Indian government tender - not the actual tender document/PDF, which is not available to you. Do not invent or assume clause-level details (eligibility criteria, discretionary powers, financial structure) that can only exist in the real document - if asked about those, say plainly that the document itself would need to be reviewed.

Based ONLY on what's below, give a calibrated plausibility assessment: does anything about the published metadata itself look unusual (competition type, EMD, timeline, recall/re-tender status, submission mode)? Be honest and specific - if nothing stands out, say so rather than manufacturing a concern.

Tender title: {title}

Metadata:
{detail_lines}

Respond with the assessment, a list of specific flags (each with severity), a short note on what the actual document would be needed to check, and - only if at least one flag is Medium or High severity - specific RTI Act 2005 questions a citizen could file with the tendering authority about the flagged concern(s)."""

    try:
        result = call_gemini_structured(gemini_key, prompt, GHMC_METADATA_REVIEW_SCHEMA, max_tokens=500, timeout=20)
    except Exception as e:
        return jsonify({"ok": False, "error": f"Gemini review failed: {str(e)[:300]}"}), 502

    return jsonify({"ok": True, "review": result})


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

    def content_sample(html):
        # A slice starting at byte 0 just shows <head>/CSS links, which is
        # useless for debugging the label->value markup pattern. Anchor on
        # "Tender Value" instead (present on every real detail page, seen in
        # both the Overview and Finance sections) and show the surrounding
        # window where the actual fields live.
        idx = html.lower().find("tender value")
        start = max(0, idx - 100) if idx != -1 else 0
        return html[start:start + 1500]

    try:
        detail, found_count = parse_tenderdetail_detail_page(html)
    except Exception as e:
        # Parsing must never 500 the whole request - an uncaught exception
        # here previously produced Flask's raw error page (no CORS headers),
        # which browsers report as an opaque "Network error" rather than a
        # readable error, making it look like a connectivity problem instead
        # of what it actually was. Always return valid JSON with a sample of
        # the real page instead, so a parser bug is diagnosable, not silent.
        return jsonify({
            "ok": True,
            "detail": {},
            "parse_error": str(e)[:300],
            "debug_html_len": len(html),
            "debug_sample": content_sample(html),
        })

    response = {"ok": True, "detail": detail}
    if found_count < 3:
        # Parser found almost nothing - same diagnostic pattern as the list
        # endpoint, since this parser was also written without being able
        # to see this domain's raw HTML from this sandbox's own network.
        response["debug_html_len"] = len(html)
        response["debug_sample"] = content_sample(html)
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
        r["is_patancheru_area"] = matches_area_keywords(r["title"])

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
    relevant_new_rows = [r for r in new_rows if matches_area_keywords(r["title"])]

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

@app.route('/api/ghmc-tender-auto-pipeline', methods=['GET'])
def ghmc_tender_auto_pipeline():
    # The actual hourly job (triggered by cron-job.org). No historical
    # backfill happens here - there's no free source to page through for
    # past months (checked and confirmed absent). Instead, every run:
    # 1. Checks the current live listing for area-matching tenders
    # 2. Skips anything already in the permanent archive (so re-running
    #    hourly just catches whatever's newly appeared since last time)
    # 3. For genuinely new ones: fetches full detail, computes Quick Flags,
    #    runs the AI metadata review (incl. RTI points when warranted)
    # 4. Archives EVERY new tender permanently, flagged or not
    # 5. Only sends a Telegram PDF alert for the ones with a real Medium/
    #    High severity flag - not everything, so the channel doesn't
    #    become noise
    site_token = os.environ.get("SITE_REPO_TOKEN")
    gemini_key = os.environ.get("GEMINI_API_KEY")
    bot_token = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat_id_config = os.environ.get("TELEGRAM_CHAT_ID")

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

    area_rows = [r for r in all_rows if matches_area_keywords(r["title"])]
    # De-dupe by id across the pages fetched this run - each page should be
    # distinct in normal operation, but this is cheap insurance against
    # double-processing (and double Telegram-alerting) if any two page
    # fetches ever overlap.
    seen_this_run = set()
    deduped_area_rows = []
    for r in area_rows:
        if r["id"] in seen_this_run:
            continue
        seen_this_run.add(r["id"])
        deduped_area_rows.append(r)
    area_rows = deduped_area_rows

    try:
        archive, archive_sha = github_get(GHMC_TENDER_ARCHIVE_FILE, site_token, timeout=10)
    except Exception:
        archive, archive_sha = None, None
    if archive is None:
        archive = {"tenders": {}}
    archived_ids = set(archive.get("tenders", {}).keys())

    new_rows = [r for r in area_rows if r["id"] not in archived_ids]
    to_process = new_rows[:GHMC_AUTO_PIPELINE_MAX_PER_RUN]
    # Anything beyond the cap this run simply isn't archived yet, so it's
    # naturally picked up on the next hourly run - no separate cursor
    # needed since "not yet in the archive" IS the queue.

    processed = []
    alerted = []
    errors = []

    for row in to_process:
        try:
            detail_html = fetch_tenderdetail_page(row["detail_url"])
            detail, _ = parse_tenderdetail_detail_page(detail_html)
        except Exception as e:
            detail = {}
            errors.append({"id": row["id"], "stage": "detail_fetch", "error": str(e)[:200]})

        quick_flags = []
        try:
            quick_flags = compute_quick_flags(row["title"], detail)
        except Exception as e:
            errors.append({"id": row["id"], "stage": "quick_flags", "error": str(e)[:200]})

        ai_review = None
        if gemini_key:
            try:
                detail_lines = "\n".join(f"- {k}: {v}" for k, v in detail.items() if v)
                prompt = f"""You are reviewing METADATA ONLY for an Indian government tender - not the actual tender document/PDF, which is not available to you. Do not invent or assume clause-level details (eligibility criteria, discretionary powers, financial structure) that can only exist in the real document - if asked about those, say plainly that the document itself would need to be reviewed.

Based ONLY on what's below, give a calibrated plausibility assessment: does anything about the published metadata itself look unusual (competition type, EMD, timeline, recall/re-tender status, submission mode)? Be honest and specific - if nothing stands out, say so rather than manufacturing a concern.

Tender title: {row['title']}

Metadata:
{detail_lines}

Respond with the assessment, a list of specific flags (each with severity), a short note on what the actual document would be needed to check, and - only if at least one flag is Medium or High severity - specific RTI Act 2005 questions a citizen could file with the tendering authority about the flagged concern(s)."""
                ai_review = call_gemini_structured(gemini_key, prompt, GHMC_METADATA_REVIEW_SCHEMA, max_tokens=800, timeout=25)
            except Exception as e:
                errors.append({"id": row["id"], "stage": "ai_review", "error": str(e)[:200]})
        if not ai_review:
            ai_review = {"assessment": "", "flags": [], "what_the_document_would_show": "", "rti_points": []}

        is_questionable = any(f.get("severity") in ("Medium", "High") for f in ai_review.get("flags", [])) or \
            any(f["level"] == "warn" for f in quick_flags)

        record = {
            "id": row["id"], "title": row["title"], "deadline": row.get("deadline"),
            "value": row.get("value"), "detail_url": row.get("detail_url"),
            "first_archived_at": datetime.now(timezone.utc).isoformat(),
            "detail": detail, "quick_flags": quick_flags, "ai_review": ai_review,
            "is_questionable": is_questionable,
        }
        archive["tenders"][row["id"]] = record
        processed.append({"id": row["id"], "title": row["title"][:100], "questionable": is_questionable})

        if is_questionable and bot_token and chat_id_config:
            try:
                pdf_bytes = generate_tender_pdf_report(row, detail, quick_flags, ai_review)
                high_flags = [f for f in ai_review.get("flags", []) if f.get("severity") == "High"]
                caption = (
                    f"🚩 <b>Questionable tender flagged</b>\n"
                    f"{row['title'][:200]}\n"
                    f"Tender ID: {row['id']} · High-severity flags: {len(high_flags)}\n"
                    f"{row.get('detail_url', '')}"
                )
                filename = f"tender_{row['id']}_report.pdf"
                # send_telegram_document_to_all catches per-chat exceptions
                # internally (so one failing chat doesn't stop others) and
                # returns a results dict rather than raising - the caller
                # MUST inspect that dict, since a bare call here would
                # silently report success even when every single chat send
                # actually failed (this was a real bug: "telegram_alerts_sent"
                # was previously incremented unconditionally regardless of
                # whether delivery genuinely succeeded).
                send_results = send_telegram_document_to_all(bot_token, chat_id_config, pdf_bytes, filename, caption)
                any_sent = any(v == "sent" for v in send_results.values())
                if any_sent:
                    alerted.append(row["id"])
                else:
                    errors.append({"id": row["id"], "stage": "telegram_send", "error": str(send_results)[:300]})
            except Exception as e:
                errors.append({"id": row["id"], "stage": "pdf_or_telegram", "error": str(e)[:300]})
        elif is_questionable and not (bot_token and chat_id_config):
            errors.append({"id": row["id"], "stage": "telegram_config", "error": "Tender was flagged but TELEGRAM_BOT_TOKEN/TELEGRAM_CHAT_ID is not configured on this service - no alert could be sent."})

    try:
        github_put(GHMC_TENDER_ARCHIVE_FILE, site_token, archive, archive_sha, f"Archive {len(processed)} new area tenders", timeout=15)
    except Exception as e:
        errors.append({"stage": "archive_save", "error": str(e)[:300]})

    return jsonify({
        "ok": True,
        "total_scanned": len(all_rows),
        "area_matches_live": len(area_rows),
        "already_archived": len(archived_ids),
        "new_this_run": len(new_rows),
        "processed_this_run": len(processed),
        "still_queued_for_next_run": max(0, len(new_rows) - len(to_process)),
        "questionable_flagged": sum(1 for p in processed if p["questionable"]),
        "telegram_alerts_sent": len(alerted),
        "errors": errors or None,
    })


@app.route('/api/ghmc-telegram-test', methods=['GET'])
def ghmc_telegram_test():
    # Isolated diagnostic - sends a tiny real PDF to Telegram directly,
    # independent of the actual pipeline finding a genuinely flagged
    # tender to test against. Answers "does delivery even work" on its
    # own, since that could otherwise stay unverified for a long time if
    # few real Patancheru-area tenders happen to get flagged.
    bot_token = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat_id_config = os.environ.get("TELEGRAM_CHAT_ID")
    if not bot_token or not chat_id_config:
        return jsonify({
            "ok": False,
            "error": "TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID is not set on this service.",
            "bot_token_present": bool(bot_token),
            "chat_id_present": bool(chat_id_config),
        }), 500

    from fpdf import FPDF
    pdf = FPDF()
    pdf.add_page()
    pdf.set_font("Helvetica", "B", 14)
    pdf.cell(0, 10, "Telegram delivery test - lawsticker-ghmc-relay", new_x="LMARGIN", new_y="NEXT")
    pdf.set_font("Helvetica", "", 10)
    pdf.multi_cell(0, 6, f"If you can see this, PDF+Telegram delivery works correctly. Generated {datetime.now(timezone.utc).isoformat()}.")
    pdf_bytes = bytes(pdf.output())

    results = send_telegram_document_to_all(bot_token, chat_id_config, pdf_bytes, "telegram_test.pdf", "🧪 Test message from the GHMC tender auto-pipeline diagnostic endpoint.")
    any_sent = any(v == "sent" for v in results.values())
    return jsonify({"ok": any_sent, "chat_results": results})


@app.route('/', methods=['GET'])
def health():
    return jsonify({"ok": True, "service": "lawsticker-ghmc-relay", "routes": [
        "/api/ghmc-connectivity-test", "/api/ghmc-tenders-list", "/api/ghmc-tender-watch", "/api/ghmc-fetch-doc-test", "/api/ghmc-tender-detail", "/api/ghmc-tender-metadata-review", "/api/ghmc-tender-auto-pipeline", "/api/ghmc-telegram-test",
        "(note: ghmc-tenders-list and ghmc-tender-watch now source from tenderdetail.com, not ghmc.gov.in)",
    ]})


if __name__ == '__main__':
    app.run(host='0.0.0.0', port=int(os.environ.get('PORT', 8080)))
