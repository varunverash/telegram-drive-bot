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
