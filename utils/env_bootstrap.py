"""Load runtime environment before frameworks snapshot process settings."""

from pathlib import Path

from dotenv import load_dotenv


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def bootstrap_environment() -> bool:
    """Load cwd `.env` first, falling back to the project file without overrides."""

    cwd_dotenv = Path.cwd() / ".env"
    dotenv_path = cwd_dotenv if cwd_dotenv.is_file() else PROJECT_ROOT / ".env"
    return load_dotenv(dotenv_path=dotenv_path, override=False)
