"""최종 인수 점검기가 불완전한 모델 평가 증빙을 승인하지 않는지 검증합니다."""
from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from scripts import verify_integration_handoff as 검수


class 인수증빙검수(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.evidence = self.root / "artifacts/integration-e2e/전체평가"
        self.evidence.mkdir(parents=True)
        ids = list(검수.EXPECTED_IDS)
        turn_counts = [1, 2, 2, 1, 1, 1, 2, 1, 1, 1, 1, 2]
        self.assertEqual(sum(turn_counts), 검수.EXPECTED_TURNS)
        cases = [{"id": name, "turns": [{"user": "평가 질문"}] * count}
                 for name, count in zip(ids, turn_counts)]
        self._write(self.root / "evals/integration_e2e_cases.json", cases)
        required = ["app.py", "agents/juneok/agent.py", "agents/juneok/tools.py",
                    "agents/juneok/workspace.py", "agents/common/price_history_service.py",
                    "data/processed/daily_prices.csv", "data/processed/price_comparison_availability.csv",
                    "evals/integration_e2e_cases.json", "scripts/evaluate_integrated_agent.py"]
        for path in required:
            p = self.root / path
            if not p.exists():
                p.parent.mkdir(parents=True, exist_ok=True)
                p.write_text("통합 코드 또는 자료", encoding="utf-8")
        source = {p: hashlib.sha256((self.root / p).read_bytes()).hexdigest() for p in required}
        self.metadata = {"model": "검수모델", "cases": ids, "repeats": 1,
                         "expected_turns_per_repeat": 16, "actual_model_api_called": True,
                         "preflight": {"status": "OK", "verified_availability_rows": 4776,
                                       "activated_scenario_tool": False}, "source_sha256": source}
        self.summary = {"model": "검수모델", "planned_runs": 12, "completed_runs": 12,
                        "passed_runs": 12, "complete": True, "stop_reason": None, "failure_counts": {}}
        self.rows = [{"id": name, "passed": True, "completed_turns": count, "expected_turns": count,
                      "abort_reason": None, "turns": [{"passed": True, "run_status": "OK", "failures": []}
                                                    for _ in range(count)]}
                     for name, count in zip(ids, turn_counts)]
        self.save()

    def _write(self, path: Path, value):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")

    def save(self):
        self._write(self.evidence / "metadata.json", self.metadata)
        self._write(self.evidence / "summary.json", self.summary)
        (self.evidence / "cases.jsonl").write_text(
            "\n".join(json.dumps(r, ensure_ascii=False) for r in self.rows) + "\n", encoding="utf-8")

    def check(self):
        return 검수.verify_live_evidence(self.evidence, self.root)

    def test_정상_전체_평가와_코드_해시는_통과(self):
        result = self.check()
        self.assertEqual(result["status"], "OK", result["issues"])
        self.assertEqual((result["passed_cases"], result["passed_turns"]), (12, 16))
        self.assertEqual(검수._test_count("Ran 543 tests in 99.0s\nOK"), 543)
        self.assertIsNone(검수._test_count("진단 결과만 출력"))

    def test_파일이_없으면_실패(self):
        (self.evidence / "cases.jsonl").unlink()
        self.assertEqual(self.check()["status"], "FAIL")

    def test_일부_평가만_통과하면_실패(self):
        self.summary["passed_runs"] = 11
        self.save()
        self.assertEqual(self.check()["status"], "FAIL")

    def test_문항_중복을_실패로_판정(self):
        self.rows[3]["id"] = "INT-03"
        self.save()
        self.assertEqual(self.check()["status"], "FAIL")

    def test_실패한_턴을_통과로_위장할_수_없음(self):
        self.rows[2]["turns"][0]["passed"] = False
        self.save()
        self.assertEqual(self.check()["status"], "FAIL")

    def test_모델_API_호출_사실이_없으면_실패(self):
        self.metadata["actual_model_api_called"] = False
        self.save()
        self.assertEqual(self.check()["status"], "FAIL")

    def test_이전_소스_버전_평가를_재사용하지_않음(self):
        (self.root / "agents/juneok/agent.py").write_text("변경된 코드", encoding="utf-8")
        self.assertEqual(self.check()["status"], "FAIL")
        self.assertFalse(self.check()["source_hashes_match"])

    def test_다른_모델_요약과_합쳐진_증빙을_차단(self):
        self.summary["model"] = "다른모델"
        self.save()
        self.assertEqual(self.check()["status"], "FAIL")

    def test_범위_밖_소스_경로는_읽지_않음(self):
        self.metadata["source_sha256"]["../키파일"] = "a" * 64
        self.save()
        self.assertEqual(self.check()["status"], "FAIL")

    def test_단일_문항_평가를_전체_기록으로_선택하지_않음(self):
        smaller = self.root / "artifacts/integration-e2e/더최근"
        smaller.mkdir(parents=True)
        self._write(smaller / "metadata.json", {"cases": ["INT-04"]})
        self._write(smaller / "summary.json", {"planned_runs": 1})
        (smaller / "cases.jsonl").write_text("{}\n", encoding="utf-8")
        self.assertEqual(검수.find_latest_full_evidence(self.root), self.evidence)

    def test_평가셋_정의가_달라지면_실패(self):
        cases = json.loads((self.root / "evals/integration_e2e_cases.json").read_text(encoding="utf-8"))
        cases[-1]["id"] = "INT-99"
        self._write(self.root / "evals/integration_e2e_cases.json", cases)
        self.assertEqual(self.check()["status"], "FAIL")


class 인수실행검수(unittest.TestCase):
    def test_기본모드는_전체_테스트를_호출하지_않음(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            with (patch.object(검수, "ROOT", root), patch("sys.argv", ["인수검수"]),
                  patch.object(검수, "find_latest_full_evidence", return_value=Path("임시")),
                  patch.object(검수, "verify_live_evidence", return_value={"status": "OK"}),
                  patch.object(검수, "verify_repository", return_value={"status": "OK", "branch": "integration/agents"}),
                  patch.object(검수, "verify_data", return_value={"status": "OK"}),
                  patch.object(검수, "run_offline_tests") as runner):
                self.assertEqual(검수.main(), 0)
                runner.assert_not_called()


if __name__ == "__main__":
    unittest.main()
