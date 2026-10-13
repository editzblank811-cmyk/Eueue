# Novel Convert + Split + Translate Telegram Bot

Features:
- EPUB → DOCX (priority), EPUB/TXT/DOCX/PDF/HTML text extraction
- Convert extracted text to TXT, DOCX, EPUB, or HTML
- Split novels by detected chapter headings or character count; returns a ZIP of TXT parts
- Translate extracted text to English or Hindi using an online translation service
- Intended for text-based novel files; scanned PDF OCR is not included

## Deploy on GitHub Actions (free, while workflow runs)

GitHub Actions is not a permanent always-on host. A scheduled/manual workflow can run the bot for a limited time, but polling workers are better hosted on an always-on service. Do not run the same bot token in multiple places at once, or Telegram polling conflicts may occur.

### 1. Create repository
Create a new repository on GitHub, upload all these files to its root, then open **Settings → Secrets and variables → Actions → New repository secret**:
- Name: `BOT_TOKEN`
- Value: your Telegram bot token from BotFather

Never put the token directly in source code or commit it to GitHub.

### 2. Start bot manually
Open **Actions → Run Telegram Bot → Run workflow**. The bot will run while the GitHub Actions job is active. GitHub may cancel long-running jobs; this is not a permanent 24/7 hosting method.

### 3. File size
Telegram bot API download limits depend on the API/hosting setup. This project defaults to a 20 MB app-level limit. Change `MAX_DOWNLOAD_MB` in workflow environment if needed, but that cannot override Telegram API limits.

## Run locally in Termux
Install Python, then run:
```bash
pkg update -y
pkg install python -y
pip install -r requirements.txt
export BOT_TOKEN="YOUR_BOT_TOKEN"
python bot.py
```

## Important limitations
- Translation uses an online service and may be rate-limited; large novels may fail or take a long time.
- EPUB → DOCX preserves readable text and paragraph breaks, not exact EPUB layout, images, or styling.
- PDF extraction only works for text-based PDFs; scanned pages need OCR.
- EPUB output is generated from extracted text and may not retain the original table of contents or illustrations.
