"""Interactive/non-interactive authentication flows.

``daemon auth add`` implements the intelligent key experience:

1. the key is analysed LOCALLY (prefix + format fingerprinting),
2. if the provider is uniquely identified we verify against exactly that
   provider's model endpoint,
3. if ambiguous we ask exactly ONE short question (never a giant menu),
4. discovered models are cached, and the account becomes usable.
"""

from __future__ import annotations

import asyncio
from typing import Protocol

from ..config import Config, daemon_home
from ..errors import AuthError
from ..log import audit, get_logger
from ..providers.catalog import PROVIDERS, provider_by_id
from ..providers.detect import fingerprint, question_options
from ..providers.registry import AdapterRegistry, custom_descriptor, discover_models
from ..security import redact, redactor
from .store import CredentialStore, Profile
from .vault import open_vault

_logger = get_logger("auth.manager")


class PromptIO(Protocol):
    """UI abstraction so flows are testable without a terminal."""

    def info(self, message: str) -> None: ...

    def warn(self, message: str) -> None: ...

    def success(self, message: str) -> None: ...

    def prompt_secret(self, prompt: str) -> str: ...

    def ask_text(self, prompt: str, default: str | None = None) -> str: ...

    def ask_choice(self, question: str, options: list[tuple[str, str]]) -> str: ...

    def confirm(self, question: str, default: bool = False) -> bool: ...


