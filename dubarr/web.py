"""Built-in web UI, JSON API and webhooks (stdlib http.server)."""
import base64
import hashlib
import hmac
import html
import json
import secrets
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from . import __version__, config, engine as eng, store

E = html.escape

CSS = """
:root{--bg:#f7f7f5;--card:#fff;--ink:#1d1d1b;--muted:#6b6b66;--line:#e3e2dd;--accent:#7a4fd6;
--warn:#b7791f;--bad:#c53030;--good:#2f855a;--dry:#fff4d6}
@media (prefers-color-scheme:dark){:root{--bg:#161615;--card:#1f1f1d;--ink:#ecebe6;--muted:#9c9b95;
--line:#33322f;--accent:#a786ec;--warn:#e0a84a;--bad:#f07171;--good:#6cc08f;--dry:#3a3218}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--ink);
font:15px/1.5 system-ui,-apple-system,Segoe UI,Roboto,sans-serif}
header{display:flex;flex-wrap:wrap;gap:16px;align-items:center;padding:14px 20px;border-bottom:1px solid var(--line);background:var(--card)}
header b{font-size:18px;margin-right:12px}header a{color:var(--muted);text-decoration:none}header a.on{color:var(--ink);font-weight:600}
main{max-width:1100px;margin:0 auto;padding:20px 16px}
.card{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:16px 18px;margin-bottom:16px;overflow-x:auto}
.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(150px,1fr));gap:12px}
.stat b{display:block;font-size:26px}.stat span{color:var(--muted);font-size:13px}
table{border-collapse:collapse;width:100%}th,td{text-align:left;padding:7px 8px;border-bottom:1px solid var(--line);vertical-align:top}
th{color:var(--muted);font-weight:500;font-size:13px}.muted{color:var(--muted)}
.banner{background:var(--dry);border:1px solid var(--warn);border-radius:10px;padding:10px 14px;margin-bottom:16px}
.tag{display:inline-block;font-size:12px;padding:1px 8px;border-radius:99px;border:1px solid var(--line)}
.t-error{color:var(--bad)}.t-warning{color:var(--warn)}.t-action{color:var(--good)}.t-plan{color:var(--accent)}
input[type=text],input[type=number],input[type=password],textarea{width:100%;padding:7px 9px;border:1px solid var(--line);border-radius:6px;background:var(--bg);color:var(--ink);font:inherit}
textarea{font-family:ui-monospace,Menlo,Consolas,monospace;font-size:13px}
label{display:block;margin:10px 0 4px;font-weight:500}small{color:var(--muted)}
button,.btn{display:inline-block;padding:8px 16px;border-radius:6px;border:1px solid var(--accent);background:var(--accent);color:#fff;font:inherit;cursor:pointer;text-decoration:none}
button.sec{background:transparent;color:var(--accent)}
fieldset{border:1px solid var(--line);border-radius:8px;margin:0 0 14px;padding:4px 14px 14px}legend{padding:0 6px;color:var(--muted)}
code{font-size:13px;background:var(--bg);padding:1px 5px;border-radius:4px;word-break:break-all}
.filters a{margin-right:10px}
"""


def _ago(ts):
    if not ts:
        return "never"
    d = time.time() - ts
    if d < 120:
        return f"{int(d)} s ago"
    if d < 7200:
        return f"{int(d // 60)} min ago"
    if d < 172800:
        return f"{int(d // 3600)} h ago"
    return f"{int(d // 86400)} days ago"


def _hash_pw(pw, salt=None):
    salt = salt or secrets.token_hex(8)
    return f"{salt}${hashlib.pbkdf2_hmac('sha256', pw.encode(), salt.encode(), 200_000).hex()}"


def _check_pw(pw, stored):
    if not stored or "$" not in stored:
        return False
    return hmac.compare_digest(_hash_pw(pw, stored.split("$", 1)[0]), stored)


def _lines(s):
    return [x.strip() for x in (s or "").replace("\r", "").split("\n") if x.strip()]


def _commas(s):
    return [x.strip() for x in (s or "").split(",") if x.strip()]


