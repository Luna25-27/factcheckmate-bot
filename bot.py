import logging
import requests
import aiosqlite
import asyncio
import os
from telegram import Update, InlineKeyboardMarkup, InlineKeyboardButton
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ContextTypes,
    filters
)
from dotenv import load_dotenv
from flask import Flask, request

load_dotenv()

# Logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# API Keys
BOT_TOKEN = os.getenv("BOT_TOKEN")
GOOGLE_API_KEY = os.getenv("GOOGLE_API_KEY")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
ADMIN_IDS = [int(x) for x in os.getenv("ADMIN_IDS", "").split(",") if x]

feedback_store = {}

WEBHOOK_HOST = os.getenv("WEBHOOK_HOST")  # e.g. https://your-service.onrender.com
WEBHOOK_PATH = f"/webhook/{BOT_TOKEN}"
WEBHOOK_URL = f"{WEBHOOK_HOST}{WEBHOOK_PATH}"
PORT = int(os.getenv("PORT", 10000))

app_flask = Flask(__name__)

@app.route("/")
def index():
    return "OK", 200

@app_flask.route("/health", methods=["GET"])
def health():
    return "ok", 200

@app_flask.route(WEBHOOK_PATH, methods=["POST"])
def webhook():
    from telegram import Update
    from telegram.ext import Application
    application = app_flask.application
    update = Update.de_json(request.get_json(force=True), application.bot)
    application.update_queue.put_nowait(update)
    return "", 204

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🤖 *FactCheckMate* at your service!\n\n"
        "Send a claim using `/factcheck <statement>` to verify.\n"
        "Try: `/factcheck The moon is made of cheese`",
        parse_mode='Markdown'
    )

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🆘 *Help Guide*\n\n"
        "/factcheck <claim> - Check a claim\n"
        "/quicknews - Trending claims\n"
        "👍👎 Vote on responses\n"
        "🛡️ Auto-check in groups (admin-only)",
        parse_mode='Markdown'
    )

def query_google_fact_check_api(claim: str) -> str:
    url = "https://factchecktools.googleapis.com/v1alpha1/claims:search"
    params = {"query": claim, "key": GOOGLE_API_KEY, "languageCode": "en-US"}
    response = requests.get(url, params=params)
    data = response.json()

    if not data.get("claims"):
        return None

    c = data["claims"][0]
    text = c.get("text", "Unknown")
    source = c.get("claimant", "Unknown")
    review = c.get("claimReview", [{}])[0]
    rating = review.get("textualRating", "Unrated")
    pub = review.get("publisher", {}).get("name", "Unknown")
    url = review.get("url", "")

    return (
        f"🔎 *Claim:* {text}\n"
        f"👤 *Source:* {source}\n"
        f"📝 *Rating:* {rating}\n"
        f"🏢 *Publisher:* {pub}\n"
        f"🔗 [Read more]({url})"
    )

def ai_suggest_fact_check(claim: str) -> str:
    import openai
    openai.api_key = OPENAI_API_KEY

    try:
        res = openai.ChatCompletion.create(
            model="gpt-4",
            messages=[
                {"role": "system", "content": "You're a factual assistant. Be objective."},
                {"role": "user", "content": f"Is this claim true or false?\nClaim: {claim}"}
            ],
            max_tokens=150
        )
        return f"🤖 *AI Suggestion:*\n\n{res['choices'][0]['message']['content']}"
    except Exception as e:
        logger.error(f"AI Error: {e}")
        return "⚠️ AI check unavailable right now."

async def init_db():
    async with aiosqlite.connect("claims.db") as db:
        await db.execute("""
            CREATE TABLE IF NOT EXISTS claims (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER,
                username TEXT,
                claim TEXT,
                timestamp DATETIME DEFAULT CURRENT_TIMESTAMP
            )
        """)
        await db.commit()

async def log_claim(update, claim):
    async with aiosqlite.connect("claims.db") as db:
        await db.execute(
            "INSERT INTO claims (user_id, username, claim) VALUES (?, ?, ?)",
            (update.effective_user.id, update.effective_user.username, claim)
        )
        await db.commit()

def create_vote_buttons(claim_id: str):
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("👍 Helpful", callback_data=f"vote_yes:{claim_id}"),
        InlineKeyboardButton("👎 Not Helpful", callback_data=f"vote_no:{claim_id}")
    ]])

async def factcheck(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("❗ Usage: /factcheck <your claim>")
        return

    claim = " ".join(context.args)
    await log_claim(update, claim)

    response = query_google_fact_check_api(claim)
    if not response:
        response = ai_suggest_fact_check(claim)

    claim_id = claim[:50].replace(" ", "_")
    await update.message.reply_text(
        response,
        parse_mode='Markdown',
        reply_markup=create_vote_buttons(claim_id),
        disable_web_page_preview=True
    )

async def quicknews(update: Update, context: ContextTypes.DEFAULT_TYPE):
    trending = [
        "📰 *Claim:* Earth is flat\n📝 *Rating:* False\n🔗 [More](https://en.wikipedia.org/wiki/Spherical_Earth)",
        "📰 *Claim:* COVID vaccines cause autism\n📝 *Rating:* False\n🔗 [CDC](https://www.cdc.gov)",
        "📰 *Claim:* Climate change is a hoax\n📝 *Rating:* False\n🔗 [NASA](https://climate.nasa.gov)"
    ]
    await update.message.reply_text("🔥 *Trending Fact-Checks:*\n\n" + "\n\n".join(trending), parse_mode='Markdown')

async def auto_fact_check(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in ADMIN_IDS:
        return

    text = update.message.text
    if len(text.split()) < 4:
        return

    response = query_google_fact_check_api(text)
    if not response:
        response = ai_suggest_fact_check(text)

    await update.message.reply_text("📢 Auto Fact-Check:\n\n" + response, parse_mode='Markdown', disable_web_page_preview=True)

async def vote_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    vote_type, claim_id = query.data.split(":")
    key = f"{claim_id}_{query.from_user.id}"

    if key in feedback_store:
        await query.message.reply_text("✅ You already voted.")
        return

    feedback_store[key] = vote_type
    await query.message.reply_text("🙌 Thanks for your feedback!")

async def main():
    await init_db()
    application = ApplicationBuilder().token(BOT_TOKEN).build()

    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(CommandHandler("factcheck", factcheck))
    application.add_handler(CommandHandler("quicknews", quicknews))
    application.add_handler(MessageHandler(filters.TEXT & filters.ChatType.GROUPS, auto_fact_check))
    application.add_handler(CallbackQueryHandler(vote_handler))

    app_flask.application = application
    await application.initialize()
    await application.start()
    await application.bot.set_webhook(url=WEBHOOK_URL)
    print(f"✅ Webhook set: {WEBHOOK_URL}")

if __name__ == "__main__":
    import nest_asyncio
    nest_asyncio.apply()
    import asyncio
    loop = asyncio.get_event_loop()
    loop.run_until_complete(main())
    app_flask.run(host="0.0.0.0", port=PORT)
