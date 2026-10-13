# Novel Convert + Split + Translate Telegram Bot

Features:
- EPUB → DOCX
- EPUB, TXT, DOCX, PDF and HTML text extraction
- Convert extracted text to TXT, DOCX, EPUB or HTML
- Split novels by chapter headings or character count
- Translate extracted text to English or Hindi

## GitHub deployment

1. Create a GitHub repository.
2. Upload all project files.
3. Open Settings → Secrets and variables → Actions.
4. Add a repository secret:
   Name: BOT_TOKEN
   Value: Your Telegram bot token from BotFather.
5. Open Actions → Run Telegram Bot → Run workflow.

GitHub Actions is not permanent 24/7 hosting.
The workflow may stop when its runtime limit is reached.

## Run in Termux

    pkg update -y
    pkg install python -y
    pip install -r requirements.txt
    export BOT_TOKEN="YOUR_BOT_TOKEN"
    python bot.py

## Limitations

- Translation needs internet and can be rate-limited.
- EPUB → DOCX extracts readable text and paragraphs.
- Original images and exact formatting may not be preserved.
- Scanned PDFs need OCR.
