import asyncio
import html
import logging
import os
from decimal import Decimal, InvalidOperation

import aiosqlite
from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import Command, CommandStart, CommandObject
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message
from dotenv import load_dotenv

load_dotenv()
TOKEN = os.environ["BOT_TOKEN"]
ADMINS = {int(x) for x in os.getenv("ADMIN_IDS", "").replace(" ", "").split(",") if x}
COMM = Decimal(os.getenv("COMMISSION_PERCENT", "5"))
GUARANT = os.getenv("GUARANT_USERNAME", "@admin")
MARKET_NAME = os.getenv("MARKET_NAME", "NOT MRKT")
MARKET_PHOTO = os.getenv("MARKET_PHOTO", "").strip()
DEAL_PHOTO = os.getenv("DEAL_PHOTO", "").strip()
CURS = ["TON", "USDT", "STARS", "RUB", "UAH"]
Q = Decimal("0.000001")
STATUS = {
    "pending": "⏳ ждём вторую сторону",
    "paid": "💸 оплачено, ждём передачи NFT",
    "completed": "✅ завершена",
    "cancelled": "❌ отменена",
    "dispute": "⚠️ спор",
}

bot = Bot(TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
db: aiosqlite.Connection
lock = asyncio.Lock()  # все операции с деньгами идут под этим замком
r = Router()  # пользователи
ar = Router()  # админ
ar.message.filter(F.from_user.id.in_(ADMINS))
ar.callback_query.filter(F.from_user.id.in_(ADMINS))

SCHEMA = """
CREATE TABLE IF NOT EXISTS users(id INTEGER PRIMARY KEY, username TEXT, created TEXT DEFAULT CURRENT_TIMESTAMP);
CREATE TABLE IF NOT EXISTS balances(user_id INTEGER, cur TEXT, amount TEXT, PRIMARY KEY(user_id,cur));
CREATE TABLE IF NOT EXISTS deals(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  creator INTEGER, creator_role TEXT,          -- 'buyer' или 'seller'
  buyer INTEGER, seller INTEGER,
  title TEXT,
  buyer_link TEXT, seller_link TEXT,           -- ссылка на NFT с каждой стороны
  amount TEXT, cur TEXT, fee TEXT,
  status TEXT DEFAULT 'pending',
  created TEXT DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS topups(id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER, cur TEXT, amount TEXT, status TEXT DEFAULT 'pending', created TEXT DEFAULT CURRENT_TIMESTAMP);
CREATE TABLE IF NOT EXISTS withdrawals(id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER, cur TEXT, amount TEXT, details TEXT, status TEXT DEFAULT 'pending', created TEXT DEFAULT CURRENT_TIMESTAMP);
CREATE TABLE IF NOT EXISTS logs(id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT DEFAULT CURRENT_TIMESTAMP, actor INTEGER, action TEXT, details TEXT);
"""


# ---------- helpers ----------
async def fetch(sql, *a):
    async with db.execute(sql, a) as c:
        return await c.fetchall()


async def fetch1(sql, *a):
    async with db.execute(sql, a) as c:
        return await c.fetchone()


async def run(sql, *a):
    cur = await db.execute(sql, a)
    await db.commit()
    return cur


async def log(actor, action, details=""):
    await run("INSERT INTO logs(actor,action,details) VALUES(?,?,?)", actor, action, details)
    logging.info("%s %s %s", actor, action, details)


def esc(t):
    return html.escape(str(t))


def fmt(x):
    return format(Decimal(x).normalize(), "f")


def parse_amt(t, positive=True):
    try:
        d = Decimal(t.replace(",", ".").strip())
    except (InvalidOperation, AttributeError):
        return None
    if not d.is_finite() or abs(d) >= Decimal("1e12"):
        return None
    d = d.quantize(Q)
    if positive and d <= 0:
        return None
    return d


def kb(rows):
    return InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text=t, callback_data=d) for t, d in row] for row in rows]
    )


BACK = ("« Меню", "menu")
MENU = kb([
    [("🤝 Создать сделку", "newdeal")],
    [("📋 Мои сделки", "mydeals")],
    [("💰 Баланс", "balance")],
    [("⬇️ Пополнить", "dep"), ("⬆️ Вывести", "wd")],
])


def cur_kb(prefix):
    return kb([[(c, f"{prefix}:{c}") for c in CURS[:3]], [(c, f"{prefix}:{c}") for c in CURS[3:]], [BACK]])


