import base64
import os
import pickle
import tempfile

from google.auth.transport.requests import Request
from google_auth_oauthlib.flow import InstalledAppFlow

SCOPES = ["https://www.googleapis.com/auth/contacts"]

_TMP_TOKEN = os.path.join(tempfile.gettempdir(), "gc_token.pickle")
_TMP_CREDS = os.path.join(tempfile.gettempdir(), "gc_credentials.json")


def _st_secrets():
    try:
        import streamlit as st
        return st.secrets
    except Exception:
        return {}


def _load_token_from_secrets():
    """Secrets'tan refresh_token kullanarak Credentials nesnesi oluştur."""
    try:
        from google.oauth2.credentials import Credentials
        s = _st_secrets()
        refresh_token = s.get("REFRESH_TOKEN", "")
        if not refresh_token:
            return None
        return Credentials(
            token=None,
            refresh_token=refresh_token,
            token_uri=s.get("TOKEN_URI", "https://oauth2.googleapis.com/token"),
            client_id=s.get("CLIENT_ID", ""),
            client_secret=s.get("CLIENT_SECRET", ""),
            scopes=["https://www.googleapis.com/auth/contacts"],
        )
    except Exception:
        return None


def _credentials_file() -> str:
    """credentials.json yolunu döndürür; secrets'tan geldiyse /tmp'e yazar."""
    try:
        import json
        s = _st_secrets()
        client_id = s.get("CLIENT_ID", "")
        if client_id:
            data = {"installed": {
                "client_id": client_id,
                "client_secret": s.get("CLIENT_SECRET", ""),
                "auth_uri": "https://accounts.google.com/o/oauth2/auth",
                "token_uri": s.get("TOKEN_URI", "https://oauth2.googleapis.com/token"),
                "redirect_uris": ["http://localhost"],
            }}
            with open(_TMP_CREDS, "w") as f:
                json.dump(data, f)
            return _TMP_CREDS
    except Exception:
        pass
    if os.path.exists("credentials.json"):
        return "credentials.json"
    return ""


def get_credentials():
    creds = None

    # 1. /tmp — aynı session içinde daha önce yenilenmiş token
    if os.path.exists(_TMP_TOKEN):
        with open(_TMP_TOKEN, "rb") as fh:
            creds = pickle.load(fh)

    # 2. Streamlit secrets (cloud deploy)
    if creds is None:
        creds = _load_token_from_secrets()

    # 3. Yerel token.pickle (local dev)
    if creds is None and os.path.exists("token.pickle"):
        with open("token.pickle", "rb") as fh:
            creds = pickle.load(fh)

    if creds and creds.valid:
        return creds

    if creds and creds.expired and creds.refresh_token:
        creds.refresh(Request())
        _save(creds)
        return creds

    # OAuth browser akışı — sadece local dev için
    creds_file = _credentials_file()
    if not creds_file:
        return None

    flow = InstalledAppFlow.from_client_secrets_file(creds_file, SCOPES)
    creds = flow.run_local_server(port=0)
    _save(creds)
    return creds


def has_cloud_token() -> bool:
    """Secrets'ta token var mı? (otomatik giriş kararı için)"""
    try:
        s = _st_secrets()
        return bool(s["REFRESH_TOKEN"])
    except Exception:
        return False


def revoke():
    for path in (_TMP_TOKEN, "token.pickle"):
        if os.path.exists(path):
            os.remove(path)


def _save(creds):
    with open(_TMP_TOKEN, "wb") as fh:
        pickle.dump(creds, fh)
