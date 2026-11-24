#!/usr/bin/env python3
"""
mpd-cd-loader: Automatically detect Audio CDs, fetch metadata via CD-Text or MusicBrainz,
generate a CUE sheet, and load it into MPD.
"""

import argparse
import configparser
import fcntl
import json
import logging
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import time
import tempfile
import urllib.error
import urllib.request
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path

try:
    import discid
except ImportError:
    discid = None

DEFAULT_CONFIG = {
    "device": "/dev/sr0",
    "playlist_dir": "/var/lib/mpd/music/CD",
    "playlist_name": "Audio-CD.cue",
    "owner": "mpd:mpd",
    "cache_db": "/var/cache/mpdcdtag/mb_cache.db",
    "host": "localhost",
    "port": "6600",
    "mpd_playlist_path": "CD/Audio-CD.cue",
    "user_agent": "mpd-cd-cue/1.0 (https://github.com/UncleZI/mpd-cd-cue)",
}

LOCK_PATH = "/var/lock/mpd-cd-loader.lock"
LOG_PATH = "/tmp/cdtracks.log"


def setup_logging(debug=False):
    level = logging.DEBUG if debug else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s [%(levelname)s] %(message)s",
        handlers=[
            logging.FileHandler(LOG_PATH, mode="w", encoding="utf-8"),
            logging.StreamHandler(sys.stdout),
        ],
    )


def load_config(config_path):
    cfg = DEFAULT_CONFIG.copy()
    if os.path.exists(config_path):
        parser = configparser.ConfigParser()
        parser.read(config_path, encoding="utf-8")
        for section in ("general", "mpd", "musicbrainz"):
            if section in parser:
                cfg.update(parser[section])
    return cfg


def normalize_string(s: str) -> str:
    """Replace typographic punctuation with ASCII equivalents while preserving Unicode letters."""
    if not s:
        return ""
    replacements = {
        "\u2010": "-", "\u2011": "-", "\u2012": "-", "\u2013": "-",
        "\u2014": "-", "\u2212": "-", "\u2026": "...",
        "\u2018": "'", "\u2019": "'", "\u201c": '"', "\u201d": '"',
        "\u00ab": '"', "\u00bb": '"',
    }
    for old, new in replacements.items():
        s = s.replace(old, new)
    return s.strip()


def clean_for_cue(s: str) -> str:
    """Sanitize a string for safe inclusion inside CUE double-quoted fields."""
    s = normalize_string(s)
    s = s.replace('"', '""')
    s = re.sub(r"[\x00-\x1f\x7f]", "", s)
    return s.strip()


