import os
import re
import html
import time
import shutil
import zipfile
import logging
from pathlib import Path

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.error import RetryAfter, TimedOut, NetworkError
from telegram.ext import (
    Application, CommandHandler, MessageHandler,
    CallbackQueryHandler, ContextTypes, filters
)
from ebooklib import epub, ITEM_DOCUMENT
from bs4 import BeautifulSoup
from docx import Document
from pypdf import PdfReader
from deep_translator import GoogleTranslator

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s"
)
log = logging.getLogger("novelbot")

TOKEN = os.getenv("BOT_TOKEN", "").strip()
WORK = Path(os.getenv("WORKDIR", "work"))
WORK.mkdir(parents=True, exist_ok=True)
MAX_DOWNLOAD = int(os.getenv("MAX_DOWNLOAD_MB", "20")) * 1024 * 1024
# Large jobs: one user job at a time. Files are kept in RAM only as extracted text.
JOBS = {}

SUPPORTED = {".txt", ".epub", ".docx", ".pdf", ".html", ".htm"}
CHAPTER_RE = re.compile(
    r"(?im)^\s*(?=(?:chapter|chap\.?|ch\.?\s*\d|"
    r"第.{1,16}章|卷[一二三四五六七八九十百千0-9]+|"
    r"序章|前言|番外)[^\n]{0,140}$)"
)


def get_user_id(update):
    user = getattr(update, "effective_user", None)
    if user is None:
        user = getattr(update, "from_user", None)
    return user.id if user else None


def get_message(update):
    msg = getattr(update, "effective_message", None)
    if msg is None:
        msg = getattr(update, "message", None)
    if msg is None:
        msg = getattr(update, "callback_query", None)
        msg = getattr(msg, "message", None)
    return msg


def safe_name(name):
    return re.sub(r"[^A-Za-z0-9._-]+", "_", name or "file")[:100] or "file"


def read_file(path):
    ext = path.suffix.lower()

    if ext == ".txt":
        return path.read_text(encoding="utf-8-sig", errors="replace")

    if ext == ".docx":
        doc = Document(str(path))
        blocks = [p.text for p in doc.paragraphs if p.text.strip()]
        for table in doc.tables:
            for row in table.rows:
                blocks.append(" | ".join(c.text for c in row.cells))
        return "\n\n".join(blocks)

    if ext == ".epub":
        book = epub.read_epub(str(path))
        chunks = []
        for item in book.get_items():
            if item.get_type() == ITEM_DOCUMENT:
                soup = BeautifulSoup(item.get_content(), "html.parser")
                for tag in soup(["script", "style", "nav"]):
                    tag.decompose()
                text = soup.get_text("\n", strip=True)
                if text:
                    chunks.append(text)
        return "\n\n".join(chunks)

    if ext == ".pdf":
        reader = PdfReader(str(path))
        return "\n\n".join(page.extract_text() or "" for page in reader.pages)

    if ext in (".html", ".htm"):
        soup = BeautifulSoup(
            path.read_text(encoding="utf-8", errors="replace"),
            "html.parser"
        )
        for tag in soup(["script", "style", "nav"]):
            tag.decompose()
        return soup.get_text("\n", strip=True)

    raise ValueError("Unsupported input format.")


def write_txt(text, path):
    path.write_text(text, encoding="utf-8")


def write_docx(text, path):
    doc = Document()
    for block in re.split(r"\n\s*\n", text):
        if block.strip():
            doc.add_paragraph(block.strip())
    doc.save(str(path))


def write_epub(text, path, title="Converted Novel"):
    book = epub.EpubBook()
    book.set_identifier("converted-" + str(abs(hash(text[:2000]))))
    book.set_title(title)
    book.set_language("en")

    chapter = epub.EpubHtml(
        title="Novel", file_name="chapter_001.xhtml", lang="en"
    )
    paragraphs = [p.strip() for p in re.split(r"\n\s*\n", text) if p.strip()]
    body = "".join(
        "<p>" + html.escape(p).replace("\n", "<br/>") + "</p>"
        for p in paragraphs
    )
    chapter.content = (
        "<html><head><title>" + html.escape(title) +
        "</title></head><body>" + body + "</body></html>"
    ).encode("utf-8")

    book.add_item(chapter)
    book.toc = (epub.Link("chapter_001.xhtml", "Novel", "novel"),)
    book.add_item(epub.EpubNcx())
    book.add_item(epub.EpubNav())
    book.spine = ["nav", chapter]
    epub.write_epub(str(path), book)


