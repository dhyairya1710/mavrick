#!/usr/bin/env python3
"""
MAVRICK Debian Package Builder for Pop!_OS / Ubuntu
Packages MAVRICK into a standard .deb package (mavrick_2.0.0_all.deb)
conforming to Debian/Ubuntu filesystem standards.
"""

import os
import sys
import shutil
import subprocess
import argparse
import tempfile
from pathlib import Path

SOURCE_DIR = Path(__file__).resolve().parent
RUNTIME_FILES = (
    "__init__.py", "ai.py", "cli.py", "config.py", "core.py", "daemon.py",
    "executor.py", "history.py", "ipc.py", "policy.py", "router.py", "schema.py", "workflows.py",
)
RUNTIME_PACKAGES = ("mcp", "platform", "providers", "tools", "ui")


def runtime_manifest() -> tuple[Path, ...]:
    modules = [Path(name) for name in RUNTIME_FILES]
    for package in RUNTIME_PACKAGES:
        modules.extend(
            path.relative_to(SOURCE_DIR)
            for path in sorted((SOURCE_DIR / package).rglob("*.py"))
            if not {"__pycache__", ".extract"}.intersection(path.parts)
        )
    return tuple(modules)


def get_mavrick_version() -> str:
    """Reads version from __init__.py or falls back to default."""
    try:
        init_file = SOURCE_DIR / "__init__.py"
        if init_file.exists():
            with open(init_file, "r", encoding="utf-8") as f:
                for line in f:
                    if line.startswith("__version__"):
                        return line.split("=")[1].strip().strip('"').strip("'")
    except Exception:
        pass
    return "2.0.0"

def create_control_file(staging_dir: Path, version: str) -> None:
    """Creates the DEBIAN/control file with proper Debian metadata and dependencies."""
    debian_dir = staging_dir / "DEBIAN"
    debian_dir.mkdir(parents=True, exist_ok=True)

    control_content = f"""Package: mavrick
Version: {version}
Section: utils
Priority: optional
Architecture: all
Maintainer: MAVRICK Team <support@system76.com>
Depends: python3 (>= 3.10), python3-gi, gir1.2-gtk-3.0, libnotify-bin, playerctl
Recommends: system76-power
Suggests: fontconfig
Description: Minimalist Autonomous AI Operating System Hub for Pop!_OS
 MAVRICK is a production-grade, keyboard-first AI operating system
 assistant designed for Pop!_OS (COSMIC Desktop, Wayland). It operates
 through natural human language with zero rigid keywords, while enforcing
 a strict controlled autonomy security model.
"""
    control_path = debian_dir / "control"
    control_path.write_text(control_content, encoding="utf-8")
    os.chmod(control_path, 0o644)

def create_maintainer_scripts(staging_dir: Path) -> None:
    """Creates postinst and prerm maintainer scripts with executable permissions."""
    debian_dir = staging_dir / "DEBIAN"
    debian_dir.mkdir(parents=True, exist_ok=True)

    postinst_content = """#!/bin/sh
set -e

if [ "$1" = "configure" ]; then
    # Update desktop entry database
    if command -v update-desktop-database >/dev/null 2>&1; then
        update-desktop-database -q /usr/share/applications || true
    fi

    # Reload systemd system daemon
    if command -v systemctl >/dev/null 2>&1; then
        systemctl --system daemon-reload 2>/dev/null || true
    fi
fi

exit 0
"""
    postinst_path = debian_dir / "postinst"
    postinst_path.write_text(postinst_content, encoding="utf-8")
    os.chmod(postinst_path, 0o755)

    prerm_content = """#!/bin/sh
set -e

if [ "$1" = "remove" ] || [ "$1" = "deconfigure" ]; then
    # Stop user services gracefully if active
    if command -v systemctl >/dev/null 2>&1; then
        systemctl --user stop mavrick.service mavrickd.service 2>/dev/null || true
    fi
fi

exit 0
"""
    prerm_path = debian_dir / "prerm"
    prerm_path.write_text(prerm_content, encoding="utf-8")
    os.chmod(prerm_path, 0o755)

