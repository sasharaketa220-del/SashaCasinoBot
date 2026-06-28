import json
import logging
import os
import random
import re
import sqlite3
import time
from datetime import datetime, timedelta, date

from telegram import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    ReplyKeyboardMarkup,
    Update,
)
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

BOT_TOKEN = os.environ.get("BOT_TOKEN", "")
ADMIN_IDS = {7507020081}
START_BALANCE = 50000
COIN_FLIP_COOLDOWN_MIN = 5
COIN_FLIP_REWARD = 50
MAX_REWARDS = 5
CURRENCY = "🏆"
DB_PATH = "casino.db"
FREE_CLAIM_BUTTONS = {
    "Отримати 50000🏆": ("claim_50000", 50000),
    "Отримати 500000🏆": ("claim_500000", 500000),
}
logging.basicConfig(format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO)
log = logging.getLogger(__name__)

def db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    with db() as conn:
        conn.execute("""CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY, username TEXT, full_name TEXT,
            balance INTEGER NOT NULL DEFAULT 0, wins INTEGER NOT NULL DEFAULT 0,
            losses INTEGER NOT NULL DEFAULT 0, total_deposited INTEGER NOT NULL DEFAULT 0,
            message_count INTEGER NOT NULL DEFAULT 0, streak INTEGER NOT NULL DEFAULT 0,
            last_active_date TEXT DEFAULT '', rewards TEXT DEFAULT '[]',
            last_coin_flip REAL DEFAULT 0, last_casino REAL DEFAULT 0,
            last_claim_50000 REAL DEFAULT 0, last_claim_500000 REAL DEFAULT 0)""")
        conn.commit()

def get_user(user_id):
    with db() as conn:
        return conn.execute("SELECT * FROM users WHERE user_id=?", (user_id,)).fetchone()

def ensure_user(user_id, username, full_name):
    with db() as conn:
        row = conn.execute("SELECT user_id FROM users WHERE user_id=?", (user_id,)).fetchone()
        if row is None:
            conn.execute("INSERT INTO users (user_id, username, full_name, balance, total_deposited) VALUES (?,?,?,?,?)",
                (user_id, username, full_name, START_BALANCE, 0))
        else:
            conn.execute("UPDATE users SET username=?, full_name=? WHERE user_id=?", (username, full_name, user_id))
        conn.commit()

def change_balance(user_id, delta):
    with db() as conn:
        conn.execute("UPDATE users SET balance = balance + ? WHERE user_id=?", (delta, user_id))
        conn.commit()
        return conn.execute("SELECT balance FROM users WHERE user_id=?", (user_id,)).fetchone()["balance"]

def record_game_result(user_id, win, stake, profit):
    with db() as conn:
        if win:
            conn.execute("UPDATE users SET wins=wins+1, balance=balance+?, total_deposited=total_deposited+? WHERE user_id=?", (profit, stake, user_id))
        else:
            conn.execute("UPDATE users SET losses=losses+1, balance=balance-?, total_deposited=total_deposited+? WHERE user_id=?", (stake, stake, user_id))
        conn.commit()
        return conn.execute("SELECT balance FROM users WHERE user_id=?", (user_id,)).fetchone()["balance"]

def set_cooldown(user_id, column, ts):
    with db() as conn:
        conn.execute(f"UPDATE users SET {column}=? WHERE user_id=?", (ts, user_id))
        conn.commit()

def bump_message_and_streak(user_id):
    today = date.today().isoformat()
    with db() as conn:
        row = conn.execute("SELECT last_active_date, streak FROM users WHERE user_id=?", (user_id,)).fetchone()
        if row is None:
            return
        if row["last_active_date"] == today:
            conn.execute("UPDATE users SET message_count=message_count+1 WHERE user_id=?", (user_id,))
        else:
            yesterday = (date.today() - timedelta(days=1)).isoformat()
            streak = row["streak"] + 1 if row["last_active_date"] == yesterday else 1
            conn.execute("UPDATE users SET message_count=message_count+1, streak=?, last_active_date=? WHERE user_id=?", (streak, today, user_id))
        conn.commit()

def get_rank(user_id):
    with db() as conn:
        rows = conn.execute("SELECT user_id FROM users ORDER BY wins DESC").fetchall()
        for i, r in enumerate(rows, start=1):
            if r["user_id"] == user_id:
                return i
    return 0

