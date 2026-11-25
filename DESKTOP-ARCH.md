# Daemon Desktop on Arch Linux

The desktop application is in `desktop/`. It provides a native desktop workspace for the existing Daemon agent engine.

## Install the build prerequisites

```bash
sudo pacman -Syu --needed \
  webkit2gtk-4.1 base-devel curl wget file openssl \
  appmenu-gtk-module libappindicator-gtk3 librsvg xdotool \
  rustup nodejs npm python-pipx

rustup default stable
```

These are the Arch-specific Tauri Linux prerequisites documented by Tauri, with Node.js/npm and pipx added for this project. See the [official Tauri prerequisites](https://v2.tauri.app/start/prerequisites/).

## Install the supplied Daemon engine wheel

From the repository root:

```bash
pipx install ./dist/daemon_agent-1.0.0-py3-none-any.whl
pipx ensurepath
```

Open a new terminal, then configure your provider:

```bash
daemon auth add
```

The desktop app does not collect or duplicate your API key. Authentication remains in Daemon's existing vault/keyring flow.

## Launch the desktop app

```bash
cd desktop
npm install
npm run tauri dev
```

For a release AppImage:

```bash
npm run tauri build
```

## Execution behaviour

- Build runs Daemon non-interactively without `--yes`. Permission requests that require a person to approve them are refused by its fail-closed policy.
- Plan creates a saved plan only. It does not execute it.
- Research uses the restricted, read-only Research agent.
- The file explorer previews text files only, within the currently selected workspace.

## Validation status

The original Daemon Python package was inspected and its version/help commands were run successfully in the build environment. The current development container cannot produce native binaries: npm dependency installation timed out and the container has no Rust/Cargo toolchain. Source, Windows and Arch build scripts, and a CI workflow are included. Native builds must run on their target platforms.


## Build the app

From the `desktop/` directory, run `./build-arch.sh`. The resulting AppImage is written to `src-tauri/target/release/bundle/appimage/`.

The Agents view includes built-in specialists and local custom agent profiles. **Generate with API** asks the configured Daemon Research provider to draft the specialist instructions; no API key is entered into or stored by the desktop shell.
