# bot.py (aiogram 2.25.2)
# база + продление 59 PLN (без ссылки) + "уже подписан"
# авто-чистка pending + санитарка pending + уведомления админу
# постоянная кнопка 🚀START
# АДМИНКА: /admin, /give_link, /who, /extend, /find, /revoke
# панели: Активные, Истекает ≤3 дней, Просрочены, Все пользователи (pagination), Поиск
import os
import json
import asyncio
import time
from datetime import datetime, timedelta

from aiohttp import web
from aiogram import Bot, Dispatcher, types
from aiogram.types import ParseMode, InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.types import ReplyKeyboardMarkup, KeyboardButton
from aiogram.utils.exceptions import MessageNotModified

import stripe

# -------- Конфиг из окружения --------
API_TOKEN = os.getenv("API_TOKEN")
WEBHOOK_HOST = os.getenv("WEBHOOK_HOST")          # напр.: https://your-domain.com
CHANNEL_ID = int(os.getenv("CHANNEL_ID", "0"))
ADMIN_ID = int(os.getenv("ADMIN_ID", "2119400801"))

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
PRICE_INITIAL_PLN = int(os.getenv("PRICE_INITIAL_PLN", "99"))  # базовая покупка
PRICE_RENEW_PLN   = int(os.getenv("PRICE_RENEW_PLN", "59"))     # продление

# --- Параметры санитарки pending-сессий ---
PENDING_TTL_SEC    = int(os.getenv("PENDING_TTL_SEC", "1800"))  # 30 мин
PENDING_SWEEP_SEC  = int(os.getenv("PENDING_SWEEP_SEC", "300")) # 5 мин

# -------- Бот/диспетчер --------
bot = Bot(token=API_TOKEN, parse_mode=ParseMode.HTML)
dp = Dispatcher(bot)

# -------- Утилиты БД (расширенная схема) --------
def _ensure_data_dir():
    d = os.path.dirname(DB_FILE)
    if d and not os.path.exists(d):
        os.makedirs(d, exist_ok=True)

def _empty_db():
    # subs: { user_id(str): "YYYY-MM-DD" }
    # pending: { user_id(str): {"id": "cs_...", "ts": 123, "kind": "initial"|"renew"} }
    # users: { user_id(str): {"username": str|null, "name": str|null, "first_seen": int, "last_seen": int} }
    # processed: { "sessions": {sid: ts}, "intents": {pi: ts} }
    return {"subs": {}, "pending": {}, "users": {}, "processed": {"sessions": {}, "intents": {}}}

