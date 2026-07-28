"""
Anagram Scramble — Telegram bot
--------------------------------
Starts a timed anagram round in any chat. Everyone in the chat taps the
"Play" button to open the Web App (webapp/index.html), races to build
words from the same letter set, and the bot posts a leaderboard when
the clock runs out.

Group chats can only open a Mini App via a Direct Link
(t.me/<bot>/<short_name>) or an inline URL button — and Telegram does
NOT allow those launch types to use WebApp.sendData() to report results
back (that only works for a private-chat keyboard button). So instead,
the webapp calls this bot's own HTTP endpoint (/submit) directly when a
round ends, authenticated using Telegram's initData signature. That
means this bot needs a public HTTPS URL to run in a group — see
README.md for deploying it to Railway/Render.

Setup: see README.md in the project root.
"""

import asyncio
import hashlib
import hmac
import json
import logging
import os
import random
import time
import uuid
from dataclasses import dataclass, field
from urllib.parse import parse_qsl

from aiohttp import web
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
)

logging.basicConfig(
    format="%(asctime)s %(levelname)s %(name)s: %(message)s", level=logging.INFO
)
log = logging.getLogger("anagram-bot")

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
WEBAPP_URL = os.environ.get("WEBAPP_URL")  # e.g. https://yourname.github.io/anagram-bot/
APP_SHORT_NAME = os.environ.get("APP_SHORT_NAME")  # short name from BotFather /newapp
ROUND_DURATION = int(os.environ.get("ROUND_DURATION", "60"))  # seconds
NUM_LETTERS = int(os.environ.get("NUM_LETTERS", "7"))
PORT = int(os.environ.get("PORT", "8080"))  # Railway/Render set this automatically

if not BOT_TOKEN:
    raise SystemExit("Set TELEGRAM_BOT_TOKEN in your environment before running the bot.")
if not WEBAPP_URL:
    raise SystemExit(
        "Set WEBAPP_URL to the HTTPS URL where you hosted webapp/index.html "
        "(Telegram requires HTTPS for Web Apps)."
    )
if not APP_SHORT_NAME:
    raise SystemExit(
        "Set APP_SHORT_NAME to the short name you gave your Mini App via "
        "BotFather's /newapp command. See README.md."
    )

# Scrabble-ish English letter distribution, used as a weighted bag.
LETTER_BAG = (
    "E" * 12 + "A" * 9 + "I" * 9 + "O" * 8 + "N" * 6 + "R" * 6 + "T" * 6
    + "L" * 4 + "S" * 4 + "U" * 4 + "D" * 4 + "G" * 3
    + "B" * 2 + "C" * 2 + "M" * 2 + "P" * 2 + "F" * 2 + "H" * 2
    + "V" * 2 + "W" * 2 + "Y" * 2 + "K" * 1 + "J" * 1 + "X" * 1 + "Q" * 1 + "Z" * 1
)
VOWELS = set("AEIOU")

DICTIONARY_PATH = os.path.join(os.path.dirname(__file__), "dictionary.json")
with open(DICTIONARY_PATH) as f:
    DICTIONARY = set(json.load(f))

POINTS = {3: 10, 4: 25, 5: 50, 6: 80, 7: 120}


def score_word(word: str) -> int:
    if len(word) in POINTS:
        return POINTS[len(word)]
    return 160 if len(word) > 7 else 0


def can_form(word: str, letters: list[str]) -> bool:
    pool = {}
    for c in letters:
        pool[c] = pool.get(c, 0) + 1
    for c in word:
        if pool.get(c, 0) <= 0:
            return False
        pool[c] -= 1
    return True


def has_enough_words(letters: list[str], minimum: int = 8) -> bool:
    """Rejection-sample check so a round isn't handed out with a dead letter set."""
    count = 0
    for w in DICTIONARY:
        if 3 <= len(w) <= len(letters) and can_form(w.upper(), letters):
            count += 1
            if count >= minimum:
                return True
    return False


