# bot.py
import os
import json
import asyncio
from datetime import datetime, timedelta

from aiohttp import web
from aiogram import Bot, Dispatcher, types
from aiogram.types import ParseMode, InlineKeyboardMarkup, InlineKeyboardButton

import stripe

# ---------- Конфиг ----------
API_TOKEN = os.getenv("API_TOKEN")
WEBHOOK_HOST = os.getenv("WEBHOOK_HOST")                 # напр.: https://your-domain.com
CHANNEL_ID = int(os.getenv("CHANNEL_ID", "0"))
ADMIN_ID = int(os.getenv("ADMIN_ID", "1279721354"))

STRIPE_SECRET_KEY = os.getenv("STRIPE_SECRET_KEY")
STRIPE_WEBHOOK_SECRET = os.getenv("STRIPE_WEBHOOK_SECRET")
stripe.api_key = STRIPE_SECRET_KEY

WEBHOOK_PATH = f"/webhook/{API_TOKEN}"                   # путь TG вебхука
STRIPE_WEBHOOK_PATH = "/webhook/stripe"                  # путь Stripe вебхука
WEBHOOK_URL = f"{WEBHOOK_HOST}{WEBHOOK_PATH}"

WEBAPP_HOST = "0.0.0.0"
WEBAPP_PORT = int(os.getenv("PORT", "8000"))

DB_FILE = "/data/subscriptions.json"

# ---------- Бот / диспетчер ----------
bot = Bot(token=API_TOKEN, parse_mode=ParseMode.HTML)
dp = Dispatcher(bot)

# ---------- Хранилище подписок ----------
def _ensure_data_dir():
    d = os.path.dirname(DB_FILE)
    if d and not os.path.exists(d):
        os.makedirs(d, exist_ok=True)

