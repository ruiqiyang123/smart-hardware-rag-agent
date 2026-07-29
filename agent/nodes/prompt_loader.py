"""Race-resistant loading for trusted, repository-owned agent prompts."""

import os
import stat
from pathlib import Path

from agent.security.secrets import contains_unredacted_secret


MAX_PROMPT_BYTES = 16 * 1024


def contains_forbidden_control(value: str) -> bool:
    return any(
        (ord(character) < 0x20 and character not in {"\n", "\t"})
        or 0x7F <= ord(character) <= 0x9F
        for character in value
    )


def load_safe_prompt(path: Path, prompt_name: str) -> str:
    """Open, inspect and read one prompt through the same no-follow fd."""
    if not isinstance(path, Path) or not path.is_absolute():
        raise RuntimeError(f"{prompt_name} prompt 不可用")
    if not hasattr(os, "O_NOFOLLOW"):
        raise RuntimeError(f"{prompt_name} prompt 不可用")
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | os.O_NOFOLLOW
    descriptor = -1
    try:
        descriptor = os.open(path, flags)
        metadata = os.fstat(descriptor)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_size <= 0
            or metadata.st_size > MAX_PROMPT_BYTES
        ):
            raise RuntimeError(f"{prompt_name} prompt 不可用")
        chunks = []
        total = 0
        while total <= MAX_PROMPT_BYTES:
            chunk = os.read(
                descriptor,
                min(8 * 1024, MAX_PROMPT_BYTES + 1 - total),
            )
            if not chunk:
                break
            chunks.append(chunk)
            total += len(chunk)
        content = b"".join(chunks)
    except RuntimeError:
        raise
    except OSError:
        raise RuntimeError(f"{prompt_name} prompt 不可用") from None
    finally:
        if descriptor >= 0:
            try:
                os.close(descriptor)
            except OSError:
                pass
    if len(content) > MAX_PROMPT_BYTES:
        raise RuntimeError(f"{prompt_name} prompt 不可用")
    try:
        prompt = content.decode("utf-8")
    except UnicodeError:
        raise RuntimeError(f"{prompt_name} prompt 不可用") from None
    if contains_forbidden_control(prompt) or contains_unredacted_secret(prompt):
        raise ValueError(f"{prompt_name} prompt 内容非法")
    prompt = prompt.strip()
    if not prompt:
        raise ValueError(f"{prompt_name} prompt 内容非法")
    return prompt
