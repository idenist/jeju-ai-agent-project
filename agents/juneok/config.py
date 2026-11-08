"""Read only this product's explicit .env path; never search parent projects."""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]


@dataclass(frozen=True)
class Settings:
    api_key: str = field(default="", repr=False)
    model: str = "gpt-5-mini"

    @classmethod
    def load(cls, root: Path = PROJECT_ROOT) -> "Settings":
        from dotenv import dotenv_values
        values = dotenv_values(root / ".env", interpolate=False)
        def get(name: str, fallback: str = "") -> str:
            return str(os.environ.get(name, values.get(name) or fallback)).strip()
        return cls(api_key=get("OPENAI_API_KEY"), model=get("OPENAI_MODEL", "gpt-5-mini"))

    def public_status(self) -> dict:
        return {"env_path": str(PROJECT_ROOT / ".env"), "api_key_present": bool(self.api_key),
                "model": self.model, "precedence": "OS environment > product .env"}
