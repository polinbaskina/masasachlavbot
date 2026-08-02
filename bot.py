import asyncio
import io
import logging
import os
import re
import zipfile

import qrcode
from aiogram import Bot, Dispatcher, F
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import Command, CommandStart, CommandObject
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    Message,
    BufferedInputFile,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
    CallbackQuery,
)

import database as db

logging.basicConfig(level=logging.INFO)

BOT_TOKEN = os.environ["BOT_TOKEN"]
BOT_USERNAME = os.environ["BOT_USERNAME"]  # без @, например graduation2026_bot
ADMIN_IDS = {
    int(x) for x in os.environ.get("ADMIN_CHAT_IDS", "").replace(" ", "").split(",") if x
}

bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
dp = Dispatcher(storage=MemoryStorage())


class GuestFlow(StatesGroup):
    choosing_identity = State()
    writing_message = State()


def is_admin(user_id: int) -> bool:
    return user_id in ADMIN_IDS


from PIL import Image, ImageDraw, ImageFont

FONT_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fonts", "DejaVuSans-Bold.ttf")


def _fit_font(text: str, max_width: int, max_size=32, min_size=14):
    size = max_size
    while size > min_size:
        font = ImageFont.truetype(FONT_PATH, size)
        bbox = font.getbbox(text)
        if (bbox[2] - bbox[0]) <= max_width:
            return font
        size -= 2
    return ImageFont.truetype(FONT_PATH, min_size)


