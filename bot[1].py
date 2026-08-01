import os
import json
import time
import random
import logging
import threading
from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, ContextTypes
from telegram.constants import ParseMode

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

BOT_TOKEN = os.getenv("BOT_TOKEN", "")
# Render gives every web service its own public URL automatically.
BACKEND_URL = os.getenv("RENDER_EXTERNAL_URL", "").rstrip("/")
WEBAPP_URL = os.getenv("WEBAPP_URL", "")

GAMES = {}
LOCK = threading.Lock()

TURN_SECONDS = 50
START_WAIT_SECONDS = 120

SUITS = ["♠", "♥", "♦", "♣"]
RANKS = ["A", "2", "3", "4", "5", "6", "7", "8", "9", "10", "J", "Q", "K"]


def new_deck():
    deck = [f"{r}{s}" for s in SUITS for r in RANKS]
    random.shuffle(deck)
    return deck


class Game:
    def __init__(self, game_id, chat_id, mode, host_id, host_name):
        self.game_id = game_id
        self.chat_id = chat_id
        self.mode = mode
        self.host_id = host_id
        self.players = {host_id: {"name": host_name, "cards": []}}
        self.status = "waiting"  # waiting -> playing -> finished / cancelled
        self.turn_order = []
        self.current_turn = None
        self.table_cards = []
        self.deck = []
        self.turn_deadline = None
        self.winners = []
        self.created_at = time.time()

    def add_player(self, uid, name):
        if uid not in self.players:
            self.players[uid] = {"name": name, "cards": []}
            return True
        return False

    def start(self):
        if len(self.players) < 2:
            return False, "Need at least 2 players"
        self.deck = new_deck()
        self.turn_order = list(self.players.keys())
        random.shuffle(self.turn_order)
        per_player = 13
        idx = 0
        for uid in self.turn_order:
            self.players[uid]["cards"] = self.deck[idx:idx + per_player]
            idx += per_player
        self.table_cards = []
        self.current_turn = self.turn_order[0]
        self.turn_deadline = time.time() + TURN_SECONDS
        self.status = "playing"
        return True, "started"

    def advance_turn(self):
        alive = [u for u in self.turn_order if len(self.players[u]["cards"]) > 0]
        if len(alive) <= 1:
            self.status = "finished"
            if alive:
                self.winners.append(self.players[alive[0]]["name"])
            self.current_turn = None
            return
        idx = self.turn_order.index(self.current_turn)
        for step in range(1, len(self.turn_order) + 1):
            nxt = self.turn_order[(idx + step) % len(self.turn_order)]
            if len(self.players[nxt]["cards"]) > 0:
                self.current_turn = nxt
                break
        self.turn_deadline = time.time() + TURN_SECONDS

    def check_timeout(self):
        """If current player's turn expired, auto-drop a card for them."""
        if self.status != "playing" or self.current_turn is None:
            return
        if self.turn_deadline and time.time() > self.turn_deadline:
            cards = self.players[self.current_turn]["cards"]
            if cards:
                dropped = cards.pop(0)
                self.table_cards.append(dropped)
                if len(cards) == 0:
                    self.winners.append(self.players[self.current_turn]["name"])
            self.advance_turn()

    def play_card(self, uid, card):
        if self.status != "playing" or uid != self.current_turn:
            return False, "Not your turn"
        cards = self.players[uid]["cards"]
        if card not in cards:
            return False, "Card not in hand"
        cards.remove(card)
        self.table_cards.append(card)
        if len(cards) == 0:
            self.winners.append(self.players[uid]["name"])
        self.advance_turn()
        return True, "played"

    def to_dict(self, viewer_id=None):
        self.check_timeout()
        players_public = {
            str(uid): {"name": p["name"], "card_count": len(p["cards"])}
            for uid, p in self.players.items()
        }
        d = {
            "game_id": self.game_id,
            "mode": self.mode,
            "host_id": self.host_id,
            "status": self.status,
            "players": players_public,
            "table_cards": self.table_cards,
            "current_turn": self.current_turn,
            "turn_seconds_left": max(0, int(self.turn_deadline - time.time())) if self.turn_deadline else None,
            "winners": self.winners,
        }
        if viewer_id is not None and viewer_id in self.players:
            d["my_hand"] = self.players[viewer_id]["cards"]
        return d