async def show(c: CallbackQuery, text, markup=None):
    try:
        await c.message.edit_text(text, reply_markup=markup)
    except Exception:
        await c.message.answer(text, reply_markup=markup)
    await c.answer()


async def send_banner(chat_id, photo_url, text, markup=None):
    """Отправляет фото с подписью, если photo_url задан, иначе обычный текст."""
    if photo_url:
        try:
            return await bot.send_photo(chat_id, photo_url, caption=text, reply_markup=markup)
        except Exception:
            logging.warning("не удалось отправить фото %s, шлю текстом", photo_url)
    return await bot.send_message(chat_id, text, reply_markup=markup)


async def tell(uid, text, markup=None):
    try:
        await bot.send_message(uid, text, reply_markup=markup)
    except Exception:
        logging.warning("не удалось отправить сообщение %s", uid)


async def tell_admins(text, markup=None):
    for a in ADMINS:
        await tell(a, text, markup)


async def uname(uid):
    row = await fetch1("SELECT username FROM users WHERE id=?", uid)
    return f"@{row['username']}" if row and row["username"] else f"id{uid}"


async def me(uid, username):
    await run(
        "INSERT INTO users(id,username) VALUES(?,?) ON CONFLICT(id) DO UPDATE SET username=excluded.username",
        uid, username,
    )


async def bal(uid, cur):
    row = await fetch1("SELECT amount FROM balances WHERE user_id=? AND cur=?", uid, cur)
    return Decimal(row["amount"]) if row else Decimal(0)


async def _add(uid, cur, delta):  # вызывать только под lock
    new = await bal(uid, cur) + delta
    if new < 0:
        return False
    await run(
        "INSERT INTO balances(user_id,cur,amount) VALUES(?,?,?) "
        "ON CONFLICT(user_id,cur) DO UPDATE SET amount=excluded.amount",
        uid, cur, str(new),
    )
    return True


async def add_bal(uid, cur, delta):
    async with lock:
        return await _add(uid, cur, delta)


async def bot_username():
    me_ = await bot.get_me()
    return me_.username


def deal_text(d):
    text = (
        f"🤝 <b>Сделка #{d['id']}</b> — {STATUS[d['status']]}\n"
        f"🏷 {MARKET_NAME}\n"
        f"Предмет: {esc(d['title'])}\n"
        f"Сумма: {fmt(d['amount'])} {d['cur']}\n"
    )
    text += f"Ссылка покупателя: {esc(d['buyer_link']) if d['buyer_link'] else '— не указана —'}\n"
    text += f"Ссылка продавца: {esc(d['seller_link']) if d['seller_link'] else '— не указана —'}\n"
    return text


# ---------- старт ----------
@r.message(CommandStart())
async def start(m: Message, state: FSMContext, command: CommandObject):
    await state.clear()
    await me(m.from_user.id, m.from_user.username)
    arg = command.args
    if arg and arg.startswith("deal_"):
        try:
            did = int(arg[5:])
        except ValueError:
            did = None
        if did:
            return await open_invite(m, did)
    await send_banner(
        m.chat.id, MARKET_PHOTO,
        f"👋 <b>{MARKET_NAME}</b>\nОбмен NFT-подарков со встроенным гарантом. Все сделки проходят через менеджера.",
        MENU,
    )


@r.callback_query(F.data == "menu")
async def menu(c: CallbackQuery, state: FSMContext):
    await state.clear()
    await show(c, "Главное меню:", MENU)


@r.callback_query(F.data == "balance")
async def balance(c: CallbackQuery):
    lines = [f"• {cur}: {fmt(await bal(c.from_user.id, cur))}" for cur in CURS]
    await show(c, "💰 <b>Ваш баланс</b>\n" + "\n".join(lines) + f"\n\nВаш ID: <code>{c.from_user.id}</code>", kb([[BACK]]))


# ---------- создание сделки ----------
class NewDeal(StatesGroup):
    title = State()
    cur = State()
    amount = State()
    link = State()


@r.callback_query(F.data == "newdeal")
async def newdeal_role(c: CallbackQuery):
    await show(c, "Вы в этой сделке покупатель или продавец?", kb([
        [("🛒 Я покупаю", "role:buyer"), ("💼 Я продаю", "role:seller")],
        [BACK],
    ]))


