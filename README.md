# Soma.fm

Soma.fm playlists in various formats

SomaFM is an independent Internet-only streaming group of radio channels, supported entirely with donations from listeners. SomaFM originally started broadcasting out of founder Rusty Hodge's basement garage in the Bernal Heights neighborhood of San Francisco, as a micropower radio station broadcast at the Burning Man festival in 1999. The response to the project was sufficiently positive that Rusty Hodge launched it as a full-time internet radio station in February 2000.

This is an unofficial repository. The playlists only point to SomaFM's own streams. If you enjoy the music, please [support SomaFM](https://somafm.com/support/).

## Playlists

All channels in one playlist, in the `Playlists/` folder:

| File | Format | Notes |
|---|---|---|
| `SomaFM.m3u` | M3U | ASCII titles, CRLF line endings |
| `SomaFM.m3u8` | M3U8 | UTF-8 (no BOM) |
| `SomaFM.pls` | PLS | ASCII titles |
| `SomaFM.aimppl4` | AIMP | native AIMP playlist, UTF-16 |

Streams are MP3 over HTTPS from `ice1.somafm.com`, one entry per channel.

## Keeping the playlists up to date

`generate.py` rebuilds all four files from SomaFM's official channel list
(`https://api.somafm.com/channels.json`). It needs only Python 3.9+ and no packages.

```sh
python3 generate.py                # fetch the list and rewrite Playlists/
python3 generate.py --dry-run      # show what would change, write nothing
python3 generate.py --check        # also test every stream URL (advisory)
python3 generate.py --input FILE   # use a saved channels.json instead of the API
python3 generate.py --notes-file notes.md   # if files changed, also write release notes
```

The script prints what changed compared with the previous playlist: channels added,
removed or renamed, bitrate changes and changed stream addresses. It refuses to write anything
(and exits with an error) if the data looks wrong: too few channels, no MP3 streams,
or a sudden drop in the channel count. Existing playlists are then left untouched.

### Options (`config.json`)

| Option | Default | Meaning |
|---|---|---|
| `output_dir` | `"Playlists"` | where the files go (relative to `config.json`) |
| `basename` | `"SomaFM"` | file name without extension |
| `scheme`, `host` | `"https"`, `"ice1.somafm.com"` | stream URL prefix; use `"http"` for old players without modern TLS |
| `default_bitrate` | `"max"` | MP3 bitrate for every channel: a number in kbps (`128`, `256`, `320`) or `"max"` for the best one each channel offers |
| `bitrate_overrides` | `{}` | per-channel bitrate by channel id, same values as above, e.g. `{"deepspaceone": 128}` |
| `exclude` | `[]` | channel ids to leave out, e.g. `["scanner", "sfinsf"]` |
| `exclude_genres` | `[]` | genre tags to leave out, e.g. `["holiday", "spoken"]` |
| `order` | `"keep"` | see below |
| `min_channels` | `20` | safety limit: fewer usable channels than this aborts the run |

If a requested number is not offered for a channel, the closest lower one is used.
`"max"` means 320 kbps where offered, otherwise 256, otherwise 128. Higher bitrates
use more data (320 kbps is roughly 140 MB per hour, 128 kbps roughly 55 MB).

`order`:

- `keep`: keep the order of the existing playlist, append new channels at the end (most
  popular first). Nothing is reshuffled when listener counts change, so the automatic
  commits only happen when the channel list really changes. On the very first run it
  behaves like `listeners`.
- `listeners`: most listeners first, as on the site's "Channels by Popularity" page.
  The order changes constantly, so expect frequent commits.
- `alpha`: alphabetical by title.
- `api`: the order returned by the API.

Genre tags are the ones shown on the SomaFM site (for example `holiday`, `spoken`,
`news`, `live`, `specials`). Beware that some music channels carry `news` or `live`
as one of several tags.

### GitHub Actions

`.github/workflows/update-playlists.yml` runs the tests, regenerates the playlists,
commits them if anything changed and then publishes a release. It runs every Monday at
21:00 (UTC+3), which is 18:00 UTC in the cron expression, and can be started by hand:
**Actions → Update playlists → Run workflow**. GitHub may start scheduled runs a few
minutes late.

Each release is tagged `vYYYY.MM.DD.N` (N is the workflow run number), carries the four
playlist files as downloads, and its description lists what changed: added, removed and
renamed channels, bitrate changes and changed stream addresses. A release is created only
after all previous steps succeeded and only when the playlists actually changed.

Stream problems show up as warnings in the run log. A failed run means a test failed or
the generator refused to write bad data (you get GitHub's usual failure e-mail); the
existing playlists stay as they were.

Things to know:

- GitHub pauses scheduled workflows in public repositories after 60 days without
  repository activity. If the schedule stops, re-enable it in the Actions tab.
- If you change `output_dir`, change `git add Playlists` and `Playlists/*` in the workflow
  as well.
- If `.gitattributes` converts line endings (`* text=auto`), add `Playlists/* -text`
  so the files are stored exactly as generated.

### Tests

```sh
python3 -m unittest discover -s tests -v
```

The tests run offline.
