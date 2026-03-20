"""YouTube upload with multi-account support, channel selection, and full metadata."""

import json
import os
import sys
from pathlib import Path
from datetime import datetime, timedelta

# In PyInstaller frozen builds, __file__ points to temp _MEIPASS dir.
# Secrets and tokens must live next to the .exe so they persist.
if getattr(sys, 'frozen', False):
    _BASE = Path(sys.executable).parent
else:
    _BASE = Path(__file__).parent
_SECRETS = _BASE / "client_secrets.json"
_TOKENS_DIR = _BASE / "tokens"
_TOKEN_LEGACY = _BASE / "token.json"  # old single-token path
_SCOPES = [
    "https://www.googleapis.com/auth/youtube.upload",
    "https://www.googleapis.com/auth/youtube.readonly",
]

# Cache: account_id -> youtube service
_service_cache: dict = {}


# ── Authentication ───────────────────────────────────────────────────────────


def _ensure_tokens_dir():
    _TOKENS_DIR.mkdir(exist_ok=True)
    # Migrate legacy single token.json → tokens/ folder
    if _TOKEN_LEGACY.exists():
        try:
            from google.oauth2.credentials import Credentials
            creds = Credentials.from_authorized_user_file(str(_TOKEN_LEGACY), _SCOPES)
            if creds and creds.valid:
                svc = _build_service(creds)
                resp = svc.channels().list(part="snippet", mine=True).execute()
                items = resp.get("items", [])
                if items:
                    acct_id = items[0]["id"]
                    acct_title = items[0]["snippet"]["title"]
                    _save_token(acct_id, acct_title, creds)
            _TOKEN_LEGACY.unlink()
        except Exception:
            # Migration failed — just remove the old file
            try:
                _TOKEN_LEGACY.unlink()
            except Exception:
                pass


def _build_service(creds):
    from googleapiclient.discovery import build
    return build("youtube", "v3", credentials=creds)


def _token_path(account_id: str) -> Path:
    return _TOKENS_DIR / f"{account_id}.json"


def _save_token(account_id: str, account_title: str, creds):
    """Save token with account metadata."""
    data = json.loads(creds.to_json())
    data["_account_id"] = account_id
    data["_account_title"] = account_title
    _token_path(account_id).write_text(json.dumps(data, indent=2))


def _load_creds(account_id: str):
    """Load credentials for a specific account, refreshing if expired."""
    from google.oauth2.credentials import Credentials
    from google.auth.transport.requests import Request
    path = _token_path(account_id)
    if not path.exists():
        return None
    creds = Credentials.from_authorized_user_file(str(path), _SCOPES)
    if not creds:
        return None
    if creds.valid:
        return creds
    # Token expired — try to refresh
    if creds.expired and creds.refresh_token:
        try:
            creds.refresh(Request())
            # Save the refreshed token
            data = json.loads(path.read_text())
            title = data.get("_account_title", account_id)
            _save_token(account_id, title, creds)
            print(f"[+] Refreshed token for {title}")
            return creds
        except Exception as e:
            print(f"[!] Token refresh failed for {account_id}: {e}")
    return None


def get_youtube_service(account_id: str = None, force_new: bool = False):
    """Get YouTube service for a specific account. If account_id is None, use first available."""
    _ensure_tokens_dir()

    if account_id is None:
        accounts = list_accounts()
        if not accounts:
            raise RuntimeError("No YouTube accounts connected")
        account_id = accounts[0]["id"]

    if account_id in _service_cache and not force_new:
        return _service_cache[account_id]

    creds = _load_creds(account_id)
    if not creds:
        raise RuntimeError(f"Account {account_id} not connected or token expired")

    svc = _build_service(creds)
    _service_cache[account_id] = svc
    return svc


def add_account() -> dict:
    """Run OAuth flow to add a new account. Returns {id, title} of the added account."""
    _ensure_tokens_dir()

    if not _SECRETS.exists():
        raise FileNotFoundError(
            "client_secrets.json not found.\n"
            "1. https://console.cloud.google.com → create project\n"
            "2. Enable YouTube Data API v3\n"
            "3. Create OAuth 2.0 credentials (Desktop app)\n"
            "4. Download JSON → save as client_secrets.json"
        )

    from google_auth_oauthlib.flow import InstalledAppFlow
    flow = InstalledAppFlow.from_client_secrets_file(str(_SECRETS), _SCOPES)
    creds = flow.run_local_server(port=0)

    # Discover which account this is
    svc = _build_service(creds)
    resp = svc.channels().list(part="snippet,statistics", mine=True).execute()
    items = resp.get("items", [])
    if not items:
        raise RuntimeError("No YouTube channel found for this Google account")

    ch = items[0]
    account_id = ch["id"]
    account_title = ch["snippet"]["title"]

    _save_token(account_id, account_title, creds)
    _service_cache[account_id] = svc

    print(f"[+] Added YouTube account: {account_title} ({account_id})")
    return {"id": account_id, "title": account_title}


def list_accounts() -> list[dict]:
    """Return all connected accounts (from tokens/ folder)."""
    _ensure_tokens_dir()
    accounts = []
    for f in sorted(_TOKENS_DIR.glob("*.json")):
        try:
            data = json.loads(f.read_text())
            accounts.append({
                "id": data.get("_account_id", f.stem),
                "title": data.get("_account_title", f.stem),
            })
        except Exception:
            continue
    return accounts