def write_html(text, path, title="Converted file"):
    paragraphs = "".join(
        "<p>" + html.escape(p).replace("\n", "<br>") + "</p>"
        for p in re.split(r"\n\s*\n", text) if p.strip()
    )
    path.write_text(
        "<!doctype html><html><head><meta charset='utf-8'>"
        f"<title>{html.escape(title)}</title></head>"
        f"<body>{paragraphs}</body></html>",
        encoding="utf-8"
    )


def output_file(src, fmt, text, destdir, name=None):
    dest = destdir / (name or (src.stem + "." + fmt))
    if fmt == "txt":
        write_txt(text, dest)
    elif fmt == "docx":
        write_docx(text, dest)
    elif fmt == "epub":
        write_epub(text, dest, src.stem)
    elif fmt == "html":
        write_html(text, dest, src.stem)
    else:
        raise ValueError("Supported output: TXT, DOCX, EPUB, HTML.")
    return dest


def split_chapters(text):
    # Keep chapter heading at the start of each returned part.
    matches = list(CHAPTER_RE.finditer(text))
    if len(matches) < 2:
        return [text.strip()] if text.strip() else []
    starts = [m.start() for m in matches]
    if starts[0] > 0:
        starts.insert(0, 0)
    parts = []
    for i, start in enumerate(starts):
        end = starts[i + 1] if i + 1 < len(starts) else len(text)
        part = text[start:end].strip()
        if part:
            parts.append(part)
    return parts or [text.strip()]


def split_by_chars(text, size):
    size = max(1000, min(100000, int(size)))
    # Prefer paragraph boundaries so the text is not cut mid-paragraph.
    paragraphs = text.splitlines(keepends=True)
    parts, current = [], ""
    for paragraph in paragraphs:
        if current and len(current) + len(paragraph) > size:
            parts.append(current.strip())
            current = ""
        if len(paragraph) > size:
            if current:
                parts.append(current.strip())
                current = ""
            for i in range(0, len(paragraph), size):
                piece = paragraph[i:i + size]
                if piece.strip():
                    parts.append(piece.strip())
        else:
            current += paragraph
    if current.strip():
        parts.append(current.strip())
    return parts


def group_chapters(chapters, max_chars):
    groups, current = [], []
    count = 0
    for chapter in chapters:
        # A single unusually long chapter is kept intact.
        if current and count + len(chapter) > max_chars:
            groups.append("\n\n".join(current))
            current, count = [], 0
        current.append(chapter)
        count += len(chapter)
    if current:
        groups.append("\n\n".join(current))
    return groups


def keyboard():
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("Convert", callback_data="convert"),
            InlineKeyboardButton("Split", callback_data="split")
        ],
        [
            InlineKeyboardButton("Chinese → English", callback_data="tr_en"),
            InlineKeyboardButton("Chinese → Hindi", callback_data="tr_hi")
        ],
        [
            InlineKeyboardButton("English → Hindi", callback_data="en_hi"),
            InlineKeyboardButton("Hindi → English", callback_data="hi_en")
        ],
        [InlineKeyboardButton("Help", callback_data="help")]
    ])


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.effective_message.reply_text(
        "📚 Convert + Split + Translate Bot\n\n"
        "Send TXT, EPUB, DOCX, PDF or HTML.\n"
        "Supports long novels; chapter operations run sequentially.\n"
        "EPUB → DOCX is available under Convert.\n"
        "Translation needs internet and may take a long time for 500–1000 chapters.",
        reply_markup=keyboard()
    )


async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.effective_message.reply_text(
        "Send a file and use the buttons.\n\n"
        "Commands:\n"
        "/convert docx | txt | epub | html\n"
        "/split chapter\n"
        "/split 20000\n"
        "/translate en\n"
        "/translate hi\n\n"
        "Split by chapter creates a ZIP containing TXT files grouped to about "
        "50,000 characters each. If headings aren't detected, use character split.\n"
        "Translation is online and can be rate-limited. Scanned PDFs need OCR."
    )