def generate_letters(n: int = NUM_LETTERS) -> list[str]:
    for _ in range(200):
        bag = list(LETTER_BAG)
        random.shuffle(bag)
        picked = bag[:n]
        vowels = sum(1 for c in picked if c in VOWELS)
        consonants = n - vowels
        if vowels < 2 or consonants < 2:
            continue
        if has_enough_words(picked):
            return picked
    return list("ARTISEN")[:n]


# ---------------------------------------------------------------------------
# Telegram initData verification
# https://core.telegram.org/bots/webapps#validating-data-received-via-the-mini-app
# ---------------------------------------------------------------------------
def verify_init_data(init_data: str, max_age_seconds: int = 3600):
    """Returns the parsed user dict if init_data is a genuine, fresh payload
    signed by Telegram for this bot; raises ValueError otherwise. This is
    what stops anyone from just POSTing fake scores to /submit."""
    if not init_data:
        raise ValueError("missing init_data")

    pairs = parse_qsl(init_data, keep_blank_values=True)
    data = dict(pairs)
    received_hash = data.pop("hash", None)
    if not received_hash:
        raise ValueError("missing hash")

    data_check_string = "\n".join(f"{k}={v}" for k, v in sorted(data.items()))
    secret_key = hmac.new(b"WebAppData", BOT_TOKEN.encode(), hashlib.sha256).digest()
    computed_hash = hmac.new(
        secret_key, data_check_string.encode(), hashlib.sha256
    ).hexdigest()

    if not hmac.compare_digest(computed_hash, received_hash):
        raise ValueError("bad signature")

    auth_date = int(data.get("auth_date", "0"))
    if time.time() - auth_date > max_age_seconds:
        raise ValueError("stale init_data")

    user = json.loads(data.get("user", "{}"))
    if "id" not in user:
        raise ValueError("no user in init_data")
    return user


# ---------------------------------------------------------------------------
# Round state (in-memory — fine for a single-process bot; swap for redis/db
# if you run more than one worker)
# ---------------------------------------------------------------------------
@dataclass
class PlayerResult:
    name: str
    words: list = field(default_factory=list)
    score: int = 0


@dataclass
class Round:
    round_id: str
    chat_id: int
    letters: list
    start_time: float
    duration: int
    message_id: int = None
    players: dict = field(default_factory=dict)  # user_id -> PlayerResult
    finished: bool = False


ROUNDS: dict[str, Round] = {}


# ---------------------------------------------------------------------------
# Telegram command handlers
# ---------------------------------------------------------------------------
async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🧵 Anagram Scramble\n\n"
        "Send /anagram to start a round. Everyone in the chat gets the same "
        "scrambled letters and races to pin as many real words as they can "
        f"before the {ROUND_DURATION}s clock runs out. Highest score wins."
    )


async def anagram_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    letters = generate_letters()
    round_id = uuid.uuid4().hex[:10]

    rnd = Round(
        round_id=round_id,
        chat_id=chat_id,
        letters=letters,
        start_time=time.time(),
        duration=ROUND_DURATION,
    )
    ROUNDS[round_id] = rnd

    start_param = f"{round_id}-{''.join(letters)}-{ROUND_DURATION}-{int(rnd.start_time)}"
    bot_username = context.bot.username
    play_url = f"https://t.me/{bot_username}/{APP_SHORT_NAME}?startapp={start_param}"

    keyboard = InlineKeyboardMarkup(
        [[InlineKeyboardButton("🔤 Play Anagram", url=play_url)]]
    )

    msg = await update.message.reply_text(
        "🧵 *New round pinned to the board!*\n\n"
        f"Letters: `{'  '.join(letters)}`\n"
        f"Clock: {ROUND_DURATION}s per player, starting the moment you open it.\n\n"
        "Tap below to play — everyone can join.",
        parse_mode="Markdown",
        reply_markup=keyboard,
    )
    rnd.message_id = msg.message_id

    context.job_queue.run_once(
        post_leaderboard, ROUND_DURATION + 8, data=round_id, chat_id=chat_id
    )


