# Windows installation

Windows 10/11 is a first-class target. PowerShell is used for agent shell
commands.

## 1. Install Python 3.11+

Download from <https://www.python.org/downloads/windows/> and tick
"Add python.exe to PATH", or with winget:

```powershell
winget install Python.Python.3.12
```

Verify:

```powershell
python --version
```

## 2. Install Daemon

```powershell
git clone https://github.com/adnqcr7-code/daemon-cli-agent.git daemon
cd daemon
.\scripts\install.ps1
.\.venv\Scripts\daemon.exe version
```

If scripts are blocked by execution policy:

```powershell
Set-ExecutionPolicy -ExecutionPolicy RemoteSigned -Scope CurrentUser
```

The installer includes the recommended `keyring`, `crypto` and `repl` extras.
The `keyring` extra uses **Windows Credential Manager** — the best secret
storage option on this platform and the default once installed. If you prefer
manual installation, run:

```powershell
python -m pip install "daemon-agent[keyring,crypto,repl]"
```

## 4. Verify

```powershell
.\.venv\Scripts\daemon.exe doctor
```

Expected on a healthy machine: Python 3.11+, `pwsh` or `powershell`
detected, credential storage = *os-keychain* (with the keyring extra),
git present.

## Notes

- Agent shell commands run through `pwsh -NoProfile -Command` when
  available, otherwise `powershell -NoProfile -Command`, otherwise
  `cmd /C`.
- The permission engine's command classifier knows both POSIX and Windows
  idioms (`rm -rf`, `Remove-Item -Recurse -Force` style flags are analysed;
  catastrophic patterns floor to deny).
- `NO_COLOR` and `DAEMON_ASCII=1` are honoured if your terminal lacks
  colour/Unicode.
- The automated test suite executes on Linux; Windows-specific behaviour
  (PowerShell execution, paths) is implemented and exercised manually -
  please report issues.
