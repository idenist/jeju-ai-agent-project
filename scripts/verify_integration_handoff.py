"""통합 Agent 최종 인수 점검: 동결 데이터·모델 증빙·회귀 테스트를 분리 검증합니다.

이 명령은 실제 모델 API를 호출하지 않습니다. 통과해도 팀 승인이나 실제 농가 검증을 대신하지 않습니다.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
EXPECTED_IDS = tuple(f"INT-{n:02d}" for n in range(1, 13))
EXPECTED_TURNS = 16


def _json(path: Path):
    return json.loads(path.read_text(encoding="utf-8-sig"))


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _case_definitions(root: Path) -> tuple[tuple[str, ...], int]:
    cases = _json(root / "evals/integration_e2e_cases.json")
    if (not isinstance(cases, list) or not all(isinstance(c, dict) for c in cases)):
        raise ValueError("통합 평가셋이 배열이 아닙니다.")
    ids = tuple(c.get("id") for c in cases)
    if ids != EXPECTED_IDS:
        raise ValueError("평가 문항 ID 또는 순서가 검수 계약과 다릅니다.")
    if any(not isinstance(c.get("turns"), list) or not c["turns"] for c in cases):
        raise ValueError("평가 문항의 사용자 턴이 누락됐습니다.")
    return ids, sum(len(c["turns"]) for c in cases)


def find_latest_full_evidence(root: Path) -> Path | None:
    """단건 또는 부분 평가가 아니라 전체 12문항 평가 디렉터리만 자동 선택합니다."""
    base = root / "artifacts/integration-e2e"
    if not base.is_dir():
        return None
    for path in sorted((p for p in base.iterdir() if p.is_dir()), reverse=True):
        try:
            metadata = _json(path / "metadata.json")
            summary = _json(path / "summary.json")
            if (metadata.get("cases") == list(EXPECTED_IDS)
                    and summary.get("planned_runs") == len(EXPECTED_IDS)
                    and (path / "cases.jsonl").is_file()):
                return path
        except (OSError, ValueError, TypeError, AttributeError, json.JSONDecodeError):
            continue
    return None


def verify_live_evidence(folder: Path, root: Path = ROOT) -> dict:
    """로컬 실제 평가 기록을 현행 소스 해시·평가셋과 대조합니다.

    이미 채점된 결과를 독립 재채점하지는 않습니다. 요약뿐 아니라 모든 문항과 턴의
    완료·통과 상태도 확인하며 원문 질문과 응답은 출력하지 않습니다.
    """
    issues: list[str] = []
    info: dict = {"status": "FAIL", "evidence_present": False, "source_hashes_match": False,
                  "issues": issues, "model": None, "passed_cases": 0, "passed_turns": 0}
    try:
        metadata = _json(folder / "metadata.json")
        summary = _json(folder / "summary.json")
        with (folder / "cases.jsonl").open(encoding="utf-8-sig") as stream:
            cases = [json.loads(line) for line in stream if line.strip()]
        ids, expected_turns = _case_definitions(root)
        if not isinstance(metadata, dict) or not isinstance(summary, dict):
            raise ValueError("메타데이터나 요약의 형식이 올바르지 않습니다.")
        info["evidence_present"] = True
        info["model"] = metadata.get("model")
        if not metadata.get("actual_model_api_called") is True:
            issues.append("실제 모델 API 호출 증빙이 없습니다.")
        if not isinstance(metadata.get("model"), str) or not metadata["model"].strip() or summary.get("model") != metadata["model"]:
            issues.append("평가 모델 정보가 일치하지 않습니다.")
        if metadata.get("cases") != list(ids) or metadata.get("repeats") != 1 or metadata.get("expected_turns_per_repeat") != expected_turns:
            issues.append("고정 평가셋 또는 반복·턴 수 계약이 다릅니다.")
        if (summary.get("planned_runs") != len(ids) or summary.get("completed_runs") != len(ids)
                or summary.get("passed_runs") != len(ids) or summary.get("complete") is not True
                or summary.get("stop_reason") is not None or summary.get("failure_counts")):
            issues.append("전체 평가 요약이 완료·전부 통과 조건을 만족하지 않습니다.")
        preflight = metadata.get("preflight")
        if (not isinstance(preflight, dict) or preflight.get("status") != "OK"
                or preflight.get("verified_availability_rows") != 4776
                or preflight.get("activated_scenario_tool") is not False):
            issues.append("평가 당시 데이터 사전검증 또는 활성 도구 구성이 다릅니다.")
        if not isinstance(cases, list) or [c.get("id") for c in cases if isinstance(c, dict)] != list(ids) or len(cases) != len(ids):
            issues.append("실제 문항 기록이 누락되거나 중복됐습니다.")
        else:
            if any(c.get("passed") is not True or c.get("abort_reason") is not None
                   or c.get("completed_turns") != c.get("expected_turns")
                   or not isinstance(c.get("turns"), list) or len(c["turns"]) != c.get("expected_turns")
                   for c in cases):
                issues.append("실제 문항 기록에 실패·중단·누락 턴이 있습니다.")
            turns = [t for c in cases for t in (c.get("turns") if isinstance(c.get("turns"), list) else [])]
            info["passed_cases"] = sum(c.get("passed") is True for c in cases)
            info["passed_turns"] = sum(t.get("passed") is True for t in turns if isinstance(t, dict))
            if (len(turns) != expected_turns or any(not isinstance(t, dict) or t.get("passed") is not True
                    or t.get("run_status") != "OK" or t.get("failures") for t in turns)):
                issues.append("실제 턴 기록이 모두 정상 통과하지 않았습니다.")
        source = metadata.get("source_sha256")
        required = {"app.py", "agents/juneok/agent.py", "agents/juneok/tools.py",
                    "agents/juneok/workspace.py", "agents/common/price_history_service.py",
                    "data/processed/daily_prices.csv", "data/processed/price_comparison_availability.csv",
                    "evals/integration_e2e_cases.json", "scripts/evaluate_integrated_agent.py"}
        if not isinstance(source, dict) or not required.issubset(source):
            issues.append("평가 당시 핵심 소스 해시 목록이 누락됐습니다.")
        else:
            mismatches = []
            for rel, digest in source.items():
                parts = Path(rel).parts if isinstance(rel, str) else ()
                if (not parts or Path(rel).is_absolute() or any(p in {"..", "."} for p in parts)
                        or not isinstance(digest, str) or len(digest) != 64):
                    mismatches.append("경로 또는 SHA-256 형식 오류")
                    continue
                try:
                    if _sha(root.joinpath(*parts)) != digest:
                        mismatches.append(rel)
                except OSError:
                    mismatches.append(rel)
            if mismatches:
                issues.append(f"평가 이후 변경되었거나 누락된 소스 {len(mismatches)}개")
            else:
                info["source_hashes_match"] = True
    except (OSError, UnicodeError, ValueError, TypeError, AttributeError, KeyError, json.JSONDecodeError) as exc:
        issues.append(f"평가 기록을 검증할 수 없습니다: {type(exc).__name__}")
    info["status"] = "OK" if not issues else "FAIL"
    return info


def verify_repository(root: Path) -> dict:
    """현재 체크아웃의 버전·비밀 파일 추적 상태를 요약합니다."""
    def git(*args):
        return subprocess.run(["git", *args], cwd=root, capture_output=True, text=True, encoding="utf-8", errors="replace")
    try:
        branch = git("branch", "--show-current")
        head = git("rev-parse", "HEAD")
        dirty = git("status", "--porcelain", "--untracked-files=normal")
        tracked = git("ls-files")
        if any(x.returncode != 0 for x in (branch, head, dirty, tracked)):
            return {"status": "FAIL", "reason": "GIT_READ_FAILED"}
        lines = tracked.stdout.splitlines()
        secrets = [p for p in lines if (p == ".env" or p.startswith(".env.") and p != ".env.example"
                    or p.startswith("artifacts/") or p.startswith(".venv/")
                    or p.endswith("/secrets.toml") or p == "secrets.toml")]
        return {"status": "OK" if not secrets else "FAIL", "branch": branch.stdout.strip(),
                "head": head.stdout.strip(), "working_tree_clean": not dirty.stdout.strip(),
                "prohibited_tracked_files": len(secrets)}
    except OSError:
        return {"status": "FAIL", "reason": "GIT_UNAVAILABLE"}


def verify_data(root: Path = ROOT) -> dict:
    from agents.common.data_contract import verify_integrated_price_sources
    from agents.jeonghyeon.price_scenario import content_sha256
    try:
        result = verify_integrated_price_sources(root)
        if result.get("status") != "OK" or result.get("full_content_verified") is not True:
            return {"status": "FAIL", "reason": result.get("reason", "DATA_VERIFICATION_FAILED")}
        report = _json(root / "data/reports/price_comparison_report.json")
        name = "price_comparison_availability.csv"
        expected = (report.get("output_sha256") or {}).get(name)
        actual = content_sha256((root / "data/processed" / name).read_bytes())
        if expected != actual:
            return {"status": "FAIL", "reason": "PAIR_REPORT_HASH_MISMATCH"}
        from agents.juneok.tools import TOOLS
        names = {tool["name"] for tool in TOOLS}
        if not {"get_price_history", "compare_shipment_options"}.issubset(names) or "get_price_scenarios" in names:
            return {"status": "FAIL", "reason": "ACTIVE_TOOL_CONTRACT_CHANGED"}
        return {"status": "OK", "full_content_verified": True,
                "verified_comparison_rows": result.get("verified_comparison_rows"),
                "verified_availability_rows": result.get("verified_availability_rows"),
                "price_scenario_tool_active": False}
    except (OSError, ValueError, TypeError, KeyError, ImportError, json.JSONDecodeError) as exc:
        return {"status": "FAIL", "reason": "DATA_CHECK_ERROR", "error_type": type(exc).__name__}


def _test_count(log: str) -> int | None:
    """테스트 발견 실패가 0개 성공으로 처리되지 않도록 실행 개수를 읽습니다."""
    matches = re.findall(r"Ran\s+(\d+)\s+tests?", log)
    return int(matches[-1]) if matches else None


def run_offline_tests(root: Path, output: Path) -> list[dict]:
    """외부 API를 호출하지 않는 테스트·평가 데이터 검증을 순서대로 실행합니다."""
    commands = [
        ("전체 단위 테스트", ["-m", "unittest", "discover", "-s", "tests"], 543),
        ("평가기 단위 테스트", ["-m", "unittest", "discover", "-s", "evals", "-p", "test_*.py"], 14),
        ("통합 평가 사전검증", ["-m", "scripts.evaluate_integrated_agent", "--validate"], None),
    ]
    results = []
    for index, (name, args, minimum_tests) in enumerate(commands, 1):
        log_name = f"검증_{index}.log"
        print(f"[{index}/{len(commands)}] {name} 실행", flush=True)
        with (output / log_name).open("w", encoding="utf-8") as log:
            try:
                p = subprocess.run([sys.executable, *args], cwd=root, stdout=log,
                                   stderr=subprocess.STDOUT, timeout=600, check=False)
                returncode = p.returncode
            except subprocess.TimeoutExpired:
                returncode = -1
                log.write("\n제한 시간을 초과해 중단했습니다.\n")
            except OSError as exc:
                returncode = -2
                log.write(f"\n테스트 실행 오류: {type(exc).__name__}\n")
        observed = _test_count((output / log_name).read_text(encoding="utf-8", errors="replace"))
        if minimum_tests is not None and (observed is None or observed < minimum_tests):
            returncode = -3 if returncode == 0 else returncode
        results.append({"name": name, "status": "OK" if returncode == 0 else "FAIL",
                        "returncode": returncode, "test_count": observed,
                        "minimum_expected_tests": minimum_tests, "log": log_name})
    return results


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--evidence", type=Path, help="12문항 전체 평가의 metadata/summary/cases가 있는 폴더")
    parser.add_argument("--full", action="store_true", help="전체 단위 테스트·평가 테스트·무과금 검증까지 실행")
    parser.add_argument("--require-clean", action="store_true", help="인수 확정 시 Git 작업트리 청결 상태도 요구")
    args = parser.parse_args()
    folder = args.evidence or find_latest_full_evidence(ROOT)
    evidence = verify_live_evidence(folder, ROOT) if folder else {"status": "FAIL", "issues": ["12문항 전체 평가 기록 없음"]}
    repository = verify_repository(ROOT)
    data = verify_data(ROOT)
    stamp = datetime.now(timezone(timedelta(hours=9))).strftime("%Y%m%d_%H%M%S_%f")
    output = ROOT / "artifacts/integration-handoff" / stamp
    output.mkdir(parents=True, exist_ok=True)
    tests = run_offline_tests(ROOT, output) if args.full else []
    checks_ok = (evidence["status"] == "OK" and data["status"] == "OK"
                 and repository["status"] == "OK" and repository.get("branch") == "integration/agents")
    if args.require_clean and not repository.get("working_tree_clean"):
        checks_ok = False
    if args.full and not all(t["status"] == "OK" for t in tests):
        checks_ok = False
    verdict = {"status": ("PASS" if args.full else "PREFLIGHT_PASS") if checks_ok else "FAIL",
               "scope": "기술 인수 점검만 해당. 실제 농가 검증·외부 자료 재배포 허용·팀 승인·main 병합 미포함",
               "paid_model_called": False, "tests_executed": args.full,
               "evidence": evidence, "data": data, "repository": repository, "tests": tests,
               "working_tree_clean_required": args.require_clean,
               "manual_open_issues": ["INT-04 명시 등급·크기 누락", "INT-11 중량 충돌 안내 부족",
                                      "실사용자 검증·#855 자료 이용조건·팀 main 병합 승인 미완료"]}
    (output / "summary.json").write_text(json.dumps(verdict, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({**verdict, "report_folder": str(output)}, ensure_ascii=False, indent=2))
    return 0 if checks_ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
