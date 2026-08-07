from __future__ import annotations

from difflib import get_close_matches
from pathlib import Path

from ..skill_limits import MAX_SKILL_INSTRUCTION_CHARS
from ..skills import load_skills, resolve_skill_file


def _skill_path(file_path: str) -> Path | None:
    return resolve_skill_file(file_path)


def read_skill(name: str | None = None):
    """List available skills or read one skill's instructions.

    Args:
        name: Skill name or title. Omit it to list every available skill.
    """
    skills = load_skills()
    if name is None or not name.strip():
        return {
            "skills": skills,
            "count": len(skills),
        }

    requested = name.strip().casefold()
    selected = next(
        (
            skill
            for skill in skills
            if requested
            in {
                skill.get("name", "").casefold(),
                skill.get("title", "").casefold(),
            }
        ),
        None,
    )
    if selected is None:
        available = [skill["name"] for skill in skills]
        suggestions = get_close_matches(
            name.strip(),
            available,
            n=3,
            cutoff=0.5,
        )
        result: dict[str, object] = {
            "error": f"skill not found: {name.strip()}",
            "available": available,
        }
        if suggestions:
            result["suggestions"] = suggestions
        return result

    path = _skill_path(selected["file"])
    if path is None or not path.is_file():
        return {
            "error": f"skill file is unavailable: {selected['file']}",
            "skill": selected,
        }

    try:
        with path.open(encoding="utf-8") as file:
            instructions = file.read(MAX_SKILL_INSTRUCTION_CHARS + 1)
    except (OSError, UnicodeError):
        return {
            "error": f"skill file could not be read: {selected['file']}",
            "skill": selected,
        }
    truncated = len(instructions) > MAX_SKILL_INSTRUCTION_CHARS
    instructions = instructions[:MAX_SKILL_INSTRUCTION_CHARS]
    return {
        "skill": selected,
        "instructions": instructions,
        "truncated": truncated,
    }
