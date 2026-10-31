# Linux installation

## Python 3.11+

Most distributions carry a suitable package; otherwise use your package
manager or [pyenv](https://github.com/pyenv/pyenv):

```bash
# Debian/Ubuntu
sudo apt install python3.12 python3.12-venv git

# Fedora
sudo dnf install python3.12 git

# Arch
sudo pacman -S python git
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

## Secret storage on Linux

The storage ladder is: **libsecret/Secret Service** (GNOME Keyring or
KeePassXC with secret-service) → **AES-256-GCM encrypted vault** →
**0600 plain file** (warning).

On headless servers without a keyring daemon, Daemon automatically selects
the encrypted vault when `cryptography` is installed:

```bash
pip install 'daemon-agent[crypto]'
```

You can force a backend for debugging:

```bash
DAEMON_VAULT_BACKEND=encrypted daemon auth add
```

## Verify

```bash
daemon doctor          # add --net to probe provider endpoints
```
