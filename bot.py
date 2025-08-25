# bot.py (aiogram 2.25.2)
# база + продление 59 PLN + "уже подписан" + авто-чистка pending + санитарка pending
# + уведомления админу + постоянная клавиатура с кнопкой 🚀START (работает как /start)
import os
import json
import asyncio
import time
from datetime import datetime, timedelta

from aiohttp import web
from aiogram import Bot, Dispatcher, types
from aiogram.types import ParseMode, InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.types import ReplyKeyboardMarkup, KeyboardButton

import stripe

# -------- Конфиг из окружения --------
API_TOKEN = os.getenv("API_TOKEN")
WEBHOOK_HOST = os.getenv("WEBHOOK_HOST")          # напр.: https://your-domain.com
CHANNEL_ID = int(os.getenv("CHANNEL_ID", "0"))
ADMIN_ID = int(os.getenv("ADMIN_ID", "1279721354"))

STRIPE_SECRET_KEY = os.getenv("STRIPE_SECRET_KEY")
STRIPE_WEBHOOK_SECRET = os.getenv("STRIPE_WEBHOOK_SECRET")
stripe.api_key = STRIPE_SECRET_KEY

WEBHOOK_PATH = f"/webhook/{API_TOKEN}"            # Telegram webhook path
STRIPE_WEBHOOK_PATH = "/webhook/stripe"           # Stripe webhook path
WEBHOOK_URL = f"{WEBHOOK_HOST}{WEBHOOK_PATH}"

# username бота для тихого редиректа из Stripe
BOT_USERNAME = os.getenv("BOT_USERNAME", "").lstrip("@")

WEBAPP_HOST = "0.0.0.0"
WEBAPP_PORT = int(os.getenv("PORT", "8000"))

DB_FILE = "/data/subscriptions.json"  # база локальных подписок

# Цены (в PLN)
PRICE_INITIAL_PLN = int(os.getenv("PRICE_INITIAL_PLN", "159"))  # базовая покупка
PRICE_RENEW_PLN   = int(os.getenv("PRICE_RENEW_PLN", "59"))     # продление

# --- Параметры санитарки pending-сессий ---
PENDING_TTL_SEC    = int(os.getenv("PENDING_TTL_SEC", "1800"))  # 30 мин
PENDING_SWEEP_SEC  = int(os.getenv("PENDING_SWEEP_SEC", "300")) # 5 мин

# -------- Бот/диспетчер --------
bot = Bot(token=API_TOKEN, parse_mode=ParseMode.HTML)
dp = Dispatcher(bot)

# -------- Утилиты БД --------
def _ensure_data_dir():
    d = os.path.dirname(DB_FILE)
    if d and not os.path.exists(d):
        os.makedirs(d, exist_ok=True)

def _empty_db():
    # subs: { user_id(str): "YYYY-MM-DD" }
    # pending: { user_id(str): {"id": "cs_...", "ts": 123, "kind": "initial"|"renew"} }
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
    if isinstance(data, dict) and "subs" not in data and "pending" not in data:
        # старый формат: {user_id: "YYYY-MM-DD"}
        return {"subs": data, "pending": {}}
    return {"subs": dict(data.get("subs", {})), "pending": dict(data.get("pending", {}))}

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

def set_pending_session(user_id: int, session_id: str, kind: str):
    db["pending"][str(user_id)] = {"id": session_id, "ts": int(time.time()), "kind": kind}
    save_db(db)

def peek_pending_session(user_id: int):
    item = db["pending"].get(str(user_id))
    if isinstance(item, str):  # бэкомпат
        return {"id": item, "ts": int(time.time()), "kind": "initial"}
    return item

def pop_pending_session(user_id: int):
    db["pending"].pop(str(user_id), None)
    save_db(db)

