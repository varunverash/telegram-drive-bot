import os
import io
import asyncio
import json
import tempfile
from pathlib import Path
from datetime import datetime, timedelta, timezone

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
from googleapiclient.http import (
    MediaFileUpload,
    MediaIoBaseUpload,
)

from telethon import TelegramClient
from telethon.errors import FloodWaitError


# ============================================================
# CONFIGURATION
# ============================================================

TELEGRAM_BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]

ALLOWED_TELEGRAM_IDS = {
    556318583,
    5237041275,
}

TELEGRAM_API_ID = int(
    os.environ["TELEGRAM_API_ID"]
)

TELEGRAM_API_HASH = os.environ[
    "TELEGRAM_API_HASH"
]

TELEGRAM_SESSION = "drive_bot_session"

BOT_USERNAME = "Drivegesavemaadadu_bot"

DRIVE_FOLDER_ID = (
    "1u9R8-cU4im44hcPDOZzsZ2lVa_6u0cX9"
)

GOOGLE_CLIENT_ID = os.environ[
    "GOOGLE_CLIENT_ID"
]

GOOGLE_CLIENT_SECRET = os.environ[
    "GOOGLE_CLIENT_SECRET"
]

GOOGLE_REFRESH_TOKEN = os.environ[
    "GOOGLE_REFRESH_TOKEN"
]

SCOPES = [
    "https://www.googleapis.com/auth/drive.file"
]

STATE_FILENAME = (
    ".telegram_drive_bot_state.json"
)

INITIAL_RECOVERY_HOURS = 12

PROCESS_INTERVAL_SECONDS = 2


# ============================================================
# GOOGLE DRIVE
# ============================================================

def get_drive_service():

    credentials = Credentials(
        token=None,
        refresh_token=GOOGLE_REFRESH_TOKEN,
        token_uri=(
            "https://oauth2.googleapis.com/token"
        ),
        client_id=GOOGLE_CLIENT_ID,
        client_secret=GOOGLE_CLIENT_SECRET,
        scopes=SCOPES,
    )

    return build(
        "drive",
        "v3",
        credentials=credentials,
        cache_discovery=False,
    )


def escape_drive_value(value):

    return (
        str(value)
        .replace("\\", "\\\\")
        .replace("'", "\\'")
    )


# ============================================================
# STATE FILE IN GOOGLE DRIVE
# ============================================================

def find_state_file(service):

    query = (
        f"name = "
        f"'{escape_drive_value(STATE_FILENAME)}' "
        f"and '{DRIVE_FOLDER_ID}' in parents "
        f"and trashed = false"
    )

    result = (
        service.files()
        .list(
            q=query,
            spaces="drive",
            pageSize=10,
            fields="files(id,name,size)",
        )
        .execute()
    )

    files = result.get(
        "files",
        []
    )

    return files[0] if files else None


def load_state_sync():

    service = get_drive_service()

    state_file = find_state_file(
        service
    )

    if not state_file:
        return None

    request = service.files().get_media(
        fileId=state_file["id"]
    )

    data = request.execute()

    return json.loads(
        data.decode("utf-8")
    )


def save_state_sync(state):

    service = get_drive_service()

    state_file = find_state_file(
        service
    )

    data = json.dumps(
        state,
        indent=2,
        ensure_ascii=False,
    ).encode("utf-8")

    media = MediaIoBaseUpload(
        io.BytesIO(data),
        mimetype="application/json",
        resumable=False,
    )

    if state_file:

        service.files().update(
            fileId=state_file["id"],
            media_body=media,
        ).execute()

    else:

        metadata = {
            "name": STATE_FILENAME,
            "parents": [
                DRIVE_FOLDER_ID
            ],
            "mimeType": "application/json",
        }

        service.files().create(
            body=metadata,
            media_body=media,
            fields="id",
        ).execute()