# ---------------- Telegram command handlers ----------------

async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🎴 *BLUFF MASTER* 🎴\n\n"
        "/creategame - start a new game in this group",
        parse_mode=ParseMode.MARKDOWN
    )


async def creategame_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.message.chat.type == "private":
        await update.message.reply_text("Use this in a group!")
        return

    game_id = f"{update.message.chat_id}_{int(time.time())}"
    keyboard = [
        [InlineKeyboardButton("🎰 RUMMY", callback_data=f"mode|rummy|{game_id}")],
        [InlineKeyboardButton("♠️ NORMAL", callback_data=f"mode|normal|{game_id}")],
    ]
    await update.message.reply_text(
        "*SELECT GAME MODE*",
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode=ParseMode.MARKDOWN
    )


async def mode_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    _, mode, game_id = query.data.split("|")

    with LOCK:
        game = Game(game_id, query.message.chat_id, mode, query.from_user.id, query.from_user.first_name)
        GAMES[game_id] = game

    context.job_queue.run_once(auto_cancel_job, START_WAIT_SECONDS, data={
        "game_id": game_id, "chat_id": query.message.chat_id
    })

    keyboard = [
        [InlineKeyboardButton("✅ JOIN", callback_data=f"join|{game_id}")],
    ]
    await query.edit_message_text(
        f"Mode: {mode.upper()}\nHost: {query.from_user.first_name}\nPlayers: 1\n\n"
        f"Tap JOIN to enter. Host must /startgame within 2 minutes.",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )


async def join_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    game_id = query.data.split("|")[1]

    with LOCK:
        game = GAMES.get(game_id)
        if not game or game.status != "waiting":
            await query.answer("Game not joinable", show_alert=True)
            return
        game.add_player(query.from_user.id, query.from_user.first_name)
        names = ", ".join(p["name"] for p in game.players.values())

    keyboard = [[InlineKeyboardButton("✅ JOIN", callback_data=f"join|{game_id}")]]
    await query.edit_message_text(
        f"Mode: {game.mode.upper()}\nHost: {game.players[game.host_id]['name']}\n"
        f"Players ({len(game.players)}): {names}\n\n"
        f"Tap JOIN to enter. Host must /startgame within 2 minutes.",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )


async def auto_cancel_job(context: ContextTypes.DEFAULT_TYPE):
    data = context.job.data
    game_id = data["game_id"]
    with LOCK:
        game = GAMES.get(game_id)
        if not game or game.status != "waiting":
            return
        game.status = "cancelled"
    await context.bot.send_message(
        data["chat_id"],
        "⏱️ Game was not started within 2 minutes. Cancelled."
    )


async def startgame_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.message.chat.type == "private":
        await update.message.reply_text("Use this in a group!")
        return

    with LOCK:
        game = None
        for g in GAMES.values():
            if g.chat_id == update.message.chat_id and g.status == "waiting":
                game = g
                break
        if not game:
            await update.message.reply_text("No waiting game found. Use /creategame first.")
            return
        if update.message.from_user.id != game.host_id:
            await update.message.reply_text("Only the host can start the game!")
            return
        ok, msg = game.start()

    if not ok:
        await update.message.reply_text(f"❌ {msg}")
        return

    url = f"{WEBAPP_URL}?game_id={game.game_id}&player_id={update.message.from_user.id}&backend={BACKEND_URL}"
    keyboard = [[InlineKeyboardButton("🎮 OPEN TABLE", url=url)]]
    await update.message.reply_text(
        "🎴 GAME STARTED!\nTap OPEN TABLE — everyone taps the same link, "
        "the page shows each person their own cards once they open it.\n"
        "Each turn has 50 seconds!",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )


async def endgame_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.message.chat.type == "private":
        await update.message.reply_text("Use this in a group!")
        return

    with LOCK:
        game = None
        for g in GAMES.values():
            if g.chat_id == update.message.chat_id and g.status in ("waiting", "playing"):
                game = g
                break
        if not game:
            await update.message.reply_text("No active game found.")
            return
        if update.message.from_user.id != game.host_id:
            await update.message.reply_text("Only the host can end the game!")
            return
        game.status = "cancelled"

    await update.message.reply_text("🛑 Game ended by host.")


async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "/creategame - create a new game\n"
        "/startgame - host starts the game (within 2 min of creation)\n"
        "/endgame - host ends the game early"
    )


# ---------------- HTTP API for the WebApp ----------------

class ApiHandler(BaseHTTPRequestHandler):
    def _cors(self):
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")

    def do_OPTIONS(self):
        self.send_response(200)
        self._cors()
        self.end_headers()

    def do_GET(self):
        parsed = urlparse(self.path)
        if parsed.path == "/api/state":
            qs = parse_qs(parsed.query)
            game_id = qs.get("game_id", [""])[0]
            player_id = qs.get("player_id", [""])[0]
            with LOCK:
                game = GAMES.get(game_id)
                if not game:
                    body = json.dumps({"error": "not_found"}).encode()
                    self.send_response(404)
                else:
                    try:
                        pid = int(player_id)
                    except ValueError:
                        pid = None
                    body = json.dumps(game.to_dict(pid)).encode()
                    self.send_response(200)
            self._cors()
            self.send_header("Content-type", "application/json")
            self.end_headers()
            self.wfile.write(body)
        else:
            self.send_response(200)
            self._cors()
            self.send_header("Content-type", "text/plain")
            self.end_headers()
            self.wfile.write(b"Bluff Master bot is running!")

    def do_POST(self):
        parsed = urlparse(self.path)
        if parsed.path == "/api/play":
            length = int(self.headers.get("Content-Length", 0))
            raw = self.rfile.read(length)
            try:
                data = json.loads(raw)
                game_id = data.get("game_id")
                player_id = int(data.get("player_id"))
                card = data.get("card")
                with LOCK:
                    game = GAMES.get(game_id)
                    if not game:
                        result = {"success": False, "message": "not_found"}
                    else:
                        ok, msg = game.play_card(player_id, card)
                        result = {"success": ok, "message": msg, "state": game.to_dict(player_id)}
            except Exception as e:
                result = {"success": False, "message": str(e)}
            body = json.dumps(result).encode()
            self.send_response(200)
            self._cors()
            self.send_header("Content-type", "application/json")
            self.end_headers()
            self.wfile.write(body)
        else:
            self.send_response(404)
            self._cors()
            self.end_headers()

    def log_message(self, format, *args):
        pass


def run_api_server():
    port = int(os.getenv("PORT", 10000))
    server = HTTPServer(("0.0.0.0", port), ApiHandler)
    server.serve_forever()


def main():
    threading.Thread(target=run_api_server, daemon=True).start()

    app = Application.builder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start_cmd))
    app.add_handler(CommandHandler("creategame", creategame_cmd))
    app.add_handler(CommandHandler("create", creategame_cmd))
    app.add_handler(CommandHandler("startgame", startgame_cmd))
    app.add_handler(CommandHandler("endgame", endgame_cmd))
    app.add_handler(CommandHandler("help", help_cmd))
    app.add_handler(CallbackQueryHandler(mode_callback, pattern="^mode\\|"))
    app.add_handler(CallbackQueryHandler(join_callback, pattern="^join\\|"))

    logger.info("Bot starting...")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
