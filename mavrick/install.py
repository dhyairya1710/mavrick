#!/usr/bin/env python3
"""
MAVRICK Production Installer & Uninstaller for Pop!_OS
Installs application files to stable user directory (~/.local/share/mavrick/app),
verifies and prompts for Pop!_OS utilities, automatically downloads and caches Albert Sans font,
configures COSMIC shortcuts, and auto-enables user systemd services.
"""

import os
import sys
import shutil
import urllib.request
import subprocess
import argparse
from pathlib import Path

HOME = os.path.expanduser("~")
STABLE_DIR = os.path.join(HOME, ".local", "share", "mavrick")
STABLE_APP_DIR = os.path.join(STABLE_DIR, "app")
STABLE_MAVRICK_DIR = os.path.join(STABLE_DIR, "mavrick")
BIN_DIR = os.path.join(HOME, ".local", "bin")
BIN_PATH = os.path.join(BIN_DIR, "mavrick")
DESKTOP_DIR = os.path.join(HOME, ".local", "share", "applications")
DESKTOP_FILE = os.path.join(DESKTOP_DIR, "mavrick.desktop")
SYSTEMD_USER_DIR = os.path.join(HOME, ".config", "systemd", "user")
COSMIC_SHORTCUTS = os.path.join(HOME, ".config", "cosmic", "com.system76.CosmicSettings.Shortcuts", "v1", "custom")

SOURCE_DIR = os.path.dirname(os.path.realpath(__file__))

def check_and_prompt_optional_utilities(auto_yes: bool = False) -> None:
    """Checks for Pop!_OS utilities (playerctl, system76-power, libnotify-bin) and prompts if missing."""
    print("\nChecking optional Pop!_OS utilities...")
    missing_packages = []

    # 1. playerctl
    if shutil.which("playerctl"):
        print("  ✓ playerctl: Found (media playback control enabled)")
    else:
        print("  ⚠️ playerctl: Not found (media playback control tool disabled)")
        missing_packages.append("playerctl")

    # 2. system76-power
    if shutil.which("system76-power"):
        print("  ✓ system76-power: Found (native Pop!_OS power profile switching enabled)")
    elif shutil.which("powerprofilesctl"):
        print("  ✓ powerprofilesctl: Found (fallback power profile switching enabled)")
    else:
        print("  ⚠️ system76-power: Not found (power profiles tool disabled)")
        missing_packages.append("system76-power")

    # 3. libnotify-bin (notify-send)
    if shutil.which("notify-send"):
        print("  ✓ libnotify-bin (notify-send): Found (native desktop notifications enabled)")
    else:
        print("  ⚠️ libnotify-bin: Not found (desktop notifications disabled)")
        missing_packages.append("libnotify-bin")

    if missing_packages:
        pkgs_str = " ".join(missing_packages)
        print(f"\nMissing recommended Pop!_OS packages: {pkgs_str}")

        install_now = False
        if auto_yes:
            install_now = True
        elif sys.stdin.isatty():
            try:
                response = input(f"Would you like to install them now with 'sudo apt install {pkgs_str}'? [y/N]: ").strip().lower()
                if response in ["y", "yes"]:
                    install_now = True
            except (EOFError, KeyboardInterrupt):
                print("")

        if install_now:
            print(f"Running: sudo apt install -y {pkgs_str}")
            try:
                subprocess.run(["sudo", "apt", "update"], check=False)
                res = subprocess.run(["sudo", "apt", "install", "-y"] + missing_packages, check=False)
                if res.returncode == 0:
                    print("  ✓ Optional packages installed successfully.")
                else:
                    print(f"  ⚠️ Package installation exited with code {res.returncode}")
            except Exception as e:
                print(f"  ⚠️ Could not run apt install: {e}")
        else:
            print(f"  ℹ️ You can install them later via: sudo apt install {pkgs_str}")

