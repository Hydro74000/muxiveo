#!/usr/bin/env bash
# Installe APT_PACKAGES depuis archive.ubuntu.com (miroir Azure des runners contourné).
set -euo pipefail
set -f  # noms de paquets : pas d'expansion de motifs

read -r -d '' -a packages <<<"${APT_PACKAGES:-}" || true
if (( ${#packages[@]} == 0 )); then
  echo "::error::apt-install : aucun paquet demandé"
  exit 1
fi

missing=()
for package in "${packages[@]}"; do
  if [[ "$(dpkg-query -W -f='${Status}' "$package" 2>/dev/null || true)" != "install ok installed" ]]; then
    missing+=("$package")
  fi
done
if (( ${#missing[@]} == 0 )); then
  echo "Paquets déjà installés : ${packages[*]}"
  exit 0
fi
echo "Paquets à installer : ${missing[*]}"

# Copie des sources du runner (deb822 en 24.04, sources.list en 22.04) pointant sur l'archive officielle.
if [[ -f /etc/apt/sources.list.d/ubuntu.sources ]]; then
  source_file=/etc/apt/sources.list.d/ubuntu.sources
  sources="${RUNNER_TEMP:-/tmp}/apt-archive.sources"
else
  source_file=/etc/apt/sources.list
  sources="${RUNNER_TEMP:-/tmp}/apt-archive.list"
fi
sed -E \
  -e 's#mirror\+file:/etc/apt/apt-mirrors\.txt#https://archive.ubuntu.com/ubuntu/#g' \
  -e 's#https?://azure\.archive\.ubuntu\.com/ubuntu/?#https://archive.ubuntu.com/ubuntu/#g' \
  "$source_file" > "$sources"

apt_options=(
  -o "Dir::Etc::sourcelist=$sources"
  -o Dir::Etc::sourceparts=-
  -o Acquire::ForceIPv4=true
  -o Acquire::Retries=3
  -o Acquire::http::Timeout=30
  -o Acquire::https::Timeout=30
  -o DPkg::Lock::Timeout=120
)
for attempt in 1 2 3; do
  if sudo timeout --kill-after=15s 180s apt-get "${apt_options[@]}" -o APT::Update::Error-Mode=any update; then
    break
  fi
  if (( attempt == 3 )); then
    echo "::error::apt-get update impossible depuis archive.ubuntu.com"
    exit 1
  fi
  echo "apt-get update : nouvel essai ($attempt/3)"
  sleep 10
done
sudo timeout --kill-after=15s 900s env DEBIAN_FRONTEND=noninteractive \
  apt-get "${apt_options[@]}" install -y "${missing[@]}"
