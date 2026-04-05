"""
PDF Agent — Hello Bank / BNP Paribas Fortis statement parser.
Format observed:
  - Date headers: DD-MM-YYYY on their own line
  - Transaction blocks spanning multiple lines:
      Nr   Type description    Tegenpartij    DD-MM    amount +/-
  - Amount format: 1.234,56 + or 10,45 -
  - Outgoing = trailing " -", incoming = trailing " +"
"""

import re
import ollama
import json
from datetime import datetime

try:
    import pdfplumber
    HAS_PDF = True
except ImportError:
    HAS_PDF = False

OLLAMA_MODEL = "phi3:mini"


def _call_ollama(prompt: str) -> str:
    try:
        response = ollama.generate(model=OLLAMA_MODEL, prompt=prompt)
        return response['response'].strip()
    except Exception as e:
        return f"[ERROR] {e}"


def extract_text(pdf_path: str) -> str:
    """Extract raw text — used for debug and LLM fallback."""
    if not HAS_PDF:
        return "[ERROR] pdfplumber not installed."
    try:
        with pdfplumber.open(pdf_path) as pdf:
            return "\n".join(page.extract_text() or "" for page in pdf.pages)
    except Exception as e:
        return f"[ERROR] {e}"


# ── Hello Bank specific parser ────────────────────────────────────────────────

# Amount at end of line: "10,45 -" or "1.234,56 +" or "800,00+"
AMOUNT_RE = re.compile(
    r"(\d{1,3}(?:\.\d{3})*,\d{2})\s*([+-])\s*$"
)

# Date header line: "31-03-2026" alone on a line (or "01-04-2026")
DATE_HEADER_RE = re.compile(r"^\s*(\d{2}-\d{2}-\d{4})\s*$")

# Valuta date on a transaction line: "30-03" (short date, no year)
VALUTA_RE = re.compile(r"\b(\d{2}-\d{2})\b")

# Transaction number at start: "0231"
TXN_NR_RE = re.compile(r"^\s*(\d{4})\s+")

# Noise lines to skip
NOISE_PATTERNS = [
    re.compile(p, re.IGNORECASE) for p in [
        r"^saldo",
        r"^bankreferentie",
        r"^nummer \d",
        r"^visa debit",
        r"^apple pay",
        r"^ecommerce",
        r"^uitvoeringsdatum",
        r"^geen mededeling",
        r"^andere betaling",
        r"^bic ",
        r"^be\d{2}\s",        # IBAN lines
        r"^kredbebbxxx",
        r"^gebabebb",
        r"nr\.\s+type",       # header row
        r"valuta",
        r"bedrag \(eur\)",
        r"bnp paribas",
        r"warandeberg",
        r"rpr brussel",
        r"tel\.:",
        r"p\.doc",
        r"^\d+\s*/\s*\d+",   # page numbers "1 / 15"
        r"^hellobank",
        r"hello4you",
        r"^dhr\s",
        r"^klantnr",
    ]
]

# Transaction type keywords (Dutch Hello Bank labels)
TYPE_KEYWORDS = [
    "betaling met debetkaart",
    "overschrijving in euro",
    "instant europese overschrijving",
    "domiciliëring",
    "domiciliering",
    "storting",
    "terugbetaling",
    "interest",
]


def _is_noise(line: str) -> bool:
    ll = line.lower().strip()
    return any(p.search(ll) for p in NOISE_PATTERNS)


def _normalise_date(dd_mm: str, year: int) -> str:
    """Convert DD-MM to YYYY-MM-DD using the provided year."""
    try:
        day, month = dd_mm.split("-")
        return f"{year}-{month.zfill(2)}-{day.zfill(2)}"
    except Exception:
        return datetime.now().strftime("%Y-%m-%d")


def _extract_vendor_from_block(lines: list) -> str:
    """
    Pull the merchant name from a transaction block.
    In Hello Bank format the vendor appears after the type description line,
    e.g.: "DELHAIZE HEVERLEE" or "PAYPAL *NETFLIX COM"
    """
    # Skip type keyword lines and noise, take first remaining ALL-CAPS or mixed line
    for line in lines:
        ll = line.strip()
        if not ll:
            continue
        if _is_noise(ll):
            continue
        if any(kw in ll.lower() for kw in TYPE_KEYWORDS):
            continue
        if re.match(r"^\d{4}\s+", ll):   # transaction number line
            continue
        if VALUTA_RE.search(ll) and AMOUNT_RE.search(ll):
            continue
        # Looks like a vendor name
        if len(ll) > 2:
            return ll[:50].title()
    return "Unknown"