DEFAULT_STATE = {
    "version": 1,
    "last_message_id": 0,

    "sent": 0,
    "downloaded": 0,
    "uploaded": 0,
    "failed": 0,
    "duplicates": 0,

    "downloaded_bytes": 0,
    "uploaded_bytes": 0,

    "initialized": False,
}


class StateManager:

    def __init__(self):

        self.state = None
        self.lock = asyncio.Lock()

    async def load(self):

        self.state = await asyncio.to_thread(
            load_state_sync
        )

        if not self.state:

            self.state = (
                DEFAULT_STATE.copy()
            )

        return self.state

    async def save(self):

        async with self.lock:

            snapshot = dict(
                self.state
            )

            await asyncio.to_thread(
                save_state_sync,
                snapshot,
            )

    def get(
        self,
        key,
        default=None,
    ):

        return self.state.get(
            key,
            default,
        )

    def set(
        self,
        key,
        value,
    ):

        self.state[key] = value

    def increment(
        self,
        key,
        amount=1,
    ):

        self.state[key] = (
            self.state.get(
                key,
                0,
            )
            + amount
        )


# ============================================================
# GOOGLE DRIVE DUPLICATE CHECK
# ============================================================

def find_drive_duplicate_sync(
    telegram_file_id,
    filename,
    file_size,
):

    service = get_drive_service()

    # --------------------------------------------------------
    # Exact Telegram file ID check
    # --------------------------------------------------------

    if telegram_file_id:

        safe_id = escape_drive_value(
            telegram_file_id
        )

        query = (
            f"'{DRIVE_FOLDER_ID}' in parents "
            f"and trashed = false "
            f"and appProperties has "
            f"{{ key='telegram_file_id' "
            f"and value='{safe_id}' }}"
        )

        result = (
            service.files()
            .list(
                q=query,
                spaces="drive",
                pageSize=10,
                fields=(
                    "files(id,name,size)"
                ),
            )
            .execute()
        )

        files = result.get(
            "files",
            []
        )

        if files:

            return files[0]

    # --------------------------------------------------------
    # Compatibility check for files uploaded by
    # your previous bot version.
    #
    # Filename + exact size.
    # --------------------------------------------------------

    safe_name = escape_drive_value(
        filename
    )

    query = (
        f"'{DRIVE_FOLDER_ID}' in parents "
        f"and trashed = false "
        f"and name = '{safe_name}' "
        f"and size = {int(file_size)}"
    )

    result = (
        service.files()
        .list(
            q=query,
            spaces="drive",
            pageSize=20,
            fields=(
                "files(id,name,size)"
            ),
        )
        .execute()
    )

    files = result.get(
        "files",
        []
    )

    return files[0] if files else None


# ============================================================
# GOOGLE DRIVE UPLOAD
# ============================================================

def upload_file_to_drive_sync(
    file_path,
    filename,
    mime_type,
    telegram_file_id,
):

    service = get_drive_service()

    metadata = {
        "name": filename,
        "parents": [
            DRIVE_FOLDER_ID
        ],
        "appProperties": {
            "telegram_file_id": str(
                telegram_file_id or ""
            )
        },
    }

    media = MediaFileUpload(
        file_path,
        mimetype=mime_type,
        chunksize=8 * 1024 * 1024,
        resumable=True,
    )

    request = (
        service.files()
        .create(
            body=metadata,
            media_body=media,
            fields="id,name,size",
        )
    )

    response = None

    while response is None:

        status, response = (
            request.next_chunk()
        )

        if status:

            percent = int(
                status.progress() * 100
            )

            print(
                f"Drive upload: "
                f"{filename} "
                f"{percent}%"
            )

    return response


# ============================================================
# TELEGRAM FILE INFORMATION
# ============================================================