def make_handler(engine):
    class H(BaseHTTPRequestHandler):
        server_version = f"Dubarr/{__version__}"

        def log_message(self, *a):
            pass

        # ----- helpers -----
        def _send(self, code, body, ctype="text/html; charset=utf-8", extra=None):
            b = body.encode() if isinstance(body, str) else body
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(b)))
            self.send_header("X-Frame-Options", "DENY")
            for k, v in (extra or {}).items():
                self.send_header(k, v)
            self.end_headers()
            self.wfile.write(b)

        def _json(self, obj, code=200):
            self._send(code, json.dumps(obj, default=str), "application/json")

        def _redirect(self, to):
            self._send(303, "", extra={"Location": to})

        def _body(self):
            n = int(self.headers.get("Content-Length") or 0)
            return self.rfile.read(min(n, 1_000_000)) if n else b""

        def _form(self):
            return {k: v[-1] for k, v in urllib.parse.parse_qs(self._body().decode(), keep_blank_values=True).items()}

        def _authed(self, cfg):
            a = cfg["auth"]
            if not a.get("enabled") or not a.get("password_hash"):
                return True
            h = self.headers.get("Authorization", "")
            if h.startswith("Basic "):
                try:
                    u, p = base64.b64decode(h[6:]).decode().split(":", 1)
                    return hmac.compare_digest(u, a.get("username", "")) and _check_pw(p, a["password_hash"])
                except Exception:
                    return False
            return False

        def _page(self, title, active, body):
            cfg = config.load()
            nav = "".join(f'<a href="{h}" class="{"on" if active == h else ""}">{n}</a>'
                          for h, n in [("/", "Overview"), ("/titles", "Titles"), ("/rules", "Rules"), ("/audio", "Audio"),
                                       ("/log", "Log"), ("/settings", "Settings")])
            banner = ""
            if not engine.configured(cfg):
                banner += ('<div class="banner"><b>Welcome to Dubarr.</b> Connect Sonarr and/or Radarr under '
                           '<a href="/settings">Settings</a>, press <i>Test connections</i>, then check the '
                           '<a href="/rules">Rules</a>.</div>')
            if cfg["dry_run"]:
                banner += ('<div class="banner"><b>Dry run.</b> Dubarr only logs what it would do. '
                           'Nothing is changed in Sonarr or Radarr. Turn it off under Settings.</div>')
            if config.paused():
                banner += '<div class="banner"><b>Paused.</b> A PAUSE file exists in the config folder.</div>'
            if engine.last_error:
                banner += f'<div class="banner"><b>Last run failed:</b> {E(engine.last_error)}</div>'
            return self._send(200, f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>Dubarr · {E(title)}</title>
<style>{CSS}</style></head><body><header><b>Dubarr</b>{nav}</header><main>{banner}{body}</main></body></html>""")

        # ----- routing -----
        def do_GET(self):
            self._route("GET")

        def do_POST(self):
            self._route("POST")

        def _route(self, method):
            u = urllib.parse.urlparse(self.path)
            path, q = u.path.rstrip("/") or "/", {k: v[-1] for k, v in urllib.parse.parse_qs(u.query).items()}
            cfg = config.load()
            try:
                if path == "/health":
                    hb = store.kv_get("heartbeat", 0)
                    ok = time.time() - hb < 3 * max(60, cfg["sync_interval_minutes"] * 60) + 900
                    return self._json({"ok": ok, "heartbeat": hb}, 200 if ok else 503)
                if path.startswith("/webhook/"):
                    return self._webhook(method, path, q, cfg)
                if path == "/api/status":
                    return self._json(self._status())
                if not self._authed(cfg):
                    return self._send(401, "Login required", extra={"WWW-Authenticate": 'Basic realm="Dubarr"'})
                if path == "/":
                    return self._overview()
                if path == "/titles":
                    return self._titles(q)
                if path == "/rules":
                    return self._rules(method, cfg)
                if path == "/audio":
                    return self._audio(q.get("status", "planned"))
                if path == "/log":
                    return self._log(q.get("level"))
                if path == "/settings":
                    return self._settings(method, cfg)
                if path == "/api/plan":
                    return self._json({"titles": engine.plans, "formats": engine.format_plan})
                if path == "/done":
                    return self._page("Done", "/", f'<div class="card"><b>{E(q.get("msg", ""))}</b></div>')
                if path == "/actions" and method == "POST":
                    return self._actions(self._form())
                return self._send(404, "Not found")
            except Exception as e:
                store.log(f"Web error on {path}: {e}", "error")
                return self._send(500, f"Error: {E(str(e))}")

        # ----- webhooks -----
        def _webhook(self, method, path, q, cfg):
            if method != "POST":
                return self._json({"error": "POST only"}, 405)
            token = q.get("token") or self.headers.get("X-Dubarr-Token", "")
            if not hmac.compare_digest(token, cfg["webhook_token"]):
                return self._json({"error": "bad token"}, 403)
            try:
                payload = json.loads(self._body() or b"{}")
            except ValueError:
                payload = {}
            app = path.split("/")[2]
            if app not in ("sonarr", "radarr"):
                return self._json({"error": "unknown source"}, 404)
            ev = payload.get("eventType")
            if ev == "Test":
                store.log(f"{app.capitalize()} test webhook received", "info")
                return self._json({"ok": True})
            item = payload.get("movie") if app == "radarr" else payload.get("series")
            if ev in ("SeriesAdd", "MovieAdded") and item and item.get("id"):
                store.log(f"{app.capitalize()}: added {item.get('title')}", "info")
                threading.Thread(target=engine.title_added, args=(app, item["id"]), daemon=True).start()
            else:
                engine.trigger()  # downloads/imports: check the new file's audio soon
            return self._json({"ok": True})

        # ----- pages -----
        def _status(self):
            s = engine.summary or {}
            apps = s.get("apps", {})
            day = time.time() - 86400
            tot = lambda k: sum(a.get(k, 0) for a in apps.values())
            return {"version": __version__, "dry_run": s.get("dry_run"), "last_sync": s.get("last_sync"),
                    "titles": tot("titles"), "managed": tot("managed"),
                    "profile_switches_pending": tot("switch"), "language_fixes_pending": tot("fixes"),
                    "upgrade_candidates": tot("upgrades"), "format_changes_pending": tot("format_changes"),
                    "changes_24h": store.count_log_since("action", day),
                    "profile_switches_24h": store.count_log_since("action", day, "move %"),
                    "language_fixes_24h": store.count_log_since("action", day, "set file language%"),
                    "default_audio_changed_24h": store.count_log_since("action", day, "set default audio%"),
                    "default_audio_pending": (s.get("audio") or {}).get("planned", 0),
                    "audio": s.get("audio") or {},
                    "apps": apps}

        def _overview(self):
            st = self._status()
            tiles = "".join(f'<div class="stat"><b>{v}</b><span>{n}</span></div>' for n, v in [
                ("titles", st["titles"]), ("managed", st["managed"]),
                ("profile switches pending", st["profile_switches_pending"]),
                ("file languages to fix", st["language_fixes_pending"]),
                ("upgrade candidates", st["upgrade_candidates"]),
                ("format changes pending", st["format_changes_pending"]),
                ("default audio to set", st["default_audio_pending"]),
                ("changes 24 h", st["changes_24h"])])
            kinds = "".join(f'<tr><td>{E(app.capitalize())}</td><td>{a["titles"]}</td><td>'
                            + ", ".join(f'{E(k)} {n}' for k, n in sorted(a["kinds"].items())) + "</td></tr>"
                            for app, a in st["apps"].items())
            logs = self._log_rows(store.recent_log(15))
            return self._page("Overview", "/", f"""
<div class="card"><div class="grid">{tiles}</div>
<p class="muted">Last sync {_ago(st['last_sync'])} · last upgrade search {_ago(store.kv_get('last_upgrade', 0))}</p>
<form method="post" action="/actions" style="display:flex;gap:8px;flex-wrap:wrap">
<button name="do" value="sync">Sync now</button>
<button name="do" value="upgrade" class="sec">Search upgrades now</button></form></div>
<div class="card"><b>Titles per kind</b><table><tr><th>App</th><th>Titles</th><th>Kinds</th></tr>{kinds}</table></div>
<div class="card"><b>Recent activity</b>{logs}</div>""")

        def _titles(self, q):
            app, kind, only = q.get("app", ""), q.get("kind", ""), q.get("only", "")
            rows = []
            for a, plan in engine.plans.items():
                if app and a != app:
                    continue
                for r in plan:
                    if kind and r["kind"] != kind:
                        continue
                    if only == "changes" and not (r["switch"] or r["fixes"]):
                        continue
                    if only == "upgrades" and not r["upgrades"]:
                        continue
                    prof = E(r["profile"]) + (f' → <b>{E(r["target"])}</b>' if r["switch"] else "")
                    if not r["managed"] and not r["scored"]:
                        prof += ' <small>(not managed)</small>'
                    fixes = "; ".join(f'{E(x["label"] or "file")}: {E(x["from"] or "none")} → {E(", ".join(x["to"]))}'
                                      for x in r["fixes"][:4]) + (" …" if len(r["fixes"]) > 4 else "")
                    files = (f'{E(r["tags"])} <small>audio {E(r["audio"])}</small>' if a == "radarr" and r["files"]
                             else f'{r["files"]} files' if r["files"] else '<span class="muted">-</span>')
                    rows.append(f'<tr><td>{E(r["title"])}</td><td><span class="tag">{E(a)}</span></td>'
                                f'<td>{E(r["kind"])}</td><td>{prof}</td><td>{files}</td>'
                                f'<td>{fixes or ""}</td><td>{len(r["upgrades"]) or ""}</td></tr>')
            kinds = sorted({r["kind"] for p in engine.plans.values() for r in p})
            link = lambda **kw: "/titles?" + urllib.parse.urlencode({**{"app": app, "kind": kind, "only": only}, **kw})
            filt = (" · ".join(f'<a href="{link(app=x)}">{x or "all apps"}</a>' for x in ("", "sonarr", "radarr"))
                    + "<br>" + " · ".join(f'<a href="{link(kind=x)}">{E(x or "all kinds")}</a>' for x in [""] + kinds)
                    + "<br>" + " · ".join(f'<a href="{link(only=x)}">{n}</a>'
                                         for x, n in (("", "everything"), ("changes", "with pending changes"),
                                                      ("upgrades", "with upgrade candidates"))))
            return self._page("Titles", "/titles", f"""<div class="card filters">{filt}</div>
<div class="card"><b>{len(rows)} titles</b><table><tr><th>Title</th><th>App</th><th>Kind</th><th>Profile</th>
<th>File languages</th><th>Language fixes</th><th>Upgrades</th></tr>
{''.join(rows) or '<tr><td colspan=7 class=muted>No sync yet</td></tr>'}</table></div>""")

        def _rules(self, method, cfg):
            msg = ""
            if method == "POST":
                f = self._form()
                kinds, err = [], ""
                for i in range(int(f.get("count", "0")) + 1):
                    name = f.get(f"k{i}.name", "").strip()
                    if not name or f.get(f"k{i}.delete"):
                        continue
                    k = {"name": name, "when": _lines(f.get(f"k{i}.when")) or ["*"],
                         "languages": _commas(f.get(f"k{i}.languages")),
                         "profile": f.get(f"k{i}.profile", "").strip(),
                         "also_profiles": _commas(f.get(f"k{i}.also_profiles")),
                         "_order": float(f.get(f"k{i}.order") or i)}
                    try:
                        for line in k["when"]:
                            eng.parse_when(line)
                    except ValueError as e:
                        err = f"{name}: {e}"
                    if not k["languages"] or not k["profile"]:
                        err = f"{name}: needs at least one language and a profile"
                    kinds.append(k)
                kinds.sort(key=lambda k: k.pop("_order"))
                if not kinds:
                    err = "At least one kind is needed"
                if err:
                    msg = f"Not saved. {err}"
                else:
                    cfg["kinds"] = kinds
                    cfg["retire_formats"] = _lines(f.get("retire_formats"))
                    config.save(cfg)
                    store.log("Rules changed", "info")
                    engine.trigger()
                    return self._redirect("/done?msg=" + urllib.parse.quote("Rules saved. The next sync uses them."))
            blocks = []
            for i, k in enumerate(cfg["kinds"] + [{"name": "", "when": [], "languages": [], "profile": "",
                                                    "also_profiles": []}]):
                new = not k["name"]
                sc = eng.scores_for(k) if k["languages"] else {}
                blocks.append(f"""<fieldset><legend>{E(k['name']) if not new else 'Add a kind'}</legend>
<div style="display:grid;grid-template-columns:1fr 90px;gap:10px">
<div><label>Name</label><input type="text" name="k{i}.name" value="{E(k['name'])}"></div>
<div><label>Order</label><input type="number" name="k{i}.order" value="{i}"></div></div>
<label>When (one rule per line, first kind that matches wins)</label>
<textarea name="k{i}.when" rows="3">{E(chr(10).join(k['when']))}</textarea>
<label>Languages, best first</label><input type="text" name="k{i}.languages" value="{E(', '.join(k['languages']))}">
{'<small>Scores: ' + E(', '.join(f'{n.replace(eng.PREFIX, "")} {s}' for n, s in sc.items())) + '</small>' if sc else ''}
<label>Profile</label><input type="text" name="k{i}.profile" value="{E(k['profile'])}">
<label>Also score these profiles (titles in them are not moved)</label>
<input type="text" name="k{i}.also_profiles" value="{E(', '.join(k.get('also_profiles') or []))}">
{'' if new else f'<label><input type="checkbox" name="k{i}.delete"> Delete this kind</label>'}</fieldset>""")
            fplan = "".join(f'<li><span class="tag">{E(a)}</span> {E(c)}</li>'
                            for a, cs in engine.format_plan.items() for c in cs)
            return self._page("Rules", "/rules", f"""{f'<div class="banner">{E(msg)}</div>' if msg else ''}
<div class="card"><b>How rules work</b><p class="muted">Every movie and series gets the first kind whose rules match.
A rule line matches when all its terms match: <code>genre:animation|anime</code>, <code>original:Japanese</code>,
<code>series_type:anime</code>, joined with <code>&amp;</code>. <code>*</code> matches everything.
Languages use the names Sonarr and Radarr use (Danish, English …). <code>Original</code> is the title's own language,
<code>title:REGEX</code> matches the release name, e.g. <code>title:\\bNORDIC\\b</code>.
The first language scores 100 and is the cutoff, so upgrades continue until a file has it.
Releases in none of the listed languages are rejected.</p>
<p class="muted">Only titles in one of the kinds' profiles are moved between them. Titles in other profiles
(e.g. Ultra-HD) keep their profile.</p></div>
<form method="post"><input type="hidden" name="count" value="{len(cfg['kinds'])}">
<div class="card">{''.join(blocks)}</div>
<div class="card"><label>Retire these custom formats (one per line)</label>
<textarea name="retire_formats" rows="6">{E(chr(10).join(cfg.get('retire_formats') or []))}</textarea>
<small>Language formats from an earlier setup. Their scores are set to 0 in the profiles above.</small></div>
<button>Save rules</button></form>
<div class="card" style="margin-top:16px"><b>Pending changes to formats and profiles</b>
<ul>{fplan or '<li class="muted">none</li>'}</ul></div>""")

        def _audio(self, status):
            a = (engine.summary or {}).get("audio") or {}
            names = [("planned", "would change (dry run)"), ("changed", "changed"), ("seeding", "skipped: seeding"),
                     ("read-only", "skipped: read-only"), ("no match", "no wanted language"),
                     ("ok", "already right"), ("error", "errors")]
            nav = " · ".join(f'<a href="/audio?status={urllib.parse.quote(k)}">{n} ({a.get(k, 0)})</a>' for k, n in names)
            rows = "".join(f'<tr><td>{E(r["path"].rsplit("/", 1)[-1])}</td><td><code>{E(r["detail"])}</code></td>'
                           f'<td class="muted">{_ago(r["ts"])}</td></tr>' for r in store.audio_rows(status))
            return self._page("Audio", "/audio", f"""<div class="card"><b>Default audio track</b>
<p class="muted">Dubarr marks the audio track in the best language of the title's kind as the default in MKV files,
so Plex starts with it for users who have not chosen a language themselves. Only the file header changes.
Files that are still seeding (more than one hard link), read-only files and MP4 files are left alone.
Seeding files are checked again later. <code>*</code> marks the default track.</p>
<p>{a.get('checked', 0)} of {a.get('mkv_files', 0)} MKV files checked.</p><p>{nav}</p></div>
<div class="card"><table><tr><th>File</th><th>Audio tracks</th><th>Checked</th></tr>
{rows or '<tr><td colspan=3 class=muted>none</td></tr>'}</table></div>""")

        def _log(self, level):
            levels = ["", "action", "plan", "warning", "error", "info"]
            nav = " · ".join(f'<a href="/log?level={l}">{l or "all"}</a>' for l in levels)
            return self._page("Log", "/log", f'<div class="card">{nav}{self._log_rows(store.recent_log(400, level or None))}</div>')

        def _log_rows(self, rows):
            return "<table>" + "".join(
                f'<tr><td class="muted" style="white-space:nowrap">{time.strftime("%m-%d %H:%M", time.localtime(r["ts"]))}</td>'
                f'<td class="t-{r["level"]}">{E(r["level"])}</td><td>{E(r["msg"])}</td></tr>' for r in rows) + "</table>"

        def _actions(self, f):
            if f.get("do") == "sync":
                threading.Thread(target=engine.sync, args=("manual",), daemon=True).start()
            elif f.get("do") == "upgrade":
                threading.Thread(target=engine.upgrade, args=("manual",), daemon=True).start()
                return self._redirect("/done?msg=" + urllib.parse.quote("Upgrade search started, see the Log page."))
            elif f.get("do") == "test":
                rows = "".join(f'<tr><td>{E(n)}</td><td class="{"t-action" if ok else "t-error" if ok is False else "muted"}">'
                               f'{"OK" if ok else "failed" if ok is False else "-"}</td><td>{E(d)}</td></tr>'
                               for n, ok, d in engine.test_connections())
                return self._page("Test connections", "/settings",
                                  f'<div class="card"><b>Connection test</b><table>{rows}</table>'
                                  f'<p><a href="/settings">Back to settings</a></p></div>')
            return self._redirect("/")

        def _settings(self, method, cfg):
            saved = ""
            if method == "POST":
                f = self._form()
                for svc in ("sonarr", "radarr"):
                    cfg[svc]["url"] = f.get(f"{svc}.url", "").strip()
                    if f.get(f"{svc}.api_key", "").strip():
                        cfg[svc]["api_key"] = f[f"{svc}.api_key"].strip()
                cfg["plex"]["url"] = f.get("plex.url", "").strip()
                if f.get("plex.token", "").strip():
                    cfg["plex"]["token"] = f["plex.token"].strip()
                cfg["plex"]["verify_ssl"] = "plex.verify_ssl" in f
                for k in ("upgrade_searches_per_night", "upgrade_retry_days", "upgrade_hour",
                          "sync_interval_minutes", "audio_files_per_run"):
                    if f.get(k, "").strip().isdigit():
                        cfg[k] = int(f[k])
                if f.get("reject_score", "").strip().lstrip("-").isdigit():
                    cfg["reject_score"] = int(f["reject_score"])
                cfg["nextarr_url"] = f.get("nextarr_url", "").strip()
                cfg["torrent_folders"] = [x.strip() for x in f.get("torrent_folders", "").split(",") if x.strip()]
                for k in ("dry_run", "manage_formats", "switch_profiles", "fix_file_languages", "set_default_audio",
                          "search_new_movies", "search_new_series"):
                    cfg[k] = k in f
                cfg["auth"]["enabled"] = "auth.enabled" in f
                cfg["auth"]["username"] = f.get("auth.username", cfg["auth"]["username"]).strip() or "admin"
                if f.get("auth.password"):
                    cfg["auth"]["password_hash"] = _hash_pw(f["auth.password"])
                if cfg["auth"]["enabled"] and not cfg["auth"]["password_hash"]:
                    cfg["auth"]["enabled"] = False
                    saved = "Login was not enabled: set a password first. "
                config.save(cfg)
                store.log("Settings changed", "info")
                engine.trigger()
                saved += "Saved."
            c = cfg

            def txt(name, val, help_="", typ="text"):
                return (f'<label>{E(name.split(".")[-1].replace("_", " ").capitalize())}</label>'
                        f'<input type="{typ}" name="{name}" value="{E(str(val))}">'
                        + (f"<small>{E(help_)}</small>" if help_ else ""))

            def box(name, on, text):
                return f'<label><input type="checkbox" name="{name}" {"checked" if on else ""}> {E(text)}</label>'

            svc = "".join(f'<fieldset><legend>{n.capitalize()}</legend>{txt(n + ".url", c[n]["url"])}'
                          f'<label>API key</label><input type="password" name="{n}.api_key" '
                          f'placeholder="{"stored, leave empty to keep" if c[n]["api_key"] else "not set"}">'
                          f'<small>Leave the URL empty to not use {n.capitalize()}.</small></fieldset>'
                          for n in ("sonarr", "radarr"))
            base = "http://<dubarr-host>:8791"
            return self._page("Settings", "/settings", f"""<form method="post">
{f'<div class="banner">{E(saved)}</div>' if saved else ''}
<div class="card"><fieldset><legend>Mode</legend>{box('dry_run', c['dry_run'], 'Dry run: only log what would happen')}</fieldset>
<fieldset><legend>What Dubarr does</legend>
{box('manage_formats', c['manage_formats'], 'Create language custom formats and set their scores in the profiles')}
{box('switch_profiles', c['switch_profiles'], 'Move titles to the profile of their kind')}
{box('fix_file_languages', c['fix_file_languages'], "Set a file's languages from its real audio tracks")}
{box('set_default_audio', c['set_default_audio'], 'Mark the best audio track as default in MKV files (needs the media folder mounted read-write)')}
{txt('audio_files_per_run', c['audio_files_per_run'], 'MKV files read per sync', 'number')}
{txt('torrent_folders', ', '.join(c.get('torrent_folders') or []), "Your torrent client's download folders, comma separated, as Dubarr sees them. Files hard-linked to them are never edited.")}
{box('search_new_movies', c['search_new_movies'], 'Search new movies in Radarr (when Overseerr does not search)')}
{box('search_new_series', c['search_new_series'], 'Search new series in Sonarr')}
{txt('nextarr_url', c['nextarr_url'], 'Optional. While Nextarr runs live, it decides which episodes are searched.')}
{txt('reject_score', c['reject_score'], 'Score for releases in none of the languages', 'number')}</fieldset>
<fieldset><legend>Upgrades and schedule</legend>
{txt('upgrade_searches_per_night', c['upgrade_searches_per_night'], 'Per app. Small batches spare your disks and indexers', 'number')}
{txt('upgrade_retry_days', c['upgrade_retry_days'], 'Search the same item again after this many days', 'number')}
{txt('upgrade_hour', c['upgrade_hour'], 'Hour of the nightly upgrade search (0-23)', 'number')}
{txt('sync_interval_minutes', c['sync_interval_minutes'], '', 'number')}</fieldset></div>
<div class="card">{svc}<fieldset><legend>Plex (optional)</legend>{txt('plex.url', c['plex']['url'], 'Refreshes the folder in Plex after a default audio change, e.g. https://plex:32400')}
<label>Token</label><input type="password" name="plex.token" placeholder="{'stored, leave empty to keep' if c['plex']['token'] else 'not set'}">
{box('plex.verify_ssl', c['plex'].get('verify_ssl', False), 'Verify the Plex certificate')}</fieldset></div>
<div class="card"><fieldset><legend>Login</legend>{box('auth.enabled', c['auth']['enabled'], 'Require login for the web UI')}
{txt('auth.username', c['auth']['username'])}
<label>Password</label><input type="password" name="auth.password" placeholder="{'stored, leave empty to keep' if c['auth']['password_hash'] else 'not set'}">
</fieldset></div>
<div class="card"><b>Webhooks</b><p class="muted">Optional, makes Dubarr react at once. Sonarr: Connect → Webhook,
"On Series Add" and "On Import". Radarr: "On Movie Added" and "On Import".</p>
<code>{E(base)}/webhook/sonarr?token={E(c['webhook_token'])}</code><br>
<code>{E(base)}/webhook/radarr?token={E(c['webhook_token'])}</code></div>
<div style="display:flex;gap:8px;flex-wrap:wrap"><button>Save</button>
<button formaction="/actions" name="do" value="test" class="sec">Test connections</button></div></form>""")

    return H


def serve(engine, port):
    srv = ThreadingHTTPServer(("0.0.0.0", port), make_handler(engine))
    srv.daemon_threads = True
    srv.serve_forever()
