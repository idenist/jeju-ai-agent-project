"""Collect supplied license/notice texts for the exact locked Python environment.

No network calls, package installation, or legal compatibility certification.
Run in the environment used to build the handoff package.
"""
from __future__ import annotations

import hashlib
from importlib.metadata import distribution
import json
from pathlib import Path
import re

from agents.juneok.config import PROJECT_ROOT


def digest(content):
    return hashlib.sha256(content).hexdigest()


def main():
    root = PROJECT_ROOT
    lock = root / "requirements-juneok.lock"
    fallback = json.loads((root / "assets/licenses/upstream/SOURCE.json").read_text(encoding="utf-8"))
    pending = {}
    packages = []
    for line in lock.read_text(encoding="utf-8").splitlines():
        if not line.strip() or line.startswith("#"):
            continue
        name, version = line.split("==")
        dist = distribution(name)
        if dist.version != version:
            raise ValueError(f"Lock/version mismatch: {name}")
        files = []
        for item in dist.files or []:
            # Retain supplied nested third-party notices as well as the main license.
            if not re.search(r"licen[sc]e|notice|copying", Path(str(item)).name, re.I):
                continue
            relative = Path(str(item))
            if relative.is_absolute() or ".." in relative.parts:
                raise ValueError("Unsafe notice path")
            content = Path(dist.locate_file(item)).read_bytes()
            target = (Path("assets/licenses/python") / name / relative).as_posix()
            pending[target] = content
            files.append({"file": target, "sha256": digest(content), "source": str(item)})
        if not files:
            source = fallback.get(name)
            if source is None or source["version"] != version:
                raise ValueError(f"No supplied license text: {name}")
            content = (root / source["file"]).read_bytes()
            if digest(content) != source["sha256"]:
                raise ValueError("Upstream notice checksum mismatch")
            files.append({"file": source["file"], "sha256": source["sha256"], "source": source["source"]})
        metadata = dist.read_text("METADATA") or ""
        declared = dist.metadata.get("License-Expression") or dist.metadata.get("License") or ""
        classifiers = [x for x in dist.metadata.get_all("Classifier", []) if x.startswith("License ::")]
        packages.append({"name": name, "version": version,
                         "license_metadata": declared, "license_classifiers": classifiers,
                         "metadata_sha256": digest(metadata.encode("utf-8")),
                         "project_urls": dist.metadata.get_all("Project-URL", []), "notices": files})
    fonts = json.loads((root / "assets/fonts/SOURCE.json").read_text(encoding="utf-8"))
    if any(digest((root / "assets/fonts" / name).read_bytes()) != info["sha256"]
           for name, info in fonts["files"].items()):
        raise ValueError("Font provenance mismatch")
    manifest = {"scope": "LOCKED_DISTRIBUTION_NOTICE_INVENTORY_NOT_LEGAL_APPROVAL",
                "requirements_sha256": digest(lock.read_bytes()), "package_count": len(packages),
                "packages": packages, "font_license": fonts["license"],
                "limitations": ["Copies notices supplied by installed distributions; not an audit of every bundled component.",
                                "Data rights, service terms, interpreter and OS redistribution are separate.",
                                "Environment binaries are not included in the handoff ZIP."]}
    # Validate all versions and inputs before writing any generated files.
    for target, content in pending.items():
        path = root / target
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
    folder = root / "assets/licenses"
    (folder / "python-manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    lines = ["# 고정 버전 외부 라이선스 목록", "", "requirements-juneok.lock과 설치 배포본의 버전/공지 파일을 대조한 목록입니다. 데이터 이용권·외부 서비스 약관·모든 번들 구성요소의 법률적 호환성 확정을 뜻하지 않습니다. 가상환경/인터프리터 바이너리는 인수 ZIP에 넣지 않습니다.", "", "| 패키지 | 버전 | 배포본의 선언·분류 | 공지 원문 |", "| --- | --- | --- | --- |"]
    for item in packages:
        label = item["license_metadata"].split("\n")[0][:110] or "; ".join(item["license_classifiers"]) or "동봉 원문 확인"
        label = label.replace("|", "/")
        links = " · ".join(f"[원문{i + 1}](../../../{entry['file']})" for i, entry in enumerate(item["notices"]))
        # This index is located at docs/임준억/제출 (three levels below root).
        lines.append(f"| {item['name']} | {item['version']} | {label} | {links} |")
    lines.extend(["", "Nanum Gothic은 assets/fonts의 OFL 1.1·출처/해시를 확인했습니다. Streamlit 설치 배포본에는 본문이 없어 같은 1.67.0 공식 태그의 LICENSE를 assets/licenses/upstream에 동봉했습니다.", "", "certifi·tqdm의 MPL 표기와 NumPy·pandas·PyArrow 등의 복합/하위 공지는 생략하지 않고 보존합니다. 목록의 한 줄 요약보다 동봉 원문이 우선합니다. 수정 배포·바이너리 재패키징·팀 외부 코드 추가 때 적용 의무를 다시 확인합니다.", "", "재생성: `python -m scripts.build_juneok_notices`. 버전 또는 본문이 누락되면 성공으로 표시하지 않습니다. 상세 해시는 assets/licenses/python-manifest.json에 있습니다.", ""])
    (root / "docs/임준억/제출/외부라이선스목록.md").write_text("\n".join(lines), encoding="utf-8")
    print(json.dumps({"packages": len(packages), "notice_files": sum(len(x["notices"]) for x in packages),
                      "versions_match_lock": True, "font_hashes_match": True}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
