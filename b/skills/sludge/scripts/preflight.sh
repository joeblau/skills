#!/usr/bin/env bash
# Check (and optionally install) everything b:sludge needs to render.
#
#   ./preflight.sh            report what is present and what is missing
#   ./preflight.sh --install  install what is missing, using the platform's package manager
#   ./preflight.sh --sludgify also require yt-dlp, which b:sludgify needs to fetch videos
#
# Exits 0 when every REQUIRED dependency is present, 1 otherwise. Optional
# dependencies never affect the exit code — the renderer falls back without them.
#
# Written for bash 3.2 (the macOS system bash), so no associative arrays.

set -u

DO_INSTALL=0
WANT_YTDLP=0
for arg in "$@"; do
  case "$arg" in
    --install) DO_INSTALL=1 ;;
    --sludgify) WANT_YTDLP=1 ;;
    -h|--help) sed -n '2,10p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) echo "unknown argument: $arg" >&2; exit 2 ;;
  esac
done

# ---------------------------------------------------------------- platform ---

OS="$(uname -s)"
PLATFORM="unknown"
PKG=""          # human name of the package manager
INSTALL_FFMPEG=""
INSTALL_UV=""

case "$OS" in
  Darwin)
    PLATFORM="macOS"
    if command -v brew >/dev/null 2>&1; then
      PKG="Homebrew"
      INSTALL_FFMPEG="brew install ffmpeg"
      INSTALL_UV="brew install uv"
    else
      PKG="none (install Homebrew first)"
      INSTALL_FFMPEG='/bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)" && brew install ffmpeg'
      INSTALL_UV="curl -LsSf https://astral.sh/uv/install.sh | sh"
    fi
    ;;
  Linux)
    PLATFORM="Linux"
    # uv is not packaged by most distros; the official installer is the reliable path.
    INSTALL_UV="curl -LsSf https://astral.sh/uv/install.sh | sh"
    if command -v apt-get >/dev/null 2>&1; then
      PKG="apt"; INSTALL_FFMPEG="sudo apt-get update && sudo apt-get install -y ffmpeg"
    elif command -v dnf >/dev/null 2>&1; then
      PKG="dnf"; INSTALL_FFMPEG="sudo dnf install -y ffmpeg"
    elif command -v pacman >/dev/null 2>&1; then
      PKG="pacman"; INSTALL_FFMPEG="sudo pacman -S --needed --noconfirm ffmpeg"
    elif command -v zypper >/dev/null 2>&1; then
      PKG="zypper"; INSTALL_FFMPEG="sudo zypper install -y ffmpeg"
    elif command -v apk >/dev/null 2>&1; then
      PKG="apk"; INSTALL_FFMPEG="sudo apk add ffmpeg"
    else
      PKG="none detected"; INSTALL_FFMPEG="# install ffmpeg with your distro's package manager"
    fi
    ;;
  MINGW*|MSYS*|CYGWIN*)
    PLATFORM="Windows"
    PKG="winget"
    INSTALL_FFMPEG="winget install --id Gyan.FFmpeg -e"
    INSTALL_UV="winget install --id astral-sh.uv -e"
    ;;
esac

# ------------------------------------------------------------------ checks ---

MISSING_REQUIRED=""
INSTALL_CMDS=""
# Distro yt-dlp packages go stale quickly and a stale yt-dlp fails on YouTube,
# so prefer the tool's own upgrade channel everywhere except Homebrew.
: "${INSTALL_YTDLP:=uv tool install yt-dlp}"

note_missing() {   # name, install-command
  MISSING_REQUIRED="$MISSING_REQUIRED $1"
  case "$INSTALL_CMDS" in
    *"$2"*) ;;                                   # already queued
    *) INSTALL_CMDS="$INSTALL_CMDS$2
" ;;
  esac
}

report() { printf '  %-9s %-8s %s\n' "$1" "$2" "$3"; }

WHAT="b:sludge"; [ "$WANT_YTDLP" -eq 1 ] && WHAT="b:sludge + b:sludgify"
echo "$WHAT preflight — $PLATFORM (package manager: ${PKG:-unknown})"
echo
echo "Required:"

