"""Portable local case files. Imported outputs are archival data, never commands."""
from copy import deepcopy
from datetime import datetime, timezone, timedelta
import hashlib
import json
from .comparison import validate_partial, TOP_FIELDS
from .ui_workflow import INPUT_CONTEXTS
from .task_context import validate_context

MAX_BYTES = 1024 * 1024
FORMAT = "jeju-shipping-case"


def _canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")


def export_case(workspace, name):
    if not isinstance(name, str) or not 1 <= len(name.strip()) <= 80:
        raise ValueError("비교 이름은 1~80자로 입력하세요.")
    if workspace.check_data_change():
        raise ValueError("사용 자료가 바뀌었습니다. 다시 계산한 뒤 저장하세요.")
    payload = {"name": name.strip(), "created_at_kst": datetime.now(timezone(timedelta(hours=9))).isoformat(),
        "input_context": workspace.input_context, "snapshot": workspace.store.snapshot(),
        "history": deepcopy(workspace.store.history[-100:]), "data_revision": list(workspace.data_revision),
        "archived_results": [x for x in workspace.current_entries() if x.get("tool") != "reconcile_settlement"], "task_context": workspace.session.task_context.snapshot()}
    content = _canonical(payload)
    result = _canonical({"format": FORMAT, "version": 2, "payload": payload, "sha256": hashlib.sha256(content).hexdigest()})
    if len(result) > MAX_BYTES:
        raise ValueError("비교 파일이 1MiB를 초과합니다. 결과를 줄여 저장하세요.")
    return result


def parse_case(content):
    try:
        if not isinstance(content, bytes) or len(content) > MAX_BYTES:
            raise ValueError("비교 파일은 1MiB 이하 JSON이어야 합니다.")
        def unique(pairs):
            result = {}
            for key, value in pairs:
                if key in result:
                    raise ValueError("중복 필드가 있습니다.")
                result[key] = value
            return result
        def invalid(value):
            raise ValueError("유한하지 않은 숫자는 지원하지 않습니다.")
        document = json.loads(content.decode("utf-8-sig"), object_pairs_hook=unique, parse_constant=invalid)
        if not isinstance(document, dict) or set(document) != {"format", "version", "payload", "sha256"}:
            raise ValueError("비교 파일 구조가 다릅니다.")
        if document["format"] != FORMAT or type(document["version"]) is not int or document["version"] not in {1, 2}:
            raise ValueError("지원하지 않는 비교 파일 버전입니다.")
        payload = document["payload"]
        required = {"name", "created_at_kst", "input_context", "snapshot", "history", "data_revision", "archived_results"}
        if document["version"] == 2:
            required.add("task_context")
        if not isinstance(payload, dict) or set(payload) != required:
            raise ValueError("비교 내용의 필드를 확인하세요.")
        if hashlib.sha256(_canonical(payload)).hexdigest() != document["sha256"]:
            raise ValueError("비교 파일의 내용과 해시가 일치하지 않습니다.")
        if document["version"] == 2:
            validate_context(payload["task_context"])
        if not isinstance(payload["name"], str) or not 1 <= len(payload["name"].strip()) <= 80 or payload["input_context"] not in INPUT_CONTEXTS:
            raise ValueError("비교 이름·입력 구분을 확인하세요.")
        datetime.fromisoformat(payload["created_at_kst"])
        if not isinstance(payload["history"], list) or len(payload["history"]) > 100 or not isinstance(payload["archived_results"], list):
            raise ValueError("이력·과거 결과 형식이 다릅니다.")
        for snapshot in [payload["snapshot"], *payload["history"]]:
            if not isinstance(snapshot, dict) or set(snapshot) != {"version", "scenario", "provenance"} or type(snapshot["version"]) is not int or snapshot["version"] < 0 or not isinstance(snapshot["provenance"], dict):
                raise ValueError("조건 이력 형식이 다릅니다.")
            if not isinstance(snapshot["scenario"], dict) or set(snapshot["scenario"]) != TOP_FIELDS:
                raise ValueError("조건 필드가 누락됐습니다.")
            validate_partial(snapshot["scenario"])
        if not isinstance(payload["data_revision"], list) or not all(isinstance(x, str) for x in payload["data_revision"]):
            raise ValueError("자료 버전 형식이 다릅니다.")
        return deepcopy(payload)
    except (UnicodeError, ValueError, TypeError, KeyError, RecursionError, OverflowError) as exc:
        raise ValueError("비교 JSON의 형식·버전·해시·조건을 확인하세요.") from exc
