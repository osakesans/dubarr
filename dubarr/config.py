"""Configuration: defaults, loading and saving /config/config.json."""
import copy
import json
import os
import secrets
import threading

CONFIG_DIR = os.environ.get("DUBARR_CONFIG_DIR", "/config")
CONFIG_PATH = os.path.join(CONFIG_DIR, "config.json")

# Each kind of title gets a language priority and a quality profile. Kinds are tried from the top;
# the first one whose "when" rules match wins. A "when" line matches when all its terms match:
#   genre:a|b          the title has genre a or b
#   original:Japanese  the original language is one of these
#   series_type:anime  Sonarr series type
#   *                  always
# Languages are tried in order. "Original" means the title's original language, and
# "title:REGEX" matches the release name (for tags like NORDiC that the apps do not parse).
DEFAULT_KINDS = [
    {"name": "Anime",
     "when": ["series_type:anime", "genre:anime", "genre:animation & original:Japanese"],
     "languages": ["English", "Original"],
     "profile": "Anime", "also_profiles": []},
    {"name": "Animation",
     "when": ["genre:animation & genre:family|children|kids"],
     "languages": ["Danish", "title:\\bNORDIC\\b", "English"],
     "profile": "Animation", "also_profiles": []},
    {"name": "Normal",
     "when": ["*"],
     "languages": ["Original", "English"],
     "profile": "HD - 720p/1080p", "also_profiles": []},
]

DEFAULTS = {
    # Safety first: a fresh install only logs what it would do.
    "dry_run": True,
    "sonarr": {"url": "http://sonarr:8989", "api_key": ""},
    "radarr": {"url": "http://radarr:7878", "api_key": ""},
    "kinds": DEFAULT_KINDS,
    # Custom formats from an earlier setup that Dubarr replaces: their scores are set to 0
    # in the profiles Dubarr manages.
    "retire_formats": [],
    "reject_score": -10000,
    # What Dubarr does
    "manage_formats": True,       # create Dubarr's custom formats and set profile scores
    "switch_profiles": True,      # move titles to the profile of their kind
    "fix_file_languages": True,   # set a file's languages from its real audio tracks
    "set_default_audio": True,    # mark the best audio track as default in MKV files (for Plex)
    "audio_files_per_run": 300,   # MKV files read per sync; each is read again only when it changes
    "torrent_folders": [],        # where your torrent client keeps files; hard links to them are never edited
    "plex": {"url": "", "token": "", "verify_ssl": False},  # optional: refresh Plex after a change
    "search_new_movies": True,    # search new Radarr movies (when Overseerr does not)
    "search_new_series": True,    # search new Sonarr series; turn off when Nextarr decides
    "nextarr_url": "",            # if set and Nextarr is live, new series are left to Nextarr
    "upgrade_searches_per_night": 10,
    "upgrade_retry_days": 14,
    "upgrade_hour": 3,
    "sync_interval_minutes": 15,
    "webhook_token": "",
    "auth": {"enabled": False, "username": "admin", "password_hash": ""},
}

_lock = threading.Lock()
_cache = None  # (file signature, merged config): the file is only parsed again when it changes


def _merge(base, override):
    out = copy.deepcopy(base)
    for k, v in (override or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _merge(out[k], v)
        else:
            out[k] = v
    return out


def _signature():
    try:
        st = os.stat(CONFIG_PATH)
        return (st.st_mtime_ns, st.st_size, st.st_ino)
    except OSError:
        return None


def load():
    """The current configuration. Every caller gets its own copy and may change it freely."""
    global _cache
    with _lock:
        sig = _signature()
        if _cache and sig is not None and _cache[0] == sig:
            return copy.deepcopy(_cache[1])
        data = {}
        if sig is not None:
            with open(CONFIG_PATH, encoding="utf-8") as f:
                data = json.load(f)
        cfg = _merge(DEFAULTS, data)
        if not cfg["webhook_token"]:
            cfg["webhook_token"] = secrets.token_urlsafe(24)
            _write(cfg)
        _cache = (_signature(), copy.deepcopy(cfg))
        return cfg


def _write(cfg):
    os.makedirs(CONFIG_DIR, exist_ok=True)
    tmp = CONFIG_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(cfg, f, indent=2)
    os.chmod(tmp, 0o600)
    os.replace(tmp, CONFIG_PATH)


def save(cfg):
    global _cache
    with _lock:
        _write(cfg)
        _cache = None


def paused():
    return os.path.exists(os.path.join(CONFIG_DIR, "PAUSE"))
