# MAVRICK 2.0 — Production AI Operating System Assistant
> **Autonomous, Safe & Keyboard-First for Pop!_OS / COSMIC / Wayland**  
> Instant local commands · Antigravity, Ollama, or OpenAI-compatible AI · Albert Sans/Typography

**MAVRICK** is a production-grade, keyboard-first AI operating system assistant designed for Pop!_OS (COSMIC Desktop, Wayland). It operates through natural human language with zero rigid keywords, while enforcing a strict **controlled autonomy** security model: arbitrary shell execution has been completely eliminated in favor of strictly-typed, validated tools with least-privilege confirmation.

---

## 🏛️ Architectural Overview

```
                        ┌──────────────────────────────────────────────┐
                        │            User Request (NL Input)           │
                        │    HUD (Super + M)  OR  CLI (mavrick "...")   │
                        └──────────────────────┬───────────────────────┘
                                               │
                                               ▼
                        ┌──────────────────────────────────────────────┐
                        │        Instant local router, then AI         │
                        │       agy / Ollama / OpenAI-compatible       │
                        └──────────────────────┬───────────────────────┘
                                               │ Structured JSON Action
                                               ▼
                        ┌──────────────────────────────────────────────┐
                        │          Schema & Parameter Validator        │
                        │  • Tool allowlist validation                 │
                        │  • Injection defense (blocks ; & | ` $ < >)  │
                        │  • Path safety (canonical user boundary)     │
                        └──────────────────────┬───────────────────────┘
                                               │
                                               ▼
                        ┌──────────────────────────────────────────────┐
                        │            Security Policy Engine            │
                        │  • READ-ONLY:    Run immediately             │
                        │  • REVERSIBLE:   Review card (Confirm/Undo)  │
                        │  • PRIVILEGED:   Mandatory Confirm + Terminal│
                        │  • DESTRUCTIVE:  Mandatory Explicit Confirm  │
                        └──────────────────────┬───────────────────────┘
                                               │
                                               ▼
                        ┌──────────────────────────────────────────────┐
                        │             Shared ActionExecutor            │
                        │  • Job lifecycle & thread-safe state         │
                        │  • Visible [Stop] cancellation               │
                        │  • Multi-monitor active placement (Wayland)  │
                        │  • Native desktop notifications (notify-send)│
                        │  • Outcome verification (post-launch checks) │
                        │  • Audit logging & Secret scrubbing          │
                        └──────────────────────┬───────────────────────┘
                                               │ Direct Argument Vectors (No shell=True)
                                               ▼
                        ┌──────────────────────────────────────────────┐
                        │           Typed Tool Implementations         │
                        │ • system: health, GPU, power, volume, lock   │
                        │ • media: playerctl play/pause, next, prev    │
                        │ • network: interface, route IP, ping test    │
                        │ • maintenance: check updates, clean cache    │
                        │ • files: list, organize_downloads, undo      │
                        │ • apps: open_app, open_url, arrange_windows  │
                        │ • packages: apt, flatpak interactive terms   │
                        └──────────────────────────────────────────────┘
