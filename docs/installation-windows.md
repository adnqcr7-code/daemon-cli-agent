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
.\.venv\Scripts\daemon.exe version
```

For interactive use, activate the environment once in each new PowerShell
window:

```powershell
.\.venv\Scripts\Activate.ps1
daemon version
```

Without activation, always call `.\.venv\Scripts\daemon.exe` explicitly.

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

## 3. First Ollama run

```powershell
Set-Location $HOME\Downloads\daemon
.\.venv\Scripts\Activate.ps1
daemon auth add --provider ollama
daemon models list
$model = Read-Host "Enter the exact model id shown above"
daemon models set default $model
daemon models set build $model
daemon models set coding $model
daemon
```

The `build` role matters because `daemon run` uses it by default. Configure it
alongside `default` and `coding` to avoid a stale or missing build-model
error.

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
