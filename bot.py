"""
Expense Agent — Discord Bot
Tracks expenses via photo, text, or PDF. Full analysis + weekly digest.

Channels used:
  #expenses       — log expenses, ask questions
  #finance-digest — weekly automated digest (Sunday 8pm)

Commands:
  !summary           — spending summary last 30 days
  !week              — this week's breakdown
  !month             — this month's breakdown
  !top               — top expenses this month
  !categories        — all categories + monthly totals
  !budget <cat> <€>  — set a monthly budget for a category
  !delete <id>       — delete an expense
!recategorise      — re-run categorisation on uncategorised transactions
  !savings           — AI savings advice based on your patterns
  !help              — this message

Natural input:
  Send a photo/screenshot  → extracts expense automatically
  "spent €12 on lunch"     → logs expense
  Attach a PDF             → parses bank statement
"""

import discord
import asyncio
import os
import sys
import tempfile
from datetime import datetime
from apscheduler.schedulers.asyncio import AsyncIOScheduler

sys.path.insert(0, os.path.dirname(__file__))
from data import db
from agents import vision, analysis, pdf_parser, categoriser

# ── Config ────────────────────────────────────────────────────────────────────
BOT_TOKEN        = os.getenv("DISCORD_EXPENSE_BOT_TOKEN", os.getenv("DISCORD_BOT_TOKEN", ""))
EXPENSE_CHANNEL  = os.getenv("EXPENSE_CHANNEL_NAME",  "expenses")
DIGEST_CHANNEL   = os.getenv("DIGEST_CHANNEL_NAME",   "finance-digest")

# ── Discord setup ─────────────────────────────────────────────────────────────
intents                 = discord.Intents.default()
intents.message_content = True
client                  = discord.Client(intents=intents)
scheduler               = AsyncIOScheduler()

_scheduler_started         = False
_handled_message_ids: set  = set()
_MAX_CACHE                 = 500


# ── Helpers ───────────────────────────────────────────────────────────────────

async def send_long(channel, text: str):
    if len(text) <= 1900:
        await channel.send(text)
        return
    for chunk in [text[i:i+1900] for i in range(0, len(text), 1900)]:
        await channel.send(chunk)


async def get_channel(name: str):
    for guild in client.guilds:
        ch = discord.utils.get(guild.channels, name=name)
        if ch:
            return ch
    return None


def already_handled(message_id: int) -> bool:
    global _handled_message_ids
    if message_id in _handled_message_ids:
        return True
    _handled_message_ids.add(message_id)
    if len(_handled_message_ids) > _MAX_CACHE:
        oldest = list(_handled_message_ids)[:100]
        for mid in oldest:
            _handled_message_ids.discard(mid)
    return False


def format_expense_confirmation(data: dict, expense_id: int) -> str:
    """Format a confirmation message after logging an expense."""
    conf = data.get("confidence", "medium")
    conf_icon = {"high": "✅", "medium": "🟡", "low": "⚠️"}.get(conf, "🟡")
    return (
        f"{conf_icon} **Logged** (ID #{expense_id})\n"
        f"```"
        f"\nAmount   : €{data.get('amount', '?'):.2f}\n"
        f"Vendor   : {data.get('vendor') or 'unknown'}\n"
        f"Category : {data.get('category') or 'Other'}\n"
        f"Date     : {data.get('date') or 'today'}\n"
        f"```"
        f"*Reply `!edit {expense_id} category <name>` to fix category, "
        f"`!delete {expense_id}` to remove.*"
    )


def format_category_breakdown(rows: list, title: str, days: int) -> str:
    if not rows:
        return f"No expenses found for the last {days} days."

    total = sum(r["total"] for r in rows)
    budgets = {c["name"]: c.get("budget") for c in db.get_categories()}

    # Header
    lines = [f"**{title}** — €{total:.2f} total\n", "```text"] # Added 'text'
    
    for r in rows:
        cat = r["category"]
        # Use emoji to save space and add visual flair
        emoji = CAT_EMOJIS.get(cat, "💰")
        budget = budgets.get(cat)
        
        # Narrower progress bar: 10 chars instead of 20
        bar_str = ""
        if budget:
            pct = min(int((r["total"] / budget) * 10), 10)
            bar_str = f" {'█' * pct}{'░' * (10 - pct)} {r['total']/budget*100:.0f}%"
        
        # Reduced padding: 12 chars for category instead of 22
        # This keeps the total line width around 38-42 chars
        lines.append(
            f"{emoji} {cat[:12]:<12} €{r['total']:>7.2f}{bar_str}"
        )
        
    lines.append("```")
    return "\n".join(lines)