def ensure_albert_sans_font() -> bool:
    """Verifies Albert Sans font is available, downloads and caches it into ~/.local/share/fonts if missing."""
    print("\nVerifying typography (Albert Sans)...")
    try:
        res = subprocess.run(["fc-match", "Albert Sans"], capture_output=True, text=True)
        if "albert" in res.stdout.lower():
            print(f"  ✓ Albert Sans typography active: {res.stdout.strip()}")
            return True
    except Exception:
        pass

    print("  ⬇ Albert Sans not detected. Automatically downloading Google Font for pixel-perfect Pop!_OS typography...")
    fonts_dir = Path(HOME) / ".local" / "share" / "fonts" / "albertsans"
    fonts_dir.mkdir(parents=True, exist_ok=True)

    font_urls = [
        ("AlbertSans-VariableFont_wght.ttf", "https://github.com/google/fonts/raw/main/ofl/albertsans/AlbertSans%5Bwght%5D.ttf"),
        ("AlbertSans-Italic-VariableFont_wght.ttf", "https://github.com/google/fonts/raw/main/ofl/albertsans/AlbertSans-Italic%5Bwght%5D.ttf")
    ]

    downloaded = 0
    for fname, url in font_urls:
        target = fonts_dir / fname
        if not target.exists() or target.stat().st_size < 1000:
            try:
                req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (MAVRICK Installer)"})
                with urllib.request.urlopen(req, timeout=12) as resp:
                    content = resp.read()
                    if len(content) > 1000:
                        target.write_bytes(content)
                        downloaded += 1
                        print(f"    ✓ Downloaded {fname} ({len(content) / 1024:.1f} KB)")
            except Exception as e:
                print(f"    ⚠️ Could not download {fname}: {e}")

    # Update font cache
    if shutil.which("fc-cache"):
        print("  Updating system font cache (fc-cache -f)...")
        try:
            subprocess.run(["fc-cache", "-f", str(fonts_dir.parent)], capture_output=True, text=True)
            res = subprocess.run(["fc-match", "Albert Sans"], capture_output=True, text=True)
            if "albert" in res.stdout.lower():
                print("  ✓ Albert Sans successfully registered and cached.")
                return True
        except Exception as e:
            print(f"  ⚠️ Error updating font cache: {e}")

    return False

def check_prerequisites(auto_yes: bool = False) -> bool:
    """Verifies that system satisfies all runtime dependencies and checks optional tools."""
    print("Checking core prerequisites...")
    errors = []

    # 1. Python version >= 3.10
    if sys.version_info < (3, 10):
        errors.append(f"Python 3.10+ required. Current version is {sys.version}")
    else:
        print(f"  ✓ Python {sys.version.split()[0]} detected.")

    # 2. PyGObject / GTK3
    try:
        import gi
        gi.require_version("Gtk", "3.0")
        from gi.repository import Gtk
        print("  ✓ PyGObject (GTK3) runtime verified.")
    except Exception as e:
        errors.append(f"PyGObject (GTK3) is required: {e}. Install via: sudo apt install python3-gi gir1.2-gtk-3.0")

    # 3. Antigravity CLI or Daemon
    agy = shutil.which("agy") or os.path.exists(os.path.join(HOME, ".local", "bin", "agy"))
    if not agy:
        print("  ⚠️ Notice: 'agy' CLI not found on PATH. AI queries will use mavrickd warm daemon.")
    else:
        print(f"  ✓ Antigravity AI Core detected: {agy if isinstance(agy, str) else 'agy'}")

    if errors:
        print("\nPrerequisite check failed:")
        for err in errors:
            print(f"  ✗ {err}")
        return False

    # Check optional Pop!_OS utilities
    check_and_prompt_optional_utilities(auto_yes)

    # Ensure Albert Sans typography
    ensure_albert_sans_font()

    return True

