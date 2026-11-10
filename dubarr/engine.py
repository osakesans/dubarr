"""Dubarr engine: classify titles, keep language formats and profiles in shape, fix file languages
from real audio tracks, search new titles, and search upgrades in small nightly batches."""
import functools
import hashlib
import json
import re
import threading
import time

from . import audio, clients, config, store

PREFIX = "Dubarr: "
ORIGINAL_ID = -2
TOP_SCORE = 100

# ISO 639-2 codes in media info -> language names used by Sonarr and Radarr
ISO = {
    "eng": "English", "dan": "Danish", "swe": "Swedish", "nor": "Norwegian", "nob": "Norwegian",
    "nno": "Norwegian", "fin": "Finnish", "ice": "Icelandic", "isl": "Icelandic", "ger": "German",
    "deu": "German", "fre": "French", "fra": "French", "spa": "Spanish", "ita": "Italian",
    "por": "Portuguese", "dut": "Dutch", "nld": "Dutch", "pol": "Polish", "tur": "Turkish",
    "hun": "Hungarian", "rus": "Russian", "ukr": "Ukrainian", "jpn": "Japanese", "kor": "Korean",
    "chi": "Chinese", "zho": "Chinese", "cze": "Czech", "ces": "Czech", "slo": "Slovak", "slk": "Slovak",
    "gre": "Greek", "ell": "Greek", "heb": "Hebrew", "hin": "Hindi", "ara": "Arabic", "tha": "Thai",
    "vie": "Vietnamese", "rum": "Romanian", "ron": "Romanian", "bul": "Bulgarian", "hrv": "Croatian",
    "srp": "Serbian", "slv": "Slovenian", "est": "Estonian", "lav": "Latvian", "lit": "Lithuanian",
    "per": "Persian", "fas": "Persian", "ind": "Indonesian", "may": "Malay", "msa": "Malay",
    "tam": "Tamil", "tel": "Telugu", "tgl": "Tagalog", "fil": "Tagalog", "cat": "Catalan",
    "baq": "Basque", "eus": "Basque", "ben": "Bengali", "mar": "Marathi", "kan": "Kannada",
    "mal": "Malayalam", "urd": "Urdu", "geo": "Georgian", "kat": "Georgian", "mac": "Macedonian",
    "mkd": "Macedonian", "alb": "Albanian", "sqi": "Albanian", "bos": "Bosnian", "glg": "Galician",
}
UNKNOWN_AUDIO = {"", "und", "unk", "mis", "mul", "zxx", "qaa"}


# ---------- rules ----------
@functools.lru_cache(maxsize=512)
def parse_when(line):
    """'genre:animation & original:Japanese' -> [('genre', {'animation'}), ('original', {'japanese'})]"""
    line = line.strip()
    if not line or line == "*":
        return ()
    terms = []
    for part in line.split("&"):
        if ":" not in part:
            raise ValueError(f"bad rule term {part.strip()!r} (use key:value)")
        k, v = part.split(":", 1)
        k = k.strip().lower()
        if k not in ("genre", "original", "series_type"):
            raise ValueError(f"unknown rule key {k!r} (genre, original or series_type)")
        terms.append((k, frozenset(x.strip().lower() for x in v.split("|") if x.strip())))
    return tuple(terms)


def title_info(t):
    return {"genre": {g.lower() for g in t.get("genres") or []},
            "original": {((t.get("originalLanguage") or {}).get("name") or "").lower()},
            "series_type": {(t.get("seriesType") or "").lower()}}


def classify(kinds, t):
    info = title_info(t)
    for kind in kinds:
        for line in kind["when"]:
            if all(info[k] & vals for k, vals in parse_when(line)):
                return kind
    return None


def token_kind(tok):
    return "title" if tok.lower().startswith("title:") else "lang"


def token_format_name(tok):
    if token_kind(tok) == "title":
        rx = tok.split(":", 1)[1]
        return f"{PREFIX}Release {re.sub(r'[^A-Za-z0-9 ]', '', rx.replace('\\b', '')).strip() or 'title'}"
    return PREFIX + tok


def scores_for(kind):
    """First language 100 (the cutoff). The rest 40, 20, 10 ... so that all lower ones together
    never reach the cutoff: a file only stops upgrading when it has the first language."""
    out, s = {}, 40
    for i, tok in enumerate(kind["languages"]):
        out[token_format_name(tok)] = TOP_SCORE if i == 0 else max(1, s)
        if i:
            s //= 2
    return out


