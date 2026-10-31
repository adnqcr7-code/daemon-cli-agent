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

Run these commands one at a time from a normal folder such as
`$HOME\Downloads`. Never clone into `C:\Windows\System32`.

```powershell
Set-Location $HOME\Downloads
git clone https://github.com/adnqcr7-code/daemon-cli-agent.git daemon
Set-Location .\daemon
powershell -ExecutionPolicy Bypass -File .\scripts\install.ps1
```

The installer installs Daemon in `%LOCALAPPDATA%\Programs\Daemon` and adds
its Scripts directory to your **user PATH**. Close this PowerShell window,
open a new one, then verify:

```powershell
daemon version
daemon doctor
```

You can run it without restarting the shell using:
`& "$env:LOCALAPPDATA\Programs\Daemon\Scripts\daemon.exe" version`.

The installer includes the recommended `keyring`, `crypto` and `repl` extras.
The `keyring` extra uses **Windows Credential Manager** — the best secret
storage option on this platform and the default once installed. If you prefer
manual installation, run:

```powershell
python -m pip install "daemon-agent[keyring,crypto,repl]"
```

## 3. Verify

```powershell
daemon doctor
```

Expected on a healthy machine: Python 3.11+, `pwsh` or `powershell`
detected, credential storage = *os-keychain* (with the keyring extra),
git present.

## 4. Connect a provider and launch

```powershell
daemon auth add --provider ollama-cloud
daemon launch
```

At the hidden prompt, paste the Ollama Cloud API key you created at
<https://ollama.com/settings/keys>. Daemon stores it in Windows Credential
Manager when available. Choose the `ollama-cloud` profile and a listed model
in the launch menus.

For local Ollama instead, use `daemon auth add --provider ollama`. For other
providers, use `daemon auth add` and follow the provider/key prompts. If a
profile already exists, skip adding it and run `daemon launch`.

Once installed and added to PATH, `daemon launch` works from any directory.
The current directory is the project Daemon will work in.

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
