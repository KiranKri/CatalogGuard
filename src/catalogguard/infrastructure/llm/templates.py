"""Load versioned prompt files from prompts/<name>/<version>.md."""

from pathlib import Path

from catalogguard.application.prompts import PromptTemplate


class PromptLibrary:
    def __init__(self, root: Path) -> None:
        self._root = root

    def load(self, name: str, version: str = "v1") -> PromptTemplate:
        text = self._read(name, version)
        if "## system" not in text or "## user" not in text:
            raise ValueError(f"Prompt {name}/{version} must contain ## system and ## user")
        _, rest = text.split("## system", 1)
        system, user = rest.split("## user", 1)
        return PromptTemplate(
            prompt_version=f"{name}/{version}",
            system=system.strip(),
            user=user.strip(),
        )

    def load_text(self, name: str, version: str = "v1") -> str:
        return self._read(name, version)

    def _read(self, name: str, version: str) -> str:
        path = self._root / name / f"{version}.md"
        if not path.is_file():
            raise FileNotFoundError(f"Prompt file not found: {path}")
        return path.read_text(encoding="utf-8")
