# Authentication and automatic provider detection

## The intended experience

```
$ daemon auth add
Paste your API key: ••••••••••••••••••••••••••••••••••••••
Analyzing credentials...
Provider identified: Groq (starts with the registered 'Groq' key prefix)
Verifying credentials against Groq ...
Models accessible: llama-3.3-70b-versatile, ...
Found 9 accessible models.
Authentication successful. Profile: groq
Secret storage: os-keychain
```

No menus, no API-format questions - unless the key genuinely cannot be
identified, in which case you get **exactly one** short question.

## How detection works

Detection is **local first**:

1. **Prefix matching** against registered key prefixes (`sk-ant-`,
   `gsk_`, `xai-`, `pplx-`, `fw_`, `tgp_v1_`, `sk-or-v1-`, `hf_`,
   `nvapi-`, `csk-`, `AIza`, …).
2. **Format families** for prefixless keys (`sk-` + 32 hex → DeepSeek /
   DashScope family; `{32hex}.{16hex}` → Z.AI; 32 plain alphanumerics →
   Mistral; JWT-shaped → DeepInfra/AI21; …).
3. **Environment confirmation** - if the pasted key equals e.g.
   `ANTHROPIC_API_KEY`, that is decisive evidence.

Rules that matter:

- **Ambiguity is honest.** Some formats are genuinely shared (plain `sk-`
  keys are used by OpenAI *and* many compatible providers). When confidence
  is below threshold, Daemon says so and asks **one** question with the
  plausible candidates plus "Other / custom endpoint". It never guesses and
  never pretends.
- **No probing.** The key is never sent to random endpoints to "discover"
  where it works. A verification request goes to **exactly one** endpoint:
  the identified provider's model endpoint (or a URL you supplied).
- **Verification = model listing.** A key is "valid" when `GET /models`
  (or the protocol equivalent) succeeds; the discovered models are cached
  for 24 h under `~/.daemon/cache/models/`.

## Command reference

```
daemon auth add [--provider ID] [--base-url URL] [--label NAME]
                [--env VAR] [--api-key KEY] [--no-verify]
daemon auth list                  # profiles + storage backend (never secrets)
daemon auth remove <profile>
daemon auth test [profile]        # live credential check
daemon auth switch <profile>
daemon auth models [profile]      # discovered model list
```

Non-interactive scripting:

```bash
daemon auth add --api-key "$KEY" --base-url https://gw/v1
```

(Interactive use prompts with hidden input; the `--api-key` flag exists for
pipelines where the key already lives in a secret manager.)

Environment-variable profiles store **nothing**:

```bash
daemon auth add --env GROQ_API_KEY
```

Local servers need no key at all:

```bash
daemon auth add --provider ollama
```

## Secret storage

Backend ladder (first available wins; force with
`DAEMON_VAULT_BACKEND=keyring|encrypted|plain`):

1. **OS keychain** via `keyring` — Windows Credential Manager, macOS
   Keychain, Linux Secret Service. Verified with a round-trip probe at
   startup, so a broken keyring falls through instead of silently losing
   credentials.
2. **AES-256-GCM encrypted vault** (`~/.daemon/vault.bin`) with a separate
   master key (`~/.daemon/vault.key`, mode 0600). A backup that only
   captures the vault leaks nothing.
3. **Restrictive plain file** (`~/.daemon/vault.json`, 0600) with a loud
   warning — comparable to an AWS credentials file. Only used when neither
   optional extra is installed.

`auth.json` stores **metadata only** (provider, label, endpoint, timestamps).
`daemon doctor` and `daemon auth list` always show which backend is active.

## Secret hygiene guarantees

- Keys are **never** printed, echoed, logged or written to session
  transcripts. All log records pass a redaction filter that also rewrites
  tracebacks; every UI render path goes through the same redactor.
- Live secret values are registered with the process-wide redactor, so any
  accidental mention becomes `***REDACTED***`.
- Child processes launched by the shell tool get a **scrubbed environment**:
  all known provider-key variables (`OPENAI_API_KEY`, `ANTHROPIC_API_KEY`,
  …) are removed unless a tool explicitly requests them.
- The automated suite includes a leakage sweep: a full agent run is executed
  and every file under the state directory is scanned for the secret.

## Multiple accounts

Profiles are `provider` or `provider#label`. Add as many as you like; the
active one is switched with `daemon auth switch`, and role models can
reference models from any profiled provider.