if command -v ffmpeg >/dev/null 2>&1; then
  report ffmpeg OK "$(command -v ffmpeg)"

  # libx264 encodes the output; libass burns the captions. An ffmpeg built
  # without either will fail mid-render rather than at startup.
  if ffmpeg -hide_banner -encoders 2>/dev/null | grep -q ' libx264 '; then
    report "  libx264" OK "H.264 encoder present"
  else
    report "  libx264" MISSING "ffmpeg built without libx264 — reinstall a full build"
    note_missing "ffmpeg(libx264)" "$INSTALL_FFMPEG"
  fi

  if ffmpeg -hide_banner -filters 2>/dev/null | grep -qE '^ *\.\.\.? +ass +'; then
    report "  libass" OK "subtitle burn-in present"
  else
    report "  libass" MISSING "ffmpeg built without libass — captions cannot burn in"
    note_missing "ffmpeg(libass)" "$INSTALL_FFMPEG"
  fi
else
  report ffmpeg MISSING "renders the video"
  note_missing ffmpeg "$INSTALL_FFMPEG"
fi

if command -v ffprobe >/dev/null 2>&1; then
  report ffprobe OK "$(command -v ffprobe)"
else
  report ffprobe MISSING "inspects the inputs (ships with ffmpeg)"
  note_missing ffprobe "$INSTALL_FFMPEG"
fi

if command -v uv >/dev/null 2>&1; then
  report uv OK "$(command -v uv)"
else
  report uv MISSING "runs sludge.py and installs OpenCV/NumPy on demand"
  note_missing uv "$INSTALL_UV"
fi

if [ "$WANT_YTDLP" -eq 1 ]; then
  if command -v yt-dlp >/dev/null 2>&1; then
    report yt-dlp OK "$(command -v yt-dlp)"
  else
    report yt-dlp MISSING "b:sludgify downloads source videos with it"
    note_missing yt-dlp "$INSTALL_YTDLP"
  fi
fi

echo
echo "Optional (the renderer falls back to 'uv' if these are absent):"

if command -v whisper-cli >/dev/null 2>&1; then
  report whisper OK "whisper-cli — fastest transcription (30-46x realtime)"
elif command -v whisper >/dev/null 2>&1; then
  report whisper OK "openai-whisper on PATH"
else
  report whisper fallback "will run via 'uvx --from openai-whisper whisper' (~10x slower)"
fi

if command -v demucs >/dev/null 2>&1; then
  report demucs OK "$(command -v demucs)"
else
  report demucs fallback "will run via uvx when a head has music under it"
fi

# ------------------------------------------------------------------ result ---

echo
if [ -z "$MISSING_REQUIRED" ]; then
  echo "All required dependencies present. b:sludge can render."
  echo
  echo "First render also downloads, once:"
  echo "  - 230 KB YuNet face model  -> ~/.cache/b-sludge"
  echo "  - 1.5 GB whisper large-v3-turbo -> ~/.cache/whisper (skip with --model small)"
  exit 0
fi

echo "Missing:$MISSING_REQUIRED"
echo

if [ "$DO_INSTALL" -eq 1 ]; then
  echo "Installing..."
  echo
  # Process substitution, not a pipe: a pipe would run the loop in a subshell
  # and a failed install could not abort the script.
  while IFS= read -r cmd; do
    [ -n "$cmd" ] || continue
    echo "\$ $cmd"
    sh -c "$cmd" || { echo "FAILED: $cmd" >&2; exit 1; }
    echo
  done < <(printf '%s' "$INSTALL_CMDS")
  echo "Re-checking..."
  echo
  if [ "$WANT_YTDLP" -eq 1 ]; then exec "$0" --sludgify; else exec "$0"; fi
fi

echo "To install, run:"
echo
printf '%s' "$INSTALL_CMDS" | sed 's/^/  /'
echo
echo "Or re-run this script with --install to execute the above."
exit 1