# -------- Вспомогательные хелперы --------
def extend_30_days_from_current_or_today(current_end_str: str | None) -> str:
    """Продлевает на 30 дней от большей даты: сегодня или текущего конца."""
    today = datetime.now().date()
    base = today
    if current_end_str:
        try:
            end_date = datetime.strptime(current_end_str, "%Y-%m-%d").date()
            if end_date > base:
                base = end_date
        except Exception:
            pass
    new_end = base + timedelta(days=30)
    return new_end.strftime("%Y-%m-%d")

def _parse_date(date_str: str):
    return datetime.strptime(date_str, "%Y-%m-%d").date()

def _sub_status(user_id: int):
    """Возвращает (is_active: bool, end_date: date|None, days_left: int|None)."""
    end_str = get_sub_end(user_id)
    if not end_str:
        return False, None, None
    try:
        end_date = _parse_date(end_str)
    except Exception:
        return False, None, None
    today = datetime.now().date()
    if end_date >= today:
        return True, end_date, (end_date - today).days
    return False, end_date, 0

# --- Уведомления админу ---
async def _get_user_display(user_id: int) -> str:
    try:
        u = await bot.get_chat(user_id)
        parts = []
        if getattr(u, "full_name", None):
            parts.append(u.full_name)
        if getattr(u, "username", None):
            parts.append(f"@{u.username}")
        parts.append(f"id:{user_id}")
        return " / ".join(parts)
    except Exception:
        return f"id:{user_id}"

async def notify_admin_purchase(user_id: int, kind: str, new_end: str, amount_pln: int, session_id: str | None = None):
    user_disp = await _get_user_display(user_id)
    title = "🆕 Покупка (initial)" if kind == "initial" else "🔄 Продление (renew)"
    sid_line = f"\nSID: <code>{session_id}</code>" if session_id else ""
    msg = (
        f"{title}\n"
        f"👤 Пользователь: {user_disp}\n"
        f"💰 Сумма: {amount_pln} PLN\n"
        f"📅 Новая дата окончания: <b>{new_end}</b>"
        f"{sid_line}"
    )
    try:
        await bot.send_message(ADMIN_ID, msg)
    except Exception:
        pass

# --- Аккуратная очистка старой pending-сессии перед созданием новой ---
def expire_and_clear_pending_if_open(user_id: int):
    item = peek_pending_session(user_id)
    session_id = item["id"] if item else None
    if not session_id:
        return
    try:
        session = stripe.checkout.Session.retrieve(session_id)
        if session and session.get("status") == "open":
            try:
                stripe.checkout.Session.expire(session_id)
            except Exception:
                pass
    except Exception:
        pass
    pop_pending_session(user_id)

# --- Фоновая санитарка pending-сессий ---
async def sanitize_pending_loop():
    while True:
        now_ts = int(time.time())
        to_delete = []

        for uid, item in list(db["pending"].items()):
            try:
                if isinstance(item, str):
                    item = {"id": item, "ts": now_ts, "kind": "initial"}

                sid = item.get("id")
                ts  = int(item.get("ts", now_ts))

                if not sid:
                    to_delete.append(uid)
                    continue

                sess = None
                try:
                    sess = stripe.checkout.Session.retrieve(sid)
                except Exception:
                    pass

                if sess and sess.get("status") in ("complete", "expired"):
                    to_delete.append(uid)
                    continue

                age = now_ts - ts
                if age >= PENDING_TTL_SEC:
                    try:
                        if sess is None:
                            sess = stripe.checkout.Session.retrieve(sid)
                    except Exception:
                        sess = None
                    try:
                        if sess and sess.get("status") == "open":
                            stripe.checkout.Session.expire(sid)
                    except Exception:
                        pass
                    to_delete.append(uid)

            except Exception as e:
                try:
                    await bot.send_message(
                        ADMIN_ID,
                        f"⚠️ sanitize_pending error for {uid}: <code>{e}</code>"
                    )
                except Exception:
                    pass

        if to_delete:
            for uid in to_delete:
                db["pending"].pop(uid, None)
            save_db(db)

        await asyncio.sleep(PENDING_SWEEP_SEC)