def get_telegram_file_info(
    message
):

    if not message:

        return None

    if not message.media:

        return None

    file = message.file

    if not file:

        return None

    filename = file.name

    if not filename:

        extension = (
            file.ext or ""
        )

        filename = (
            f"file_{message.id}"
            f"{extension}"
        )

    mime_type = (
        file.mime_type
        or "application/octet-stream"
    )

    file_size = int(
        file.size or 0
    )

    telegram_file_id = (
        str(file.id)
        if file.id
        else f"message:{message.id}"
    )

    return (
        filename,
        mime_type,
        file_size,
        telegram_file_id,
    )


# ============================================================
# PERMISSION
# ============================================================

def is_allowed(
    update: Update
):

    return (
        update.effective_user
        and update.effective_user.id
        in ALLOWED_TELEGRAM_IDS
    )


# ============================================================
# START COMMAND
# ============================================================

async def start(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):

    if not is_allowed(update):

        return

    await update.message.reply_text(
        "🎵 Telegram → Google Drive Bot\n\n"
        "Send MP3, FLAC, video, or any file.\n\n"
        "📦 Large-file mode: enabled\n"
        "🔄 One-file-at-a-time: enabled\n"
        "⏭️ Drive duplicate check: enabled\n\n"
        "📊 /stats\n"
        "🟢 /status"
    )


# ============================================================
# STATS COMMAND
# ============================================================

async def stats(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):

    if not is_allowed(update):

        return

    state_manager = (
        context.application.bot_data[
            "state_manager"
        ]
    )

    state = state_manager.state

    downloaded_mb = (
        state.get(
            "downloaded_bytes",
            0,
        )
        / 1024
        / 1024
    )

    uploaded_mb = (
        state.get(
            "uploaded_bytes",
            0,
        )
        / 1024
        / 1024
    )

    await update.message.reply_text(

        "📊 BOT STATISTICS\n\n"

        f"📥 Files sent: "
        f"{state.get('sent', 0)}\n"

        f"⬇️ Downloaded: "
        f"{state.get('downloaded', 0)}\n"

        f"☁️ Uploaded: "
        f"{state.get('uploaded', 0)}\n"

        f"❌ Failed: "
        f"{state.get('failed', 0)}\n"

        f"⏭️ Duplicates skipped: "
        f"{state.get('duplicates', 0)}\n\n"

        f"📦 Downloaded data: "
        f"{downloaded_mb:.2f} MB\n"

        f"☁️ Uploaded data: "
        f"{uploaded_mb:.2f} MB"
    )


# ============================================================
# STATUS COMMAND
# ============================================================

async def status_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):

    if not is_allowed(update):

        return

    state_manager = (
        context.application.bot_data[
            "state_manager"
        ]
    )

    state = state_manager.state

    await update.message.reply_text(

        "🟢 BOT STATUS\n\n"

        "✅ Telegram Bot API: active\n"
        "✅ Telegram MTProto: active\n"
        "✅ Google Drive: configured\n"
        "✅ Large-file mode: active\n"
        "✅ One-file queue: active\n"
        "💾 State: Google Drive\n\n"

        f"📌 Last processed message: "
        f"{state.get('last_message_id', 0)}"
    )


# ============================================================
# END OF PART 1
# ============================================================
# ============================================================
# TELEGRAM BOT API FILE HANDLER
# ============================================================

async def file_received(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):

    if not is_allowed(update):
        return

    message = update.message

    print(
        "Bot API received file message:",
        message.message_id,
    )

    # We intentionally DO NOT download the file here.
    #
    # Telethon reads the same Telegram conversation and
    # downloads the file using MTProto.
    #
    # This avoids the Bot API's small-file limitation and
    # allows the large-file system to handle the download.
    return


# ============================================================
# UNKNOWN MESSAGE
# ============================================================

async def unknown_message(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):

    if not is_allowed(update):
        return

    # Ignore normal text messages.
    return


# ============================================================
# FIRST-RUN / RECOVERY INITIALIZATION
# ============================================================