async def handle_document(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.effective_message
    doc = msg.document
    ext = Path(doc.file_name or "").suffix.lower()

    if ext not in SUPPORTED:
        await msg.reply_text("Unsupported format. Send TXT, EPUB, DOCX, PDF or HTML.")
        return
    if doc.file_size and doc.file_size > MAX_DOWNLOAD:
        await msg.reply_text(
            f"File exceeds this bot's {MAX_DOWNLOAD // (1024 * 1024)} MB limit."
        )
        return

    uid = update.effective_user.id
    jobdir = WORK / str(uid)
    shutil.rmtree(jobdir, ignore_errors=True)
    jobdir.mkdir(parents=True, exist_ok=True)
    path = jobdir / safe_name(doc.file_name)

    try:
        status = await msg.reply_text("📥 Downloading and extracting text...")
        tgfile = await context.bot.get_file(doc.file_id)
        await tgfile.download_to_drive(custom_path=str(path))
        text = read_file(path)

        if not text.strip():
            await status.edit_text(
                "No readable text found. Scanned PDFs need OCR."
            )
            return

        JOBS[uid] = {"path": path, "text": text, "jobdir": jobdir}
        await status.edit_text(
            f"✅ Received: {doc.file_name}\n"
            f"Characters: {len(text):,}\n"
            f"Detected chapters: {max(0, len(split_chapters(text)) - 1) if len(split_chapters(text)) > 1 else 'not detected'}\n"
            "Choose an action:",
            reply_markup=keyboard()
        )
    except Exception as e:
        log.exception("File handling failed")
        await msg.reply_text(f"❌ Could not read file: {str(e)[:300]}")


async def send_path(update, path, caption="Done"):
    msg = get_message(update)
    if msg is None:
        raise ValueError("Telegram message not found.")
    with path.open("rb") as f:
        await msg.reply_document(
            document=f, filename=path.name, caption=caption[:900]
        )


async def do_convert(update, context, fmt):
    uid = get_user_id(update)
    job = JOBS.get(uid)
    msg = get_message(update)
    if not job:
        await msg.reply_text("Send a file first.")
        return
    try:
        status = await msg.reply_text(f"⚙️ Converting to {fmt.upper()}...")
        dest = output_file(job["path"], fmt, job["text"], job["jobdir"])
        await status.delete()
        await send_path(update, dest, f"✅ Converted to {fmt.upper()}")
    except Exception as e:
        log.exception("Conversion failed")
        await msg.reply_text(f"❌ Conversion failed: {str(e)[:300]}")


async def do_split(update, context, mode="chapter", value="50000"):
    uid = get_user_id(update)
    job = JOBS.get(uid)
    msg = get_message(update)
    if not job:
        await msg.reply_text("Send a file first.")
        return

    try:
        status = await msg.reply_text("✂️ Splitting file...")
        if mode == "chapter":
            chapters = split_chapters(job["text"])
            parts = group_chapters(chapters, int(value))
        else:
            parts = split_by_chars(job["text"], int(value))

        outdir = job["jobdir"] / "split_parts"
        shutil.rmtree(outdir, ignore_errors=True)
        outdir.mkdir(parents=True)

        for i, part in enumerate(parts, 1):
            write_txt(part, outdir / f"part_{i:04}.txt")

        archive = job["jobdir"] / f"{job['path'].stem}_split.zip"
        with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as z:
            for p in sorted(outdir.iterdir()):
                z.write(p, p.name)

        await status.delete()
        await send_path(
            update, archive,
            f"✅ Split complete: {len(parts)} parts in ZIP."
        )
    except Exception as e:
        log.exception("Split failed")
        await msg.reply_text(f"❌ Split failed: {str(e)[:300]}")


async def translate_with_retry(text, target, status, label):
    translator = GoogleTranslator(source="auto", target=target)
    # Translate in small blocks and update progress; avoids one huge request.
    blocks = split_chapters(text)
    if len(blocks) <= 1:
        blocks = split_by_chars(text, 2500)

    translated_blocks = []
    total = len(blocks)

    for idx, block in enumerate(blocks, 1):
        # Break oversized chapters into API-safe chunks, then rejoin.
        pieces = [block[i:i + 2500] for i in range(0, len(block), 2500)]
        translated_pieces = []

        for piece in pieces:
            last_error = None
            for attempt in range(4):
                try:
                    translated_pieces.append(translator.translate(piece) or piece)
                    last_error = None
                    break
                except (RetryAfter, TimedOut, NetworkError) as e:
                    last_error = e
                    await __import__("asyncio").sleep(min(30, 2 ** attempt))
                except Exception as e:
                    last_error = e
                    await __import__("asyncio").sleep(min(20, 2 ** attempt))

            if last_error is not None:
                raise RuntimeError(
                    f"Translation stopped near block {idx}/{total}: {last_error}"
                )

            # Gentle pacing helps reduce rate-limit errors.
            await __import__("asyncio").sleep(0.15)

        translated_blocks.append("".join(translated_pieces))

        if idx == 1 or idx % 10 == 0 or idx == total:
            try:
                await status.edit_text(
                    f"🌐 Translating {label}\n"
                    f"Chapters/blocks: {idx}/{total} ({idx * 100 // total}%)\n"
                    "Please keep the bot workflow running."
                )
            except Exception:
                pass

    return "\n\n".join(translated_blocks)


async def do_translate(update, context, target):
    uid = get_user_id(update)
    job = JOBS.get(uid)
    msg = get_message(update)
    if not job:
        await msg.reply_text("Send a file first.")
        return

    status = await msg.reply_text(
        f"🌐 Starting translation to {target.title()}..."
    )

    try:
        translated = await translate_with_retry(
            job["text"], target, status, target.title()
        )

        # Keep the translated output in a text-friendly document format.
        fmt = "docx" if job["path"].suffix.lower() in (".epub", ".docx", ".pdf") else "txt"
        dest = job["jobdir"] / f"{job['path'].stem}_{target}.{fmt}"

        if fmt == "docx":
            write_docx(translated, dest)
        else:
            write_txt(translated, dest)

        await status.delete()
        await send_path(
            update, dest,
            f"✅ Translation complete: {target.title()} ({fmt.upper()})"
        )
    except Exception as e:
        log.exception("Translation failed")
        try:
            await status.edit_text(
                f"❌ Translation failed: {str(e)[:700]}\n\n"
                "The free translation provider may rate-limit long novels. "
                "Retry later or split the novel and translate smaller parts."
            )
        except Exception:
            await msg.reply_text(f"❌ Translation failed: {str(e)[:500]}")


async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    uid = q.from_user.id

    if q.data == "help":
        await q.message.reply_text(
            "Send a file, then choose Convert, Split or Translate."
        )
        return

    if uid not in JOBS:
        await q.message.reply_text("Send a file first.")
        return

    if q.data == "convert":
        await q.message.reply_text(
            "Choose output format:",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("DOCX", callback_data="fmt_docx"),
                InlineKeyboardButton("TXT", callback_data="fmt_txt"),
                InlineKeyboardButton("EPUB", callback_data="fmt_epub"),
                InlineKeyboardButton("HTML", callback_data="fmt_html")
            ]])
        )
    elif q.data.startswith("fmt_"):
        await do_convert(update, context, q.data[4:])
    elif q.data == "split":
        await q.message.reply_text(
            "Choose split method:",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton(
                    "By chapter (about 50k chars/file)",
                    callback_data="split_chapter"
                )],
                [InlineKeyboardButton(
                    "20,000 characters/file",
                    callback_data="split_chars_20000"
                )],
                [InlineKeyboardButton(
                    "50,000 characters/file",
                    callback_data="split_chars_50000"
                )],
                [InlineKeyboardButton(
                    "100,000 characters/file",
                    callback_data="split_chars_100000"
                )]
            ])
        )
    elif q.data == "split_chapter":
        await do_split(update, context, "chapter", "50000")
    elif q.data.startswith("split_chars_"):
        await do_split(update, context, "chars", q.data.rsplit("_", 1)[1])
    elif q.data == "tr_en":
        await do_translate(update, context, "english")
    elif q.data == "tr_hi":
        await do_translate(update, context, "hindi")
    elif q.data == "en_hi":
        await do_translate(update, context, "hindi")
    elif q.data == "hi_en":
        await do_translate(update, context, "english")