def load_subscriptions():
    _ensure_data_dir()
    if os.path.exists(DB_FILE):
        try:
            with open(DB_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return {}
    return {}

def save_subscriptions(data: dict):
    _ensure_data_dir()
    with open(DB_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False)

subscriptions = load_subscriptions()

# ---------- Stripe: создание checkout session ----------
async def create_checkout_session(user_id: int):
    try:
        session = stripe.checkout.Session.create(
            payment_method_types=["card"],
            line_items=[{
                "price_data": {
                    "currency": "pln",
                    "product_data": {"name": "Dostęp do kanału VIP"},
                    "unit_amount": 500,  # 5 PLN
                },
                "quantity": 1,
            }],
            mode="payment",
            success_url="https://t.me/TwojBot?start=success",
            cancel_url="https://t.me/TwojBot?start=cancel",
            metadata={"user_id": str(user_id)},
        )
        return session.url
    except Exception as e:
        print(f"[Stripe] create_checkout_session error: {e}")
        return None

# ---------- Хэндлеры команд ----------
@dp.message_handler(commands=["start"])
async def cmd_start(message: types.Message):
    keyboard = InlineKeyboardMarkup(row_width=1).add(
        InlineKeyboardButton("📞 Kontakt z administratorem", url="https://t.me/wawaadmin"),
        InlineKeyboardButton("💳 Link do płatności", callback_data="pay")
    )
    await message.answer("👋 Cześć! Kliknij przycisk poniżej, aby uzyskać dostęp:", reply_markup=keyboard)

# ---------- Callback: оплата ----------
@dp.callback_query_handler(lambda c: c.data == "pay")
async def handle_payment(callback: types.CallbackQuery):
    user_id = callback.from_user.id
    payment_url = await create_checkout_session(user_id)
    if payment_url:
        await callback.message.answer(
            "💳 Kliknij poniżej, aby przejść do płatności:",
            reply_markup=InlineKeyboardMarkup().add(
                InlineKeyboardButton("🔗 Zapłać teraz", url=payment_url)
            )
        )
    else:
        await callback.message.answer("❌ Błąd podczas generowania linku do płatności.")
    await callback.answer()

# ---------- Stripe webhook ----------
async def stripe_webhook(request: web.Request):
    payload = await request.read()
    sig_header = request.headers.get("Stripe-Signature")

    try:
        event = stripe.Webhook.construct_event(payload, sig_header, STRIPE_WEBHOOK_SECRET)
    except stripe.error.SignatureVerificationError:
        return web.Response(status=400)

    if event.get("type") == "checkout.session.completed":
        session = event["data"]["object"]
        user_id = session.get("metadata", {}).get("user_id")

        if user_id:
            end_date = (datetime.now() + timedelta(days=30)).strftime("%Y-%m-%d")
            subscriptions[user_id] = end_date
            save_subscriptions(subscriptions)

            try:
                invite = await bot.create_chat_invite_link(
                    chat_id=CHANNEL_ID,
                    expire_date=int((datetime.now() + timedelta(days=1)).timestamp()),
                    member_limit=1
                )
                kb = InlineKeyboardMarkup().add(
                    InlineKeyboardButton("🔗 Dołącz do kanału", url=invite.invite_link)
                )
                await bot.send_message(int(user_id), "✅ Płatność potwierdzona! Kliknij poniżej, aby dołączyć do kanału:", reply_markup=kb)
            except Exception as e:
                await bot.send_message(ADMIN_ID, f"⚠️ Błąd przy wysyłaniu linku użytkownikowi {user_id}:\n<code>{e}</code>")

    return web.Response(status=200)

# ---------- Telegram webhook (aiohttp маршрут) ----------
async def telegram_webhook(request: web.Request):
    try:
        data = await request.json()
    except Exception:
        return web.Response(status=400)
    update = types.Update.to_object(data)
    await dp.process_updates([update])
    return web.Response(status=200)

# ---------- Периодическая проверка окончаний подписок ----------
async def check_expired():
    already_notified = set()
    while True:
        now = datetime.now().date()
        to_remove = []

        for user_id, end_str in list(subscriptions.items()):
            try:
                end_date = datetime.strptime(end_str, "%Y-%m-%d").date()

                # Напоминание за день
                if end_date == now + timedelta(days=1) and user_id not in already_notified:
                    await bot.send_message(int(user_id), "⏳ Twoja subskrypcja kończy się jutro!")
                    already_notified.add(user_id)

                # Истёк срок — удаляем с канала
                elif end_date <= now:
                    try:
                        await bot.send_message(int(user_id), "❌ Twoja subskrypcja wygasła. Zostałeś usunięty z kanału.")
                    except Exception:
                        pass
                    try:
                        # кик + мгновенный unban, чтобы можно было снова зайти по новой оплате
                        await bot.kick_chat_member(CHANNEL_ID, int(user_id))
                        await asyncio.sleep(1)
                        await bot.unban_chat_member(CHANNEL_ID, int(user_id))
                    except Exception as e:
                        await bot.send_message(ADMIN_ID, f"⚠️ Błąd przy usuwaniu {user_id}:\n<code>{e}</code>")
                    to_remove.append(user_id)

            except Exception as e:
                await bot.send_message(ADMIN_ID, f"⚠️ Błąd przy przetwarzaniu {user_id}:\n<code>{e}</code>")

        for uid in to_remove:
            subscriptions.pop(uid, None)

        save_subscriptions(subscriptions)
        await asyncio.sleep(86400)
        already_notified.clear()

# ---------- Хуки запуска/остановки ----------
async def on_startup_app(app: web.Application):
    # ставим вебхук TG и запускаем фоновую задачу проверки подписок
    await bot.set_webhook(WEBHOOK_URL)
    asyncio.create_task(check_expired())

async def on_shutdown_app(app: web.Application):
    await bot.delete_webhook()

# ---------- Точка входа ----------
if __name__ == "__main__":
    # Приложение aiohttp с уже настроенным обработчиком Telegram вебхука
    app = get_new_configured_app(dispatcher=dp, path=WEBHOOK_PATH)

    # Stripe вебхук
    app.router.add_post(STRIPE_WEBHOOK_PATH, stripe_webhook)

    # health-check (по желанию)
    async def health(request):
        return web.Response(text="OK")
    app.router.add_get("/health", health)

    # Подписываемся на события старта/остановки
    app.on_startup.append(on_startup_app)
    app.on_shutdown.append(on_shutdown_app)

    # Запуск
    web.run_app(app, host=WEBAPP_HOST, port=WEBAPP_PORT)