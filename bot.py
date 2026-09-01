import os
import io
import json
import asyncio
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

TELEGRAM_BOT_TOKEN = os.environ[
    "TELEGRAM_BOT_TOKEN"
]

# Your two allowed Telegram accounts
ALLOWED_TELEGRAM_IDS = {
    556318583,
    5237041275,
}

# Telethon
TELEGRAM_API_ID = int(
    os.environ["TELEGRAM_API_ID"]
)

TELEGRAM_API_HASH = os.environ[
    "TELEGRAM_API_HASH"
]

TELEGRAM_SESSION = (
    "drive_bot_session"
)

# Telegram bot username
BOT_USERNAME = (
    "Drivegesavemaadadu_bot"
)

# Main Google Drive folder
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

# Persistent state file
STATE_FILENAME = (
    ".telegram_drive_bot_state.json"
)

# On first run, only recover recent messages
INITIAL_RECOVERY_HOURS = 12

# Wait between worker checks
PROCESS_INTERVAL_SECONDS = 2


# ============================================================
# DRIVE SUBFOLDERS
# ============================================================

# These folders will automatically be created
# inside your main DRIVE_FOLDER_ID.

DRIVE_SUBFOLDER_NAMES = {
    "audio": "Music",
    "video": "Videos",
    "photo": "Photos",
    "document": "Documents",
    "other": "Other",
}


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
# DRIVE SUBFOLDER MANAGEMENT
# ============================================================

def find_or_create_subfolder_sync(
    service,
    folder_name,
):

    safe_name = escape_drive_value(
        folder_name
    )

    query = (
        f"name = '{safe_name}' "
        f"and '{DRIVE_FOLDER_ID}' in parents "
        f"and mimeType = "
        f"'application/vnd.google-apps.folder' "
        f"and trashed = false"
    )

    result = (
        service.files()
        .list(
            q=query,
            spaces="drive",
            pageSize=10,
            fields="files(id,name)",
        )
        .execute()
    )

    files = result.get(
        "files",
        []
    )

    if files:
        return files[0]["id"]

    metadata = {
        "name": folder_name,
        "parents": [
            DRIVE_FOLDER_ID
        ],
        "mimeType": (
            "application/vnd.google-apps.folder"
        ),
    }

    created = (
        service.files()
        .create(
            body=metadata,
            fields="id,name",
        )
        .execute()
    )

    print(
        f"Created Drive subfolder: "
        f"{folder_name}"
    )

    return created["id"]


def get_category_folder_sync(
    category,
):

    service = get_drive_service()

    folder_name = (
        DRIVE_SUBFOLDER_NAMES.get(
            category,
            "Other",
        )
    )

    return find_or_create_subfolder_sync(
        service,
        folder_name,
    )


# ============================================================
# STATE FILE
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

    return (
        files[0]
        if files
        else None
    )