async def initialize_state(
    state_manager,
    telethon_client,
    bot_chat,
):

    if state_manager.get(
        "initialized",
        False,
    ):
        return

    print(
        "First run: checking recent Telegram history..."
    )

    cutoff = (
        datetime.now(timezone.utc)
        - timedelta(
            hours=INITIAL_RECOVERY_HOURS
        )
    )

    messages = await telethon_client.get_messages(
        bot_chat,
        limit=5000,
    )

    eligible = []

    for message in messages:

        if not message.date:
            continue

        message_date = message.date

        if message_date.tzinfo is None:

            message_date = (
                message_date.replace(
                    tzinfo=timezone.utc
                )
            )

        if message_date < cutoff:
            continue

        if (
            message.sender_id
            not in ALLOWED_TELEGRAM_IDS
        ):
            continue

        if not message.media:
            continue

        if not message.file:
            continue

        eligible.append(message)

    if eligible:

        first_id = min(
            message.id
            for message in eligible
        )

        state_manager.set(
            "last_message_id",
            first_id - 1,
        )

        print(
            f"Found {len(eligible)} "
            f"recent file message(s)."
        )

    else:

        latest_id = max(
            (
                message.id
                for message in messages
            ),
            default=0,
        )

        state_manager.set(
            "last_message_id",
            latest_id,
        )

        print(
            "No recent files found."
        )

    state_manager.set(
        "initialized",
        True,
    )

    await state_manager.save()


# ============================================================
# PROCESS ONE FILE
# ============================================================

