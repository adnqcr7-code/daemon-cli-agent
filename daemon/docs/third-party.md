# Third-party notices

Daemon (MIT) depends on the following open-source components. License
texts are available from each project's repository.

## Required dependencies

| Component | License | Notes |
|---|---|---|
| [click](https://palletsprojects.com/p/click/) | BSD-3-Clause | CLI framework |
| [rich](https://github.com/Textualize/rich) | MIT | terminal rendering, tables, panels |
| [httpx](https://www.python-httpx.org/) | BSD-3-Clause | HTTP client for providers, MCP, web tools |

## Optional dependencies

| Component | License | Notes |
|---|---|---|
| [keyring](https://github.com/jaraco/keyring) | MIT | OS keychain storage (Windows Credential Manager, macOS Keychain, Linux Secret Service) |
| [cryptography](https://cryptography.io/) | Apache-2.0 OR BSD-3-Clause | AES-256-GCM encrypted vault |
| [prompt_toolkit](https://github.com/prompt-toolkit/python-prompt-toolkit) | BSD-3-Clause | REPL history, completion, history search |
| [pytest](https://pytest.org) (dev) | MIT | test suite only - not shipped |

## Development-time tools used by the test suite

The mock MCP echo server and scripted provider servers in `tests/` are
part of Daemon (MIT) and use only the Python standard library.

No dependency is copyleft, and none of the optional extras are required at
runtime: Daemon degrades gracefully when they are absent.