def _label_lines(label: str, max_width: int):
    """Returns [(line_text, font, bbox), ...] — one line, or two if it doesn't fit at min size."""
    font = _fit_font(label, max_width)
    bbox = font.getbbox(label)
    if (bbox[2] - bbox[0]) <= max_width:
        return [(label, font, bbox)]

    words = label.split()
    mid = max(1, len(words) // 2)
    lines = [" ".join(words[:mid]), " ".join(words[mid:])] if len(words) > 1 else [label]
    result = []
    for line in lines:
        f = _fit_font(line, max_width)
        result.append((line, f, f.getbbox(line)))
    return result


def build_qr_png_bytes(link: str, label: str) -> bytes:
    """QR code with the person's name printed underneath, ready for printing on badges."""
    qr = qrcode.QRCode(box_size=10, border=2)
    qr.add_data(link)
    qr.make(fit=True)
    qr_img = qr.make_image(fill_color="black", back_color="white").convert("RGB")

    padding = 20
    max_text_width = qr_img.width - padding * 2
    lines = _label_lines(label.strip(), max_text_width)
    line_gap = 8

    line_heights = [(bbox[3] - bbox[1]) for _, _, bbox in lines]
    text_block_height = sum(line_heights) + line_gap * (len(lines) - 1)

    canvas = Image.new(
        "RGB", (qr_img.width, qr_img.height + text_block_height + padding * 2), "white"
    )
    canvas.paste(qr_img, (0, 0))
    draw = ImageDraw.Draw(canvas)

    y = qr_img.height + padding
    for line, font, bbox in lines:
        w = bbox[2] - bbox[0]
        x = (qr_img.width - w) // 2
        draw.text((x, y), line, font=font, fill="black")
        y += (bbox[3] - bbox[1]) + line_gap

    buf = io.BytesIO()
    canvas.save(buf, format="PNG")
    return buf.getvalue()


def build_qr(link: str, label: str) -> BufferedInputFile:
    png_bytes = build_qr_png_bytes(link, label)
    return BufferedInputFile(png_bytes, filename="qr.png")


def _safe_filename(name: str) -> str:
    slug = re.sub(r"[^\w\- ]", "", name, flags=re.UNICODE).strip().replace(" ", "_")
    return slug or "qr"


def build_qr_zip(graduates) -> bytes:
    """graduates: iterable of dicts/rows with 'name' and 'guest_code'. Returns a zip file's
    bytes, one PNG per person (QR + printed name), named after them, ready for printing."""
    zip_buf = io.BytesIO()
    used_names = set()
    with zipfile.ZipFile(zip_buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for g in graduates:
            link = guest_link(g["guest_code"])
            png_bytes = build_qr_png_bytes(link, g["name"])
            base = _safe_filename(g["name"])
            filename = base
            i = 2
            while filename in used_names:
                filename = f"{base}_{i}"
                i += 1
            used_names.add(filename)
            zf.writestr(f"{filename}.png", png_bytes)
    zip_buf.seek(0)
    return zip_buf.getvalue()


def guest_link(guest_code: str) -> str:
    return f"https://t.me/{BOT_USERNAME}?start={guest_code}"


def owner_link(owner_code: str) -> str:
    return f"https://t.me/{BOT_USERNAME}?start={owner_code}"


async def send_qr(chat_id: int, guest_code: str, name: str):
    link = guest_link(guest_code)
    qr_file = build_qr(link, name)
    await bot.send_photo(
        chat_id,
        qr_file,
        caption=(
            f"Готово, {name}! 🎓\n\n"
            f"Вот твой личный QR-код для выпускного.\n"
            f"Покажи его друзьям и знакомым — отсканировав, они смогут "
            f"написать тебе пожелание прямо через этого бота.\n\n"
            f"Сохрани картинку или распечатай её.\n"
            f"Ссылка на всякий случай: {link}\n\n"
            f"Позже все полученные письма можно перечитать командой /letters"
        ),
    )


# ---------- Deep-link entry point (owner activation OR guest writing) ----------

@dp.message(CommandStart(deep_link=True))
async def start_with_code(message: Message, command: CommandObject, state: FSMContext):
    code = command.args or ""

    if code.startswith("o_"):
        await handle_owner_activation(message, code)
        return

    if code.startswith("g_"):
        await handle_guest_scan(message, code, state)
        return

    await message.answer("Не узнаю этот код 🤔 Попробуй отсканировать QR ещё раз.")


async def handle_owner_activation(message: Message, owner_code: str):
    graduate, error = db.activate_graduate(owner_code, message.from_user.id)

    if error == "not_found":
        await message.answer(
            "Такой персональной ссылки нет в списке. Уточни у организатора."
        )
        return
    if error == "already_claimed":
        await message.answer(
            "Эта ссылка уже была активирована другим аккаунтом. "
            "Если это ошибка — напиши организатору."
        )
        return
    if error == "user_has_other_profile":
        await message.answer(
            "Похоже, ты уже активировал(а) другую персональную ссылку в этом боте. "
            "Если это ошибка — напиши организатору."
        )
        return

    await send_qr(message.from_user.id, graduate["guest_code"], graduate["name"])


async def handle_guest_scan(message: Message, guest_code: str, state: FSMContext):
    graduate = db.get_graduate_by_guest_code(guest_code)
    if not graduate:
        await message.answer("Хм, не нахожу такой QR-код 🤔 Попробуй отсканировать заново.")
        return

    if graduate["chat_id"] == message.from_user.id:
        await message.answer("Это твой собственный QR-код 🙂 Покажи его кому-нибудь другому!")
        return

    await state.update_data(target_graduate_id=graduate["id"], target_name=graduate["name"])

    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="Подписаться своим именем", callback_data="named")],
            [InlineKeyboardButton(text="Написать анонимно", callback_data="anon")],
        ]
    )
    await message.answer(
        f"Ты пишешь пожелание для <b>{graduate['name']}</b> 🎓\n\n"
        f"Как подписать сообщение?",
        reply_markup=kb,
    )
    await state.set_state(GuestFlow.choosing_identity)


@dp.callback_query(GuestFlow.choosing_identity, F.data.in_({"named", "anon"}))
async def choose_identity(callback: CallbackQuery, state: FSMContext):
    is_anon = callback.data == "anon"
    await state.update_data(is_anonymous=is_anon)
    await callback.message.edit_text(
        "Пиши текст пожелания (можно приложить фото с подписью). "
        "Когда закончишь — просто отправь сообщение."
    )
    await state.set_state(GuestFlow.writing_message)
    await callback.answer()