async def process_message(
    application,
    state_manager,
    telethon_client,
    message,
):

    message_id = message.id

    # --------------------------------------------------------
    # Only process messages from allowed users
    # --------------------------------------------------------

    if (
        message.sender_id
        not in ALLOWED_TELEGRAM_IDS
    ):

        state_manager.set(
            "last_message_id",
            message_id,
        )

        await state_manager.save()

        return True

    # --------------------------------------------------------
    # Determine whether this is a file
    # --------------------------------------------------------

    file_info = get_telegram_file_info(
        message
    )

    if not file_info:

        state_manager.set(
            "last_message_id",
            message_id,
        )

        await state_manager.save()

        return True

    (
        filename,
        mime_type,
        expected_size,
        telegram_file_id,
    ) = file_info

    # Count received file
    state_manager.increment(
        "sent"
    )

    await state_manager.save()

    bot = application.bot

    chat_id = message.sender_id

    # --------------------------------------------------------
    # CHECK GOOGLE DRIVE FOR DUPLICATE
    # --------------------------------------------------------

    duplicate = await asyncio.to_thread(
        find_drive_duplicate_sync,
        telegram_file_id,
        filename,
        expected_size,
    )

    if duplicate:

        state_manager.increment(
            "duplicates"
        )

        state_manager.set(
            "last_message_id",
            message_id,
        )

        await state_manager.save()

        await bot.send_message(
            chat_id=chat_id,
            text=(
                "⏭️ Duplicate skipped!\n\n"
                f"📄 {filename}\n\n"
                "This file is already in "
                "Google Drive."
            ),
            reply_to_message_id=message_id,
            allow_sending_without_reply=True,
        )

        print(
            f"DUPLICATE: {filename}"
        )

        return True

    # --------------------------------------------------------
    # STATUS MESSAGE
    # --------------------------------------------------------

    try:

        status = await bot.send_message(
            chat_id=chat_id,
            text=(
                "⬇️ Downloading:\n"
                f"{filename}\n\n"
                f"📦 "
                f"{expected_size / 1024 / 1024:.2f} MB\n"
                "💾 Temporary disk storage"
            ),
            reply_to_message_id=message_id,
            allow_sending_without_reply=True,
        )

    except Exception:

        status = None

    temp_path = None

    try:

        # ====================================================
        # TEMPORARY FILE
        #
        # The complete file is stored on the GitHub runner's
        # temporary disk, NOT on your phone.
        # ====================================================

        suffix = (
            Path(filename).suffix
            or ".tmp"
        )

        with tempfile.NamedTemporaryFile(
            prefix="telegram_",
            suffix=suffix,
            delete=False,
        ) as temp_file:

            temp_path = temp_file.name

        # ====================================================
        # DOWNLOAD USING TELETHON
        # ====================================================

        print(
            f"Downloading: {filename}"
        )

        downloaded_path = (
            await telethon_client.download_media(
                message,
                file=temp_path,
            )
        )

        if not downloaded_path:

            raise RuntimeError(
                "Telegram download failed."
            )

        actual_size = os.path.getsize(
            downloaded_path
        )

        if (
            expected_size > 0
            and actual_size != expected_size
        ):

            raise RuntimeError(
                "Downloaded file size does not "
                "match Telegram's reported size."
            )

        state_manager.increment(
            "downloaded"
        )

        state_manager.increment(
            "downloaded_bytes",
            actual_size,
        )

        await state_manager.save()

        # ====================================================
        # GOOGLE DRIVE UPLOAD
        # ====================================================

        if status:

            try:

                await status.edit_text(
                    "⬆️ Uploading to Google Drive:\n"
                    f"{filename}\n\n"
                    f"📦 "
                    f"{actual_size / 1024 / 1024:.2f} MB\n"
                    "🔄 Resumable upload..."
                )

            except Exception:
                pass

        print(
            f"Uploading: {filename}"
        )

        result = await asyncio.to_thread(
            upload_file_to_drive_sync,
            downloaded_path,
            filename,
            mime_type,
            telegram_file_id,
        )

        uploaded_size = int(
            result.get(
                "size",
                actual_size,
            )
        )

        # ====================================================
        # SUCCESS
        # ====================================================

        state_manager.increment(
            "uploaded"
        )

        state_manager.increment(
            "uploaded_bytes",
            uploaded_size,
        )

        # IMPORTANT:
        #
        # Only after Drive confirms the upload do we advance
        # last_message_id.
        #
        # Therefore, if GitHub stops while a file is being
        # uploaded, that message will be checked again after
        # restart.
        #
        # The Drive duplicate check prevents a second copy
        # from being created.
        # ====================================================

        state_manager.set(
            "last_message_id",
            message_id,
        )

        await state_manager.save()

        if status:

            try:

                await status.edit_text(
                    "✅ Uploaded successfully!\n\n"
                    f"📄 {result['name']}\n"
                    f"📦 "
                    f"{uploaded_size / 1024 / 1024:.2f} MB"
                )

            except Exception:
                pass

        print(
            f"SUCCESS: {filename}"
        )

        return True

    except Exception as e:

        print(
            f"ERROR processing "
            f"{filename}: {repr(e)}"
        )

        state_manager.increment(
            "failed"
        )

        await state_manager.save()

        if status:

            try:

                await status.edit_text(
                    "❌ Upload failed!\n\n"
                    f"📄 {filename}\n\n"
                    "The message was NOT marked "
                    "as completed.\n"
                    "It will be retried."
                )

            except Exception:
                pass

        return False

    finally:

        # ====================================================
        # DELETE TEMPORARY FILE
        # ====================================================

        if temp_path:

            try:

                if os.path.exists(
                    temp_path
                ):

                    os.remove(
                        temp_path
                    )

            except Exception as e:

                print(
                    "Temporary cleanup error:",
                    repr(e),
                )


# ============================================================
# SINGLE-FILE WORKER
# ============================================================

