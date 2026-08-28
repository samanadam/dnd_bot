# D&D Session Recorder & Transcriber

A self-hosted Discord bot that records D&D sessions per speaker in a voice
channel and transcribes them locally with Whisper. Fully offline after the first model download — no cloud APIs, no
audio ever leaves the host.

Audio is expected to be mostly Turkish with occasional English names and
sentences; the transcript language is fixed to Turkish (`tr`), which handles
that code-switching well enough without special-casing.

- **Recording:** per-speaker, written to disk incrementally as packets arrive.
- **Transcription:** `faster-whisper`, deferred to a nightly quiet-hours window,
  strictly one job at a time.
- **State:** SQLite (WAL) for metadata, filesystem for audio.
- **Bot UI language:** English. Only the transcript is Turkish.

---

## Table of contents

1. [How a session flows](#how-a-session-flows)
2. [Commands](#commands)
3. [Setup: creating the Discord bot](#setup-creating-the-discord-bot)
4. [Configuration](#configuration)
5. [Running it](#running-it)
6. [Getting good Turkish transcripts](#getting-good-turkish-transcripts)
7. [Production deployment](#production-deployment)
8. [Your first session](#your-first-session)
9. [Data layout](#data-layout)
10. [Retention, exports and backups](#retention-exports-and-backups)
11. [Tuning resource limits](#tuning-resource-limits)
12. [Development and tests](#development-and-tests)
13. [Troubleshooting](#troubleshooting)
14. [Known limitations](#known-limitations)

---

## How a session flows

```
/session start            -> bot joins your voice channel, records each speaker
                             to /data/sessions/<id>/audio/raw/<user_id>.pcm
   (during play)             packets are appended and fsync-ed every ~5s
/session stop             -> PCM is wrapped into .wav, the job is INSERTed into
                             transcription_queue with status 'pending'
   (quiet hours, 00:00-08:00 by default)
                          -> worker picks up ONE job, posts "starting now",
                             runs Whisper per speaker, merges the timelines
                          -> transcript.md is posted to the channel the command
                             came from, with duration/speaker/word counts
   (+7 days)              -> raw audio is deleted; transcripts are kept forever
```

Transcription is **deliberately not immediate**. On CPU with the `medium` model
it is slow, and a self-hosted box is typically shared with other services — so
the heavy step is pushed into the night. Expect a transcript the next morning, not right after
`/session stop`.

### On recording two channels at once

All session state is keyed by `(guild_id, channel_id)` — there is no global
"current session" anywhere in the code, and the storage, queue and auto-stop
logic all handle several concurrent sessions.

**Discord itself is the ceiling here:** an account can only be connected to one
voice channel per server, so one bot token can only record one channel at a time.
A second `/session start` in another channel gets an explicit error naming the
session that is already running. To genuinely record a side table in parallel,
run a second instance of this bot with its own bot token (its own application in
the Developer Portal) and its own `DATA_DIR`; the per-channel design means the
two never collide.

The transcription queue drains one job at a time regardless of how many sessions
were recorded.

## Commands

Anyone in the relevant voice channel can run these — there is no role gating.

| Command | What it does |
| --- | --- |
| `/session start [name]` | Joins your current voice channel and starts recording it. Rejects a second session while one is already running (see the note on concurrent channels). |
| `/session stop` | Stops your channel's session, finalizes audio, and queues transcription for the next quiet-hours window. |
| `/session status` | Your channel's session if there is one; otherwise every active session in the server, plus the queue depth. |
| `/session cancel` | Stops and **discards** your channel's session, deleting its partial audio. |
| `/session list` | Recent completed sessions with id, name, channel, date, duration. |
| `/session transcript <id>` | Re-posts a past session's `transcript.md`. |
| `/session recover <id>` | Finalizes and queues a session left open by a crash. Recoverable sessions are logged at startup. |
| `/session export <id>` | Zips that session's audio + transcripts to `/data/exports/<id>.zip` and uploads it if it is under `EXPORT_MAX_DISCORD_UPLOAD_MB`, otherwise replies with the server path. |
| `/character set <user> <name>` | Maps a Discord user to a character name used in future transcripts. |
| `/character clear <user>` | Removes a mapping. |
| `/character list` | Shows all mappings. |

**Speaker labels** resolve in this order: character name (`/character set`) →
Discord server nickname → Discord username. They are resolved and stored *per
session*, so re-naming a character later does not rewrite old transcripts.

## Setup: creating the Discord bot

1. Go to the [Discord Developer Portal](https://discord.com/developers/applications)
   and click **New Application**.
2. **Bot** tab → **Reset Token** → copy the token. This is `DISCORD_TOKEN`.
   **Never commit it.** It is a full credential for your bot; anyone with it can
   read and post as the bot. If it leaks, reset it immediately.
3. Still on the **Bot** tab, enable **Server Members Intent**. (Message Content
   is not needed.)
4. **OAuth2 → URL Generator**:
   - Scopes: `bot`, `applications.commands`
   - Bot permissions: **View Channel**, **Connect**, **Send Messages**,
     **Attach Files**
5. Open the generated URL and add the bot to your server. The resulting URL
   looks like:

   ```
   https://discord.com/api/oauth2/authorize?client_id=<APPLICATION_ID>&permissions=274881105920&scope=bot%20applications.commands
   ```

6. Enable Developer Mode in Discord (User Settings → Advanced), then right-click
   your server → **Copy Server ID**. That is `GUILD_ID`.
7. Right-click yourself → **Copy User ID** for `ADMIN_USER_ID` (used as the
   fallback DM target for disk warnings and fatal startup errors).

Commands are registered to that single guild, so they appear within seconds of
the bot starting rather than after Discord's global propagation delay.

## Configuration

Copy `.env.example` to `.env` and fill it in. Every setting is read from the
environment; nothing is hardcoded.

| Variable | Default | Notes |
| --- | --- | --- |
| `DISCORD_TOKEN` | — | **Required.** Never commit it. |
| `GUILD_ID` | — | **Required.** The single server this bot serves. |
| `WHISPER_MODEL` | `medium` | Better Turkish accuracy than `small`, at a real speed/memory cost. |
| `WHISPER_DEVICE` | `cpu` | `cuda` if you pass a GPU through. |
| `WHISPER_COMPUTE_TYPE` | `int8` | Use `float16` on CUDA. |
| `TRANSCRIBE_LANGUAGE` | `tr` | Fixed; there is no per-session override. |
| `WHISPER_BEAM_SIZE` | `5` | Wider is slightly better and slower. |
| `WHISPER_CONDITION_ON_PREVIOUS_TEXT` | `false` | Keep it off; see the Turkish section. |
| `WHISPER_VAD_MIN_SILENCE_MS` | `500` | Raise to ~800 if short words get clipped. |
| `WHISPER_PROMPT_EXTRA` | — | Extra campaign vocabulary for the Whisper prompt. |
| `FILTER_HALLUCINATIONS` | `true` | Strip subtitle boilerplate and stuck repeats. |
| `TRANSCRIBE_CHUNK_MINUTES` | `10` | Memory ceiling for transcription; see below. |
| `DATA_DIR` | `/data` | Everything persistent lives here. |
| `AUDIO_FORMAT` | `wav` | Or `mp3` (smaller, transcoded with ffmpeg). |
| `AUDIO_RETENTION_DAYS` | `7` | Days after transcription before raw audio is deleted. |
| `DISK_WARNING_THRESHOLD_MB` | `2000` | Below this, a warning is sent to the channel and by DM. |
| `ADMIN_USER_ID` | — | Fallback DM recipient. |
| `EXPORT_MAX_DISCORD_UPLOAD_MB` | `25` | Larger exports are reported as a path instead of uploaded. |
| `QUIET_HOURS_ENABLED` | `true` | Set `false` to transcribe as soon as a job is queued. |
| `QUIET_HOURS_START` / `QUIET_HOURS_END` | `00:00` / `08:00` | Windows may wrap past midnight (e.g. `23:00`–`06:00`). |
| `TIMEZONE` | `Europe/Istanbul` | Used for quiet hours and transcript timestamps. |
| `DB_BACKUP_KEEP_DAYS` | `14` | Daily `bot.db` copies kept in `/data/backups`. |
| `LOG_LEVEL` | `INFO` | Everything logs to stdout. |

## Running it

```bash
cp .env.example .env
$EDITOR .env            # at minimum DISCORD_TOKEN and GUILD_ID
docker compose up -d --build
docker compose logs -f
```

> **First start needs internet access.** The `medium` model (~1.5 GB) is
> downloaded into the `whisper-models` volume on the first run. Every restart
> after that is fully offline, as long as that volume survives. The bot loads
> the model *before* declaring itself ready — if the model cannot be loaded it
> logs a fatal error, DMs `ADMIN_USER_ID`, and exits rather than sitting there
> looking healthy with transcription quietly broken.

Useful operations:

```bash
docker compose logs -f dnd-bot      # follow logs
docker compose restart dnd-bot      # in-flight sessions are finalized first
docker compose down                 # SIGTERM: audio is flushed before exit
docker compose ps                   # HEALTHCHECK status (heartbeat freshness)
```

Running without Docker (development):

```bash
python -m venv .venv && . .venv/bin/activate
pip install -r requirements-dev.txt
DATA_DIR=./data python -m dnd_bot
```

## Getting good Turkish transcripts

Four things do the heavy lifting, all on by default:

1. **Character names are fed to Whisper as a prompt.** Invented names are what a
   transcript gets most wrong — Whisper snaps them to whatever real Turkish word
   sounds closest. Every name from `/character set`, plus the session's speaker
   labels, goes into the decode prompt. **Run `/character set` for everyone
   before your first session** — it is the single biggest accuracy win, and it
   costs nothing.
   Add campaign vocabulary (places, NPCs, items) via `WHISPER_PROMPT_EXTRA`.
2. **`condition_on_previous_text` is off.** Each speaker gets their own track, so
   most of any one file is silence while others talk. Carrying decode context
   across those gaps is exactly what sends Whisper into "evet evet evet evet"
   repetition loops. Leave it `false`.
3. **Hallucination filtering.** On silent stretches Whisper emits subtitle
   boilerplate it learned from training data — `Altyazı M.K.`,
   `Abone olmayı unutmayın`, `Thanks for watching`. Those are dropped, along
   with segments that are both low-confidence *and* probably silence, and runs
   of the same line repeated more than three times.
4. **Temperature fallback + VAD.** A segment that decodes badly is retried at a
   higher temperature instead of being emitted as garbage, and voice-activity
   detection trims the dead air first.

Beyond that:

- **`medium` is the right model for CPU.** `large-v3` is meaningfully better at
  Turkish but far too slow on this hardware; `small` is noticeably worse at
  Turkish morphology. If transcripts come out mangled, check microphone quality
  before changing models — Whisper is far more sensitive to a bad mic and room
  echo than to model size.
- **Discord's per-speaker tracks are already the best case here.** No speaker
  diarization is needed or attempted; each person's audio is separate, which is
  why attribution is exact even when people talk over each other.
- English words and names inside Turkish speech are handled by the fixed `tr`
  hint — no code-switching logic is needed.

## Production deployment

### Sizing the data directory first

Discord delivers 48 kHz stereo audio, and every speaker gets their own track, so
raw session audio is bulky:

| session length | 3 players | 5 players | 7 players |
| --- | --- | --- | --- |
| 2 h | 4.1 GB | 6.9 GB | 9.7 GB |
| 3 h | 6.2 GB | 10.4 GB | 14.5 GB |
| 4 h | 8.3 GB | 13.8 GB | 19.4 GB |

That is ~0.7 GB per hour **per speaker**, kept for `AUDIO_RETENTION_DAYS` (7 by
default) after transcription. Transcripts are tiny and kept forever; it is the
audio that needs room. Point `DATA_HOST_DIR` at a disk that has it — the code
can live anywhere, the data should not follow it by accident.

### First run

The image runs as an unprivileged user (uid 10001), so the data directory must
be writable by that user. Docker creates a bind-mounted directory owned by root,
so **do this once before the first start** or the bot exits immediately with a
`Configuration error` naming this exact fix:

```bash
git clone https://github.com/samanadam/dnd_bot.git /opt/dnd-bot
cd /opt/dnd-bot

cp .env.example .env
$EDITOR .env      # DISCORD_TOKEN, GUILD_ID, ADMIN_USER_ID, DATA_HOST_DIR

# Whatever DATA_HOST_DIR points at:
sudo mkdir -p /srv/dnd-bot-data
sudo chown -R 10001:10001 /srv/dnd-bot-data

docker compose up -d --build
docker compose logs -f
```

Wait for `Whisper model medium ready`, then `Bot ready`. The first start
downloads ~1.5 GB of model and needs internet; afterwards it runs fully offline
as long as the `whisper-models` volume survives.

What the compose file does for you, and why:

- `restart: unless-stopped` — survives host reboots.
- `stop_grace_period: 60s` — shutdown finalizes every in-progress recording to
  disk first. Docker's 10 s default would `SIGKILL` that halfway through and
  lose the tail of a live session.
- `init: true` — reaps ffmpeg subprocesses and forwards signals cleanly.
- `mem_limit` / `cpus` / `cpu_shares` — see [tuning](#tuning-resource-limits).
- `whisper-models` named volume — keep it, or the ~1.5 GB model is downloaded
  again on every recreate.
- log rotation capped at 3 × 10 MB.

Set `TZ` in `.env` if you want container log timestamps in local time; transcript
timestamps always follow `TIMEZONE` regardless.

### Hosting on a laptop

A spare laptop makes a fine host — the battery is a free UPS — but three of its
defaults will break this bot specifically, because the heavy work happens at
night with nobody watching.

**1. It must not sleep.** Closing the lid suspends the machine, which kills a
live recording and stops the nightly transcription queue dead.

```bash
sudo tee /etc/systemd/logind.conf.d/no-suspend.conf >/dev/null <<'EOF'
[Login]
HandleLidSwitch=ignore
HandleLidSwitchExternalPower=ignore
HandleLidSwitchDocked=ignore
EOF
sudo systemctl restart systemd-logind

sudo systemctl mask sleep.target suspend.target hibernate.target hybrid-sleep.target
```

**2. Automatic reboots collide with the transcription window.** Ubuntu's
`unattended-upgrades` reboots at 02:00 by default — the middle of quiet hours.
A restart mid-job is survivable (the queue is persisted and the job re-runs),
but it wastes the whole night's work. Move it outside your window:

```bash
sudo sed -i 's|^//\s*Unattended-Upgrade::Automatic-Reboot-Time.*|Unattended-Upgrade::Automatic-Reboot-Time "10:00";|'   /etc/apt/apt.conf.d/50unattended-upgrades
```

**3. Heat.** Transcription pins the CPU for hours at a time, and a closed laptop
on a desk has poor airflow. Prop the lid open or raise the chassis, and watch
the first long job:

```bash
sudo apt install -y lm-sensors && sensors        # during a transcription run
```

If it throttles, lower `cpus:` in `docker-compose.yml` or switch to
`WHISPER_MODEL=small`. Sustained thermal throttling costs you more time than a
smaller model would.

Also worth doing:

- **Use ethernet if you can.** Voice receive over Wi-Fi drops more often. The
  bot reconnects, but each drop leaves a gap in that speaker's timeline. If
  Wi-Fi is unavoidable, disable power saving:
  `echo -e "[connection]
wifi.powersave = 2" | sudo tee /etc/NetworkManager/conf.d/wifi-powersave.conf`
- **Install Docker from Docker's own repo, not snap.** The snap package is
  confined and handles bind mounts poorly.
- **Enable Docker at boot:** `sudo systemctl enable --now docker`.
- **Check the disk before a long session:** `df -h`. Laptop SSDs are often
  small, and one session can be 14 GB.
- No inbound ports are needed. The bot only makes outbound connections to
  Discord, so nothing has to be forwarded or exposed.

### Upgrading

```bash
git pull
docker compose up -d --build
```

Migrations run automatically on start, and are idempotent. In-progress
recordings are finalized before the old container exits, and queued
transcription jobs survive the restart.

### Backups and restore

`bot.db` is copied to `/data/backups/bot-<date>.db` daily at 05:00 local time,
keeping `DB_BACKUP_KEEP_DAYS` of history. That covers metadata only — session
names, character mappings, the queue. **Transcripts and audio are plain files
under `/data/sessions/`; back up that directory separately** if it matters to
you.

To restore the database:

```bash
docker compose down
cp data/backups/bot-2026-05-01.db data/bot.db
sudo chown 10001:10001 data/bot.db
docker compose up -d
```

The restored database and the files on disk are independent, so a database
restored from yesterday still finds today's audio — it just will not know about
sessions created after the backup. Their audio is still on disk under
`/data/sessions/<id>/`.

## Your first session

A checklist for one real session, start to finish.

**Before play**

1. `docker compose up -d`, then `docker compose logs -f` and wait for
   `Whisper model medium ready` followed by `Bot ready`. On the very first run
   this includes a ~1.5 GB model download.
2. In Discord, run `/character set` once per player. This is what makes names
   come out right — do not skip it.
3. Optional but worth it: put your campaign's places and NPCs in
   `WHISPER_PROMPT_EXTRA` in `.env`, then `docker compose up -d` again.
4. For the *first* session only, set `QUIET_HOURS_ENABLED=false` so the
   transcript arrives right after you stop instead of the next morning. Turn it
   back on once you trust the setup.
5. Do a 60-second dry run: join voice, `/session start test`, everyone says a
   sentence, `/session stop`. Confirm a `transcript.md` comes back and the names
   look right. Then `/session cancel` is not needed — just ignore the test
   session, or delete its folder under `data/sessions/`.

**During play**

6. Everyone joins the voice channel, then any one of you runs `/session start`
   with a name. `/session status` shows who has been heard so far.
7. Play. If everyone leaves the channel the session auto-stops after ~45 s.

**After play**

8. `/session stop`. The reply confirms duration and speaker count.
9. With quiet hours off, the worker starts within a few minutes and posts
   `transcript.md` when done. With quiet hours on, it lands overnight.
10. `/session export <id>` if you want the audio archived — raw audio is deleted
    7 days after transcription.

**If something goes wrong:** the bot crashing does not lose the session. Restart
it, look for `Recoverable session <id>` in the logs, and run
`/session recover <id>`.

## Data layout

```
/data/
  sessions/
    <session_id>/
      audio/
        raw/<user_id>.pcm      # appended live; removed once finalized
        <user_id>.wav          # finalized at stop/recover
      transcript.md
      transcript.json
  exports/<session_id>.zip     # from /session export; never auto-deleted
  backups/bot-<date>.db        # nightly database copies
  bot.db                       # SQLite metadata (WAL mode)
  heartbeat                    # touched every 30s; read by the healthcheck
```

Schema changes go in `migrations/NNN_description.sql`. A `schema_version` table
records what has been applied; the runner applies anything newer on startup and
is safe to run repeatedly.

## Retention, exports and backups

- After a **successful** transcription, `audio_expires_at` is set to now + 7
  days. A cleanup pass (on startup, then every few hours) deletes the `audio/`
  directory of any session past that point.
- **Transcripts, database rows and `/data/exports/` are never touched by
  cleanup.** If you want to keep the audio itself, run `/session export <id>`
  before the window closes.
- A failed transcription does *not* set an expiry, so the audio stays until you
  retry with `/session recover <id>`.
- The same periodic pass checks free disk space and warns (channel + DM) below
  `DISK_WARNING_THRESHOLD_MB`.
- Once a day at 05:00 local time, `bot.db` is copied to
  `/data/backups/bot-<date>.db` using SQLite's online backup API, and copies
  older than `DB_BACKUP_KEEP_DAYS` are pruned. This is a cheap safety net for
  metadata — it is **not** a substitute for a host-level backup of `/data`.

## Tuning resource limits

`docker-compose.yml` ships with `mem_limit: 4g`, `cpus: 2.0` and
`cpu_shares: 512`. Those are a **starting point for a small shared
box, not a tuned answer.** Watch a real run:

```bash
docker stats dnd-bot          # during a quiet-hours transcription
docker compose logs dnd-bot | grep -i "Transcribing session"
```

- If the container is OOM-killed mid-job (`docker inspect dnd-bot | grep OOM`),
  raise `mem_limit` or drop to `WHISPER_MODEL=small`.
- If the rest of the box stalls at night, lower `cpus` and `cpu_shares`.
### Memory: why tracks are transcribed in chunks

faster-whisper loads whatever file you hand it **entirely into memory** before
it starts streaming segments out — decoded waveform, VAD-trimmed copy, and the
full mel spectrogram. Measured on this codebase:

| speaker track | peak RAM above the model |
| --- | --- |
| 3 min | 155 MB |
| 10 min | 308 MB |
| 20 min | 661 MB |

That is ~33 MB per minute, linear, so an unchunked four-hour session would need
roughly **8 GB for a single speaker** — an instant OOM kill against the
default `mem_limit: 4g`, and out of reach for a small host entirely.

So each speaker track is split into `TRANSCRIBE_CHUNK_MINUTES` pieces (default
10) before transcription, and each chunk is deleted as soon as it is done.
Measured on the same 20-minute track:

| mode | peak RAM above the model | wall clock |
| --- | --- | --- |
| unchunked | 694 MB | 113 s |
| 5-min chunks | 216 MB | 113 s |
| 2-min chunks | 158 MB | 113 s |

Peak memory stops depending on session length, at no cost in speed. Chunk
boundaries are snapped to the quietest moment near the target time so a cut does
not land mid-word, and segment timestamps are re-based onto the full session
timeline afterwards, so the transcript reads as one continuous session.

Chunking also sidesteps Whisper's long-form failure mode, where it can skip
ahead after a bad stretch and silently drop minutes of audio.

If a track cannot be split — `AUDIO_FORMAT=mp3` produces files Python's `wave`
module cannot read — it is transcribed whole with a warning in the logs. Keep
`AUDIO_FORMAT=wav` for long sessions.

### How long transcription actually takes

Voice-activity detection strips the silence first, so decode time tracks *speech*
rather than wall-clock session length — a four-hour session where people mostly
take turns is roughly four hours of speech in total, not four hours per speaker.
On CPU with `medium`, expect that to decode somewhere in the region of
**2–5× slower than realtime** on this class of hardware, i.e. a long session can
take most of a night. Measure your own box before trusting any number: time the
gap between `Transcribing session` and `Session ... transcribed` in the logs for
a session of known length.

If it does not fit:

- `WHISPER_MODEL=small` is roughly 2–3× faster, at a real cost in Turkish
  morphology. `WHISPER_BEAM_SIZE=1` buys a bit more speed for a bit more error.
- Widen the quiet-hours window. The window is checked *before* a job starts, not
  during it — a job already running keeps going past the window rather than
  being killed halfway. Only the *next* job waits.
- Pending jobs survive restarts, so a queue that does not drain tonight drains
  tomorrow night.

## Development and tests

```bash
pip install -r requirements-dev.txt
pytest                  # unit tests, no Discord connection required
ruff check .
black --check .
```

Or against the image, with the source mounted (the image ships without tests, to
stay small):

```bash
docker compose run --rm --entrypoint "" -v "$PWD:/src" -w /src dnd-bot \
  sh -c "pip install -r requirements-dev.txt && pytest -q"
```

Tests cover transcript merging across speakers and offsets (including
overlapping speech), speaker-label priority, crash-recovery detection, retention
cleanup, the quiet-hours queue worker (window gating, strict serialization,
failure reporting), migration idempotency, audio finalization, exports and
backup pruning. Discord and the Whisper model are faked; nothing touches the
network.

CI (`.github/workflows/ci.yml`) runs `ruff check .`, `black --check .` and
`pytest` on every push and pull request.

## Troubleshooting

| Symptom | Likely cause |
| --- | --- |
| `Configuration error: Cannot write to /data/...` | The bind-mounted `data/` is not owned by uid 10001. Run `sudo chown -R 10001:10001 data`. |
| Container marked `unhealthy` on the very first run | The model download outran the healthcheck start period. The heartbeat starts before the download, so this should not happen; check `docker compose logs` for a real error. |
| Bot exits at startup with a fatal model error | Not enough RAM/disk for `medium`, a bad `WHISPER_MODEL`, or no internet on the very first run. |
| Commands do not appear in Discord | Wrong `GUILD_ID`, or the bot was invited without the `applications.commands` scope. |
| "I do not have permission to connect" | Missing **Connect**/**View Channel** on that voice channel. |
| Transcript posted as a path instead of a file | Missing **Attach Files** in that text channel. |
| `/session stop` says nothing was queued | No audio was captured — check that speakers were actually unmuted, and look for voice-connection warnings in the logs. |
| Nothing transcribes overnight | Check `TIMEZONE` and the quiet-hours window; `/session status` reports the queue depth. |
| Sessions listed as recoverable at startup | The bot crashed mid-session. Run `/session recover <id>`. |

## Known limitations

- Audio is flushed every ~5 seconds, not per packet. A hard crash can lose the
  last few seconds per speaker — not the whole session.
- Transcription with `medium` on CPU is slow, and it is deferred to quiet hours
  on top of that. Same-session transcripts are typically ready **the next
  morning**. This is intentional, not a bug.
- Raw audio is deleted after `AUDIO_RETENTION_DAYS`. Use `/session export`
  before then if you want a permanent copy of the audio itself.
- If the voice connection drops and is restored mid-session, the reconnected
  audio is appended to the same per-speaker file, so the silent gap is not
  represented — timestamps after a reconnect can drift by the length of the
  outage.
- Resource limits in `docker-compose.yml` need tuning against real observed
  usage on your specific host.
- One bot token can only record one voice channel at a time, because Discord
  allows a single voice connection per account per server. The code is written
  per channel, so a second bot token running the same image lifts this.
- Single guild only, no web UI, no live transcription, no consent-announcement
  flow, no role-based access control — all deliberate non-goals.