def sectors_to_mmss(sectors: int) -> str:
    """Convert CD audio sectors (75 sectors/sec) into MM:SS format."""
    total_seconds = max(0, sectors // 75)
    return f"{total_seconds // 60:02d}:{total_seconds % 60:02d}"


def run_cmd(cmd, timeout=15):
    try:
        res = subprocess.run(
            cmd, capture_output=True, text=True, timeout=timeout, check=False
        )
        return res.returncode, res.stdout, res.stderr
    except subprocess.TimeoutExpired:
        logging.warning("Command timed out: %s", " ".join(cmd))
        return -1, "", "timeout"
    except FileNotFoundError:
        logging.error("Command not found: %s", cmd[0])
        return -1, "", "not found"


def get_toc_info(device: str):
    """Query track count and Media Catalog Number (MCN) using cdparanoia."""
    rc, out, err = run_cmd(["cdparanoia", "-d", device, "-sQ"], timeout=10)
    combined = out + "\n" + err
    tracks = re.findall(r"^\s+(\d+)\.\s+(\d+)\s+\[", combined, re.MULTILINE)
    if rc != 0 or not tracks:
        return 0, None

    mcn_match = re.search(r"Media Catalog Number:\s*(\S+)", combined, re.IGNORECASE)
    mcn = mcn_match.group(1) if mcn_match else None
    return len(tracks), mcn


def get_cdtext_metadata(device: str, track_count: int):
    """Attempt to read embedded CD-Text metadata using cdda2wav."""
    try:
        with tempfile.TemporaryDirectory(prefix="mpdcd_") as tmpdir:
            res = subprocess.run(
                ["cdda2wav", "-J", "-T", "-D", device, "-t", "1+", "-v", "!trackid"],
                capture_output=True,
                text=True,
                timeout=15,
                cwd=tmpdir,
            )
    except (subprocess.TimeoutExpired, FileNotFoundError) as e:
        logging.warning("cdda2wav execution failed: %s", e)
        return None

    combined = res.stdout + "\n" + res.stderr
    if res.returncode != 0 or "CD-Text: detected" not in combined:
        return None

    logging.info("CD-Text detected, parsing...")
    album = ""
    album_artist = ""

    album_match = re.search(
        r"Album title:\s*'([^']*)'(?:\s*\[from\s+([^\]]+)\])?", combined
    )
    if album_match:
        album = album_match.group(1).strip()
        album_artist = (album_match.group(2) or "").strip()

    tracks_data = {}
    for match in re.finditer(
        r"Track\s+(\d+):\s*'([^']*)'(?:\s*\[from\s+([^\]]+)\])?", combined
    ):
        t_num = int(match.group(1))
        tracks_data[t_num] = {
            "title": match.group(2).strip(),
            "artist": (match.group(3) or "").strip(),
        }

    durations = {}
    for match in re.finditer(r"(\d+)\.\(\s*(\d+:\d+)\)", combined):
        durations[int(match.group(1))] = match.group(2)

    if not album_artist:
        for i in range(1, track_count + 1):
            if tracks_data.get(i, {}).get("artist"):
                album_artist = tracks_data[i]["artist"]
                break
    album_artist = album_artist or "Unknown Artist"
    album = album or f"Audio CD {datetime.now().strftime('%Y-%m-%d %H:%M')}"

    tracks = []
    for i in range(1, track_count + 1):
        t_info = tracks_data.get(i, {})
        tracks.append({
            "number": i,
            "title": t_info.get("title") or f"Track {i}",
            "artist": t_info.get("artist") or album_artist,
            "duration": durations.get(i, "00:00"),
        })

    return {"artist": album_artist, "album": album, "tracks": tracks}


@contextmanager
def db_connection(db_path: str):
    os.makedirs(os.path.dirname(db_path), exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_db(db_path: str):
    with db_connection(db_path) as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS discs (
                discid TEXT PRIMARY KEY,
                mcn TEXT,
                release_id TEXT,
                artist TEXT NOT NULL,
                album TEXT NOT NULL,
                date_added INTEGER NOT NULL
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS tracks (
                discid TEXT NOT NULL,
                number INTEGER NOT NULL,
                title TEXT NOT NULL,
                track_artist TEXT NOT NULL,
                FOREIGN KEY(discid) REFERENCES discs(discid),
                PRIMARY KEY (discid, number)
            )
        """)


def get_from_cache(db_path: str, disc_id: str):
    init_db(db_path)
    with db_connection(db_path) as conn:
        disc = conn.execute(
            "SELECT artist, album FROM discs WHERE discid = ?", (disc_id,)
        ).fetchone()
        if not disc:
            return None
        rows = conn.execute(
            "SELECT number, title, track_artist FROM tracks WHERE discid = ? ORDER BY number",
            (disc_id,),
        ).fetchall()
        return {
            "artist": disc["artist"],
            "album": disc["album"],
            "tracks": [
                {"number": r["number"], "title": r["title"], "artist": r["track_artist"]}
                for r in rows
            ],
        }


def save_to_cache(db_path: str, disc_id: str, data: dict, mcn=None):
    init_db(db_path)
    with db_connection(db_path) as conn:
        conn.execute(
            "INSERT OR REPLACE INTO discs (discid, mcn, release_id, artist, album, date_added) VALUES (?, ?, ?, ?, ?, ?)",
            (
                disc_id,
                mcn,
                data.get("release_id"),
                normalize_string(data["artist"]),
                normalize_string(data["album"]),
                int(time.time()),
            ),
        )
        for t in data["tracks"]:
            conn.execute(
                "INSERT OR REPLACE INTO tracks (discid, number, title, track_artist) VALUES (?, ?, ?, ?)",
                (
                    disc_id,
                    t["number"],
                    normalize_string(t["title"]),
                    normalize_string(t["artist"]),
                ),
            )


def fetch_json(url: str, user_agent: str, timeout=20):
    req = urllib.request.Request(url, headers={"User-Agent": user_agent})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except (urllib.error.URLError, json.JSONDecodeError, TimeoutError) as e:
        logging.warning("MusicBrainz request failed (%s): %s", url, e)
        return None


def get_musicbrainz_metadata(device: str, mcn: str, cfg: dict):
    """Calculate DiscID and look up release metadata in local cache or MusicBrainz API."""
    if discid is None:
        logging.error("python-discid is not installed")
        return None

    try:
        disc = discid.read(device)
    except Exception as e:
        logging.error("Failed to read DiscID from %s: %s", device, e)
        return None

    disc_id = disc.id
    logging.info("Calculated DiscID: %s", disc_id)

    offsets = [t.offset for t in disc.tracks] + [disc.sectors]
    durations = {
        i + 1: sectors_to_mmss(offsets[i + 1] - offsets[i])
        for i in range(len(disc.tracks))
    }

    cached = get_from_cache(cfg["cache_db"], disc_id)
    if cached:
        logging.info("Metadata found in SQLite cache")
        for t in cached["tracks"]:
            t["duration"] = durations.get(t["number"], "00:00")
        return cached

    logging.info("Fetching metadata from MusicBrainz...")
    ua = cfg["user_agent"]
    disc_data = fetch_json(f"https://musicbrainz.org/ws/2/discid/{disc_id}?fmt=json", ua)
    if not disc_data or not disc_data.get("releases"):
        return None

    release_id = disc_data["releases"][0]["id"]
    time.sleep(1.1)  # Respect MusicBrainz API rate limit (1 req/sec)

    rel_data = fetch_json(
        f"https://musicbrainz.org/ws/2/release/{release_id}?inc=recordings+artist-credits&fmt=json",
        ua,
    )
    if not rel_data:
        return None

    album_artist = "Unknown Artist"
    if rel_data.get("artist-credit"):
        artists = [
            ac["artist"]["name"]
            for ac in rel_data["artist-credit"]
            if isinstance(ac, dict) and ac.get("artist")
        ]
        if artists:
            album_artist = ", ".join(artists)

    album = rel_data.get("title", "Unknown Album")
    medium_pos = disc_data.get("medium", {}).get("position", 1)
    tracks = []

    for medium in rel_data.get("media", []):
        if medium.get("position") == medium_pos and medium.get("tracks"):
            for tr in medium["tracks"]:
                t_num = int(tr["position"])
                t_artist = album_artist
                if tr.get("artist-credit"):
                    t_artists = [
                        ac["artist"]["name"]
                        for ac in tr["artist-credit"]
                        if isinstance(ac, dict) and ac.get("artist")
                    ]
                    if t_artists:
                        t_artist = ", ".join(t_artists)
                tracks.append({
                    "number": t_num,
                    "title": tr.get("title", f"Track {t_num}"),
                    "artist": t_artist,
                    "duration": durations.get(t_num, "00:00"),
                })
            break

    if not tracks:
        return None

    result = {
        "artist": album_artist,
        "album": album,
        "release_id": release_id,
        "tracks": tracks,
    }
    save_to_cache(cfg["cache_db"], disc_id, result, mcn)
    return result


def write_cue_file(metadata: dict, cfg: dict):
    """Generate the CUE sheet using cdda:/// URIs for MPD playback."""
    playlist_dir = Path(cfg["playlist_dir"])
    if playlist_dir.exists():
        shutil.rmtree(playlist_dir)
    playlist_dir.mkdir(parents=True, exist_ok=True)

    cue_path = playlist_dir / cfg["playlist_name"]
    album_artist = clean_for_cue(metadata["artist"])
    album_title = clean_for_cue(metadata["album"])

    lines = [
        "REM GENRE Unknown",
        f"REM DATE {datetime.now().year}",
        f'PERFORMER "{album_artist}"',
        f'TITLE "{album_title}"',
    ]

    for tr in metadata["tracks"]:
        num = tr["number"]
        title = clean_for_cue(tr["title"]) or f"Track {num}"
        artist = clean_for_cue(tr["artist"]) or album_artist
        duration = tr.get("duration", "00:00")

        lines.extend([
            f'FILE "cdda:///{num}" WAVE',
            "    INDEX 01 00:00:00",
            f"  TRACK {num:02d} AUDIO",
            f'    TITLE "{title}"',
            f'    PERFORMER "{artist}"',
            f"    REM DURATION {duration}",
        ])

    cue_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    os.chmod(playlist_dir, 0o775)
    os.chmod(cue_path, 0o664)

    if cfg["owner"]:
        run_cmd(["chown", "-R", cfg["owner"], str(playlist_dir)])


def mpc_cmd(cfg: dict, *args):
    cmd = ["mpc", "-h", cfg["host"], "-p", str(cfg["port"]), *args]
    return run_cmd(cmd, timeout=10)


def main():
    parser = argparse.ArgumentParser(description="Auto-load Audio CD into MPD via CUE")
    parser.add_argument("-c", "--config", default="/etc/mpdcd.conf", help="Path to config file")
    parser.add_argument("-d", "--device", help="Override CD-ROM device (e.g. /dev/sr0)")
    parser.add_argument("--debug", action="store_true", help="Enable debug logging")
    args = parser.parse_args()

    setup_logging(args.debug)
    cfg = load_config(args.config)
    if args.device:
        cfg["device"] = args.device

    # Prevent concurrent udev executions
    lock_fd = open(LOCK_PATH, "w")
    try:
        fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except IOError:
        logging.info("Another instance is already running, exiting.")
        sys.exit(0)

    mpc_cmd(cfg, "clear")
    mpc_cmd(cfg, "update")

    track_count, mcn = get_toc_info(cfg["device"])
    if track_count == 0:
        logging.info("No Audio CD detected in %s", cfg["device"])
        sys.exit(0)

    logging.info("Detected Audio CD with %d tracks", track_count)

    # 1. Try reading CD-Text
    metadata = get_cdtext_metadata(cfg["device"], track_count)

    # 2. Fallback to MusicBrainz (with SQLite cache)
    if not metadata:
        metadata = get_musicbrainz_metadata(cfg["device"], mcn, cfg)

    # 3. Fallback to generic track titles
    if not metadata:
        logging.info("No metadata found, using fallback track names")
        metadata = {
            "artist": "Unknown Artist",
            "album": f"Audio CD {datetime.now().strftime('%Y-%m-%d %H:%M')}",
            "tracks": [
                {"number": i, "title": f"Track {i}", "artist": "Unknown Artist", "duration": "00:00"}
                for i in range(1, track_count + 1)
            ],
        }

    write_cue_file(metadata, cfg)
    mpc_cmd(cfg, "update", "--wait")
    mpc_cmd(cfg, "clear")
    mpc_cmd(cfg, "load", cfg["mpd_playlist_path"])
    logging.info("Playlist loaded into MPD successfully")


if __name__ == "__main__":
    main()
