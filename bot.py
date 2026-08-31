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

DB_FILE = "bot_stats.db"

# Normal files: 3 at a time
WORKER_COUNT = 3


# =========================
# GLOBAL QUEUE
# =========================

file_queue = asyncio.Queue()

# Files currently waiting or processing.
# Prevents simultaneous duplicate uploads.
in_progress = set()

in_progress_lock = asyncio.Lock()

workers = []


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
        return

    conn = get_db()

    conn.execute(
        f"""
        UPDATE stats
        SET {stat_name} = {stat_name} + ?
        WHERE id = 1
        """,
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

    if not file_unique_id:
        return False

    conn = get_db()

    row = conn.execute(
        """
        SELECT 1
        FROM processed_files
        WHERE file_unique_id = ?
        """,
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

    if not file_unique_id:
        return

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


def check_drive_duplicate(
    filename,
    file_size,
):
    """
    Checks the Google Drive folder for a file
    with the same filename AND size.

    Returns:
        matching file information if found
        None if not found
    """

    service = get_drive_service()

    # Escape single quotes for Drive query
    safe_filename = filename.replace("\\", "\\\\").replace(
        "'",
        "\\'"
    )

    query = (
        f"name = '{safe_filename}' "
        f"and '{DRIVE_FOLDER_ID}' in parents "
        f"and trashed = false"
    )

    results = service.files().list(
        q=query,
        spaces="drive",
        fields="files(id,name,size,mimeType)",
        pageSize=100,
    ).execute()

    files = results.get("files", [])

    for drive_file in files:

        drive_size = drive_file.get("size")

        if drive_size is not None:

            if int(drive_size) == int(file_size):

                return drive_file

    return None


def upload_to_drive(
    file_bytes,
    filename,
    mime_type,
):

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
        and update.effective_user.id
        in ALLOWED_TELEGRAM_IDS
    )


# =========================
# START
# =========================

async def start(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):

    if not is_allowed(update):
        return

    await update.message.reply_text(
        "🎵 Telegram → Google Drive Bot\n\n"
        "Send me MP3, FLAC, video, or any other file.\n\n"
        "📊 /stats - Statistics\n"
        "📈 /status - Current queue"
    )


# =========================
# STATS
# =========================

async def stats(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):

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

    downloaded_mb = (
        downloaded_bytes / 1024 / 1024
    )

    uploaded_mb = (
        uploaded_bytes / 1024 / 1024
    )

    await update.message.reply_text(
        "📊 BOT STATISTICS\n\n"

        f"📥 Files sent: {sent}\n"
        f"⬇️ Downloaded: {downloaded}\n"
        f"☁️ Uploaded: {uploaded}\n"
        f"❌ Failed: {failed}\n"
        f"⏭️ Duplicates skipped: {duplicates}\n\n"

        f"📦 Downloaded data: "
        f"{downloaded_mb:.2f} MB\n"

        f"☁️ Uploaded data: "
        f"{uploaded_mb:.2f} MB"
    )


# =========================
# STATUS
# =========================

async def status(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):

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

    waiting = file_queue.qsize()

    async with in_progress_lock:
        processing = len(in_progress)

    await update.message.reply_text(
        "📈 BOT STATUS\n\n"

        f"📥 Files received: {sent}\n"
        f"☁️ Uploaded: {uploaded}\n"
        f"❌ Failed: {failed}\n"
        f"⏭️ Duplicates: {duplicates}\n\n"

        f"⚙️ Processing now: {processing}\n"
        f"📋 Waiting in queue: {waiting}\n"
        f"🔢 Total remaining: "
        f"{waiting + processing}"
    )


# =========================
# GET FILE INFORMATION
# =========================

def get_file_information(message):

    if message.audio:

        return (
            message.audio,
            message.audio.file_name or "audio.mp3",
            message.audio.mime_type or "audio/mpeg",
            message.audio.file_unique_id,
            message.audio.file_size or 0,
        )

    if message.document:

        return (
            message.document,
            message.document.file_name or "file",
            message.document.mime_type
            or "application/octet-stream",
            message.document.file_unique_id,
            message.document.file_size or 0,
        )

    if message.video:

        return (
            message.video,
            "video.mp4",
            "video/mp4",
            message.video.file_unique_id,
            message.video.file_size or 0,
        )

    return None


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

    file_info = get_file_information(message)

    if not file_info:

        await message.reply_text(
            "❌ Please send a file."
        )

        return

    (
        telegram_media,
        filename,
        mime_type,
        file_unique_id,
        telegram_file_size,
    ) = file_info

    # ---------------------------------
    # Count received
    # ---------------------------------

    increment_stat("sent")

    # ---------------------------------
    # LOCAL DATABASE / IN-PROGRESS CHECK
    # ---------------------------------

    async with in_progress_lock:

        if file_unique_id and is_duplicate(
            file_unique_id
        ):

            increment_stat("duplicates")

            await message.reply_text(
                "⏭️ Duplicate skipped!\n\n"
                f"📄 {filename}\n\n"
                "This file has already been uploaded."
            )

            return

        if (
            file_unique_id
            and file_unique_id in in_progress
        ):

            increment_stat("duplicates")

            await message.reply_text(
                "⏭️ Duplicate skipped!\n\n"
                f"📄 {filename}\n\n"
                "This file is already in the queue."
            )

            return

        # Reserve immediately
        if file_unique_id:
            in_progress.add(file_unique_id)

    # ---------------------------------
    # GOOGLE DRIVE CHECK
    # ---------------------------------

    try:

        # Telegram gives us the file size,
        # so we can check Drive WITHOUT downloading.
        if telegram_file_size > 0:

            drive_match = await asyncio.to_thread(
                check_drive_duplicate,
                filename,
                telegram_file_size,
            )

            if drive_match:

                increment_stat("duplicates")

                # Record it locally too
                save_processed_file(
                    file_unique_id=file_unique_id,
                    filename=filename,
                    drive_file_id=drive_match["id"],
                    file_size=telegram_file_size,
                )

                async with in_progress_lock:
                    in_progress.discard(
                        file_unique_id
                    )

                await message.reply_text(
                    "⏭️ Duplicate skipped!\n\n"
                    f"📄 {filename}\n"
                    f"📦 {telegram_file_size / 1024 / 1024:.2f} MB\n\n"
                    "This file already exists in "
                    "your Google Drive."
                )

                return

    except Exception as e:

        print(
            "DRIVE DUPLICATE CHECK ERROR:",
            repr(e),
        )

        # We don't want to risk uploading a duplicate
        # if Google Drive cannot be checked.
        increment_stat("failed")

        async with in_progress_lock:
            in_progress.discard(
                file_unique_id
            )

        await message.reply_text(
            "❌ Could not check Google Drive.\n\n"
            f"📄 {filename}\n\n"
            "The file was NOT downloaded or uploaded."
        )

        return

    # ---------------------------------
    # ADD TO QUEUE
    # ---------------------------------

    await file_queue.put({
        "message": message,
        "telegram_media": telegram_media,
        "filename": filename,
        "mime_type": mime_type,
        "file_unique_id": file_unique_id,
    })


# =========================
# PROCESS ONE FILE
# =========================

async def process_file(item):

    message = item["message"]
    telegram_media = item["telegram_media"]
    filename = item["filename"]
    mime_type = item["mime_type"]
    file_unique_id = item["file_unique_id"]

    status_message = None

    try:

        # ---------------------------------
        # GET TELEGRAM FILE
        # ---------------------------------

        telegram_file = (
            await telegram_media.get_file()
        )

        status_message = await message.reply_text(
            f"⬇️ Downloading:\n{filename}"
        )

        # ---------------------------------
        # DOWNLOAD
        # ---------------------------------

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

        # ---------------------------------
        # UPLOAD
        # ---------------------------------

        await status_message.edit_text(
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

        # ---------------------------------
        # SAVE SUCCESS
        # ---------------------------------

        save_processed_file(
            file_unique_id=file_unique_id,
            filename=filename,
            drive_file_id=result["id"],
            file_size=uploaded_size,
        )

        increment_stat("uploaded")

        increment_stat(
            "uploaded_bytes",
            uploaded_size,
        )

        # ---------------------------------
        # SUCCESS
        # ---------------------------------

        await status_message.edit_text(
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

        if status_message:

            try:

                await status_message.edit_text(
                    "❌ Upload failed.\n\n"
                    f"📄 {filename}\n\n"
                    "The file was not marked as uploaded."
                )

            except Exception:
                pass

    finally:

        # ---------------------------------
        # REMOVE FROM IN-PROGRESS
        # ---------------------------------

        if file_unique_id:

            async with in_progress_lock:

                in_progress.discard(
                    file_unique_id
                )


# =========================
# QUEUE WORKER
# =========================

async def queue_worker(worker_number):

    print(
        f"Queue worker {worker_number} started."
    )

    while True:

        item = await file_queue.get()

        try:

            await process_file(item)

        except Exception as e:

            print(
                f"WORKER {worker_number} ERROR:",
                repr(e),
            )

        finally:

            file_queue.task_done()


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
# START QUEUE WORKERS
# =========================

async def post_init(application):

    global workers

    for i in range(WORKER_COUNT):

        worker = asyncio.create_task(
            queue_worker(i + 1)
        )

        workers.append(worker)


# =========================
# MAIN
# =========================

def main():

    # Initialize database
    get_db()

    app = (
        Application.builder()
        .token(TELEGRAM_BOT_TOKEN)
        .post_init(post_init)
        .build()
    )

    # Commands
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
        CommandHandler(
            "status",
            status,
        )
    )

    # Files
    app.add_handler(
        MessageHandler(
            filters.AUDIO
            | filters.Document.ALL
            | filters.VIDEO,
            handle_file,
        )
    )

    # Everything else
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