def clean_category(input_str: str) -> str:
    input_lower = input_str.lower().strip()
    
    # 1. Check for exact matches in VALID_CATEGORIES
    for cat in VALID_CATEGORIES:
        if input_lower == cat.lower():
            return cat
            
    # 2. Check keywords (borrowing logic from vision.py/categoriser.py)
    # You can import KEYWORD_CATEGORIES or define a local mapping
    mapping = {
        "Food & Dining": ["drinks", "coffee", "lunch", "dinner", "bar", "pub"],
        "Groceries": ["food", "supermarket", "snacks"]
    }
    
    for cat, keywords in mapping.items():
        if any(kw in input_lower for kw in keywords):
            return cat
            
    return input_str.title() # Fallback to Title Case if no match


# ── Scheduled jobs ────────────────────────────────────────────────────────────

async def weekly_digest_job():
    """Sunday 8pm: post weekly digest to #finance-digest."""
    ch = await get_channel(DIGEST_CHANNEL)
    if not ch:
        ch = await get_channel(EXPENSE_CHANNEL)
    if not ch:
        return

    await ch.send("📊 **Weekly Finance Digest** — crunching your numbers...")
    loop   = asyncio.get_event_loop()
    digest = await loop.run_in_executor(None, analysis.generate_weekly_digest)

    now    = datetime.now().strftime("%d %b %Y")
    header = f"💰 **Week ending {now}**\n\n"
    await send_long(ch, header + digest)

    # Also post anomalies separately if any
    current_exp = db.get_expenses(days=30)
    hist_exp    = db.get_expenses(days=120)
    anomalies   = analysis.detect_anomalies(current_exp, hist_exp)
    if anomalies:
        icons = {"HIGH": "🔴", "MEDIUM": "🟡", "LOW": "🔵"}
        lines = ["**⚠️ Spending Anomalies:**"]
        for a in anomalies:
            lines.append(f"{icons.get(a['severity'],'⚪')} {a['message']}")
        await ch.send("\n".join(lines))



# ── Pending category questions ───────────────────────────────────────────────
# Maps message_id → asyncio.Future so we can await user replies
_pending_cat_questions: dict[int, asyncio.Future] = {}

# Maps vendor_key → asyncio.Future (deduplicate: only ask once per vendor)
_pending_vendors: dict[str, asyncio.Future] = {}

CAT_EMOJIS = {
    "Food & Dining": "🍽️", "Groceries": "🛒", "Transport": "🚗",
    "Health": "💊", "Shopping": "🛍️", "Entertainment": "🎬",
    "Subscriptions": "📱", "Utilities": "💡", "Travel": "✈️",
    "Education": "📚", "Personal Care": "🪥", "Other": "📦",
}

VALID_CATEGORIES = [
    "Food & Dining", "Groceries", "Transport", "Health", "Shopping",
    "Entertainment", "Subscriptions", "Utilities", "Travel",
    "Education", "Personal Care", "Other",
]

def _build_cat_buttons(vendor: str, description: str, llm_guess: str, expense_ids: list):
    """Build a discord.ui.View with category buttons."""

    class CatView(discord.ui.View):
        def __init__(self):
            super().__init__(timeout=120)
            self.chosen   = None
            self.always   = True

        async def _respond(self, interaction, category):
            self.chosen = category
            self.stop()
            # Ask if this should always apply
            always_view = AlwaysView(category, vendor, expense_ids)
            await interaction.response.edit_message(
                content=(
                    f"✅ **{vendor}** → **{category}**\n"
                    f"Should I always categorise **{vendor}** as **{category}**?"
                ),
                view=always_view
            )

    # Add a button per category (split into 3 rows of 5)
    for i, cat in enumerate(VALID_CATEGORIES):
        emoji = CAT_EMOJIS.get(cat, "")
        btn   = discord.ui.Button(
            label=f"{emoji} {cat}",
            style=discord.ButtonStyle.primary if cat == llm_guess else discord.ButtonStyle.secondary,
            custom_id=f"cat_{cat}_{expense_ids[0]}",
            row=i // 5,
        )
        async def make_callback(c=cat):
            async def cb(interaction):
                await _respond_to_cat(interaction, c, vendor, expense_ids)
            return cb
        btn.callback = asyncio.coroutine(make_callback()) if False else None  # patched below
        CatView.add_item(btn)  # type: ignore

    return CatView()


