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

TELEGRAM_BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]

# BOTH ACCOUNTS ARE ALLOWED
ALLOWED_TELEGRAM_IDS = {
    556318583,
    5237041275,
}


# ============================================================
# TELEGRAM MTProto
# ============================================================

TELEGRAM_API_ID = int(
    os.environ["TELEGRAM_API_ID"]
)

TELEGRAM_API_HASH = os.environ[
    "TELEGRAM_API_HASH"
]

TELEGRAM_SESSION = "drive_bot_session"

BOT_USERNAME = "Drivegesavemaadadu_bot"


# ============================================================
# GOOGLE DRIVE
# ============================================================

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


# ============================================================
# STATE / WORKER
# ============================================================

STATE_FILENAME = (
    ".telegram_drive_bot_state.json"
)

INITIAL_RECOVERY_HOURS = 12

PROCESS_INTERVAL_SECONDS = 2


# ============================================================
# DRIVE SUBFOLDERS
# ============================================================

SUBFOLDERS = {
    "audio": "Audio",
    "video": "Video",
    "image": "Images",
    "document": "Documents",
    "other": "Other",
}


# ============================================================
# GOOGLE DRIVE CONNECTION
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
# DRIVE FOLDER FUNCTIONS
# ============================================================

def find_child_folder(
    service,
    parent_id,
    folder_name,
):

    safe_name = escape_drive_value(
        folder_name
    )

    query = (
        f"name = '{safe_name}' "
        f"and '{parent_id}' in parents "
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

    return files[0] if files else None


def get_or_create_subfolder_sync(
    folder_type,
):

    service = get_drive_service()

    folder_name = SUBFOLDERS[
        folder_type
    ]

    existing = find_child_folder(
        service,
        DRIVE_FOLDER_ID,
        folder_name,
    )

    if existing:
        return existing["id"]

    metadata = {
        "name": folder_name,
        "parents": [
            DRIVE_FOLDER_ID
        ],
        "mimeType": (
            "application/vnd.google-apps.folder"
        ),
    }

    folder = (
        service.files()
        .create(
            body=metadata,
            fields="id,name",
        )
        .execute()
    )

    print(
        f"Created Drive folder: "
        f"{folder_name}"
    )

    return folder["id"]


def get_folder_type(
    filename,
    mime_type,
):

    name = filename.lower()

    if (
        mime_type.startswith("audio/")
        or name.endswith(
            (
                ".mp3",
                ".flac",
                ".m4a",
                ".aac",
                ".wav",
                ".ogg",
                ".opus",
            )
        )
    ):
        return "audio"

    if (
        mime_type.startswith("video/")
        or name.endswith(
            (
                ".mp4",
                ".mkv",
                ".avi",
                ".mov",
                ".webm",
                ".m4v",
            )
        )
    ):
        return "video"

    if (
        mime_type.startswith("image/")
        or name.endswith(
            (
                ".jpg",
                ".jpeg",
                ".png",
                ".gif",
                ".webp",
                ".bmp",
                ".heic",
            )
        )
    ):
        return "image"

    if (
        mime_type.startswith("text/")
        or name.endswith(
            (
                ".pdf",
                ".doc",
                ".docx",
                ".xls",
                ".xlsx",
                ".ppt",
                ".pptx",
                ".txt",
                ".csv",
                ".zip",
                ".rar",
                ".7z",
            )
        )
    ):
        return "document"

    return "other"


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
            "mimeType": "application/json",
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
    # 1. Exact Telegram file ID
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
                fields="files(id,name,size)",
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
    # 2. Compatibility check
    #
    # IMPORTANT:
    # Do NOT put "size = 123" in the Drive query.
    #
    # We search by filename and compare size in Python.
    # --------------------------------------------------------

    safe_name = escape_drive_value(
        filename
    )

    query = (
        f"'{DRIVE_FOLDER_ID}' in parents "
        f"and trashed = false "
        f"and name = '{safe_name}'"
    )

    result = (
        service.files()
        .list(
            q=query,
            spaces="drive",
            pageSize=100,
            fields="files(id,name,size)",
        )
        .execute()
    )

    files = result.get(
        "files",
        []
    )

    for drive_file in files:

        drive_size = int(
            drive_file.get(
                "size",
                0,
            )
            or 0
        )

        if drive_size == int(
            file_size
        ):

            return drive_file

    return None


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

    # --------------------------------------------------------
    # Choose subfolder
    # --------------------------------------------------------

    folder_type = get_folder_type(
        filename,
        mime_type,
    )

    target_folder_id = (
        get_or_create_subfolder_sync(
            folder_type
        )
    )

    # --------------------------------------------------------
    # Drive metadata
    # --------------------------------------------------------

    metadata = {
        "name": filename,
        "parents": [
            target_folder_id
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

    # --------------------------------------------------------
    # DOCUMENT
    # --------------------------------------------------------

    if message.document:

        document = message.document

        filename = None
        mime_type = (
            "application/octet-stream"
        )

        if message.file:

            filename = (
                message.file.name
            )

            mime_type = (
                message.file.mime_type
                or mime_type
            )

        if not filename:

            filename = (
                f"file_{message.id}"
            )

        file_size = int(
            document.size or 0
        )

        telegram_file_id = str(
            document.id
        )

        return (
            filename,
            mime_type,
            file_size,
            telegram_file_id,
        )

    # --------------------------------------------------------
    # PHOTO
    #
    # DO NOT use message.file here.
    #
    # That was causing:
    # AttributeError:
    # 'PhotoSize' object has no attribute 'location'
    # --------------------------------------------------------

    if message.photo:

        photo = message.photo

        file_size = 0

        if getattr(
            photo,
            "sizes",
            None,
        ):

            largest = max(
                photo.sizes,
                key=lambda x: (
                    getattr(
                        x,
                        "size",
                        0,
                    )
                    or 0
                ),
            )

            file_size = int(
                getattr(
                    largest,
                    "size",
                    0,
                )
                or 0
            )

        filename = (
            f"photo_{message.id}.jpg"
        )

        mime_type = "image/jpeg"

        telegram_file_id = str(
            photo.id
        )

        return (
            filename,
            mime_type,
            file_size,
            telegram_file_id,
        )

    return None


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
        "Send MP3, FLAC, video, photo, "
        "document, or any supported file.\n\n"
        "📦 Large-file mode: ON\n"
        "🔄 One-file-at-a-time: ON\n"
        "⏭️ Duplicate check: ON\n"
        "📁 Automatic subfolders: ON\n\n"
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
        context.application.bot_data[
            "state_manager"
        ]
    )

    state = state_manager.state

    await update.message.reply_text(

        "🟢 BOT STATUS\n\n"

        "✅ Telegram Bot API\n"
        "✅ Telegram MTProto\n"
        "✅ Google Drive\n"
        "✅ Large-file mode\n"
        "✅ One-file queue\n"
        "✅ Automatic folders\n"
        "💾 State saved in Drive\n\n"

        f"📌 Last processed message: "
        f"{state.get('last_message_id', 0)}"
    )


# ============================================================
# BOT API FILE HANDLER
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

    # IMPORTANT:
    # Do NOT download here.
    #
    # Telethon downloads the actual file.
    #
    # This allows files larger than the normal
    # Bot API download limitation.

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

    return


# ============================================================
# FIRST RUN
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

        message_date = message.date

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

        if (
            not message.document
            and not message.photo
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
