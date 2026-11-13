"""Assess published center constraints on this Windows machine, not physical center identity."""
from __future__ import annotations

import argparse
import ctypes
from ctypes import wintypes
import hashlib
import json
import os
import platform
import re
from pathlib import Path
import struct
import subprocess
import sys
import time
from unittest.mock import patch
from agents.juneok.config import PROJECT_ROOT


def windows_process():
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.GetCurrentProcess.restype = wintypes.HANDLE
    return kernel, kernel.GetCurrentProcess()


def constrain_worker_cpu():
    if platform.system() != "Windows":
        return False
    kernel, handle = windows_process()
    mask, system = ctypes.c_size_t(), ctypes.c_size_t()
    kernel.GetProcessAffinityMask.argtypes = [wintypes.HANDLE, ctypes.POINTER(ctypes.c_size_t), ctypes.POINTER(ctypes.c_size_t)]
    kernel.SetProcessAffinityMask.argtypes = [wintypes.HANDLE, ctypes.c_size_t]
    if not kernel.GetProcessAffinityMask(handle, ctypes.byref(mask), ctypes.byref(system)):
        return False
    one_cpu = mask.value & -mask.value
    return bool(one_cpu and kernel.SetProcessAffinityMask(handle, one_cpu))


def peak_memory():
    if platform.system() != "Windows":
        return None
    class Counters(ctypes.Structure):
        _fields_ = [("cb", wintypes.DWORD), ("PageFaultCount", wintypes.DWORD)] + [
            (name, ctypes.c_size_t) for name in ["PeakWorkingSetSize", "WorkingSetSize", "QuotaPeakPagedPoolUsage",
            "QuotaPagedPoolUsage", "QuotaPeakNonPagedPoolUsage", "QuotaNonPagedPoolUsage", "PagefileUsage", "PeakPagefileUsage", "PrivateUsage"]]
    _, handle = windows_process()
    counter = Counters()
    counter.cb = ctypes.sizeof(counter)
    psapi = ctypes.WinDLL("psapi", use_last_error=True)
    psapi.GetProcessMemoryInfo.argtypes = [wintypes.HANDLE, ctypes.c_void_p, wintypes.DWORD]
    if not psapi.GetProcessMemoryInfo(handle, ctypes.byref(counter), counter.cb):
        return None
    return {"peak_working_set_bytes": counter.PeakWorkingSetSize,
            "peak_committed_bytes": counter.PeakPagefileUsage}


def worker(mode):
    single_cpu = constrain_worker_cpu()
    started = time.perf_counter()
    import requests
    import httpx
    with patch.object(requests.sessions.Session, "request", side_effect=AssertionError("HTTP_DISABLED")), \
         patch.object(httpx.Client, "send", side_effect=AssertionError("HTTP_DISABLED")):
        if mode == "csv":
            from agents.juneok.cli import offline_demo
            from agents.juneok.sensitivity import compare_sensitivity
            from agents.juneok.state import demo_scenario
            statuses = [x["result"]["status"] for x in offline_demo()]
            assert statuses == ["OK", "OK", "OK", "NO_DATA"]
            grid = compare_sensitivity(demo_scenario(), "seven_days", [1, 2, 4], [3000, 3300])
            assert grid["status"] == "OK" and len(grid["rows"]) == 6
        else:
            from streamlit.testing.v1 import AppTest
            app = AppTest.from_file(str(PROJECT_ROOT / "app.py"), default_timeout=30).run()
            assert not list(app.exception) and app.chat_input[0].disabled
            next(b for b in app.button if b.label == "가상 예시 조건 불러오기").click().run()
            next(b for b in app.button if b.label == "입력 조건 비교").click().run()
            next(b for b in app.button if b.label == "과거 출하량 조회").click().run()
            next(x for x in app.text_input if x.key == "sensitivity_losses").set_value("1,2,4").run()
            next(x for x in app.text_input if x.key == "sensitivity_prices").set_value("3000,3300").run()
            next(b for b in app.button if b.label == "가정 범위 비교").click().run()
            assert not list(app.exception)
            assert len(app.session_state.sensitivity_result["rows"]) == 6
            from io import BytesIO
            from pypdf import PdfReader
            pdf = app.session_state.pdf_cache[1]
            assert pdf.startswith(b"%PDF-")
            assert "조건 버전" in "\n".join(p.extract_text() for p in PdfReader(BytesIO(pdf)).pages)
    return {"mode": mode, "passed": True, "single_logical_cpu": single_cpu,
            "http_clients_blocked": True, "keys_blank": not os.environ.get("OPENAI_API_KEY") and not os.environ.get("DATA_GO_KR_KEY"),
            "elapsed_seconds": round(time.perf_counter() - started, 3), "memory": peak_memory()}