def load_db():
    _ensure_data_dir()
    if not os.path.exists(DB_FILE):
        return _empty_db()
    try:
        with open(DB_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception:
        return _empty_db()
    # апгрейд старых форматов
    if isinstance(data, dict) and "subs" not in data and "pending" not in data:
        data = {"subs": data, "pending": {}, "users": {}}
    if "users" not in data:
        data["users"] = {}
    proc = data.get("processed", {}) or {}
    return {
        "subs": dict(data.get("subs", {})),
        "pending": dict(data.get("pending", {})),
        "users": dict(data.get("users", {})),
        "processed": {
            "sessions": dict(proc.get("sessions", {})),
            "intents": dict(proc.get("intents", {})),
        },
    }

def save_db(data: dict):
    _ensure_data_dir()
    with open(DB_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False)

db = load_db()

# ---- users трекинг ----
def _now_ts() -> int:
    return int(time.time())

def track_user_from_message(message: types.Message):
    try:
        u = message.from_user
        uid = str(u.id)
        entry = db["users"].get(uid, {})
        if "first_seen" not in entry:
            entry["first_seen"] = _now_ts()
        entry["last_seen"] = _now_ts()
        entry["username"] = u.username or entry.get("username")
        entry["name"] = u.full_name or entry.get("name")
        db["users"][uid] = entry
        save_db(db)
    except Exception:
        pass

def track_user_from_callback(cb: types.CallbackQuery):
    try:
        u = cb.from_user
        uid = str(u.id)
        entry = db["users"].get(uid, {})
        if "first_seen" not in entry:
            entry["first_seen"] = _now_ts()
        entry["last_seen"] = _now_ts()
        entry["username"] = u.username or entry.get("username")
        entry["name"] = u.full_name or entry.get("name")
        db["users"][uid] = entry
        save_db(db)
    except Exception:
        pass

# ---- subs/pending ----
def get_sub_end(user_id: int):
    return db["subs"].get(str(user_id))

def set_sub_end(user_id: int, end_date_str: str):
    db["subs"][str(user_id)] = end_date_str
    save_db(db)

def set_pending_session(user_id: int, session_id: str, kind: str):
    db["pending"][str(user_id)] = {"id": session_id, "ts": _now_ts(), "kind": kind}
    save_db(db)

def peek_pending_session(user_id: int):
    item = db["pending"].get(str(user_id))
    if isinstance(item, str):  # бэкомпат
        return {"id": item, "ts": _now_ts(), "kind": "initial"}
    return item

def pop_pending_session(user_id: int):
    db["pending"].pop(str(user_id), None)
    save_db(db)

# -------- Вспомогательные подписки --------
def extend_30_days_from_current_or_today(current_end_str: str | None, days: int = 30) -> str:
    today = datetime.now().date()
    base = today
    if current_end_str:
        try:
            end_date = datetime.strptime(current_end_str, "%Y-%m-%d").date()
            if end_date > base:
                base = end_date
        except Exception:
            pass
    new_end = base + timedelta(days=days)
    return new_end.strftime("%Y-%m-%d")

def _parse_date(date_str: str):
    return datetime.strptime(date_str, "%Y-%m-%d").date()

def _sub_status(user_id: int):
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
        now_ts = _now_ts()
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

# -------- Stripe: создание сессии оплаты (card + BLIK) --------
def _success_url():
    return f"https://t.me/{BOT_USERNAME}" if BOT_USERNAME else WEBHOOK_HOST

def _cancel_url():
    return f"https://t.me/{BOT_USERNAME}" if BOT_USERNAME else WEBHOOK_HOST

async def create_checkout_session(user_id: int, amount_pln: int, product_name: str, kind: str):
    try:
        expire_and_clear_pending_if_open(user_id)
        session = stripe.checkout.Session.create(
            payment_method_types=["card", "blik"],  # добавили BLIK
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

def _session_effectively_paid(session) -> tuple[bool, int | None]:
    """
    Returns (paid, amount_pln) for a Checkout Session.
    paid = True if session is complete+paid OR its PaymentIntent is succeeded (async methods like BLIK).
    amount_pln best-effort from session.amount_total or PI.amount.
    """
    try:
        status = session.get("status")
        pay_status = session.get("payment_status")
        if status == "complete" and pay_status == "paid":
            amt = session.get("amount_total")
            return True, (int(amt // 100) if isinstance(amt, int) else None)
        pi_id = session.get("payment_intent")
        if pi_id:
            try:
                pi = stripe.PaymentIntent.retrieve(pi_id)
                if pi and pi.get("status") == "succeeded":
                    amt = pi.get("amount")
                    return True, (int(amt // 100) if isinstance(amt, int) else None)
            except Exception:
                pass
    except Exception:
        pass
    return False, None


# ---- Stripe idempotency helpers ----
def _is_processed(session_or_obj) -> bool:
    try:
        sid = session_or_obj.get("id")
        pi = session_or_obj.get("payment_intent")
    except Exception:
        sid = None
        pi = None
    if sid and sid in db.get("processed", {}).get("sessions", {}):
        return True
    if isinstance(pi, str) and pi in db.get("processed", {}).get("intents", {}):
        return True
    return False

def _mark_processed(session_or_obj):
    ts = _now_ts()
    try:
        sid = session_or_obj.get("id")
        if sid:
            db.setdefault("processed", {}).setdefault("sessions", {})[sid] = ts
        pi = session_or_obj.get("payment_intent")
        if isinstance(pi, str):
            db.setdefault("processed", {}).setdefault("intents", {})[pi] = ts
        save_db(db)
    except Exception:
        pass


# -------- Клавиатуры --------
def reply_persistent_kb() -> ReplyKeyboardMarkup:
    kb = ReplyKeyboardMarkup(resize_keyboard=True, one_time_keyboard=False)
    kb.add(KeyboardButton("🚀START"))
    return kb

def main_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(row_width=1).add(
        InlineKeyboardButton("📞 Kontakt z administratorem", url="https://t.me/wawaadmin"),
        InlineKeyboardButton("💳 VIP na miesiąc 99zl", callback_data="pay"),
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

# -------- Утилиты безопасной обработки callback’ов и редактирования --------
async def _cb_ack(cb: types.CallbackQuery):
    try:
        await cb.answer()
    except Exception:
        pass

async def _safe_edit_text(msg: types.Message, text: str, reply_markup=None):
    try:
        same_text = (msg.text or "") == (text or "")
        same_kb = False
        try:
            same_kb = (
                (msg.reply_markup and reply_markup) and
                (msg.reply_markup.inline_keyboard == reply_markup.inline_keyboard)
            ) or (msg.reply_markup is None and reply_markup is None)
        except Exception:
            same_kb = False
        if same_text and same_kb:
            return
        await msg.edit_text(text, reply_markup=reply_markup)
    except MessageNotModified:
        pass
    except Exception:
        pass

# -------- /start и алиас для 🚀START --------
@dp.message_handler(commands=["start"])
async def cmd_start(message: types.Message):
    track_user_from_message(message)
    user_id = message.from_user.id
    item = peek_pending_session(user_id)
    if item:
        await message.answer(
            "👋 Cześć! Widzę, że masz rozpoczętą płatność.\n"
            "Jeśli już opłaciłeś (w tym BLIK), naciśnij „✅ Zapłaciłem”.",
            reply_markup=reply_persistent_kb()
        )
        await message.answer("👇 Wybierz działanie:", reply_markup=main_keyboard())
    else:
        await message.answer("👋 Cześć! Kliknij przyciski poniżej:", reply_markup=reply_persistent_kb())
        await message.answer("👇 Menu:", reply_markup=main_keyboard())

def _is_start_btn_text(text: str) -> bool:
    if not text:
        return False
    t = text.strip().lower()
    t = t.replace("🚀", "").strip()
    return t in {"start", "/start"}

@dp.message_handler(lambda m: _is_start_btn_text(m.text))
async def start_button_alias(message: types.Message):
    track_user_from_message(message)
    await cmd_start(message)

# -------- Первичная оплата / с проверкой активной подписки --------
@dp.callback_query_handler(lambda c: c.data == "pay")
async def handle_payment(callback: types.CallbackQuery):
    await _cb_ack(callback)
    track_user_from_callback(callback)
    user_id = callback.from_user.id

    is_active, end_date, days_left = _sub_status(user_id)
    if is_active:
        expire_and_clear_pending_if_open(user_id)
        await callback.message.answer(
            "✅ Masz już aktywną subskrypcję.\n"
            f"📅 Ważna do: <b>{end_date.strftime('%Y-%m-%d')}</b> "
            f"(pozostało dni: <b>{days_left}</b>).\n\n"
            f"Chcesz przedłużyć o kolejne 30 dni za <b>{PRICE_RENEW_PLN} PLN</b>? (dostępne: BLIK, karta)",
            reply_markup=renew_offer_keyboard()
        )
        return

    payment_url = await create_checkout_session(
        user_id, PRICE_INITIAL_PLN, "Dostęp do kanału VIP", kind="initial"
    )
    if payment_url:
        await callback.message.answer(
            "💳 Kliknij, aby zapłacić (BLIK / karta):",
            reply_markup=InlineKeyboardMarkup().add(
                InlineKeyboardButton("🔗 Zapłać teraz", url=payment_url)
            )
        )
        await callback.message.answer(
            "Po opłaceniu (także BLIK), jeśli link nie przyszedł automatycznie, naciśnij „✅ Zapłaciłem”.",
            reply_markup=paid_inline_keyboard()
        )
    else:
        await callback.message.answer("❌ Błąd podczas generowania linku do płatności.")

# -------- Продление --------
@dp.callback_query_handler(lambda c: c.data == "renew")
async def handle_renew(callback: types.CallbackQuery):
    await _cb_ack(callback)
    track_user_from_callback(callback)
    user_id = callback.from_user.id
    payment_url = await create_checkout_session(
        user_id, PRICE_RENEW_PLN, "VIP_WAWA — przedłużenie 30 dni", kind="renew"
    )
    if payment_url:
        await callback.message.answer(
            f"🔄 Przedłużenie VIP_WAWA za {PRICE_RENEW_PLN} PLN — kliknij, aby zapłacić (BLIK / karta):",
            reply_markup=InlineKeyboardMarkup().add(
                InlineKeyboardButton("🔗 Zapłać teraz", url=payment_url)
            )
        )
        await callback.message.answer(
            "Po opłaceniu naciśnij „✅ Zapłaciłem”, aby potwierdzić przedłużenie.",
            reply_markup=paid_inline_keyboard()
        )
    else:
        await callback.message.answer("❌ Nie udało się wygenerować linku do przedłużenia.")

@dp.callback_query_handler(lambda c: c.data == "norenew")
async def handle_no_renew(callback: types.CallbackQuery):
    await _cb_ack(callback)
    track_user_from_callback(callback)
    await callback.message.answer("Rozumiem. Możesz wrócić do przedłużenia w dowolnym momencie z menu.")

# -------- Ручная проверка "Zapłaciłem" --------
@dp.callback_query_handler(lambda c: c.data == "paid")
async def handle_paid(callback: types.CallbackQuery):
    await _cb_ack(callback)
    track_user_from_callback(callback)
    user_id = callback.from_user.id
    item = peek_pending_session(user_id)
    session_id = item["id"] if item and isinstance(item, dict) else (item if isinstance(item, str) else None)
    kind = item.get("kind") if isinstance(item, dict) else "initial"

    is_active, end_date, days_left = _sub_status(user_id)

    # Fallback: no pending -> search recent Checkout Sessions for this user (helps old BLIK payments)
    if not session_id:
        try:
            sessions = stripe.checkout.Session.list(limit=50)
            target = None
            for s in sessions.get("data", []):
                md = s.get("metadata") or {}
                if md.get("user_id") == str(user_id):
                    ok, _ = _session_effectively_paid(s)
                    if ok:
                        target = s
                        break
            if target:
                md = target.get("metadata") or {}
                kind2 = md.get("kind") or "initial"
                paid_ok, amount_pln = _session_effectively_paid(target)
                if _is_processed(target):
                    # already handled previously
                    if kind2 == "initial":
                        if await _is_in_channel(user_id):
                            await callback.message.answer(
                                "✅ Płatność potwierdzona! Dostęp jest już aktywny.\n"
                                f"📅 Data końca: <b>{get_sub_end(user_id)}</b>"
                            )
                        else:
                            await callback.message.answer(
                                "✅ Płatność potwierdzona! Dostęp jest aktywny. Jeśli nie widzisz kanału, napisz do administratora."
                            )
                    else:
                        await callback.message.answer(
                            "✅ Płatność potwierdzona! Subskrypcja jest aktywna."
                        )
                    return
                if amount_pln is None:
                    amt = target.get("amount_total")
                    amount_pln = int(amt // 100) if isinstance(amt, int) else (PRICE_RENEW_PLN if kind2 == "renew" else PRICE_INITIAL_PLN)

                _mark_processed(target)
                new_end = extend_30_days_from_current_or_today(get_sub_end(user_id))
                set_sub_end(user_id, new_end)
                pop_pending_session(user_id)

                await notify_admin_purchase(user_id, kind2, new_end, amount_pln, session_id=target.get("id"))

                if kind2 == "initial":
                    try:
                        invite = await bot.create_chat_invite_link(
                            chat_id=CHANNEL_ID,
                            expire_date=int((datetime.now() + timedelta(days=1)).timestamp()),
                            member_limit=1
                        )
                        kb = InlineKeyboardMarkup().add(InlineKeyboardButton("🔗 Dołącz do kanału", url=invite.invite_link))
                        await callback.message.answer(
                            "✅ Płatność potwierdzona! Otrzymujesz dostęp do kanału na 30 dni.\n"
                            f"📅 Data końca: <b>{new_end}</b>\n"
                            "Kliknij, aby dołączyć:",
                            reply_markup=kb
                        )
                    except Exception as e:
                        await bot.send_message(ADMIN_ID, f"⚠️ Błąd przy wysyłaniu linku użytkownikowi {user_id}:\n<code>{e}</code>")
                        await callback.message.answer("⚠️ Wystąpił błąd po stronie bota. Admin został powiadomiony.")
                else:
                    await callback.message.answer(
                        "✅ Płatność potwierdzona! Twoja subskrypcja została przedłużona o 30 dni.\n"
                        f"📅 Nowa data końca: <b>{new_end}</b>"
                    )
                return
        except Exception:
            pass

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
                "Nie widzę aktywnej płatności. Najpierw użyj „💳 VIP na miesiąc 99zl” lub „🔄 Przedłuż”.",
                reply_markup=main_keyboard()
            )
        return

    # Normal flow with a known pending session_id
    try:
        session = stripe.checkout.Session.retrieve(session_id)
    except Exception as e:
        await callback.message.answer(f"❌ Błąd sprawdzania płatności: {e}")
        return

    paid_ok, amount_pln = _session_effectively_paid(session)
    if paid_ok:
        if _is_processed(session):
            # Already processed earlier: just inform the user; no duplicate extend or link
            if kind == "initial":
                if await _is_in_channel(user_id):
                    await callback.message.answer(
                        "✅ Płatność potwierdzona! Dostęp jest już aktywny.\n"
                        f"📅 Data końca: <b>{get_sub_end(user_id)}</b>"
                    )
                else:
                    await callback.message.answer(
                        "✅ Płatność potwierdzona! Dostęp jest aktywny. Jeśli nie widzisz kanału, napisz do administratora."
                    )
            else:
                await callback.message.answer(
                    "✅ Płatność potwierdzona! Subskrypcja jest aktywna."
                )
            return
        if amount_pln is None:
            amount_total = session.get("amount_total")
            amount_pln = int(amount_total // 100) if isinstance(amount_total, int) else (PRICE_RENEW_PLN if kind == "renew" else PRICE_INITIAL_PLN)

        _mark_processed(session)
        new_end = extend_30_days_from_current_or_today(get_sub_end(user_id))
        set_sub_end(user_id, new_end)
        pop_pending_session(user_id)

        await notify_admin_purchase(user_id, kind, new_end, amount_pln, session_id=session.get("id"))

        if kind == "initial":
            try:
                invite = await bot.create_chat_invite_link(
                    chat_id=CHANNEL_ID,
                    expire_date=int((datetime.now() + timedelta(days=1)).timestamp()),
                    member_limit=1
                )
                kb = InlineKeyboardMarkup().add(InlineKeyboardButton("🔗 Dołącz do kanału", url=invite.invite_link))
                await callback.message.answer(
                    "✅ Płatność potwierdzona! Otrzymujesz dostęp do kanału na 30 dni.\n"
                    f"📅 Data końca: <b>{new_end}</b>\n"
                    "Kliknij, aby dołączyć:",
                    reply_markup=kb
                )
            except Exception as e:
                await bot.send_message(ADMIN_ID, f"⚠️ Błąd przy wysyłaniu linku użytkownikowi {user_id}:\n<code>{e}</code>")
                await callback.message.answer("⚠️ Wystąpił błąd po stronie bota. Admin został powiadomiony.")
        else:
            await callback.message.answer(
                "✅ Płatność potwierdzona! Twoja subskrypcja została przedłużona o 30 dni.\n"
                f"📅 Nowa data końca: <b>{new_end}</b>"
            )
    else:
        if is_active:
            await callback.message.answer(
                "🔎 Płatność jeszcze niepotwierdzona.\n"
                f"✅ Masz aktywną subskrypcję do <b>{end_date.strftime('%Y-%m-%d')}</b> "
                f"(pozostało dni: <b>{days_left}</b>).\n"
                "Jeśli zapłaciłeś, odczekaj chwilę i naciśnij ponownie „✅ Zapłaciłem”."
            )
        else:
            await callback.message.answer(
                "🔎 Płatność jeszcze niepotwierdzona. Jeśli zapłaciłeś, odczekaj chwilę i naciśnij ponownie "
                "„✅ Zapłaciłem”."
            )


# --------- АДМИНКА ----------
def admin_keyboard() -> InlineKeyboardMarkup:
    kb = InlineKeyboardMarkup(row_width=1)
    kb.add(
        InlineKeyboardButton("👥 Активные подписки", callback_data="admin_active:1"),
        InlineKeyboardButton("⏳ Истекает ≤3 дней", callback_data="admin_expiring:1"),
        InlineKeyboardButton("❌ Просрочены", callback_data="admin_expired:1"),
        InlineKeyboardButton("👣 Все пользователи", callback_data="admin_users:1"),
        InlineKeyboardButton("🔎 Поиск (подсказка)", callback_data="admin_find_help"),
    )
    return kb

def _format_user_line(uid: str, rec: dict) -> str:
    u = rec or {}
    name = u.get("name") or "-"
    username = ("@" + u["username"]) if u.get("username") else "-"
    last_seen = datetime.fromtimestamp(u.get("last_seen", 0)).strftime("%Y-%m-%d %H:%M")
    active, end_date, days_left = _sub_status(int(uid))
    if active:
        sub = f"✅ do {end_date.strftime('%Y-%m-%d')} ({days_left} d.)"
    else:
        sub = f"— (was do {end_date.strftime('%Y-%m-%d')})" if end_date else "—"
    return f"<code>{uid}</code> | {username} | {name} | last: {last_seen} | sub: {sub}"

def _paginate(items, page: int, per_page: int = 10):
    total = max(1, (len(items) + per_page - 1) // per_page)
    page = max(1, min(page, total))
    start = (page - 1) * per_page
    return items[start:start+per_page], page, total

def _admin_nav(prefix: str, page: int, total: int, uids_for_links=None) -> InlineKeyboardMarkup:
    kb = InlineKeyboardMarkup(row_width=3)
    if uids_for_links:
        for uid in uids_for_links:
            kb.add(InlineKeyboardButton(f"🔗 {uid}", callback_data=f"admin_give:{uid}"))
    nav_row = []
    if page > 1:
        nav_row.append(InlineKeyboardButton("⬅️ Prev", callback_data=f"{prefix}:{page-1}"))
    if page < total:
        nav_row.append(InlineKeyboardButton("Next ➡️", callback_data=f"{prefix}:{page+1}"))
    if nav_row:
        kb.row(*nav_row)
    kb.add(InlineKeyboardButton("🏠 Панель", callback_data="admin_home"))
    return kb

@dp.message_handler(commands=["admin"])
async def admin_panel(message: types.Message):
    if message.from_user.id != ADMIN_ID:
        return
    track_user_from_message(message)
    total_users = len(db["users"])
    total_subs = len([1 for uid, end in db["subs"].items() if _sub_status(int(uid))[0]])
    await message.answer(
        f"👑 Админ-панель\n"
        f"👥 Активных подписок: <b>{total_subs}</b>\n"
        f"👣 Всего пользователей: <b>{total_users}</b>\n\n"
        f"Команды:\n"
        f"• /give_link &lt;user_id&gt; — выдать одноразовую ссылку пользователю\n"
        f"• /extend &lt;user_id&gt; [days] — продлить вручную (30 дней по умолч.)\n"
        f"• /revoke &lt;user_id&gt; — завершить доступ (кикнуть из канала)\n"
        f"• /who &lt;user_id&gt; — статус пользователя\n"
        f"• /find &lt;query&gt; — поиск по id/@username/имени",
        reply_markup=admin_keyboard()
    )

@dp.callback_query_handler(lambda c: c.data == "admin_home")
async def admin_home_cb(cb: types.CallbackQuery):
    if cb.from_user.id != ADMIN_ID:
        return await _cb_ack(cb)
    await _safe_edit_text(cb.message, "👑 Админ-панель", reply_markup=admin_keyboard())
    await _cb_ack(cb)

@dp.callback_query_handler(lambda c: c.data.startswith("admin_active:"))
async def admin_active_cb(cb: types.CallbackQuery):
    if cb.from_user.id != ADMIN_ID:
        return await _cb_ack(cb)
    page = int(cb.data.split(":")[1])
    active_uids = []
    for uid, _ in db["subs"].items():
        active, _, _ = _sub_status(int(uid))
        if active:
            active_uids.append(uid)
    active_uids.sort(key=lambda x: db["subs"].get(x, "9999-99-99"))
    slice_, page, total = _paginate(active_uids, page)

    lines = ["👥 Активные подписки:"]
    if not slice_:
        lines.append("— пусто —")
    else:
        for uid in slice_:
            lines.append(_format_user_line(uid, db["users"].get(uid)))
    text = "\n".join(lines)
    await _safe_edit_text(cb.message, text, reply_markup=_admin_nav("admin_active", page, total, uids_for_links=slice_))
    await _cb_ack(cb)

@dp.callback_query_handler(lambda c: c.data.startswith("admin_expiring:"))
async def admin_expiring_cb(cb: types.CallbackQuery):
    if cb.from_user.id != ADMIN_ID:
        return await _cb_ack(cb)
    page = int(cb.data.split(":")[1])
    expiring = []
    today = datetime.now().date()
    for uid, end_str in db["subs"].items():
        try:
            end_date = _parse_date(end_str)
            days_left = (end_date - today).days
            if 0 <= days_left <= 3:
                expiring.append(uid)
        except Exception:
            pass
    expiring.sort(key=lambda x: db["subs"].get(x, "9999-99-99"))
    slice_, page, total = _paginate(expiring, page)

    lines = ["⏳ Истекает ≤3 дней:"]
    if not slice_:
        lines.append("— пусто —")
    else:
        for uid in slice_:
            lines.append(_format_user_line(uid, db["users"].get(uid)))
    text = "\n".join(lines)
    await _safe_edit_text(cb.message, text, reply_markup=_admin_nav("admin_expiring", page, total, uids_for_links=slice_))
    await _cb_ack(cb)

@dp.callback_query_handler(lambda c: c.data.startswith("admin_expired:"))
async def admin_expired_cb(cb: types.CallbackQuery):
    if cb.from_user.id != ADMIN_ID:
        return await _cb_ack(cb)
    page = int(cb.data.split(":")[1])
    expired = []
    today = datetime.now().date()
    for uid, end_str in db["subs"].items():
        try:
            end_date = _parse_date(end_str)
            if end_date < today:
                expired.append(uid)
        except Exception:
            pass
    expired.sort(key=lambda x: db["subs"].get(x, "0000-00-00"), reverse=True)
    slice_, page, total = _paginate(expired, page)

    lines = ["❌ Просрочены:"]
    if not slice_:
        lines.append("— пусто —")
    else:
        for uid in slice_:
            lines.append(_format_user_line(uid, db["users"].get(uid)))
    text = "\n".join(lines)
    await _safe_edit_text(cb.message, text, reply_markup=_admin_nav("admin_expired", page, total, uids_for_links=slice_))
    await _cb_ack(cb)

# >>> NEW: Все пользователи (работает кнопка "👣 Все пользователи")
@dp.callback_query_handler(lambda c: c.data.startswith("admin_users:"))
async def admin_users_cb(cb: types.CallbackQuery):
    if cb.from_user.id != ADMIN_ID:
        return await _cb_ack(cb)
    page = int(cb.data.split(":")[1])

    # сортируем по last_seen (свежее — выше)
    uitems = []
    for uid, rec in db["users"].items():
        last = int(rec.get("last_seen", 0))
        uitems.append((uid, last))
    uitems.sort(key=lambda x: x[1], reverse=True)
    uids = [uid for uid, _ in uitems]

    slice_, page, total = _paginate(uids, page)

    lines = ["👣 Wszystkie osoby (wszystkie użytkownicy):"]
    if not slice_:
        lines.append("— пусто —")
    else:
        for uid in slice_:
            lines.append(_format_user_line(uid, db["users"].get(uid)))
    text = "\n".join(lines)

    await _safe_edit_text(
        cb.message,
        text,
        reply_markup=_admin_nav("admin_users", page, total, uids_for_links=slice_)
    )
    await _cb_ack(cb)

@dp.callback_query_handler(lambda c: c.data == "admin_find_help")
async def admin_find_help(cb: types.CallbackQuery):
    if cb.from_user.id != ADMIN_ID:
        return await _cb_ack(cb)
    await _safe_edit_text(
        cb.message,
        "🔎 Поиск:\n"
        "Используй команду:\n"
        "• <code>/find 123456789</code> — по user_id\n"
        "• <code>/find @username</code> — по username\n"
        "• <code>/find Jan</code> — подстрока в имени/username",
        reply_markup=_admin_nav("admin_users", 1, 1)
    )
    await _cb_ack(cb)

@dp.callback_query_handler(lambda c: c.data.startswith("admin_give:"))
async def admin_give_inline(cb: types.CallbackQuery):
    if cb.from_user.id != ADMIN_ID:
        return await _cb_ack(cb)
    await _cb_ack(cb)  # ранний ack
    uid = int(cb.data.split(":")[1])
    try:
        invite = await bot.create_chat_invite_link(
            chat_id=CHANNEL_ID,
            expire_date=int((datetime.now() + timedelta(days=1)).timestamp()),
            member_limit=1
        )
        kb = InlineKeyboardMarkup().add(InlineKeyboardButton("🔗 Dołącz do kanału", url=invite.invite_link))
        await bot.send_message(uid, "🔗 Ręczny dostęp do VIP_WAWA (ważny 1 dzień, jednorazowy):", reply_markup=kb)
        try:
            await cb.answer("Ссылка отправлена ✅", show_alert=False)
        except Exception:
            pass
    except Exception as e:
        try:
            await cb.answer(f"Ошибка: {e}", show_alert=True)
        except Exception:
            pass

# ----- Команды администратора -----
@dp.message_handler(commands=["give_link"])
async def admin_give_link(message: types.Message):
    if message.from_user.id != ADMIN_ID:
        return
    track_user_from_message(message)
    parts = message.text.strip().split()
    if len(parts) < 2 or not parts[1].isdigit():
        return await message.reply("⚠️ Использование: <code>/give_link &lt;user_id&gt;</code>", parse_mode=ParseMode.HTML)

    target_id = int(parts[1])
    try:
        invite = await bot.create_chat_invite_link(
            chat_id=CHANNEL_ID,
            expire_date=int((datetime.now() + timedelta(days=1)).timestamp()),
            member_limit=1
        )
        kb = InlineKeyboardMarkup().add(InlineKeyboardButton("🔗 Dołącz do kanału", url=invite.invite_link))
        await bot.send_message(target_id, "🔗 Ręczny доступ do VIP_WAWA (ważny 1 dzień, jednorazowy):", reply_markup=kb)
        await message.reply(f"✅ Ссылка отправлена пользователю <code>{target_id}</code>.", parse_mode=ParseMode.HTML)
    except Exception as e:
        await message.reply(f"❌ Не удалось отправить: <code>{e}</code>", parse_mode=ParseMode.HTML)

@dp.message_handler(commands=["extend"])
async def admin_extend(message: types.Message):
    if message.from_user.id != ADMIN_ID:
        return
    track_user_from_message(message)
    parts = message.text.strip().split()
    if len(parts) < 2 or not parts[1].isdigit():
        return await message.reply("⚠️ Использование: <code>/extend &lt;user_id&gt; [days]</code>", parse_mode=ParseMode.HTML)
    uid = int(parts[1])
    days = 30
    if len(parts) >= 3 and parts[2].isdigit():
        days = max(1, int(parts[2]))
    new_end = extend_30_days_from_current_or_today(get_sub_end(uid), days=days)
    set_sub_end(uid, new_end)
    try:
        await bot.send_message(uid, f"✅ Twoja subskrypcja została przedłużona o {days} dni.\n📅 Nowa data końca: <b>{new_end}</b>")
    except Exception:
        pass
    await message.reply(f"✅ Продлено пользователю <code>{uid}</code> на {days} дн. Новая дата: <b>{new_end}</b>", parse_mode=ParseMode.HTML)

@dp.message_handler(commands=["who"])
async def admin_who(message: types.Message):
    if message.from_user.id != ADMIN_ID:
        return
    track_user_from_message(message)
    parts = message.text.strip().split()
    if len(parts) < 2 or not parts[1].isdigit():
        return await message.reply("⚠️ Использование: <code>/who &lt;user_id&gt;</code>", parse_mode=ParseMode.HTML)

    uid = int(parts[1])
    rec = db["users"].get(str(uid), {})
    active, end_date, days_left = _sub_status(uid)
    sub_line = "нет"
    if active:
        sub_line = f"активна до {end_date.strftime('%Y-%m-%d')} ({days_left} дн.)"
    elif end_date:
        sub_line = f"была до {end_date.strftime('%Y-%m-%d')}, неактивна"

    text = (
        f"👤 Пользователь: <code>{uid}</code>\n"
        f"Имя: {rec.get('name','-')}\n"
        f"Username: {'@'+rec['username'] if rec.get('username') else '-'}\n"
        f"Подписка: {sub_line}\n"
        f"first_seen: {datetime.fromtimestamp(rec.get('first_seen',0)).strftime('%Y-%m-%d %H:%M') if rec.get('first_seen') else '-'}\n"
        f"last_seen: {datetime.fromtimestamp(rec.get('last_seen',0)).strftime('%Y-%m-%d %H:%M') if rec.get('last_seen') else '-'}"
    )
    await message.reply(text, parse_mode=ParseMode.HTML)

@dp.message_handler(commands=["find"])
async def admin_find(message: types.Message):
    if message.from_user.id != ADMIN_ID:
        return
    track_user_from_message(message)
    q = message.get_args().strip()
    if not q:
        return await message.reply("⚠️ Использование: <code>/find &lt;query&gt;</code> (user_id, @username или часть имени)", parse_mode=ParseMode.HTML)

    results = []
    if q.isdigit():
        if q in db["users"]:
            results = [q]
    elif q.startswith("@"):
        uname = q[1:].lower()
        for uid, rec in db["users"].items():
            if (rec.get("username") or "").lower() == uname:
                results.append(uid)
    else:
        needle = q.lower()
        for uid, rec in db["users"].items():
            name = (rec.get("name") or "").lower()
            uname = (rec.get("username") or "").lower()
            if needle in name or needle in uname:
                results.append(uid)

    if not results:
        return await message.reply("Ничего не найдено.", parse_mode=ParseMode.HTML)

    results = results[:50]
    lines = [f"🔎 Результаты поиска ({len(results)}):"]
    for uid in results[:20]:
        lines.append(_format_user_line(uid, db["users"].get(uid)))
    text = "\n".join(lines)
    kb = InlineKeyboardMarkup(row_width=3)
    for uid in results[:10]:
        kb.add(InlineKeyboardButton(f"🔗 {uid}", callback_data=f"admin_give:{uid}"))
    await message.reply(text, reply_markup=kb, parse_mode=ParseMode.HTML)

@dp.message_handler(commands=["revoke"])
async def admin_revoke(message: types.Message):
    if message.from_user.id != ADMIN_ID:
        return
    track_user_from_message(message)
    parts = message.text.strip().split()
    if len(parts) < 2 or not parts[1].isdigit():
        return await message.reply(
            "⚠️ Использование: <code>/revoke &lt;user_id&gt;</code>",
            parse_mode=ParseMode.HTML
        )
    uid = int(parts[1])

    # убрать из базы подписок
    db["subs"].pop(str(uid), None)
    save_db(db)

    # кикнуть из канала (и сразу разбанить, чтобы можно было снова приглашать)
    try:
        await bot.kick_chat_member(CHANNEL_ID, uid)
        await asyncio.sleep(1)
        await bot.unban_chat_member(CHANNEL_ID, uid)
    except Exception as e:
        await bot.send_message(ADMIN_ID, f"⚠️ Ошибка при кике {uid}: <code>{e}</code>")

    # уведомить пользователя
    try:
        await bot.send_message(uid, "❌ Twoja subskrypcja została zakończona przez administratora.")
    except Exception:
        pass

    await message.reply(
        f"✅ Подписка пользователя <code>{uid}</code> отозвана и он удалён из канала.",
        parse_mode=ParseMode.HTML
    )

# -------- Stripe webhook (автоматический путь) --------
async def stripe_webhook(request: web.Request):
    payload = await request.read()
    sig_header = request.headers.get("Stripe-Signature")

    try:
        event = stripe.Webhook.construct_event(payload, sig_header, STRIPE_WEBHOOK_SECRET)
    except stripe.error.SignatureVerificationError:
        return web.Response(status=400)

    if event.get("type") in ("checkout.session.completed", "checkout.session.async_payment_succeeded"):
        session = event["data"]["object"]
        # Idempotency guard: Stripe may retry or send multiple related events (completed + async_payment_succeeded)
        if _is_processed(session):
            return web.Response(status=200)
        user_id = session.get("metadata", {}).get("user_id")
        kind = session.get("metadata", {}).get("kind", "initial")
        if user_id:
            user_id_int = int(user_id)
            _mark_processed(session)
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
                if kind == "initial":
                    invite = await bot.create_chat_invite_link(
                        chat_id=CHANNEL_ID,
                        expire_date=int((datetime.now() + timedelta(days=1)).timestamp()),
                        member_limit=1
                    )
                    kb = InlineKeyboardMarkup().add(InlineKeyboardButton("🔗 Dołącz do kanału", url=invite.invite_link))
                    await bot.send_message(
                        user_id_int,
                        "✅ Płatność potwierdzona! Otrzymujesz доступ до kanału na 30 dni.\n"
                        f"📅 Data końca: <b>{new_end}</b>\n"
                        "Kliknij, aby dołączyć:",
                        reply_markup=kb
                    )
                else:
                    await bot.send_message(
                        user_id_int,
                        "✅ Płatność potwierdzona! Twoja subskrypcja została przedłużona o 30 dni.\n"
                        f"📅 Nowa data końca: <b>{new_end}</b>"
                    )
            except Exception:
                pass

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

# -------- Telegram: трекинг любых callback --------
@dp.callback_query_handler()
async def track_fallback(cb: types.CallbackQuery):
    await _cb_ack(cb)
    track_user_from_callback(cb)

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
    # закрываем aiohttp-сессию, чтобы не было Unclosed client session
    try:
        await bot.session.close()
    except Exception:
        pass

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