def stage_payload_files(staging_dir: Path) -> None:
    """Copies all application, binary, desktop, doc, and systemd files into staging tree."""
    # 1. Target Directories
    usr_lib = staging_dir / "usr" / "lib" / "mavrick"
    usr_bin = staging_dir / "usr" / "bin"
    usr_apps = staging_dir / "usr" / "share" / "applications"
    usr_doc = staging_dir / "usr" / "share" / "doc" / "mavrick"
    usr_systemd = staging_dir / "usr" / "lib" / "systemd" / "user"

    usr_lib.mkdir(parents=True, exist_ok=True)
    usr_bin.mkdir(parents=True, exist_ok=True)
    usr_apps.mkdir(parents=True, exist_ok=True)
    usr_doc.mkdir(parents=True, exist_ok=True)
    usr_systemd.mkdir(parents=True, exist_ok=True)

    # 2. Files to include in /usr/lib/mavrick
    for module in runtime_manifest():
        destination = usr_lib / module
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(SOURCE_DIR / module, destination)

    desktop_src_dir = SOURCE_DIR / "desktop"
    if desktop_src_dir.exists():
        shutil.copytree(desktop_src_dir, usr_lib / "desktop", dirs_exist_ok=True,
                        ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))

    # Backward-compatible symlink so `app.xyz` resolves to `/usr/lib/mavrick`
    app_symlink = usr_lib / "app"
    if not app_symlink.exists():
        try:
            os.symlink(".", app_symlink)
        except OSError:
            pass

    # 3. Create /usr/bin/mavrick wrapper
    bin_wrapper = usr_bin / "mavrick"
    bin_wrapper_content = """#!/usr/bin/env bash
# MAVRICK Executable Entrypoint for Pop!_OS / Ubuntu
export PATH="$HOME/.npm-global/bin:$HOME/.local/bin:/usr/local/bin:/usr/bin:/bin:$PATH"
export PYTHONPATH="/usr/lib/mavrick:/usr/lib:$PYTHONPATH"
exec /usr/bin/python3 -m mavrick.cli "$@"
"""
    bin_wrapper.write_text(bin_wrapper_content, encoding="utf-8")
    os.chmod(bin_wrapper, 0o755)

    # 4. Install /usr/share/applications/mavrick.desktop
    desktop_file = SOURCE_DIR / "desktop" / "mavrick.desktop"
    desktop_dst = usr_apps / "mavrick.desktop"
    if desktop_file.exists():
        shutil.copy2(desktop_file, desktop_dst)
    else:
        desktop_dst.write_text("""[Desktop Entry]
Name=Mavrick
GenericName=AI System Assistant
Comment=Autonomous Pop!_OS assistant powered by Antigravity
Exec=mavrick
Icon=system-search
Terminal=false
Type=Application
Categories=Utility;System;Core;
Keywords=ai;jarvis;mavrick;antigravity;assistant;spotlight;split;
StartupNotify=false
""", encoding="utf-8")
    os.chmod(desktop_dst, 0o644)

    # 5. Documentation in /usr/share/doc/mavrick
    readme_file = SOURCE_DIR / "README.md"
    if readme_file.exists():
        shutil.copy2(readme_file, usr_doc / "README.md")
        os.chmod(usr_doc / "README.md", 0o644)

    # 6. Install systemd user services to /usr/lib/systemd/user/
    mavrick_service_src = SOURCE_DIR / "desktop" / "mavrick.service"
    mavrickd_service_src = SOURCE_DIR / "desktop" / "mavrickd.service"

    mavrick_service_dst = usr_systemd / "mavrick.service"
    mavrickd_service_dst = usr_systemd / "mavrickd.service"

    if mavrick_service_src.exists():
        shutil.copy2(mavrick_service_src, mavrick_service_dst)
    else:
        mavrick_service_dst.write_text("""[Unit]
Description=MAVRICK Desktop AI Operating System Assistant
After=graphical-session.target

[Service]
Type=simple
Environment="PATH=%h/.npm-global/bin:%h/.local/bin:/usr/local/bin:/usr/bin:/bin"
Environment="PYTHONPATH=/usr/lib/mavrick:/usr/lib"
ExecStart=/usr/bin/python3 -m mavrick.ui
Restart=on-failure
RestartSec=2

[Install]
WantedBy=default.target
""", encoding="utf-8")
    os.chmod(mavrick_service_dst, 0o644)

    if mavrickd_service_src.exists():
        shutil.copy2(mavrickd_service_src, mavrickd_service_dst)
    else:
        mavrickd_service_dst.write_text("""[Unit]
Description=MAVRICK AI Intelligence Daemon (mavrickd)
After=default.target

[Service]
Type=simple
Environment="PATH=%h/.npm-global/bin:%h/.local/bin:/usr/local/bin:/usr/bin:/bin"
Environment="PYTHONPATH=/usr/lib/mavrick:/usr/lib"
ExecStart=/usr/bin/python3 -m mavrick.daemon
Restart=always
RestartSec=2

[Install]
WantedBy=default.target
""", encoding="utf-8")
    os.chmod(mavrickd_service_dst, 0o644)

    # 7. Normalize file and directory permissions
    for root, dirs, files in os.walk(staging_dir):
        for d in dirs:
            os.chmod(os.path.join(root, d), 0o755)
        for f in files:
            fpath = os.path.join(root, f)
            if fpath.endswith(("/usr/bin/mavrick", "/DEBIAN/postinst", "/DEBIAN/prerm")):
                os.chmod(fpath, 0o755)
            elif "/DEBIAN/" not in fpath or f == "control":
                if not os.access(fpath, os.X_OK):
                    os.chmod(fpath, 0o644)

def build_deb_package(staging_dir: Path, output_file: Path) -> bool:
    """Executes dpkg-deb to package the directory into a .deb file."""
    if not shutil.which("dpkg-deb"):
        print("✗ Error: 'dpkg-deb' utility is not installed on this system.")
        return False

    cmd = ["dpkg-deb", "--build", "--root-owner-group", str(staging_dir), str(output_file)]
    try:
        subprocess.run(cmd, capture_output=True, text=True, check=True)
        return True
    except subprocess.CalledProcessError as e:
        print(f"✗ dpkg-deb failed: {e.stderr}")
        return False