def get_top(limit=10):
    with db() as conn:
        return conn.execute("SELECT * FROM users ORDER BY wins DESC LIMIT ?", (limit,)).fetchall()

def get_rewards(user_id):
    row = get_user(user_id)
    if row is None:
        return []
    try:
        return json.loads(row["rewards"] or "[]")
    except:
        return []

def set_rewards(user_id, rewards):
    with db() as conn:
        conn.execute("UPDATE users SET rewards=? WHERE user_id=?", (json.dumps(rewards), user_id))
        conn.commit()

def fmt_num(n):
    return f"{n:,}".replace(",", " ")

def display_name(full_name):
    return f"❂ {full_name} ❂"

def is_admin(user_id):
    return user_id in ADMIN_IDS

def casino_card_text(balance):
    return (f"🎰 <b>КАЗИНО</b>\n━━━━━━━━━━━━━━━\n💰 Баланс: <b>{fmt_num(balance)}</b> 🏆\n\n"
            f"🎲 <code>казино &lt;ставка&gt;</code> — 50/50, виграш +50% від ставки\n"
            f"🪙 <code>монетка</code> — безкоштовно кожні {COIN_FLIP_COOLDOWN_MIN} хв, орел або решка")

MAIN_MENU = ReplyKeyboardMarkup(
    [[KeyboardButton("🎰 Казино")],
     [KeyboardButton("Отримати 50000🏆"), KeyboardButton("Отримати 500000🏆")],
     [KeyboardButton("🏆 Топ"), KeyboardButton("👤 Профіль")]],
    resize_keyboard=True, is_persistent=True)

def admin_card_keyboard(target_id):
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("Видати 50000🏆", callback_data=f"give:{target_id}:50000"),
         InlineKeyboardButton("Видати 100000🏆", callback_data=f"give:{target_id}:100000")],
        [InlineKeyboardButton("✏️ Своя сума", callback_data=f"give_custom:{target_id}")]])

GIVE_RE = re.compile(r"^Дати\s+(\d+)\s+(\d+)$", re.IGNORECASE)
TAKE_RE = re.compile(r"^Забрати\s+(\d+)\s+(\d+)$", re.IGNORECASE)
AWARD_RE = re.compile(r"^Нагородити\s+(\d+)\s+(.+)$", re.IGNORECASE)
UNAWARD_RE = re.compile(r"^Забрати\s+нагороду\s+(\d+)\s+(\d+)$", re.IGNORECASE)
MIN_ADMIN_AMOUNT = 1
MAX_ADMIN_AMOUNT = 1_000_000_000_000

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    ensure_user(user.id, user.username or "", user.full_name)
    await update.message.reply_text("Привіт! Скористайся меню нижче 👇", reply_markup=MAIN_MENU)

async def new_member(update: Update, context: ContextTypes.DEFAULT_TYPE):
    for member in update.message.new_chat_members:
        if member.is_bot:
            continue
        existing = get_user(member.id)
        ensure_user(member.id, member.username or "", member.full_name)
        if existing is None:
            await update.message.reply_text(f"Вітаю у нас є бот SashaCasinoBot\nТому тримай 50000🏆", reply_markup=MAIN_MENU)