async def convert_cmd(update, context):
    if not context.args:
        await update.effective_message.reply_text("Usage: /convert docx")
        return
    await do_convert(update, context, context.args[0].lower().lstrip("."))


async def split_cmd(update, context):
    mode = context.args[0].lower() if context.args else "chapter"
    if mode == "chapter":
        await do_split(update, context, "chapter", "50000")
    else:
        try:
            size = str(max(1000, min(100000, int(mode))))
        except ValueError:
            size = "50000"
        await do_split(update, context, "chars", size)


async def translate_cmd(update, context):
    if not context.args:
        await update.effective_message.reply_text(
            "Usage: /translate en OR /translate hi"
        )
        return
    lang = context.args[0].lower()
    if lang in ("en", "english"):
        await do_translate(update, context, "english")
    elif lang in ("hi", "hindi"):
        await do_translate(update, context, "hindi")
    else:
        await update.effective_message.reply_text("Supported targets: en, hi")


def main():
    if not TOKEN:
        raise SystemExit("BOT_TOKEN environment variable is missing.")

    app = Application.builder().token(TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_cmd))
    app.add_handler(CommandHandler("convert", convert_cmd))
    app.add_handler(CommandHandler("split", split_cmd))
    app.add_handler(CommandHandler("translate", translate_cmd))
    app.add_handler(MessageHandler(filters.Document.ALL, handle_document))
    app.add_handler(CallbackQueryHandler(button_handler))
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