def is_connected() -> bool:
    """Check if at least one account is connected."""
    _ensure_tokens_dir()
    return len(list_accounts()) > 0


def disconnect(account_id: str = None):
    """Remove a specific account, or all accounts if account_id is None."""
    _ensure_tokens_dir()
    if account_id:
        path = _token_path(account_id)
        if path.exists():
            path.unlink()
        _service_cache.pop(account_id, None)
    else:
        # Remove all
        for f in _TOKENS_DIR.glob("*.json"):
            f.unlink()
        _service_cache.clear()


# ── Channel & Category listing ───────────────────────────────────────────────


def list_channels() -> list[dict]:
    """Return channels across ALL connected accounts, including Brand Accounts."""
    _ensure_tokens_dir()
    all_channels = []
    seen_ids = set()
    for acct in list_accounts():
        try:
            yt = get_youtube_service(acct["id"])
            # Primary channel (mine=True)
            resp = yt.channels().list(part="snippet,statistics", mine=True).execute()
            for ch in resp.get("items", []):
                if ch["id"] not in seen_ids:
                    seen_ids.add(ch["id"])
                    all_channels.append({
                        "id": ch["id"],
                        "title": ch["snippet"]["title"],
                        "thumbnail": ch["snippet"]["thumbnails"]["default"]["url"],
                        "subscribers": ch["statistics"].get("subscriberCount", "0"),
                        "account_id": acct["id"],
                        "account_title": acct["title"],
                    })
            # Brand Account / managed channels
            try:
                resp2 = yt.channels().list(part="snippet,statistics", managedByMe=True, maxResults=50).execute()
                for ch in resp2.get("items", []):
                    if ch["id"] not in seen_ids:
                        seen_ids.add(ch["id"])
                        all_channels.append({
                            "id": ch["id"],
                            "title": ch["snippet"]["title"],
                            "thumbnail": ch["snippet"]["thumbnails"]["default"]["url"],
                            "subscribers": ch["statistics"].get("subscriberCount", "0"),
                            "account_id": acct["id"],
                            "account_title": acct["title"],
                        })
            except Exception:
                pass  # managedByMe may not be available for all accounts
        except Exception as e:
            print(f"[!] Failed to list channels for {acct['title']}: {e}")
    return all_channels


def list_categories(region: str = "US") -> list[dict]:
    """Return assignable YouTube video categories."""
    accounts = list_accounts()
    if not accounts:
        return []
    yt = get_youtube_service(accounts[0]["id"])
    resp = yt.videoCategories().list(part="snippet", regionCode=region).execute()
    return [
        {"id": cat["id"], "title": cat["snippet"]["title"]}
        for cat in resp.get("items", [])
        if cat["snippet"].get("assignable")
    ]


# ── Upload ───────────────────────────────────────────────────────────────────


def upload_to_youtube(
    video_path: Path,
    title: str,
    description: str = "",
    tags: list = None,
    category_id: str = "22",
    privacy: str = "private",
    scheduled_time: datetime = None,
    channel_id: str = None,
) -> dict | None:
    """Upload a video with full metadata.  Returns {'id', 'url'} or None.

    channel_id: used to identify which account to upload from (the channel's
    account_id is the same as its channel_id for single-channel accounts).
    """
    from googleapiclient.http import MediaFileUpload

    # Use channel_id as account_id (for YouTube, channel ID = primary account ID)
    yt = get_youtube_service(channel_id)

    # Ensure Shorts format — append #Shorts to title and description
    if "#Shorts" not in title and "#shorts" not in title:
        title = f"{title} #Shorts"
    title = title[:100]
    if "#Shorts" not in description and "#shorts" not in description:
        description = f"{description}\n\n#Shorts".strip() if description else "#Shorts"
    if tags is None:
        tags = ["shorts", "viral", "clips"]
    elif "shorts" not in [t.lower() for t in tags]:
        tags = ["shorts"] + tags

    status_privacy = privacy
    if scheduled_time and privacy == "public":
        status_privacy = "private"  # must be private for scheduling

    body = {
        "snippet": {
            "title": title[:100],
            "description": description,
            "tags": tags or ["shorts", "viral", "clips"],
            "categoryId": str(category_id),
        },
        "status": {
            "privacyStatus": status_privacy,
            "selfDeclaredMadeForKids": False,
        },
    }
    if scheduled_time:
        body["status"]["publishAt"] = scheduled_time.strftime("%Y-%m-%dT%H:%M:%S.000Z")

    media = MediaFileUpload(str(video_path), chunksize=-1, resumable=True, mimetype="video/mp4")

    channel_info = f" → channel {channel_id}" if channel_id else ""
    print(f"[*] Uploading {video_path.name}{channel_info} ...")
    request = yt.videos().insert(part="snippet,status", body=body, media_body=media)

    response = None
    while response is None:
        status, response = request.next_chunk()
        if status:
            print(f"    {int(status.progress() * 100)}%")

    vid = response["id"]
    url = f"https://youtu.be/{vid}"
    print(f"[+] Uploaded → {url}")
    return {"id": vid, "url": url}


def build_schedule(
    clip_paths: list,
    start_time: datetime = None,
    interval_hours: int = 24,
) -> list:
    if start_time is None:
        start_time = datetime.utcnow() + timedelta(hours=1)
    return [
        {"path": p, "scheduled_time": start_time + timedelta(hours=interval_hours * i)}
        for i, p in enumerate(clip_paths)
    ]
