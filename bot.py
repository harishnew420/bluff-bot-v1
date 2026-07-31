import os
import logging
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, WebAppInfo
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, ContextTypes
from telegram.constants import ParseMode

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

BOT_TOKEN = os.getenv("BOT_TOKEN", "")
WEBAPP_URL = os.getenv("WEBAPP_URL", "")

GAMES = {}

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🎴 *BLUFF MASTER* 🎴\n\n"
        "Use /create in your group to start!",
        parse_mode=ParseMode.MARKDOWN
    )

async def create(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.message.chat.type == "private":
        await update.message.reply_text("Use in group!")
        return
    
    game_id = f"{update.message.chat_id}"
    keyboard = [
        [InlineKeyboardButton("🎰 RUMMY", callback_data=f"mode_rummy_{game_id}")],
        [InlineKeyboardButton("♠️ NORMAL", callback_data=f"mode_normal_{game_id}")],
        [InlineKeyboardButton("🌈 UNO", callback_data=f"mode_uno_{game_id}")]
    ]
    
    await update.message.reply_text(
        "*SELECT MODE*",
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode=ParseMode.MARKDOWN
    )

async def mode_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    
    parts = query.data.split("_")
    mode = parts[1]
    game_id = parts[2]
    
    GAMES[game_id] = {
        "mode": mode,
        "host": query.from_user.id,
        "players": [query.from_user.id],
        "status": "waiting"
    }
    
    keyboard = [
        [InlineKeyboardButton("✅ JOIN", callback_data=f"join_{game_id}")],
        [InlineKeyboardButton("🎮 OPEN", web_app=WebAppInfo(url=f"{WEBAPP_URL}?game_id={game_id}&player_id={query.from_user.id}"))]
    ]
    
    await query.edit_message_text(
        f"Mode: {mode.upper()}\n"
        f"Players: {len(GAMES[game_id]['players'])}\n\n"
        "Click JOIN or OPEN!",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )

async def join_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    
    game_id = query.data.split("_")[1]
    
    if game_id not in GAMES:
        await query.edit_message_text("Game not found!")
        return
    
    if query.from_user.id not in GAMES[game_id]["players"]:
        GAMES[game_id]["players"].append(query.from_user.id)
    
    await query.edit_message_text(
        f"Mode: {GAMES[game_id]['mode'].upper()}\n"
        f"Players: {len(GAMES[game_id]['players'])}\n\n✅ Joined!"
    )

async def startgame(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.message.chat.type == "private":
        await update.message.reply_text("Use in group!")
        return
    
    game_id = str(update.message.chat_id)
    if game_id not in GAMES:
        await update.message.reply_text("No game found!")
        return
    
    game = GAMES[game_id]
    if update.message.from_user.id != game["host"]:
        await update.message.reply_text("Only host!")
        return
    
    game["status"] = "playing"
    
    url = f"{WEBAPP_URL}?game_id={game_id}&player_id={update.message.from_user.id}"
    keyboard = [[InlineKeyboardButton("🎮 PLAY", web_app=WebAppInfo(url=url))]]
    
    await update.message.reply_text(
        "🎴 GAME STARTED!\n\nClick PLAY!",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )

async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "/create - New game\n"
        "/startgame - Start"
    )

def main():
    app = Application.builder().token(BOT_TOKEN).build()
    
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("create", create))
    app.add_handler(CommandHandler("startgame", startgame))
    app.add_handler(CommandHandler("help", help_cmd))
    app.add_handler(CallbackQueryHandler(mode_callback, pattern="^mode_"))
    app.add_handler(CallbackQueryHandler(join_callback, pattern="^join_"))
    
    app.run_polling()

if __name__ == "__main__":
    main()
