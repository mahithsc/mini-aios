from __future__ import annotations

from .documentation import get_available_guideline_modules, get_guidelines

SUPPORTED_FUNCTIONS = {"documentation", "generate"}


def _normalize_optional_string(value: object) -> str | None:
    if not isinstance(value, str):
        return None

    normalized = value.strip()
    return normalized or None


def _normalize_widget_markup(value: str) -> str:
    normalized = value.strip()
    if normalized.startswith("```") and normalized.endswith("```"):
        lines = normalized.splitlines()
        if len(lines) >= 2:
            return "\n".join(lines[1:-1]).strip()
    return normalized


def generative_widget(
    function: str,
    widget: str | None = None,
    modules: list[str] | None = None,
    title: str | None = None,
):
    """
    Load generative UI documentation or return an inline widget artifact.
    """
    normalized_function = function.strip().lower() if isinstance(function, str) else ""
    if normalized_function not in SUPPORTED_FUNCTIONS:
        supported = ", ".join(sorted(SUPPORTED_FUNCTIONS))
        return f"error: function must be one of {supported}"

    available_modules = set(get_available_guideline_modules())
    if modules is not None and any(not isinstance(module, str) for module in modules):
        return "error: modules must be a list of strings"

    normalized_modules = [
        module.strip().lower() for module in modules or [] if isinstance(module, str) and module.strip()
    ]
    invalid_modules = [module for module in normalized_modules if module not in available_modules]
    if invalid_modules:
        supported = ", ".join(sorted(available_modules))
        invalid = ", ".join(sorted(set(invalid_modules)))
        return f"error: unsupported modules: {invalid}. Supported modules: {supported}"

    if normalized_function == "documentation":
        selected_modules = normalized_modules or list(get_available_guideline_modules())
        return {
            "ok": True,
            "type": "generative_widget_documentation",
            "modules": selected_modules,
            "documentation": get_guidelines(selected_modules),
            "message": "Generative widget documentation loaded.",
        }

    normalized_widget = _normalize_optional_string(widget)
    if normalized_widget is None:
        return "error: widget is required when function is generate"
    normalized_widget = _normalize_widget_markup(normalized_widget)

    normalized_title = _normalize_optional_string(title) or "Generative UI"
    return {
        "ok": True,
        "type": "generative_widget_artifact",
        "artifact": {
            "version": 1,
            "title": normalized_title,
            "textPreview": "Generative UI",
            "widget": normalized_widget,
        },
        "message": "Generative widget prepared.",
    }