class AlwaysView(discord.ui.View):
    def __init__(self, category, vendor, expense_ids):
        super().__init__(timeout=60)
        self.category    = category
        self.vendor      = vendor
        self.expense_ids = expense_ids

    @discord.ui.button(label="✅ Yes, always", style=discord.ButtonStyle.success)
    async def yes(self, interaction: discord.Interaction, button: discord.ui.Button):
        db.save_vendor_rule(self.vendor, self.category, always=True)
        # Update all matching expenses
        db.bulk_update_categories([(self.category, eid) for eid in self.expense_ids])
        # Resolve any waiting future
        key = db._vendor_key(self.vendor)
        if key in _pending_vendors and not _pending_vendors[key].done():
            _pending_vendors[key].set_result((self.category, True))
        await interaction.response.edit_message(
            content=f"✅ Got it — **{self.vendor}** will always be **{self.category}**.",
            view=None
        )
        self.stop()

    @discord.ui.button(label="🔁 Just this time", style=discord.ButtonStyle.secondary)
    async def once(self, interaction: discord.Interaction, button: discord.ui.Button):
        db.bulk_update_categories([(self.category, eid) for eid in self.expense_ids])
        key = db._vendor_key(self.vendor)
        if key in _pending_vendors and not _pending_vendors[key].done():
            _pending_vendors[key].set_result((self.category, False))
        await interaction.response.edit_message(
            content=f"✅ **{self.vendor}** → **{self.category}** (just this time).",
            view=None
        )
        self.stop()


async def ask_user_category(vendor: str, description: str, llm_guess: str, expense_ids: list) -> str | None:
    """
    Post a category question to the expense channel with buttons.
    Waits up to 90 seconds for a reply. Returns chosen category or None.
    Deduplicates: if the same vendor is already being asked, waits on same future.
    """
    ch  = await get_channel(EXPENSE_CHANNEL)
    if not ch:
        return None

    key = db._vendor_key(vendor)

    # Already waiting on this vendor — return same future
    if key in _pending_vendors and not _pending_vendors[key].done():
        try:
            result = await asyncio.wait_for(
                asyncio.shield(_pending_vendors[key]), timeout=90
            )
            return result[0] if result else None
        except asyncio.TimeoutError:
            return None

    # Create future
    loop   = asyncio.get_event_loop()
    future = loop.create_future()
    _pending_vendors[key] = future

    # Build interactive view
    view = CategoryView(vendor, description, llm_guess, expense_ids, future)

    desc_short = description[:60] + "..." if len(description) > 60 else description
    msg = await ch.send(
        f"❓ **Unknown vendor:** `{vendor}`\n"
        f"Description: _{desc_short}_\n"
        f"My best guess: **{llm_guess}** — what is this?",
        view=view,
    )

    try:
        result = await asyncio.wait_for(asyncio.shield(future), timeout=90)
        return result[0] if result else None
    except asyncio.TimeoutError:
        if not future.done():
            future.cancel()
        await msg.edit(
            content=f"⏱ No response for `{vendor}` — using guess: **{llm_guess}**",
            view=None
        )
        return None
    finally:
        _pending_vendors.pop(key, None)