```

---

## 🔒 Trust Model & Security Architecture

1. **Zero Arbitrary Shell Execution**:
   - The model is strictly prohibited from generating raw executable bash commands.
   - All operations are dispatched through typed Python tools using explicit argument vectors (`subprocess.run([binary, arg1, ...])` or direct kernel interfaces). `shell=True` has been completely eliminated.
2. **Action Risk Tiers & Explicit Confirmation**:
   - **`READ_ONLY`**: Metric inspections (`get_system_health`, `get_network_status`, `check_updates`, `inspect_processes`, `list_files`) run immediately.
   - **`REVERSIBLE`** actions generally require confirmation. Move, Organize Downloads, and Undo always show the exact file rows for selection. Undo applies to recorded file moves.
   - **`PRIVILEGED`**: Package management (`install_package`, `uninstall_package`) always requires explicit confirmation and launches an interactive terminal window (`cosmic-term`). MAVRICK never silently requests or escalates `sudo` privileges.
   - **`DESTRUCTIVE`** actions such as clearing caches and locking the session require explicit confirmation.
3. **Prompt Injection & Parameter Sanitization**:
   - Model responses are treated as untrusted data.
   - The shared executor validates typed parameters and resolves approved application IDs before launch or confirmation. Application and terminal proposals carry no free-form arguments.
4. **Path Boundary Security**:
   - File moves use literal paths under the permitted user directories, refuse symlinked parent directories, and use an atomic no-replace rename. Cross-filesystem moves fail safely.
   - Access to system roots (`/etc`, `/bin`, `/usr`, `/root`, `/sys`, `/proc`) is strictly blocked.
5. **Private On-Disk State**:
   - Directory `~/.local/share/mavrick/` is enforced at permission mode **`0700`**.
   - State files (`history.json`, `audit.log`, `context.json`, `config.json`) are enforced at permission mode **`0600`**.
   - All API keys, passwords, bearer tokens, and credentials are automatically scrubbed and redacted before storage.
   - User command/request history is kept separate from detailed execution audit logs.
   - Telemetry policy: No user files, outputs, secrets, or browser contents are sent to the model without explicit user approval.

---

## 🐧 Pop!_OS Native Capabilities

MAVRICK is deeply integrated into Pop!_OS (COSMIC Desktop, Wayland):

- **🎮 GPU Telemetry**: Live metrics for NVIDIA GPUs (via `nvidia-smi`: utilization %, VRAM used/total, temperature °C) and AMD GPUs (via `/sys/class/drm/card*/device/gpu_busy_percent`) embedded in system health reports.
- **⚡ System76 Power Profiles**: Seamlessly switch between `battery`, `balanced`, and `performance` power profiles via native `system76-power` (with graceful `powerprofilesctl` fallback).
- **🎵 Native Media Playback**: Control background media players (Spotify, Brave, Chrome, VLC) via `playerctl` (`pause`, `play`, `next`, `previous`, `status`) and display current track title/artist.
- **🌐 Network Diagnostics**: Inspect default network route, local network interface, and 1-hop latency ping test (`1.1.1.1`).
- **🧹 System Updates & Cache Cleaning**: Check upgradable packages across `apt` and `flatpak`, and safely preview / reclaim disk space in thumbnail caches (`~/.cache/thumbnails`) and package caches.
- **🖥️ Multi-Monitor Active HUD Placement**: Under Wayland / COSMIC, MAVRICK detects pointer coordinates via `Gdk.Seat` to center the floating HUD directly on the display the user is actively working on.
- **🔔 Native Desktop Notifications**: Long-running actions (>1.5s) or background operations trigger desktop notifications via `notify-send`.
- **✨ Albert Sans Typography**: Automatic download and caching of Google's Albert Sans variable font into `~/.local/share/fonts/albertsans/` ensures 100% pixel-perfect obsidian rendering out of the box.

---

## 🛠️ Supported Structured Tools

| Tool | Risk Level | Description | Pop!_OS Integration | Verification |
| :--- | :--- | :--- | :--- | :--- |
| `get_system_health` | `READ_ONLY` | Reads CPU load, RAM usage, root disk space, battery status, and NVIDIA/AMD GPU metrics. | `nvidia-smi` / `/sys/class/drm` | Kernel `/proc` & GPU driver check |
| `set_power_profile` | `REVERSIBLE` | Switches system power mode (`battery`, `balanced`, `performance`). | `system76-power profile` | Power profile query |
| `control_media` | `REVERSIBLE` | Controls media playback (`pause`, `play`, `next`, `previous`, `status`). | `playerctl` MPRIS bus | Track status query |
| `get_network_status`| `READ_ONLY` | Reports default route IP, local interface, and ping test latency. | `ip route` & `ping -c 1` | Ping exit code check |
| `check_updates` | `READ_ONLY` | Inspects pending system upgrades across APT and Flatpak. | `apt list --upgradable` & `flatpak` | Upgradable package count |
| `clean_cache` | `READ_ONLY` preview, `DESTRUCTIVE` apply | Calculates reclaimable space and purges thumbnail & APT caches with dry-run safety. | `~/.cache/thumbnails` & APT | Reclaimed byte delta |
| `inspect_processes` | `READ_ONLY` | Lists top processes ordered by memory (RSS) or CPU load. | Native Linux `ps` | Direct `ps` table parse |
| `list_files` | `READ_ONLY` | Tree directory listing with file counts and human-readable sizes (KB/MB). | Canonical path sandbox | Bounded `os.scandir` |
| `open_app` | `READ_ONLY` | Requests an approved application launch. Approved CLI utilities open in a terminal. | `.desktop` entries and terminal | Launcher result |
| `run_in_terminal` | `READ_ONLY` | Opens an approved utility such as `htop` in a terminal. | COSMIC Terminal | Launcher result |
| `open_url` | `READ_ONLY` | Opens valid `http`/`https` link in default or specified browser. | Default browser (`xdg-open`) | Browser launch request |
| `arrange_windows` | `READ_ONLY` | Requests two validated app or browser launches. | Desktop launchers | Individual launcher results |
| `set_volume` | `REVERSIBLE` | Adjusts default audio sink volume (0-150%) or mute state via `wpctl`. | WirePlumber PipeWire audio | `wpctl get-volume` query |
| `organize_downloads`| `REVERSIBLE` | Sorts loose files in Downloads into categorized folders. Includes dry-run review and undo. | Canonical user boundary | Post-move file verification |
| `undo` | `REVERSIBLE` | Reverts the last file organization or move operation, restoring files to original paths. | Atomic no-replace rename per entry | Individual restoration results |
| `install_package` | `PRIVILEGED` | Spawns interactive terminal for APT / Flatpak package installation with user oversight. | APT / Flatpak terminal | Package manager exit status and installed-state readback |
| `uninstall_package`| `PRIVILEGED` | Spawns interactive terminal for safe package removal after confirmation. | APT / Flatpak terminal | Package manager exit status and installed-state readback |
| `lock_session` | `DESTRUCTIVE` | Locks desktop session via `loginctl lock-session`. | Pop!_OS session manager | Return code check |

---

## 📦 Debian Packaging (`.deb`)

MAVRICK includes a standalone packaging engine (`package_deb.py`) that builds a Debian/Ubuntu-standard package (`mavrick_2.0.0_all.deb`).

### Building the `.deb` Package

```bash
cd mavrick
python3 package_deb.py
```

Options:
- `--version <ver>`: Override version string (default: reads `mavrick.__version__`).
- `--output-dir <dir>`: Target directory for the generated `.deb` file (default: repository root).
- `--keep-build`: Preserve temporary build staging directory for inspection.

### Package Architecture & Contents

The generated `.deb` package installs to standard Debian paths:
- **`/usr/lib/mavrick/`**: Core application runtime, modules, and subpackages.
- **`/usr/bin/mavrick`**: Global executable launcher wrapper.
- **`/usr/share/applications/mavrick.desktop`**: Desktop application entry.
- **`/usr/share/doc/mavrick/README.md`**: Package documentation.
- **`/usr/lib/systemd/user/mavrick.service`**: Systemd user service for the desktop HUD.
- **`/usr/lib/systemd/user/mavrickd.service`**: Systemd user service for the warm background AI daemon.

Package control dependencies:
- **`Depends`**: `python3 (>= 3.10)`, `python3-gi`, `gir1.2-gtk-3.0`, `libnotify-bin`, `playerctl`
- **`Recommends`**: `system76-power`
- **`Suggests`**: `fontconfig`

### Installing the `.deb` Package

```bash
sudo apt install ./mavrick_2.0.0_all.deb
# Or using dpkg:
sudo dpkg -i ./mavrick_2.0.0_all.deb && sudo apt-get install -f
```

To enable the user daemon on boot:
```bash
systemctl --user enable --now mavrickd.service
```

### Removing the Package

```bash
sudo apt remove mavrick
# Or to purge all configuration:
sudo apt purge mavrick
```

---

## Cross-platform headless runtime

The Python package exposes `mavrick` and `mavrickd`. The headless CLI, daemon, calculator, and providers can load without GTK. The Debian package and COSMIC shortcut are Linux integrations.

| Host | State and runtime | App launch and packages | Desktop primitives |
|---|---|---|---|
| Linux | `~/.local/share/mavrick` and the existing XDG Unix socket, with a state-directory fallback | Desktop IDs and approved binaries, APT and Flatpak | Existing COSMIC/Linux telemetry and tools, systemd, XDG open, display clipboard tools |
| macOS | `~/Library/Application Support/Mavrick`, `~/Library/Caches/Mavrick`, `~/Library/Logs/Mavrick`, private Unix runtime under `/tmp` | App bundles via `open -a`, Homebrew | `launchctl`, `open`, `pbcopy`, system and disk information |
| Windows | `%APPDATA%\Mavrick` for config and `%LOCALAPPDATA%\Mavrick` for data, cache, and runtime | Start Menu shortcuts and PATH executables, Winget with Scoop detection | Task Scheduler or service query, file associations, `clip.exe` or PowerShell, system and disk information |

Windows uses a protected state ACL and byte-range file locks. Linux and macOS use POSIX modes and file locks. Verified move and Undo apply use native no-replace operations on Linux and macOS. On Windows, file listing and Downloads preview are available and Apply reports that verified moves are unavailable. Destructive cache cleanup and the Linux-specific volume, media, process, power, and network tools remain Linux features. Windows and macOS can preview Mavrick cache targets. The native Windows and macOS integrations need verification on their respective systems.

### Local daemon transport

Linux and macOS default to `unix-socket`. Windows defaults to `tcp-token`. Set `MAVRICK_IPC_TRANSPORT=tcp-token` for both the daemon and client to opt in on a POSIX host, or add `"ipc": {"transport": "tcp-token"}` to Mavrick's config. The daemon also accepts `--transport` and `--descriptor`. To use a custom descriptor, both endpoints must pick the same absolute location via config or `MAVRICK_IPC_DESCRIPTOR`. On Windows the descriptor stays within the user's runtime directory.

TCP binds only to `127.0.0.1` and lets the OS choose a port. The daemon atomically publishes its address, PID, and a fresh random token in a private runtime directory. The client authenticates every request. Both modes use the same 2 MiB NDJSON limit, negotiated query budget, and disconnect cancellation. `mavrick --doctor` reports the selected platform, IPC transport, package manager, and backend status.

## 🚀 Linux User Space Installation

For local development or single-user installation without root privileges:

Use `install.py` for an interactive user install or uninstall. `install.sh` is a retained shell-based alternative for existing workflows. Both target the Linux/COSMIC user installation and configure the same two units. `mavrick.service` runs the graphical palette. `mavrickd.service` runs the separate headless inference daemon. Debian packages ship both units.

### Interactive Python Installer

```bash
cd mavrick
python3 install.py
```

Pass `-y` to automatically install missing optional utilities:
```bash
python3 install.py -y
```

### Shell Installer

```bash
cd mavrick
./install.sh
```

The installer:
1. Verifies core prerequisites (Python 3.10+, PyGObject/GTK3, Antigravity core).
2. Checks and prompts for optional Pop!_OS utilities (`playerctl`, `system76-power`, `libnotify-bin`).
3. Automatically downloads and caches `Albert Sans` font into `~/.local/share/fonts/albertsans/` and runs `fc-cache -f`.
4. Copies codebase into `~/.local/share/mavrick/app` and links `~/.local/bin/mavrick`.
5. Registers desktop application `~/.local/share/applications/mavrick.desktop`.
6. Installs and auto-enables user systemd services:
   - `mavrickd.service`: Persistent warm daemon with sub-second AI latency.
   - `mavrick.service`: Floating desktop HUD.
7. Registers global hotkey **<kbd>Super</kbd> + <kbd>M</kbd>** in COSMIC desktop settings.

### Clean User Uninstallation

```bash
mavrick --uninstall
# Or:
python3 install.py --uninstall
```

---

## ⌨️ CLI & Keyboard Reference

### Global Desktop Shortcut
- **<kbd>Super</kbd> + <kbd>M</kbd>**: Toggle floating MAVRICK spotlight HUD on active monitor.

### In-HUD Controls
- <kbd>Enter</kbd> in the input: Submit the query
- <kbd>Up</kbd> / <kbd>Down</kbd>: Cycle through recent query history in search entry
- <kbd>Esc</kbd>: Request Stop during an active operation, cancel a review, or dismiss the current view
- <kbd>Ctrl</kbd> + <kbd>H</kbd>: Toggle activity history view
- <kbd>Ctrl</kbd> + <kbd>Z</kbd>: Review Undo for the last file batch
- <kbd>Ctrl</kbd> + <kbd>L</kbd>: Clear screen and reset HUD
- **`[✎ Edit]`**: Return action command to search entry for parameter adjustments
- **Stop** requests cancellation. File jobs finish the current entry and offer Undo for the entries already moved. For an active package manager, finish or cancel the operation in Terminal.
- **`[🗑 Clear History]`**: Securely purge request history and session context

### Browse Commands

The compact palette shows three suggestions and a searchable **Browse commands** panel. In the browser, use <kbd>Up</kbd> and <kbd>Down</kbd> to select a command and <kbd>Enter</kbd> to fill the input. Press <kbd>Enter</kbd> in the main input to submit. File reviews and results expand the palette. Drag the lower-right corner to set a manual size for the HUD session.

### Organize Downloads and Undo

Run `organize downloads` to scan loose files. Review each source and destination, exclude any files you want to leave in place, and choose **Move N files**. Use **Rename** in the HUD or `rename N NAME` in the CLI to resolve an occupied destination. If a reviewed source or destination changes, Mavrick highlights the affected rows for another review.

After a move, use the Undo button, <kbd>Ctrl</kbd> + <kbd>Z</kbd>, or `mavrick --undo` to preview restoration. The CLI reports a batch ID that can also be passed to `mavrick --undo ID`. Conflicted or skipped Undo entries stay in the batch.

### Coding Workspace

Set `coding_workspace` in `~/.local/share/mavrick/config.json` to use a preferred project and editor, for example `"coding_workspace": {"project": "~/Projects/my-project", "editor": "code"}`. You can also ask `open my coding workspace at ~/Projects/my-project`. With no configured path, Mavrick looks for one Git project under `~/Projects`, `~/Code`, or `~/Developer`. The review shows the project, the editor and terminal launch steps, and any unavailable apps.

### Command-Line Usage

```bash
# Natural language commands
mavrick "how much ram is free"
mavrick "gpu stats and temperature"
mavrick "switch power profile to battery"
mavrick "pause music"
mavrick "check network ping"
mavrick "check for system updates"
mavrick "clean cache"
mavrick "organize downloads"
mavrick "split screen youtube and reddit in brave"
mavrick "245 * 18.5"                 # Instant calculator
mavrick firefox                      # Exact installed app match
mavrick history                      # Instant activity view
mavrick "open claude"                 # Claude Desktop
mavrick "open claude in terminal"     # Claude Code in a terminal