def setup_systemd_services():
    """Installs and auto-enables user systemd services (mavrickd.service and mavrick.service)."""
    print("\nConfiguring systemd user services...")
    os.makedirs(SYSTEMD_USER_DIR, exist_ok=True)

    mavrick_service_file = os.path.join(SYSTEMD_USER_DIR, "mavrick.service")
    mavrickd_service_file = os.path.join(SYSTEMD_USER_DIR, "mavrickd.service")

    mavrick_unit_content = f"""[Unit]
Description=MAVRICK Desktop AI Operating System Assistant
After=graphical-session.target

[Service]
Type=simple
Environment="PATH={HOME}/.npm-global/bin:{HOME}/.local/bin:/usr/local/bin:/usr/bin:/bin"
Environment="PYTHONPATH={STABLE_DIR}:{STABLE_APP_DIR}:/usr/lib/mavrick:/usr/lib"
ExecStart=/usr/bin/python3 -m mavrick.ui
Restart=on-failure
RestartSec=2

[Install]
WantedBy=default.target
"""

    mavrickd_unit_content = f"""[Unit]
Description=MAVRICK AI Intelligence Daemon (mavrickd)
After=default.target

[Service]
Type=simple
Environment="PATH={HOME}/.npm-global/bin:{HOME}/.local/bin:/usr/local/bin:/usr/bin:/bin"
Environment="PYTHONPATH={STABLE_DIR}:{STABLE_APP_DIR}:/usr/lib/mavrick:/usr/lib"
ExecStart=/usr/bin/python3 -m mavrick.daemon
Restart=always
RestartSec=2

[Install]
WantedBy=default.target
"""

    with open(mavrick_service_file, "w", encoding="utf-8") as f:
        f.write(mavrick_unit_content)
    os.chmod(mavrick_service_file, 0o644)
    print(f"  ✓ Installed {mavrick_service_file}")

    with open(mavrickd_service_file, "w", encoding="utf-8") as f:
        f.write(mavrickd_unit_content)
    os.chmod(mavrickd_service_file, 0o644)
    print(f"  ✓ Installed {mavrickd_service_file}")

    if shutil.which("systemctl"):
        print("  Reloading user systemd daemon...")
        subprocess.run(["systemctl", "--user", "daemon-reload"], capture_output=True)

        print("  Auto-enabling and starting mavrickd.service...")
        res_d = subprocess.run(["systemctl", "--user", "enable", "--now", "mavrickd.service"], capture_output=True, text=True)
        if res_d.returncode == 0:
            print("  ✓ mavrickd.service enabled and active.")
        else:
            print(f"  ⚠️ Notice: Could not start mavrickd.service immediately: {res_d.stderr.strip()}")

        print("  Auto-enabling mavrick.service...")
        res_ui = subprocess.run(["systemctl", "--user", "enable", "mavrick.service"], capture_output=True, text=True)
        if res_ui.returncode == 0:
            print("  ✓ mavrick.service enabled for graphical session startup.")
        else:
            print(f"  ⚠️ Notice: Could not enable mavrick.service: {res_ui.stderr.strip()}")

def configure_cosmic_shortcut():
    """Binds Super + M to Spawn('mavrick') in COSMIC desktop settings."""
    if not os.path.exists(os.path.dirname(COSMIC_SHORTCUTS)):
        return

    content = ""
    if os.path.exists(COSMIC_SHORTCUTS):
        with open(COSMIC_SHORTCUTS, "r") as fp:
            content = fp.read()

    if 'Spawn("mavrick")' in content and 'key: "m"' in content:
        print("  ✓ Super + M shortcut is already active in COSMIC.")
        return

    new_binding = """    (
        modifiers: [
            Super,
        ],
        key: "m",
    ): Spawn("mavrick"),"""

    if content.strip().endswith("}"):
        idx = content.rfind("}")
        updated = content[:idx].rstrip() + "\n" + new_binding + "\n}\n"
    else:
        updated = "{\n" + new_binding + "\n}\n"

    try:
        with open(COSMIC_SHORTCUTS, "w") as fp:
            fp.write(updated)
        print("  ✓ Registered Super + M hotkey in COSMIC Settings.")
    except Exception as e:
        print(f"  ⚠️ Could not write COSMIC shortcut: {e}")