@r.callback_query(F.data.startswith("role:"))
async def newdeal_title(c: CallbackQuery, state: FSMContext):
    await state.update_data(role=c.data.split(":")[1])
    await state.set_state(NewDeal.title)
    await show(c, "Опишите, на что сделка (например: Plush Pepe #123).")


@r.message(NewDeal.title)
async def deal_title_msg(m: Message, state: FSMContext):
    if not m.text or len(m.text) > 150:
        return await m.answer("Описание до 150 символов, текстом.")
    await state.update_data(title=m.text.strip())
    await state.set_state(NewDeal.cur)
    await m.answer("В какой валюте оплата?", reply_markup=cur_kb("dealcur"))


@r.callback_query(NewDeal.cur, F.data.startswith("dealcur:"))
async def deal_cur_cb(c: CallbackQuery, state: FSMContext):
    await state.update_data(cur=c.data.split(":")[1])
    await state.set_state(NewDeal.amount)
    await show(c, "Сумма сделки? (число)")


@r.message(NewDeal.amount)
async def deal_amount_msg(m: Message, state: FSMContext):
    a = parse_amt(m.text or "")
    if a is None:
        return await m.answer("Введите положительное число, например 9000")
    await state.update_data(amount=str(a))
    await state.set_state(NewDeal.link)
    await m.answer("Вставьте вашу ссылку на NFT (обязательно для обеих сторон).")


@r.message(NewDeal.link)
async def deal_link_msg(m: Message, state: FSMContext):
    link = (m.text or "").strip()
    if not link or len(link) > 300:
        return await m.answer("Нужна ссылка (до 300 символов), текстом.")
    d = await state.get_data()
    await state.clear()
    buyer = m.from_user.id if d["role"] == "buyer" else None
    seller = m.from_user.id if d["role"] == "seller" else None
    buyer_link = link if d["role"] == "buyer" else None
    seller_link = link if d["role"] == "seller" else None
    row = await run(
        "INSERT INTO deals(creator,creator_role,buyer,seller,title,buyer_link,seller_link,amount,cur,fee) "
        "VALUES(?,?,?,?,?,?,?,?,?,?)",
        m.from_user.id, d["role"], buyer, seller, d["title"], buyer_link, seller_link, d["amount"], d["cur"],
        str((Decimal(d["amount"]) * COMM / 100).quantize(Q)),
    )
    did = row.lastrowid
    uname_bot = await bot_username()
    invite = f"https://t.me/{uname_bot}?start=deal_{did}"
    await log(m.from_user.id, "deal_create", f"deal={did} {d['title']} {d['amount']} {d['cur']}")
    other = "продавцу" if d["role"] == "buyer" else "покупателю"
    await m.answer(
        f"✅ Сделка #{did} создана.\nОтправьте эту ссылку {other}:\n{invite}\n\n"
        "Как только он откроет её, вставит свою ссылку на NFT и примет сделку, оплата спишется автоматически.",
        reply_markup=kb([[("📋 Мои сделки", "mydeals")], [BACK]]),
    )
    await tell_admins(
        f"🆕 Открыта сделка #{did} ({MARKET_NAME})\n{esc(d['title'])} — {fmt(d['amount'])} {d['cur']}\nСоздал: {await uname(m.from_user.id)}",
        kb([[("Открыть", f"adeal:{did}")]]),
    )


async def open_invite(m: Message, did):
    d = await fetch1("SELECT * FROM deals WHERE id=?", did)
    if not d or d["status"] != "pending":
        return await m.answer("Эта сделка недоступна (уже принята, отменена или не существует).", reply_markup=MENU)
    if d["creator"] == m.from_user.id:
        return await m.answer("Это ваша собственная сделка, поделитесь ссылкой со второй стороной.", reply_markup=MENU)

    my_role = "seller" if d["creator_role"] == "buyer" else "buyer"
    my_link = d["seller_link"] if my_role == "seller" else d["buyer_link"]
    text = deal_text(d) + f"\nВы вступаете в роли: {'продавец' if my_role=='seller' else 'покупатель'}"

    if not my_link:
        await fsm_storage_set(m.from_user.id, m.chat.id, did, my_role)
        return await send_banner(m.chat.id, DEAL_PHOTO, text + "\n\nЧтобы продолжить, вставьте вашу ссылку на NFT.")

    rows = [[("✅ Принять сделку", f"accept:{did}")], [("« Меню", "menu")]]
    await send_banner(m.chat.id, DEAL_PHOTO, text, kb(rows))


