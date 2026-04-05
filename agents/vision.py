"""
Vision Agent — extracts expense data from receipt/screenshot images.
Uses llama3.2-vision via the ollama Python library. Fully local.
Falls back to llava:7b if llama3.2-vision isn't available.
"""

import json
import re
import subprocess
from datetime import datetime

try:
    import ollama
    HAS_OLLAMA = True
except ImportError:
    HAS_OLLAMA = False

VISION_MODELS = ["llama3.2-vision", "llava:7b", "llava"]


def _available_vision_model() -> str:
    """Return the first installed vision model."""
    try:
        result = subprocess.run(
            ["ollama", "list"], capture_output=True, text=True, timeout=10
        )
        for model in VISION_MODELS:
            if model.split(":")[0] in result.stdout:
                return model
    except Exception:
        pass
    return VISION_MODELS[0]


def _call_vision(image_path: str, prompt: str, model: str) -> str:
    """Call Ollama vision model using the ollama Python library."""
    if not HAS_OLLAMA:
        return "[ERROR] ollama package not installed. Run: pip install ollama"
    try:
        response = ollama.chat(
            model=model,
            messages=[{
                "role":    "user",
                "content": prompt,
                "images":  [image_path],
            }]
        )
        return response["message"]["content"].strip()
    except Exception as e:
        return f"[VISION ERROR] {e}"


def extract_from_image(image_path: str) -> dict:
    """
    Extract expense fields from a receipt or payment screenshot.
    Returns: amount, currency, vendor, date, category, description, confidence
    """
    model  = _available_vision_model()
    prompt = """Extract expense information from this receipt or payment screenshot.
Return ONLY a JSON object with these fields (use null if not found):
{
  "amount": <number, the final total paid>,
  "currency": <EUR/USD/GBP, default EUR if unclear>,
  "vendor": <business or merchant name>,
  "date": <YYYY-MM-DD, use today if not visible>,
  "category": <one of: Food & Dining, Groceries, Transport, Health, Shopping, Entertainment, Subscriptions, Utilities, Travel, Education, Personal Care, Other>,
  "description": <brief description of what was purchased>,
  "confidence": <high/medium/low based on image clarity>
}
Return only the JSON object, no explanation."""

    raw = _call_vision(image_path, prompt, model)

    try:
        clean = re.sub(r"```json|```", "", raw).strip()
        match = re.search(r"\{.*\}", clean, re.DOTALL)
        if match:
            data = json.loads(match.group())
            data["_model"] = model
            return data
    except Exception:
        pass

    return {
        "amount":      None,
        "currency":    "EUR",
        "vendor":      None,
        "date":        datetime.now().strftime("%Y-%m-%d"),
        "category":    "Other",
        "description": "Could not parse receipt",
        "confidence":  "low",
        "_raw":        raw,
        "_model":      model,
    }


def extract_from_text(text: str) -> dict:
    """
    Extract expense fields from natural language.
    e.g. "spent €12 on lunch at Panos" or "Albert Heijn 45.30"
    Pure regex — no LLM needed for simple cases.
    """
    result = {
        "amount":      None,
        "currency":    "EUR",
        "vendor":      None,
        "date":        datetime.now().strftime("%Y-%m-%d"),
        "category":    None,
        "description": text.strip(),
        "confidence":  "medium",
    }

    # Amount patterns
    amount_patterns = [
        r"[€$£]\s*(\d+[.,]\d{1,2})",
        r"(\d+[.,]\d{1,2})\s*[€$£]",
        r"(\d+[.,]\d{1,2})\s*(?:euro|eur|usd|dollar|pound|gbp)",
        r"(?:euro|eur|usd)\s*(\d+[.,]\d{1,2})",
        r"\b(\d+[.,]\d{1,2})\b",
        r"\b(\d{1,4})\s*(?:euro|eur|dollar)\b",
        r"[€$£]\s*(\d+)\b",
        r"\b(\d+)\s*[€$£]",
    ]
    for p in amount_patterns:
        m = re.search(p, text, re.IGNORECASE)
        if m:
            result["amount"] = float(m.group(1).replace(",", "."))
            break

    # Currency
    if "€" in text or re.search(r"\beuro\b|\beur\b", text, re.IGNORECASE):
        result["currency"] = "EUR"
    elif "$" in text or re.search(r"\busd\b|\bdollar\b", text, re.IGNORECASE):
        result["currency"] = "USD"
    elif "£" in text or re.search(r"\bgbp\b|\bpound\b", text, re.IGNORECASE):
        result["currency"] = "GBP"

    # Category from keywords
    category_keywords = {
        "Food & Dining":  ["restaurant", "cafe", "lunch", "dinner", "breakfast", "pizza",
                           "burger", "sushi", "bar", "pub", "coffee", "drinks", "eating"],
        "Groceries":      ["supermarket", "grocery", "groceries", "albert heijn", "delhaize",
                           "lidl", "aldi", "colruyt", "carrefour", "jumbo"],
        "Transport":      ["taxi", "uber", "lyft", "bus", "metro", "train", "fuel", "petrol",
                           "parking", "nmbs", "stib", "de lijn", "tec", "sncb"],
        "Health":         ["pharmacy", "doctor", "dentist", "hospital", "medicine",
                           "supplement", "vitamin", "apotheek", "pharmacie"],
        "Shopping":       ["amazon", "bol.com", "zalando", "h&m", "zara", "ikea",
                           "clothing", "shoes", "electronics", "fnac", "mediamarkt"],
        "Entertainment":  ["cinema", "movie", "netflix", "spotify", "game", "concert",
                           "theatre", "theater", "museum", "ticket"],
        "Subscriptions":  ["subscription", "monthly", "annual", "membership",
                           "netflix", "spotify", "adobe", "microsoft", "apple"],
        "Utilities":      ["electricity", "gas", "water", "internet", "phone", "mobile",
                           "proximus", "telenet", "orange", "base"],
        "Travel":         ["hotel", "airbnb", "booking", "flight", "airline", "ryanair",
                           "brussels airlines", "airport", "hostel"],
        "Education":      ["course", "book", "udemy", "coursera", "school", "training"],
        "Personal Care":  ["haircut", "salon", "spa", "gym", "fitness", "kine", "beauty"],
    }
    text_lower = text.lower()
    for cat, keywords in category_keywords.items():
        if any(kw in text_lower for kw in keywords):
            result["category"] = cat
            break
    if not result["category"]:
        result["category"] = "Other"

    # Vendor from "at X" / "from X" / "@ X"
    vendor_match = re.search(
        r"(?:at|from|@|in)\s+([A-Z][a-zA-Z\s&']+?)(?:\s+for|\s+\d|$|,|\.|€|\$)",
        text
    )
    if vendor_match:
        result["vendor"] = vendor_match.group(1).strip()

    return result
