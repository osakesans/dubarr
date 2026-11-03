# Dubarr

Language rules for Sonarr and Radarr. Dubarr sorts every movie and series into a kind
(for example *Normal*, *Animation* for kids, *Anime*) and makes sure each kind is
downloaded in the right audio language.

- Moves titles to the **quality profile of their kind**, based on genre, original language
  and Sonarr series type.
- Creates **language custom formats** and sets their scores in those profiles, from a simple
  priority list per kind (for example `Danish, title:\bNORDIC\b, English`).
- Reads the **real audio tracks** of every file and fixes the file's languages when they are
  tagged wrong, so upgrades continue when a "Nordic" release has no Danish audio, and stop
  when a file already has it.
- Marks the **best audio track as default** in MKV files, so Plex starts with the right language
  for users who have not picked one (for example English instead of Japanese on a dubbed anime).
- Searches **new movies and series** after their profile is right (useful when Overseerr is
  set not to search).
- Searches **upgrades in small nightly batches**, so your disks and indexers are not flooded.

Dubarr starts in **dry run**: it only logs what it would do. Nothing is changed in Sonarr
or Radarr until you turn dry run off in the web UI.

## Rules

Edit them under **Rules** in the web UI. The defaults:

| Kind | Matches | Languages, best first | Profile |
| --- | --- | --- | --- |
| Anime | `series_type:anime`, `genre:anime`, or `genre:animation & original:Japanese` | English, Original | Anime |
| Animation | `genre:animation & genre:family\|children\|kids` | Danish, `title:\bNORDIC\b`, English | Animation |
| Normal | everything else (`*`) | Original, English | HD - 720p/1080p |

- Kinds are tried from the top; the first one with a matching rule line wins.
- A rule line matches when all its terms match. Terms: `genre:a|b`, `original:Japanese`,
  `series_type:anime`, joined with `&`. `*` matches everything.
- Languages use the names Sonarr and Radarr use. `Original` is the title's own language.
  `title:REGEX` matches the release name, for tags the apps do not parse (like NORDiC).
- The first language scores **100** and is the profile's cutoff. The others score 40, 20, 10 …,
  which together never reach 100: a file keeps upgrading until it has the first language.
- Releases in none of the listed languages score −10000 (rejected).
- Only titles that are in one of the kinds' profiles are moved between them. A title you put
  in, say, *Ultra-HD* keeps that profile. Add such profiles under *Also score these profiles*
  to give them the same language scores.

Dubarr's custom formats are named `Dubarr: …`. If you had language formats from an earlier
setup, list them under **Retire these custom formats**: their scores are set to 0 in the
managed profiles, and you can delete them later.

Change *Danish* to *Swedish* (or anything else) and you have the same setup for another country.

## Audio track check

Sonarr and Radarr tag a file's languages from the release name. That is often wrong:
a "NORDiC" release can lack Danish audio, and many Danish-dubbed releases are tagged English only.
Dubarr compares the tags with the audio tracks Sonarr/Radarr read from the file (media info).
When they disagree on a language that matters for the title's kind, the file's languages are set
to its audio tracks. Files with unknown or untagged tracks are left alone.

## Default audio track

Players such as Plex start with the audio track the file marks as *default*, unless the user has
chosen a preferred language in their account settings (that setting wins when the file has that
language). Many releases mark the wrong track: a foreign dub on an English series, Japanese on a
dubbed anime, or every track at once.

Dubarr reads the audio tracks of each MKV file (`mkvmerge -J`) and marks the track in the first of
the kind's languages that the file has as default, and unmarks the others. Commentary and
audio-description tracks are never chosen. Only the header is edited with `mkvpropedit`; nothing
is re-encoded. Each file is read once and again only when it changes. Set a Plex URL and token to
let Dubarr refresh the folder in Plex after a change.

Files are **left alone** when:

- they have more than one hard link — a torrent client is probably still seeding them, and editing
  the file would break the torrent (they are checked again later),
- they cannot be written, or are not MKV (MP4 is not supported),
- none of the kind's languages is in them.

This needs the media folder mounted read-write at the **same path as in Sonarr/Radarr**, for example
`- /srv/media:/data/media`. Without the mount, turn the feature off under Settings.

## Safety

- **Dry run** by default. The *Titles* and *Rules* pages and the log show what would change.
- Dubarr never deletes, renames or re-encodes files. It changes profiles, custom formats, file
  language tags and the default flag of audio tracks in MKV headers, and starts searches.
- Upgrade searches are limited per night (10 per app by default), and the same item is not
  searched again for 14 days.
- A file named `PAUSE` in the config folder stops all activity.
- Only the Python standard library. The image is pinned to an exact Python version and digest.

## Install

Images for amd64 and arm64 are published on GitHub Container Registry. Pin an exact version;
Dubarr never updates itself.

```sh
mkdir -p dubarr/config && sudo chown 1000:1000 dubarr/config   # the container runs as uid 1000
```

```yaml
services:
  dubarr:
    image: ghcr.io/osakesans/dubarr:0.2.0
    container_name: dubarr
    restart: unless-stopped
    user: "1000:1000"
    environment:
      - TZ=Etc/UTC   # your time zone, used for the nightly upgrade hour
    volumes:
      - ./config:/config
      - /srv/media:/data/media   # your media, same path as in Sonarr/Radarr (for default audio)
    ports:
      - "8791:8791"
    labels:
      com.centurylinklabs.watchtower.enable: "false"
```

Open `http://<host>:8791`, fill in Sonarr and/or Radarr under **Settings**, save, and press
**Test connections**. Then check **Rules**: the profile names must match profiles that exist in
your apps. Everything is stored in `/config/config.json`.

Requirements: Sonarr v4 and/or Radarr v5 or newer.

### Webhooks (optional)

Dubarr syncs every 15 minutes. Webhooks make it set the profile of a new title within seconds,
before anything searches for it. The URLs with your token are on the Settings page.

- **Sonarr:** Settings → Connect → Webhook, triggers *On Series Add* and *On Import*.
- **Radarr:** Settings → Connect → Webhook, triggers *On Movie Added* and *On Import*.

### With Overseerr

If you let Dubarr search new titles, turn off **Enable Automatic Search** for Sonarr/Radarr in
Overseerr, so the first search happens with the right profile.

### With Nextarr

[Nextarr](https://github.com/osakesans/nextarr) keeps a rolling TV library. Put its address
under Settings → *Nextarr URL*: while Nextarr runs live, Dubarr leaves the search for new
series to Nextarr. Upgrade searches only include monitored episodes, so episodes Nextarr
removed are never fetched again by Dubarr.

### Homepage

```yaml
labels:
  homepage.group: Media
  homepage.name: Dubarr
  homepage.href: http://dubarr.example
  homepage.widget.type: customapi
  homepage.widget.url: http://dubarr:8791/api/status
  homepage.widget.mappings[0].field: titles
  homepage.widget.mappings[0].label: Titles
  homepage.widget.mappings[1].field: profile_switches_24h
  homepage.widget.mappings[1].label: Moved today
  homepage.widget.mappings[2].field: language_fixes_pending
  homepage.widget.mappings[2].label: To fix
  homepage.widget.mappings[3].field: upgrade_candidates
  homepage.widget.mappings[3].label: Upgrades
```

## API

| Path | |
| --- | --- |
| `/api/status` | Counts as JSON (no login, for dashboards) |
| `/api/plan` | Full plan per title and pending format changes |
| `/health` | 200 when the scheduler is alive |

## License

MIT
