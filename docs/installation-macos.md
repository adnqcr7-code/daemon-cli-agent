# macOS installation

## Python 3.11+

Use [Homebrew](https://brew.sh) or an official installer:

```bash
brew install python@3.12 git
```

## Install

```bash
git clone https://github.com/adnqcr7-code/daemon-cli-agent.git daemon
cd daemon
./scripts/install.sh
```

The installer creates a per-user installation under
`~/.local/share/daemon` and links the command into `~/.local/bin`. Ensure
`~/.local/bin` is on your `PATH` (the installer prints a hint if it is not),
then open a new terminal. After installation, `daemon launch` works from any
project directory.

## Secret storage

The installer includes the `keyring` extra, so credentials are stored in the
**macOS Keychain** (service name `daemon`). Add a provider and launch:

```sh
daemon auth add
daemon launch
```

Grant `daemon` (the terminal host application) access to the keychain item
when prompted. Without keyring or cryptography, Daemon falls back to a
0600-permission file under `~/.daemon/` and warns loudly in `doctor`.

## Verify

```sh
daemon version
daemon doctor
```
