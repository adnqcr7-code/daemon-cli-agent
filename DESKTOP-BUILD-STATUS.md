# Daemon Desktop build status

This source update adds the Agent Studio, API-assisted agent instruction generation, tool/MCP registry view, Windows executable-installer build path, Arch AppImage build path, and a GitHub Actions workflow for native builds.

## Verified in this environment

- TypeScript/TSX transpilation syntax checks: passed for `App.tsx`, `main.tsx`, and `vite.config.ts`.
- JSON validation: passed for `package.json` and `src-tauri/tauri.conf.json`.
- Shell syntax: passed for `build-arch.sh` and `install-arch.sh`.
- CSS brace balance: passed.
- Python regression subset: 35 tests passed (`test_config`, `test_provider_catalog`, `test_tool_selector`, `test_permissions`).
- ZIP integrity: checked before delivery.

## Not verified here

- Full TypeScript type-check and Vite production build: npm dependency installation timed out because the registry was unavailable.
- Rust/Tauri native compilation: this container does not have `rustc` or `cargo`.
- Actual Windows `.exe` installer and Linux AppImage: not built in this environment.

## Native build outputs

- Windows: run `desktop/build-windows.ps1` on Windows 10/11 with Node.js and Rust installed. The NSIS setup executable is written to `desktop/src-tauri/target/release/bundle/nsis/`.
- Arch Linux: run `desktop/build-arch.sh` on Arch Linux. The AppImage is written to `desktop/src-tauri/target/release/bundle/appimage/`.
- CI: `.github/workflows/desktop-build.yml` builds both platform artifacts when manually dispatched or when a `desktop-v*` tag is pushed.

The desktop app expects the Daemon engine to be installed separately. API credentials remain managed by Daemon's existing secure authentication store.
