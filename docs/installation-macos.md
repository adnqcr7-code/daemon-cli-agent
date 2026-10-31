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
python3.12 -m venv .venv
source .venv/bin/activate
pip install .
daemon version
```

## Secret storage on macOS

With the `keyring` extra installed, credentials are stored in the
**macOS Keychain** (service name `daemon`):

```bash
pip install 'daemon-agent[keyring]'
```

Grant `daemon` (the terminal host application) access to the keychain item
when prompted. Without keyring or cryptography, Daemon falls back to a
0600-permission file under `~/.daemon/` and warns loudly in `doctor`.

## Verify

```bash
daemon doctor
```