@dp.message(GuestFlow.writing_message)
async def receive_guest_message(message: Message, state: FSMContext):
    data = await state.get_data()
    graduate_id = data["target_graduate_id"]
    target_name = data["target_name"]
    is_anon = data.get("is_anonymous", False)

    sender_name = "Аноним" if is_anon else (message.from_user.full_name or "Гость")
    text = message.caption if message.photo else message.text
    photo_file_id = message.photo[-1].file_id if message.photo else None

    if not text and not photo_file_id:
        await message.answer("Похоже, сообщение пустое — напиши текст или пришли фото с подписью.")
        return

    db.save_message(
        graduate_id=graduate_id,
        sender_chat_id=message.from_user.id,
        sender_name=sender_name,
        is_anonymous=is_anon,
        text=text,
        photo_file_id=photo_file_id,
    )

    graduate_row = db.get_graduate_by_id(graduate_id)

    caption_prefix = f"💌 Новое пожелание от <b>{sender_name}</b>:\n\n"
    if graduate_row["chat_id"]:
        try:
            if photo_file_id:
                await bot.send_photo(
                    graduate_row["chat_id"], photo_file_id, caption=caption_prefix + (text or "")
                )
            else:
                await bot.send_message(graduate_row["chat_id"], caption_prefix + text)
        except Exception:
            logging.exception("Failed to deliver message live, it's still saved in /letters")

    await message.answer(f"Готово! Пожелание для {target_name} отправлено 🎉")
    await state.clear()


# ---------- Plain /start (no payload) ----------

@dp.message(CommandStart())
async def start_plain(message: Message):
    existing = db.get_graduate_by_chat_id(message.from_user.id)
    if existing:
        await send_qr(message.from_user.id, existing["guest_code"], existing["name"])
        return

    await message.answer(
        "Привет! Это бот пожеланий для выпускного 🎓\n\n"
        "Я тебя пока не узнаю — организатор ещё не добавил твой Telegram ID в список "
        "выпускников. Напиши организатору, пусть добавит тебя, и попробуй /start ещё раз."
    )


@dp.message(Command("myqr"))
async def myqr(message: Message):
    graduate = db.get_graduate_by_chat_id(message.from_user.id)
    if not graduate:
        await message.answer("Тебя пока нет в списке выпускников, или ты ещё не писал(а) мне /start. Уточни у организатора.")
        return
    await send_qr(message.from_user.id, graduate["guest_code"], graduate["name"])


@dp.message(Command("letters"))
async def letters(message: Message):
    graduate = db.get_graduate_by_chat_id(message.from_user.id)
    if not graduate:
        await message.answer("Тебя пока нет в списке выпускников, или ты ещё не писал(а) мне /start. Уточни у организатора.")
        return

    msgs = db.get_messages_for_graduate(graduate["id"])
    if not msgs:
        await message.answer("Пока нет ни одного пожелания. Покажи свой QR друзьям на выпускном!")
        return

    await message.answer(f"Все твои пожелания ({len(msgs)}) 🎉")
    for m in msgs:
        header = f"💌 <b>{m['sender_name']}</b>:"
        if m["photo_file_id"]:
            await bot.send_photo(
                message.from_user.id, m["photo_file_id"], caption=f"{header}\n{m['text'] or ''}"
            )
        else:
            await message.answer(f"{header}\n{m['text']}")


# ---------- Admin: bulk pre-registration ----------

@dp.message(Command("remove"))
async def remove_graduate(message: Message):
    if not is_admin(message.from_user.id):
        return

    parts = message.text.split(maxsplit=1)
    if len(parts) < 2 or not parts[1].strip():
        await message.answer(
            "Пришли имя или Telegram ID того, кого нужно удалить, например:\n"
            "/remove Иванов Иван\nили\n/remove 137093440"
        )
        return

    query = parts[1].strip()

    if query.isdigit():
        graduate = db.get_graduate_by_telegram_id(int(query))
        if not graduate:
            await message.answer(f"Не нахожу выпускника с ID {query}.")
            return
        db.delete_graduate(graduate["id"])
        await message.answer(f"Удалено: {graduate['name']} (ID {query}).")
        return

    matches = db.find_graduates_by_name(query)
    if not matches:
        await message.answer(f"Не нахожу «{query}» в списке.")
        return
    if len(matches) > 1:
        lines = "\n".join(f"— {m['name']} (ID: {m['chat_id'] or 'ещё не активирован'})" for m in matches)
        await message.answer(
            f"Нашлось несколько совпадений, уточни через ID:\n{lines}"
        )
        return

    graduate = matches[0]
    db.delete_graduate(graduate["id"])
    await message.answer(f"Удалено: {graduate['name']}.")