def main():
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--worker", choices=["csv", "ui"])
    args = parser.parse_args()
    if args.worker:
        print(json.dumps(worker(args.worker)))
        return 0
    folder = PROJECT_ROOT / "artifacts/center-spec"
    folder.mkdir(parents=True, exist_ok=True)
    env = dict(os.environ)
    env.update(OPENAI_API_KEY="", DATA_GO_KR_KEY="", OMP_NUM_THREADS="1", OPENBLAS_NUM_THREADS="1",
               STREAMLIT_BROWSER_GATHER_USAGE_STATS="false", TEMP=str(PROJECT_ROOT / "artifacts/tmp"), TMP=str(PROJECT_ROOT / "artifacts/tmp"))
    (PROJECT_ROOT / "artifacts/tmp").mkdir(parents=True, exist_ok=True)
    results = []
    for mode in ["csv", "ui"]:
        result = subprocess.run([sys.executable, "-X", "utf8", "-m", "scripts.verify_juneok_center_spec", "--worker", mode],
                                cwd=PROJECT_ROOT, env=env, capture_output=True, text=True, encoding="utf-8", timeout=60)
        (folder / (mode + ".stderr.log")).write_text(result.stderr, encoding="utf-8")
        results.append(json.loads(result.stdout) if result.returncode == 0 else {"mode": mode, "passed": False, "exit_code": result.returncode})
    checks = {"windows_11_x64_family": platform.system() == "Windows" and platform.release() == "11" and struct.calcsize("P") == 8,
        "python_313": sys.version_info[:2] == (3, 13), "workflows": all(r["passed"] for r in results),
        "single_cpu_workers": all(r.get("single_logical_cpu") for r in results),
        "python_peak_below_512_mib": all(r.get("memory") and max(r["memory"].values()) < 512 * 1024**2 for r in results)}
    from agents.juneok.data_refresh import market_status
    report = {"scope": "PUBLISHED_SPEC_COMPATIBILITY_ON_LOCAL_WINDOWS", "assessment": "PASS_WITH_STATED_SCOPE" if all(checks.values()) else "REVIEW_REQUIRED",
        "published_target": {"os": "Windows 11 (official answer: 26H2)", "cpu": "Intel Core i7-8700", "gpu": "GTX 1060", "ram_gib": 32},
        "sources": ["https://daker.ai/public/hackathons/jeju-ai-bigdata-agent-challenge", "https://daker.ai/public/hackathons/jeju-ai-bigdata-agent-challenge?section=board&postId=3d8a1ba3-d0d8-4667-9e0e-b6d5d671e6d5"],
        "observed_machine": {"platform": platform.platform(), "processor": platform.processor(), "python": platform.python_version(), "executable": sys.executable},
        "checks": checks, "workers": results, "market": market_status(), "local_gpu_inference_required": False,
        "physical_center_executed": False, "limits": ["Host CPU is not an i7-8700 emulator; timings are local observations.",
        "Peak memory covers each Python worker, not the browser or all PC processes.", "Windows 11 family tested; exact 26H2 hardware/driver image is not reproduced.",
        "512 MiB is a conservative project check budget, not an organizer requirement or an OS memory cap."]}
    candidates = sorted(p for p in (PROJECT_ROOT / "artifacts/evaluation-runs").glob("*.json") if re.fullmatch(r"\d{8}T\d+\.json", p.name))
    live = candidates[-1] if candidates else None
    if live:
        data = json.loads(live.read_text(encoding="utf-8"))
        report["prior_live_model_evidence"] = {"file": live.relative_to(PROJECT_ROOT).as_posix(), "passed": data["passed_runs"], "total": data["expected_runs"],
            "source_mismatches": [p for p, sha in data["source_sha256"].items() if hashlib.sha256((PROJECT_ROOT / p).read_bytes()).hexdigest() != sha]}
    (folder / "result.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if all(checks.values()) else 1


if __name__ == "__main__":
    raise SystemExit(main())