# Privileged action with explicit prompt
mavrick "install htop"

# Skip routine reversible confirmation for an audio change
mavrick -y "turn it down a little"

# Diagnostics and state management
mavrick --health          # Real daemon socket & environment check
mavrick --doctor          # Provider, timeouts, socket, router, and provider health
mavrick --history         # Display recent activity log
mavrick --search-history "gpu"
mavrick --clear-history   # Securely purge history and context
mavrick --undo            # Review Undo for the most recent file batch
mavrick --workflows       # List saved workflows, step counts, and aggregate risk
mavrick --run-workflow system-check --dry-run  # Preview workflow steps and policy verdict
mavrick --mcp             # List configured MCP servers, policy tier, and approval status
mavrick --mcp-approve notes --grant-env NOTES_DIR --grant-readonly list_notes  # Record 0600 server grant
mavrick --setup --non-interactive --provider groq --theme dark  # Save provider & HUD theme (mode 0600)
mavrick --service-template macos  # Generate launchd plist (or linux systemd / windows task XML)
mavrick --uninstall       # Cleanly remove MAVRICK
```

### Instant Router

The HUD and CLI recognize complete installed application IDs, known app aliases, multi-app launches, arithmetic, `undo`, `history`, and saved workflow names locally with zero AI calls.
- **Multi-app instant launch**: Prefix one or more app names with `open`, `launch`, `start`, or `run` separated by `,`, `and`, `&`, or `+` (e.g., `open files, vlc and vscode` or `launch browser + terminal`). Matched apps launch immediately on **Enter** with no confirmation gate and report both `Launched: ...` and `Not found: ...` targets. Saved workflow slugs (`run <workflow-name>`) still take precedence over bare app verbs, and non-app mixed tasks such as `open firefox and clean downloads` or `open files in downloads and sort pdfs` fall through to the AI workflow planner.
- **Cached aliases**: `files` / `file manager` resolve across `cosmic-files`, `nautilus`, `dolphin`, and `thunar`; `vscode` / `vs code` / `code` resolve to VS Code; `terminal` and `browser` select the installed system terminal or browser.
- The HUD previews a cached app match, multi-app launch, saved workflow, or calculation as you type. Press **Enter** to launch or copy the calculation.

### Saved Workflows and Batch Undo

Create `workflows.json` next to `config.json` in the platform config directory. Mavrick reads this file on demand. To save a recipe, give it a lowercase slug, an optional description, and one to eight typed action steps.

```json
{
  "workflows": [
    {
      "name": "system-check",
      "description": "Check local health and estimate reclaimable cache space",
      "steps": [
        {"action": "get_system_health", "params": {}},
        {"action": "clean_cache", "params": {"dry_run": true}}
      ]
    }
  ]
}
```

Enter `/system-check` or `run system-check` in the HUD, or use `mavrick --workflows` and `mavrick --run-workflow system-check` from the CLI. Add `--dry-run` to print the exact resolved steps and policy verdict without executing. Saved recipes resolve locally. The executor validates every step and uses the highest risk across read-only, mutating, privileged, and destructive. Any invalid or unsupported step blocks the entire plan. Mutating workflows require a review even with `--yes`.

The review includes exact file pairs. A file step with unresolved conflicts or paths that overlap an earlier move is blocked during preflight. Use the standalone file review for those moves or to choose individual files. During a run, steps execute in order and stop on failure or cancellation. Completed steps and partial file changes are reported.

Use **Undo**, **Ctrl+Z**, `mavrick undo`, or `mavrick --undo BATCH_ID` to review recorded file moves from a workflow. Undo restores them in reverse order. Conflicts preserve both files and their receipts while available rows can still be restored. Package changes, launches, and other steps without a durable inverse are outside batch Undo.

### AI Providers

Add an `ai_provider` object to `~/.local/share/mavrick/config.json`. The default provider is `agy` (Antigravity CLI/daemon; interactive turns typically take 20–40s). For sub-second interactive HUD queries, configure one of the built-in fast presets (`groq`, `cerebras`, `openrouter`, or local `ollama`), or any `openai_compat` endpoint—all implemented with the Python standard library.

```json
{"ai_provider": {"name": "groq", "model": "openai/gpt-oss-20b"}}
```

| Preset | Default model | Default base URL | Host-scoped key env |
|---|---|---|---|
| `agy` | `gemini-3.8-flash-low` | *(local daemon / CLI)* | *(local CLI)* |
| `groq` | `openai/gpt-oss-20b` | `https://api.groq.com/openai/v1` | `GROQ_API_KEY` |
| `cerebras` | `gpt-oss-120b` | `https://api.cerebras.ai/v1` | `CEREBRAS_API_KEY` |
| `openrouter` | `openai/gpt-4o-mini` | `https://openrouter.ai/api/v1` | `OPENROUTER_API_KEY` |
| `ollama` | `llama3.2` | `http://127.0.0.1:11434` | *(local loopback)* |
| `openai_compat` | `gpt-4o-mini` | `https://api.openai.com/v1` | `OPENAI_API_KEY` |