async def processing_worker(
    application,
    state_manager,
    telethon_client,
    bot_chat,
):

    print(
        "Single-file worker started."
    )

    while True:

        try:

            last_id = state_manager.get(
                "last_message_id",
                0,
            )

            # Get messages newer than the last
            # completely processed message.
            messages = (
                await telethon_client.get_messages(
                    bot_chat,
                    limit=100,
                    min_id=last_id,
                    reverse=True,
                )
            )

            if not messages:

                await asyncio.sleep(
                    PROCESS_INTERVAL_SECONDS
                )

                continue

            for message in messages:

                if message.id <= last_id:
                    continue

                success = await process_message(
                    application,
                    state_manager,
                    telethon_client,
                    message,
                )

                if not success:

                    # Keep the failed message as the
                    # next message to retry.
                    await asyncio.sleep(10)

                    break

                last_id = state_manager.get(
                    "last_message_id",
                    last_id,
                )

        except FloodWaitError as e:

            print(
                "Telegram requested a wait:",
                e.seconds,
                "seconds",
            )

            await asyncio.sleep(
                e.seconds
            )

        except Exception as e:

            print(
                "Worker error:",
                repr(e),
            )

            await asyncio.sleep(10)


# ============================================================
# STARTUP
# ============================================================

async def post_init(
    application: Application,
):

    print(
        "Starting Telegram MTProto..."
    )

    # --------------------------------------------------------
    # Load persistent state
    # --------------------------------------------------------

    state_manager = StateManager()

    await state_manager.load()

    application.bot_data[
        "state_manager"
    ] = state_manager

    # --------------------------------------------------------
    # Start Telethon
    # --------------------------------------------------------

    telethon_client = TelegramClient(
        TELEGRAM_SESSION,
        TELEGRAM_API_ID,
        TELEGRAM_API_HASH,
    )

    await telethon_client.connect()

    if not await telethon_client.is_user_authorized():

        raise RuntimeError(
            "Telegram session is not authorized."
        )

    me = await telethon_client.get_me()

    print(
        "Telethon logged in as:",
        getattr(
            me,
            "username",
            None,
        )
        or getattr(
            me,
            "first_name",
            None,
        )
        or me.id,
    )

    # --------------------------------------------------------
    # Find bot chat
    # --------------------------------------------------------

    bot_chat = await telethon_client.get_entity(
        BOT_USERNAME
    )

    print(
        "Monitoring:",
        BOT_USERNAME,
    )

    # --------------------------------------------------------
    # Recovery initialization
    # --------------------------------------------------------

    await initialize_state(
        state_manager,
        telethon_client,
        bot_chat,
    )

    application.bot_data[
        "telethon_client"
    ] = telethon_client

    application.bot_data[
        "bot_chat"
    ] = bot_chat

    # --------------------------------------------------------
    # Start ONE worker only
    # --------------------------------------------------------

    application.create_task(
        processing_worker(
            application,
            state_manager,
            telethon_client,
            bot_chat,
        ),
        name="telegram-drive-worker",
    )

    print(
        "Telegram → Google Drive bot is running."
    )


# ============================================================
# SHUTDOWN
# ============================================================

async def post_shutdown(
    application: Application,
):

    telethon_client = (
        application.bot_data.get(
            "telethon_client"
        )
    )

    if telethon_client:

        try:

            await telethon_client.disconnect()

        except Exception as e:

            print(
                "Telethon shutdown error:",
                repr(e),
            )


# ============================================================
# MAIN
# ============================================================

def main():

    app = (
        Application.builder()
        .token(
            TELEGRAM_BOT_TOKEN
        )
        .post_init(
            post_init
        )
        .post_shutdown(
            post_shutdown
        )
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
        CommandHandler(
            "status",
            status_command,
        )
    )

    # Bot API only detects the incoming message.
    # Telethon performs the actual download.
    app.add_handler(
        MessageHandler(
            filters.AUDIO
            | filters.Document.ALL
            | filters.VIDEO,
            file_received,
        )
    )

    app.add_handler(
        MessageHandler(
            filters.ALL,
            unknown_message,
        )
    )

    print(
        "Starting Telegram → Google Drive bot..."
    )

    app.run_polling()


if __name__ == "__main__":

    main()