def load_state_sync():

    service = get_drive_service()

    state_file = find_state_file(
        service
    )

    if not state_file:
        return None

    request = (
        service.files()
        .get_media(
            fileId=state_file["id"]
        )
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

        (
            service.files()
            .update(
                fileId=state_file["id"],
                media_body=media,
            )
            .execute()
        )

    else:

        metadata = {
            "name": STATE_FILENAME,
            "parents": [
                DRIVE_FOLDER_ID
            ],
            "mimeType": (
                "application/json"
            ),
        }

        (
            service.files()
            .create(
                body=metadata,
                media_body=media,
                fields="id",
            )
            .execute()
        )


DEFAULT_STATE = {

    "version": 2,

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

        self.state = (
            await asyncio.to_thread(
                load_state_sync
            )
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
    folder_id,
):

    service = get_drive_service()

    # --------------------------------------------------------
    # First: exact Telegram file ID
    # --------------------------------------------------------

    if telegram_file_id:

        safe_id = (
            escape_drive_value(
                telegram_file_id
            )
        )

        query = (
            f"'{folder_id}' in parents "
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
    # Second: filename compatibility check
    #
    # IMPORTANT:
    # Google Drive does NOT search size here.
    # We compare size in Python afterward.
    # --------------------------------------------------------

    safe_name = (
        escape_drive_value(
            filename
        )
    )

    query = (
        f"'{folder_id}' in parents "
        f"and trashed = false "
        f"and name = '{safe_name}'"
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

    for file in files:

        if int(
            file.get("size", 0)
        ) == int(file_size):

            return file

    return None


# ============================================================
# DRIVE UPLOAD
# ============================================================

def upload_file_to_drive_sync(
    file_path,
    filename,
    mime_type,
    telegram_file_id,
    folder_id,
):

    service = get_drive_service()

    metadata = {
        "name": filename,
        "parents": [
            folder_id
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
            fields=(
                "id,name,size"
            ),
        )
    )

    response = None

    while response is None:

        status, response = (
            request.next_chunk()
        )

        if status:

            percent = int(
                status.progress()
                * 100
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

    # --------------------------------------------------------
    # AUDIO / DOCUMENT / VIDEO
    # --------------------------------------------------------

    if message.document:

        document = message.document

        filename = None
        mime_type = (
            "application/octet-stream"
        )

        if document.attributes:

            for attribute in (
                document.attributes
            ):

                if hasattr(
                    attribute,
                    "file_name",
                ):

                    filename = (
                        attribute.file_name
                    )

        mime_type = (
            document.mime_type
            or mime_type
        )

        if not filename:

            extension = ""

            if mime_type == "audio/mpeg":
                extension = ".mp3"

            elif mime_type == "audio/flac":
                extension = ".flac"

            elif mime_type.startswith(
                "video/"
            ):
                extension = ".mp4"

            filename = (
                f"file_{message.id}"
                f"{extension}"
            )

        file_size = int(
            document.size or 0
        )

        telegram_file_id = (
            str(document.id)
        )

        if mime_type.startswith(
            "audio/"
        ):

            category = "audio"

        elif mime_type.startswith(
            "video/"
        ):

            category = "video"

        else:

            category = "document"

        return {
            "filename": filename,
            "mime_type": mime_type,
            "file_size": file_size,
            "telegram_file_id": (
                telegram_file_id
            ),
            "category": category,
        }

    # --------------------------------------------------------
    # PHOTO
    #
    # IMPORTANT:
    # We do NOT access PhotoSize.location.
    # This fixes the PhotoSize error.
    # --------------------------------------------------------

    if message.photo:

        photo = message.photo

        filename = (
            f"photo_{message.id}.jpg"
        )

        file_size = 0

        try:

            if hasattr(
                photo,
                "sizes",
            ):

                sizes = photo.sizes

                if sizes:

                    largest = sizes[-1]

                    file_size = int(
                        getattr(
                            largest,
                            "size",
                            0,
                        )
                        or 0
                    )

        except Exception:

            file_size = 0

        telegram_file_id = str(
            getattr(
                photo,
                "id",
                f"message:{message.id}",
            )
        )

        return {
            "filename": filename,
            "mime_type": "image/jpeg",
            "file_size": file_size,
            "telegram_file_id": (
                telegram_file_id
            ),
            "category": "photo",
        }

    return None


# ============================================================
# TELEGRAM PERMISSION
# ============================================================

def is_allowed(
    update: Update
):

    return bool(
        update.effective_user
        and update.effective_user.id
        in ALLOWED_TELEGRAM_IDS
    )


# ============================================================
# /START
# ============================================================

async def start(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):

    if not is_allowed(update):
        return

    await update.message.reply_text(
        "🎵 Telegram → Google Drive Bot\n\n"
        "Send any file.\n\n"
        "📦 Large-file mode: ON\n"
        "📁 Automatic subfolders: ON\n"
        "⏭️ Duplicate checking: ON\n"
        "🔄 One-file-at-a-time: ON\n\n"
        "📊 /stats\n"
        "🟢 /status"
    )


# ============================================================
# /STATS
# ============================================================

async def stats(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):

    if not is_allowed(update):
        return

    state_manager = (
        context.application
        .bot_data[
            "state_manager"
        ]
    )

    state = (
        state_manager.state
    )

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

        f"📥 Files received: "
        f"{state.get('sent', 0)}\n"

        f"⬇️ Downloaded: "
        f"{state.get('downloaded', 0)}\n"

        f"☁️ Uploaded: "
        f"{state.get('uploaded', 0)}\n"

        f"❌ Failed: "
        f"{state.get('failed', 0)}\n"

        f"⏭️ Duplicates: "
        f"{state.get('duplicates', 0)}\n\n"

        f"📦 Downloaded: "
        f"{downloaded_mb:.2f} MB\n"

        f"☁️ Uploaded: "
        f"{uploaded_mb:.2f} MB"
    )


# ============================================================
# /STATUS
# ============================================================

async def status_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):

    if not is_allowed(update):
        return

    state_manager = (
        context.application
        .bot_data[
            "state_manager"
        ]
    )

    state = (
        state_manager.state
    )

    await update.message.reply_text(

        "🟢 BOT STATUS\n\n"

        "✅ Telegram Bot API\n"
        "✅ Telegram MTProto\n"
        "✅ Google Drive\n"
        "✅ Large-file mode\n"
        "✅ Automatic subfolders\n"
        "✅ Duplicate checking\n"
        "✅ One-file queue\n\n"

        f"📌 Last processed: "
        f"{state.get('last_message_id', 0)}"
    )


# ============================================================
# BOT API FILE DETECTOR
# ============================================================

async def file_received(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):

    if not is_allowed(update):
        return

    if not update.message:
        return

    print(
        "Bot API received file message:",
        update.message.message_id,
    )

    # We intentionally do not download here.
    #
    # Telethon handles the real download.
    #
    # This allows large files.


# ============================================================
# FIRST-RUN INITIALIZATION
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
        "First run: checking recent "
        "Telegram history..."
    )

    cutoff = (
        datetime.now(timezone.utc)
        - timedelta(
            hours=INITIAL_RECOVERY_HOURS
        )
    )

    messages = (
        await telethon_client.get_messages(
            bot_chat,
            limit=5000,
        )
    )

    eligible = []

    for message in messages:

        if not message.date:
            continue

        message_date = (
            message.date
        )

        if (
            message_date.tzinfo
            is None
        ):

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

        if not (
            message.document
            or message.photo
        ):

            continue

        eligible.append(
            message
        )

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
            f"recent file(s)."
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
    # Permission
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
    # Get file information
    # --------------------------------------------------------

    file_info = (
        get_telegram_file_info(
            message
        )
    )

    if not file_info:

        # Not a supported file.
        # Mark it processed so the worker
        # doesn't get stuck.

        state_manager.set(
            "last_message_id",
            message_id,
        )

        await state_manager.save()

        return True

    filename = (
        file_info["filename"]
    )

    mime_type = (
        file_info["mime_type"]
    )

    expected_size = (
        file_info["file_size"]
    )

    telegram_file_id = (
        file_info[
            "telegram_file_id"
        ]
    )

    category = (
        file_info["category"]
    )

    state_manager.increment(
        "sent"
    )

    await state_manager.save()

    bot = application.bot

    chat_id = (
        message.sender_id
    )

    # --------------------------------------------------------
    # Find category subfolder
    # --------------------------------------------------------

    try:

        folder_id = (
            await asyncio.to_thread(
                get_category_folder_sync,
                category,
            )
        )

    except Exception as e:

        print(
            "Folder error:",
            repr(e),
        )

        state_manager.increment(
            "failed"
        )

        await state_manager.save()

        return False

    # --------------------------------------------------------
    # Duplicate check
    # --------------------------------------------------------

    duplicate = (
        await asyncio.to_thread(
            find_drive_duplicate_sync,
            telegram_file_id,
            filename,
            expected_size,
            folder_id,
        )
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

        try:

            await bot.send_message(
                chat_id=chat_id,
                text=(
                    "⏭️ Duplicate skipped!\n\n"
                    f"📄 {filename}\n"
                    f"📁 {DRIVE_SUBFOLDER_NAMES.get(category, 'Other')}\n\n"
                    "Already in Google Drive."
                ),
                reply_to_message_id=(
                    message_id
                ),
                allow_sending_without_reply=True,
            )

        except Exception:
            pass

        print(
            f"DUPLICATE: {filename}"
        )

        return True

    # --------------------------------------------------------
    # Status message
    # --------------------------------------------------------

    try:

        status = (
            await bot.send_message(
                chat_id=chat_id,
                text=(
                    "⬇️ Downloading...\n\n"
                    f"📄 {filename}\n"
                    f"📁 {DRIVE_SUBFOLDER_NAMES.get(category, 'Other')}\n"
                    f"📦 {expected_size / 1024 / 1024:.2f} MB"
                ),
                reply_to_message_id=(
                    message_id
                ),
                allow_sending_without_reply=True,
            )
        )

    except Exception:

        status = None

    temp_path = None

    try:

        # ----------------------------------------------------
        # Temporary disk file
        # ----------------------------------------------------

        suffix = (
            Path(filename).suffix
            or ".tmp"
        )

        with tempfile.NamedTemporaryFile(
            prefix="telegram_",
            suffix=suffix,
            delete=False,
        ) as temp_file:

            temp_path = (
                temp_file.name
            )

        # ----------------------------------------------------
        # Download using Telethon
        #
        # We deliberately use the Message object.
        # We do NOT touch PhotoSize.location.
        # ----------------------------------------------------

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

        actual_size = (
            os.path.getsize(
                downloaded_path
            )
        )

        # ----------------------------------------------------
        # Size validation
        #
        # Photo size can be unavailable, so only validate
        # when Telegram actually reported a size.
        # ----------------------------------------------------

        if (
            expected_size > 0
            and actual_size
            != expected_size
            and category != "photo"
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

        # ----------------------------------------------------
        # Upload status
        # ----------------------------------------------------

        if status:

            try:

                await status.edit_text(
                    "⬆️ Uploading to Google Drive...\n\n"
                    f"📄 {filename}\n"
                    f"📁 {DRIVE_SUBFOLDER_NAMES.get(category, 'Other')}\n"
                    f"📦 {actual_size / 1024 / 1024:.2f} MB"
                )

            except Exception:
                pass

        # ----------------------------------------------------
        # Drive upload
        # ----------------------------------------------------

        print(
            f"Uploading: {filename}"
        )

        result = (
            await asyncio.to_thread(
                upload_file_to_drive_sync,
                downloaded_path,
                filename,
                mime_type,
                telegram_file_id,
                folder_id,
            )
        )

        uploaded_size = int(
            result.get(
                "size",
                actual_size,
            )
        )

        # ----------------------------------------------------
        # SUCCESS
        # ----------------------------------------------------

        state_manager.increment(
            "uploaded"
        )

        state_manager.increment(
            "uploaded_bytes",
            uploaded_size,
        )

        # Only mark processed after Drive
        # confirms successful upload.

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
                    f"📁 {DRIVE_SUBFOLDER_NAMES.get(category, 'Other')}\n"
                    f"📦 {uploaded_size / 1024 / 1024:.2f} MB"
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

        # ----------------------------------------------------
        # Delete temporary file
        # ----------------------------------------------------

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

            last_id = (
                state_manager.get(
                    "last_message_id",
                    0,
                )
            )

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

                if (
                    message.id
                    <= last_id
                ):

                    continue

                success = (
                    await process_message(
                        application,
                        state_manager,
                        telethon_client,
                        message,
                    )
                )

                if not success:

                    # Retry this message later.
                    await asyncio.sleep(
                        10
                    )

                    break

                last_id = (
                    state_manager.get(
                        "last_message_id",
                        last_id,
                    )
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

            await asyncio.sleep(
                10
            )


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
    # Load state
    # --------------------------------------------------------

    state_manager = (
        StateManager()
    )

    await state_manager.load()

    application.bot_data[
        "state_manager"
    ] = state_manager

    # --------------------------------------------------------
    # Start Telethon
    # --------------------------------------------------------

    telethon_client = (
        TelegramClient(
            TELEGRAM_SESSION,
            TELEGRAM_API_ID,
            TELEGRAM_API_HASH,
        )
    )

    await telethon_client.connect()

    authorized = (
        await telethon_client
        .is_user_authorized()
    )

    if not authorized:

        raise RuntimeError(
            "Telegram session is not authorized."
        )

    me = (
        await telethon_client.get_me()
    )

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
    # Find bot conversation
    # --------------------------------------------------------

    bot_chat = (
        await telethon_client.get_entity(
            BOT_USERNAME
        )
    )

    print(
        "Monitoring:",
        BOT_USERNAME,
    )

    # --------------------------------------------------------
    # First-run recovery
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
    # Worker
    #
    # We store the coroutine so main() can start it
    # after the application itself is running.
    # --------------------------------------------------------

    application.bot_data[
        "worker_started"
    ] = False

    print(
        "Telegram → Google Drive bot initialized."
    )


# ============================================================
# START WORKER AFTER APPLICATION STARTS
# ============================================================

async def start_worker_after_start(
    application: Application,
):

    if application.bot_data.get(
        "worker_started",
        False,
    ):

        return

    application.bot_data[
        "worker_started"
    ] = True

    print(
        "Starting single-file worker..."
    )

    asyncio.create_task(
        processing_worker(
            application,
            application.bot_data[
                "state_manager"
            ],
            application.bot_data[
                "telethon_client"
            ],
            application.bot_data[
                "bot_chat"
            ],
        )
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

    application = (
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

    # --------------------------------------------------------
    # Commands
    # --------------------------------------------------------

    application.add_handler(
        CommandHandler(
            "start",
            start,
        )
    )

    application.add_handler(
        CommandHandler(
            "stats",
            stats,
        )
    )

    application.add_handler(
        CommandHandler(
            "status",
            status_command,
        )
    )

    # --------------------------------------------------------
    # File messages
    #
    # PHOTO is now included.
    # --------------------------------------------------------

    application.add_handler(
        MessageHandler(
            (
                filters.AUDIO
                | filters.Document.ALL
                | filters.VIDEO
                | filters.PHOTO
            ),
            file_received,
        )
    )

    # --------------------------------------------------------
    # Start polling
    # --------------------------------------------------------

    print(
        "Starting Telegram → Google Drive bot..."
    )

    # --------------------------------------------------------
    # We use a small wrapper so the worker starts only
    # after polling/application startup.
    # --------------------------------------------------------

    async def run():

        await application.initialize()

        await application.post_init(
            application
        )

        await application.start()

        await start_worker_after_start(
            application
        )

        await application.updater.start_polling()

        print(
            "Telegram → Google Drive bot is running."
        )

        try:

            while True:

                await asyncio.sleep(
                    3600
                )

        finally:

            await application.updater.stop()

            await application.stop()

            await application.post_shutdown(
                application
            )

            await application.shutdown()

    asyncio.run(
        run()
    )


if __name__ == "__main__":

    main()