# -------- Stripe: создание сессии оплаты --------
def _success_url():
    # Тихий редирект в чат бота — Telegram сам покажет системную кнопку Start
    return f"https://t.me/{BOT_USERNAME}" if BOT_USERNAME else WEBHOOK_HOST

def _cancel_url():
    return f"https://t.me/{BOT_USERNAME}" if BOT_USERNAME else WEBHOOK_HOST

async def create_checkout_session(user_id: int, amount_pln: int, product_name: str, kind: str):
    try:
        expire_and_clear_pending_if_open(user_id)
        session = stripe.checkout.Session.create(
            payment_method_types=["card"],
            line_items=[{
                "price_data": {
                    "currency": "pln",
                    "product_data": {"name": product_name},
                    "unit_amount": amount_pln * 100,
                },
                "quantity": 1,
            }],
            mode="payment",
            success_url=_success_url(),
            cancel_url=_cancel_url(),
            metadata={"user_id": str(user_id), "kind": kind},
        )
        set_pending_session(user_id, session.id, kind)
        return session.url
    except Exception as e:
        print(f"[Stripe] create_checkout_session error: {e}")
        return None

# -------- Клавиатуры --------
def reply_persistent_kb() -> ReplyKeyboardMarkup:
    """Постоянная клавиатура внизу чата: кнопка отправляет 🚀START (мы перехватываем и обрабатываем как /start)."""
    kb = ReplyKeyboardMarkup(resize_keyboard=True, one_time_keyboard=False)
    kb.add(KeyboardButton("🚀START"))
    return kb

def main_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(row_width=1).add(
        InlineKeyboardButton("📞 Kontakt z administratorem", url="https://t.me/wawaadmin"),
        InlineKeyboardButton("💳 VIP na miesiąc 159zl", callback_data="pay"),
        InlineKeyboardButton("✅ Zapłaciłem", callback_data="paid"),
    )

def renew_offer_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(row_width=2).add(
        InlineKeyboardButton(f"🔄 Przedłuż za {PRICE_RENEW_PLN} PLN", callback_data="renew"),
        InlineKeyboardButton("Nie przedłużaj", callback_data="norenew"),
    )

def paid_inline_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(row_width=1).add(
        InlineKeyboardButton("✅ Zapłaciłem", callback_data="paid")
    )

# -------- /start и алиасы для кнопки 🚀START --------
@dp.message_handler(commands=["start"])
async def cmd_start(message: types.Message):
    user_id = message.from_user.id
    item = peek_pending_session(user_id)
    if item:
        await message.answer(
            "👋 Cześć! Widzę, że masz rozpoczętą płatność.\n"
            "Jeśli już opłaciłeś, naciśnij „✅ Zapłaciłem”.",
            reply_markup=reply_persistent_kb()
        )
        await message.answer(
            "👇 Wybierz działanie:",
            reply_markup=main_keyboard()
        )
    else:
        await message.answer(
            "👋 Cześć! Kliknij przyciski poniżej:",
            reply_markup=reply_persistent_kb()
        )
        await message.answer(
            "👇 Menu:",
            reply_markup=main_keyboard()
        )

def _is_start_btn_text(text: str) -> bool:
    """Нормализуем текст и считаем 🚀START / START как /start."""
    if not text:
        return False
    t = text.strip().lower()
    t = t.replace("🚀", "").strip()
    return t in {"start", "/start"}

@dp.message_handler(lambda m: _is_start_btn_text(m.text))
async def start_button_alias(message: types.Message):
    # Нажали на кнопку 🚀START в reply-клавиатуре — выполняем ту же логику, что и /start
    await cmd_start(message)

