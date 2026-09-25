"""The shipped `.env.example` must actually work.

`cp .env.example .env` is the first instruction a new user follows, and the file drifted
far enough from `Settings` that following it raised a ValidationError on startup: it still
set `DK_LLM_PROVIDER` and `DK_VISION_PROVIDER` (fields that no longer exist) and used the
value `fake` for providers that now accept only `mock` or `openai`. Documentation that is
wrong about configuration is worse than absent -- it fails at the first step, before the
user has any way to tell the difference between a broken app and a broken example.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from douyin_knowledge.config.settings import Settings

ENV_EXAMPLE = Path(__file__).resolve().parents[2] / ".env.example"


@pytest.fixture(scope="module")
def env_lines() -> list[str]:
    assert ENV_EXAMPLE.exists(), f"{ENV_EXAMPLE} must be committed"
    return ENV_EXAMPLE.read_text(encoding="utf-8").splitlines()


def _assignments(lines: list[str]) -> dict[str, str]:
    """Active (uncommented) assignments only."""
    out = {}
    for line in lines:
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, _, value = stripped.partition("=")
        out[key.strip()] = value.strip()
    return out


def test_the_example_file_loads_as_settings(tmp_path: Path) -> None:
    """The whole point: copying the file must produce a working configuration."""
    target = tmp_path / ".env"
    target.write_text(ENV_EXAMPLE.read_text(encoding="utf-8"), encoding="utf-8")
    settings = Settings(_env_file=str(target))
    assert settings.ai_provider == "mock"


def test_the_example_defaults_to_demo_mode(tmp_path: Path) -> None:
    """A copied example must not be able to spend money or leave the machine.

    Someone evaluating the project should reach a working system without a key, and
    should never discover they were billed for the walkthrough.
    """
    target = tmp_path / ".env"
    target.write_text(ENV_EXAMPLE.read_text(encoding="utf-8"), encoding="utf-8")
    settings = Settings(_env_file=str(target))
    assert not settings.uses_real_providers()
    assert settings.capture_provider == "fixture"


def test_every_documented_key_is_a_real_setting(env_lines: list[str]) -> None:
    """Guards the drift that broke the file: keys removed from Settings but left here.

    Checked against commented-out lines too, since those are the ones a user uncomments
    and the failure mode is identical.
    """
    fields = set(Settings.model_fields)
    documented = set()
    for line in env_lines:
        stripped = line.strip().lstrip("#").strip()
        if not stripped or "=" not in stripped or not stripped.startswith("DK_"):
            continue
        documented.add(stripped.partition("=")[0].strip())

    unknown = {
        key for key in documented if key.removeprefix("DK_").lower() not in fields
    }
    assert not unknown, f"documented but not a Settings field: {sorted(unknown)}"


def test_no_secret_has_a_value(env_lines: list[str]) -> None:
    """SEC-001 at the level of the file most likely to be filled in and then committed."""
    active = _assignments(env_lines)
    for key in (
        "DK_OPENAI_API_KEY",
        "DK_DOUYIN_SIDECAR_API_KEY",
        "DK_DOUBAO_ASR_API_KEY",
    ):
        assert key not in active, f"{key} must stay commented out in the example"

    text = "\n".join(env_lines)
    # `sk-your-key-here` is the one allowed occurrence: it is inside a comment and is
    # obviously a placeholder. Anything else shaped like a live key is a leak.
    assert text.count("sk-") == text.count("sk-your-key-here")


def test_the_example_documents_a_loopback_only_api(env_lines: list[str]) -> None:
    active = _assignments(env_lines)
    assert active.get("DK_API_HOST") == "127.0.0.1"
