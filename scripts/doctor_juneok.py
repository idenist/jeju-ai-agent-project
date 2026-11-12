"""Readiness evidence for THIS machine; does not certify the actual center PC."""
from __future__ import annotations

import argparse
import importlib.metadata
import json
import platform
import shutil
import socket
import sys
from pathlib import Path
from uuid import uuid4
from agents.juneok.config import PROJECT_ROOT, Settings
from agents.juneok.data_refresh import market_status
from agents.juneok.cli import offline_demo


def main():
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--network", action="store_true", help="Probe public HTTPS only; no key transmitted")
    args = parser.parse_args()
    folder = PROJECT_ROOT / "artifacts"
    folder.mkdir(exist_ok=True)
    path = folder / ("write-probe-" + uuid4().hex + ".tmp")
    checks = {}
    try:
        path.write_text("probe", encoding="utf-8")
        checks["workspace_writable"] = path.read_text(encoding="utf-8") == "probe"
    finally:
        path.unlink(missing_ok=True)
    try:
        with socket.socket() as server:
            server.bind(("127.0.0.1", 0))
            checks["localhost_bind"] = True
    except OSError:
        checks["localhost_bind"] = False
    checks["csv_tools"] = [r["result"]["status"] for r in offline_demo()] == ["OK", "OK", "OK", "NO_DATA"]
    evidence = {"scope": "CURRENT_MACHINE_ONLY", "python_executable": sys.executable,
        "python_version": platform.python_version(), "os": platform.platform(), "architecture": platform.machine(),
        "packages": {p: importlib.metadata.version(p) for p in ["openai", "streamlit", "requests", "python-dotenv"]},
        "free_disk_bytes": shutil.disk_usage(PROJECT_ROOT).free,
        "openai_key_present": bool(Settings.load().api_key), "market_data": market_status(),
        "checks": checks, "network": {}, "model_called": False,
        "center_pc_verified": False, "farmer_usability_verified": False}
    if args.network:
        import requests
        try:
            r = requests.get("https://api.openai.com/v1/models", timeout=10)
            evidence["network"] = {"openai_https_reachable": True, "http_status": r.status_code,
                                   "credentials_sent": False, "note": "HTTPS 연결 확인과 계정 인증·이용 한도 확인은 별개입니다."}
        except requests.RequestException as exc:
            evidence["network"] = {"openai_https_reachable": False, "error_type": type(exc).__name__, "credentials_sent": False}
    (folder / "readiness.json").write_text(json.dumps(evidence, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(evidence, ensure_ascii=False, indent=2))
    return 0 if all(checks.values()) else 1


if __name__ == "__main__":
    raise SystemExit(main())