# простое FSM через ключ состояния на уровне aiogram Storage недоступно вне хэндлера с state,
# поэтому ждём ссылку обычным хэндлером с личным состоянием в таблице users не нужно —
# используем общий Dispatcher FSM через контекст при следующем сообщении пользователя.
_waiting_link: dict[int, tuple[int, str]] = {}  # user_id -> (deal_id, role)


async def fsm_storage_set(user_id, chat_id, did, role):
    _waiting_link[user_id] = (did, role)


@r.message(F.text, F.from_user.id.func(lambda uid: uid in _waiting_link))
async def waitlink_msg(m: Message):
    did, role = _waiting_link.pop(m.from_user.id)
    link = (m.text or "").strip()
    if not link or len(link) > 300:
        _waiting_link[m.from_user.id] = (did, role)
        return await m.answer("Нужна ссылка (до 300 символов), текстом.")
    col = "seller_link" if role == "seller" else "buyer_link"
    await run(f"UPDATE deals SET {col}=? WHERE id=?", link, did)
    await m.answer("Ссылка сохранена.")
    await open_invite(m, did)


@r.callback_query(F.data.startswith("accept:"))
async def accept_deal(c: CallbackQuery):
    uid, did = c.from_user.id, int(c.data[7:])
    async with lock:
        d = await fetch1("SELECT * FROM deals WHERE id=?", did)
        if not d or d["status"] != "pending" or d["creator"] == uid:
            return await c.answer("Сделка недоступна", show_alert=True)
        buyer = d["buyer"] or uid
        seller = d["seller"] or uid
        if buyer == seller:
            return await c.answer("Ошибка ролей сделки", show_alert=True)
        if not d["buyer_link"] or not d["seller_link"]:
            return await c.answer("Сначала обе стороны должны указать ссылку на NFT", show_alert=True)
        amount = Decimal(d["amount"])
        if not await _add(buyer, d["cur"], -amount):
            await c.answer("У покупателя не хватает баланса. Сначала пополните баланс.", show_alert=True)
            return
        await run("UPDATE deals SET buyer=?, seller=?, status='paid' WHERE id=?", buyer, seller, did)
        d = await fetch1("SELECT * FROM deals WHERE id=?", did)
    await log(uid, "deal_accept_paid", f"deal={did} buyer={buyer} seller={seller}")
    await show(c, f"💸 Оплата прошла. Деньги заморожены у гаранта.\nСвяжитесь с менеджером для передачи NFT: {esc(GUARANT)}", kb([[("📋 Мои сделки", "mydeals")]]))
    await tell(buyer, f"💸 Сделка #{did}: оплата {fmt(amount)} {d['cur']} списана с баланса и заморожена у гаранта.\nМенеджер для передачи NFT: {esc(GUARANT)}")
    await tell(seller, f"💸 Сделка #{did}: покупатель оплатил {fmt(amount)} {d['cur']}. Деньги у гаранта.\nПередайте NFT менеджеру: {esc(GUARANT)}")
    await tell_admins(f"💸 Сделка #{did} оплачена, ждём передачи NFT.", kb([[("Открыть", f"adeal:{did}")]]))


# ---------- мои сделки ----------
@r.callback_query(F.data == "mydeals")
async def mydeals(c: CallbackQuery):
    uid = c.from_user.id
    rows = await fetch("SELECT * FROM deals WHERE creator=? OR buyer=? OR seller=? ORDER BY id DESC LIMIT 20", uid, uid, uid)
    if not rows:
        return await show(c, "Сделок пока нет.", kb([[BACK]]))
    btns = [[(f"#{d['id']} {d['title']} — {STATUS[d['status']]}", f"udeal:{d['id']}")] for d in rows]
    await show(c, "📋 Ваши сделки:", kb(btns + [[BACK]]))


