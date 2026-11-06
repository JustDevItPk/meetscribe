"""API keys for AI agents: kept in your desktop's keyring (GNOME Keyring / KWallet, via libsecret) when it is
available, otherwise in ~/.config/meetscribe/api-keys.json, readable by you only. Never in settings.json, never
logged.

Keys are stored per agents.account(cfg), e.g. "openai:https://api.openai.com/v1".
"""
import json
import os
import pathlib

CONFIG_DIR = pathlib.Path(os.environ.get("MEETSCRIBE_CONFIG_DIR", pathlib.Path.home() / ".config/meetscribe"))
LABEL = "MeetScribe API key"
_schema = None
_secret = None


def _keyring():
    """libsecret, or None when it isn't installed or no keyring service is running (set MEETSCRIBE_NO_KEYRING=1
    to always use the file)."""
    global _schema, _secret
    if os.environ.get("MEETSCRIBE_NO_KEYRING"):
        return None
    if _secret is None:
        try:
            import gi
            gi.require_version("Secret", "1")
            from gi.repository import Secret
            _schema = Secret.Schema.new("dev.meetscribe.ApiKey", Secret.SchemaFlags.NONE,
                                        {"account": Secret.SchemaAttributeType.STRING})
            _secret = Secret
        except (ImportError, ValueError, AttributeError):
            _secret = False
    return _secret or None


def _file():
    return CONFIG_DIR / "api-keys.json"


def _read_file():
    try:
        data = json.loads(_file().read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _write_file(data):
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    path = _file()
    tmp = path.with_suffix(".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(data, f)
    os.chmod(tmp, 0o600)
    tmp.replace(path)


def get(account):
    """The stored key for `account`, or ""."""
    secret = _keyring()
    if secret is not None:
        try:
            value = secret.password_lookup_sync(_schema, {"account": account}, None)
            if value:
                return value
        except Exception:  # noqa: BLE001 - keyring locked or not running: fall back to the file
            pass
    return _read_file().get(account, "")


def put(account, key):
    """Store (or with an empty key, forget) the key for `account`. Returns where it went: "keyring" or "file"."""
    key = (key or "").strip()
    if not key:
        forget(account)
        return None
    secret = _keyring()
    if secret is not None:
        try:
            if secret.password_store_sync(_schema, {"account": account}, secret.COLLECTION_DEFAULT,
                                          f"{LABEL} ({account.split(':', 1)[0]})", key, None):
                data = _read_file()
                if data.pop(account, None) is not None:
                    _write_file(data)
                return "keyring"
        except Exception:  # noqa: BLE001 - no keyring service: use the file
            pass
    data = _read_file()
    data[account] = key
    _write_file(data)
    return "file"


def forget(account):
    secret = _keyring()
    if secret is not None:
        try:
            secret.password_clear_sync(_schema, {"account": account}, None)
        except Exception:  # noqa: BLE001
            pass
    data = _read_file()
    if data.pop(account, None) is not None:
        _write_file(data)
