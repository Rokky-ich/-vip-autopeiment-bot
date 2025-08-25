# bot.py (aiogram 2.25.2) — с кнопкой "Zapłaciłem" и ручной проверкой Stripe
import os
import json
import asyncio
from datetime import datetime, timedelta

from aiohttp import web
from aiogram import Bot, Dispatcher, types
from aiogram.types import ParseMode, InlineKeyboardMarkup, InlineKeyboardButton

import stripe

# -------- Конфиг из окружения --------
API_TOKEN = os.getenv("API_TOKEN")
WEBHOOK_HOST = os.getenv("WEBHOOK_HOST")          # пример: https://your-domain.com
CHANNEL_ID = int(os.getenv("CHANNEL_ID", "0"))
ADMIN_ID = int(os.getenv("ADMIN_ID", "1279721354"))

STRIPE_SECRET_KEY = os.getenv("STRIPE_SECRET_KEY")
STRIPE_WEBHOOK_SECRET = os.getenv("STRIPE_WEBHOOK_SECRET")
stripe.api_key = STRIPE_SECRET_KEY

WEBHOOK_PATH = f"/webhook/{API_TOKEN}"            # путь Telegram вебхука
STRIPE_WEBHOOK_PATH = "/webhook/stripe"           # путь Stripe вебхука
WEBHOOK_URL = f"{WEBHOOK_HOST}{WEBHOOK_PATH}"

# username бота для редиректов из Stripe
BOT_USERNAME = os.getenv("BOT_USERNAME", "").lstrip("@")

WEBAPP_HOST = "0.0.0.0"
WEBAPP_PORT = int(os.getenv("PORT", "8000"))

DB_FILE = "/data/subscriptions.json"  # в исходнике ты уже использовал этот путь :contentReference[oaicite:0]{index=0}

# -------- Бот/диспетчер --------
bot = Bot(token=API_TOKEN, parse_mode=ParseMode.HTML)
dp = Dispatcher(bot)

# -------- Утилиты БД (совместимость со старым форматом) --------
def _ensure_data_dir():
    d = os.path.dirname(DB_FILE)
    if d and not os.path.exists(d):
        os.makedirs(d, exist_ok=True)

def _empty_db():
    # subs: { user_id(str): "YYYY-MM-DD" }
    # pending: { user_id(str): "cs_test_..." }
    return {"subs": {}, "pending": {}}

