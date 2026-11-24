# mpd-cd-cue

Seamless Audio CD playback in **MPD (Music Player Daemon)** on Linux.

Insert an Audio CD into your optical drive, and `mpd-cd-cue` will automatically detect the disc via `udev`, read track metadata from **CD-Text** or **MusicBrainz** (with local SQLite caching), generate a CUE sheet pointing to `cdda:///` URIs, and load the album directly into your MPD queue.

## Features

- **Zero-touch udev automation:** Automatically triggers on disc insertion (`DISK_MEDIA_CHANGE=1`) via `systemd-run`.
- **Smart Metadata Lookup:**
  1. Reads embedded **CD-Text** first via `cdda2wav` for instant offline tagging.
  2. Falls back to **MusicBrainz** via DiscID (`python-discid`) if CD-Text is absent.
  3. Caches MusicBrainz results in a local **SQLite** database so repeat plays work offline and instantaneously.
  4. Preserves full Unicode/Cyrillic characters without stripping diacritics or breaking characters like `й` / `ё`.
- **Native MPD Integration:** Generates a clean CUE file with accurate track durations and loads it via `mpc`.

## Requirements

- **MPD** compiled with `libcdio_paranoia` support
- **mpc** (MPD command-line client)
- **libcdio-paranoia** (`cdparanoia` binary for TOC detection)
- **cdrtools** (`cdda2wav` binary for CD-Text extraction)
- **Python 3** & **python-discid**

### Arch Linux Installation

```bash
git clone https://github.com/UncleZI/mpd-cd-cue
cd mpd-cd-cue
sudo ./install.sh
```

The interactive installer will install all required packages via `pacman`, prompt for your MPD paths/user, generate `/etc/mpdcd.conf`, and install the `udev` rule.

## MPD Configuration

Make sure the `cdio_paranoia` input plugin is enabled in `/etc/mpd.conf`:

```conf
input {
    plugin "cdio_paranoia"
}
```

Also ensure the `mpd` user has permission to read the optical drive (usually via the `optical` group):

```bash
sudo usermod -aG optical mpd
sudo systemctl restart mpd
```

## Configuration (`/etc/mpdcd.conf`)

```ini
[general]
device = /dev/sr0
playlist_dir = /var/lib/mpd/music/CD
playlist_name = Audio-CD.cue
owner = mpd:mpd
cache_db = /var/cache/mpdcdtag/mb_cache.db

[mpd]
host = localhost
port = 6600
mpd_playlist_path = CD/Audio-CD.cue

[musicbrainz]
user_agent = mpd-cd-cue/1.0 (https://github.com/UncleZI/mpd-cd-cue)
```

## Manual Testing & Debugging

You can run the loader manually at any time with verbose output:

```bash
sudo /usr/local/bin/mpd-cd-loader --device /dev/sr0 --debug
```

Logs from `udev` runs are written to `/tmp/cdtracks.log`.

## Uninstallation

```bash
sudo ./uninstall.sh
```

## License
Licensed under the [Apache License 2.0](LICENSE).