@r.callback_query(F.data.startswith("udeal:"))
async def udeal(c: CallbackQuery):
    uid = c.from_user.id
    d = await fetch1("SELECT * FROM deals WHERE id=?", int(c.data[6:]))
    if not d or uid not in (d["creator"], d["buyer"], d["seller"]):
        return await c.answer("Нет доступа", show_alert=True)
    text = deal_text(d)
    rows = []
    if d["status"] == "pending" and d["creator"] == uid:
        uname_bot = await bot_username()
        text += f"\nСсылка для второй стороны:\nhttps://t.me/{uname_bot}?start=deal_{d['id']}"
        rows.append([("❌ Отменить сделку", f"ucancel:{d['id']}")])
    if d["status"] == "paid":
        rows.append([("⚠️ Открыть спор", f"dispute:{d['id']}")])
    await show(c, text, kb(rows + [[("« Сделки", "mydeals")]]))


@r.callback_query(F.data.startswith("ucancel:"))
async def ucancel(c: CallbackQuery):
    did = int(c.data[8:])
    cur = await run("UPDATE deals SET status='cancelled' WHERE id=? AND status='pending' AND creator=?", did, c.from_user.id)
    if not cur.rowcount:
        return await c.answer("Нельзя отменить", show_alert=True)
    await log(c.from_user.id, "deal_cancel_by_creator", f"deal={did}")
    await c.answer("Сделка отменена", show_alert=True)
    await mydeals(c)


@r.callback_query(F.data.startswith("dispute:"))
async def dispute(c: CallbackQuery):
    uid, did = c.from_user.id, int(c.data[8:])
    cur = await run("UPDATE deals SET status='dispute' WHERE id=? AND status='paid' AND (buyer=? OR seller=?)", did, uid, uid)
    if not cur.rowcount:
        return await c.answer("Нельзя открыть спор", show_alert=True)
    await log(uid, "dispute", f"deal={did}")
    await c.answer("Спор открыт, менеджер свяжется с вами", show_alert=True)
    await tell_admins(f"⚠️ Спор по сделке #{did} от {await uname(uid)}", kb([[("Открыть", f"adeal:{did}")]]))


# ---------- пополнение / вывод ----------
class Dep(StatesGroup):
    amount = State()


class Wd(StatesGroup):
    amount = State()
    details = State()


@r.callback_query(F.data == "dep")
async def dep(c: CallbackQuery):
    await show(c, "⬇️ Пополнение. Выберите валюту:", cur_kb("depcur"))


@r.callback_query(F.data.startswith("depcur:"))
async def depcur(c: CallbackQuery, state: FSMContext):
    await state.set_state(Dep.amount)
    await state.update_data(cur=c.data.split(":")[1])
    await show(c, "Сколько хотите внести? (число)")


@r.message(Dep.amount)
async def dep_amount(m: Message, state: FSMContext):
    a = parse_amt(m.text or "")
    if a is None:
        return await m.answer("Введите положительное число.")
    cur = (await state.get_data())["cur"]
    await state.clear()
    t = await run("INSERT INTO topups(user_id,cur,amount) VALUES(?,?,?)", m.from_user.id, cur, str(a))
    req = os.getenv(f"DEPOSIT_{cur}", f"уточните реквизиты у менеджера {GUARANT}")
    await log(m.from_user.id, "topup_request", f"topup={t.lastrowid} {a} {cur}")
    await m.answer(
        f"Заявка #{t.lastrowid}: {fmt(a)} {cur}\n\nРеквизиты: <code>{esc(req)}</code>\nВ комментарии укажите ваш ID: <code>{m.from_user.id}</code>\n\n"
        "После перевода баланс пополнит менеджер, когда деньги придут.",
        reply_markup=kb([[BACK]]),
    )
    await tell_admins(
        f"⬇️ Заявка на пополнение #{t.lastrowid}\n{await uname(m.from_user.id)} (id{m.from_user.id}): {fmt(a)} {cur}",
        kb([[("✅ Зачислить", f"tu_ok:{t.lastrowid}"), ("❌ Отклонить", f"tu_no:{t.lastrowid}")]]),
    )


@r.callback_query(F.data == "wd")
async def wd(c: CallbackQuery):
    await show(c, "⬆️ Вывод. Выберите валюту:", cur_kb("wdcur"))


@r.callback_query(F.data.startswith("wdcur:"))
async def wdcur(c: CallbackQuery, state: FSMContext):
    cur = c.data.split(":")[1]
    await state.set_state(Wd.amount)
    await state.update_data(cur=cur)
    await show(c, f"Доступно: {fmt(await bal(c.from_user.id, cur))} {cur}\nСколько вывести?")


