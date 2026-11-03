"""Default audio track: mark the best audio track of an MKV file as default, so players such as
Plex start with the right language for users who have not chosen one themselves.

Only the MKV header is edited (mkvpropedit), nothing is re-encoded. A file is skipped when it has
more than one hard link (a torrent client is probably still seeding it: editing it would break
the torrent), when it cannot be written, or when none of the kind's languages is in it."""
import json
import os
import shutil
import subprocess

from . import store

TOOLS = bool(shutil.which("mkvmerge") and shutil.which("mkvpropedit"))
SKIP_WORDS = ("comment", "descriptive", "description", "audio description", "synstolk", "visually")


def language_codes(name, iso):
    """'Danish' -> {'dan', 'da'}; codes from the ISO table plus 2-letter IETF tags."""
    codes = {c for c, n in iso.items() if n.lower() == name.lower()}
    two = {"English": "en", "Danish": "da", "Swedish": "sv", "Norwegian": "no", "Finnish": "fi",
           "German": "de", "French": "fr", "Spanish": "es", "Italian": "it", "Japanese": "ja",
           "Dutch": "nl", "Portuguese": "pt", "Polish": "pl", "Korean": "ko", "Chinese": "zh"}
    if name in two:
        codes.add(two[name])
        if name == "Norwegian":
            codes |= {"nb", "nn"}
    return codes


def probe(path):
    r = subprocess.run(["mkvmerge", "-J", path], capture_output=True, text=True, timeout=120)
    if r.returncode not in (0, 1):
        raise RuntimeError((r.stdout or r.stderr).strip()[:200])
    tracks = []
    for t in json.loads(r.stdout).get("tracks", []):
        if t.get("type") != "audio":
            continue
        p = t.get("properties", {})
        tracks.append({"lang": (p.get("language") or "und").lower(),
                       "ietf": (p.get("language_ietf") or "").split("-")[0].lower(),
                       "default": bool(p.get("default_track")),
                       "name": p.get("track_name") or "",
                       "skip": bool(p.get("flag_commentary") or p.get("flag_visual_impaired"))
                       or any(w in (p.get("track_name") or "").lower() for w in SKIP_WORDS)})
    return tracks


def choose(tracks, wanted_codes):
    """Index (0-based among audio tracks) that should be default, or None to leave the file alone."""
    for codes in wanted_codes:
        hits = [i for i, t in enumerate(tracks) if not t["skip"] and (t["lang"] in codes or t["ietf"] in codes)]
        if hits:
            cur = [i for i in hits if tracks[i]["default"]]
            return cur[0] if cur else hits[0]
    return None


def needs_change(tracks, idx):
    return any(t["default"] != (i == idx) for i, t in enumerate(tracks))


def apply(path, tracks, idx):
    cmd = ["mkvpropedit", path]
    for i in range(len(tracks)):
        cmd += ["--edit", f"track:a{i + 1}", "--set", f"flag-default={1 if i == idx else 0}"]
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
    if r.returncode not in (0, 1):
        raise RuntimeError((r.stdout or r.stderr).strip()[:200])


def describe(tracks, idx=None):
    return " / ".join(f"{t['lang']}{'*' if (t['default'] if idx is None else i == idx) else ''}"
                      for i, t in enumerate(tracks))


def run(items, iso, dry, limit, say):
    """items: [{'path', 'label', 'wanted': [codes, ...]}]. Returns (folders changed, files read)."""
    changed_dirs, read = set(), 0
    for it in items:
        path = it["path"]
        if not path.lower().endswith(".mkv"):
            continue
        try:
            st = os.stat(path)
        except OSError:
            continue
        prev = store.audio_state(path)
        if prev and prev["mtime"] == st.st_mtime and prev["size"] == st.st_size \
                and prev["status"] not in ("planned", "seeding"):
            continue  # unchanged since we looked (seeding files are looked at again)
        if prev and prev["status"] == "planned" and dry:
            continue  # already listed in the dry-run plan
        if read >= limit:
            break
        read += 1
        try:
            tracks = probe(path)
        except Exception as e:
            store.set_audio_state(path, st.st_mtime, st.st_size, "error", str(e))
            continue
        idx = choose(tracks, it["wanted"]) if len(tracks) > 0 else None
        if idx is None or not needs_change(tracks, idx):
            store.set_audio_state(path, st.st_mtime, st.st_size,
                                  "ok" if idx is not None else "no match", describe(tracks))
            continue
        detail = f"{describe(tracks)} → {describe(tracks, idx)}"
        if st.st_nlink > 1:
            store.set_audio_state(path, st.st_mtime, st.st_size, "seeding", detail)
            continue
        if not os.access(path, os.W_OK):
            store.set_audio_state(path, st.st_mtime, st.st_size, "read-only", detail)
            continue
        if dry:
            store.set_audio_state(path, st.st_mtime, st.st_size, "planned", detail)
            say(f"set default audio of {it['label']} to {tracks[idx]['lang']} ({detail})")
            continue
        try:
            apply(path, tracks, idx)
        except Exception as e:
            store.set_audio_state(path, st.st_mtime, st.st_size, "error", f"{detail}: {e}")
            store.log(f"Default audio of {it['label']} failed: {e}", "error")
            continue
        st2 = os.stat(path)
        store.set_audio_state(path, st2.st_mtime, st2.st_size, "changed", detail)
        say(f"set default audio of {it['label']} to {tracks[idx]['lang']} ({detail})")
        changed_dirs.add(os.path.dirname(path))
    return changed_dirs, read
