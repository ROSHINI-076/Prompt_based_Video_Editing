"""Write the application's live OpenAPI contract as YAML."""

from __future__ import annotations

from pathlib import Path

import yaml

from incubrix.api.app import app


def main() -> None:
    """Generate the committed YAML document from the running app definition."""
    target = Path(__file__).resolve().parents[1] / "openapi.yaml"
    target.write_text(
        yaml.safe_dump(app.openapi(), allow_unicode=True, sort_keys=False, width=100),
        encoding="utf-8",
    )
    print(f"Wrote {target}")


if __name__ == "__main__":
    main()
