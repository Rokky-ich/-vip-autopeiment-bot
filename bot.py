from aiogram import Bot, Dispatcher, types
from aiogram.types import ParseMode, InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.utils.executor import start_webhook
from datetime import datetime, timedelta
from aiohttp import web
import asyncio
import json
import os
import stripe

API_TOKEN = os.getenv("API_TOKEN")
CHANNEL_ID = int(os.getenv("CHANNEL_ID"))
WEBHOOK_HOST = os.getenv("WEBHOOK_HOST")
ADMIN_ID = 1279721354

STRIPE_SECRET_KEY = os.getenv("STRIPE_SECRET_KEY")
STRIPE_WEBHOOK_SECRET = os.getenv("STRIPE_WEBHOOK_SECRET")
stripe.api_key = STRIPE_SECRET_KEY

WEBHOOK_PATH = f"/webhook/{API_TOKEN}"
STRIPE_WEBHOOK_PATH = "/webhook/stripe"
WEBHOOK_URL = f"{WEBHOOK_HOST}{WEBHOOK_PATH}"
WEBAPP_HOST = "0.0.0.0"
WEBAPP_PORT = int(os.getenv("PORT", default=8000))

bot = Bot(token=API_TOKEN, parse_mode=ParseMode.HTML)
dp = Dispatcher(bot)

DB_FILE = "/data/subscriptions.json"

def load_subscriptions():
    if os.path.exists(DB_FILE):
        with open(DB_FILE, "r") as f:
            return json.load(f)
    return {}

def save_subscriptions(data):
    with open(DB_FILE, "w") as f:
        json.dump(data, f)

subscriptions = load_subscriptions()

# Создание индивидуальной Stripe-сессии
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
            metadata={"user_id": str(user_id)}
        )
        return session.url
    except Exception as e:
        print(f"Stripe error: {e}")
        return None

# Команда /start
@dp.message_handler(commands=["start"])
async def cmd_start(message: types.Message):
    keyboard = InlineKeyboardMarkup(row_width=1).add(
        InlineKeyboardButton("📞 Kontakt z administratorem", url="https://t.me/wawaadmin"),
        InlineKeyboardButton("💳 Link do płatności", callback_data="pay")
    )
    await message.answer(
        "👋 Cześć! Kliknij przycisk poniżej, aby uzyskać dostęp:",
        reply_markup=keyboard
    )

# Обработка нажатия кнопки "Оплатить"
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

# Stripe Webhook — автоматическая проверка оплаты
async def stripe_webhook(request):
    payload = await request.read()
    sig_header = request.headers.get("Stripe-Signature")

    try:
        event = stripe.Webhook.construct_event(
            payload, sig_header, STRIPE_WEBHOOK_SECRET
        )
    except stripe.error.SignatureVerificationError:
        return web.Response(status=400)

    # Оплата завершена успешно
    if event["type"] == "checkout.session.completed":
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
                await bot.send_message(
                    int(user_id),
                    "✅ Płatność potwierdzona! Kliknij poniżej, aby dołączyć do kanału:",
                    reply_markup=InlineKeyboardMarkup().add(
                        InlineKeyboardButton("🔗 Dołącz do kanału", url=invite.invite_link)
                    )
                )
            except Exception as e:
                await bot.send_message(
                    ADMIN_ID,
                    f"⚠️ Błąd przy wysyłaniu linku użytkownikowi {user_id}:\n<code>{e}</code>",
                    parse_mode="HTML"
                )
    return web.Response(status=200)

# Проверка окончания подписок
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
                await bot.send_message(
                    ADMIN_ID,
                    f"⚠️ Błąd przy usuwaniu {user_id}:\n<code>{e}</code>",
                    parse_mode="HTML"
                )

        for user_id in to_remove:
            subscriptions.pop(user_id, None)

        save_subscriptions(subscriptions)
        await asyncio.sleep(86400)
        already_notified.clear()

# Запуск вебхуков
async def on_startup(dp):
    await bot.set_webhook(WEBHOOK_URL)
    asyncio.create_task(check_expired())

async def on_shutdown(dp):
    await bot.delete_webhook()

def setup_web_app(dp):
    app = web.Application()
    app.router.add_post(STRIPE_WEBHOOK_PATH, stripe_webhook)
    return app

if __name__ == "__main__":
    start_webhook(
        dispatcher=dp,
        webhook_path=WEBHOOK_PATH,
        on_startup=on_startup,
        on_shutdown=on_shutdown,
        skip_updates=True,
        host=WEBAPP_HOST,
        port=WEBAPP_PORT,
        web_app=setup_web_app(dp)
    )