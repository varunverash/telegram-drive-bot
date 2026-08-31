import os
import io
import asyncio
import sqlite3
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

GOOGLE_CLIENT_ID = os.environ["GOOGLE_CLIENT_ID"]
GOOGLE_CLIENT_SECRET = os.environ["GOOGLE_CLIENT_SECRET"]
GOOGLE_REFRESH_TOKEN = os.environ["GOOGLE_REFRESH_TOKEN"]

SCOPES = ["https://www.googleapis.com/auth/drive.file"]

# Database file
DB_FILE = "bot_stats.db"


# =========================
# DATABASE
# =========================

def get_db():
    conn = sqlite3.connect(DB_FILE)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS stats (
            id INTEGER PRIMARY KEY CHECK (id = 1),
            sent INTEGER DEFAULT 0,
            downloaded INTEGER DEFAULT 0,
            uploaded INTEGER DEFAULT 0,
            failed INTEGER DEFAULT 0,
            duplicates INTEGER DEFAULT 0,
            downloaded_bytes INTEGER DEFAULT 0,
            uploaded_bytes INTEGER DEFAULT 0
        )
    """)

    conn.execute("""
        INSERT OR IGNORE INTO stats
        (id, sent, downloaded, uploaded, failed, duplicates,
         downloaded_bytes, uploaded_bytes)
        VALUES (1, 0, 0, 0, 0, 0, 0, 0)
    """)

    conn.execute("""
        CREATE TABLE IF NOT EXISTS processed_files (
            file_unique_id TEXT PRIMARY KEY,
            filename TEXT,
            drive_file_id TEXT,
            file_size INTEGER DEFAULT 0
        )
    """)

    conn.commit()
    return conn


def increment_stat(stat_name, amount=1):
    conn = get_db()

    allowed = {
        "sent",
        "downloaded",
        "uploaded",
        "failed",
        "duplicates",
        "downloaded_bytes",
        "uploaded_bytes",
    }

    if stat_name not in allowed:
        conn.close()
        return

    conn.execute(
        f"UPDATE stats SET {stat_name} = {stat_name} + ? WHERE id = 1",
        (amount,),
    )

    conn.commit()
    conn.close()


def get_stats():
    conn = get_db()

    row = conn.execute("""
        SELECT
            sent,
            downloaded,
            uploaded,
            failed,
            duplicates,
            downloaded_bytes,
            uploaded_bytes
        FROM stats
        WHERE id = 1
    """).fetchone()

    conn.close()

    return row


def is_duplicate(file_unique_id):
    conn = get_db()

    row = conn.execute(
        "SELECT 1 FROM processed_files WHERE file_unique_id = ?",
        (file_unique_id,),
    ).fetchone()

    conn.close()

    return row is not None


def save_processed_file(
    file_unique_id,
    filename,
    drive_file_id,
    file_size,
):
    conn = get_db()

    conn.execute("""
        INSERT OR IGNORE INTO processed_files
        (file_unique_id, filename, drive_file_id, file_size)
        VALUES (?, ?, ?, ?)
    """, (
        file_unique_id,
        filename,
        drive_file_id,
        file_size,
    ))

    conn.commit()
    conn.close()


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

    return build(
        "drive",
        "v3",
        credentials=credentials,
    )


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


# =========================
# START
# =========================

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update):
        return

    await update.message.reply_text(
        "🎵 Telegram → Google Drive Bot\n\n"
        "Send me an MP3, FLAC, video, or any other file "
        "and I'll upload it to your Google Drive.\n\n"
        "📊 Use /stats to see upload statistics."
    )


# =========================
# STATS COMMAND
# =========================

async def stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update):
        return

    (
        sent,
        downloaded,
        uploaded,
        failed,
        duplicates,
        downloaded_bytes,
        uploaded_bytes,
    ) = get_stats()

    downloaded_mb = downloaded_bytes / 1024 / 1024
    uploaded_mb = uploaded_bytes / 1024 / 1024

    await update.message.reply_text(
        "📊 BOT STATISTICS\n\n"
        f"📥 Files sent: {sent}\n"
        f"⬇️ Downloaded: {downloaded}\n"
        f"☁️ Uploaded: {uploaded}\n"
        f"❌ Failed: {failed}\n"
        f"⏭️ Duplicates skipped: {duplicates}\n\n"
        f"📦 Downloaded data: {downloaded_mb:.2f} MB\n"
        f"☁️ Uploaded data: {uploaded_mb:.2f} MB"
    )


# =========================
# HANDLE FILE
# =========================

async def handle_file(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    if not is_allowed(update):
        return

    message = update.message

    # ---------------------------------
    # Determine Telegram file
    # ---------------------------------

    telegram_file = None
    filename = None
    mime_type = "application/octet-stream"
    file_unique_id = None

    if message.audio:

        telegram_file = await message.audio.get_file()

        filename = (
            message.audio.file_name
            or "audio.mp3"
        )

        mime_type = (
            message.audio.mime_type
            or "audio/mpeg"
        )

        file_unique_id = message.audio.file_unique_id

    elif message.document:

        telegram_file = await message.document.get_file()

        filename = (
            message.document.file_name
            or "file"
        )

        mime_type = (
            message.document.mime_type
            or "application/octet-stream"
        )

        file_unique_id = message.document.file_unique_id

    elif message.video:

        telegram_file = await message.video.get_file()

        filename = "video.mp4"
        mime_type = "video/mp4"

        file_unique_id = message.video.file_unique_id

    else:
        await message.reply_text(
            "❌ Please send a file."
        )
        return

    # ---------------------------------
    # Count received file
    # ---------------------------------

    increment_stat("sent")

    # ---------------------------------
    # DUPLICATE CHECK
    # ---------------------------------

    if file_unique_id and is_duplicate(file_unique_id):

        increment_stat("duplicates")

        await message.reply_text(
            f"⏭️ Duplicate skipped!\n\n"
            f"📄 {filename}\n\n"
            f"This file has already been uploaded."
        )

        return

    # ---------------------------------
    # Status message
    # ---------------------------------

    status = await message.reply_text(
        f"⬇️ Downloading:\n{filename}"
    )

    try:

        # =============================
        # DOWNLOAD FROM TELEGRAM
        # =============================

        file_data = io.BytesIO()

        await telegram_file.download_to_memory(
            file_data
        )

        file_bytes = file_data.getvalue()
        file_size = len(file_bytes)

        increment_stat("downloaded")
        increment_stat(
            "downloaded_bytes",
            file_size,
        )

        # =============================
        # UPLOAD TO GOOGLE DRIVE
        # =============================

        await status.edit_text(
            f"⬆️ Uploading to Google Drive:\n"
            f"{filename}\n\n"
            f"📦 {file_size / 1024 / 1024:.2f} MB"
        )

        result = await asyncio.to_thread(
            upload_to_drive,
            file_bytes,
            filename,
            mime_type,
        )

        uploaded_size = int(
            result.get(
                "size",
                file_size,
            )
        )

        # =============================
        # SAVE AS PROCESSED
        # =============================

        if file_unique_id:

            save_processed_file(
                file_unique_id=file_unique_id,
                filename=filename,
                drive_file_id=result["id"],
                file_size=uploaded_size,
            )

        # =============================
        # UPDATE STATISTICS
        # =============================

        increment_stat("uploaded")

        increment_stat(
            "uploaded_bytes",
            uploaded_size,
        )

        # =============================
        # SUCCESS
        # =============================

        await status.edit_text(
            f"✅ Uploaded successfully!\n\n"
            f"📄 {result['name']}\n"
            f"📦 {uploaded_size / 1024 / 1024:.2f} MB"
        )

    except Exception as e:

        print(
            "ERROR:",
            repr(e),
        )

        increment_stat("failed")

        await status.edit_text(
            "❌ Upload failed.\n\n"
            "The file was not marked as uploaded, "
            "so you can send it again."
        )


# =========================
# UNKNOWN MESSAGE
# =========================

async def unknown_message(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):

    if not is_allowed(update):
        return

    await update.message.reply_text(
        "Please send a file such as MP3 or FLAC."
    )


# =========================
# MAIN
# =========================

def main():

    # Initialize database
    get_db()

    app = (
        Application.builder()
        .token(TELEGRAM_BOT_TOKEN)
        .build()
    )

    app.add_handler(
        CommandHandler(
            "start",
            start,
        )
    )

    app.add_handler(
        CommandHandler(
            "stats",
            stats,
        )
    )

    app.add_handler(
        MessageHandler(
            filters.AUDIO
            | filters.Document.ALL
            | filters.VIDEO,
            handle_file,
        )
    )

    app.add_handler(
        MessageHandler(
            filters.ALL,
            unknown_message,
        )
    )

    print(
        "Telegram → Google Drive bot is running..."
    )

    app.run_polling()


if __name__ == "__main__":
    main()
