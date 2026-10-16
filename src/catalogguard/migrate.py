"""Apply Alembic migrations. Used by `python -m catalogguard.migrate` and Docker."""

from pathlib import Path

from alembic import command
from alembic.config import Config


def _config() -> Config:
    candidates = [
        Path.cwd() / "alembic.ini",
        Path(__file__).resolve().parents[2] / "alembic.ini",
    ]
    for path in candidates:
        if path.is_file():
            return Config(str(path))
    raise RuntimeError("alembic.ini not found. Run this from the repository root.")


def main() -> None:
    command.upgrade(_config(), "head")


if __name__ == "__main__":
    main()
