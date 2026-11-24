#!/usr/bin/env bash
set -euo pipefail

GREEN='\033[0;32m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
NC='\033[0m'

info()  { echo -e "${GREEN}[INFO]${NC} $1"; }
warn()  { echo -e "${YELLOW}[WARN]${NC} $1"; }
error() { echo -e "${RED}[ERROR]${NC} $1"; exit 1; }

if [[ $EUID -ne 0 ]]; then
    error "Please run this uninstaller as root: sudo ./uninstall.sh"
fi

info "Removing udev rule..."
rm -f /etc/udev/rules.d/99-cd-autostart.rules
udevadm control --reload-rules

info "Removing /usr/local/bin/mpd-cd-loader..."
rm -f /usr/local/bin/mpd-cd-loader

read -rp "Remove configuration file /etc/mpdcd.conf? [y/N]: " REMOVE_CONF
if [[ "${REMOVE_CONF,,}" == "y" ]]; then
    rm -f /etc/mpdcd.conf
    info "Removed /etc/mpdcd.conf"
fi

read -rp "Remove MusicBrainz SQLite cache (/var/cache/mpdcdtag)? [y/N]: " REMOVE_CACHE
if [[ "${REMOVE_CACHE,,}" == "y" ]]; then
    rm -rf /var/cache/mpdcdtag
    info "Removed /var/cache/mpdcdtag"
fi

info "Uninstallation complete."
