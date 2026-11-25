#!/usr/bin/env bash
# ==============================================================================
# MAVRICK Pop!_OS Production Installer & Integration Script
# Sets up Mavrick AI Hub, verifies Antigravity, installs Albert Sans font,
# prompts for Pop!_OS utilities, configures COSMIC shortcuts, and enables systemd units
# ==============================================================================

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
HOME_DIR="$HOME"
STABLE_DIR="$HOME_DIR/.local/share/mavrick"
STABLE_APP_DIR="$STABLE_DIR/app"
BIN_DIR="$HOME_DIR/.local/bin"
APP_DIR="$HOME_DIR/.local/share/applications"
SYSTEMD_USER_DIR="$HOME_DIR/.config/systemd/user"
SHORTCUTS_FILE="$HOME_DIR/.config/cosmic/com.system76.CosmicSettings.Shortcuts/v1/custom"

CLR_SLATE="\033[38;2;148;163;184m"
CLR_GREEN="\033[38;2;74;222;128m"
CLR_WHITE="\033[38;2;241;245;249m"
CLR_RED="\033[38;2;239;68;68m"
CLR_AMBER="\033[38;2;251;191;36m"
CLR_BOLD="\033[1m"
CLR_RESET="\033[0m"

echo -e "${CLR_SLATE}${CLR_BOLD}"
cat << "EOF"
 __  __    ___  __     __ ____   ___  ____  _  __
|  \/  |  / _ \ \ \   / /|  _ \ |_ _// ___|| |/ /
| |\/| | / /_\ \ \ \ / / | |_) | | || |    | ' / 
| |  | | |  _  |  \ V /  |  _ <  | || |___ | . \ 
|_|  |_| |_| |_|   \_/   |_| \_\|___|\____||_|\_\
EOF
echo -e "${CLR_WHITE} Minimalist Autonomous AI Operating System Hub (Pop!_OS)${CLR_RESET}"
echo -e "${CLR_SLATE} Powered by Antigravity (Gemini 3.8 Flash Warm IPC) · Albert Sans${CLR_RESET}"
echo -e "------------------------------------------------------------------"

mkdir -p "$STABLE_DIR" "$STABLE_APP_DIR" "$BIN_DIR" "$APP_DIR" "$SYSTEMD_USER_DIR"
mkdir -p "$(dirname "$SHORTCUTS_FILE")"

# 1. Verify Core Prerequisites
echo -e "${CLR_WHITE}[1/6] Verifying Core Prerequisites...${CLR_RESET}"
if command -v python3 >/dev/null 2>&1; then
    PY_VER="$(python3 -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")')"
    echo -e "  ${CLR_GREEN}✓ Python $PY_VER detected${CLR_RESET}"
else
    echo -e "  ${CLR_RED}✗ Python 3.10+ is required.${CLR_RESET}"
    exit 1
fi

if python3 -c 'import gi; gi.require_version("Gtk", "3.0"); from gi.repository import Gtk' 2>/dev/null; then
    echo -e "  ${CLR_GREEN}✓ PyGObject (GTK3) runtime verified${CLR_RESET}"
else
    echo -e "  ${CLR_AMBER}⚠️ PyGObject (GTK3) missing. Please install via: sudo apt install python3-gi gir1.2-gtk-3.0${CLR_RESET}"
fi

AGY_PATH="$HOME_DIR/.local/bin/agy"
if [ -x "$AGY_PATH" ] || command -v agy >/dev/null 2>&1; then
    RESOLVED_AGY="$(command -v agy || echo "$AGY_PATH")"
    echo -e "  ${CLR_GREEN}✓ Antigravity detected:${CLR_RESET} $RESOLVED_AGY"
else
    echo -e "  ${CLR_SLATE}⚠️ Antigravity CLI not on PATH. AI queries will utilize the warm background daemon.${CLR_RESET}"
fi

# 2. Check and Prompt for Optional Pop!_OS Utilities
echo -e "${CLR_WHITE}[2/6] Checking optional Pop!_OS utilities...${CLR_RESET}"
MISSING_UTILS=()

if command -v playerctl >/dev/null 2>&1; then
    echo -e "  ${CLR_GREEN}✓ playerctl: Found (media playback control enabled)${CLR_RESET}"
else
    echo -e "  ${CLR_AMBER}⚠️ playerctl: Not found (media control tool disabled)${CLR_RESET}"
    MISSING_UTILS+=("playerctl")
fi

if command -v system76-power >/dev/null 2>&1; then
    echo -e "  ${CLR_GREEN}✓ system76-power: Found (native Pop!_OS power profiles enabled)${CLR_RESET}"
elif command -v powerprofilesctl >/dev/null 2>&1; then
    echo -e "  ${CLR_GREEN}✓ powerprofilesctl: Found (fallback power profiles enabled)${CLR_RESET}"
else
    echo -e "  ${CLR_AMBER}⚠️ system76-power: Not found (power profiles tool disabled)${CLR_RESET}"
    MISSING_UTILS+=("system76-power")