def parse_hellobank(text: str) -> list:
    """
    Parse Hello Bank / BNP Paribas Fortis multi-line transaction format.
    """
    transactions = []
    lines        = text.split("\n")
    current_year = datetime.now().year

    # Pass 1: collect transaction blocks
    # A block starts with a 4-digit transaction number
    # We also track the current date section header

    current_date_section = None   # "31-03-2026"
    i = 0

    while i < len(lines):
        line = lines[i].strip()

        # Date section header
        m = DATE_HEADER_RE.match(lines[i])
        if m:
            current_date_section = m.group(1)  # "31-03-2026"
            i += 1
            continue

        # Transaction start: line beginning with 4-digit number
        nr_match = TXN_NR_RE.match(line)
        if nr_match:
            # Collect all lines belonging to this transaction block
            block_lines = [line]
            j = i + 1
            while j < len(lines):
                next_line = lines[j].strip()
                # Stop if we hit another transaction number, date header, or empty + next txn
                if TXN_NR_RE.match(next_line):
                    break
                if DATE_HEADER_RE.match(lines[j]):
                    break
                block_lines.append(next_line)
                j += 1

            # Find the valuta line (contains short date + amount)
            valuta_date = None
            amount      = None
            direction   = None

            for bl in block_lines:
                amt_m = AMOUNT_RE.search(bl)
                if amt_m:
                    raw_amt   = amt_m.group(1).replace(".", "").replace(",", ".")
                    direction = amt_m.group(2)
                    try:
                        amount = float(raw_amt)
                    except ValueError:
                        pass

                    # Short date on same line
                    val_m = VALUTA_RE.search(bl)
                    if val_m:
                        valuta_date = val_m.group(1)
                    break

            # Only process outgoing (expenses)
            if amount is not None and direction == "-":
                # Determine full date
                year = current_year
                if current_date_section:
                    try:
                        year = int(current_date_section.split("-")[2])
                    except Exception:
                        pass

                if valuta_date:
                    full_date = _normalise_date(valuta_date, year)
                elif current_date_section:
                    # Parse from section header DD-MM-YYYY
                    try:
                        full_date = datetime.strptime(
                            current_date_section, "%d-%m-%Y"
                        ).strftime("%Y-%m-%d")
                    except Exception:
                        full_date = datetime.now().strftime("%Y-%m-%d")
                else:
                    full_date = datetime.now().strftime("%Y-%m-%d")

                # Extract description — first real description line after txn nr
                desc_lines = []
                for bl in block_lines[1:]:
                    bl = bl.strip()
                    if not bl or _is_noise(bl):
                        continue
                    if AMOUNT_RE.search(bl):
                        continue
                    desc_lines.append(bl)

                description = " | ".join(desc_lines[:3]) if desc_lines else "Unknown"
                vendor      = _extract_vendor_from_block(desc_lines)

                transactions.append({
                    "date":        full_date,
                    "amount":      round(amount, 2),
                    "currency":    "EUR",
                    "description": description[:120],
                    "vendor":      vendor,
                    "source":      "pdf_hellobank",
                })

            i = j
            continue

        i += 1

    return transactions


# ── LLM fallback ──────────────────────────────────────────────────────────────

def parse_with_llm(text: str) -> list:
    preview = text[:3000]
    prompt = f"""Extract all expense transactions from this Hello Bank statement.
Return ONLY a JSON array. Each item:
  {{"date": "YYYY-MM-DD", "amount": <positive number>, "vendor": "name", "description": "text"}}

Only outgoing payments (amounts ending in " -"). Skip incoming (ending in " +").

Statement:
{preview}

JSON array only:"""

    raw = _call_ollama(prompt)
    try:
        clean = re.sub(r"```json|```", "", raw).strip()
        match = re.search(r"\[.*\]", clean, re.DOTALL)
        if match:
            items = json.loads(match.group())
            for item in items:
                item.setdefault("currency", "EUR")
                item.setdefault("source",   "pdf_llm")
            return [i for i in items if i.get("amount") and i.get("date")]
    except Exception:
        pass
    return []


# ── Categorisation ────────────────────────────────────────────────────────────

