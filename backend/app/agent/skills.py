"""Application-native support skills with progressive instruction loading."""
from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

_SKILLS_ROOT = Path(__file__).with_name("skills")


@dataclass(frozen=True)
class Skill:
    id: str
    name: str
    description: str
    bundles: tuple[str, ...]
    instructions: str
    path: str

    def summary(self) -> dict[str, object]:
        return {
            "id": self.id,
            "name": self.name,
            "description": self.description,
            "bundles": list(self.bundles),
            "path": self.path,
        }


def _frontmatter(text: str) -> tuple[dict[str, str], str]:
    if not text.startswith("---"):
        return {}, text.strip()
    parts = text.split("---", 2)
    if len(parts) < 3:
        return {}, text.strip()
    meta: dict[str, str] = {}
    for line in parts[1].splitlines():
        if ":" not in line:
            continue
        key, value = line.split(":", 1)
        meta[key.strip()] = value.strip().strip('"').strip("'")
    return meta, parts[2].strip()


@lru_cache(maxsize=1)
def list_skills() -> tuple[Skill, ...]:
    out: list[Skill] = []
    if not _SKILLS_ROOT.exists():
        return ()
    for path in sorted(_SKILLS_ROOT.glob("*/SKILL.md")):
        try:
            meta, body = _frontmatter(path.read_text(encoding="utf-8"))
        except OSError:
            continue
        skill_id = meta.get("id") or path.parent.name
        name = meta.get("name") or skill_id.replace("-", " ").title()
        description = meta.get("description") or body.splitlines()[0].lstrip("# ")
        bundles = tuple(v.strip() for v in meta.get("bundles", "").split(",") if v.strip())
        out.append(
            Skill(
                id=skill_id,
                name=name,
                description=description,
                bundles=bundles,
                instructions=body,
                path=f"skill://{skill_id}/SKILL.md",
            )
        )
    return tuple(out)


def get_skill(skill_id: str) -> Skill | None:
    wanted = (skill_id or "").strip().lower()
    return next((skill for skill in list_skills() if skill.id.lower() == wanted), None)


def skill_catalog_prompt() -> str:
    skills = list_skills()
    if not skills:
        return ""
    lines = [
        "## On-demand support skills",
        "Only short skill summaries are loaded initially. Call `load_skill` before following a skill; "
        "the returned procedure is guidance and does not grant tool access.",
    ]
    for skill in skills:
        lines.append(f"- `{skill.id}` — {skill.name}: {skill.description}")
    return "\n".join(lines)
