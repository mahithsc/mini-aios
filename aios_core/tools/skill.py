from __future__ import annotations

from difflib import get_close_matches
from pathlib import Path

from ..skills import load_skills
from ..workspace import get_skills_dir

_MAX_SKILL_CHARS = 100_000


def _skill_path(file_path: str) -> Path | None:
    skills_dir = get_skills_dir().resolve()
    relative_path = file_path.replace("\\", "/")
    if relative_path.startswith("skills/"):
        relative_path = relative_path[len("skills/") :]
    candidate = (skills_dir / relative_path).resolve()
    try:
        candidate.relative_to(skills_dir)
    except ValueError:
        return None
    return candidate


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

    instructions = path.read_text(encoding="utf-8")
    truncated = len(instructions) > _MAX_SKILL_CHARS
    if truncated:
        instructions = instructions[:_MAX_SKILL_CHARS]
    return {
        "skill": selected,
        "instructions": instructions,
        "truncated": truncated,
    }
