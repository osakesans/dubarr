"""Small HTTP client for Sonarr and Radarr (stdlib only)."""
import json
import ssl
import urllib.error
import urllib.parse
import urllib.request

UA = "Dubarr/0.1 (+https://github.com/osakesans/dubarr)"


class ApiError(Exception):
    pass


def _request(method, url, headers=None, body=None, timeout=60, verify=True):
    data = json.dumps(body).encode() if body is not None else None
    h = {"Accept": "application/json", "User-Agent": UA}
    if data is not None:
        h["Content-Type"] = "application/json"
    h.update(headers or {})
    req = urllib.request.Request(url, data=data, method=method, headers=h)
    try:
        ctx = None if verify or not url.startswith("https") else ssl._create_unverified_context()
        with urllib.request.urlopen(req, timeout=timeout, context=ctx) as r:
            raw = r.read()
            return json.loads(raw) if raw else None
    except urllib.error.HTTPError as e:
        detail = e.read()[:300].decode(errors="replace")
        raise ApiError(f"{method} {url.split('?')[0]} -> HTTP {e.code}: {detail}") from None
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        raise ApiError(f"{method} {url.split('?')[0]} -> {e}") from None


class Arr:
    """Sonarr v4 or Radarr v5+ (both speak API v3)."""

    def __init__(self, app, url, key):
        self.app = app  # "sonarr" or "radarr"
        self.base = url.rstrip("/") + "/api/v3"
        self.h = {"X-Api-Key": key}

    def get(self, path, timeout=120, **params):
        q = ("?" + urllib.parse.urlencode(params, doseq=True)) if params else ""
        return _request("GET", self.base + path + q, self.h, timeout=timeout)

    def send(self, method, path, body=None, timeout=120):
        return _request(method, self.base + path, self.h, body, timeout=timeout)

    # titles: movies or series
    def titles(self):
        return self.get("/movie" if self.app == "radarr" else "/series")

    def set_profile(self, ids, profile_id):
        if self.app == "radarr":
            self.send("PUT", "/movie/editor", {"movieIds": list(ids), "qualityProfileId": profile_id})
        else:
            self.send("PUT", "/series/editor", {"seriesIds": list(ids), "qualityProfileId": profile_id})

    def set_file_languages(self, file_ids, languages):
        key = "movieFileIds" if self.app == "radarr" else "episodeFileIds"
        path = "/moviefile/editor" if self.app == "radarr" else "/episodefile/editor"
        self.send("PUT", path, {key: list(file_ids), "languages": languages})

    def search_titles(self, ids):
        if not ids:
            return
        if self.app == "radarr":
            self.send("POST", "/command", {"name": "MoviesSearch", "movieIds": list(ids)})
        else:
            for sid in ids:
                self.send("POST", "/command", {"name": "SeriesSearch", "seriesId": sid})

    def search_episodes(self, ids):
        if ids:
            self.send("POST", "/command", {"name": "EpisodeSearch", "episodeIds": list(ids)})


class Plex:
    def __init__(self, url, token, verify_ssl=True):
        self.base = url.rstrip("/")
        self.h = {"X-Plex-Token": token}
        self.verify = verify_ssl

    def sections(self):
        r = _request("GET", self.base + "/library/sections", self.h, verify=self.verify) or {}
        return [(d["key"], [loc["path"] for loc in d.get("Location", [])])
                for d in r.get("MediaContainer", {}).get("Directory", [])]

    def refresh_path(self, path):
        """Partial scan of one folder, so Plex reads the changed file again."""
        for key, locs in self.sections():
            if any(path == l or path.startswith(l.rstrip("/") + "/") for l in locs):
                _request("GET", f"{self.base}/library/sections/{key}/refresh?"
                         + urllib.parse.urlencode({"path": path}), self.h, verify=self.verify)
                return True
        return False
