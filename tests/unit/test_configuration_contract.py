"""Configuration completeness and source hard-coded-value checks."""

from __future__ import annotations

import re
from pathlib import Path

from incubrix.config import Settings

ROOT = Path(__file__).resolve().parents[2]


def test_every_settings_field_is_documented() -> None:
    names = {
        match.group(1)
        for line in (ROOT / ".env.example").read_text(encoding="utf-8").splitlines()
        if (match := re.match(r"^\s*([A-Z][A-Z0-9_]*)=", line))
    }

    assert set(Settings.model_fields) <= names
    readme_settings = set(
        re.findall(r"`([A-Z][A-Z0-9_]*)`\s*\|", (ROOT / "README.md").read_text(encoding="utf-8"))
    )
    assert set(Settings.model_fields) <= readme_settings
    compose = (ROOT / "docker-compose.yml").read_text(encoding="utf-8")
    assert "AUTH_DISABLED" not in compose


def test_package_has_no_embedded_credentials_paths_or_hosted_model_ids() -> None:
    absolute_path = re.compile(r"(?:[A-Z]:\\(?:Users|home)\\|/(?:home|Users)/)")
    credential_value = re.compile(
        r"""(?i)(?:api[_-]?key|access[_-]?key|secret|token)\s*[:=]\s*['"][^'"]{8,}['"]"""
    )
    hosted_model_id = re.compile(
        r"(?:llama-\d(?:\.\d)?-[\w-]+|sentence-transformers/[\w.-]+|api\.groq\.com)"
    )
    package_root = ROOT / "incubrix"

    for source_path in package_root.rglob("*.py"):
        content = source_path.read_text(encoding="utf-8")
        assert absolute_path.search(content) is None, source_path
        assert credential_value.search(content) is None, source_path
        assert hosted_model_id.search(content) is None, source_path
