"""
Categoriser Agent — smart categorisation with user learning.

Priority order:
  1. Vendor rules DB   — what you've told us before (never asks twice)
  2. Keyword match     — known merchant names
  3. Web search        — DuckDuckGo enrichment for unknown vendors
  4. LLM guess         — with web context, flags low-confidence results
  5. Ask user          — posts to Discord if still ambiguous, learns answer

The ask_callback is an async function the bot passes in to post questions
and wait for answers interactively.
"""

import ollama
import subprocess
import urllib.request
import urllib.parse
import json
import time
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
from data import db

PREFERRED_MODELS = ["mistral", "phi3:mini", "llama3", "phi3"]

VALID_CATEGORIES = [
    "Food & Dining", "Groceries", "Transport", "Health", "Shopping",
    "Entertainment", "Subscriptions", "Utilities", "Travel",
    "Education", "Personal Care", "Other",
]

CAT_EMOJIS = {
    "Food & Dining": "🍽️", "Groceries": "🛒", "Transport": "🚗",
    "Health": "💊", "Shopping": "🛍️", "Entertainment": "🎬",
    "Subscriptions": "📱", "Utilities": "💡", "Travel": "✈️",
    "Education": "📚", "Personal Care": "🪥", "Other": "📦",
}

KEYWORD_CATEGORIES = {
    "Groceries":     ["delhaize", "carrefour", "colruyt", "lidl", "aldi",
                      "albert heijn", "jumbo", "bioplanet", "okay", "spar",
                      "intermarche", "supermarkt", "supermarche", "night shop"],
    "Food & Dining": ["restaurant", "cafe", "brasserie", "frituur", "pizza",
                      "sushi", "burger", "mcdonalds", "quick", "kfc", "subway",
                      "starbucks", "coffee", "traiteur", "snack", "bakkerij",
                      "boulangerie", "panos", "exki", "le pain", "bar ", "pub "],
    "Transport":     ["nmbs", "sncb", "de lijn", "tec ", "stib", "mivb",
                      "uber", "bolt", "taxi", "parking", "q-park", "indigo",
                      "benzine", "shell", "total ", "texaco", "esso", "velo"],
    "Subscriptions": ["netflix", "spotify", "apple", "microsoft", "adobe",
                      "amazon prime", "paypal *netflix", "paypal *spotify",
                      "disney", "hbo", "dazn", "youtube", "google one",
                      "dropbox", "notion", "chatgpt", "openai"],
    "Health":        ["apotheek", "pharmacie", "pharmacy", "dokter", "medic",
                      "kine", "dentist", "tandarts", "hospital", "ziekenhuis",
                      "vitamin", "supplement", "kruidvat"],
    "Shopping":      ["amazon", "bol.com", "zalando", "h&m", "zara", "ikea",
                      "primark", "fnac", "mediamarkt", "brico", "gamma",
                      "action ", "hema", "jbc", "coolblue"],
    "Entertainment": ["kinepolis", "ugc", "cinema", "concert", "theatre",
                      "museum", "ticket", "eventbrite", "livenation", "bowling"],
    "Utilities":     ["proximus", "telenet", "orange", "base ", "engie",
                      "luminus", "fluvius", "vivaqua", "swde"],
    "Travel":        ["ryanair", "brussels airlines", "easyjet", "booking.com",
                      "airbnb", "hotel", "hostel", "expedia", "tui",
                      "eurostar", "thalys", "flixbus"],
    "Personal Care": ["kapper", "coiffeur", "salon", "spa", "fitness",
                      "basic-fit", "jims ", "beauty", "nail"],
    "Education":     ["udemy", "coursera", "skillshare", "standaard boekhandel",
                      "school", "universite"],
}


# ── Model ─────────────────────────────────────────────────────────────────────

def _best_model() -> str:
    try:
        result = subprocess.run(
            ["ollama", "list"], capture_output=True, text=True, timeout=10
        )
        for m in PREFERRED_MODELS:
            if m.split(":")[0] in result.stdout:
                return m
    except Exception:
        pass
    return "phi3:mini"


def _call_ollama(prompt: str) -> str:
    model = _best_model()
    try:
        response = ollama.generate(model=model, prompt=prompt)
        return response['response'].strip()
    except Exception as e:
        return f"[ERROR] {e}"


# ── Categorisation passes ─────────────────────────────────────────────────────

def from_vendor_rules(vendor: str) -> str | None:
    """Check if we've learned this vendor before."""
    return db.get_vendor_rule(vendor)


def from_keywords(vendor: str, description: str) -> str | None:
    combined = (vendor + " " + description).lower()
    for cat, kws in KEYWORD_CATEGORIES.items():
        if any(kw in combined for kw in kws):
            return cat
    return None


_search_cache: dict = {}

def web_enrich(vendor: str) -> str:
    key = vendor.lower().strip()
    if key in _search_cache:
        return _search_cache[key]
    try:
        q    = urllib.parse.quote(f"{vendor} company type of business")
        url  = f"https://api.duckduckgo.com/?q={q}&format=json&no_redirect=1&no_html=1"
        req  = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        resp = urllib.request.urlopen(req, timeout=8)
        data = json.loads(resp.read())
        parts = []
        if data.get("AbstractText"):
            parts.append(data["AbstractText"][:200])
        for t in data.get("RelatedTopics", [])[:2]:
            if isinstance(t, dict) and t.get("Text"):
                parts.append(t["Text"][:100])
        ctx = " | ".join(parts)
        _search_cache[key] = ctx
        return ctx
    except Exception:
        return ""