| Setting | Environment override | CLI override |
|---|---|---|
| Provider | `MAVRICK_PROVIDER` | `--provider agy\|groq\|cerebras\|openrouter\|ollama\|openai_compat` |
| Model | `MAVRICK_MODEL` | `--model MODEL` |
| API root | `MAVRICK_BASE_URL` | `--base-url URL` |
| API key | `MAVRICK_API_KEY` | Set via environment or the mode-`0600` config's `api_key` field |

Provider-specific environment keys (`GROQ_API_KEY`, `CEREBRAS_API_KEY`, `OPENROUTER_API_KEY`, `OPENAI_API_KEY`) are only sent over HTTPS to their respective official hostnames; `MAVRICK_API_KEY` overrides any HTTP preset. Credentials sent outside loopback require HTTPS, and all keys are redacted from logs and history.

```bash
mavrick --doctor --provider groq
mavrick --doctor --provider ollama --model llama3.2
mavrick --provider openai_compat --base-url http://127.0.0.1:1234/v1 --model local-model "find large PDFs"
```

The HUD identifies results as **Instant**, **Instant · Workflow**, **agy**, **Groq**, **Cerebras**, **OpenRouter**, **Ollama**, or **OpenAI-Compatible**. `mavrick --doctor` displays the active provider/model and emits a latency hint when `agy` is selected.