async def open_casino_card(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    ensure_user(user.id, user.username or "", user.full_name)
    row = get_user(user.id)
    await update.message.reply_text(casino_card_text(row["balance"]), parse_mode="HTML", reply_markup=MAIN_MENU)

async def casino_bet(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    text = update.message.text.strip()
    match = re.match(r"^казино\s+(\d+)$", text, re.IGNORECASE)
    if not match:
        return
    stake = int(match.group(1))
    ensure_user(user.id, user.username or "", user.full_name)
    row = get_user(user.id)
    if stake <= 0:
        await update.message.reply_text("Ставка має бути більшою за 0.", reply_markup=MAIN_MENU)
        return
    if stake > row["balance"]:
        await update.message.reply_text(f"❌ Недостатньо трофеїв.\nУ тебе: {fmt_num(row['balance'])} 🏆", reply_markup=MAIN_MENU)
        return
    win = random.random() < 0.5
    if win:
        profit = stake // 2
        new_balance = record_game_result(user.id, True, stake, profit)
        await update.message.reply_text(f"🎰 ВИГРАШ!\n━━━━━━━━━━━━━━━\nСтавка: {fmt_num(stake)} 🏆\nПрибуток: +{fmt_num(profit)} 🏆\n💰 Новий баланс: {fmt_num(new_balance)} 🏆\n🍀 Удача на твоєму боці!", reply_markup=MAIN_MENU)
    else:
        new_balance = record_game_result(user.id, False, stake, 0)
        await update.message.reply_text(f"🎰 ПРОГРАШ!\n━━━━━━━━━━━━━━━\nСтавка: {fmt_num(stake)} 🏆\nЗбиток: -{fmt_num(stake)} 🏆\n💰 Новий баланс: {fmt_num(new_balance)} 🏆\n😔 Спробуй ще раз!", reply_markup=MAIN_MENU)

async def coin_flip(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    ensure_user(user.id, user.username or "", user.full_name)
    row = get_user(user.id)
    now = time.time()
    elapsed = now - (row["last_coin_flip"] or 0)
    cooldown = COIN_FLIP_COOLDOWN_MIN * 60
    if elapsed < cooldown:
        remaining = int(cooldown - elapsed)
        mins, secs = divmod(remaining, 60)
        await update.message.reply_text(f"⏳ Монетка ще на перезарядці: {mins} хв {secs} с.", reply_markup=MAIN_MENU)
        return
    set_cooldown(user.id, "last_coin_flip", now)
    side = random.choice(["Орел", "Решка"])
    new_balance = change_balance(user.id, COIN_FLIP_REWARD)
    await update.message.reply_text(f"🪙 Підкидаємо монетку... {side}!\n+{COIN_FLIP_REWARD} 🏆\n💰 Баланс: {fmt_num(new_balance)} 🏆", reply_markup=MAIN_MENU)

async def claim_free_bonus(update: Update, context: ContextTypes.DEFAULT_TYPE, column: str, amount: int):
    user = update.effective_user
    ensure_user(user.id, user.username or "", user.full_name)
    new_balance = change_balance(user.id, amount)
    await update.message.reply_text(f"🏆 +{fmt_num(amount)} 🏆!\n💰 Баланс: {fmt_num(new_balance)} 🏆", reply_markup=MAIN_MENU)

async def show_top(update: Update, context: ContextTypes.DEFAULT_TYPE):
    rows = get_top(10)
    medals = ["🥇", "🥈", "🥉"]
    lines = ["🏆 <b>ТОП-10 ГРАВЦІВ ЗА ВИГРАШАМИ</b>\n"]
    for i, r in enumerate(rows):
        marker = medals[i] if i < 3 else f"{i+1}."
        lines.append(f"{marker} {r['full_name']}\n• Перемог: {r['wins']}\n• Поразок: {r['losses']}\n• Депнуто: {fmt_num(r['total_deposited'])} 🏆\n")
    await update.message.reply_text("\n".join(lines), parse_mode="HTML", reply_markup=MAIN_MENU)

async def show_profile(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    ensure_user(user.id, user.username or "", user.full_name)
    row = get_user(user.id)
    rank = get_rank(user.id)
    rewards = get_rewards(user.id)
    role = "Адмін" if is_admin(user.id) else "Учасник"
    rewards_block = "\n".join(f"🎖 {r}" for r in rewards) if rewards else "Немає нагород 🎖"
    text = (f"👤 Ім'я: {row['full_name']}\n🆔 ID: {row['user_id']}\n🎭 Роль: {role}\n──────────────\n"
            f"🏆 Трофеїв: {fmt_num(row['balance'])}\n🥇 Місце в топі: #{rank}\n"
            f"✉️ Повідомлень: {row['message_count']}\n🔥 Стрік: {row['streak']} дн.\n──────────────\n🎁 Нагороди:\n{rewards_block}")
    try:
        photos = await context.bot.get_user_profile_photos(user.id, limit=1)
        if photos and photos.photos:
            await update.message.reply_photo(photo=photos.photos[0][-1].file_id, caption=text, reply_markup=MAIN_MENU)
            return
    except Exception:
        pass
    await update.message.reply_text(text, reply_markup=MAIN_MENU)

async def admin_give(update, target_id, amount):
    target = get_user(target_id)
    if target is None:
        await update.message.reply_text("Гравця не знайдено.")
        return
    if not (MIN_ADMIN_AMOUNT <= amount <= MAX_ADMIN_AMOUNT):
        await update.message.reply_text(f"Сума від {MIN_ADMIN_AMOUNT} до {fmt_num(MAX_ADMIN_AMOUNT)}.")
        return
    new_balance = change_balance(target_id, amount)
    await update.message.reply_text(f"⚡️ <b>БАЛАНС ОНОВЛЕНО!</b>\n━━━━━━━━━━━━━━━\n\n👤 Гравець: {target['full_name']} ({target_id})\n🏆 Видано: {fmt_num(amount)} трофеїв", parse_mode="HTML")
    try:
        await update.get_bot().send_message(chat_id=target_id, text=f"🏆 Тобі нараховано {fmt_num(amount)} 🏆!\n💰 Новий баланс: {fmt_num(new_balance)} 🏆")
    except Exception:
        pass

async def admin_take(update, target_id, amount):
    target = get_user(target_id)
    if target is None:
        await update.message.reply_text("Гравця не знайдено.")
        return
    new_balance = change_balance(target_id, -amount)
    await update.message.reply_text(f"⚡️ <b>БАЛАНС ОНОВЛЕНО!</b>\n━━━━━━━━━━━━━━━\n\n👤 Гравець: {target['full_name']} ({target_id})\n🏆 Забрано: {fmt_num(amount)} трофеїв", parse_mode="HTML")
    try:
        await update.get_bot().send_message(chat_id=target_id, text=f"⚠️ З твого балансу знято {fmt_num(amount)} 🏆.\n💰 Новий баланс: {fmt_num(new_balance)} 🏆")
    except Exception:
        pass

async def admin_award(update, target_id, reward_name):
    target = get_user(target_id)
    if target is None:
        await update.message.reply_text("Гравця не знайдено.")
        return
    rewards = get_rewards(target_id)
    if len(rewards) >= MAX_REWARDS:
        await update.message.reply_text(f"У гравця вже максимум нагород ({MAX_REWARDS}).")
        return
    rewards.append(reward_name)
    set_rewards(target_id, rewards)
    await update.message.reply_text(f"🎖 <b>НАГОРОДУ ВИДАНО!</b>\n━━━━━━━━━━━━━━━\n\n👤 Гравець: {target['full_name']} ({target_id})\n🎖 Нагорода: {reward_name}", parse_mode="HTML")
    try:
        await update.get_bot().send_message(chat_id=target_id, text=f"🎖 Тобі видано нагороду: «{reward_name}»!")
    except Exception:
        pass

async def admin_unaward(update, target_id, index):
    target = get_user(target_id)
    if target is None:
        await update.message.reply_text("Гравця не знайдено.")
        return
    rewards = get_rewards(target_id)
    if index < 1 or index > len(rewards):
        await update.message.reply_text(f"Немає нагороди під номером {index}.")
        return
    removed = rewards.pop(index - 1)
    set_rewards(target_id, rewards)
    await update.message.reply_text(f"🎖 <b>НАГОРОДУ ЗАБРАНО</b>\n━━━━━━━━━━━━━━━\n\n👤 Гравець: {target['full_name']} ({target_id})\n🎖 Видалено: {removed}", parse_mode="HTML")

async def find_player(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not is_admin(user.id):
        return
    if not context.args:
        await update.message.reply_text("Використання: /find <user_id>")
        return
    try:
        target_id = int(context.args[0])
    except ValueError:
        await update.message.reply_text("ID має бути числом.")
        return
    target = get_user(target_id)
    if target is None:
        await update.message.reply_text("Гравця не знайдено.")
        return
    await update.message.reply_text(
        f"👤 <b>Картка гравця</b>\n━━━━━━━━━━━━━━━\nГравець: {display_name(target['full_name'])} ({target['user_id']})\n💰 Баланс: {fmt_num(target['balance'])} 🏆",
        parse_mode="HTML", reply_markup=admin_card_keyboard(target_id))

async def admin_give_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if not is_admin(query.from_user.id):
        await query.answer("Немає доступу.", show_alert=True)
        return
    data = query.data
    if data.startswith("give_custom:"):
        target_id = int(data.split(":")[1])
        context.user_data["awaiting_custom_give_for"] = target_id
        await query.answer()
        await query.message.reply_text("Введи суму наступним повідомленням.")
        return
    _, target_id_str, amount_str = data.split(":")
    target_id, amount = int(target_id_str), int(amount_str)
    target = get_user(target_id)
    if target is None:
        await query.answer("Гравця не знайдено.", show_alert=True)
        return
    new_balance = change_balance(target_id, amount)
    await query.answer("Видано!")
    await query.message.reply_text(f"⚡️ <b>БАЛАНС ОНОВЛЕНО!</b>\n━━━━━━━━━━━━━━━\n\n👤 Гравець: {display_name(target['full_name'])} ({target_id})\n🏆 Видано: {fmt_num(amount)} трофеїв", parse_mode="HTML")
    try:
        await context.bot.send_message(chat_id=target_id, text=f"🏆 Тобі нараховано {fmt_num(amount)} 🏆!\n💰 Новий баланс: {fmt_num(new_balance)} 🏆")
    except Exception:
        pass

async def custom_give_amount(update: Update, context: ContextTypes.DEFAULT_TYPE):
    admin = update.effective_user
    target_id = context.user_data.get("awaiting_custom_give_for")
    if target_id is None or not is_admin(admin.id):
        return
    text = update.message.text.strip()
    if not text.isdigit():
        await update.message.reply_text("Потрібно ввести ціле число.")
        return
    amount = int(text)
    context.user_data.pop("awaiting_custom_give_for", None)
    target = get_user(target_id)
    if target is None:
        await update.message.reply_text("Гравця не знайдено.")
        return
    new_balance = change_balance(target_id, amount)
    await update.message.reply_text(f"⚡️ <b>БАЛАНС ОНОВЛЕНО!</b>\n━━━━━━━━━━━━━━━\n\n👤 Гравець: {display_name(target['full_name'])} ({target_id})\n🏆 Видано: {fmt_num(amount)} трофеїв", parse_mode="HTML")
    try:
        await context.bot.send_message(chat_id=target_id, text=f"🏆 Тобі нараховано {fmt_num(amount)} 🏆!\n💰 Новий баланс: {fmt_num(new_balance)} 🏆")
    except Exception:
        pass

async def text_router(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    text = update.message.text.strip()
    ensure_user(user.id, user.username or "", user.full_name)
    bump_message_and_streak(user.id)
    if is_admin(user.id):
        if context.user_data.get("awaiting_custom_give_for") is not None:
            await custom_give_amount(update, context)
            return
        m = UNAWARD_RE.match(text)
        if m:
            await admin_unaward(update, int(m.group(1)), int(m.group(2)))
            return
        m = AWARD_RE.match(text)
        if m:
            await admin_award(update, int(m.group(1)), m.group(2).strip())
            return
        m = GIVE_RE.match(text)
        if m:
            await admin_give(update, int(m.group(1)), int(m.group(2)))
            return
        m = TAKE_RE.match(text)
        if m:
            await admin_take(update, int(m.group(1)), int(m.group(2)))
            return
    if text == "🎰 Казино":
        await open_casino_card(update, context)
        return
    if text == "🏆 Топ":
        await show_top(update, context)
        return
    if text == "👤 Профіль":
        await show_profile(update, context)
        return
    if text in FREE_CLAIM_BUTTONS:
        column, amount = FREE_CLAIM_BUTTONS[text]
        await claim_free_bonus(update, context, column, amount)
        return
    if re.match(r"^казино\s+\d+$", text, re.IGNORECASE):
        await casino_bet(update, context)
        return
    if text.lower() == "монетка":
        await coin_flip(update, context)
        return

def main():
    init_db()
    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("find", find_player))
    app.add_handler(MessageHandler(filters.StatusUpdate.NEW_CHAT_MEMBERS, new_member))
    app.add_handler(CallbackQueryHandler(admin_give_callback, pattern=r"^give"))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, text_router))
    log.info("Бот запущено.")
    app.run_polling()

if __name__ == "__main__":
    main()