# Keyword map — covers the most common Hello Bank / Belgian merchant names
KEYWORD_CATEGORIES = {
    "Groceries":      [
        "delhaize", "carrefour", "colruyt", "lidl", "aldi", "albert heijn",
        "ah ", "jumbo", "bioplanet", "okay", "proxy delhaize", "spar",
        "intermarche", "supermarkt", "supermarche", "night shop",
    ],
    "Food & Dining":  [
        "restaurant", "cafe", "brasserie", "frituur", "pizza", "sushi",
        "burger", "mcdonalds", "quick", "kfc", "subway", "starbucks",
        "coffee", "lunch", "traiteur", "snack", "bakkerij", "boulangerie",
        "panos", "exki", "le pain", "eating", "bar ", "pub ",
    ],
    "Transport":      [
        "nmbs", "sncb", "de lijn", "tec ", "stib", "mivb", "uber",
        "bolt", "taxi", "parking", "q-park", "indigo", "benzine",
        "shell", "total ", "texaco", "esso", "tinker", "velo",
    ],
    "Subscriptions":  [
        "netflix", "spotify", "apple", "microsoft", "adobe", "amazon prime",
        "paypal *netflix", "paypal *spotify", "disney", "hbo", "dazn",
        "youtube", "google one", "dropbox", "notion",
    ],
    "Health":         [
        "apotheek", "pharmacie", "pharmacy", "dokter", "medic", "kine",
        "dentist", "tandarts", "hospital", "ziekenhuis", "vitamin",
        "supplement", "kruidvat", "di ", "ici paris",
    ],
    "Shopping":       [
        "amazon", "bol.com", "zalando", "h&m", "zara", "ikea", "primark",
        "fnac", "mediamarkt", "brico", "gamma", "action ", "hema",
        "jbc", "esprit", "mango", "uniqlo", "coolblue",
    ],
    "Entertainment":  [
        "kinepolis", "ugc", "cinema", "pathé", "concert", "theatre",
        "museum", "ticket", "eventbrite", "livenation", "standup",
        "bowling", "escape room",
    ],
    "Utilities":      [
        "proximus", "telenet", "orange", "base ", "engie", "luminus",
        "fluvius", "vivaqua", "swde", "internet", "telecom",
    ],
    "Travel":         [
        "ryanair", "brussels airlines", "easyjet", "booking.com",
        "airbnb", "hotel", "hostel", "expedia", "kayak", "tui",
        "eurostar", "thalys", "flixbus",
    ],
    "Personal Care":  [
        "kapper", "coiffeur", "salon", "spa", "fitness", "basic-fit",
        "jims ", "my gym", "look", "beauty", "nail",
    ],
    "Education":      [
        "udemy", "coursera", "skillshare", "boek", "standaard",
        "fnac livre", "bol boek", "school", "universite",
    ],
}

VALID_CATEGORIES = set(KEYWORD_CATEGORIES.keys()) | {"Other"}


def _keyword_categorise(vendor: str, description: str) -> str | None:
    """Fast keyword-based categorisation. Returns None if ambiguous."""
    combined = (vendor + " " + description).lower()
    for cat, keywords in KEYWORD_CATEGORIES.items():
        if any(kw in combined for kw in keywords):
            return cat
    return None


def _llm_categorise_one(vendor: str, description: str) -> str:
    """Ask LLM to categorise a single transaction — more reliable than batch for phi3."""
    prompt = (
        f"What is the spending category for this transaction?\n"
        f"Vendor: {vendor}\nDescription: {description}\n\n"
        f"Choose exactly one: Food & Dining, Groceries, Transport, Health, "
        f"Shopping, Entertainment, Subscriptions, Utilities, Travel, "
        f"Education, Personal Care, Other\n\nCategory:"
    )
    raw = _call_ollama(prompt).strip()
    # Extract first valid category mentioned
    for cat in VALID_CATEGORIES:
        if cat.lower() in raw.lower():
            return cat
    return "Other"


def categorise_batch(transactions: list) -> list:
    """
    Two-pass categorisation:
      1. Keyword matching (instant, no LLM) — catches ~80% of transactions
      2. LLM per-item for anything ambiguous (more reliable than batch for small models)
    """
    if not transactions:
        return transactions

    ambiguous = []

    # Pass 1: keywords
    for t in transactions:
        cat = _keyword_categorise(t.get("vendor", ""), t.get("description", ""))
        if cat:
            t["category"] = cat
        else:
            t["category"] = None
            ambiguous.append(t)

    print(f"  [PDFAgent] Keywords matched {len(transactions) - len(ambiguous)}/{len(transactions)}")

    # Pass 2: LLM for ambiguous ones (one at a time — more reliable for phi3)
    for t in ambiguous:
        cat = _llm_categorise_one(t.get("vendor", ""), t.get("description", ""))
        t["category"] = cat
        print(f"  [PDFAgent] LLM categorised: {t['vendor']} → {cat}")

    return transactions


# ── Main entry ────────────────────────────────────────────────────────────────

def parse_pdf(pdf_path: str) -> dict:
    if not HAS_PDF:
        return {"error": "pdfplumber not installed. Run: pip install pdfplumber", "transactions": []}

    print(f"  [PDFAgent] Extracting text...")
    text = extract_text(pdf_path)

    if text.startswith("[ERROR]"):
        return {"error": text, "transactions": []}

    print("  [PDFAgent] Parsing Hello Bank format...")
    transactions = parse_hellobank(text)
    method = "hellobank"

    if not transactions:
        print("  [PDFAgent] Falling back to LLM parsing...")
        transactions = parse_with_llm(text)
        method = "llm"

    if not transactions:
        return {
            "error": (
                "No transactions found. Send the PDF with `!pdftest` attached "
                "to see what text is being extracted."
            ),
            "transactions":     [],
            "raw_text_preview": text[:800],
        }

    print(f"  [PDFAgent] Categorising {len(transactions)} transactions ({method})...")
    transactions = categorise_batch(transactions)

    total = sum(t["amount"] for t in transactions)
    return {
        "transactions": transactions,
        "count":        len(transactions),
        "total":        round(total, 2),
        "method":       method,
        "error":        None,
    }