async def post_leaderboard(context: ContextTypes.DEFAULT_TYPE):
    round_id = context.job.data
    rnd = ROUNDS.get(round_id)
    if rnd is None or rnd.finished:
        return
    rnd.finished = True

    if not rnd.players:
        await context.bot.send_message(
            rnd.chat_id,
            "🧵 Round closed — nobody pinned a word that time. Try /anagram again!",
        )
        return

    ranked = sorted(rnd.players.values(), key=lambda p: p.score, reverse=True)
    medals = ["🥇", "🥈", "🥉"]

    lines = ["🧵 *Round results*\n"]
    for i, p in enumerate(ranked):
        medal = medals[i] if i < len(medals) else f"{i + 1}."
        word_list = ", ".join(w for w, _ in sorted(p.words, key=lambda x: -x[1])[:6])
        more = f" (+{len(p.words) - 6} more)" if len(p.words) > 6 else ""
        lines.append(f"{medal} *{p.name}* — {p.score} pts\n   {word_list}{more}")

    lines.append("\nSend /anagram to run it back.")
    await context.bot.send_message(rnd.chat_id, "\n".join(lines), parse_mode="Markdown")

    context.job_queue.run_once(lambda ctx: ROUNDS.pop(round_id, None), 60)


# ---------------------------------------------------------------------------
# HTTP endpoint the webapp calls directly to report results
# ---------------------------------------------------------------------------
@web.middleware
async def cors_middleware(request, handler):
    if request.method == "OPTIONS":
        resp = web.Response()
    else:
        resp = await handler(request)
    resp.headers["Access-Control-Allow-Origin"] = "*"
    resp.headers["Access-Control-Allow-Methods"] = "POST, OPTIONS"
    resp.headers["Access-Control-Allow-Headers"] = "Content-Type"
    return resp


async def handle_submit(request: web.Request):
    try:
        body = await request.json()
    except json.JSONDecodeError:
        return web.json_response({"ok": False, "error": "bad json"}, status=400)

    round_id = body.get("round_id")
    client_words = [str(w).upper() for w in body.get("words", [])]
    init_data = body.get("init_data", "")

    try:
        user = verify_init_data(init_data)
    except ValueError as e:
        return web.json_response({"ok": False, "error": f"auth failed: {e}"}, status=401)

    rnd = ROUNDS.get(round_id)
    if rnd is None:
        return web.json_response({"ok": False, "error": "round already ended"}, status=404)

    verified_words = []
    total = 0
    seen = set()
    for w in client_words:
        wl = w.lower()
        if wl in seen:
            continue
        seen.add(wl)
        if len(w) < 3 or len(w) > len(rnd.letters):
            continue
        if not can_form(w, rnd.letters):
            continue
        if wl not in DICTIONARY:
            continue
        pts = score_word(w)
        verified_words.append((w, pts))
        total += pts

    rnd.players[user["id"]] = PlayerResult(
        name=user.get("first_name") or user.get("username") or "Player",
        words=verified_words,
        score=total,
    )

    return web.json_response(
        {"ok": True, "word_count": len(verified_words), "score": total}
    )


async def handle_health(request: web.Request):
    return web.json_response({"ok": True})


async def run_web_server():
    app = web.Application(middlewares=[cors_middleware])
    app.router.add_post("/submit", handle_submit)
    app.router.add_options("/submit", handle_submit)
    app.router.add_get("/health", handle_health)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", PORT)
    await site.start()
    log.info(f"HTTP submit endpoint listening on port {PORT}")


# ---------------------------------------------------------------------------
# Entrypoint — runs the Telegram poller and the HTTP server side by side
# ---------------------------------------------------------------------------
async def main():
    application = Application.builder().token(BOT_TOKEN).build()
    application.add_handler(CommandHandler("start", start_cmd))
    application.add_handler(CommandHandler("anagram", anagram_cmd))

    log.info("Anagram Scramble bot starting…")
    await application.initialize()
    await application.start()
    await application.updater.start_polling()

    await run_web_server()

    try:
        stop_event = asyncio.Event()
        await stop_event.wait()
    finally:
        await application.updater.stop()
        await application.stop()
        await application.shutdown()


if __name__ == "__main__":
    asyncio.run(main())