### HUD Theme & UI Backend (`MAVRICK_UI_BACKEND`)

Set `"theme": "dark"` in `~/.local/share/mavrick/config.json`, run `mavrick --setup --theme dark`, or export `MAVRICK_THEME=dark` to switch between the default `Open Design` light linen palette and the high-contrast monochrome editorial dark palette (matte charcoal canvas, subtle borders, crisp off-white typography, restrained amber/crimson status accents).

The desktop HUD automatically selects **GTK3** on Linux when PyGObject is available and falls back to the zero-dependency **Tkinter Spotlight HUD** on Windows, macOS, or headless/non-GTK systems (`MAVRICK_UI_BACKEND=auto|gtk|tk`). Both backends share `HUDController` (`mavrick.ui.controller`). Generate declarative background-daemon startup definitions with `mavrick --service-template linux|macos|windows` (`systemd --user`, `launchd` `.plist`, or Windows Task Scheduler XML).

### AI Timeouts

Add an `ai_timeouts` object to `~/.local/share/mavrick/config.json` to override the defaults in seconds.

```json
{"ai_timeouts": {"init_timeout": 10, "query_timeout": 35, "fallback_timeout": 20}}
```

`MAVRICK_INIT_TIMEOUT`, `MAVRICK_QUERY_TIMEOUT`, and `MAVRICK_FALLBACK_TIMEOUT` override this file in the process that reads them. The daemon accepts `--init-timeout` and `--query-timeout`. A Mavrick CLI query accepts `--init-timeout` and `--fallback-timeout` for its local fallback, and `--query-timeout` to request a lower daemon cap. CLI flags take precedence over the environment. The daemon advertises the effective query budget, including queueing and worker startup. The client adds two seconds for IPC.