def reject_name(kind):
    return f"{PREFIX}Reject other languages ({kind['name']})"


# ---------- engine ----------
class Engine:
    def __init__(self):
        self.lock = threading.Lock()
        self._wake = threading.Event()
        self.summary = {}
        self.plans = {"sonarr": [], "radarr": []}
        self.format_plan = {"sonarr": [], "radarr": []}
        self.last_error = None
        self._last_msg = {}

    def trigger(self):
        self._wake.set()

    def configured(self, cfg):
        return any(cfg[a]["url"] and cfg[a]["api_key"] for a in ("sonarr", "radarr"))

    def _clients(self, cfg):
        return {a: clients.Arr(a, cfg[a]["url"], cfg[a]["api_key"])
                for a in ("sonarr", "radarr") if cfg[a]["url"] and cfg[a]["api_key"]}

    def test_connections(self):
        cfg = config.load()
        out = []
        for a in ("sonarr", "radarr"):
            if not (cfg[a]["url"] and cfg[a]["api_key"]):
                out.append((a.capitalize(), None, "not configured"))
                continue
            try:
                v = clients.Arr(a, cfg[a]["url"], cfg[a]["api_key"]).get("/system/status")["version"]
                out.append((a.capitalize(), True, f"version {v}"))
            except clients.ApiError as e:
                out.append((a.capitalize(), False, str(e)))
        return out

    def nextarr_live(self, cfg):
        if not cfg.get("nextarr_url"):
            return False
        try:
            return clients._request("GET", cfg["nextarr_url"].rstrip("/") + "/api/status",
                                    timeout=10).get("dry_run") is False
        except Exception:
            return False

    def _say(self, dry, key, msg, level="action"):
        """Log an action; in dry run only when the planned message changes."""
        if dry:
            msg = "[dry run] would " + msg
            if self._last_msg.get(key) == msg:
                return
            self._last_msg[key] = msg
            level = "plan"
        store.log(msg, level)

    # ---------- custom formats and profiles ----------
    def _desired_formats(self, cfg, lang_ids):
        want = {}
        for kind in cfg["kinds"]:
            for tok in kind["languages"]:
                name = token_format_name(tok)
                if token_kind(tok) == "title":
                    want[name] = [{"name": "Release title", "implementation": "ReleaseTitleSpecification",
                                   "negate": False, "required": True,
                                   "fields": [{"name": "value", "value": tok.split(":", 1)[1]}]}]
                else:
                    want[name] = [self._lang_spec(tok, lang_ids, False, False)]
            langs = [t for t in kind["languages"] if token_kind(t) == "lang"]
            if langs:
                want[reject_name(kind)] = [self._lang_spec(t, lang_ids, True, True) for t in langs]
        return want

    @staticmethod
    def _lang_spec(tok, lang_ids, negate, required):
        lid = ORIGINAL_ID if tok.lower() == "original" else lang_ids.get(tok.lower())
        if lid is None:
            raise ValueError(f"unknown language {tok!r}")
        return {"name": tok, "implementation": "LanguageSpecification", "negate": negate, "required": required,
                "fields": [{"name": "value", "value": lid}, {"name": "exceptLanguage", "value": False}]}

    @staticmethod
    def _spec_key(specs):
        return sorted((s["implementation"], bool(s["negate"]), bool(s["required"]),
                       tuple(str(f.get("value")) for f in s["fields"] if f["name"] in ("value", "exceptLanguage")))
                      for s in specs)

    def _sync_formats(self, cfg, arr, dry, languages):
        """Create/update Dubarr's custom formats and set the scores in the managed profiles."""
        changes = []
        lang_ids = {l["name"].lower(): l["id"] for l in languages}
        want = self._desired_formats(cfg, lang_ids)
        have = {cf["name"]: cf for cf in arr.get("/customformat")}
        for name, specs in want.items():
            cf = have.get(name)
            if cf is None:
                changes.append(f"create custom format {name}")
                if not dry:
                    have[name] = arr.send("POST", "/customformat",
                                          {"name": name, "includeCustomFormatWhenRenaming": False,
                                           "specifications": specs})
            elif self._spec_key(cf["specifications"]) != self._spec_key(specs):
                changes.append(f"update custom format {name}")
                if not dry:
                    arr.send("PUT", f"/customformat/{cf['id']}", dict(cf, specifications=specs))
        profiles = {p["name"]: p for p in arr.get("/qualityprofile")}
        owner = {}
        for kind in cfg["kinds"]:
            for pname in [kind["profile"]] + list(kind.get("also_profiles") or []):
                if pname in owner and owner[pname] is not kind:
                    raise ValueError(f"profile {pname!r} is used by both {owner[pname]['name']} and {kind['name']}")
                owner[pname] = kind
        retire = {n.strip() for n in cfg.get("retire_formats") or [] if n.strip()}
        for pname, kind in owner.items():
            p = profiles.get(pname)
            if not p:
                if pname == kind["profile"]:
                    changes.append(f"(profile {pname!r} for {kind['name']} does not exist in {arr.app})")
                continue
            score = scores_for(kind)
            if reject_name(kind) in want:
                score[reject_name(kind)] = int(cfg["reject_score"])
            diffs = []
            items = []
            for fi in p["formatItems"]:
                n = fi["name"]
                if n in score:
                    new = score[n]
                elif n.startswith(PREFIX) or n in retire:
                    new = 0
                else:
                    new = fi["score"]
                if new != fi["score"]:
                    diffs.append(f"{n} {fi['score']}→{new}")
                items.append(dict(fi, score=new))
            missing = [n for n in score if n not in {fi["name"] for fi in p["formatItems"]}]
            for n in missing:  # a format created just now
                if n in have and "id" in have[n]:
                    items.append({"format": have[n]["id"], "name": n, "score": score[n]})
                diffs.append(f"{n} →{score[n]}")
            fix = {}
            if p.get("cutoffFormatScore") != TOP_SCORE:
                fix["cutoffFormatScore"] = TOP_SCORE
                diffs.append(f"cutoff score {p.get('cutoffFormatScore')}→{TOP_SCORE}")
            if not p.get("upgradeAllowed"):
                fix["upgradeAllowed"] = True
                diffs.append("allow upgrades")
            if diffs:
                changes.append(f"profile {pname}: " + ", ".join(diffs))
                if not dry:
                    arr.send("PUT", f"/qualityprofile/{p['id']}", dict(p, formatItems=items, **fix))
        return changes

    # ---------- per-title planning ----------
    @staticmethod
    def audio_languages(media_info):
        codes = [c.strip().lower() for c in (media_info or {}).get("audioLanguages", "").split("/")]
        if not codes or any(c in UNKNOWN_AUDIO or c not in ISO for c in codes):
            return None  # unknown or untagged tracks: do not guess
        return {ISO[c] for c in codes}

    @staticmethod
    def relevant_languages(kind, t):
        orig = (t.get("originalLanguage") or {}).get("name")
        out = set()
        for tok in kind["languages"]:
            if token_kind(tok) == "lang":
                out.add(orig if tok.lower() == "original" else tok)
        out.discard(None)
        return out

    def _file_fix(self, kind, t, f, lang_ids):
        """New languages for a file whose tags disagree with its audio on a language that matters."""
        audio = self.audio_languages(f.get("mediaInfo"))
        if not audio:
            return None
        tagged = {l["name"] for l in f.get("languages") or []}
        rel = self.relevant_languages(kind, t)
        if all((l in audio) == (l in tagged) for l in rel):
            return None
        if any(a.lower() not in lang_ids for a in audio):
            return None
        return sorted(audio)

    def _plan_app(self, cfg, arr, dry, nextarr_live, languages, profile_list):
        app = arr.app
        lang_ids = {l["name"].lower(): l["id"] for l in languages}
        profiles = {p["id"]: p for p in profile_list}
        pid = {p["name"]: i for i, p in profiles.items()}
        managed = {pid[k["profile"]] for k in cfg["kinds"] if k["profile"] in pid}
        scored = managed | {pid[n] for k in cfg["kinds"] for n in k.get("also_profiles") or [] if n in pid}
        titles = arr.titles()
        rows = []
        for t in titles:
            kind = classify(cfg["kinds"], t)
            target = pid.get(kind["profile"]) if kind else None
            row = {"id": t["id"], "title": t["title"], "kind": kind["name"] if kind else "-",
                   "profile": profiles.get(t["qualityProfileId"], {}).get("name", "?"),
                   "target": profiles[target]["name"] if target else None,
                   "managed": t["qualityProfileId"] in managed, "scored": t["qualityProfileId"] in scored,
                   "switch": bool(target and t["qualityProfileId"] in managed and t["qualityProfileId"] != target),
                   "monitored": t.get("monitored", False), "fixes": [], "upgrades": [],
                   "files": 0, "audio": "", "tags": "", "score": None, "missing": False, "media": [],
                   "original": (t.get("originalLanguage") or {}).get("name")}
            rows.append((t, kind, row))
        # files
        if app == "radarr":
            ids = [t["id"] for t, _, _ in rows if t.get("hasFile")]
            files = {}
            for i in range(0, len(ids), 100):
                for f in arr.get("/moviefile", movieId=ids[i:i + 100]) or []:
                    files[f["movieId"]] = f
            for t, kind, row in rows:
                f = files.get(t["id"])
                row["missing"] = not f
                if not f or not kind or not row["scored"]:
                    continue
                row["files"] = 1
                row["media"].append({"path": f.get("path") or "", "label": t["title"]})
                row["audio"] = (f.get("mediaInfo") or {}).get("audioLanguages", "")
                row["tags"] = ", ".join(l["name"] for l in f.get("languages") or [])
                row["score"] = f.get("customFormatScore")
                fix = self._file_fix(kind, t, f, lang_ids)
                if fix:
                    row["fixes"].append({"file_id": f["id"], "label": "", "to": fix, "from": row["tags"]})
                cut = profiles[t["qualityProfileId"]].get("cutoffFormatScore", TOP_SCORE)
                if t.get("monitored") and ((row["score"] or 0) < cut or f.get("qualityCutoffNotMet")):
                    row["upgrades"].append({"id": t["id"], "label": ""})
        else:
            for t, kind, row in rows:
                st = t.get("statistics") or {}
                row["missing"] = st.get("episodeFileCount", 0) < st.get("episodeCount", 0)
                if not kind or not row["scored"] or not st.get("episodeFileCount"):
                    continue
                # Two calls on purpose: /episodefile also lists files that no episode points to.
                eps = {e["episodeFileId"]: e for e in arr.get("/episode", seriesId=t["id"]) if e.get("hasFile")}
                cut = profiles[t["qualityProfileId"]].get("cutoffFormatScore", TOP_SCORE)
                for f in arr.get("/episodefile", seriesId=t["id"]) or []:
                    row["files"] += 1
                    e = eps.get(f["id"])
                    label = f"S{e['seasonNumber']:02d}E{e['episodeNumber']:02d}" if e else f.get("relativePath", "")
                    row["media"].append({"path": f.get("path") or "", "label": f"{t['title']} {label}"})
                    fix = self._file_fix(kind, t, f, lang_ids)
                    if fix:
                        row["fixes"].append({"file_id": f["id"], "label": label, "to": fix,
                                             "from": ", ".join(l["name"] for l in f.get("languages") or [])})
                    if e and e.get("monitored") and ((f.get("customFormatScore") or 0) < cut
                                                    or f.get("qualityCutoffNotMet")):
                        row["upgrades"].append({"id": e["id"], "label": label})
        return rows

    # ---------- sync ----------
    def sync(self, reason="schedule"):
        if not self.lock.acquire(blocking=False):
            return
        try:
            self._sync(reason)
            self.last_error = None
        except Exception as e:
            self.last_error = str(e)
            store.log(f"Sync failed: {e}", "error")
        finally:
            self.lock.release()

    def _sync(self, reason):
        cfg = config.load()
        store.kv_set("heartbeat", time.time())
        if not self.configured(cfg):
            self.summary = {"setup_needed": True}
            return
        if config.paused():
            store.log("PAUSE file present: sync skipped", "warning")
            return
        dry = cfg["dry_run"]
        live_nextarr = self.nextarr_live(cfg)
        summary = {"last_sync": time.time(), "dry_run": dry, "nextarr_live": live_nextarr, "apps": {}}
        all_rows = []
        for app, arr in self._clients(cfg).items():
            languages = arr.get("/language")  # read once per app and sync
            if cfg["manage_formats"]:
                fchanges = self._sync_formats(cfg, arr, dry, languages)
                self.format_plan[app] = fchanges
                for c in fchanges:
                    self._say(dry, f"{app}:fmt:{c}", f"{app}: {c}")
            profile_list = arr.get("/qualityprofile")  # after the formats: their scores may just have changed
            rows = self._plan_app(cfg, arr, dry, live_nextarr, languages, profile_list)
            self._act(cfg, arr, rows, dry, live_nextarr, languages, profile_list)
            self.plans[app] = [r for _, _, r in rows]
            all_rows += [(t, kind, r) for t, kind, r in rows if kind and r["scored"]]
            kinds = {}
            for _, _, r in rows:
                kinds[r["kind"]] = kinds.get(r["kind"], 0) + 1
            summary["apps"][app] = {
                "titles": len(rows), "kinds": kinds,
                "managed": sum(r["managed"] for _, _, r in rows),
                "switch": sum(r["switch"] for _, _, r in rows),
                "fixes": sum(len(r["fixes"]) for _, _, r in rows),
                "upgrades": sum(len(r["upgrades"]) for _, _, r in rows),
                "format_changes": len(self.format_plan[app]) if cfg["manage_formats"] else 0}
        if cfg["set_default_audio"]:
            summary["audio"] = self._default_audio(cfg, dry, all_rows)
        self.summary = summary
        store.kv_set("heartbeat", time.time())

    def _act(self, cfg, arr, rows, dry, live_nextarr, languages, profile_list):
        app = arr.app
        pid = {p["name"]: p["id"] for p in profile_list}
        # 1. profile per kind
        if cfg["switch_profiles"]:
            for t, kind, r in rows:
                if r["switch"]:
                    self._say(dry, f"{app}:{r['id']}:profile",
                              f"move {r['title']} to profile {r['target']} ({r['kind']})")
                    if not dry:
                        arr.set_profile([r["id"]], pid[r["target"]])
                        r["profile"], r["switch"] = r["target"], False
        # 2. file languages from the real audio tracks
        if cfg["fix_file_languages"]:
            lang = {l["name"].lower(): l for l in languages}
            for t, kind, r in rows:
                groups = {}
                for fx in r["fixes"]:
                    groups.setdefault(tuple(fx["to"]), []).append(fx)
                for to, fxs in groups.items():
                    labels = ", ".join(x["label"] for x in fxs if x["label"])[:200]
                    self._say(dry, f"{app}:{r['id']}:lang:{to}:{len(fxs)}",
                              f"set file language of {r['title']}{' ' + labels if labels else ''} "
                              f"to {', '.join(to)} (audio tracks; was {fxs[0]['from'] or 'none'})")
                    if not dry:
                        arr.set_file_languages([x["file_id"] for x in fxs],
                                               [{"id": lang[a.lower()]["id"], "name": lang[a.lower()]["name"]}
                                                for a in to])
                if not dry and r["fixes"]:
                    # scores change with the new languages: decide upgrades on the next sync
                    r["fixes"], r["upgrades"] = [], []
        # 3. new titles: search after the profile is right
        seen = store.seen(app)
        ids = [r["id"] for _, _, r in rows]
        if not seen:
            store.mark_seen(app, ids)  # first run: everything already there counts as seen
            return
        new = [r for _, _, r in rows if r["id"] not in seen]
        want = cfg["search_new_movies"] if app == "radarr" else (cfg["search_new_series"] and not live_nextarr)
        for r in new:
            if want and r["monitored"] and r["missing"]:
                self._say(dry, f"{app}:{r['id']}:new", f"search new title {r['title']} [{r['kind']}]")
                if not dry:
                    arr.search_titles([r["id"]])
        if not dry:
            store.mark_seen(app, [r["id"] for r in new])

    # ---------- default audio track ----------
    def _default_audio(self, cfg, dry, rows):
        if not audio.TOOLS:
            return {"error": "mkvtoolnix not found"}
        # Rules changed: look at every file again.
        fp = hashlib.sha1(json.dumps(cfg["kinds"], sort_keys=True).encode()).hexdigest()
        if store.kv_get("audio_rules") != fp:
            store.forget_audio(("ok", "no match", "planned"))
            store.kv_set("audio_rules", fp)
        items = []
        for t, kind, r in rows:
            wanted = []
            for tok in kind["languages"]:
                if token_kind(tok) != "lang":
                    continue
                name = r["original"] if tok.lower() == "original" else tok
                if name:
                    wanted.append(audio.language_codes(name, ISO))
            for m in r["media"]:
                if m["path"]:
                    items.append(dict(m, wanted=wanted))
        plex = None
        if cfg["plex"]["url"] and cfg["plex"]["token"]:
            plex = clients.Plex(cfg["plex"]["url"], cfg["plex"]["token"], cfg["plex"].get("verify_ssl", False))

        def playing():
            """Names of the files Plex is playing; None when Plex cannot be asked (then nothing is edited)."""
            if not plex:
                return frozenset()
            try:
                return plex.playing_files()
            except clients.ApiError as e:
                store.log(f"Could not ask Plex what is playing, default audio waits for the next sync: {e}", "warning")
                return None

        dirs, read = audio.run(items, ISO, dry, int(cfg["audio_files_per_run"]),
                               lambda msg: self._say(dry, "audio:" + msg, msg),
                               lambda: audio.seeding_inodes([p for p in cfg.get("torrent_folders") or [] if p]),
                               playing)
        if dirs and plex:
            for d in sorted(dirs):
                try:
                    plex.refresh_path(d)
                except clients.ApiError as e:
                    store.log(f"Plex refresh of {d} failed: {e}", "warning")
        counts = store.audio_counts()
        mkv = sum(1 for i in items if i["path"].lower().endswith(".mkv"))
        return {"mkv_files": mkv, "checked": sum(counts.values()), "read_this_run": read, **counts}

    # ---------- webhooks: a title was just added ----------
    def title_added(self, app, title_id):
        """Fast path for a new title: set its profile before anything searches for it."""
        cfg = config.load()
        arr = self._clients(cfg).get(app)
        if not arr or not cfg["switch_profiles"]:
            return self.trigger()
        try:
            t = arr.get(f"/{'movie' if app == 'radarr' else 'series'}/{title_id}")
            kind = classify(cfg["kinds"], t)
            profiles = {p["id"]: p["name"] for p in arr.get("/qualityprofile")}
            pid = {n: i for i, n in profiles.items()}
            managed = {pid[k["profile"]] for k in cfg["kinds"] if k["profile"] in pid}
            if kind and kind["profile"] in pid and t["qualityProfileId"] in managed \
                    and t["qualityProfileId"] != pid[kind["profile"]]:
                self._say(cfg["dry_run"], f"{app}:{t['id']}:profile",
                          f"move {t['title']} to profile {kind['profile']} ({kind['name']})")
                if not cfg["dry_run"]:
                    arr.set_profile([t["id"]], pid[kind["profile"]])
        except Exception as e:
            store.log(f"New-title handling failed: {e}", "error")
        self.trigger()

    # ---------- nightly upgrades ----------
    def upgrade(self, reason="schedule"):
        with self.lock:
            cfg = config.load()
            dry = cfg["dry_run"]
            store.kv_set("last_upgrade", time.time())
            n = int(cfg["upgrade_searches_per_night"])
            cutoff = time.time() - cfg["upgrade_retry_days"] * 86400
            for app, arr in self._clients(cfg).items():
                searched = store.search_times(app)
                cands = []
                for r in self.plans.get(app, []):
                    for u in r["upgrades"]:
                        if searched.get(u["id"], 0) < cutoff:
                            cands.append((r, u))
                pick = cands[:n]
                if not pick:
                    continue
                desc = ", ".join(f"{r['title']}{' ' + u['label'] if u['label'] else ''}" for r, u in pick)
                if dry:
                    store.log(f"[dry run] {app}: would search upgrades for {len(pick)} of {len(cands)}: {desc}", "plan")
                    continue
                ids = [u["id"] for _, u in pick]
                if app == "radarr":
                    arr.search_titles(ids)
                else:
                    arr.search_episodes(ids)
                store.mark_searched(app, ids)
                store.log(f"{app}: searched upgrades for {len(pick)} of {len(cands)}: {desc}", "action")

    def upgrade_due(self, cfg, now=None):
        """True once a day, from upgrade_hour and for as long as one sync interval can last."""
        now = now or time.time()
        t = time.localtime(now)
        due = time.mktime((t.tm_year, t.tm_mon, t.tm_mday, int(cfg["upgrade_hour"]), 0, 0, 0, 0, -1))
        window = max(3600, int(cfg["sync_interval_minutes"]) * 60 + 900)
        return due <= now < due + window and store.kv_get("last_upgrade", 0) < due

    def run_forever(self):
        while True:
            self.sync()
            cfg = config.load()
            if self.upgrade_due(cfg):
                try:
                    self.upgrade()
                except Exception as e:
                    store.log(f"Upgrade run failed: {e}", "error")
            self._wake.wait(timeout=max(60, int(cfg["sync_interval_minutes"]) * 60))
            if self._wake.is_set():
                self._wake.clear()
                time.sleep(5)
