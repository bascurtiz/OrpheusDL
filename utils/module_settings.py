"""Helpers for merging module settings with schema defaults and user-facing config errors."""

from __future__ import annotations

from typing import Any, Mapping


def merge_module_settings(module_information, stored_settings: Any) -> dict:
    """Return module settings with schema defaults applied under user overrides."""
    if not isinstance(stored_settings, dict):
        stored_settings = {}

    global_settings = getattr(module_information, "global_settings", None) or {}
    session_settings = getattr(module_information, "session_settings", None) or {}
    defaults = {**global_settings, **session_settings}
    return {**defaults, **stored_settings}


def format_module_config_error(module: str, exc: Exception, *, service_name: str | None = None) -> str:
    """Turn config-related exceptions into actionable messages for GUI/CLI."""
    label = service_name or module.replace("_", " ").title()

    try:
        from modules.amazonmusic.interface import AmazonMusicConfigError
    except ImportError:
        AmazonMusicConfigError = None  # type: ignore

    if AmazonMusicConfigError is not None and isinstance(exc, AmazonMusicConfigError):
        return str(exc)

    if isinstance(exc, KeyError) and exc.args:
        key = exc.args[0]
        return (
            f"{label}: settings are incomplete (missing '{key}').\n"
            f"Open Settings > {label} and save your configuration, or restart the app "
            "so settings.json is regenerated with default module keys."
        )

    err_str = str(exc).strip()
    if err_str.startswith(f"{label}:") or err_str.lower().startswith(f"{label.lower()}:"):
        return err_str

    if isinstance(exc, PermissionError):
        err_lower = err_str.lower()
        if module == "amazonmusic" or "wvd" in err_lower:
            return (
                f"{label}: Widevine device file (.wvd) is missing or invalid.\n"
                f"Set wvd_path in Settings > {label} (browse to a .wvd file)."
            )

    if "please fill in country" in err_str.lower():
        return (
            f"{label}: Country is required before login.\n"
            f"Set your two-letter Amazon store country (e.g. US, DE, NL) in Settings > {label}, "
            "then sign in using the browser login flow."
        )

    return err_str or f"{label}: configuration error ({type(exc).__name__})"
