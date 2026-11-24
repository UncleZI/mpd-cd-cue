#!/usr/bin/env bash
set -euo pipefail

# Terminal colors
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
NC='\033[0m'

info()  { echo -e "${GREEN}[INFO]${NC} $1"; }
warn()  { echo -e "${YELLOW}[WARN]${NC} $1"; }
error() { echo -e "${RED}[ERROR]${NC} $1"; exit 1; }

# Ensure script is run as root
if [[ $EUID -ne 0 ]]; then
    error "Please run this installer as root: sudo ./install.sh"
fi

# Locate the main Python loader script
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LOADER_SRC=""
if [[ -f "$SCRIPT_DIR/bin/mpd-cd-loader" ]]; then
    LOADER_SRC="$SCRIPT_DIR/bin/mpd-cd-loader"
elif [[ -f "$SCRIPT_DIR/mpd-cd-loader" ]]; then
    LOADER_SRC="$SCRIPT_DIR/mpd-cd-loader"
elif [[ -f "$SCRIPT_DIR/mpd-cd-loader.py" ]]; then
    LOADER_SRC="$SCRIPT_DIR/mpd-cd-loader.py"
else
    error "Could not find mpd-cd-loader (or mpd-cd-loader.py) next to install.sh"
fi

# 1. Install dependencies on Arch Linux
if command -v pacman >/dev/null 2>&1; then
    DEPS=(mpd mpc libcdio-paranoia cdrtools python python-discid)
    info "Checking and installing dependencies via pacman: ${DEPS[*]}"
    pacman -S --needed --noconfirm "${DEPS[@]}"
else
    warn "pacman not found. Make sure the following tools are installed: mpd, mpc, cdparanoia, cdda2wav (cdrtools), python, python-discid"
fi

echo ""
info "Configuring /etc/mpdcd.conf (press Enter to accept default values):"

# 2. Interactive configuration prompts
read -rp "1. CD-ROM device path [/dev/sr0]: " CD_DEVICE
CD_DEVICE="${CD_DEVICE:-/dev/sr0}"

read -rp "2. Directory to save CUE playlist (inside MPD music_directory) [/var/lib/mpd/music/CD]: " PLAYLIST_DIR
PLAYLIST_DIR="${PLAYLIST_DIR:-/var/lib/mpd/music/CD}"

read -rp "3. CUE playlist filename [Audio-CD.cue]: " PLAYLIST_NAME
PLAYLIST_NAME="${PLAYLIST_NAME:-Audio-CD.cue}"

DEFAULT_REL_PATH="$(basename "$PLAYLIST_DIR")/$PLAYLIST_NAME"
read -rp "4. Relative playlist path for 'mpc load' [$DEFAULT_REL_PATH]: " MPD_REL_PATH
MPD_REL_PATH="${MPD_REL_PATH:-$DEFAULT_REL_PATH}"

read -rp "5. Playlist directory owner (user:group, e.g., mpd:mpd) [mpd:mpd]: " FILE_OWNER
FILE_OWNER="${FILE_OWNER:-mpd:mpd}"

read -rp "6. MPD host [localhost]: " MPD_HOST
MPD_HOST="${MPD_HOST:-localhost}"

read -rp "7. MPD port [6600]: " MPD_PORT
MPD_PORT="${MPD_PORT:-6600}"

read -rp "8. SQLite cache database path [/var/cache/mpdcdtag/mb_cache.db]: " CACHE_DB
CACHE_DB="${CACHE_DB:-/var/cache/mpdcdtag/mb_cache.db}"

# 3. Write /etc/mpdcd.conf
info "Writing configuration to /etc/mpdcd.conf..."
cat > /etc/mpdcd.conf <<EOF
[general]
device = $CD_DEVICE
playlist_dir = $PLAYLIST_DIR
playlist_name = $PLAYLIST_NAME
owner = $FILE_OWNER
cache_db = $CACHE_DB

[mpd]
host = $MPD_HOST
port = $MPD_PORT
mpd_playlist_path = $MPD_REL_PATH

[musicbrainz]
user_agent = mpd-cd-cue/1.0 (https://github.com/yourusername/mpd-cd-cue)
EOF
chmod 644 /etc/mpdcd.conf

# 4. Install the main Python script
info "Installing /usr/local/bin/mpd-cd-loader..."
install -Dm755 "$LOADER_SRC" /usr/local/bin/mpd-cd-loader

# 5. Install udev rule
info "Installing udev rule to /etc/udev/rules.d/99-cd-autostart.rules..."
cat > /etc/udev/rules.d/99-cd-autostart.rules <<'EOF'
ACTION=="change", SUBSYSTEM=="block", KERNEL=="sr[0-9]*", ENV{DISK_MEDIA_CHANGE}=="1", RUN+="/usr/bin/systemd-run --unit=cdtracks-%k --collect --no-block /usr/local/bin/mpd-cd-loader --device /dev/%k"
EOF
chmod 644 /etc/udev/rules.d/99-cd-autostart.rules

# Reload udev rules
udevadm control --reload-rules
udevadm trigger --subsystem-match=block --action=change || true

echo ""
info "Installation completed successfully!"
echo -e "${YELLOW}Important:${NC} Ensure the cdio_paranoia input plugin is enabled in your /etc/mpd.conf:"
echo '  input {'
echo '      plugin "cdio_paranoia"'
echo '  }'
