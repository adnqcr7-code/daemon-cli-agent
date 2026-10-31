# Installation

Daemon is a Python 3.11+ CLI. It runs on Windows 10/11, Linux and macOS.

## Requirements

- Python **3.11, 3.12 or 3.13** (`python3 --version`)
- git (optional but strongly recommended for the git tools)
- ~50 MB of disk for the package and its state directory

## Install from source

```bash
git clone <this repository> daemon
cd daemon
python -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\Activate.ps1
pip install .
daemon version
```

For development (tests included):

```bash
pip install -e .[dev]
python -m pytest
```

## Optional extras

| Extra | Enables |
|---|---|
| `pip install 'daemon-agent[keyring]'` | OS keychain storage (Windows Credential Manager, macOS Keychain, Linux Secret Service) |
| `pip install 'daemon-agent[crypto]'` | AES-256-GCM encrypted credential vault (recommended where no keychain is available) |
| `pip install 'daemon-agent[repl]'` | prompt_toolkit: input history, autocomplete for slash commands, history search |

All three together: `pip install 'daemon-agent[keyring,crypto,repl]'`.

Daemon degrades gracefully without them: the vault falls back from keychain
to encrypted storage to a 0600-permission file (with a loud warning from
`daemon doctor`), and the REPL works with plain `input()`.

## First run

```bash
daemon doctor        # verify the installation
daemon auth add      # paste an API key (see docs/authentication.md)
daemon               # start the interactive agent
```

State lives in `~/.daemon/` (override with the `DAEMON_HOME` environment
variable):

```
~/.daemon/
├── config.toml      user configuration
├── auth.json        credential profiles (metadata only - no secrets)
├── vault.bin/.key   encrypted credential vault (or keychain / vault.json)
├── sessions/        saved sessions (JSONL transcripts)
├── cache/models/    cached model lists per profile
├── skills/          user-level skills
├── history          REPL input history (with prompt_toolkit)
└── logs/            daemon.log (debug) + audit.log (security events)
```

Project-level state lives in `<workspace>/.daemon/` (create with
`daemon init`): config overlay, project memory, skills, checkpoints and the
incremental project index.

## Updating

Re-run `pip install .` (or `pip install -e .`) from a fresh checkout.
User state in `~/.daemon` is forward-compatible across 0.x releases;
`daemon doctor` after upgrading will flag anything unusual.

## Uninstalling

```bash
pip uninstall daemon-agent
rm -rf ~/.daemon      # removes config, profiles, sessions, caches, logs
```
