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
  with everything needed to transcribe them.
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
10. [Development and tests](#development-and-tests)
11. [Troubleshooting](#troubleshooting)
12. [Known limitations](#known-limitations)

---

## How a session flows

```
/session start   -> bot joins your voice channel, records each speaker
                    to /data/sessions/<id>/audio/raw/<user_id>.pcm
  (during play)     packets are appended and fsync-ed every ~5s
/session stop    -> PCM is encoded to Opus and moved to /data/outbox/<id>/
                    with metadata.json, then marked READY
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

Anyone in the relevant voice channel can run these — there is no role gating.

| Command | What it does |
| --- | --- |
| `/session start [name]` | Joins your current voice channel and starts recording. |
| `/session stop` | Stops the session, encodes the audio and stages it for the transcriber. |
| `/session status` | The active session, or what is waiting for a transcript. |
| `/session cancel` | Stops and **discards** the session, deleting its audio. |
| `/session list` | Recent completed sessions with id, name, channel, date, duration. |
| `/session transcript <id>` | Re-posts a past session's transcript. |
| `/session recover <id>` | Finalizes and stages a session left open by a crash. |
| `/session export <id>` | Zips a session's transcript and any audio still here. |
| `/character set <user> <name>` | Maps a Discord user to a character name. |
| `/character clear <user>` | Removes a mapping. |
| `/character list` | Shows all mappings. |

**Speaker labels** resolve as character name (`/character set`) → server nickname
→ username, and are frozen into `metadata.json` when the session is staged. That
is what lets the transcriber work with no database and no Discord access — and
why running `/character set` for every player before your first session is the
single biggest thing you can do for transcript quality.

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
| `DISK_WARNING_THRESHOLD_MB` | `5000` | Warn below this much free space. |
| `ADMIN_USER_ID` | — | Fallback DM recipient for warnings and fatal errors. |
| `EXPORT_MAX_DISCORD_UPLOAD_MB` | `25` | Larger exports report a path instead of uploading. |
| `TIMEZONE` | `Europe/Istanbul` | Transcript timestamps and the backup schedule. |
| `DB_BACKUP_KEEP_DAYS` | `14` | Daily `bot.db` copies kept in `/data/backups`. |
| `LOG_LEVEL` | `INFO` | Everything logs to stdout. |

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

Staged audio is deleted as soon as the transcriber confirms it has a copy, so
the steady-state footprint is small. **During play, though, the raw PCM capture
is full size** — budget ~0.7 GB per speaker-hour of free space, released when
the session is finalized.

A 40 GB VPS with 1 GB RAM is comfortable.

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
- `stop_grace_period: 60s` — shutdown finalizes in-progress recordings first.
  Docker's 10 s default would `SIGKILL` that halfway through and lose audio.
- `init: true` — reaps ffmpeg subprocesses and forwards signals cleanly.
- `mem_limit: 1g`, `cpus: 1.0` — this half only writes packets to disk.
- Log rotation capped at 3 × 10 MB.

### Connecting the transcriber

The transcriber pulls over SSH, so it needs a user on this host that can read
`<DATA_HOST_DIR>/outbox` and write `<DATA_HOST_DIR>/inbox`. The simplest
arrangement is to add your login user to the group owning that directory, then
add the transcriber machine's public key to `~/.ssh/authorized_keys`.

Nothing needs to be exposed to the internet: the transcriber always initiates.

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
- One bot token records one voice channel at a time; Discord allows a single
  voice connection per account per server.
- If a voice connection drops and recovers mid-session, the reconnected audio is
  appended to the same track, so timestamps after the outage can drift by the
  length of the gap.
- Transcription depends on someone running the transcriber. Nothing here will
  chase them.
- Single guild, no web UI, no live transcription, no consent-announcement flow,
  no role-based access control — all deliberate non-goals.

## License

MIT — see [LICENSE](LICENSE).
