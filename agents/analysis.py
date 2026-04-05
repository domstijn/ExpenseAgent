"""
Analysis Agent — spending trends, anomaly detection, savings advice.
Pure logic for anomalies, LLM for narrative and savings advice.
"""

import ollama
import json
import re
from datetime import datetime, date
from collections import defaultdict

import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
from data import db

OLLAMA_MODEL = "phi3:mini"


def _call_ollama(prompt: str) -> str:
    try:
        # Using ollama.generate provides clean text output directly
        response = ollama.generate(model=OLLAMA_MODEL, prompt=prompt)
        return response['response'].strip()
    except Exception as e:
        return f"[ERROR] {e}"


# ── Anomaly detection (pure logic) ───────────────────────────────────────────

def detect_anomalies(current_expenses: list, historical_expenses: list) -> list:
    """
    Compare current month spending to previous months.
    Flags categories where spending is significantly above average.
    """
    anomalies = []

    # Group current month by category
    current_by_cat = defaultdict(float)
    for e in current_expenses:
        current_by_cat[e.get("category", "Other")] += e["amount"]

    # Group historical by category + month
    hist_by_cat_month = defaultdict(lambda: defaultdict(float))
    for e in historical_expenses:
        month = e["date"][:7]
        cat   = e.get("category", "Other")
        hist_by_cat_month[cat][month] += e["amount"]

    current_month = datetime.now().strftime("%Y-%m")

    for cat, current_total in current_by_cat.items():
        past_months = {
            m: t for m, t in hist_by_cat_month[cat].items()
            if m != current_month
        }
        if len(past_months) < 2:
            continue

        avg    = sum(past_months.values()) / len(past_months)
        max_h  = max(past_months.values())

        if avg == 0:
            continue

        ratio = current_total / avg

        if ratio >= 2.0:
            anomalies.append({
                "type":     "OVERSPEND",
                "severity": "HIGH" if ratio >= 3.0 else "MEDIUM",
                "category": cat,
                "current":  round(current_total, 2),
                "average":  round(avg, 2),
                "ratio":    round(ratio, 2),
                "message":  f"{cat}: €{current_total:.0f} this month vs €{avg:.0f} avg ({ratio:.1f}x normal)",
            })
        elif ratio <= 0.3 and avg > 50:
            # Unusually low — might be missing data, flag gently
            anomalies.append({
                "type":     "UNDERSPEND",
                "severity": "LOW",
                "category": cat,
                "current":  round(current_total, 2),
                "average":  round(avg, 2),
                "ratio":    round(ratio, 2),
                "message":  f"{cat}: only €{current_total:.0f} logged (usually ~€{avg:.0f}) — missing receipts?",
            })

    anomalies.sort(key=lambda x: {"HIGH": 0, "MEDIUM": 1, "LOW": 2}[x["severity"]])
    return anomalies


def check_budget_alerts(category_totals: list) -> list:
    """Flag categories that have exceeded their monthly budget."""
    alerts    = []
    budgets   = {c["name"]: c["budget"] for c in db.get_categories() if c["budget"]}
    now       = datetime.now()
    days_in   = now.day
    days_total= 30

    for row in category_totals:
        cat    = row["category"]
        total  = row["total"]
        budget = budgets.get(cat)
        if not budget:
            continue

        pct = (total / budget) * 100
        # Project to end of month
        projected = (total / days_in) * days_total if days_in > 0 else total

        if pct >= 100:
            alerts.append({
                "category":  cat,
                "spent":     round(total, 2),
                "budget":    budget,
                "pct":       round(pct, 1),
                "projected": round(projected, 2),
                "severity":  "HIGH",
                "message":   f"{cat}: €{total:.0f} spent — {pct:.0f}% of €{budget} budget (OVER)",
            })
        elif pct >= 80:
            alerts.append({
                "category":  cat,
                "spent":     round(total, 2),
                "budget":    budget,
                "pct":       round(pct, 1),
                "projected": round(projected, 2),
                "severity":  "MEDIUM",
                "message":   f"{cat}: €{total:.0f} of €{budget} budget ({pct:.0f}%) — projecting €{projected:.0f} by month end",
            })

    return alerts


# ── Trend analysis ────────────────────────────────────────────────────────────

def compute_trends() -> dict:
    """Compute month-over-month trends."""
    monthly = db.get_monthly_totals(months=6)
    if len(monthly) < 2:
        return {"monthly": monthly, "trend": "insufficient data"}

    # Most recent complete month vs previous
    totals = [m["total"] for m in monthly]
    latest = totals[0]
    prev   = totals[1] if len(totals) > 1 else latest
    avg_3m = sum(totals[1:4]) / min(3, len(totals[1:4]))

    trend_pct = ((latest - prev) / prev * 100) if prev else 0

    return {
        "monthly":      monthly,
        "latest":       round(latest, 2),
        "prev_month":   round(prev, 2),
        "avg_3m":       round(avg_3m, 2),
        "trend_pct":    round(trend_pct, 1),
        "trend_dir":    "up" if trend_pct > 5 else "down" if trend_pct < -5 else "stable",
    }