def verify_runtime_modules(deb_path: Path) -> int:
    """Extract the built artifact and compare the full runtime module payload."""
    with tempfile.TemporaryDirectory(prefix="mavrick-deb-verify-") as directory:
        subprocess.run(["dpkg-deb", "-x", str(deb_path), directory], check=True, capture_output=True)
        payload = Path(directory) / "usr" / "lib" / "mavrick"
        expected = set(runtime_manifest())
        actual = {module.relative_to(payload) for module in payload.rglob("*.py")}
        # Desktop resources are copied separately and are not application modules.
        actual = {module for module in actual if module.parts[0] != "desktop"}
        missing, extra = expected - actual, actual - expected
        changed = {module for module in expected & actual
                   if (payload / module).read_bytes() != (SOURCE_DIR / module).read_bytes()}
        if missing or extra or changed:
            raise ValueError(f"Runtime module verification failed. Missing: {sorted(missing)}. "
                             f"Extra: {sorted(extra)}. Changed: {sorted(changed)}.")
        return len(expected)


def verify_deb_package(deb_path: Path) -> None:
    """Inspect control data and verify runtime files from the built artifact."""
    module_count = verify_runtime_modules(deb_path)
    print("\nPackage Verification:")
    print(f"Verified {module_count} runtime modules against source.")
    print("---------------------------------------------------------")
    try:
        # Check control info
        ctrl_res = subprocess.run(["dpkg-deb", "-I", str(deb_path)], capture_output=True, text=True, check=True)
        print("Control Metadata:")
        for line in ctrl_res.stdout.strip().splitlines()[:14]:
            print(f"  {line}")

        # Check file listings
        content_res = subprocess.run(["dpkg-deb", "-c", str(deb_path)], capture_output=True, text=True, check=True)
        print("\nInstalled Files Summary:")
        lines = content_res.stdout.strip().splitlines()
        for line in lines:
            parts = line.split()
            if len(parts) >= 6:
                perm = parts[0]
                path = parts[-1]
                if not path.endswith("/"):
                    print(f"  {perm}  {path}")
        print(f"\nTotal packaged entries: {len(lines)}")
    except Exception as e:
        print(f"  ⚠️ Verification check encountered an issue: {e}")

def main():
    parser = argparse.ArgumentParser(description="Build Debian package (.deb) for MAVRICK")
    parser.add_argument("--version", type=str, default=None, help="Package version (defaults to __version__)")
    parser.add_argument("--output-dir", type=str, default=str(SOURCE_DIR), help="Output directory for .deb file")
    parser.add_argument("--keep-build", action="store_true", help="Keep temporary build staging directory")
    args = parser.parse_args()

    version = args.version or get_mavrick_version()
    output_dir = Path(args.output_dir).resolve()
    output_dir.mkdir(parents=True, exist_ok=True)

    package_filename = f"mavrick_{version}_all.deb"
    output_file = output_dir / package_filename

    print("=========================================================")
    print(f"  Building Debian Package: {package_filename}")
    print(f"  Version: {version} | Architecture: all")
    print(f"  Output:  {output_file}")
    print("=========================================================")

    # Temporary staging directory
    temp_dir = tempfile.mkdtemp(prefix="mavrick_deb_")
    staging_dir = Path(temp_dir) / f"mavrick_{version}_all"
    staging_dir.mkdir(parents=True, exist_ok=True)

    try:
        print("[1/4] Generating DEBIAN/control and maintainer scripts...")
        create_control_file(staging_dir, version)
        create_maintainer_scripts(staging_dir)

        print("[2/4] Staging payload into /usr/lib, /usr/bin, /usr/share, /usr/lib/systemd...")
        stage_payload_files(staging_dir)

        print("[3/4] Compiling package with dpkg-deb...")
        with tempfile.TemporaryDirectory(prefix=".mavrick-deb-", dir=output_dir) as build_dir:
            candidate = Path(build_dir) / package_filename
            if not build_deb_package(staging_dir, candidate):
                sys.exit(1)
            print("[4/4] Verifying generated package...")
            verify_deb_package(candidate)
            candidate.replace(output_file)
        size_kb = output_file.stat().st_size / 1024
        print(f"  ✓ Created: {output_file} ({size_kb:.1f} KB)")

        print("\n=========================================================")
        print(f"  ✓ Successfully built {package_filename}")
        print("  To install on Pop!_OS / Ubuntu:")
        print(f"    sudo apt install {output_file}")
        print("    # Or:")
        print(f"    sudo dpkg -i {output_file} && sudo apt-get install -f")
        print("=========================================================")

    finally:
        if not args.keep_build:
            shutil.rmtree(temp_dir, ignore_errors=True)
        else:
            print(f"Staging directory kept at: {staging_dir}")

if __name__ == "__main__":
    main()