# -------- Первичная оплата / с проверкой активной подписки --------
@dp.callback_query_handler(lambda c: c.data == "pay")
async def handle_payment(callback: types.CallbackQuery):
    user_id = callback.from_user.id

    # Если у пользователя активная подписка — не создаём первичную, предлагаем продлить
    is_active, end_date, days_left = _sub_status(user_id)
    if is_active:
        expire_and_clear_pending_if_open(user_id)
        await callback.message.answer(
            "✅ Masz już aktywną subskrypcję.\n"
            f"📅 Ważna do: <b>{end_date.strftime('%Y-%m-%d')}</b> "
            f"(pozostało dni: <b>{days_left}</b>).\n\n"
            f"Chcesz przedłużyć o kolejne 30 dni za <b>{PRICE_RENEW_PLN} PLN</b>?",
            reply_markup=renew_offer_keyboard()
        )
        await callback.answer()
        return

    payment_url = await create_checkout_session(
        user_id, PRICE_INITIAL_PLN, "Dostęp do kanału VIP", kind="initial"
    )
    if payment_url:
        await callback.message.answer(
            "💳 Kliknij poniżej, aby przejść do płatności:",
            reply_markup=InlineKeyboardMarkup().add(
                InlineKeyboardButton("🔗 Zapłać teraz", url=payment_url)
            )
        )
        await callback.message.answer(
            "Po opłaceniu, jeśli link nie przyszedł automatycznie, naciśnij „✅ Zapłaciłem”.",
            reply_markup=paid_inline_keyboard()
        )
    else:
        await callback.message.answer("❌ Błąd podczas generowania linku do płatności.")
    await callback.answer()

# -------- Продление --------
@dp.callback_query_handler(lambda c: c.data == "renew")
async def handle_renew(callback: types.CallbackQuery):
    user_id = callback.from_user.id
    payment_url = await create_checkout_session(
        user_id, PRICE_RENEW_PLN, "VIP_WAWA — przedłużenie 30 dni", kind="renew"
    )
    if payment_url:
        await callback.message.answer(
            f"🔄 Przedłużenie VIP_WAWA za {PRICE_RENEW_PLN} PLN — kliknij, aby zapłacić:",
            reply_markup=InlineKeyboardMarkup().add(
                InlineKeyboardButton("🔗 Zapłać teraz", url=payment_url)
            )
        )
        await callback.message.answer(
            "Po opłaceniu naciśnij „✅ Zapłaciłem”, aby odebrać link.",
            reply_markup=paid_inline_keyboard()
        )
    else:
        await callback.message.answer("❌ Nie udało się wygenerować linku do przedłużenia.")
    await callback.answer()

@dp.callback_query_handler(lambda c: c.data == "norenew")
async def handle_no_renew(callback: types.CallbackQuery):
    await callback.message.answer("Rozumiem. Możesz wrócić do przedłużenia w dowolnym momencie z menu.")
    await callback.answer()