@dp.message(Command("import_ids"))
async def import_with_ids(message: Message):
    if not is_admin(message.from_user.id):
        return

    raw = message.text.split("\n", 1)
    if len(raw) < 2 or not raw[1].strip():
        await message.answer(
            "Пришли команду и список «Имя, telegram_id», каждая пара с новой строки, например:\n\n"
            "/import_ids\nИванов Иван, 123456789\nПетрова Мария, 987654321"
        )
        return

    pairs = []
    bad_lines = []
    for line in raw[1].split("\n"):
        line = line.strip()
        if not line:
            continue
        if "," not in line:
            bad_lines.append(line)
            continue
        name_part, id_part = line.rsplit(",", 1)
        name_part = name_part.strip()
        id_part = id_part.strip()
        if not id_part.isdigit():
            bad_lines.append(line)
            continue
        pairs.append((name_part, int(id_part)))

    created, errors = db.bulk_create_with_ids(pairs)

    report_lines = [f"Успешно добавлено: {len(created)}"]
    for name, chat_id in [(c["name"], c["chat_id"]) for c in created]:
        report_lines.append(f"✅ {name} — {chat_id}")

    if bad_lines:
        report_lines.append(f"\nНе распознано (нет запятой или ID не число): {len(bad_lines)}")
        report_lines.extend(f"⚠️ {l}" for l in bad_lines)

    if errors:
        report_lines.append(f"\nПропущено из-за конфликтов: {len(errors)}")
        report_lines.extend(f"⚠️ {n} ({i}) — {reason}" for n, i, reason in errors)

    report = "\n".join(report_lines)
    if len(report) > 3800:
        doc = BufferedInputFile(report.encode("utf-8"), filename="import_ids_report.txt")
        await bot.send_document(message.from_user.id, doc)
    else:
        await message.answer(report)

    if created:
        qr_zip = build_qr_zip(created)
        zip_doc = BufferedInputFile(qr_zip, filename="qr_codes.zip")
        await bot.send_document(
            message.from_user.id,
            zip_doc,
            caption="Готовые QR-коды для гостей — можно печатать сразу, не дожидаясь /start.",
        )
        await message.answer(
            "Готово. Теперь каждому из списка нужно один раз лично написать боту /start — "
            "бот сразу узнает его по ID и пришлёт этот же QR ему лично. Другого способа "
            "боту написать первым не существует (ограничение Telegram)."
        )


@dp.message(Command("import"))
async def import_names(message: Message):
    if not is_admin(message.from_user.id):
        return

    raw = message.text.split("\n", 1)
    if len(raw) < 2 or not raw[1].strip():
        await message.answer(
            "Пришли команду и список имён, каждое с новой строки, например:\n\n"
            "/import\nИванов Иван\nПетрова Мария\nСидоров Пётр"
        )
        return

    names = [line for line in raw[1].split("\n") if line.strip()]
    created = db.bulk_create_graduates(names)

    lines = [f"{c['name']} — {owner_link(c['owner_code'])}" for c in created]
    report = "\n".join(lines)
    file_bytes = report.encode("utf-8")
    doc = BufferedInputFile(file_bytes, filename="activation_links.txt")

    await message.answer(f"Добавлено {len(created)} выпускников. Персональные ссылки в файле 👇")
    await bot.send_document(message.from_user.id, doc)

    if created:
        qr_zip = build_qr_zip(created)
        zip_doc = BufferedInputFile(qr_zip, filename="qr_codes.zip")
        await bot.send_document(
            message.from_user.id,
            zip_doc,
            caption="Готовые QR-коды для гостей — можно печатать сразу.",
        )


@dp.message(Command("stats"))
async def stats(message: Message):
    if not is_admin(message.from_user.id):
        return
    await message.answer(
        f"Выпускников в списке: {db.count_graduates()}\n"
        f"Активировали бота: {db.count_activated()}\n"
        f"Пожеланий отправлено: {db.count_messages()}"
    )


@dp.message(Command("list_status"))
async def list_status(message: Message):
    if not is_admin(message.from_user.id):
        return
    graduates = db.all_graduates()
    if not graduates:
        await message.answer("Список пуст. Загрузи выпускников через /import")
        return
    lines = [
        f"{'✅' if g['chat_id'] else '⬜️'} {g['name']}" for g in graduates
    ]
    text = "\n".join(lines)
    if len(text) > 3800:
        doc = BufferedInputFile(text.encode("utf-8"), filename="status.txt")
        await bot.send_document(message.from_user.id, doc)
    else:
        await message.answer(text)


async def main():
    db.init_db()
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
