from aiogram import Bot, Dispatcher, types
from aiogram.types import ParseMode, InlineKeyboardMarkup, InlineKeyboardButton, CallbackQuery
from aiogram.utils.executor import start_webhook
from datetime import datetime, timedelta
import asyncio
import json
import os

API_TOKEN = os.getenv("API_TOKEN")
CHANNEL_ID = int(os.getenv("CHANNEL_ID"))  # Przykład: -1001234567890
WEBHOOK_HOST = os.getenv("WEBHOOK_HOST")
ADMIN_ID = 1279721354

WEBHOOK_PATH = f"/webhook/{API_TOKEN}"
WEBHOOK_URL = f"{WEBHOOK_HOST}{WEBHOOK_PATH}"
WEBAPP_HOST = "0.0.0.0"
WEBAPP_PORT = int(os.getenv("PORT", default=8000))

bot = Bot(token=API_TOKEN, parse_mode=ParseMode.HTML)
dp = Dispatcher(bot)

DB_FILE = "/data/subscriptions.json"
pending_requests = {}

def load_subscriptions():
    if os.path.exists(DB_FILE):
        with open(DB_FILE, "r") as f:
            return json.load(f)
    return {}

def save_subscriptions(data):
    with open(DB_FILE, "w") as f:
        json.dump(data, f)

subscriptions = load_subscriptions()

# Обрабатываем команду /start и кнопку start
@dp.message_handler(commands=["start"])
@dp.message_handler(lambda message: message.text.lower() in ["start", "/start", "🚀 start"])
async def cmd_start(message: types.Message):
    keyboard = InlineKeyboardMarkup(row_width=1).add(
        InlineKeyboardButton("📞 Kontakt z administratorem", url="https://t.me/wawaadmin"),
        InlineKeyboardButton("💳 Link do płatności", url="https://buy.stripe.com/dRm14f633b0HagO74Rds403"),
        InlineKeyboardButton("✅ Zapłaciłem", callback_data="paid")
    )
    await message.answer("👋 Cześć! Wybierz jedną z opcji poniżej:", reply_markup=keyboard)

# Обработка кнопки "Zapłaciłem"
@dp.callback_query_handler(lambda c: c.data == "paid")
async def handle_paid(callback: CallbackQuery):
    user_id = str(callback.from_user.id)
    username = callback.from_user.username or "brak nicku"

    # Если запрос уже отправлен и ожидает ответа администратора
    if user_id in pending_requests:
        await callback.answer("❌ Twoja płatność nie została jeszcze potwierdzona. Skontaktuj się z administratorem.")
        return

    # Добавляем пользователя в список ожидания
    pending_requests[user_id] = username

    admin_keyboard = InlineKeyboardMarkup().add(
        InlineKeyboardButton("✅ Zatwierdź", callback_data=f"approve:{user_id}"),
        InlineKeyboardButton("❌ Odrzuć", callback_data=f"deny:{user_id}")
    )

    await bot.send_message(
        ADMIN_ID,
        f"🔔 Prośba o dostęp od @{username}\nID: <code>{user_id}</code>",
        reply_markup=admin_keyboard,
        parse_mode="HTML"
    )
    await callback.answer("Twoja prośba została wysłana do administratora. Poczekaj na zatwierdzenie.")

# Обработка действий администратора
@dp.callback_query_handler(lambda c: c.data.startswith("approve:") or c.data.startswith("deny:"))
async def handle_admin_action(callback: CallbackQuery):
    action, user_id = callback.data.split(":")
    username = pending_requests.get(user_id, "nieznany")

    if action == "approve":
        end_date = (datetime.now() + timedelta(days=30)).strftime("%Y-%m-%d")
        subscriptions[user_id] = end_date
        save_subscriptions(subscriptions)

        try:
            invite = await bot.create_chat_invite_link(
                chat_id=CHANNEL_ID,
                expire_date=int((datetime.now() + timedelta(days=1)).timestamp()),
                member_limit=1
            )

            await bot.send_message(int(user_id),
                "✅ Twoje konto zostało zatwierdzone! Kliknij przycisk poniżej, aby dołączyć do kanału:",
                reply_markup=InlineKeyboardMarkup().add(
                    InlineKeyboardButton("🔗 Dołącz do kanału", url=invite.invite_link)
                )
            )

            await bot.send_message(ADMIN_ID,
                f"✅ @{username} (ID: {user_id}) został dodany do {end_date}.")

        except Exception as e:
            await bot.send_message(ADMIN_ID, f"❗ Błąd przy tworzeniu linku: <code>{e}</code>", parse_mode="HTML")

    else:
        await bot.send_message(int(user_id), "❌ Twoja prośba została odrzucona.")
        await bot.send_message(ADMIN_ID, f"🚫 @{username} (ID: {user_id}) — odrzucony.")

    pending_requests.pop(user_id, None)
    await callback.answer()

# Проверка просроченных подписок
async def check_expired():
    already_notified = set()
    while True:
        now = datetime.now().date()
        to_remove = []

        for user_id, end_str in subscriptions.items():
            try:
                end_date = datetime.strptime(end_str, "%Y-%m-%d").date()

                if end_date == now + timedelta(days=1) and user_id not in already_notified:
                    await bot.send_message(int(user_id), "⏳ Twoja subskrypcja kończy się jutro!")
                    already_notified.add(user_id)

                elif end_date <= now:
                    await bot.send_message(int(user_id), "❌ Twoja subskrypcja wygasła. Zostałeś usunięty z kanału.")
                    await bot.kick_chat_member(CHANNEL_ID, int(user_id))
                    await asyncio.sleep(1)
                    await bot.unban_chat_member(CHANNEL_ID, int(user_id))
                    to_remove.append(user_id)

            except Exception as e:
                await bot.send_message(ADMIN_ID, f"⚠️ Błąd przy usuwaniu {user_id}:\n<code>{e}</code>", parse_mode="HTML")

        for user_id in to_remove:
            subscriptions.pop(user_id, None)

        save_subscriptions(subscriptions)
        await asyncio.sleep(86400)
        already_notified.clear()

async def on_startup(dp):
    await bot.set_webhook(WEBHOOK_URL)
    asyncio.create_task(check_expired())

async def on_shutdown(dp):
    await bot.delete_webhook()

if __name__ == "__main__":
    start_webhook(
        dispatcher=dp,
        webhook_path=WEBHOOK_PATH,
        on_startup=on_startup,
        on_shutdown=on_shutdown,
        skip_updates=True,
        host=WEBAPP_HOST,
        port=WEBAPP_PORT,
    )
