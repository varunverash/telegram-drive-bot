import os
import io
import json
import asyncio
import tempfile
import time
import threading
from pathlib import Path
from datetime import datetime, timedelta, timezone

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, BotCommand
from telegram.ext import (
    Application, CommandHandler, CallbackQueryHandler,
    MessageHandler, ContextTypes, filters,
)

from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload, MediaIoBaseUpload

from telethon import TelegramClient
from telethon.errors import FloodWaitError


# ============================================================
# CONFIGURATION
# ============================================================

TELEGRAM_BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]

# Add/remove Telegram user IDs here
ALLOWED_TELEGRAM_IDS = {
    556318583,
    5237041275,
    # Add more IDs here later:
    # 123456789,
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

# New session name so the old session does not interfere
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
# STATE / WORKER SETTINGS
# ============================================================

STATE_FILENAME = (
    ".telegram_drive_bot_state.json"
)

INITIAL_RECOVERY_HOURS = 12

PROCESS_INTERVAL_SECONDS = 2

# Files above this are considered large
LARGE_FILE_BYTES = (
    200 * 1024 * 1024
)

# Up to 4 small files simultaneously
SMALL_CONCURRENCY = 4

# Google Drive resumable upload chunk
DRIVE_CHUNK_SIZE = (
    8 * 1024 * 1024
)

# Telegram status message update interval
STATUS_UPDATE_SECONDS = 2
WORKFLOW_STARTED_AT = time.monotonic()

# Retry failed files after this delay
RETRY_DELAY_SECONDS = 10


# ============================================================
# DRIVE SUBFOLDERS
# ============================================================

SUBFOLDERS = {
    "video": "Video",
    "music": "Music",
    "image": "Image",
    "other": "Other",
}


# Existing/old folders that should be merged
LEGACY_FOLDERS = {
    "Audio": "music",
    "Images": "image",
    "Image": "image",
    "Documents": "other",
}


# ============================================================
# FILE EXTENSIONS
# ============================================================

VIDEO_EXTS = {
    ".mp4",
    ".mkv",
    ".avi",
    ".mov",
    ".webm",
    ".m4v",
    ".wmv",
    ".flv",
    ".mpeg",
    ".mpg",
    ".ts",
}

MUSIC_EXTS = {
    ".mp3",
    ".flac",
    ".m4a",
    ".aac",
    ".wav",
    ".ogg",
    ".opus",
    ".wma",
    ".alac",
}

IMAGE_EXTS = {
    ".jpg",
    ".jpeg",
    ".png",
    ".gif",
    ".webp",
    ".bmp",
    ".heic",
    ".tif",
    ".tiff",
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


def list_all_files_sync(
    service,
    query,
    fields=(
        "files(id,name,size,mimeType,"
        "parents,appProperties)"
    ),
):

    output = []

    page_token = None

    while True:

        result = (
            service.files()
            .list(
                q=query,
                spaces="drive",
                pageSize=1000,
                pageToken=page_token,
                fields=(
                    f"nextPageToken,{fields}"
                ),
            )
            .execute()
        )

        output.extend(
            result.get(
                "files",
                [],
            )
        )

        page_token = result.get(
            "nextPageToken"
        )

        if not page_token:
            return output


# ============================================================
# DRIVE FOLDER FUNCTIONS
# ============================================================

def find_child_folder_sync(
    service,
    parent_id,
    name,
):

    query = (
        f"name = "
        f"'{escape_drive_value(name)}' "
        f"and '{parent_id}' in parents "
        f"and mimeType = "
        f"'application/vnd.google-apps.folder' "
        f"and trashed = false"
    )

    files = list_all_files_sync(
        service,
        query,
        "files(id,name)",
    )

    return (
        files[0]
        if files
        else None
    )


def get_or_create_folder_sync(
    folder_type,
):

    service = get_drive_service()

    name = SUBFOLDERS[
        folder_type
    ]

    existing = (
        find_child_folder_sync(
            service,
            DRIVE_FOLDER_ID,
            name,
        )
    )

    if existing:
        return existing["id"]

    folder = (
        service.files()
        .create(
            body={
                "name": name,
                "parents": [
                    DRIVE_FOLDER_ID
                ],
                "mimeType": (
                    "application/"
                    "vnd.google-apps.folder"
                ),
            },
            fields="id,name",
        )
        .execute()
    )

    return folder["id"]


def get_folder_ids_sync():

    return {
        key: get_or_create_folder_sync(
            key
        )
        for key in SUBFOLDERS
    }


# ============================================================
# FILE CLASSIFICATION
# ============================================================

def classify_file(
    filename,
    mime_type="",
):

    name = (
        filename or ""
    ).lower()

    extension = (
        Path(name).suffix.lower()
    )

    mime = (
        mime_type or ""
    ).lower()

    if (
        mime.startswith("video/")
        or extension in VIDEO_EXTS
    ):
        return "video"

    if (
        mime.startswith("audio/")
        or extension in MUSIC_EXTS
    ):
        return "music"

    if (
        mime.startswith("image/")
        or extension in IMAGE_EXTS
    ):
        return "image"

    return "other"


# ============================================================
# ORGANIZE EXISTING DRIVE FILES
# ============================================================

def organize_drive_sync():

    service = get_drive_service()

    folders = (
        get_folder_ids_sync()
    )

    root_query = (
        f"'{DRIVE_FOLDER_ID}' "
        f"in parents "
        f"and trashed = false"
    )

    root_items = (
        list_all_files_sync(
            service,
            root_query,
        )
    )

    moved = 0

    # --------------------------------------------------------
    # Move unorganized files from main folder
    # --------------------------------------------------------

    for item in root_items:

        name = item.get(
            "name",
            "",
        )

        if (
            item.get("mimeType")
            == "application/"
            "vnd.google-apps.folder"
        ):
            continue

        if name == STATE_FILENAME:
            continue

        kind = classify_file(
            name,
            item.get(
                "mimeType",
                "",
            ),
        )

        target = folders[
            kind
        ]

        (
            service.files()
            .update(
                fileId=item["id"],
                addParents=target,
                removeParents=(
                    DRIVE_FOLDER_ID
                ),
                fields="id,parents",
            )
            .execute()
        )

        moved += 1

    # --------------------------------------------------------
    # Merge old folders
    # Audio -> Music
    # Images -> Image
    # Documents -> Other
    # --------------------------------------------------------

    for (
        legacy_name,
        kind,
    ) in LEGACY_FOLDERS.items():

        legacy = (
            find_child_folder_sync(
                service,
                DRIVE_FOLDER_ID,
                legacy_name,
            )
        )

        if not legacy:
            continue

        if (
            legacy["id"]
            == folders[kind]
        ):
            continue

        items = list_all_files_sync(
            service,
            (
                f"'{legacy['id']}' "
                f"in parents "
                f"and trashed = false"
            ),
        )

        for item in items:

            if (
                item.get("mimeType")
                == "application/"
                "vnd.google-apps.folder"
            ):
                continue

            (
                service.files()
                .update(
                    fileId=item["id"],
                    addParents=folders[
                        kind
                    ],
                    removeParents=legacy[
                        "id"
                    ],
                    fields="id,parents",
                )
                .execute()
            )

            moved += 1

        # Delete empty old folder
        remaining = list_all_files_sync(
            service,
            (
                f"'{legacy['id']}' "
                f"in parents "
                f"and trashed = false"
            ),
            "files(id,mimeType)",
        )

        if not remaining:

            try:

                (
                    service.files()
                    .delete(
                        fileId=legacy["id"]
                    )
                    .execute()
                )

                print(
                    "Removed empty "
                    f"legacy folder: "
                    f"{legacy_name}"
                )

            except Exception:
                pass

    return moved
  # ============================================================
# STATE FILE
# ============================================================

def find_state_file(
    service,
):

    query = (
        f"name = "
        f"'{escape_drive_value(STATE_FILENAME)}' "
        f"and '{DRIVE_FOLDER_ID}' in parents "
        f"and trashed = false"
    )

    files = list_all_files_sync(
        service,
        query,
        "files(id,name,size)",
    )

    return (
        files[0]
        if files
        else None
    )


def load_state_sync():

    service = get_drive_service()

    state_file = (
        find_state_file(
            service
        )
    )

    if not state_file:
        return None

    data = (
        service.files()
        .get_media(
            fileId=state_file["id"]
        )
        .execute()
    )

    return json.loads(
        data.decode("utf-8")
    )


def save_state_sync(
    state,
):

    service = get_drive_service()

    data = json.dumps(
        state,
        indent=2,
        ensure_ascii=False,
    ).encode()

    media = MediaIoBaseUpload(
        io.BytesIO(data),
        mimetype="application/json",
        resumable=False,
    )

    state_file = (
        find_state_file(
            service
        )
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

        (
            service.files()
            .create(
                body={
                    "name": STATE_FILENAME,
                    "parents": [
                        DRIVE_FOLDER_ID
                    ],
                    "mimeType": (
                        "application/json"
                    ),
                },
                media_body=media,
                fields="id",
            )
            .execute()
        )


# ============================================================
# DEFAULT STATE
# ============================================================

DEFAULT_STATE = {

    "version": 4,

    "sent": 0,
    "downloaded": 0,
    "uploaded": 0,

    "failed": 0,
    "duplicates": 0,
    "cancelled": 0,

    "downloaded_bytes": 0,
    "uploaded_bytes": 0,

    # Separate message position for each user
    "last_message_ids": {},

    # Completed messages waiting for earlier messages
    "completed_message_ids": {},

    "initialized_users": [],
}


# ============================================================
# STATE MANAGER
# ============================================================

class StateManager:

    def __init__(self):

        self.state = None

        self.lock = (
            asyncio.Lock()
        )


    async def load(self):

        self.state = (
            await asyncio.to_thread(
                load_state_sync
            )
        )

        if not self.state:

            self.state = json.loads(
                json.dumps(
                    DEFAULT_STATE
                )
            )

        self.state.setdefault(
            "last_message_ids",
            {},
        )

        self.state.setdefault(
            "completed_message_ids",
            {},
        )

        self.state.setdefault(
            "initialized_users",
            [],
        )

        for (
            key,
            value,
        ) in DEFAULT_STATE.items():

            if key not in self.state:
                self.state[key] = value

        # Compatibility with previous version
        old_last_id = self.state.pop(
            "last_message_id",
            None,
        )

        if (
            old_last_id is not None
            and not self.state[
                "last_message_ids"
            ]
        ):

            self.state[
                "last_message_ids"
            ]["legacy"] = int(
                old_last_id
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


    def inc(
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
# TRANSFER CANCELLATION
# ============================================================

class TransferCancelled(
    Exception
):
    pass


# ============================================================
# DISPLAY HELPERS
# ============================================================

def human_bytes(n):

    n = float(
        n or 0
    )

    for unit in (
        "B",
        "KB",
        "MB",
        "GB",
        "TB",
    ):

        if (
            n < 1024
            or unit == "TB"
        ):

            return (
                f"{n:.1f} {unit}"
            )

        n /= 1024


def eta_text(seconds):

    if (
        seconds is None
        or seconds <= 0
        or seconds
        > 7 * 24 * 3600
    ):

        return "—"

    seconds = int(
        seconds
    )

    if seconds < 60:
        return f"{seconds}s"

    if seconds < 3600:

        return (
            f"{seconds // 60}m "
            f"{seconds % 60:02d}s"
        )

    return (
        f"{seconds // 3600}h "
        f"{(seconds % 3600) // 60:02d}m"
    )


def progress_bar(
    done,
    total,
    width=16,
):

    if not total:
        return "░" * width

    percentage = max(
        0.0,
        min(
            1.0,
            done / total,
        ),
    )

    filled = int(
        percentage * width
    )

    return (
        "█" * filled
        + "░" * (
            width - filled
        )
    )


# ============================================================
# LIVE STATUS MESSAGE
# ============================================================

def build_status(
    stage,
    filename,
    done,
    total,
    speed,
    user_id,
    message_id,
    cancel=True,
):

    percentage = (
        int(
            done * 100 / total
        )
        if total
        else 0
    )

    eta = (
        (total - done) / speed
        if (
            speed
            and speed > 0
            and total
        )
        else None
    )

    text = (

        f"{'⬇️ Downloading' if stage == 'download' else '⬆️ Uploading'}\n"

        f"{filename}\n\n"

        f"{progress_bar(done, total)} "
        f"{percentage}%\n"

        f"{human_bytes(done)} / "
        f"{human_bytes(total)}\n\n"

        f"⚡ Speed: "
        f"{human_bytes(speed)}/s\n"

        f"⏱️ ETA: "
        f"{eta_text(eta)}"
    )

    markup = (
        InlineKeyboardMarkup(
            [[
                InlineKeyboardButton(
                    "🛑 Cancel",
                    callback_data=(
                        f"cancel:"
                        f"{user_id}:"
                        f"{message_id}"
                    ),
                )
            ]]
        )
        if cancel
        else None
    )

    return text, markup


# ============================================================
# ACTIVE JOB
# ============================================================

class Job:

    def __init__(
        self,
        user_id,
        message_id,
        filename,
        size,
    ):

        self.user_id = (
            user_id
        )

        self.message_id = (
            message_id
        )

        self.filename = (
            filename
        )

        self.size = size

        self.cancel = (
            threading.Event()
        )

        self.progress = {
            "stage": "",
            "done": 0,
            "total": size,
            "speed": 0.0,
        }

        self.started = (
            time.monotonic()
        )

        self.status_message = None


# ============================================================
# DRIVE DUPLICATE CHECK
# ============================================================
# DRIVE DUPLICATE CHECK
# ============================================================

def find_duplicate_sync(
    folder_id,
    telegram_file_id,
    filename,
    file_size,
):

    service = get_drive_service()

    # --------------------------------------------------------
    # 1. Exact Telegram file ID
    #
    # This is the strongest duplicate check.
    # --------------------------------------------------------

    if telegram_file_id:

        safe_id = (
            escape_drive_value(
                telegram_file_id
            )
        )

        query = (
            f"'{folder_id}' "
            f"in parents "
            f"and trashed = false "
            f"and appProperties has "
            f"{{ key='telegram_file_id' "
            f"and value='{safe_id}' }}"
        )

        files = list_all_files_sync(
            service,
            query,
            "files(id,name,size)",
        )

        if files:
            return files[0]

    # --------------------------------------------------------
    # 2. Filename + size compatibility check
    #
    # We intentionally retrieve the files in the folder and
    # compare names/sizes in Python.
    #
    # This allows us to handle small Telegram filename changes.
    # --------------------------------------------------------

    query = (
        f"'{folder_id}' "
        f"in parents "
        f"and trashed = false"
    )

    files = list_all_files_sync(
        service,
        query,
        "files(id,name,size)",
    )

    expected_size = int(
        file_size or 0
    )

    # --------------------------------------------------------
    # Normalize filename for comparison
    #
    # Examples:
    #
    # "My Song.mp3"
    # "my_song.mp3"
    # "MY-SONG.mp3"
    #
    # are treated as equivalent.
    # --------------------------------------------------------

    def normalize_filename(name):

        name = str(
            name or ""
        ).strip().lower()

        # Remove extension separately
        # so .mp3 remains important.
        if "." in name:

            stem, extension = (
                name.rsplit(
                    ".",
                    1
                )
            )

            extension = (
                extension.strip()
            )

        else:

            stem = name
            extension = ""

        # Treat common separators as the same
        for char in (
            "_",
            "-",
            "(",
            ")",
            "[",
            "]",
            "{",
            "}",
            ",",
        ):

            stem = stem.replace(
                char,
                " ",
            )

        # Collapse repeated spaces
        stem = " ".join(
            stem.split()
        )

        return (
            stem,
            extension,
        )

    wanted_stem, wanted_ext = (
        normalize_filename(
            filename
        )
    )

    # --------------------------------------------------------
    # Compare every file in the destination folder
    # --------------------------------------------------------

    for drive_file in files:

        drive_size = int(
            drive_file.get(
                "size"
            )
            or 0
        )

        # Size must match
        if drive_size != expected_size:
            continue

        drive_name = (
            drive_file.get(
                "name"
            )
            or ""
        )

        drive_stem, drive_ext = (
            normalize_filename(
                drive_name
            )
        )

        # Extension must match
        if drive_ext != wanted_ext:
            continue

        # Normalized filename must match
        if drive_stem == wanted_stem:

            print(
                "🔁 Duplicate found "
                "by filename + size:",
                drive_name,
            )

            return drive_file

    return None

# ============================================================
# ONE-TIME MUSIC DUPLICATE CLEANUP
# DRY RUN ONLY
# ============================================================

# ============================================================
# ONE-TIME MUSIC DUPLICATE CLEANUP
# MOVE DUPLICATES TO GOOGLE DRIVE TRASH
# ============================================================

def cleanup_music_duplicates_once():

    service = get_drive_service()

    folders = get_folder_ids_sync()

    music_folder_id = folders["music"]

    query = (
        f"'{music_folder_id}' in parents "
        f"and trashed = false"
    )

    files = list_all_files_sync(
        service,
        query,
        "files(id,name,size)",
    )

    print(
        f"🎵 Music files found: {len(files)}"
    )

    groups = {}

    for drive_file in files:

        name = drive_file.get(
            "name",
            ""
        )

        size = int(
            drive_file.get(
                "size",
                0
            )
            or 0
        )

        base, ext = os.path.splitext(
            name
        )

        normalized = (
            base.lower()
            .replace("_", " ")
            .replace("-", " ")
            .replace("(", " ")
            .replace(")", " ")
            .replace("[", " ")
            .replace("]", " ")
            .replace("{", " ")
            .replace("}", " ")
            .replace(",", " ")
        )

        normalized = " ".join(
            normalized.split()
        )

        key = (
            normalized,
            ext.lower(),
            size,
        )

        groups.setdefault(
            key,
            []
        ).append(
            drive_file
        )

    duplicate_groups = [
        group
        for group in groups.values()
        if len(group) > 1
    ]

    duplicate_count = sum(
        len(group) - 1
        for group in duplicate_groups
    )

    print(
        f"🔁 Duplicate groups: "
        f"{len(duplicate_groups)}"
    )

    print(
        f"🗑️ Duplicate files to trash: "
        f"{duplicate_count}"
    )

    trashed = 0

    for group in duplicate_groups:

        # Keep the first file.
        keep = group[0]

        print(
            "\n✅ KEEP:",
            keep.get("name"),
            "|",
            keep.get("id"),
        )

        # Move all remaining copies to Trash.
        for duplicate in group[1:]:

            file_id = duplicate.get(
                "id"
            )

            try:

                service.files().update(
                    fileId=file_id,
                    body={
                        "trashed": True
                    },
                ).execute()

                trashed += 1

                print(
                    "🗑️ TRASHED:",
                    duplicate.get("name"),
                    "|",
                    file_id,
                )

            except Exception as error:

                print(
                    "❌ FAILED TO TRASH:",
                    duplicate.get("name"),
                    "|",
                    repr(error),
                )

    print(
        "\n============================================================"
    )

    print(
        f"🎵 Original music files: {len(files)}"
    )

    print(
        f"🔁 Duplicate groups: "
        f"{len(duplicate_groups)}"
    )

    print(
        f"🗑️ Successfully trashed: "
        f"{trashed}"
    )

    print(
        f"✅ Music files remaining: "
        f"{len(files) - trashed}"
    )

    print(
        "============================================================"
    )
  # ============================================================
# GOOGLE DRIVE UPLOAD
# ============================================================

def upload_sync(
    path,
    filename,
    mime_type,
    telegram_file_id,
    folder_id,
    job,
):

    service = (
        get_drive_service()
    )

    metadata = {
        "name": filename,

        "parents": [
            folder_id
        ],

        "appProperties": {
            "telegram_file_id": str(
                telegram_file_id
                or ""
            )
        },
    }

    media = MediaFileUpload(
        path,
        mimetype=mime_type,
        chunksize=DRIVE_CHUNK_SIZE,
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

    last_done = 0
    last_time = (
        time.monotonic()
    )

    while response is None:

        if job.cancel.is_set():
            raise TransferCancelled()

        status, response = (
            request.next_chunk()
        )

        now = time.monotonic()

        if status:

            done = int(
                status.resumable_progress
            )

            elapsed = (
                now - last_time
            )

            speed = (
                (done - last_done)
                / elapsed
                if elapsed > 0
                else 0
            )

            job.progress.update(
                stage="upload",
                done=done,
                total=job.size,
                speed=speed,
            )

            last_done = done
            last_time = now

    return response


# ============================================================
# TELEGRAM FILE INFORMATION
# ============================================================

def get_file_info(
    message,
):

    if not message:
        return None

    # ========================================================
    # PYTHON-TELEGRAM-BOT MESSAGE
    # ========================================================

    # --------------------------------------------------------
    # DOCUMENT
    # --------------------------------------------------------

    if getattr(message, "document", None):

        document = message.document

        filename = (
            getattr(
                document,
                "file_name",
                None,
            )
            or f"file_{message.id}"
        )

        mime_type = (
            getattr(
                document,
                "mime_type",
                None,
            )
            or "application/octet-stream"
        )

        size = int(
            getattr(
                document,
                "file_size",
                0,
            )
            or 0
        )

        file_id = (
            getattr(
                document,
                "file_unique_id",
                None,
            )
            or getattr(
                document,
                "file_id",
                None,
            )
        )

        return (
            filename,
            mime_type,
            size,
            str(file_id or ""),
        )

    # --------------------------------------------------------
    # AUDIO
    # --------------------------------------------------------

    if getattr(message, "audio", None):

        audio = message.audio

        filename = (
            getattr(
                audio,
                "file_name",
                None,
            )
            or f"audio_{message.id}.mp3"
        )

        mime_type = (
            getattr(
                audio,
                "mime_type",
                None,
            )
            or "audio/mpeg"
        )

        size = int(
            getattr(
                audio,
                "file_size",
                0,
            )
            or 0
        )

        file_id = (
            getattr(
                audio,
                "file_unique_id",
                None,
            )
            or getattr(
                audio,
                "file_id",
                None,
            )
        )

        return (
            filename,
            mime_type,
            size,
            str(file_id or ""),
        )

    # --------------------------------------------------------
    # VIDEO
    # --------------------------------------------------------

    if getattr(message, "video", None):

        video = message.video

        filename = (
            getattr(
                video,
                "file_name",
                None,
            )
            or f"video_{message.id}.mp4"
        )

        mime_type = (
            getattr(
                video,
                "mime_type",
                None,
            )
            or "video/mp4"
        )

        size = int(
            getattr(
                video,
                "file_size",
                0,
            )
            or 0
        )

        file_id = (
            getattr(
                video,
                "file_unique_id",
                None,
            )
            or getattr(
                video,
                "file_id",
                None,
            )
        )

        return (
            filename,
            mime_type,
            size,
            str(file_id or ""),
        )

    # ========================================================
    # PHOTO
    # ========================================================

    if getattr(message, "photo", None):

        photo = message.photo

        # ----------------------------------------------------
        # Bot API:
        # message.photo is a tuple of PhotoSize objects.
        # ----------------------------------------------------

        if isinstance(
            photo,
            (tuple, list),
        ):

            if not photo:
                return None

            photo = photo[-1]

            size = int(
                getattr(
                    photo,
                    "file_size",
                    0,
                )
                or 0
            )

            file_id = (
                getattr(
                    photo,
                    "file_unique_id",
                    None,
                )
                or getattr(
                    photo,
                    "file_id",
                    None,
                )
            )

            return (
                f"photo_{message.id}.jpg",
                "image/jpeg",
                size,
                str(file_id or ""),
            )

        # ----------------------------------------------------
        # Telethon:
        # message.photo is a Telethon Photo object.
        # ----------------------------------------------------

        photo_id = getattr(
            photo,
            "id",
            None,
        )

        size = 0

        sizes = getattr(
            photo,
            "sizes",
            None,
        )

        if sizes:

            for item in sizes:

                item_size = getattr(
                    item,
                    "size",
                    None,
                )

                if item_size:

                    size = max(
                        size,
                        int(item_size),
                    )

        return (
            f"photo_{message.id}.jpg",
            "image/jpeg",
            size,
            str(photo_id or ""),
        )

    return None

# ============================================================
# PERMISSION
# ============================================================

def is_allowed(
    update,
):

    return bool(
        update.effective_user
        and
        update.effective_user.id
        in ALLOWED_TELEGRAM_IDS
    )


# ============================================================
# LIVE STATUS UPDATER
# ============================================================

async def update_status_loop(
    bot,
    job,
):

    last_text = None

    while not job.cancel.is_set():

        stage = (
            job.progress[
                "stage"
            ]
        )

        if stage:

            (
                text,
                markup,
            ) = build_status(
                stage,
                job.filename,
                job.progress[
                    "done"
                ],
                job.progress[
                    "total"
                ],
                job.progress[
                    "speed"
                ],
                job.user_id,
                job.message_id,
            )

            if text != last_text:

                try:

                    if job.status_message:

                        await (
                            bot
                            .edit_message_text(
                                chat_id=(
                                    job.user_id
                                ),
                                message_id=(
                                    job.status_message
                                    .message_id
                                ),
                                text=text,
                                reply_markup=markup,
                            )
                        )

                    last_text = text

                except Exception:
                    pass

        try:

            await asyncio.sleep(
                STATUS_UPDATE_SECONDS
            )

        except asyncio.CancelledError:

            return


# ============================================================
# MESSAGE COMPLETION TRACKING
# ============================================================

def mark_message_complete(
    state,
    user_id,
    message_id,
):

    uid = str(
        user_id
    )

    completed = (
        state.state.setdefault(
            "completed_message_ids",
            {},
        )
    )

    ids = set(
        completed.get(
            uid,
            [],
        )
    )

    ids.add(
        int(message_id)
    )

    last = int(
        state.state[
            "last_message_ids"
        ].get(
            uid,
            0,
        )
    )

    # Advance only through consecutive
    # completed message IDs.
    while (
        last + 1
        in ids
    ):

        last += 1

        ids.remove(
            last
        )

    state.state[
        "last_message_ids"
    ][uid] = last

    completed[uid] = sorted(
        ids
    )[-2000:]



# PROCESS ONE FILE
# ============================================================

async def process_message(
    app,
    state,
    client,
    message,
    job,
):

    bot = app.bot

    user_id = int(
        job.user_id
    )

    # --------------------------------------------------------
    # IMPORTANT:
    # Use the information captured from the BOT API message.
    #
    # Do NOT call get_file_info() on the Telethon photo here.
    # Telegram photo sizes are different from the actual
    # downloaded photo size.
    # --------------------------------------------------------

    filename = job.filename

    mime_type = getattr(
        job,
        "mime_type",
        None,
    ) or "application/octet-stream"

    expected_size = int(
        getattr(
            job,
            "expected_size",
            0,
        )
        or 0
    )

    telegram_file_id = (
        getattr(
            job,
            "telegram_file_id",
            None,
        )
        or ""
    )

    state.inc(
        "sent"
    )

    await state.save()

    # --------------------------------------------------------
    # Determine destination folder
    # --------------------------------------------------------

    folder_type = (
        classify_file(
            filename,
            mime_type,
        )
    )

    folders = (
        app.bot_data[
            "folder_ids"
        ]
    )

    target_folder = (
        folders[
            folder_type
        ]
    )

    # --------------------------------------------------------
    # Duplicate check
    # --------------------------------------------------------

    duplicate = (
        await asyncio.to_thread(
            find_duplicate_sync,
            target_folder,
            telegram_file_id,
            filename,
            expected_size,
        )
    )

    if duplicate:

        state.inc(
            "duplicates"
        )

        mark_message_complete(
            state,
            user_id,
            message.id,
        )

        await state.save()

        await bot.send_message(
            user_id,

            (
                "⏭️ Duplicate skipped!\n\n"
                f"📄 {filename}\n\n"
                f"Already exists in "
                f"{SUBFOLDERS[folder_type]}."
            ),
        )

        return "done"

    # --------------------------------------------------------
    # Status message
    # --------------------------------------------------------

    status = await bot.send_message(
        user_id,

        (
            "⏳ Preparing...\n\n"
            f"📄 {filename}"
        ),

        reply_to_message_id=(
            message.id
        ),

        allow_sending_without_reply=True,
    )

    job.status_message = (
        status
    )

    updater = (
        asyncio.create_task(
            update_status_loop(
                bot,
                job,
            )
        )
    )

    temp_path = None

    try:

        # ====================================================
        # TEMPORARY FILE
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

            temp_path = (
                temp_file.name
            )

        # ====================================================
        # TELEGRAM DOWNLOAD
        # ====================================================

        start_time = (
            time.monotonic()
        )

        last_time = start_time
        last_done = 0

        def download_progress(
            done,
            total,
        ):

            nonlocal last_time
            nonlocal last_done

            if job.cancel.is_set():

                raise TransferCancelled()

            now = (
                time.monotonic()
            )

            elapsed = (
                now - last_time
            )

            speed = (
                (done - last_done)
                / elapsed
                if elapsed > 0
                else 0
            )

            job.progress.update(
                stage="download",
                done=int(done),
                total=int(
                    total
                    or expected_size
                    or 0
                ),
                speed=speed,
            )

            last_time = now
            last_done = int(
                done
            )

        await client.download_media(
            message,
            file=temp_path,
            progress_callback=(
                download_progress
            ),
        )

        # ====================================================
        # VERIFY DOWNLOAD
        # ====================================================

        if not os.path.exists(
            temp_path
        ):

            raise RuntimeError(
                "Telegram download did not "
                "create a file."
            )

        actual_size = (
            os.path.getsize(
                temp_path
            )
        )

        # ----------------------------------------------------
        # IMPORTANT:
        #
        # Do NOT reject Telegram photos because their Bot API
        # size does not equal Telethon's downloaded size.
        #
        # For normal files we still verify the size.
        # ----------------------------------------------------

        is_photo = (
            mime_type == "image/jpeg"
            and filename.startswith(
                "photo_"
            )
        )

        if (
            not is_photo
            and expected_size
            and actual_size
            != expected_size
        ):

            raise RuntimeError(
                "Downloaded file size "
                "does not match Telegram."
            )

        state.inc(
            "downloaded"
        )

        state.inc(
            "downloaded_bytes",
            actual_size,
        )

        await state.save()

        # ====================================================
        # GOOGLE DRIVE UPLOAD
        # ====================================================

        job.progress.update(
            stage="upload",
            done=0,
            total=actual_size,
            speed=0.0,
        )

        result = (
            await asyncio.to_thread(
                upload_sync,
                temp_path,
                filename,
                mime_type,
                telegram_file_id,
                target_folder,
                job,
            )
        )

        uploaded_size = int(
            result.get(
                "size"
            )
            or actual_size
        )

        # ====================================================
        # SUCCESS
        # ====================================================

        state.inc(
            "uploaded"
        )

        state.inc(
            "uploaded_bytes",
            uploaded_size,
        )

        mark_message_complete(
            state,
            user_id,
            message.id,
        )

        await state.save()

        await bot.edit_message_text(
            chat_id=user_id,
            message_id=(
                status.message_id
            ),

            text=(
                "✅ Uploaded successfully!\n\n"
                f"📄 {filename}\n"
                f"📦 {human_bytes(uploaded_size)}"
            ),
        )

        return "done"

    # ========================================================
    # USER CANCELLED
    # ========================================================

    except TransferCancelled:

        state.inc(
            "cancelled"
        )

        mark_message_complete(
            state,
            user_id,
            message.id,
        )

        await state.save()

        try:

            await bot.edit_message_text(
                chat_id=user_id,
                message_id=(
                    status.message_id
                ),

                text=(
                    "🛑 Cancelled\n\n"
                    f"📄 {filename}"
                ),
            )

        except Exception:
            pass

        return "cancelled"

    # ========================================================
    # TELEGRAM FLOOD WAIT
    # ========================================================

    except FloodWaitError as error:

        raise RuntimeError(
            "Telegram FloodWait: "
            f"{error.seconds}s"
        )

    # ========================================================
    # OTHER ERROR
    # ========================================================

    except Exception as error:

        state.inc(
            "failed"
        )

        await state.save()

        print(
            "❌ process_message error:",
            repr(error),
        )

        try:

            await bot.edit_message_text(
                chat_id=user_id,
                message_id=(
                    status.message_id
                ),

                text=(
                    "❌ Failed\n\n"
                    f"📄 {filename}\n\n"
                    f"{error}"
                ),
            )

        except Exception:
            pass

        return "failed"

    # ========================================================
    # CLEANUP
    # ========================================================

    finally:

        updater.cancel()

        if (
            temp_path
            and os.path.exists(
                temp_path
            )
        ):

            try:

                os.remove(
                    temp_path
                )

            except OSError:
                pass


# ============================================================
# /STATS
# ============================================================

async def stats(
    update,
    context,
):

    if not is_allowed(
        update
    ):
        return

    state = (
        context.application
        .bot_data[
            "state_manager"
        ]
        .state
    )

    await update.message.reply_text(

        "📊 BOT STATISTICS\n\n"

        f"📨 Files sent: "
        f"{state.get('sent', 0)}\n"

        f"⬇️ Downloaded: "
        f"{state.get('downloaded', 0)}\n"

        f"☁️ Uploaded: "
        f"{state.get('uploaded', 0)}\n"

        f"⏭️ Duplicates: "
        f"{state.get('duplicates', 0)}\n"

        f"❌ Failed: "
        f"{state.get('failed', 0)}\n"

        f"🛑 Cancelled: "
        f"{state.get('cancelled', 0)}\n\n"

        f"📦 Downloaded: "
        f"{human_bytes(state.get('downloaded_bytes', 0))}\n"

        f"☁️ Uploaded: "
        f"{human_bytes(state.get('uploaded_bytes', 0))}"
    )
#timerooooooooooo#
#timerooooooooooo#

async def timer_command(
    update,
    context,
):

    if not is_allowed(
        update
    ):
        return

    elapsed = int(
        time.monotonic()
        - WORKFLOW_STARTED_AT
    )

    hours, remainder = divmod(
        elapsed,
        3600
    )

    minutes, seconds = divmod(
        remainder,
        60
    )

    await update.message.reply_text(
        "⏱️ WORKFLOW TIMER\n\n"
        "🟢 Running\n"
        f"⏳ Uptime: "
        f"{hours}h {minutes}m {seconds}s"
    )
    #quea#

async def queue_command(
    update,
    context,
):

    if not is_allowed(
        update
    ):
        return

    jobs = (
        context.application
        .bot_data["jobs"]
    )

    waiting = sum(
        1
        for job in jobs.values()
        if getattr(
            job,
            "queued",
            False,
        )
    )

    processing = (
        len(jobs)
        - waiting
    )

    state = (
        context.application
        .bot_data[
            "state_manager"
        ]
        .state
    )

    await update.message.reply_text(
        "📦 QUEUE STATUS\n\n"
        f"⏳ Waiting: {waiting}\n"
        f"⚙️ Processing: {processing}\n"
        f"📦 Total active: {len(jobs)}\n\n"
        f"📨 Files sent: "
        f"{state.get('sent', 0)}\n"
        f"☁️ Uploaded: "
        f"{state.get('uploaded', 0)}\n"
        f"⏭️ Duplicates: "
        f"{state.get('duplicates', 0)}\n"
        f"❌ Failed: "
        f"{state.get('failed', 0)}"
    )
# ============================================================
# /STATUS
# ============================================================

async def status_command(
    update,
    context,
):

    if not is_allowed(
        update
    ):
        return

    jobs = (
        context.application
        .bot_data[
            "jobs"
        ]
    )

    my_jobs = [
        job
        for (
            user_id,
            _,
        ), job in jobs.items()
        if (
            user_id
            == update.effective_user.id
        )
    ]

    if not my_jobs:

        await update.message.reply_text(
            "🟢 Idle — no file is "
            "currently being processed "
            "for you."
        )

        return

    lines = [
        "🟢 CURRENT STATUS\n"
    ]

    for job in my_jobs:

        progress = (
            job.progress
        )

        percentage = (
            int(
                progress["done"]
                * 100
                / progress["total"]
            )
            if progress["total"]
            else 0
        )

        icon = (
            "⬇️"
            if progress["stage"]
            == "download"
            else "⬆️"
        )

        lines.append(
    (
        f"{icon} "
        f"{job.filename}\n"
        f"{progress_bar(progress['done'], progress['total'])} "
        f"{percentage}%"
    )
)

    await update.message.reply_text(
        "\n\n".join(
            lines
        )
    )


# ============================================================
# /FOLDERS
# ============================================================

async def folders_command(
    update,
    context,
):

    if not is_allowed(
        update
    ):
        return

    service = (
        await asyncio.to_thread(
            get_drive_service
        )
    )

    folders = (
        context.application
        .bot_data[
            "folder_ids"
        ]
    )

    lines = [
        "📁 DRIVE FOLDERS"
    ]

    for (
        kind,
        name,
    ) in SUBFOLDERS.items():

        query = (
            f"'{folders[kind]}' "
            f"in parents "
            f"and trashed = false"
        )

        files = (
            await asyncio.to_thread(
                list_all_files_sync,
                service,
                query,
                "files(id)",
            )
        )

        icon = (
            "🎬"
            if kind == "video"
            else
            "🎵"
            if kind == "music"
            else
            "🖼️"
            if kind == "image"
            else
            "📦"
        )

        lines.append(
            f"{icon} {name}: "
            f"{len(files)}"
        )

    await update.message.reply_text(
        "\n".join(
            lines
        )
    )


# ============================================================
# /HELP
# ============================================================

async def help_command(
    update,
    context,
):

    if not is_allowed(
        update
    ):
        return

    await update.message.reply_text(

        "🤖 Commands\n\n"

        "/stats — totals\n"

        "/status — current transfers\n"

        "/folders — Drive folder counts\n"
        
        "/queue — queue status\n"
        
        "/timer — workflow uptime\n"

        "/cancel — cancel your current transfer\n"

        "/help — commands"
    )


# ============================================================
# /CANCEL
# ============================================================

async def cancel_command(
    update,
    context,
):

    if not is_allowed(
        update
    ):
        return

    user_id = (
        update.effective_user.id
    )

    jobs = (
        context.application
        .bot_data[
            "jobs"
        ]
    )

    my_jobs = [
        job
        for (
            uid,
            _,
        ), job in jobs.items()
        if uid == user_id
    ]

    if not my_jobs:

        await update.message.reply_text(
            "Nothing is currently "
            "downloading or uploading."
        )

        return

    for job in my_jobs:

        job.cancel.set()

    await update.message.reply_text(
        "🛑 Cancel requested.\n\n"
        "The active transfer will "
        "stop after the current "
        "transfer chunk."
    )


# ============================================================
# CANCEL BUTTON
# ============================================================

async def cancel_button(
    update,
    context,
):

    query = (
        update.callback_query
    )

    if (
        query.from_user.id
        not in ALLOWED_TELEGRAM_IDS
    ):

        await query.answer()

        return

    try:

        (
            _,
            user_id_text,
            message_id_text,
        ) = query.data.split(
            ":",
            2,
        )

        user_id = int(
            user_id_text
        )

        message_id = int(
            message_id_text
        )

    except Exception:

        await query.answer(
            "Invalid cancel button.",
            show_alert=True,
        )

        return

    if (
        user_id
        != query.from_user.id
    ):

        await query.answer(
            "Not allowed.",
            show_alert=True,
        )

        return

    job = (
        context.application
        .bot_data[
            "jobs"
        ].get(
            (
                user_id,
                message_id,
            )
        )
    )

    if job:

        job.cancel.set()

        await query.answer(
            "Cancel requested.",
            show_alert=False,
        )

    else:

        await query.answer(
            "This transfer is "
            "already finished.",
            show_alert=False,
        )


# ============================================================
# BOT API FILE RECEIVED
# ============================================================
# ============================================================
# BOT API FILE RECEIVED
# ============================================================

async def file_received(
    update,
    context,
):

    if not is_allowed(update):
        return

    message = update.message

    if not message:
        return

    file_info = get_file_info(message)

    if not file_info:
        return

    (
        filename,
        mime_type,
        size,
        file_id,
    ) = file_info

    app = context.application

    state = app.bot_data["state_manager"]

    jobs = app.bot_data["jobs"]

    user_id = int(
        update.effective_user.id
    )

    message_id = int(
        message.message_id
    )

    key = (
        user_id,
        message_id,
    )

    if key in jobs:
        return

    client = app.bot_data["telethon"]

    print(
        "========================================"
    )

    print(
        "📥 BOT API FILE RECEIVED"
    )

    print(
        "Filename:",
        filename,
    )

    print(
        "Size:",
        size,
    )

    print(
        "Bot API message ID:",
        message_id,
    )

    print(
        "User ID:",
        user_id,
    )

    print(
        "========================================"
    )

    # ========================================================
    # FIND THE TELETHON MESSAGE
    #
    # Do NOT depend on Bot API message_id == Telethon message.id
    # ========================================================

    telethon_message = None

    try:

        print(
            "Searching Telegram USER session..."
        )

        # ----------------------------------------------------
        # Search recent messages in the bot chat.
        #
        # We search by the user's sender ID and then compare
        # the actual file information.
        # ----------------------------------------------------

        async for candidate in client.iter_messages(
            BOT_USERNAME,
            limit=3000,
        ):

            try:

                candidate_sender = int(
                    candidate.sender_id or 0
                )

            except Exception:

                candidate_sender = 0

            if candidate_sender != user_id:
                continue

            # =================================================
            # DOCUMENT / AUDIO / VIDEO
            # =================================================

            candidate_file = getattr(
                candidate,
                "file",
                None,
            )

            if candidate_file:

                candidate_name = (
                    getattr(
                        candidate_file,
                        "name",
                        None,
                    )
                    or ""
                )

                candidate_size = int(
                    getattr(
                        candidate_file,
                        "size",
                        0,
                    )
                    or 0
                )

                # ------------------------------------------------
                # Best match: filename + size
                # ------------------------------------------------

                if (
                    candidate_name
                    and candidate_name == filename
                    and (
                        not size
                        or candidate_size == int(size)
                    )
                ):

                    telethon_message = candidate

                    print(
                        "✅ Telethon file found:"
                    )

                    print(
                        "Telethon message ID:",
                        candidate.id,
                    )

                    print(
                        "Filename:",
                        candidate_name,
                    )

                    print(
                        "Size:",
                        candidate_size,
                    )

                    break

                # ------------------------------------------------
                # Fallback: same filename
                # ------------------------------------------------

                if (
                    candidate_name
                    and candidate_name == filename
                ):

                    telethon_message = candidate

                    print(
                        "✅ Telethon file found by filename:"
                    )

                    print(
                        "Telethon message ID:",
                        candidate.id,
                    )

                    break

            # =================================================
            # PHOTO
            # =================================================

            candidate_photo = getattr(
                candidate,
                "photo",
                None,
            )

            if candidate_photo:

                # ------------------------------------------------
                # A Telegram photo does not have the same
                # filename as the Bot API generated filename.
                #
                # Therefore match by:
                #
                # 1. sender
                # 2. recent message
                # 3. photo presence
                #
                # We only use this fallback for photo messages.
                # ------------------------------------------------

                if getattr(
                    message,
                    "photo",
                    None,
                ):

                    telethon_message = candidate

                    print(
                        "✅ Telethon photo found:"
                    )

                    print(
                        "Telethon message ID:",
                        candidate.id,
                    )

                    break

    except Exception as error:

        print(
            "❌ Telethon search failed:",
            repr(error),
        )

    # ========================================================
    # STILL NOT FOUND
    # ========================================================

    if not telethon_message:

        print(
            "❌ Telethon message could not be found."
        )

        print(
            "Bot API message ID:",
            message_id,
        )

        print(
            "Filename:",
            filename,
        )

        await app.bot.send_message(
            user_id,
            (
                "❌ Could not access the Telegram file.\n\n"
                f"📄 {filename}\n\n"
                "Please send the file again."
            ),
        )

        return

    # ========================================================
    # SAFETY CHECK
    # ========================================================

    sender_id = int(
        telethon_message.sender_id
        or 0
    )

    if sender_id != user_id:

        print(
            "❌ Sender mismatch:"
        )

        print(
            "Telethon sender:",
            sender_id,
        )

        print(
            "Expected:",
            user_id,
        )

        return

    # ========================================================
    # CREATE JOB
    # ========================================================

    job = Job(
        user_id,
        message_id,
        filename,
        size,
    )

    job.filename = filename
    job.mime_type = mime_type
    job.expected_size = size
    job.telegram_file_id = file_id
    job.queued = True

    jobs[key] = job

    print(
        "========================================"
    )

    print(
        "🚀 STARTING FILE PROCESSING"
    )

    print(
        "Filename:",
        filename,
    )

    print(
        "Bot API message:",
        message_id,
    )

    print(
        "Telethon message:",
        telethon_message.id,
    )

    print(
        "Size:",
        size,
    )

    print(
        "========================================"
    )

    # ========================================================
    # SELECT CONCURRENCY
    # ========================================================

    if size > LARGE_FILE_BYTES:

        semaphore = (
            app.bot_data["large_sem"]
        )

    else:

        semaphore = (
            app.bot_data["small_sem"]
        )

    # ========================================================
    # PROCESS FILE
    # ========================================================

    async def run_one():

        try:
            job.queued = False

            async with semaphore:

                result = await process_message(
                    app,
                    state,
                    client,
                    telethon_message,
                    job,
                )

            if result == "failed":

                print(
                    "❌ File processing failed:",
                    filename,
                )

            else:

                print(
                    "✅ File processing completed:",
                    filename,
                )

        except TransferCancelled:

            print(
                "🛑 Transfer cancelled:",
                filename,
            )

        except Exception as error:

            print(
                "❌ File worker error:",
                repr(error),
            )

            try:

                await app.bot.send_message(
                    user_id,
                    (
                        "❌ File processing failed.\n\n"
                        f"📄 {filename}\n\n"
                        f"{error}"
                    ),
                )

            except Exception as send_error:

                print(
                    "Could not send error message:",
                    repr(send_error),
                )

        finally:

            jobs.pop(
                key,
                None,
            )

    asyncio.create_task(
        run_one()
    )


# ============================================================
# FIRST-RUN INITIALIZATION
#

# ============================================================
# APPLICATION STARTUP
# ============================================================

async def post_init(app):

    print("========================================")
    print("Telegram Drive Bot: STARTING")
    print("========================================")

    state = StateManager()
    await state.load()

    print("State loaded.")

    # --------------------------------------------------------
    # Connect Telethon USER session
    # --------------------------------------------------------

    print("Connecting Telethon user session...")

    client = TelegramClient(
        TELEGRAM_SESSION,
        TELEGRAM_API_ID,
        TELEGRAM_API_HASH,
        connection_retries=3,
        retry_delay=2,
        timeout=15,
    )

    try:
        await asyncio.wait_for(
            client.connect(),
            timeout=30,
        )
    except Exception as error:
        print(
            "❌ Telethon connection failed:",
            repr(error),
        )

        try:
            await client.disconnect()
        except Exception:
            pass

        raise

    print("✅ Telethon connected.")

    if not await client.is_user_authorized():

        await client.disconnect()

        raise RuntimeError(
            "❌ Telegram USER session is not authorized."
        )

    me = await client.get_me()

    print(
        "Telethon account:",
        me.id,
        getattr(me, "username", None),
    )

    # --------------------------------------------------------
    # Get Drive folders
    #
    # IMPORTANT:
    # This happens AFTER Telegram/Telethon is connected.
    # --------------------------------------------------------

    print("Loading Google Drive folders...")

    folder_ids = await asyncio.to_thread(
        get_folder_ids_sync
    )

    print("✅ Google Drive folders ready.")

    # --------------------------------------------------------
    # Application state
    # --------------------------------------------------------

    app.bot_data.update({

        "state_manager": state,

        "telethon": client,

        "folder_ids": folder_ids,

        "jobs": {},

        "large_sem": asyncio.Semaphore(1),

        "small_sem": asyncio.Semaphore(
            SMALL_CONCURRENCY
        ),
    })

    # --------------------------------------------------------
    # Telegram command menu
    # --------------------------------------------------------

    await app.bot.set_my_commands([

        BotCommand(
            "start",
            "Start bot",
        ),

        BotCommand(
            "stats",
            "Show statistics",
        ),
        BotCommand(
            "queue",
            "Queue status",
       ),

       BotCommand(
          "timer",
          "Workflow uptime",
        ),  

        BotCommand(
            "status",
            "Current transfers",
        ),

        BotCommand(
            "folders",
            "Drive folders",
        ),

        BotCommand(
            "cancel",
            "Cancel current transfer",
        ),

        BotCommand(
            "help",
            "Help",
        ),
    ])

    print("========================================")
    print("✅ BOT STARTED SUCCESSFULLY")
    print("========================================")

# ============================================================
# APPLICATION SHUTDOWN
# ============================================================

async def post_shutdown(
    app,
):

    

    client = (
        app.bot_data.get(
            "telethon"
        )
    )

    if client:

        await client.disconnect()


# ============================================================
# /START
# ============================================================

async def start(
    update,
    context,
):

    if not is_allowed(
        update
    ):
        return

    await update.message.reply_text(

        "🤖 Telegram → Google Drive\n\n"

        "📁 Video / Music / Image / Other\n"

        "🔍 Duplicate protection ON\n"

        "📦 Large-file support ON\n"

        "⚡ Small files: up to 4 at once\n"

        "🐢 Large files: 1 at a time\n\n"

        "Use /help for commands."
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
            "timer",
            timer_command,
        )
    )

    app.add_handler(
        CommandHandler(
            "queue",
            queue_command,
        )
    )
    
    app.add_handler(
        CommandHandler(
            "status",
            status_command,
        )
    )

    app.add_handler(
        CommandHandler(
            "folders",
            folders_command,
        )
    )

    app.add_handler(
        CommandHandler(
            "cancel",
            cancel_command,
        )
    )

    app.add_handler(
        CommandHandler(
            "help",
            help_command,
        )
    )

    app.add_handler(
        CallbackQueryHandler(
            cancel_button,
            pattern=r"^cancel:\d+:\d+$",
        )
    )

    app.add_handler(
        MessageHandler(
            filters.ALL,
            file_received,
        )
    )

    print(
        "Starting Telegram Drive Bot..."
    )

    app.run_polling(
        drop_pending_updates=False
    )


if __name__ == "__main__":
    main() 
