# Daemon Desktop

A Tauri 2 desktop workspace for Daemon 1.0.0. The UI is designed around a quiet, high-contrast graphite palette with warm parchment text and restrained ember accents. It borrows the workflow clarity of modern coding agents without copying their visual identity.

## What it does

- Opens a local project folder and remembers the most recent workspace on this device.
- Shows top-level files and folders, supports folder navigation, and previews small text files.
- Starts Daemon Build, Plan, and Research tasks through the installed Daemon engine.
- Streams stdout and stderr into the current session as Daemon runs.
- Shows Git branch/status, recent desktop activity, Daemon status, and quick Daemon utilities.
- Uses Daemon's own authentication, vault, permission broker, checkpoints, and audit trail.

The desktop app does **not** reimplement the agent runtime and does not store API keys. Rust/Tauri owns the native desktop shell; the existing Daemon engine handles execution and provider authentication.

## Arch Linux

From the repository root:

```bash
sudo pacman -Syu --needed \
  webkit2gtk-4.1 base-devel curl wget file openssl \
  appmenu-gtk-module libappindicator-gtk3 librsvg xdotool \
  rustup nodejs npm python-pipx

rustup default stable
pipx install ./dist/daemon_agent-1.0.0-py3-none-any.whl
pipx ensurepath
```

Open a new terminal after `pipx ensurepath`, then configure your provider using the existing Daemon:

```bash
daemon auth add
```

Build and run the desktop app:

```bash
cd desktop
npm install
npm run tauri dev
```

Create a Linux AppImage:

```bash
npm run tauri build
```

The AppImage build uses Tauri's Linux bundler and requires the Tauri Rust and WebKitGTK prerequisites above. For a non-bundled executable during development, use `npm run tauri build -- --no-bundle`.

## Notes on execution

- **Build** calls `daemon run <task> --workspace <path>` without `--yes`. Any operation that requires an interactive approval is therefore refused by Daemon's existing non-interactive permission policy. Use the Daemon in a terminal for tasks that need an interactive approval.
- **Plan** calls `daemon run --plan <goal> --workspace <path>`. It saves a plan and does not execute it.
- **Research** calls `daemon research <query> --workspace <path>` and uses Daemon's read-only Research agent.
- The desktop app discovers the Daemon executable on `PATH`, then checks common `pipx` / user-install locations. It never starts a shell to run the user's task.
- File previews are read-only and limited to text files smaller than 180 KB inside the chosen workspace. Files outside the workspace are rejected.

## Project layout

```text
desktop/
  src/                 React + TypeScript interface
  public/              Local decorative artwork
  src-tauri/
    src/main.rs        Rust commands and Daemon process bridge
    capabilities/      Tauri v2 window/plugin permissions
    icons/             Daemon app icons
    tauri.conf.json    Native window and Linux bundle config
```


## Agent Studio

Create custom specialist profiles in the Agents view. Use **Generate with API** to ask the configured Daemon Research provider to draft the agent's system instructions, review the result, then save it locally. Custom profiles are stored in this desktop app's local browser storage and are prepended as task-specific instructions when selected; they do not change Daemon's provider credentials or grant additional tools. The optional Model preference field accepts a model ID override for Build and Plan; Research uses Daemon’s configured research model.

## Windows build

On Windows 10/11, install Node.js LTS and Rust via rustup, then run `./build-windows.ps1` from PowerShell. This creates a Windows NSIS installer (`.exe`) under `src-tauri/target/release/bundle/nsis/`. You can also run `npm run build:windows:msi` to create an MSI installer.

## Arch Linux build

From this `desktop/` directory run `./build-arch.sh`. It installs the documented Arch dependencies, builds the frontend, and creates an AppImage under `src-tauri/target/release/bundle/appimage/`.
