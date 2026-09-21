# D&D Session Recorder (Discord bot)

A self-hosted Discord bot that records D&D sessions per speaker, hands the audio
to a transcriber running somewhere else, and posts the transcript that comes
back.

**This half does not transcribe.** No Whisper, no GPU, ~850 MB image (down from
1.4 GB before the split — over half of what remains is ffmpeg, which encodes the
audio), and it idles at a couple of hundred MB of RAM. The smallest VPS is
plenty. The heavy work happens in
[dnd_transcriber](https://github.com/samanadam/dnd_transcriber), which runs
wherever you have CPU to spare and never needs to reach Discord.

That split exists because the two jobs want opposite machines: recording needs
Discord reachability and almost no CPU; transcription needs a lot of CPU and no
network at all. It also means the bot can live outside a country where Discord
is blocked, while your recordings are transcribed at home.

- **Recording:** per-speaker, written to disk incrementally as packets arrive.
- **Handover:** finished sessions are encoded to Opus and staged in an outbox
  with everything needed to transcribe them — over a shared filesystem, or
  through a Cloudflare R2 bucket both halves reach outbound.
- **State:** SQLite (WAL) for metadata, filesystem for audio.
- **Bot UI language:** English. Transcripts are Turkish by default.

---

## Table of contents

1. [How a session flows](#how-a-session-flows)
2. [Commands](#commands)
3. [Setup: creating the Discord bot](#setup-creating-the-discord-bot)
4. [Configuration](#configuration)
5. [Deployment](#deployment)
6. [Your first session](#your-first-session)
7. [Data layout](#data-layout)
8. [The handover contract](#the-handover-contract)
9. [Retention, exports and backups](#retention-exports-and-backups)
10. [Music](#music)
11. [The portal API](#the-portal-api)
12. [Development and tests](#development-and-tests)
13. [Troubleshooting](#troubleshooting)
14. [Known limitations](#known-limitations)

---

## How a session flows

```
/session start   -> bot joins your voice channel, records each speaker
                    to /data/sessions/<id>/audio/raw/<user_id>.pcm
  (during play)     packets are appended and fsync-ed every ~5s
/session stop    -> PCM is encoded to Opus and moved to /data/outbox/<id>/
                    with metadata.json, then marked READY
  (with R2)      -> the staged session is uploaded to the bucket and the local
                    copy is released once R2 confirms it
  (whenever)     -> the transcriber collects it, transcribes it elsewhere,
                    and writes /data/inbox/<id>/ + DONE
                 -> this bot posts transcript.md to the channel the session
                    came from, and releases the staged audio
  (+7 days)      -> any audio still here is deleted; transcripts kept forever
```

How quickly a transcript comes back is up to whoever runs the transcriber. For a
laptop switched on in the evening, expect the next day.

### On recording two channels at once

All session state is keyed by `(guild_id, channel_id)` — there is no global
"current session" anywhere in the code.

**Discord itself is the ceiling here:** an account can only be connected to one
voice channel per server, so one bot token can only record one channel at a
time. A second `/session start` in another channel gets an explicit error naming
the session already running. To record a side table in parallel, run a second
instance with its own bot token and its own `DATA_DIR`.

## Commands

Starting, stopping and cancelling are open to anyone in the voice channel —
the people in the channel are the people being recorded. The three commands
marked 🔒 reach *backwards* into past sessions and publish their words or raw
audio into whatever channel the caller is in, so they are restricted to
members with **Manage Guild** or the role named by `SESSION_ADMIN_ROLE_ID`.

| Command | What it does |
| --- | --- |
| `/session start [name] [campaign]` | Joins your current voice channel and starts recording. The campaign defaults to the one mapped to the channel. |
| `/session stop` | Stops the session, encodes the audio and stages it for the transcriber. |
| `/session status` | The active session, or what is waiting for a transcript. |
| `/session cancel` | Stops and **discards** the session, deleting its audio. |
| `/session list` | Recent completed sessions with id, name, channel, date, duration. |
| `/session transcript <id>` 🔒 | Re-posts a past session's transcript. |
| `/session recover <id>` 🔒 | Finalizes and stages a session left open by a crash. |
| `/session export <id>` 🔒 | Zips a session's transcript and any audio still here. |
| `/character set <user> <name> [campaign]` | Maps a Discord user to a character name, globally or only in one campaign. 🔒 for anyone but yourself. |
| `/character clear <user> [campaign]` | Removes a mapping. 🔒 for anyone but yourself. |
| `/character list [campaign]` | Shows all mappings, or one campaign's. |
| `/init <total> [name]` | Tells the DM your initiative. You roll the dice; the number goes to the DM tracker and is never posted to the channel. |
| `/campaign list` | Campaigns, their voice channels and session counts. |
| `/campaign create <name> [voice_channel]` 🔒 | Creates a campaign, optionally owning a voice channel. |
| `/campaign channel <campaign> [voice_channel]` 🔒 | Sets or clears a campaign's voice channel. |

**Speaker labels** resolve as character name (`/character set`) → server nickname
→ username, and are frozen into `metadata.json` when the session is staged. That
is what lets the transcriber work with no database and no Discord access — and
why running `/character set` for every player before your first session is the
single biggest thing you can do for transcript quality.

### Campaigns

Several campaigns can share one server. A session picks its campaign in this
order: the one named at `/session start` (or by the portal), else the campaign
mapped to the voice channel, else none. A campaign has its own character names
(laid over the global ones), a list of names for Whisper to expect, and a list
of "heard → correct" fixes.

A session with no campaign is transcribed without a glossary and stays
*unassigned*. It can be assigned, or moved to another campaign, at any time,
even after transcription. Nothing on disk changes when that happens: speaker
labels are re-resolved from the new campaign's character names, and its
corrections are applied when the transcript is read. The Discord-posted
transcript file is not rewritten.

## Setup: creating the Discord bot

1. [Discord Developer Portal](https://discord.com/developers/applications) →
   **New Application**.
2. **Bot** tab → **Reset Token** → copy it. This is `DISCORD_TOKEN`.
   **Never commit it.** If it leaks, reset it immediately.
3. Same tab, enable **Server Members Intent**. (Message Content is not needed.)
4. **OAuth2 → URL Generator**: scopes `bot` and `applications.commands`;
   permissions **View Channel**, **Connect**, **Send Messages**, **Attach
   Files**.
5. Open the generated URL and add the bot to your server:

   ```
   https://discord.com/api/oauth2/authorize?client_id=<APPLICATION_ID>&permissions=274881105920&scope=bot%20applications.commands
   ```

6. Enable Developer Mode (User Settings → Advanced), then right-click your
   server → **Copy Server ID** for `GUILD_ID`, and yourself → **Copy User ID**
   for `ADMIN_USER_ID`.

Commands register to that single guild, so they appear within seconds rather
than after Discord's global propagation delay.

## Configuration

Copy `.env.example` to `.env`. Every setting is read from the environment.

| Variable | Default | Notes |
| --- | --- | --- |
| `DISCORD_TOKEN` | — | **Required.** Never commit it. |
| `GUILD_ID` | — | **Required.** The single server this bot serves. |
| `TRANSCRIBE_LANGUAGE` | `tr` | Travels in `metadata.json`; used by the transcriber. |
| `WHISPER_PROMPT_EXTRA` | — | Campaign vocabulary, also passed through as metadata. |
| `OUTBOX_ENABLED` | `true` | Stage sessions for collection. No reason to disable. |
| `DATA_HOST_DIR` | `./data` | Host directory to bind-mount. Must be writable by uid 10001. |
| `DATA_DIR` | `/data` | Path inside the container. Leave alone. |
| `AUDIO_FORMAT` | `opus` | ~32× smaller than `wav`, no measured accuracy cost. |
| `AUDIO_RETENTION_DAYS` | `7` | Days before audio still here is deleted. |
| `TRASH_RETENTION_DAYS` | `7` | Days a deleted session waits in the trash before it is removed for good. |
| `DISK_WARNING_THRESHOLD_MB` | `5000` | Warn below this much free space. |
| `EXPECTED_SESSION_HOURS` | `4` | Session length assumed by the free-space check at `/session start`. |
| `ADMIN_USER_ID` | — | Fallback DM recipient for warnings and fatal errors. Always privileged. |
| `SESSION_ADMIN_ROLE_ID` | — | Role allowed to run the 🔒 commands. Manage Guild works regardless. |
| `STORAGE_BACKEND` | `local` | `local` (transcriber pulls over SSH) or `r2` (Cloudflare R2). |
| `R2_ACCOUNT_ID` | — | Required when `STORAGE_BACKEND=r2`. From the R2 overview page. |
| `R2_ACCESS_KEY_ID` | — | Required when `STORAGE_BACKEND=r2`. |
| `R2_SECRET_ACCESS_KEY` | — | Required when `STORAGE_BACKEND=r2`. |
| `R2_BUCKET` | — | Required when `STORAGE_BACKEND=r2`. The transcriber uses the same bucket. |
| `UPLOAD_INTERVAL_SECONDS` | `120` | How often staged sessions are pushed to R2. |
| `EXPORT_MAX_DISCORD_UPLOAD_MB` | `25` | Larger exports report a path instead of uploading. |
| `TIMEZONE` | `Europe/Istanbul` | Transcript timestamps and the backup schedule. |
| `DB_BACKUP_KEEP_DAYS` | `14` | Daily `bot.db` copies kept in `/data/backups`. |
| `LOG_LEVEL` | `INFO` | Everything logs to stdout. |
| `API_ENABLED` | `false` | The portal API. Turning it on exposes control of recordings. |
| `API_HOST` | `127.0.0.1` | Bind address. Compose sets `0.0.0.0` inside the container. |
| `API_PORT` | `8080` | Published to the host's loopback only. |
| `API_TOKEN` | — | Required when the API is on. At least 32 characters. |
| `API_CORS_ORIGINS` | — | Exact browser origins, comma-separated. `*` is refused. |
| `API_RATE_LIMIT_PER_MINUTE` | `60` | Per token. Recording and search writes are capped lower. |
| `MUSIC_ENABLED` | `false` | Play tracks into the recorded channel. |
| `MUSIC_R2_PREFIX` | `music` | Where tracks live in the bucket. Needs `STORAGE_BACKEND=r2`. |
| `MUSIC_DEFAULT_VOLUME` | `0.3` | 1.0 is full volume, 2.0 the ceiling. |
| `MUSIC_MAX_QUEUE` | `100` | Tracks held, current one included. |
| `MUSIC_CACHE_MAX_MB` | `2000` | Cache under `DATA_DIR/music`, pruned oldest first. |
| `MUSIC_RESUME_AFTER_RECONNECT` | `true` | Restart the current track after a voice reconnect. |
| `MUSIC_YTDLP_ENABLED` | `false` | YouTube streaming, and the switch SoundCloud needs too. Not in the default image. |
| `MUSIC_SOUNDCLOUD_ENABLED` | `true` | SoundCloud search, music, ambience and effects. Only has an effect when `MUSIC_YTDLP_ENABLED=true`. |
| `MUSIC_YTDLP_TIMEOUT_SECONDS` | `20` | How long a link resolution may take before it is killed. |
| `MUSIC_YTDLP_MAX_CONCURRENT` | `2` | Resolutions running at once. This host is also recording. |
| `MUSIC_STREAM_TTL_SECONDS` | `1800` | Re-resolve a stream URL older than this before playing it. |
| `MUSIC_MAX_TRACK_SECONDS` | `10800` | Refuse tracks longer than this. |
| `MUSIC_ALLOW_LIVE` | `false` | Live streams never end, so they block the queue. |
| `MUSIC_YT_SFX_MAX_SECONDS` | `60` | Longest YouTube video accepted as a sound effect. |
| `MUSIC_YT_AMBIENCE_MAX_SECONDS` | `1800` | Longest YouTube video accepted as an ambience loop. |
| `MUSIC_YT_LAYER_MAX_MB` | `40` | Largest audio file saved for one YouTube sound. |
| `MUSIC_YT_DOWNLOAD_TIMEOUT_SECONDS` | `90` | How long saving one YouTube sound may take before it is killed. |

Note there are no Whisper model settings here. This host does not run Whisper;
those live in the transcriber's configuration.

## Deployment

### Sizing

Audio is encoded to Opus at the end of each session:

| session length | 3 players | 5 players | 7 players |
| --- | --- | --- | --- |
| 2 h | 130 MB | 216 MB | 302 MB |
| 3 h | 194 MB | 324 MB | 454 MB |
| 4 h | 259 MB | 432 MB | 605 MB |

Staged audio is deleted as soon as R2 confirms it holds every byte, so the
steady-state footprint is small. **During play, though, the raw PCM capture is
full size** — budget ~0.7 GB per speaker-hour of free space, released when the
session is finalized.

The peak lands at `/session stop`: every speaker's raw capture is still on disk
while the first one is encoded, and encoding writes an intermediate WAV. So a
6-hour game with 6 speakers touches roughly **29 GB** before it starts falling.
`/session start` refuses outright when the disk cannot hold
`EXPECTED_SESSION_HOURS` at the current headcount, because running out mid-game
does not raise — the recording would silently stop capturing.

A 40 GB VPS with 1 GB RAM suits 3–4 hour games. For 6-hour tables, or a host
shared with other services, size for the peak above.

### First run

The image runs as an unprivileged user (uid 10001), so the bind-mounted data
directory must be writable by that user. Docker creates it owned by root, so
**do this once first** or the bot exits immediately with a `Configuration error`
naming this exact fix:

```bash
git clone https://github.com/samanadam/dnd_bot.git /opt/dnd-bot
cd /opt/dnd-bot

cp .env.example .env
$EDITOR .env      # DISCORD_TOKEN, GUILD_ID, ADMIN_USER_ID, DATA_HOST_DIR

sudo mkdir -p /srv/dnd-bot-data
sudo chown -R 10001:10001 /srv/dnd-bot-data

docker compose up -d --build
docker compose logs -f
```

Wait for `Bot ready`. There is no model download — startup is seconds, not
minutes.

What the compose file does for you:

- `restart: unless-stopped` — survives host reboots.
- `stop_grace_period: 600s` — shutdown finalizes in-progress recordings first,
  and a long session is minutes of encoding. Docker's 10 s default would
  `SIGKILL` that almost immediately. Even if a restart still cuts it short
  nothing is lost — the raw capture survives and `/session recover <id>`
  finishes it — but that recovery is manual.
- `no-new-privileges` and `cap_drop: ALL` — the container writes files and
  talks to two APIs; it needs no Linux capabilities at all.
- `init: true` — reaps ffmpeg subprocesses and forwards signals cleanly.
- `mem_limit: 1g`, `cpus: 1.0` — this half only writes packets to disk.
- Log rotation capped at 3 × 10 MB.

### Connecting the transcriber

Two ways, chosen with `STORAGE_BACKEND`. Nothing is exposed to the internet
either way.

**Cloudflare R2 (`STORAGE_BACKEND=r2`)** — recommended. Neither machine needs to
reach the other, so there is no SSH account on this host and nothing forwarded
at home. R2 charges no egress, which is what makes pulling gigabytes of audio
down to a domestic connection free.

1. Cloudflare dashboard → **R2** → **Create bucket**. Any name; a private
   bucket, no public access.
2. **R2 → Manage API Tokens → Create API Token**, permission **Object Read &
   Write**, scoped to that one bucket. Copy the Access Key ID and Secret — the
   secret is shown once.
3. Take the **Account ID** from the R2 overview page (it is not on the token
   page).
4. Put all four into `.env` here *and* the same four into the transcriber's
   `.env`, with `STORAGE_BACKEND=r2` on both. Same bucket, both ends.

Verify the credentials before your first real session — this writes, lists and
deletes one small object:

```bash
docker compose run --rm dnd-bot python scripts/check_r2.py
```

Finished sessions are uploaded by a background pass every
`UPLOAD_INTERVAL_SECONDS`; the local staging copy is deleted only once R2
confirms it holds the complete session, so a failed upload costs a delay and
never audio. Sessions staged while the network is down are uploaded on the next
pass, or on the next start.

R2 also becomes the long-term audio archive: the transcriber leaves collected
sessions in place rather than deleting them (`R2_KEEP_AUDIO=false` on that side
reverses this). At Opus sizes a weekly game costs a few cents a month. Set a
bucket lifecycle rule if you would rather they expired.

**SSH (`STORAGE_BACKEND=local`)** — the transcriber pulls over SSH, so it needs
a user on this host that can read `<DATA_HOST_DIR>/outbox` and write
`<DATA_HOST_DIR>/inbox`. The simplest arrangement is to add your login user to
the group owning that directory, then add the transcriber machine's public key
to `~/.ssh/authorized_keys`. The transcriber always initiates.

### Upgrading

```bash
git pull && docker compose up -d --build
```

Migrations run automatically and are idempotent. In-progress recordings are
finalized before the old container exits.

### Hosting on a laptop

If this runs on a spare laptop rather than a VPS, three defaults will break it:

```bash
# 1. Never sleep - closing the lid would kill a live recording.
sudo mkdir -p /etc/systemd/logind.conf.d
sudo tee /etc/systemd/logind.conf.d/no-suspend.conf >/dev/null <<'EOF'
[Login]
HandleLidSwitch=ignore
HandleLidSwitchExternalPower=ignore
HandleLidSwitchDocked=ignore
EOF
sudo systemctl restart systemd-logind
sudo systemctl mask sleep.target suspend.target hibernate.target hybrid-sleep.target
```

2. Move `unattended-upgrades`' 02:00 automatic reboot outside your play hours,
   in `/etc/apt/apt.conf.d/50unattended-upgrades`.
3. Prefer ethernet. Voice receive drops more often on Wi-Fi; the bot reconnects,
   but each drop leaves a gap in that speaker's timeline. If Wi-Fi is
   unavoidable, disable power saving in NetworkManager.

## Your first session

1. `docker compose up -d`, wait for `Bot ready`.
2. Run `/character set` once per player. Do not skip this — it is what makes
   names come out right.
3. 60-second dry run: join voice, `/session start test`, everyone says a
   sentence, `/session stop`. Confirm `/data/outbox/<id>/` appears with
   `metadata.json`, the `.opus` tracks and a `READY` marker.
4. On the transcriber machine, run `dndt session`.
5. Confirm the transcript is posted back into Discord on its own.

If the bot crashes mid-session nothing is lost: restart it, look for
`Recoverable session <id>` in the logs, and run `/session recover <id>`.

## Data layout

```
/data/
  sessions/<session_id>/
    audio/raw/<user_id>.pcm    # appended live; consumed at finalize
    transcript.md              # copied here when it comes back
    transcript.json
  outbox/<session_id>/         # staged for the transcriber
    metadata.json              #   everything needed to transcribe, no DB required
    <user_id>.opus
    READY                      #   written last; nothing acts before it exists
  inbox/<session_id>/          # transcripts sent back; consumed and removed
  .inbox-staging/              # R2 downloads land here first, then move in
  exports/<session_id>.zip     # from /session export; never auto-deleted
  backups/bot-<date>.db        # nightly database copies
  bot.db                       # SQLite metadata (WAL mode)
  heartbeat                    # touched every 30s; read by the healthcheck
```

Schema changes go in `migrations/NNN_description.sql`; a `schema_version` table
records what has been applied, and the runner is safe to re-run.

## The handover contract

`dnd_bot/contract.py` is **duplicated verbatim** in the transcriber repository.
It defines the exchange format shown above, and carries a schema version so that
two halves at different versions fail loudly instead of misreading each other.

Marker files (`READY`, `DONE`) are always written last, so a directory still
being copied is invisible to the other side and an interrupted transfer is
harmless rather than a corrupt half-session.

**Change `contract.py` in both repositories in the same commit.**

`metadata.json` also carries an optional `campaign_id` and `campaign_name`. They
were added without a schema bump because nothing else about the format changed:
an older transcriber ignores them, and an older recorder omits them. The
campaign's names for Whisper travel in the existing `prompt_extra`.

R2 does not change any of this. Object keys mirror the directory layout
(`outbox/<session_id>/READY`, `inbox/<session_id>/DONE`) and the marker object
is still written last, so a session mid-upload stays invisible to the other side
exactly as a directory mid-copy does. That is why moving to object storage
needed no edit to `contract.py` at all — only a new transport either end.

## Retention, exports and backups

- When a transcript comes back, `audio_expires_at` is set to now + 7 days and
  the staged audio is released immediately — the transcriber keeps the archive.
- A cleanup pass (on startup, then every few hours) deletes audio past its
  expiry. **Transcripts, database rows and `/data/exports/` are never touched.**
- The same pass warns below `DISK_WARNING_THRESHOLD_MB`, to the channel and by
  DM.
- Daily at 05:00 local time, `bot.db` is copied to `/data/backups/bot-<date>.db`
  using SQLite's online backup API, keeping `DB_BACKUP_KEEP_DAYS` of history.
  That covers metadata only — it is not a substitute for a host-level backup.

To restore: `docker compose down`, copy the backup over `data/bot.db`,
`sudo chown 10001:10001 data/bot.db`, `docker compose up -d`.

## Music

The bot can play music into the voice channel it is recording. Discord never
sends a bot its own audio back, so playback does **not** land in the speaker
tracks — but a player without headphones will bleed it into theirs.

Off by default. To turn it on:

```
MUSIC_ENABLED=true
STORAGE_BACKEND=r2        # tracks live in the bucket you already have
MUSIC_R2_PREFIX=music
MUSIC_DEFAULT_VOLUME=0.3  # under the voices
```

Upload tracks to `music/` in the bucket (`.opus`, `.ogg`, `.mp3`, `.m4a`,
`.flac`, `.wav`, `.aac`). The bot downloads what it plays into
`DATA_DIR/music`, then prunes that cache back to `MUSIC_CACHE_MAX_MB`, oldest
first. It refuses to cache at all when free space is below
`DISK_WARNING_THRESHOLD_MB`: raw capture needs the disk more than a song does.

Music and recording share the guild's single voice connection. The recorder
always wins — starting a session takes the connection back, and music re-attaches
to it once the session is up.

### Ambience and effects library

The soundboard plays **ambience** (loops, up to 3 at once) and **effects** (one
shot, up to 6) over the music. The library lives in the bucket and nowhere else:
`music/ambience/` and `music/sfx/` (under `MUSIC_R2_PREFIX`). The bot only keeps a
cache of what it has played; deleting that cache loses nothing.

There are four ways to fill it:

- **Bulk import** a folder from your own machine with
  `scripts/import_sounds.py`. Lay the folder out as `ambience/` and `sfx/`, then:

  ```
  python scripts/import_sounds.py ./sounds --dry-run   # check, upload nothing
  python scripts/import_sounds.py ./sounds
  ```

  It needs the same `.env` as `scripts/check_r2.py` and reads the folder once,
  keeping no copy. Each file is put through the portal's upload checks: a safe
  name, the size cap (`MUSIC_UPLOAD_MAX_MB`), audio that matches its extension,
  ffprobe finding real audio, effects at most 2 minutes, ambience at most
  `MUSIC_MAX_TRACK_SECONDS`. A name already in the bucket is skipped, never
  overwritten, so re-running after adding files is safe. Anything refused is
  listed with the reason, and one bad file never stops the rest.
- **Upload one at a time** from the portal (`POST /api/v1/music/upload`).
- **Play a link** from YouTube or SoundCloud with no upload at all (see below).
- **Add objects directly** to the bucket with any S3 tool, in the same folders.

Where to find sounds that are free to use. Check each sound's own licence before
you use it: terms differ per sound and per site.

- [Freesound](https://freesound.org) — community sounds, each tagged with its own
  licence. Filter to CC0 for no strings; CC-BY needs credit.
- [Sonniss](https://sonniss.com/gameaudiogdc) — yearly free game-audio bundles.
- [Pixabay](https://pixabay.com/sound-effects/) — effects and ambience under the
  site's content licence.
- [Tabletop Audio](https://tabletopaudio.com) — ambience made for tabletop games,
  with its own usage terms.
- YouTube or SoundCloud ambience mixes, through the link sources below.

Tags on sounds (several per sound, changeable at any time) are kept by the
portal, not by the bot: the bot lists files and nothing more, so it needs no
change for them.

If a licence asks for credit, keep a credits list of your own. This repository is
public and must not hold audio, and the bucket should not be made public.

### YouTube (optional)

`MUSIC_YTDLP_ENABLED=true` adds streaming through yt-dlp. It is off by default
and not in the image, because it breaks whenever YouTube changes, needs frequent
updating, and is against YouTube's terms of service. If you want it:

```
docker compose build --build-arg WITH_YTDLP=true
```

How it is run, and why:

- **As a subprocess**, not in a thread. A timeout therefore actually kills the
  work instead of abandoning a thread that keeps running.
- **Bounded**: `MUSIC_YTDLP_MAX_CONCURRENT` resolutions at a time. The same host
  is recording audio.
- **Music is never downloaded.** yt-dlp produces a URL; ffmpeg streams it. The
  resolver is also forbidden its own cache directory, so it cannot write to the
  disk the recording depends on. The one exception is below.
- **Stream URLs expire.** YouTube signs them, and honours its own `expire`
  parameter when it is sooner than `MUSIC_STREAM_TTL_SECONDS`. A track queued
  half an hour ago is re-resolved before it plays instead of failing on a dead
  link.
- **Live streams and very long videos are refused** by default: anything queued
  behind them would never play.
- **Failures are classified.** Private, members-only, age-restricted, region
  blocked, rate limited and "yt-dlp needs updating" each get their own message.
  yt-dlp's raw output goes to the log only — it quotes URLs and local paths.

Keep it updated. When YouTube changes, the symptom is a resolver error saying so,
and the fix is rebuilding with a newer `yt-dlp` pin in
`requirements-optional.txt`.

#### Ambience and effects from YouTube

The soundboard (`/api/v1/soundboard/play` with `"source": "youtube"`) does not
stream. A loop would meet an expired URL halfway through a session, and every
effect would wait on a resolve. Instead the audio is **saved once** in
`DATA_DIR/music/youtube/` and played from disk, so a repeat is instant and needs
no network at all.

- **Exact link only.** `https://www.youtube.com/watch?v=<11 characters>`, nothing
  else: no playlists, short links, extra parameters or other hosts. The link only
  names a video; the URL yt-dlp fetches and the file name are rebuilt from the id.
- **Refused before downloading**: playlists, live streams, unknown lengths, and
  anything over `MUSIC_YT_SFX_MAX_SECONDS` (effects) or
  `MUSIC_YT_AMBIENCE_MAX_SECONDS` (ambience).
- **Bounded on disk**: `MUSIC_YT_LAYER_MAX_MB` per file (yt-dlp is told, and the
  size is checked again afterwards), the same free-space guard as every cache
  write, and the cache's oldest-first pruning. A sound you use is refreshed, so
  the ones you reach for stay.
- **Checked afterwards**: the file must be a known audio format and ffprobe must
  find audio of the expected length, otherwise it is deleted.
- **Clean failure**: a download that overruns is killed and its partial files
  removed; two requests for one video share a single download.
- **File-only for ffmpeg.** A layer only accepts a file inside the music cache;
  the protocol whitelist stays `file`.
- `POST /api/v1/soundboard/prepare` does the download without playing, so effects
  can be warmed before the game.

#### SoundCloud

YouTube refuses many datacenter addresses ("Sign in to confirm you're not a
bot"); SoundCloud does not. With `MUSIC_YTDLP_ENABLED=true` the bot also offers a
`soundcloud` source, using the same yt-dlp install and the same guards:

- **Search and play** like YouTube: `POST /api/v1/music/search` and
  `/api/v1/music/play` with `"source": "soundcloud"`. Search hits come back as
  `https://soundcloud.com/<artist>/<track>` links.
- **Ambience and effects** with `"source": "soundcloud"` on `/soundboard/play` and
  `/soundboard/prepare`, saved once under `DATA_DIR/music/soundcloud/` with the
  same length, size and disk limits as YouTube sounds.
- **Exact link only** for sounds: `https://soundcloud.com/<artist>/<track>`. No
  playlists (`/sets/`), profile pages, private-link tokens, query strings or other
  hosts. A link is rebuilt from the two names, lower-cased, and the file is named
  from a hash of them, so nothing from the link reaches the file system. A track
  that has since been renamed no longer matches and is refused.
- Only public tracks. Private ones, and tracks whose owner blocked streaming, fail
  with a normal resolver error.

### What the audio path refuses to do

Both music sources feed ffmpeg, which treats its input as a protocol
specification, not just a file name. So:

- ffmpeg is given an explicit **protocol whitelist**: a cached file may only be
  read with `file`, and a stream may only use http/https and TLS. `concat:`,
  `file://` from a stream, and the rest of ffmpeg's protocol list are
  unavailable.
- A URL a caller supplies is checked against a **host allowlist** before yt-dlp
  sees it, so it cannot be pointed at a cloud metadata endpoint.
- The URL yt-dlp returns is checked **again** before ffmpeg gets it: https, and
  every address the host resolves to must be public. An allowlisted page can
  still hand back an internal address; this is what stops it.
- Track titles are stripped of control characters and bidirectional overrides
  before they reach an API response.
- An R2 track id is only accepted if it appears in the live bucket listing —
  an allowlist rather than path arithmetic.

## The portal API

An HTTP API for a separate frontend project. Off unless `API_ENABLED=true`.

**It is not a second login for your friends.** One static bearer token grants
everything, including stopping a live recording, so treat it as admin access:

```
API_ENABLED=true
API_TOKEN=            # python -c "import secrets; print(secrets.token_urlsafe(32))"
API_PORT=8080
```

The container publishes the port to the host's loopback only. Put a reverse
proxy in front of it to terminate TLS — the token travels in a header, so it
must never cross an unencrypted hop:

```nginx
location /api/ {
    proxy_pass http://127.0.0.1:8080;
    proxy_set_header Host $host;
}
```

Rotating the token means editing `.env` and restarting; there is no revocation
list.

| Method | Path | What it does |
|---|---|---|
| GET | `/api/v1/health` | Liveness. The only route needing no token. |
| GET | `/api/v1/stats` | Live sessions, pending transcriptions, disk, storage, music. |
| GET | `/api/v1/sessions?limit=25&campaign=` | Finished sessions. `campaign` is a campaign id or `unassigned`. |
| POST | `/api/v1/sessions/{id}/campaign` | `{campaign_id}` (an id, or `null` to unassign). Finished sessions only. |
| POST | `/api/v1/sessions/{id}/update` | `{name}`: rename a session (1-100 characters). Not while it is recording. |
| GET | `/api/v1/sessions/trash` | Sessions in the trash, with when each is removed for good (`purge_at`). |
| POST | `/api/v1/sessions/{id}/trash` | `{confirm_id}` must equal the session id. Hides the session everywhere (list, search, queue, campaign counts) and keeps every file, so it can be restored. Refused while it is recording. |
| POST | `/api/v1/sessions/{id}/restore` | `{confirm_id}`. Takes a session out of the trash. |
| POST | `/api/v1/sessions/{id}/purge` | `{confirm_id}`. Only for a session already in the trash. Permanently removes the audio, transcript, export, staged and bucket copies, search entries and database rows. Refused (409) while the transcriber is working on it. The transcriber's own archive is not touched. Rate limited like recording calls. |
| GET | `/api/v1/transcripts/search?q=&campaign=&limit=` | Full-text search across delivered transcripts, as they are read (after the campaign's corrections). Words only: operators are ignored. Returns `results` and `still_indexing`. |
| GET | `/api/v1/transcription` | Sessions waiting for a transcript: `uploading` (still on the bot), `waiting` (in the bucket or with the transcriber) or `transcribing`, with `stalled` after 48 hours. |
| POST | `/api/v1/transcription/sync` | Runs one upload pass and one download pass now. Delivery stays with the bot's own loop. |
| GET | `/api/v1/initiative` | Totals players sent with `/init`: `{id, label, value, at}`, never a user id. |
| POST | `/api/v1/initiative/clear` | `{id}` drops one, `{}` drops all. |
| GET | `/api/v1/campaigns?archived=1` | Campaigns. |
| POST | `/api/v1/campaigns` | `{name, channel_id?, language?}` |
| GET | `/api/v1/campaigns/{id}` | Campaign with its names, corrections and characters (labels only, no user ids). |
| POST | `/api/v1/campaigns/{id}/update` | Any of `{name, channel_id, language, archived}`. |
| POST | `/api/v1/campaigns/{id}/terms` | `{terms: string[]}` replaces the list Whisper is primed with. |
| POST | `/api/v1/campaigns/{id}/corrections` | `{corrections: {heard, correct}[]}` replaces the fix list. |
| GET | `/api/v1/sessions/{id}/transcript?offset=0&limit=500` | One page of a delivered transcript: speaker labels, times and text (no user ids). 404 `no_transcript` until it arrives. |
| GET | `/api/v1/recording` | What is recording now. |
| POST | `/api/v1/recording/start` | `{channel_id, name?, text_channel_id?, campaign_id?}` |
| POST | `/api/v1/recording/stop` | `{channel_id}` |
| POST | `/api/v1/recording/cancel` | `{channel_id}` — **deletes the audio**. |
| POST | `/api/v1/recording/recover` | `{session_id}` |
| GET | `/api/v1/music/state` | Now playing, queue, volume, loop. |
| GET | `/api/v1/music/library?source=r2&q=` | Browse tracks. |
| POST | `/api/v1/music/search` | `{source, query}` — reaches an external source. |
| POST | `/api/v1/music/play` | `{source, id, channel_id?, position?}` |
| POST | `/api/v1/music/{pause,resume,skip,stop}` | Transport. |
| POST | `/api/v1/music/volume` | `{volume}` — 0 to 2. |
| POST | `/api/v1/music/seek` | `{position_seconds}` — restarts the current track from that offset; a paused track stays paused. |
| POST | `/api/v1/music/loop` | `{mode}` — off, track or queue. |
| DELETE | `/api/v1/music/queue` · `/queue/{i}` | Clear, or drop one track. |
| POST | `/api/v1/music/queue/move` | `{from, to}` |
| POST | `/api/v1/music/{join,leave}` | `{channel_id}` for join. |
| POST | `/api/v1/music/upload?folder=music\|ambience\|sfx&filename=` | Raw audio body (`audio/*`, `Content-Length` required, `MUSIC_UPLOAD_MAX_MB` cap). The name is sanitised, the bytes must match the extension and ffprobe must find audio; existing names are refused (409). One upload at a time. |
| POST | `/api/v1/music/delete` | `{id}` — removes a listed audio file under the music prefix. |
| GET | `/api/v1/soundboard` | Ambience (`music/ambience/`) and effects (`music/sfx/`), plus the layers playing now. |
| POST | `/api/v1/soundboard/play` | `{kind: ambience\|sfx, id, source?: r2\|youtube\|soundcloud, volume?, channel_id?}` — mixed over the music. Ambience loops (3 at most); effects play once (6 at most, oldest replaced). With `source: youtube` or `soundcloud` the `id` is an exact link and the sound is saved first (see above). |
| POST | `/api/v1/soundboard/prepare` | `{kind, id, source?: youtube\|soundcloud}` — Saves the sound without playing it and returns `{id, title, source, duration_seconds}`. |
| POST | `/api/v1/soundboard/stop` | `{layer_id}`, `{kind}` or `{}` for everything. |
| POST | `/api/v1/soundboard/volume` | `{layer_id, volume}` — 0 to 2. |
| POST | `/api/v1/dice/announce` | `{expression, total, breakdown, label?, channel_id?}` — posts a portal roll to `DICE_CHANNEL_ID` (or the named channel of this server), mentions disabled. |

Errors are always `{"error": {"code": ..., "message": ...}}`. 401 bad token,
404 missing, 409 state conflict (already recording, queue full), 413/415 bad
request shape, 429 rate limited, 502/503/504 a music source failed, is off, or
timed out.

Every response carries `X-Content-Type-Options: nosniff`, `Referrer-Policy:
no-referrer`, `Cache-Control: no-store` and `X-Frame-Options: DENY`, and the
server header does not name the stack. `/health` deliberately omits the version:
it is the one route served without a token.

Responses never carry Discord user ids: speakers appear as display labels and
counts. `API_CORS_ORIGINS` lists exact browser origins and refuses `*` — though
the recommended setup has the frontend's own server call this API, so that the
token never reaches browser JavaScript at all.

The container healthcheck still reads the heartbeat file, not this API: a bot
that records perfectly with a dead API should not be restarted for it.

## Development and tests

```bash
pip install -r requirements-dev.txt
pytest
ruff check .
black --check .
```

Tests mock Discord entirely; nothing touches the network. CI runs lint, format,
tests, and builds the image on every push.

## Troubleshooting

| Symptom | Likely cause |
| --- | --- |
| `Configuration error: Cannot write to /data/...` | The bind-mounted directory is not owned by uid 10001. Run `sudo chown -R 10001:10001 <dir>`. |
| Commands do not appear in Discord | Wrong `GUILD_ID`, or the bot was invited without the `applications.commands` scope. |
| "I do not have permission to connect" | Missing **Connect**/**View Channel** on that voice channel. |
| Transcript posted as a path instead of a file | Missing **Attach Files** in that text channel. |
| `/session stop` says nothing was handed over | No audio captured — check speakers were unmuted, and look for voice-connection warnings in the logs. |
| Sessions pile up in `outbox/` | Nobody has run `dndt session` on the transcriber. `/session status` lists what is waiting. |
| A transcript never appears | Check `inbox/<id>/` on this host: a `DELIVERY_FAILED` file there explains why. |
| Sessions listed as recoverable at startup | The bot crashed mid-session. Run `/session recover <id>`. |

## Known limitations

- Audio is flushed every ~5 seconds, not per packet. A hard crash can lose the
  last few seconds per speaker — not the whole session.
- If the disk fills *during* a session the write fails, is logged, and recording
  continues without capturing. The check at `/session start` prevents the common
  case; nothing catches it mid-game.
- One bot token records one voice channel at a time; Discord allows a single
  voice connection per account per server.
- If a voice connection drops and recovers mid-session, the reconnected audio is
  appended to the same track, so timestamps after the outage can drift by the
  length of the gap.
- Transcription depends on someone running the transcriber. Nothing here will
  chase them.
- A session finished while the network is down waits on disk until the next
  upload pass. It is not lost, but it is not in R2 either, so budget the disk.
- Music and recording share the guild's one voice connection. A voice reconnect
  mid-session restarts the current track rather than resuming it seamlessly, and
  music played into a channel is picked up by any player without headphones.
- The portal API has one shared token and no per-user identity; anyone holding it
  can stop a live recording. Discord OAuth is not implemented.
- Single guild, no live transcription, and no consent-announcement
  flow — deliberate non-goals. Access control now covers only the commands that
  reach into past sessions; starting a recording stays open by design.

## License

MIT — see [LICENSE](LICENSE).
