"""Loading and validation of bundled model profiles."""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from importlib import resources
from importlib.resources.abc import Traversable
from typing import Any, Final

CONFIG_FILENAME: Final = "config.json"
SUPPORTED_PROVIDERS: Final = frozenset({"ollama", "nvidia"})


class ConfigError(RuntimeError):
    """Raised when the agent configuration is missing or invalid."""


@dataclass(frozen=True, slots=True)
class Profile:
    """A validated model profile from ``config.json``."""

    key: str
    provider: str
    model_name: str
    context_limit: int

    @classmethod
    def from_mapping(cls, key: str, data: Any) -> Profile:
        """Build a profile from a decoded JSON object."""
        if not isinstance(data, Mapping):
            raise ConfigError(f"Profile {key!r} must be a JSON object.")

        missing = [
            field
            for field in ("provider", "model_name", "context_limit")
            if field not in data
        ]
        if missing:
            fields = ", ".join(repr(field) for field in missing)
            raise ConfigError(
                f"Profile {key!r} is missing required field(s): {fields}."
            )

        provider = data["provider"]
        model_name = data["model_name"]
        context_limit = data["context_limit"]

        if not isinstance(provider, str) or not provider.strip():
            raise ConfigError(f"Profile {key!r} has an invalid 'provider' value.")
        provider = provider.strip().lower()
        if provider not in SUPPORTED_PROVIDERS:
            supported = ", ".join(sorted(SUPPORTED_PROVIDERS))
            raise ConfigError(
                f"Profile {key!r} uses unsupported provider {provider!r} (expected one of: {supported})."
            )

        if not isinstance(model_name, str) or not model_name.strip():
            raise ConfigError(f"Profile {key!r} has an invalid 'model_name' value.")

        if (
            isinstance(context_limit, bool)
            or not isinstance(context_limit, int)
            or context_limit <= 0
        ):
            raise ConfigError(f"Profile {key!r} has an invalid 'context_limit' value.")

        return cls(
            key=key,
            provider=provider,
            model_name=model_name.strip(),
            context_limit=context_limit,
        )


def _config_path() -> Traversable:
    """Return the traversable path to the bundled configuration file."""
    package = __package__ or __name__
    return resources.files(package).joinpath(CONFIG_FILENAME)


def load_profiles() -> dict[str, Profile]:
    """Load every profile from the bundled ``config.json``."""
    try:
        raw_config = _config_path().read_text(encoding="utf-8")
    except OSError as exc:
        raise ConfigError(
            f"Configuration file {CONFIG_FILENAME!r} could not be read: {exc}"
        ) from exc
    except UnicodeError as exc:
        raise ConfigError(
            f"Configuration file {CONFIG_FILENAME!r} is not valid UTF-8: {exc}"
        ) from exc

    try:
        config = json.loads(raw_config)
    except json.JSONDecodeError as exc:
        raise ConfigError(
            f"Configuration file {CONFIG_FILENAME!r} contains invalid JSON: {exc}"
        ) from exc

    if not isinstance(config, Mapping):
        raise ConfigError(
            f"Configuration file {CONFIG_FILENAME!r} must contain a JSON object."
        )

    return {key: Profile.from_mapping(key, value) for key, value in config.items()}


def load_profile(profile_key: str) -> Profile:
    """Load a single profile by key, raising :class:`ConfigError` if unknown."""
    profiles = load_profiles()
    try:
        return profiles[profile_key]
    except KeyError:
        available = ", ".join(sorted(profiles)) or "<none>"
        raise ConfigError(
            f"Profile {profile_key!r} is not defined. Available profiles: {available}."
        ) from None