@r.message(Wd.amount)
async def wd_amount(m: Message, state: FSMContext):
    a = parse_amt(m.text or "")
    cur = (await state.get_data())["cur"]
    if a is None or a > await bal(m.from_user.id, cur):
        return await m.answer("Некорректная сумма или недостаточно средств.")
    await state.update_data(amount=str(a))
    await state.set_state(Wd.details)
    await m.answer("Куда отправить? (адрес кошелька / номер карты)")


@r.message(Wd.details)
async def wd_details(m: Message, state: FSMContext):
    d = await state.get_data()
    await state.clear()
    a, cur = Decimal(d["amount"]), d["cur"]
    if not await add_bal(m.from_user.id, cur, -a):
        return await m.answer("Недостаточно средств.", reply_markup=kb([[BACK]]))
    w = await run("INSERT INTO withdrawals(user_id,cur,amount,details) VALUES(?,?,?,?)", m.from_user.id, cur, str(a), (m.text or "")[:300])
    await log(m.from_user.id, "withdraw_request", f"wd={w.lastrowid} {a} {cur}")
    await m.answer(f"Заявка на вывод #{w.lastrowid} создана, сумма заморожена до обработки.", reply_markup=kb([[BACK]]))
    await tell_admins(
        f"⬆️ Вывод #{w.lastrowid}\n{await uname(m.from_user.id)}: {fmt(a)} {cur}\nКуда: <code>{esc(m.text)}</code>",
        kb([[("✅ Выплачено", f"wd_ok:{w.lastrowid}"), ("❌ Отклонить", f"wd_no:{w.lastrowid}")]]),
    )


# ---------- админ ----------
ADMIN_MENU = kb([
    [("🤝 Открытые сделки", "a_deals")],
    [("⬇️ Пополнения", "a_tu"), ("⬆️ Выводы", "a_wd")],
    [("📜 Логи", "a_logs")],
])


@ar.message(Command("admin"))
async def admin(m: Message):
    await m.answer(f"🛠 Админ-панель {MARKET_NAME}\n/user 123456789 — баланс и сделки\n/credit 123456789 TON 10 — ручная корректировка", reply_markup=ADMIN_MENU)


@ar.callback_query(F.data == "admin")
async def admin_cb(c: CallbackQuery):
    await show(c, "🛠 Админ-панель", ADMIN_MENU)


@ar.message(Command("user"))
async def user_info(m: Message):
    parts = (m.text or "").split()
    if len(parts) != 2 or not parts[1].isdigit():
        return await m.answer("Формат: /user 123456789")
    uid = int(parts[1])
    lines = [f"• {cur}: {fmt(await bal(uid, cur))}" for cur in CURS]
    n = (await fetch1("SELECT COUNT(*) n FROM deals WHERE buyer=? OR seller=?", uid, uid))["n"]
    await m.answer(f"👤 {await uname(uid)} (id{uid})\n" + "\n".join(lines) + f"\nСделок: {n}")


@ar.message(Command("credit"))
async def credit(m: Message):
    p = (m.text or "").split()
    amt = parse_amt(p[3], positive=False) if len(p) == 4 else None
    if amt is None or not p[1].isdigit() or p[2].upper() not in CURS:
        return await m.answer("Формат: /credit 123456789 TON 10 (с минусом — списание)")
    ok = await add_bal(int(p[1]), p[2].upper(), amt)
    await log(m.from_user.id, "manual_credit", f"user={p[1]} {amt} {p[2].upper()} ok={ok}")
    await m.answer("Готово" if ok else "Недостаточно средств для списания")


@ar.callback_query(F.data == "a_deals")
async def a_deals(c: CallbackQuery):
    rows = await fetch("SELECT * FROM deals WHERE status IN ('pending','paid','dispute') ORDER BY id DESC LIMIT 30")
    btns = [[(f"#{d['id']} {d['title']} — {STATUS[d['status']]}", f"adeal:{d['id']}")] for d in rows]
    await show(c, "🤝 Открытые сделки:" if rows else "Нет открытых сделок.", kb(btns + [[("« Админка", "admin")]]))