class AuthManager:
    def __init__(self, config: Config | None = None,
                 registry: AdapterRegistry | None = None) -> None:
        self.config = config or Config.load()
        self.home = daemon_home()
        self.home.mkdir(parents=True, exist_ok=True)
        self.registry = registry or AdapterRegistry()
        self.vault = open_vault(self.home)
        self.store = CredentialStore(self.home, self.vault)

    # ------------------------------------------------------------------ add

    def add(
        self,
        io: PromptIO,
        *,
        key: str | None = None,
        provider: str | None = None,
        base_url: str | None = None,
        label: str | None = None,
        env_var: str | None = None,
        verify: bool = True,
    ) -> Profile:
        """Full 'auth add' flow. Returns the stored profile."""
        descriptor = None
        if provider:
            descriptor = provider_by_id(provider)
            if descriptor is None and not base_url:
                raise AuthError(
                    f"Unknown provider '{provider}'. Run 'daemon auth add' "
                    f"without --provider to use automatic detection, or pass "
                    f"--base-url for a custom OpenAI-compatible endpoint.")

        # -- local providers need no key ------------------------------------
        if descriptor is not None and not descriptor.requires_key and key is None \
                and env_var is None:
            io.info(f"{descriptor.name} runs locally - no API key needed.")
            endpoint = base_url or descriptor.base_url
            profile = Profile(provider=descriptor.id, label=label,
                              endpoint=endpoint)
            io.info(f"Verifying server at {endpoint} ...")
            models = self._verify_or_none(io, profile, key=None) if verify else None
            if models is None:
                if not io.confirm(
                        f"Could not reach {endpoint}. Save the profile anyway?",
                        default=True):
                    raise AuthError("Aborted.")
            self.store.add(profile, secret=None, allow_keyless=True)
            io.success(f"Saved profile {profile.id} "
                       f"(backend: {self.vault.security_level}).")
            return profile

        # -- gather the credential ------------------------------------------
        if env_var:
            import os

            key = os.environ.get(env_var)
            if not key:
                raise AuthError(f"{env_var} is not set in the environment.")
            io.info(f"Using credential from ${env_var}.")
        elif key is None:
            key = io.prompt_secret("Paste your API key").strip()
        if not key:
            raise AuthError("No credential provided.")
        # Keys are never logged; only a redacted fingerprint marker. Register
        # the live value with the process redactor immediately.
        redactor().register(key)
        _logger.debug("auth add: analysing key (%d chars)", len(key))

        # -- identify the provider -------------------------------------------
        account_id: str | None = None
        if descriptor is None and base_url:
            # Explicit custom endpoint: no detection question needed.
            descriptor = custom_descriptor("Custom endpoint", base_url)
        if descriptor is None:
            io.info("Analyzing credentials...")
            detection = fingerprint(key)
            if detection.unique and detection.best:
                chosen = detection.best.provider_id
                io.info(f"Provider identified: "
                        f"{PROVIDERS[chosen].name if chosen in PROVIDERS else chosen} "
                        f"({detection.best.reason})")
                descriptor = provider_by_id(chosen)
            else:
                if not detection.candidates:
                    io.warn("Key format is not recognised by any known provider.")
                else:
                    io.warn("This key format is shared by multiple providers - "
                            "one question, then we are done:")
                    for candidate in detection.candidates[:4]:
                        name = PROVIDERS[candidate.provider_id].name \
                            if candidate.provider_id in PROVIDERS else candidate.provider_id
                        io.info(f"  {name}: {candidate.reason}")
                choice = io.ask_choice(
                    "Which provider issued this key?", question_options(detection))
                if choice == "custom":
                    base_url = io.ask_text("Provider base URL "
                                           "(OpenAI-compatible, e.g. https://host/v1)")
                    descriptor = custom_descriptor("Custom endpoint", base_url)
                else:
                    descriptor = provider_by_id(choice)
            if descriptor is None:
                raise AuthError("Provider could not be determined.")

        # -- provider-specific endpoint inputs -------------------------------
        endpoint = base_url or descriptor.base_url
        if descriptor.id == "cloudflare" and "{account_id}" in endpoint:
            account_id = io.ask_text("Cloudflare account id")
            endpoint = endpoint.replace("{account_id}", account_id)
        elif descriptor.id == "azure" and "{resource}" in endpoint:
            resource = io.ask_text("Azure OpenAI resource name")
            endpoint = endpoint.replace("{resource}", resource)
        elif descriptor.id == "custom":
            endpoint = base_url or io.ask_text(
                "Provider base URL (OpenAI-compatible, e.g. https://host/v1)")

        # Persist the endpoint unless it is the catalog default of a known
        # provider; custom endpoints are always stored (the descriptor is
        # not persisted, only the profile is).
        default_endpoint = (descriptor.id != "custom"
                            and endpoint == descriptor.base_url)
        profile = Profile(
            provider=descriptor.id,
            label=label,
            endpoint=None if default_endpoint else endpoint,
            account_id=account_id,
            env_var=env_var,
        )

        # -- verification against exactly ONE known endpoint ------------------
        io.info(f"Verifying credentials against {descriptor.name} ...")
        if verify:
            models = self._verify(io, profile, key)
        else:
            models = None
            io.warn("Verification skipped (--no-verify).")

        # -- store ------------------------------------------------------------
        secret = None if env_var else key
        self.store.add(profile, secret=secret)
        if models is not None:
            io.success(f"Found {len(models)} accessible model(s).")
        io.success(f"Authentication successful. Profile: {profile.id}")
        io.info(f"Secret storage: {self.vault.security_level}"
                + (f"  [warning: {self.vault.warning}]" if self.vault.warning else ""))
        return profile

    # -------------------------------------------------------------- verify

    def _verify(self, io: PromptIO, profile: Profile, key: str | None):
        descriptor = provider_by_id(profile.provider) or custom_descriptor(
            "Custom endpoint", profile.endpoint or "")
        adapter = self.registry.create(
            descriptor, api_key=key, endpoint=profile.endpoint, config=self.config)
        async def _discover_and_close():
            try:
                return await discover_models(adapter)
            finally:
                await adapter.close()

        try:
            models = asyncio.run(_discover_and_close())
        except AuthError as exc:
            io.warn(f"Verification failed: {exc}")
            if not io.confirm("Save this profile anyway?", default=False):
                raise
            return None
        if models:
            sample = ", ".join(model.model_id for model in models[:5])
            more = " ..." if len(models) > 5 else ""
            io.info(f"Models accessible: {sample}{more}")
        else:
            io.warn("Credentials valid, but no models were listed.")
        return models

    def _verify_or_none(self, io: PromptIO, profile: Profile, key: str | None):
        try:
            return self._verify(io, profile, key)
        except AuthError:
            return None

    # ------------------------------------------------------------- commands

    def test(self, io: PromptIO, profile_id: str | None = None) -> bool:
        target = self.store.get(profile_id) if profile_id else self.store.active()
        if target is None:
            raise AuthError("No profile to test. Run 'daemon auth add' first.")
        io.info(f"Testing {target.id} ...")
        key = self.store.resolve_key(target)
        descriptor = provider_by_id(target.provider) or custom_descriptor(
            "Custom endpoint", target.endpoint or "")
        adapter = self.registry.create(
            descriptor, api_key=key, endpoint=target.endpoint, config=self.config)
        async def _discover_and_close():
            try:
                return await discover_models(adapter)
            finally:
                await adapter.close()

        try:
            models = asyncio.run(_discover_and_close())
        except AuthError as exc:
            io.warn(f"Verification failed: {exc}")
            if not io.confirm("Save this profile anyway?", default=False):
                raise
            return None
        self.store.touch(target)
        io.success(f"OK - {len(models)} model(s) reachable.")
        return True

    def switch(self, profile_id: str) -> Profile:
        return self.store.set_active(profile_id)

    def remove(self, profile_id: str) -> Profile:
        return self.store.remove(profile_id)

    # ------------------------------------------------------------- models

    def models(self, io: PromptIO, profile_id: str | None = None):
        """List models for a profile (uses the shared model cache)."""
        from ..models import ModelRegistry

        target = self.store.get(profile_id) if profile_id else self.store.active()
        if target is None:
            raise AuthError("No active profile. Run 'daemon auth add' first.")
        key = self.store.resolve_key(target)
        descriptor = provider_by_id(target.provider) or custom_descriptor(
            "Custom endpoint", target.endpoint or "")
        adapter = self.registry.create(
            descriptor, api_key=key, endpoint=target.endpoint, config=self.config)
        registry = ModelRegistry(self.home)
        try:
            models = asyncio.run(
                registry.load_async(adapter, target.id, refresh=True))
        finally:
            asyncio.run(adapter.close())
        return target, models

    def close(self) -> None:
        self.vault.close()


_ = redact  # exported for potential UI use
