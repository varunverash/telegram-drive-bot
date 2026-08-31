import os
import io
import asyncio
from telegram import Update
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    ContextTypes,
    filters,
)

from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseUpload

# =========================
# CONFIGURATION
# =========================

TELEGRAM_BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
ALLOWED_TELEGRAM_IDS = {
    556318583,
    5237041275,
}

DRIVE_FOLDER_ID = "1u9R8-cU4im44hcPDOZzsZ2lVa_6u0cX9"

# Google OAuth information will be added later as GitHub Secrets
GOOGLE_CLIENT_ID = os.environ["GOOGLE_CLIENT_ID"]
GOOGLE_CLIENT_SECRET = os.environ["GOOGLE_CLIENT_SECRET"]
GOOGLE_REFRESH_TOKEN = os.environ["GOOGLE_REFRESH_TOKEN"]

SCOPES = ["https://www.googleapis.com/auth/drive.file"]


# =========================
# GOOGLE DRIVE
# =========================

def get_drive_service():
    credentials = Credentials(
        token=None,
        refresh_token=GOOGLE_REFRESH_TOKEN,
        token_uri="https://oauth2.googleapis.com/token",
        client_id=GOOGLE_CLIENT_ID,
        client_secret=GOOGLE_CLIENT_SECRET,
        scopes=SCOPES,
    )

    return build("drive", "v3", credentials=credentials)


def upload_to_drive(file_bytes, filename, mime_type):
    service = get_drive_service()

    metadata = {
        "name": filename,
        "parents": [DRIVE_FOLDER_ID],
    }

    media = MediaIoBaseUpload(
        io.BytesIO(file_bytes),
        mimetype=mime_type,
        resumable=True,
    )

    uploaded = service.files().create(
        body=metadata,
        media_body=media,
        fields="id,name,size",
    ).execute()

    return uploaded


# =========================
# TELEGRAM
# =========================

def is_allowed(update: Update):
    return (
        update.effective_user
        and update.effective_user.id in ALLOWED_TELEGRAM_IDS
    )


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update):
        return

    await update.message.reply_text(
        "🎵 Telegram → Google Drive Bot\n\n"
        "Send me an MP3, FLAC, or other file and "
        "I'll upload it to your Google Drive folder."
    )


async def handle_file(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update):
        return

    message = update.message

    # Determine which Telegram file was sent
    telegram_file = None
    filename = None
    mime_type = "application/octet-stream"

    if message.audio:
        telegram_file = await message.audio.get_file()
        filename = message.audio.file_name or "audio.mp3"
        mime_type = message.audio.mime_type or "audio/mpeg"

    elif message.document:
        telegram_file = await message.document.get_file()
        filename = message.document.file_name or "file"
        mime_type = message.document.mime_type or "application/octet-stream"

    elif message.video:
        telegram_file = await message.video.get_file()
        filename = "video.mp4"
        mime_type = "video/mp4"

    else:
        await message.reply_text("❌ Please send a file.")
        return

    status = await message.reply_text(
        f"⬇️ Downloading:\n{filename}"
    )

    try:
        # Download Telegram file into memory
        file_data = io.BytesIO()
        await telegram_file.download_to_memory(file_data)

        file_bytes = file_data.getvalue()

        await status.edit_text(
            f"⬆️ Uploading to Google Drive:\n{filename}"
        )

        # Google Drive upload is synchronous, so run it separately
        result = await asyncio.to_thread(
            upload_to_drive,
            file_bytes,
            filename,
            mime_type,
        )

        size = int(result.get("size", len(file_bytes)))

        await status.edit_text(
            f"✅ Uploaded successfully!\n\n"
            f"📄 {result['name']}\n"
            f"📦 {size / 1024 / 1024:.2f} MB"
        )

    except Exception as e:
        print("ERROR:", repr(e))

        await status.edit_text(
            "❌ Upload failed.\n\n"
            "Check the bot/server configuration."
        )


async def unknown_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update):
        return

    await update.message.reply_text(
        "Please send a file such as MP3 or FLAC."
    )


# =========================
# MAIN
# =========================

def main():
    app = (
        Application.builder()
        .token(TELEGRAM_BOT_TOKEN)
        .build()
    )

    app.add_handler(CommandHandler("start", start))

    app.add_handler(
        MessageHandler(
            filters.AUDIO | filters.Document.ALL | filters.VIDEO,
            handle_file,
        )
    )

    app.add_handler(
        MessageHandler(
            filters.ALL,
            unknown_message,
        )
    )

    print("Telegram → Google Drive bot is running...")

    app.run_polling()


if __name__ == "__main__":
    main()