class CategoryView(discord.ui.View):
    """Category selection buttons — one per valid category."""

    def __init__(self, vendor, description, llm_guess, expense_ids, future):
        super().__init__(timeout=90)
        self.vendor      = vendor
        self.description = description
        self.llm_guess   = llm_guess
        self.expense_ids = expense_ids
        self.future      = future

        for i, cat in enumerate(VALID_CATEGORIES):
            emoji = CAT_EMOJIS.get(cat, "")
            btn   = discord.ui.Button(
                label=f"{emoji} {cat}",
                style=discord.ButtonStyle.primary if cat == llm_guess
                      else discord.ButtonStyle.secondary,
                custom_id=f"cat_{i}_{expense_ids[0] if expense_ids else 0}",
                row=min(i // 5, 4),
            )
            btn.callback = self._make_callback(cat)
            self.add_item(btn)

    def _make_callback(self, category: str):
        async def callback(interaction: discord.Interaction):
            # Disable all buttons
            for item in self.children:
                item.disabled = True  # type: ignore

            await interaction.response.edit_message(
                content=(
                    f"✅ **{self.vendor}** → **{category}**\n"
                    f"Always categorise **{self.vendor}** as **{category}**?"
                ),
                view=AlwaysView(category, self.vendor, self.expense_ids, self.future),
            )
            self.stop()
        return callback


class AlwaysView(discord.ui.View):
    def __init__(self, category, vendor, expense_ids, future):
        super().__init__(timeout=60)
        self.category    = category
        self.vendor      = vendor
        self.expense_ids = expense_ids
        self.future      = future

    @discord.ui.button(label="✅ Yes, always", style=discord.ButtonStyle.success)
    async def yes(self, interaction: discord.Interaction, button: discord.ui.Button):
        db.save_vendor_rule(self.vendor, self.category, always=True)
        db.bulk_update_categories([(self.category, eid) for eid in self.expense_ids])
        if not self.future.done():
            self.future.set_result((self.category, True))
        await interaction.response.edit_message(
            content=f"✅ **{self.vendor}** → **{self.category}** (remembered forever)",
            view=None
        )
        self.stop()

    @discord.ui.button(label="🔁 Just this time", style=discord.ButtonStyle.secondary)
    async def once(self, interaction: discord.Interaction, button: discord.ui.Button):
        db.bulk_update_categories([(self.category, eid) for eid in self.expense_ids])
        if not self.future.done():
            self.future.set_result((self.category, False))
        await interaction.response.edit_message(
            content=f"✅ **{self.vendor}** → **{self.category}** (just this time)",
            view=None
        )
        self.stop()


async def run_categorisation_background(channel, transactions: list):
    """Run categoriser in background with interactive asking."""

    def progress(msg):
        print(f"  [BG Categoriser] {msg}")

    await categoriser.categorise_with_interaction(
        transactions,
        ask_callback=ask_user_category,
        progress_callback=progress,
    )

    # Summary
    cats = {}
    for t in transactions:
        c = t.get("category", "Other")
        cats[c] = cats.get(c, 0) + 1

    lines = ["✅ **Categorisation complete:**"]
    for cat, count in sorted(cats.items(), key=lambda x: -x[1]):
        emoji = CAT_EMOJIS.get(cat, "")
        lines.append(f"  {emoji} {cat}: {count}")
    await channel.send("\n".join(lines))


async def scheduled_recategorise():
    """Saturday 10am: auto-categorise via rules/keywords, ask for rest."""
    ch = await get_channel(EXPENSE_CHANNEL)
    if not ch:
        return
    loop    = asyncio.get_event_loop()
    pending = await loop.run_in_executor(
        None, categoriser.categorise_uncategorised_sync
    )
    if not pending:
        return
    await ch.send(f"🔄 Found **{len(pending)}** uncategorised transactions — asking for help...")
    await run_categorisation_background(ch, pending)


# ── Event handlers ────────────────────────────────────────────────────────────

@client.event
async def on_ready():
    global _scheduler_started
    db.init_db()
    print(f"[ExpenseAgent] Logged in as {client.user}")

    if not _scheduler_started:
        scheduler.add_job(weekly_digest_job, "cron", day_of_week="sun", hour=20, minute=0)
        scheduler.add_job(scheduled_recategorise, "cron", day_of_week="sat", hour=10, minute=0)
        scheduler.start()
        _scheduler_started = True
        print("[ExpenseAgent] Scheduler started.")

    print("[ExpenseAgent] Listening...")


@client.event
async def on_message(message: discord.Message):
    if message.author == client.user:
        return
    if already_handled(message.id):
        return

    # Only respond in expense channel or DMs
    if isinstance(message.channel, discord.DMChannel):
        pass
    elif message.channel.name != EXPENSE_CHANNEL:
        return

    text        = message.content.strip()
    attachments = message.attachments
    loop        = asyncio.get_event_loop()

    # ── Image attachment → vision extraction ─────────────────────────────────
    if attachments:
        for att in attachments:
            fname = att.filename.lower()

            # PDF bank statement
            if fname.endswith(".pdf"):
                await message.channel.send(f"📄 Parsing bank statement **{att.filename}**...")
                async with message.channel.typing():
                    with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as tmp:
                        await att.save(tmp.name)
                        result = await loop.run_in_executor(None, pdf_parser.parse_pdf, tmp.name)

                if result.get("error"):
                    await message.channel.send(f"❌ {result['error']}")
                    return

                txns = result["transactions"]
                # Log all transactions immediately as Uncategorised
                logged = []
                for t in txns:
                    exp_id = db.log_expense(
                        amount=t["amount"], vendor=t.get("vendor"),
                        category="Uncategorised",
                        description=t.get("description"), date_str=t.get("date"),
                        currency=t.get("currency", "EUR"), source="pdf"
                    )
                    t["id"] = exp_id
                    logged.append(t)

                await message.channel.send(
                    f"✅ Imported **{len(logged)}** transactions — €{result['total']:.2f} total\n"
                    f"🔄 Categorising in background... I'll update you when done."
                )

                # Fire categorisation in background — does not block
                asyncio.create_task(
                    run_categorisation_background(message.channel, logged)
                )
                return

            # Image receipt/screenshot
            if any(fname.endswith(ext) for ext in [".jpg", ".jpeg", ".png", ".webp", ".heic"]):
                await message.channel.send("🔍 Reading receipt...")
                async with message.channel.typing():
                    with tempfile.NamedTemporaryFile(suffix=".jpg", delete=False) as tmp:
                        await att.save(tmp.name)
                        data = await loop.run_in_executor(None, vision.extract_from_image, tmp.name)

                # NEW: Check if ANY field is missing or uncertain
                is_missing = (
                    not data.get("amount") or 
                    not data.get("vendor") or 
                    data.get("category") in [None, "Other", "Uncategorised"]
                )

                if is_missing:
                    view = MissingInfoView(data, message)
                    # Create a friendly string showing what we found vs what's missing
                    amt = f"€{data.get('amount')}" if data.get('amount') else "???"
                    vnd = data.get('vendor') or "???"
                    cat = data.get('category') or "???"
                    
                    await message.channel.send(
                        f"🔍 **Receipt parsed, but info is incomplete:**\n"
                        f"Amount: `{amt}` | Vendor: `{vnd}` | Category: `{cat}`",
                        view=view
                    )
                    return # Stop here and wait for the wizard

                expense_id = db.log_expense(
                    amount=data["amount"], vendor=data.get("vendor"),
                    category=data.get("category"), description=data.get("description"),
                    date_str=data.get("date"), currency=data.get("currency", "EUR"),
                    source="image", raw_text=str(data.get("_raw_response", ""))[:500]
                )
                await message.channel.send(format_expense_confirmation(data, expense_id))
                return

    # ── No attachment — handle text ───────────────────────────────────────────
    if not text:
        return

    tl = text.lower()

    # ── Commands ──────────────────────────────────────────────────────────────

    if tl.startswith("!last"):
        parts = text.split()
        n = 10
        if len(parts) > 1:
            try:
                n = min(int(parts[1]), 19)
            except ValueError:
                n = 10

        expenses = db.get_last_expenses(limit=n)
        if not expenses:
            await message.channel.send("No expenses found.")
            return

        lines = [f"**Last {len(expenses)} expenses:**", "```text"]
        for e in expenses:
            vendor = (e['vendor'] or e['description'] or 'unknown')
            # Truncate vendor for mobile compatibility
            vendor_short = vendor[:18] + ".." if len(vendor) > 20 else vendor
            lines.append(
                f"#{e['id']:<3} €{e['amount']:>7.2f} {vendor_short:<20} {e['date'][5:]}"
            )
        lines.append("```")
        # Footnote removed from here to prevent confusion
        await send_long(message.channel, "\n".join(lines))
        return

    if tl in ["!help", "help"]:
        await message.channel.send(HELP_TEXT)
        return

    if tl.startswith("!summary"):
        await message.channel.send("💭 Generating summary...")
        async with message.channel.typing():
            resp = await loop.run_in_executor(None, analysis.generate_quick_summary, 30)
        await send_long(message.channel, resp)
        return

    if tl.startswith("!week"):
        rows   = db.get_category_totals(days=7)
        total  = sum(r["total"] for r in rows)
        recent = db.get_expenses(days=7)
        lines  = [format_category_breakdown(rows, "This Week", 7)]
        if recent:
            lines.append("\n**Recent:**")
            for e in recent[:5]:
                lines.append(
                    f"`#{e['id']}` €{e['amount']:.2f} — "
                    f"{e['vendor'] or e['description'] or '?'} ({e['category']}) {e['date']}"
                )
        await send_long(message.channel, "\n".join(lines))
        return

    if tl.startswith("!month"):
        rows = db.get_category_totals(days=30)
        await send_long(message.channel, format_category_breakdown(rows, "This Month", 30))
        return

    if tl.startswith("!top"):
        expenses = db.get_expenses(days=30)
        top      = sorted(expenses, key=lambda x: x["amount"], reverse=True)[:10]
        if not top:
            await message.channel.send("No expenses logged this month.")
            return
        lines = ["**Top expenses this month:**\n```"]
        for e in top:
            lines.append(
                f"#{e['id']:<4} €{e['amount']:>7.2f}  "
                f"{(e['vendor'] or e['description'] or 'unknown'):<25} {e['category']}"
            )
        lines.append("```")
        await send_long(message.channel, "\n".join(lines))
        return

    if tl.startswith("!categories"):
        rows = db.get_category_totals(days=30)
        await send_long(message.channel, format_category_breakdown(rows, "All Categories — This Month", 30))
        return

    if tl.startswith("!budget "):
        parts = text.split()
        if len(parts) < 3:
            await message.channel.send("❓ Usage: `!budget <category> <amount>`\nExample: `!budget Food & Dining 500`")
            return
        try:
            amount   = float(parts[-1])
            category = " ".join(parts[1:-1])
            db.set_budget(category, amount)
            await message.channel.send(f"✅ Budget for **{category}** set to €{amount:.2f}/month")
        except ValueError:
            await message.channel.send("❓ Amount must be a number. Example: `!budget Groceries 400`")
        return

    if tl.startswith("!delete "):
        try:
            exp_id = int(text.split()[1])
            db.delete_expense(exp_id)
            await message.channel.send(f"✅ Expense #{exp_id} deleted.")
        except (IndexError, ValueError):
            await message.channel.send("❓ Usage: `!delete <id>`")
        return

    if tl.startswith("!edit "):
        parts = text.split(None, 3)
        if len(parts) == 4 and parts[2].lower() == "category":
            try:
                exp_id = int(parts[1])
                # Clean and map the category before saving
                new_cat = clean_category(parts[3]) 
                db.update_expense_category(exp_id, new_cat)
                await message.channel.send(f"✅ Expense #{exp_id} updated to **{new_cat}**.")
            except ValueError:
                await message.channel.send("❓ Usage: `!edit <id> category <name>`")
        return

    if tl.startswith("!savings"):
        await message.channel.send("🧠 Analysing your spending patterns...")
        async with message.channel.typing():
            resp = await loop.run_in_executor(None, analysis.generate_savings_advice)
        await send_long(message.channel, resp)
        return

    if tl.startswith("!digest"):
        await message.channel.send("📊 Generating digest...")
        async with message.channel.typing():
            digest = await loop.run_in_executor(None, analysis.generate_weekly_digest)
        await send_long(message.channel, digest)
        return

    if tl.startswith("!vendors"):
        rules = db.get_all_vendor_rules()
        if not rules:
            await message.channel.send("No vendor rules learned yet. They build up as you categorise.")
            return
        lines = ["**Learned vendor rules:**\n```"]
        for r in rules:
            always = "always" if r["always"] else "once"
            lines.append(f"{r['vendor_key']:<30} → {r['category']} ({always})")
        lines.append("```")
        lines.append("*Use `!forgetrule <vendor>` to remove a rule.*")
        await send_long(message.channel, "\n".join(lines))
        return

    if tl.startswith("!forgetrule "):
        vendor = text[12:].strip()
        conn = db.get_conn()
        conn.execute("DELETE FROM vendor_rules WHERE vendor_key LIKE ?", (f"%{vendor.lower()}%",))
        conn.commit()
        conn.close()
        await message.channel.send(f"✅ Removed rule for `{vendor}`.")
        return

    if tl.startswith("!recategorise") or tl.startswith("!recategorize"):
        uncats = db.get_uncategorised(limit=200)
        if not uncats:
            await message.channel.send("✅ Nothing to recategorise — all transactions have categories.")
            return
        await message.channel.send(f"🔄 Recategorising **{len(uncats)}** transactions in background...")
        asyncio.create_task(run_categorisation_background(message.channel, uncats))
        return

    if tl.startswith("!anomalies"):
        current_exp = db.get_expenses(days=30)
        hist_exp    = db.get_expenses(days=120)
        anomalies   = analysis.detect_anomalies(current_exp, hist_exp)
        alerts      = analysis.check_budget_alerts(db.get_category_totals(days=30))

        if not anomalies and not alerts:
            await message.channel.send("✅ No anomalies detected. Spending looks normal.")
            return

        icons = {"HIGH": "🔴", "MEDIUM": "🟡", "LOW": "🔵"}
        lines = []
        for a in anomalies:
            lines.append(f"{icons.get(a['severity'],'⚪')} {a['message']}")
        for a in alerts:
            lines.append(f"{icons.get(a['severity'],'⚪')} {a['message']}")
        await send_long(message.channel, "\n".join(lines))
        return


    if tl.startswith("!pdftest") and attachments:
        for att in attachments:
            if att.filename.lower().endswith(".pdf"):
                await message.channel.send(f"🔍 Extracting raw text from **{att.filename}**...")
                with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as tmp:
                    await att.save(tmp.name)
                    text = pdf_parser.extract_text(tmp.name)
                # Send first 1500 chars so you can see the format
                preview = text[:1500] if text else "Empty — no text layer found (scanned PDF?)"
                await send_long(message.channel, f"```\n{preview}\n```")
            return
        
    if tl.startswith("!ask"):
        query = text[4:].strip()
        if not query:
            await message.channel.send("❓ **Usage:** `!ask <your question>`\nExample: `!ask why is my Food & Dining so high?` or `!ask how can I save €100 next month?`")
            return
            
        await message.channel.send("🤔 Thinking...")
        async with message.channel.typing():
            # Use run_in_executor to keep the bot responsive while the LLM works
            resp = await loop.run_in_executor(None, analysis.chat_with_context, query)
        await send_long(message.channel, resp)
        return
    
    if tl.startswith("!budgets"):
        import calendar
        categories = db.get_categories()
        if not categories:
            await message.channel.send("No categories defined yet.")
            return

        now = datetime.now()
        _, days_in_month = calendar.monthrange(now.year, now.month)
        days_left = max(1, days_in_month - now.day + 1)

        lines = ["**📅 Monthly Budget Overview**", "```text"]
        total_limit = 0
        
        for c in categories:
            name   = c['name']
            limit  = c.get('budget') or 0
            emoji  = c.get('emoji', '💰')
            total_limit += limit
            
            limit_str = f"€{limit:,.0f}" if limit > 0 else "---"
            # Calculate daily allowance remaining for this category
            daily = f"€{limit/days_in_month:,.2f}/d" if limit > 0 else ""
            
            lines.append(f"{emoji} {name[:12]:<12} : {limit_str:>7}  {daily}")

        lines.append("-" * 32)
        lines.append(f"TOTAL BUDGET   : €{total_limit:,.2f}")
        lines.append(f"DAYS REMAINING : {days_left} days")
        lines.append("```")
        lines.append("*Use `!budget <cat> <€>` to set or change these limits.*")
        
        await message.channel.send("\n".join(lines))
        return

    # ── Natural language expense entry ────────────────────────────────────────
    data = vision.extract_from_text(text)
    
    # NEW: Trigger wizard if amount, vendor, or category is missing/Other
    is_missing = (
        not data.get("amount") or 
        not data.get("vendor") or 
        data.get("category") in [None, "Other", "Uncategorised"]
    )

    if is_missing:
        view = MissingInfoView(data, message)
        await message.channel.send(
            "❓ I couldn't capture all the details clearly. Would you like to complete them?",
            view=view
        )
    else:
        # All info is present, log normally
        expense_id = db.log_expense(
            amount=data["amount"], vendor=data.get("vendor"),
            category=data.get("category"), description=data.get("description"),
            date_str=data.get("date"), currency=data.get("currency", "EUR"),
            source="text", raw_text=text
        )
        await message.channel.send(format_expense_confirmation(data, expense_id))

# ── Help text ─────────────────────────────────────────────────────────────────

HELP_TEXT = """**Expense Agent — Commands**
```
!week              — this week's spending breakdown
!month             — this month by category
!last [n]          — show last n expenses (max 19)
!top               — top 10 expenses this month
!categories        — all categories with budget bars
!summary           — AI summary of last 30 days
!anomalies         — flag unusual spending
!savings           — AI savings advice
!digest            — generate weekly digest now
!budget <cat> <€>  — set monthly budget for category
!budgets           — view all monthly budget limits and daily allowances
!edit <id> category <name>
                   — fix a category (smart-maps keywords)
!delete <id>       — delete an expense
!recategorise      — re-run categorisation on uncategorised transactions
!vendors           — see learned vendor-to-category rules
!forgetrule <name> — remove a learned vendor rule
!ask <question>    — chat with the AI about your finances
```
**Log an expense:**
• Send a receipt photo or payment screenshot
• Type: `spent €12 at Panos for lunch`
• Attach a bank statement PDF

**Categories:** Food & Dining, Groceries, Transport, Health,
Shopping, Entertainment, Subscriptions, Utilities, Travel,
Education, Personal Care, Other
"""

class ExpenseCorrectionModal(discord.ui.Modal, title="Complete Expense Details"):
    amount_input = discord.ui.TextInput(label="Amount (€)", placeholder="e.g. 12.50", required=False)
    vendor_input = discord.ui.TextInput(label="Vendor", placeholder="e.g. Starbucks", required=False)
    # New category input field
    category_input = discord.ui.TextInput(label="Category (optional)", placeholder="e.g. Food & Dining", required=False)
    desc_input   = discord.ui.TextInput(label="Description", style=discord.TextStyle.paragraph, required=False)

    def __init__(self, data, original_msg):
        super().__init__()
        self.data = data
        self.original_msg = original_msg
        if data.get("amount"): self.amount_input.default = str(data["amount"])
        if data.get("vendor"): self.vendor_input.default = data["vendor"]
        if data.get("category"): self.category_input.default = data["category"]
        if data.get("description"): self.desc_input.default = data["description"]

    async def on_submit(self, interaction: discord.Interaction):
        try:
            # 1. Update basic fields
            if self.amount_input.value:
                self.data["amount"] = float(self.amount_input.value.replace(',', '.'))
            
            vendor_name = self.vendor_input.value or self.data.get("vendor")
            self.data["vendor"] = vendor_name
            self.data["description"] = self.desc_input.value or self.data.get("description")

            # 2. Derive Category: Manual Input > Learned Rules > Keywords > Default
            manual_cat = self.category_input.value.strip()
            if manual_cat:
                # Use the clean_category helper logic to map things like "drinks" -> "Food & Dining"
                self.data["category"] = clean_category(manual_cat) 
            else:
                # Try to auto-derive from vendor name using existing agent logic
                derived = categoriser.from_vendor_rules(vendor_name) or \
                          categoriser.from_keywords(vendor_name, self.data.get("description", ""))
                self.data["category"] = derived or "Other"

            # 3. Log to database
            expense_id = db.log_expense(
                amount=self.data.get("amount", 0.0),
                vendor=self.data.get("vendor"),
                category=self.data.get("category"),
                description=self.data.get("description"),
                date_str=self.data.get("date"),
                source="interactive"
            )

            confirmation_text = format_expense_confirmation(self.data, expense_id)
            await interaction.response.edit_message(content=confirmation_text, view=None)

        except ValueError:
            await interaction.response.send_message("❌ Invalid amount format.", ephemeral=True)

class MissingInfoView(discord.ui.View):
    def __init__(self, data, message):
        super().__init__(timeout=120)
        self.data = data
        self.message = message

    @discord.ui.button(label="Fill Missing Info", style=discord.ButtonStyle.primary)
    async def fill(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(ExpenseCorrectionModal(self.data, self.message))

    @discord.ui.button(label="Skip & Log Anyway", style=discord.ButtonStyle.secondary)
    async def skip(self, interaction: discord.Interaction, button: discord.ui.Button):
        # Log with 0.0 or whatever exists
        expense_id = db.log_expense(
            amount=self.data.get("amount") or 0.0,
            vendor=self.data.get("vendor") or "Unknown",
            category=self.data.get("category") or "Other",
            description=self.data.get("description"),
            date_str=self.data.get("date")
        )
        await interaction.response.edit_message(
            content=f"Logged with missing info (ID #{expense_id}).", 
            view=None
        )

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.danger)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.edit_message(content="❌ Logging cancelled.", view=None)


# ── Run ───────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    if not BOT_TOKEN:
        print("❌ Set DISCORD_BOT_TOKEN environment variable first.")
        sys.exit(1)
    client.run(BOT_TOKEN)