HTTP queries use `query_timeout` as one deadline across resolution, connection, TLS, and response reads. **Stop** or **Esc** cancels the local request. A remote service may continue generation after the connection closes.

### MCP Servers (Dual-Era Stdio)

Create `mcp_servers.json` next to `config.json` to connect local stdio Model Context Protocol servers without adding third-party runtime dependencies. Mavrick probes servers with the `2026-07-28` `server/discover` request (with `_meta`) and falls back automatically to legacy `initialize` -> `notifications/initialized` servers.

```json
{
  "servers": {
    "notes": {
      "command": "python3",
      "args": ["/path/to/notes_mcp.py"],
      "env": {"NOTES_DIR": "/home/user/notes"},
      "env_passthrough": ["NOTES_PROFILE"],
      "trusted": false,
      "allowed_tools": ["list_notes", "search_notes"],
      "blocked_tools": ["delete_note"],
      "timeout": 10.0
    }
  }
}
```

- **Consent-First Grants (`mcp_grants.json`, mode `0600`):** Mavrick never spawns an MCP server during discovery (`--mcp`, `--doctor`, or planning) until you explicitly approve its exact resolved executable, arguments, working directory, and environment variable names via `mavrick --mcp-approve <server> [--grant-env VAR1,VAR2] [--grant-readonly T1,T2]`. Modifying server commands, args, cwd, or granted env vars invalidates the launch hash until re-approved.
- **Scoped Environment & Loader Guards:** Child servers inherit only minimal safe base variables (`PATH`, `HOME`, `XDG_*`, `LANG`, etc.) unless keys are listed in `env` / `env_passthrough` and explicitly granted. Dangerous runtime-loader and interpreter injection variables (`LD_PRELOAD`, `LD_LIBRARY_PATH`, `DYLD_*`, `PYTHONPATH`, `NODE_OPTIONS`, `RUBYOPT`, `PERL5OPT`, `BASH_ENV`, `ENV`) are always rejected. Values passed via `env` are treated as secrets and redacted from output, logs, and history.
- **Zero-Trust Policy Governance:** Untrusted servers (`trusted: false`, the default) always require interactive confirmation even when a tool claims `readOnlyHint: true`. Even on `trusted: true` servers, only tools declaring `readOnlyHint: true` (without `destructiveHint: true`) that are explicitly listed in `approved_readonly_tools` (`--grant-readonly`) skip confirmation; everything else runs behind review with schema validation, policy fingerprint verification, full redacted arguments, and `"server_reported_success"` verification status. `allowed_tools` / `blocked_tools` are enforced before spawning the subprocess, and raw tool prose descriptions are stripped from model prompts.
- **Diagnostics:** Run `mavrick --mcp` to inspect configured servers, approval status, negotiated protocol versions, and per-tool policy tiers, or `mavrick --doctor` for a summary.

---

## 🧪 Testing & Verification

Run the comprehensive unit, integration, and UI test suite:

```bash
# Run from the parent directory of mavrick/
MAVRICK_TEST_GTK=broadway /usr/bin/python3 -m unittest discover -s mavrick/tests -t . -v
```

The suite isolates Mavrick state and sockets in temporary directories and blocks real external launches. GTK tests use `broadwayd` when available. COSMIC shortcut, compositor placement, and desktop backend integration require a COSMIC session.