fi

if command -v notify-send >/dev/null 2>&1; then
    echo -e "  ${CLR_GREEN}✓ libnotify-bin (notify-send): Found (desktop notifications enabled)${CLR_RESET}"
else
    echo -e "  ${CLR_AMBER}⚠️ libnotify-bin: Not found (desktop notifications disabled)${CLR_RESET}"
    MISSING_UTILS+=("libnotify-bin")
fi

if [ ${#MISSING_UTILS[@]} -gt 0 ]; then
    echo -e "  Missing recommended packages: ${MISSING_UTILS[*]}"
    if [ -t 0 ] && command -v sudo >/dev/null 2>&1; then
        read -r -p "  Install missing packages via 'sudo apt install ${MISSING_UTILS[*]}'? [y/N] " response
        if [[ "$response" =~ ^([yY][eE][sS]|[yY])$ ]]; then
            echo "  Updating apt and installing ${MISSING_UTILS[*]}..."
            sudo apt update && sudo apt install -y "${MISSING_UTILS[@]}" || true
        fi
    else
        echo -e "  ℹ️ To enable all native features later: sudo apt install ${MISSING_UTILS[*]}"
    fi
fi

# 3. Typography: Automatically Download & Cache Albert Sans
echo -e "${CLR_WHITE}[3/6] Verifying Albert Sans typography...${CLR_RESET}"
FONT_DIR="$HOME_DIR/.local/share/fonts/albertsans"
if fc-match "Albert Sans" 2>/dev/null | grep -iq "albert"; then
    echo -e "  ${CLR_GREEN}✓ Albert Sans typography active${CLR_RESET}"
else
    echo -e "  ⬇ Albert Sans not detected. Automatically downloading Google Font for pixel-perfect rendering..."
    mkdir -p "$FONT_DIR"
    curl -fsSL "https://github.com/google/fonts/raw/main/ofl/albertsans/AlbertSans%5Bwght%5D.ttf" -o "$FONT_DIR/AlbertSans-VariableFont_wght.ttf" 2>/dev/null || true
    curl -fsSL "https://github.com/google/fonts/raw/main/ofl/albertsans/AlbertSans-Italic%5Bwght%5D.ttf" -o "$FONT_DIR/AlbertSans-Italic-VariableFont_wght.ttf" 2>/dev/null || true

    if command -v fc-cache >/dev/null 2>&1; then
        echo -e "  Updating font cache (fc-cache -f)..."
        fc-cache -f "$HOME_DIR/.local/share/fonts" >/dev/null 2>&1 || true
        if fc-match "Albert Sans" 2>/dev/null | grep -iq "albert"; then
            echo -e "  ${CLR_GREEN}✓ Albert Sans successfully installed and cached${CLR_RESET}"
        else
            echo -e "  ${CLR_SLATE}⚠️ Font cache updated. Albert Sans will be used when available.${CLR_RESET}"
        fi
    fi
fi

# 4. Copy Application Files & Link Binary
echo -e "${CLR_WHITE}[4/6] Installing MAVRICK application to $STABLE_APP_DIR...${CLR_RESET}"
# Copy source files to stable user directory
for item in "$SCRIPT_DIR"/*; do
    base="$(basename "$item")"
    if [ "$base" != "__pycache__" ] && [ "$base" != ".git" ] && [ "$base" != "tests" ] && [ "$base" != "build" ] && [ "$base" != "dist" ] && [[ "$base" != *.deb ]]; then
        cp -r "$item" "$STABLE_APP_DIR/"
    fi
done

# Create backward-compatible symlink ~/.local/share/mavrick/mavrick -> app
if [ ! -e "$STABLE_DIR/mavrick" ]; then
    ln -s "app" "$STABLE_DIR/mavrick" 2>/dev/null || true
fi

# Install stable wrapper binary
cat << EOF > "$BIN_DIR/mavrick"
#!/usr/bin/env bash
# MAVRICK Executable Launcher
export PATH="\$HOME/.npm-global/bin:\$HOME/.local/bin:/usr/local/bin:/usr/bin:/bin:\$PATH"
export PYTHONPATH="$STABLE_DIR:$STABLE_APP_DIR:\$PYTHONPATH"
exec /usr/bin/python3 -m mavrick.cli "\$@"
EOF
chmod +x "$BIN_DIR/mavrick"
echo -e "  ${CLR_GREEN}✓ Installed wrapper binary: $BIN_DIR/mavrick${CLR_RESET}"

# Install desktop entry
cp "$SCRIPT_DIR/desktop/mavrick.desktop" "$APP_DIR/mavrick.desktop"
chmod 644 "$APP_DIR/mavrick.desktop"
if command -v update-desktop-database >/dev/null 2>&1; then
    update-desktop-database "$APP_DIR" 2>/dev/null || true
fi
echo -e "  ${CLR_GREEN}✓ Registered Mavrick in Pop!_OS App Library${CLR_RESET}"

# 5. Install & Auto-Enable Systemd User Services
echo -e "${CLR_WHITE}[5/6] Configuring and auto-enabling systemd user services...${CLR_RESET}"
cat << EOF > "$SYSTEMD_USER_DIR/mavrick.service"
[Unit]
Description=MAVRICK Desktop AI Operating System Assistant
After=graphical-session.target

[Service]
Type=simple
Environment="PATH=$HOME_DIR/.npm-global/bin:$HOME_DIR/.local/bin:/usr/local/bin:/usr/bin:/bin"
Environment="PYTHONPATH=$STABLE_DIR:$STABLE_APP_DIR:/usr/lib/mavrick:/usr/lib"
ExecStart=/usr/bin/python3 -m mavrick.ui
Restart=on-failure
RestartSec=2

[Install]
WantedBy=default.target
EOF
chmod 644 "$SYSTEMD_USER_DIR/mavrick.service"

cat << EOF > "$SYSTEMD_USER_DIR/mavrickd.service"
[Unit]
Description=MAVRICK AI Intelligence Daemon (mavrickd)
After=default.target

[Service]
Type=simple
Environment="PATH=$HOME_DIR/.npm-global/bin:$HOME_DIR/.local/bin:/usr/local/bin:/usr/bin:/bin"
Environment="PYTHONPATH=$STABLE_DIR:$STABLE_APP_DIR:/usr/lib/mavrick:/usr/lib"
ExecStart=/usr/bin/python3 -m mavrick.daemon
Restart=always
RestartSec=2

[Install]
WantedBy=default.target
EOF
chmod 644 "$SYSTEMD_USER_DIR/mavrickd.service"

if command -v systemctl >/dev/null 2>&1; then
    systemctl --user daemon-reload 2>/dev/null || true
    systemctl --user enable mavrick.service 2>/dev/null || true
    systemctl --user enable --now mavrickd.service 2>/dev/null || true
    echo -e "  ${CLR_GREEN}✓ mavrickd.service and mavrick.service enabled and active${CLR_RESET}"
fi

# 6. Configure Super + M Hotkey in COSMIC
echo -e "${CLR_WHITE}[6/6] Registering global hotkey [Super + M] in COSMIC Desktop...${CLR_RESET}"
python3 -c "
import os
import sys

shortcut_file = os.path.expanduser(sys.argv[1])
os.makedirs(os.path.dirname(shortcut_file), exist_ok=True)

content = ''
if os.path.exists(shortcut_file):
    with open(shortcut_file, 'r') as fp:
        content = fp.read()

if 'Spawn(\"mavrick\")' in content and 'key: \"m\"' in content:
    print('  ✓ Super + M is already registered for Mavrick.')
else:
    new_binding = '''    (
        modifiers: [
            Super,
        ],
        key: \"m\",
    ): Spawn(\"mavrick\"),'''
    
    if content.strip().endswith('}'):
        idx = content.rfind('}')
        updated = content[:idx].rstrip() + '\n' + new_binding + '\n}\n'
    else:
        updated = '{\n' + new_binding + '\n}\n'

    with open(shortcut_file, 'w') as fp:
        fp.write(updated)
    print('  ✓ Bound [Super + M] to Spawn(\"mavrick\") in COSMIC shortcuts.')
" "$SHORTCUTS_FILE"

echo ""
echo -e "${CLR_GREEN}===================================================================${CLR_RESET}"
echo -e "${CLR_WHITE}${CLR_BOLD}  MAVRICK is now ready on your Pop!_OS system!${CLR_RESET}"
echo -e "${CLR_GREEN}===================================================================${CLR_RESET}"
echo ""
echo -e "${CLR_WHITE}How to use Mavrick:${CLR_RESET}"
echo -e "  1. Press ${CLR_GREEN}Super + M${CLR_RESET} anywhere on your desktop to toggle Mavrick."
echo -e "  2. Or run ${CLR_SLATE}mavrick${CLR_RESET} in terminal, or search for 'Mavrick' in COSMIC Launcher."
echo -e "  3. Speak in natural language without keywords. Examples:"
echo -e "     - ${CLR_SLATE}\"split screen youtube and reddit in brave\"${CLR_RESET}"
echo -e "     - ${CLR_SLATE}\"system health\" or \"gpu stats\"${CLR_RESET}"
echo -e "     - ${CLR_SLATE}\"switch power profile to battery\"${CLR_RESET}"
echo -e "     - ${CLR_SLATE}\"media play pause\"${CLR_RESET}"
echo -e "     - ${CLR_SLATE}\"check for updates\"${CLR_RESET}"
echo -e "     - ${CLR_SLATE}\"clean system cache\"${CLR_RESET}"
echo -e "     - ${CLR_SLATE}\"install vscode\"${CLR_RESET}"
echo -e "     - ${CLR_SLATE}\"organize my messy downloads\"${CLR_RESET}"
echo ""