async def adeal_view(c: CallbackQuery, did):
    d = await fetch1("SELECT * FROM deals WHERE id=?", did)
    if not d:
        return await c.answer("Не найдено", show_alert=True)
    text = (
        f"🤝 <b>Сделка #{d['id']}</b> — {STATUS[d['status']]}\n{esc(d['title'])}\n"
        f"Ссылка покупателя: {esc(d['buyer_link']) if d['buyer_link'] else '—'}\n"
        f"Ссылка продавца: {esc(d['seller_link']) if d['seller_link'] else '—'}\n"
        f"Сумма: {fmt(d['amount'])} {d['cur']}, комиссия: {fmt(d['fee'])}\n"
        f"Покупатель: {await uname(d['buyer']) if d['buyer'] else '—'}\nПродавец: {await uname(d['seller']) if d['seller'] else '—'}"
    )
    rows = []
    if d["status"] in ("paid", "dispute"):
        rows.append([("✅ NFT передан — завершить", f"d_done:{did}")])
    if d["status"] in ("pending", "paid", "dispute"):
        rows.append([("❌ Отменить (вернуть деньги, если списаны)", f"d_cancel:{did}")])
    await show(c, text, kb(rows + [[("« Сделки", "a_deals")]]))


@ar.callback_query(F.data.startswith("adeal:"))
async def adeal(c: CallbackQuery):
    await adeal_view(c, int(c.data[6:]))


@ar.callback_query(F.data.startswith("d_done:"))
async def d_done(c: CallbackQuery):
    did = int(c.data[7:])
    async with lock:
        cur = await run("UPDATE deals SET status='completed' WHERE id=? AND status IN ('paid','dispute')", did)
        if not cur.rowcount:
            return await c.answer("Статус уже изменился", show_alert=True)
        d = await fetch1("SELECT * FROM deals WHERE id=?", did)
        await _add(d["seller"], d["cur"], Decimal(d["amount"]) - Decimal(d["fee"]))
    await log(c.from_user.id, "deal_done", f"deal={did}")
    await tell(d["buyer"], f"✅ Сделка #{did} завершена, NFT передан вам.")
    await tell(d["seller"], f"✅ Сделка #{did} завершена. Зачислено {fmt(Decimal(d['amount']) - Decimal(d['fee']))} {d['cur']}.")
    await adeal_view(c, did)


@ar.callback_query(F.data.startswith("d_cancel:"))
async def d_cancel(c: CallbackQuery):
    did = int(c.data[9:])
    async with lock:
        before = await fetch1("SELECT * FROM deals WHERE id=?", did)
        if not before or before["status"] not in ("pending", "paid", "dispute"):
            return await c.answer("Статус уже изменился", show_alert=True)
        cur = await run("UPDATE deals SET status='cancelled' WHERE id=? AND status=?", did, before["status"])
        if not cur.rowcount:
            return await c.answer("Статус уже изменился", show_alert=True)
        refunded = False
        if before["status"] in ("paid", "dispute"):
            # деньги уже были списаны с покупателя при принятии сделки — возвращаем их
            await _add(before["buyer"], before["cur"], Decimal(before["amount"]))
            refunded = True
    await log(c.from_user.id, "deal_cancel", f"deal={did} refunded={refunded}")
    if before["buyer"]:
        msg = f"❌ Сделка #{did} отменена менеджером."
        if refunded:
            msg += f" Деньги ({fmt(before['amount'])} {before['cur']}) возвращены на баланс."
        await tell(before["buyer"], msg)
    if before["seller"] and before["seller"] != before["buyer"]:
        await tell(before["seller"], f"❌ Сделка #{did} отменена менеджером.")
    await adeal_view(c, did)


@ar.callback_query(F.data == "a_tu")
async def a_tu(c: CallbackQuery):
    rows = await fetch("SELECT * FROM topups WHERE status='pending' ORDER BY id LIMIT 20")
    if not rows:
        return await show(c, "Нет заявок на пополнение.", kb([[("« Админка", "admin")]]))
    btns = [[(f"✅ #{t['id']} id{t['user_id']} {fmt(t['amount'])} {t['cur']}", f"tu_ok:{t['id']}"), ("❌", f"tu_no:{t['id']}")] for t in rows]
    await show(c, "⬇️ Заявки на пополнение:", kb(btns + [[("« Админка", "admin")]]))


