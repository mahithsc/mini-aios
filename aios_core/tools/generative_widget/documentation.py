from __future__ import annotations

import re
from functools import lru_cache
from pathlib import Path

_GUIDELINES_SOURCE_PATH = Path(__file__).with_name("guidelines.ts")
_TEMPLATE_LITERAL_PATTERN = re.compile(
    r"const\s+(?P<name>[A-Z_]+)\s*=\s*`(?P<body>.*?)`;",
    re.DOTALL,
)
_MODULE_SECTION_PATTERN = re.compile(
    r"const MODULE_SECTIONS: Record<string, string\[]> = \{(?P<body>.*?)\};",
    re.DOTALL,
)
_MODULE_LINE_PATTERN = re.compile(r"(?P<module>\w+):\s*\[(?P<sections>[A-Z_,\s]+)\],?")


def _decode_template_literal(value: str) -> str:
    return value.replace(r"\`", "`").replace(r"\${", "${")


@lru_cache(maxsize=1)
def _load_guideline_templates() -> tuple[dict[str, str], dict[str, list[str]]]:
    source = _GUIDELINES_SOURCE_PATH.read_text(encoding="utf-8")
    templates = {
        match.group("name"): _decode_template_literal(match.group("body"))
        for match in _TEMPLATE_LITERAL_PATTERN.finditer(source)
    }

    modules_match = _MODULE_SECTION_PATTERN.search(source)
    if modules_match is None:
        raise ValueError("Unable to locate MODULE_SECTIONS in generative widget guidelines.")

    module_sections: dict[str, list[str]] = {}
    for line_match in _MODULE_LINE_PATTERN.finditer(modules_match.group("body")):
        module_sections[line_match.group("module")] = [
            section.strip()
            for section in line_match.group("sections").split(",")
            if section.strip()
        ]

    if "CORE" not in templates:
        raise ValueError("Unable to locate CORE guidelines in generative widget guidelines.")

    return templates, module_sections


@lru_cache(maxsize=1)
def get_available_guideline_modules() -> tuple[str, ...]:
    _, module_sections = _load_guideline_templates()
    return tuple(module_sections.keys())


def get_guidelines(modules: list[str] | tuple[str, ...] | None = None) -> str:
    templates, module_sections = _load_guideline_templates()

    requested_modules = list(modules) if modules else list(get_available_guideline_modules())
    content = templates["CORE"]
    seen: set[str] = set()

    for module_name in requested_modules:
        for section_name in module_sections.get(module_name, []):
            if section_name in seen:
                continue
            seen.add(section_name)
            content += "\n\n\n" + templates[section_name]

    return content.rstrip() + "\n"