def install(auto_yes: bool = False):
    print("=========================================================")
    print("  MAVRICK Pop!_OS Production Installation")
    print("=========================================================")

    if not check_prerequisites(auto_yes=auto_yes):
        sys.exit(1)

    # 1. Create directories
    os.makedirs(STABLE_DIR, mode=0o700, exist_ok=True)
    os.makedirs(STABLE_APP_DIR, mode=0o755, exist_ok=True)
    os.makedirs(BIN_DIR, mode=0o755, exist_ok=True)
    os.makedirs(DESKTOP_DIR, mode=0o755, exist_ok=True)

    # 2. Copy code into ~/.local/share/mavrick/app
    print(f"\nInstalling application files into {STABLE_APP_DIR}...")
    ignore_items = {"__pycache__", ".git", "tests", "build", "dist", "*.pyc", "*.deb"}
    for item in os.listdir(SOURCE_DIR):
        if item in ignore_items or item.endswith(".deb") or item.endswith(".pyc"):
            continue
        src = os.path.join(SOURCE_DIR, item)
        dst = os.path.join(STABLE_APP_DIR, item)
        if os.path.isdir(src):
            shutil.copytree(src, dst, dirs_exist_ok=True, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
        elif os.path.isfile(src):
            shutil.copy2(src, dst)

    # Symlink ~/.local/share/mavrick/mavrick -> app for dual module compatibility
    if not os.path.exists(STABLE_MAVRICK_DIR):
        try:
            os.symlink("app", STABLE_MAVRICK_DIR)
        except OSError:
            pass

    # 3. Create stable binary wrapper in ~/.local/bin/mavrick
    wrapper_content = f"""#!/usr/bin/env bash
# MAVRICK Executable Launcher
export PATH="$HOME/.npm-global/bin:$HOME/.local/bin:/usr/local/bin:/usr/bin:/bin:$PATH"
export PYTHONPATH="{STABLE_DIR}:{STABLE_APP_DIR}:$PYTHONPATH"
exec /usr/bin/python3 -m mavrick.cli "$@"
"""
    with open(BIN_PATH, "w", encoding="utf-8") as fp:
        fp.write(wrapper_content)
    os.chmod(BIN_PATH, 0o755)
    print(f"  ✓ Installed wrapper binary: {BIN_PATH}")

    # 4. Register Desktop Application
    desktop_src = os.path.join(SOURCE_DIR, "desktop", "mavrick.desktop")
    if os.path.exists(desktop_src):
        shutil.copy2(desktop_src, DESKTOP_FILE)
        os.chmod(DESKTOP_FILE, 0o644)
        if shutil.which("update-desktop-database"):
            subprocess.run(["update-desktop-database", DESKTOP_DIR], capture_output=True)
        print(f"  ✓ Registered desktop application: {DESKTOP_FILE}")

    # 5. Auto-enable Systemd Services
    setup_systemd_services()

    # 6. Configure COSMIC Shortcut
    print("\nConfiguring COSMIC Desktop shortcuts...")
    configure_cosmic_shortcut()

    print("\n=========================================================")
    print("  ✓ MAVRICK installation complete!")
    print("  Run 'mavrick' in your terminal or press Super + M on your desktop.")
    print("=========================================================")

def uninstall():
    print("=========================================================")
    print("  Uninstalling MAVRICK from user environment...")
    print("=========================================================")

    # Stop and disable systemd user services
    if shutil.which("systemctl"):
        print("Stopping and disabling systemd user services...")
        subprocess.run(["systemctl", "--user", "disable", "--now", "mavrickd.service"], capture_output=True)
        subprocess.run(["systemctl", "--user", "disable", "--now", "mavrick.service"], capture_output=True)

    for sname in ["mavrick.service", "mavrickd.service"]:
        sfile = os.path.join(SYSTEMD_USER_DIR, sname)
        if os.path.exists(sfile):
            try:
                os.unlink(sfile)
                print(f"  ✓ Removed {sfile}")
            except Exception as e:
                print(f"  ⚠️ Error removing {sfile}: {e}")

    if shutil.which("systemctl"):
        subprocess.run(["systemctl", "--user", "daemon-reload"], capture_output=True)

    # Remove binary
    if os.path.exists(BIN_PATH):
        try:
            os.unlink(BIN_PATH)
            print(f"  ✓ Removed {BIN_PATH}")
        except Exception as e:
            print(f"  ⚠️ Error removing binary: {e}")

    # Remove desktop entry
    if os.path.exists(DESKTOP_FILE):
        try:
            os.unlink(DESKTOP_FILE)
            if shutil.which("update-desktop-database"):
                subprocess.run(["update-desktop-database", DESKTOP_DIR], capture_output=True)
            print(f"  ✓ Removed {DESKTOP_FILE}")
        except Exception as e:
            print(f"  ⚠️ Error removing desktop file: {e}")

    # Remove app code and symlink
    if os.path.islink(STABLE_MAVRICK_DIR):
        try:
            os.unlink(STABLE_MAVRICK_DIR)
        except Exception:
            pass

    if os.path.exists(STABLE_APP_DIR):
        try:
            shutil.rmtree(STABLE_APP_DIR)
            print(f"  ✓ Removed application files: {STABLE_APP_DIR}")
        except Exception as e:
            print(f"  ⚠️ Error removing app dir: {e}")

    print("\n✓ MAVRICK has been uninstalled successfully.")

def main():
    parser = argparse.ArgumentParser(description="MAVRICK Installer for Pop!_OS")
    parser.add_argument("-y", "--yes", action="store_true", help="Automatically accept prompts to install optional utilities")
    parser.add_argument("--uninstall", action="store_true", help="Uninstall MAVRICK")
    args = parser.parse_args()

    if args.uninstall:
        uninstall()
    else:
        install(auto_yes=args.yes)

if __name__ == "__main__":
    main()