@ar.callback_query(F.data.startswith("tu_ok:"))
async def tu_ok(c: CallbackQuery):
    tid = int(c.data[6:])
    async with lock:
        cur = await run("UPDATE topups SET status='done' WHERE id=? AND status='pending'", tid)
        if not cur.rowcount:
            return await c.answer("Уже обработано", show_alert=True)
        t = await fetch1("SELECT * FROM topups WHERE id=?", tid)
        await _add(t["user_id"], t["cur"], Decimal(t["amount"]))
    await log(c.from_user.id, "topup_ok", f"topup={tid} user={t['user_id']} {t['amount']} {t['cur']}")
    await tell(t["user_id"], f"✅ Баланс пополнен на {fmt(t['amount'])} {t['cur']}")
    await c.answer("Зачислено", show_alert=True)


@ar.callback_query(F.data.startswith("tu_no:"))
async def tu_no(c: CallbackQuery):
    tid = int(c.data[6:])
    cur = await run("UPDATE topups SET status='rejected' WHERE id=? AND status='pending'", tid)
    if not cur.rowcount:
        return await c.answer("Уже обработано", show_alert=True)
    t = await fetch1("SELECT * FROM topups WHERE id=?", tid)
    await log(c.from_user.id, "topup_reject", f"topup={tid}")
    await tell(t["user_id"], f"❌ Заявка на пополнение #{tid} отклонена. Свяжитесь с менеджером {esc(GUARANT)}.")
    await c.answer("Отклонено", show_alert=True)


@ar.callback_query(F.data == "a_wd")
async def a_wd(c: CallbackQuery):
    rows = await fetch("SELECT * FROM withdrawals WHERE status='pending' ORDER BY id LIMIT 20")
    if not rows:
        return await show(c, "Нет заявок на вывод.", kb([[("« Админка", "admin")]]))
    text = "\n".join(f"#{w['id']} id{w['user_id']}: {fmt(w['amount'])} {w['cur']} → <code>{esc(w['details'])}</code>" for w in rows)
    btns = [[(f"✅ #{w['id']} выплачено", f"wd_ok:{w['id']}"), ("❌", f"wd_no:{w['id']}")] for w in rows]
    await show(c, "⬆️ Заявки на вывод:\n" + text, kb(btns + [[("« Админка", "admin")]]))


@ar.callback_query(F.data.startswith("wd_ok:"))
async def wd_ok(c: CallbackQuery):
    wid = int(c.data[6:])
    cur = await run("UPDATE withdrawals SET status='paid' WHERE id=? AND status='pending'", wid)
    if not cur.rowcount:
        return await c.answer("Уже обработано", show_alert=True)
    w = await fetch1("SELECT * FROM withdrawals WHERE id=?", wid)
    await log(c.from_user.id, "withdraw_paid", f"wd={wid}")
    await tell(w["user_id"], f"✅ Вывод #{wid} выплачен: {fmt(w['amount'])} {w['cur']}")
    await c.answer("Отмечено как выплачено", show_alert=True)


@ar.callback_query(F.data.startswith("wd_no:"))
async def wd_no(c: CallbackQuery):
    wid = int(c.data[6:])
    async with lock:
        cur = await run("UPDATE withdrawals SET status='rejected' WHERE id=? AND status='pending'", wid)
        if not cur.rowcount:
            return await c.answer("Уже обработано", show_alert=True)
        w = await fetch1("SELECT * FROM withdrawals WHERE id=?", wid)
        await _add(w["user_id"], w["cur"], Decimal(w["amount"]))
    await log(c.from_user.id, "withdraw_reject", f"wd={wid}")
    await tell(w["user_id"], f"❌ Вывод #{wid} отклонён, деньги вернулись на баланс.")
    await c.answer("Отклонено, деньги возвращены", show_alert=True)


@ar.callback_query(F.data == "a_logs")
async def a_logs(c: CallbackQuery):
    rows = await fetch("SELECT * FROM logs ORDER BY id DESC LIMIT 30")
    text = "\n".join(f"{l['ts'][5:16]} id{l['actor']} {l['action']} {esc(l['details'])}" for l in rows) or "Логов нет."
    await show(c, "📜 <b>Последние действия</b>\n" + text, kb([[("« Админка", "admin")]]))


# ---------- запуск ----------
async def main():
    global db
    logging.basicConfig(level=logging.INFO, handlers=[logging.StreamHandler(), logging.FileHandler("bot.log", encoding="utf-8")])
    db = await aiosqlite.connect("bot.db")
    db.row_factory = aiosqlite.Row
    await db.executescript(SCHEMA)
    dp = Dispatcher()
    dp.include_routers(ar, r)
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