# ── LLM narrative & savings advice ────────────────────────────────────────────

def generate_weekly_digest() -> str:
    """Generate a full weekly spending digest with LLM narrative."""
    cat_totals  = db.get_category_totals(days=7)
    month_total = db.get_category_totals(days=30)
    monthly     = db.get_monthly_totals(months=3)
    recent      = db.get_expenses(days=7)
    categories  = db.get_categories()

    # Budget map
    budgets = {c["name"]: c["budget"] for c in categories if c["budget"]}

    week_total  = sum(r["total"] for r in cat_totals)
    month_spend = sum(r["total"] for r in month_total)

    # Anomalies
    current_exp  = db.get_expenses(days=30)
    hist_exp     = db.get_expenses(days=120)
    anomalies    = detect_anomalies(current_exp, hist_exp)
    budget_alerts= check_budget_alerts(month_total)
    trends       = compute_trends()

    # Build context for LLM
    cat_lines = "\n".join(
        f"  {r['category']}: €{r['total']:.2f} ({r['count']} transactions)"
        for r in cat_totals
    )
    monthly_lines = "\n".join(
        f"  {m['month']}: €{m['total']:.2f} ({m['count']} transactions)"
        for m in monthly
    )
    anomaly_lines = "\n".join(f"  ⚠️ {a['message']}" for a in anomalies) or "  None"
    budget_lines  = "\n".join(f"  {'🔴' if a['severity']=='HIGH' else '🟡'} {a['message']}"
                               for a in budget_alerts) or "  All within budget"

    # Top 3 biggest individual expenses this week
    top_expenses = sorted(recent, key=lambda x: x["amount"], reverse=True)[:3]
    top_lines    = "\n".join(
        f"  €{e['amount']:.2f} — {e['vendor'] or e['description'] or 'unknown'} ({e['category']})"
        for e in top_expenses
    )

    prompt = f"""You are a personal finance analyst preparing a weekly expense digest for an executive.

WEEK SUMMARY:
Total spent this week: €{week_total:.2f}
Total spent this month so far: €{month_spend:.2f}

BY CATEGORY (this week):
{cat_lines if cat_lines else "No expenses logged this week."}

MONTHLY TREND:
{monthly_lines}
Trend vs last month: {trends.get('trend_pct', 0):+.1f}% ({trends.get('trend_dir', 'stable')})

TOP EXPENSES THIS WEEK:
{top_lines if top_lines else "None"}

ANOMALIES:
{anomaly_lines}

BUDGET ALERTS:
{budget_lines}

RULES:
- Lead with a one-line verdict (good week / overspending / on track)
- Call out any anomalies or budget issues specifically
- Give 1-2 concrete, specific savings suggestions based on the actual data
- Keep it under 200 words
- Discord format — short paragraphs, no walls of text
- End with next week's one thing to watch

Write the digest now:"""

    return _call_ollama(prompt)


def generate_quick_summary(days: int = 30) -> str:
    """Quick on-demand summary for !summary command."""
    cat_totals = db.get_category_totals(days=days)
    total      = sum(r["total"] for r in cat_totals)
    trends     = compute_trends()

    cat_lines = "\n".join(
        f"{r['category']}: €{r['total']:.2f}"
        for r in cat_totals[:6]
    )

    prompt = f"""Quick expense summary for last {days} days.
Total: €{total:.2f}
By category:
{cat_lines}
Month trend: {trends.get('trend_pct', 0):+.1f}% vs previous month.

Write 3-4 sentences: total, biggest category, trend, one observation.
Be direct. Discord format."""

    return _call_ollama(prompt)


def generate_savings_advice() -> str:
    """Targeted savings advice based on spending patterns."""
    cat_totals = db.get_category_totals(days=90)
    monthly    = db.get_monthly_totals(months=3)
    total_90d  = sum(r["total"] for r in cat_totals)

    cat_lines = "\n".join(
        f"  {r['category']}: €{r['total']:.2f} over 90 days (avg €{r['total']/3:.0f}/month)"
        for r in cat_totals
    )

    prompt = f"""You are a financial advisor reviewing 90 days of spending.

SPENDING (90 days, €{total_90d:.2f} total):
{cat_lines}

Give 3 specific, actionable savings suggestions based on this actual data.
Each suggestion: what to cut, by how much, and what that saves monthly/yearly.
Be specific — not "eat out less" but "your dining is €X/month, reducing by 30% saves €Y/year".
Discord format. Under 150 words."""

    return _call_ollama(prompt)