def from_llm(vendor: str, description: str, web_ctx: str = "") -> tuple[str, float]:
    """
    Returns (category, confidence 0-1).
    Low confidence = should ask user.
    """
    ctx_line = f"\nWeb context: {web_ctx}" if web_ctx else ""
    prompt = (
        f"Categorise this bank transaction.\n"
        f"Vendor: {vendor}\nDescription: {description}{ctx_line}\n\n"
        f"Categories: {', '.join(VALID_CATEGORIES)}\n\n"
        f"Reply in this exact format:\n"
        f"Category: <category>\n"
        f"Confidence: <high/medium/low>\n"
        f"Nothing else."
    )
    raw = _call_ollama(prompt)

    cat   = "Other"
    conf  = 0.3

    for line in raw.splitlines():
        if line.lower().startswith("category:"):
            val = line.split(":", 1)[1].strip()
            for c in VALID_CATEGORIES:
                if c.lower() in val.lower():
                    cat = c
                    break
        if line.lower().startswith("confidence:"):
            val = line.split(":", 1)[1].strip().lower()
            conf = {"high": 0.9, "medium": 0.6, "low": 0.2}.get(val, 0.3)

    return cat, conf


# ── Main async pipeline ───────────────────────────────────────────────────────

async def categorise_with_interaction(
    transactions: list,
    ask_callback,       # async fn(vendor, description, llm_guess, expense_ids) → category or None
    progress_callback=None,
):
    """
    Full pipeline with interactive user confirmation for ambiguous vendors.

    ask_callback is called when confidence is low:
        category = await ask_callback(vendor, description, llm_guess, expense_ids)
    Returns None if user didn't respond (timeout), in which case llm_guess is used.

    Learns every confirmed answer permanently.
    """
    def _log(msg):
        print(f"  [Categoriser] {msg}")
        if progress_callback:
            progress_callback(msg)

    if not transactions:
        return transactions

    _log(f"Categorising {len(transactions)} transactions...")
    model = _best_model()
    _log(f"Model: {model}")

    # Group by vendor so we only ask once per unknown vendor
    # vendor_key → list of transaction dicts
    by_vendor: dict[str, list] = {}
    for t in transactions:
        key = db._vendor_key(t.get("vendor", "Unknown"))
        by_vendor.setdefault(key, []).append(t)

    db_updates = []

    for vendor_key, txns in by_vendor.items():
        vendor      = txns[0].get("vendor", "Unknown")
        description = txns[0].get("description", "")
        ids         = [t["id"] for t in txns]

        # Pass 1: vendor rules (learned)
        cat = from_vendor_rules(vendor)
        if cat:
            _log(f"✓ Learned rule: {vendor} → {cat}")
            for t in txns:
                t["category"] = cat
            db_updates.extend((cat, t["id"]) for t in txns)
            continue

        # Pass 2: keywords
        cat = from_keywords(vendor, description)
        if cat:
            _log(f"✓ Keyword: {vendor} → {cat}")
            for t in txns:
                t["category"] = cat
            db_updates.extend((cat, t["id"]) for t in txns)
            continue

        # Pass 3: web + LLM
        _log(f"? Searching web for: {vendor}")
        web_ctx      = web_enrich(vendor)
        llm_cat, conf = from_llm(vendor, description, web_ctx)
        _log(f"  LLM guess: {llm_cat} (confidence: {conf:.0%})")

        if conf >= 0.75:
            # High confidence — use it, but save as learned rule too
            _log(f"✓ High confidence: {vendor} → {llm_cat}")
            db.save_vendor_rule(vendor, llm_cat, always=True)
            for t in txns:
                t["category"] = llm_cat
            db_updates.extend((llm_cat, t["id"]) for t in txns)
            continue

        # Low/medium confidence — ask the user
        _log(f"? Asking user about: {vendor}")
        confirmed_cat = await ask_callback(vendor, description, llm_cat, ids)

        if confirmed_cat:
            _log(f"✓ User confirmed: {vendor} → {confirmed_cat}")
            for t in txns:
                t["category"] = confirmed_cat
            db_updates.extend((confirmed_cat, t["id"]) for t in txns)
        else:
            # No response — use LLM guess
            _log(f"  No response, using LLM guess: {llm_cat}")
            for t in txns:
                t["category"] = llm_cat
            db_updates.extend((llm_cat, t["id"]) for t in txns)

        time.sleep(0.2)

    if db_updates:
        db.bulk_update_categories(db_updates)
        _log(f"Saved {len(db_updates)} categories to DB.")

    return transactions


def categorise_uncategorised_sync(progress_callback=None) -> list:
    """
    Sync version for scheduler — keyword + vendor rules only, no asking.
    Returns list of still-ambiguous transactions for async asking.
    """
    uncats   = db.get_uncategorised(limit=200)
    updates  = []
    pending  = []

    for t in uncats:
        vendor = t.get("vendor", "")
        desc   = t.get("description", "")

        cat = from_vendor_rules(vendor) or from_keywords(vendor, desc)
        if cat:
            t["category"] = cat
            updates.append((cat, t["id"]))
        else:
            pending.append(t)

    if updates:
        db.bulk_update_categories(updates)
        if progress_callback:
            progress_callback(f"Auto-categorised {len(updates)} via rules/keywords.")

    return pending