# -------- Ручная проверка "Zapłaciłem" (и уведомление админу) --------
@dp.callback_query_handler(lambda c: c.data == "paid")
async def handle_paid(callback: types.CallbackQuery):
    user_id = callback.from_user.id
    item = peek_pending_session(user_id)
    session_id = item["id"] if item and isinstance(item, dict) else (item if isinstance(item, str) else None)
    kind = item.get("kind") if isinstance(item, dict) else "initial"

    is_active, end_date, days_left = _sub_status(user_id)

    if not session_id:
        if is_active:
            text = (
                "✅ Już masz aktywną subskrypcję.\n"
                f"📅 Ważna do: <b>{end_date.strftime('%Y-%m-%d')}</b> "
                f"(pozostało dni: <b>{days_left}</b>).\n\n"
                "Chcesz przedłużyć o kolejne 30 dni?"
            )
            await callback.message.answer(text, reply_markup=renew_offer_keyboard())
        else:
            await callback.message.answer(
                "Nie widzę aktywnej płatności. Najpierw użyj „💳 VIP na miesiąc 159zl” lub „🔄 Przedłuż”.",
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
        amount_total = session.get("amount_total")
        amount_pln = int(amount_total // 100) if isinstance(amount_total, int) else (PRICE_RENEW_PLN if kind == "renew" else PRICE_INITIAL_PLN)

        new_end = extend_30_days_from_current_or_today(get_sub_end(user_id))
        set_sub_end(user_id, new_end)
        pop_pending_session(user_id)

        await notify_admin_purchase(user_id, kind, new_end, amount_pln, session_id=session.get("id"))

        try:
            invite = await bot.create_chat_invite_link(
                chat_id=CHANNEL_ID,
                expire_date=int((datetime.now() + timedelta(days=1)).timestamp()),
                member_limit=1
            )
            kb = InlineKeyboardMarkup().add(
                InlineKeyboardButton("🔗 Dołącz do kanału", url=invite.invite_link)
            )
            await callback.message.answer(
                "✅ Płatność potwierdzona! Twoja subskrypcja została przedłużona o 30 dni.\n"
                f"📅 Nowa дата końca: <b>{new_end}</b>\n"
                "Kliknij, aby dołączyć:",
                reply_markup=kb
            )
        except Exception as e:
            await bot.send_message(
                ADMIN_ID,
                f"⚠️ Błąd przy wysyłaniu linku użytkownikowi {user_id}:\n<code>{e}</code>"
            )
            await callback.message.answer("⚠️ Wystąpił błąd po stronie бота. Admin został powiadomiony.")
    else:
        if is_active:
            await callback.message.answer(
                "🔎 Płatność jeszcze niepotwierdzona.\n"
                f"✅ Masz aktywną субскрыпцию до <b>{end_date.strftime('%Y-%m-%d')}</b> "
                f"(pozostało dni: <b>{days_left}</b>).\n"
                "Jeśli zapłaciłeś, odczekaj chwilę i нaciśnij ponownie „✅ Zapłaciłem”."
            )
        else:
            await callback.message.answer(
                "🔎 Płatność jeszcze niepotwierdzona. Jeśli zapłaciłeś, odczekaj chwilę и нaciśnij ponownie "
                "„✅ Zapłaciłem”."
            )

    await callback.answer()

# -------- Stripe webhook (автоматический путь) + уведомление админу --------
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
        kind = session.get("metadata", {}).get("kind", "initial")
        if user_id:
            user_id_int = int(user_id)
            new_end = extend_30_days_from_current_or_today(get_sub_end(user_id_int))
            set_sub_end(user_id_int, new_end)

            try:
                item = peek_pending_session(user_id_int)
                if item and item.get("id") == session.get("id"):
                    pop_pending_session(user_id_int)
            except Exception:
                pass

            amount_total = session.get("amount_total")
            amount_pln = int(amount_total // 100) if isinstance(amount_total, int) else (PRICE_RENEW_PLN if kind == "renew" else PRICE_INITIAL_PLN)
            await notify_admin_purchase(user_id_int, kind, new_end, amount_pln, session_id=session.get("id"))

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
                    user_id_int,
                    "✅ Płatność potwierdzona! Twoja subskrypcja została przedłużona o 30 dni.\n"
                    f"📅 Nowa дата końca: <b>{new_end}</b>\n"
                    "Kliknij, aby dołączyć:",
                    reply_markup=kb
                )
            except Exception as e:
                await bot.send_message(
                    ADMIN_ID,
                    f"⚠️ Błąd przy wysyłaniu linku użytkownikowi {user_id}:\n<code>{e}</code>"
                )

    return web.Response(status=200)

# -------- Напоминалки и автокик --------
async def check_expired():
    already_notified = set()
    while True:
        now = datetime.now().date()
        to_remove = []

        for user_id, end_str in list(db["subs"].items()):
            try:
                end_date = datetime.strptime(end_str, "%Y-%m-%d").date()

                if end_date == now + timedelta(days=1) and user_id not in already_notified:
                    await bot.send_message(
                        int(user_id),
                        "⏳ Twoja subskrypcja <b>VIP_WAWA</b> kończy się jutro.\n"
                        f"Możesz przedłużyć ją teraz za <b>{PRICE_RENEW_PLN} PLN</b>.",
                        reply_markup=renew_offer_keyboard()
                    )
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
    asyncio.create_task(sanitize_pending_loop())

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