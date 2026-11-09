"""프로젝트 루트 환경설정과 환경변수 우선순위를 검증한다."""
from __future__ import annotations
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from dotenv import load_dotenv


class EnvLoadingTests(unittest.TestCase):
    def test_dotenv_loads_when_no_environment_key(self):
        with tempfile.TemporaryDirectory() as tmp:
            env = Path(tmp) / ".env"
            env.write_text("OPENAI_API_KEY=test-value\n", encoding="utf-8")
            with patch.dict(os.environ, {}, clear=True):
                load_dotenv(dotenv_path=env, override=False)
                self.assertEqual(os.getenv("OPENAI_API_KEY"), "test-value")

    def test_existing_environment_variable_has_priority(self):
        with tempfile.TemporaryDirectory() as tmp:
            env = Path(tmp) / ".env"
            env.write_text("OPENAI_API_KEY=from-file\n", encoding="utf-8")
            with patch.dict(os.environ, {"OPENAI_API_KEY": "from-shell"}, clear=True):
                load_dotenv(dotenv_path=env, override=False)
                self.assertEqual(os.getenv("OPENAI_API_KEY"), "from-shell")


if __name__ == "__main__":
    unittest.main()