def load_db():
    _ensure_data_dir()
    if not os.path.exists(DB_FILE):
        return _empty_db()
    try:
        with open(DB_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception:
        return _empty_db()

    # миграция: если был старый формат {user_id: "date"}
    if isinstance(data, dict) and "subs" not in data and "pending" not in data:
        return {"subs": data, "pending": {}}
    # нормализуем
    return {
        "subs": dict(data.get("subs", {})),
        "pending": dict(data.get("pending", {})),
    }

def save_db(data: dict):
    _ensure_data_dir()
    with open(DB_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False)

db = load_db()

def get_sub_end(user_id: int):
    return db["subs"].get(str(user_id))

def set_sub_end(user_id: int, end_date_str: str):
    db["subs"][str(user_id)] = end_date_str
    save_db(db)

def set_pending_session(user_id: int, session_id: str):
    db["pending"][str(user_id)] = session_id
    save_db(db)

def pop_pending_session(user_id: int):
    return db["pending"].pop(str(user_id), None) if str(user_id) in db["pending"] else None

def peek_pending_session(user_id: int):
    return db["pending"].get(str(user_id))

# -------- Stripe: создание сессии оплаты --------
async def create_checkout_session(user_id: int):
    try:
        success_url = f"https://t.me/{BOT_USERNAME}  if BOT_USERNAME else WEBHOOK_HOST
        cancel_url  = f"https://t.me/{BOT_USERNAME}  if BOT_USERNAME else WEBHOOK_HOST

        session = stripe.checkout.Session.create(
            payment_method_types=["card"],
            line_items=[{
                "price_data": {
                    "currency": "pln",
                    "product_data": {"name": "Dostęp do kanału VIP"},
                    "unit_amount": 500,  # 5 PLN — как в исходнике :contentReference[oaicite:1]{index=1}
                },
                "quantity": 1,
            }],
            mode="payment",
            success_url=success_url,
            cancel_url=cancel_url,
            metadata={"user_id": str(user_id)},
        )
        # сохраним сессию, чтобы потом можно было вручную проверить
        set_pending_session(user_id, session.id)
        return session.url
    except Exception as e:
        print(f"[Stripe] create_checkout_session error: {e}")
        return None

# -------- Команды / Кнопки --------
def main_keyboard() -> InlineKeyboardMarkup:
    # Добавили третью кнопку "✅ Zapłaciłem"
    return InlineKeyboardMarkup(row_width=1).add(
        InlineKeyboardButton("📞 Kontakt z administratorem", url="https://t.me/wawaadmin"),
        InlineKeyboardButton("💳 Link do płatności", callback_data="pay"),
        InlineKeyboardButton("✅ Zapłaciłem", callback_data="paid"),
    )

@dp.message_handler(commands=["start"])
async def cmd_start(message: types.Message):
    user_id = message.from_user.id
    # проверяем, есть ли "pending" платеж
    item = peek_pending_session(user_id)
    if item:
        await message.answer(
            "👋 Cześć! Widzę, że masz rozpoczętą płatność.\n"
            "Jeśli już opłaciłeś, naciśnij przycisk poniżej:",
            reply_markup=main_keyboard()
        )
    else:
        await message.answer(
            "👋 Cześć! Kliknij przyciski poniżej:",
            reply_markup=main_keyboard()
        )

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
        await callback.message.answer(
            "Po opłaceniu, jeśli link nie przyszedł automatycznie, naciśnij „✅ Zapłaciłem”."
        )
    else:
        await callback.message.answer("❌ Błąd podczas generowania linku do płatności.")
    await callback.answer()

# -------- Ручная проверка "Zapłaciłem" --------
@dp.callback_query_handler(lambda c: c.data == "paid")
async def handle_paid(callback: types.CallbackQuery):
    user_id = callback.from_user.id
    session_id = peek_pending_session(user_id)

    if not session_id:
        await callback.message.answer(
            "Nie widzę aktywnej płatności. Najpierw użyj „💳 Link do płatności”.",
            reply_markup=main_keyboard()
        )
        await callback.answer()
        return

    try:
        session = stripe.checkout.Session.retrieve(session_id)
    except Exception as e:
        await callback.message.answer(f"❌ Błąd sprawdzania płatności: {e}")
        await callback.answer()
        return

    if session.get("status") == "complete" and session.get("payment_status") == "paid":
        # оформляем подписку и отправляем инвайт
        end_date = (datetime.now() + timedelta(days=30)).strftime("%Y-%m-%d")
        set_sub_end(user_id, end_date)
        # убираем pending сессию
        pop_pending_session(user_id)

        try:
            invite = await bot.create_chat_invite_link(
                chat_id=CHANNEL_ID,
                expire_date=int((datetime.now() + timedelta(days=1)).timestamp()),
                member_limit=1
            )
            kb = InlineKeyboardMarkup().add(
                InlineKeyboardButton("🔗 Dołącz do kanału", url=invite.invite_link)
            )
            await callback.message.answer("✅ Płatność potwierdzona! Oto link:", reply_markup=kb)
        except Exception as e:
            await bot.send_message(
                ADMIN_ID,
                f"⚠️ Błąd przy wysyłaniu linku użytkownikowi {user_id}:\n<code>{e}</code>"
            )
            await callback.message.answer("⚠️ Wystąpił błąd po stronie bota. Admin został powiadomiony.")
    else:
        await callback.message.answer(
            "🔎 Płatność jeszcze niepotwierdzona. Jeśli zapłaciłeś, odczekaj chwilę i naciśnij ponownie „✅ Zapłaciłem”."
        )

    await callback.answer()

# -------- Stripe вебхук (автоматический путь) --------
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
            set_sub_end(int(user_id), end_date)
            # если была pending-сессия — очищаем
            try:
                if peek_pending_session(int(user_id)) == session.get("id"):
                    pop_pending_session(int(user_id))
            except Exception:
                pass

            try:
                invite = await bot.create_chat_invite_link(
                    chat_id=CHANNEL_ID,
                    expire_date=int((datetime.now() + timedelta(days=1)).timestamp()),
                    member_limit=1
                )
                kb = InlineKeyboardMarkup().add(
                    InlineKeyboardButton("🔗 Dołącz do kanału", url=invite.invite_link)
                )
                await bot.send_message(
                    int(user_id),
                    "✅ Płatność potwierdzona! Kliknij poniżej, aby dołączyć do kanału:",
                    reply_markup=kb
                )
            except Exception as e:
                await bot.send_message(
                    ADMIN_ID,
                    f"⚠️ Błąd przy wysyłaniu linku użytkownikowi {user_id}:\n<code>{e}</code>"
                )

    return web.Response(status=200)

# -------- Периодическая проверка подписок --------
async def check_expired():
    already_notified = set()
    while True:
        now = datetime.now().date()
        to_remove = []

        for user_id, end_str in list(db["subs"].items()):
            try:
                end_date = datetime.strptime(end_str, "%Y-%m-%d").date()

                if end_date == now + timedelta(days=1) and user_id not in already_notified:
                    await bot.send_message(int(user_id), "⏳ Twoja subskrypcja kończy się jutro!")
                    already_notified.add(user_id)

                elif end_date <= now:
                    try:
                        await bot.send_message(int(user_id), "❌ Twoja subskrypcja wygasła. Zostałeś usunięty z kanału.")
                    except Exception:
                        pass
                    try:
                        await bot.kick_chat_member(CHANNEL_ID, int(user_id))
                        await asyncio.sleep(1)
                        await bot.unban_chat_member(CHANNEL_ID, int(user_id))
                    except Exception as e:
                        await bot.send_message(ADMIN_ID, f"⚠️ Błąd przy usuwaniu {user_id}:\n<code>{e}</code>")
                    to_remove.append(user_id)
            except Exception as e:
                await bot.send_message(ADMIN_ID, f"⚠️ Błąd przy przetwarzaniu {user_id}:\n<code>{e}</code>")

        for uid in to_remove:
            db["subs"].pop(uid, None)

        save_db(db)
        await asyncio.sleep(86400)
        already_notified.clear()

# -------- Telegram вебхук-хендлер --------
async def telegram_webhook(request: web.Request):
    try:
        data = await request.json()
    except Exception:
        return web.Response(status=400)

    update = types.Update(**data)

    Bot.set_current(bot)
    Dispatcher.set_current(dp)

    await dp.process_update(update)
    return web.Response(text="OK")

# -------- Хуки запуска/остановки --------
async def on_startup_app(app: web.Application):
    await bot.set_webhook(WEBHOOK_URL)
    asyncio.create_task(check_expired())

async def on_shutdown_app(app: web.Application):
    await bot.delete_webhook()

# -------- Точка входа --------
def build_app() -> web.Application:
    app = web.Application()
    app.router.add_post(WEBHOOK_PATH, telegram_webhook)
    app.router.add_post(STRIPE_WEBHOOK_PATH, stripe_webhook)
    async def health(request):
        return web.Response(text="OK")
    app.router.add_get("/health", health)
    app.on_startup.append(on_startup_app)
    app.on_shutdown.append(on_shutdown_app)
    return app

if __name__ == "__main__":
    web.run_app(build_app(), host=WEBAPP_HOST, port=WEBAPP_PORT)