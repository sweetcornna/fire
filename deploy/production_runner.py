"""Validated entry point for a durable production deployment.

The application itself consumes the same environment variables as GitHub
Actions. This adapter is deliberately strict: a malformed task or cookie
file must fail the run loudly instead of producing a successful run that
silently skipped an account.
"""

import json
import os
import sys
from pathlib import Path


ROOT_DIR = Path(__file__).resolve().parents[1]


def _read_json(path_value, label):
    path = Path(path_value).expanduser()
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as error:
        raise RuntimeError(f"{label}读取失败: {path}: {error}") from error
    try:
        return json.loads(text)
    except json.JSONDecodeError as error:
        raise RuntimeError(f"{label}不是有效 JSON: {path}: {error}") from error


def _validate_tasks(value, path):
    if not isinstance(value, list) or not value:
        raise RuntimeError(f"任务文件为空或格式错误: {path}")

    seen_ids = set()
    normalized = []
    for index, task in enumerate(value, start=1):
        if not isinstance(task, dict):
            raise RuntimeError(f"任务文件第 {index} 项不是对象: {path}")
        unique_id = str(task.get("unique_id") or "").strip()
        if not unique_id:
            raise RuntimeError(f"任务文件第 {index} 项缺少 unique_id: {path}")
        if unique_id in seen_ids:
            raise RuntimeError(f"任务文件包含重复 unique_id={unique_id}: {path}")
        seen_ids.add(unique_id)

        targets = task.get("targets")
        if not isinstance(targets, list) or not any(
            str(target).strip() for target in targets
        ):
            raise RuntimeError(f"任务文件第 {index} 项没有有效 targets: {path}")
        normalized.append(task)
    return normalized


def _cookie_payload_for_task(cookie_value, unique_id, path):
    """Return one Playwright cookie list from either supported file shape."""
    if isinstance(cookie_value, list):
        payload = cookie_value
    elif isinstance(cookie_value, dict):
        candidates = (
            unique_id,
            f"COOKIES_{unique_id}",
            f"cookies_{unique_id}",
        )
        payload = next(
            (cookie_value.get(key) for key in candidates if key in cookie_value),
            None,
        )
    else:
        payload = None

    if not isinstance(payload, list) or not payload:
        raise RuntimeError(
            f"Cookie 文件中没有账号 {unique_id} 的有效 cookie 列表: {path}"
        )
    if not all(isinstance(cookie, dict) for cookie in payload):
        raise RuntimeError(f"账号 {unique_id} 的 cookie 列表格式错误: {path}")
    return payload


def _set_cookie_environment(tasks, cookie_value, cookies_path):
    for task in tasks:
        unique_id = str(task["unique_id"]).strip()
        payload = _cookie_payload_for_task(cookie_value, unique_id, cookies_path)
        os.environ[f"COOKIES_{unique_id}".upper()] = json.dumps(
            payload, ensure_ascii=False
        )


def _set_default_environment():
    # Production defaults are explicit but remain overridable through the
    # service EnvironmentFile for one-off diagnostics or alternate paths.
    defaults = {
        "MESSAGE_AI_ENABLE": "0",
        "HEADLESS": "1",
        "BROWSER_TIMEOUT": "60000",
        "CHAT_OPEN_TIMEOUT": "2000",
        "TASK_RETRY_TIMES": "1",
        "CHAT_SEARCH_RESULT_WAIT_SECONDS": "2",
        "ALLOW_GLOBAL_USER_SEARCH": "1",
        "LOG_LEVEL": "INFO",
        "REQUIRE_ALL_TARGETS": "1",
        "DELIVERY_STATE_FILE": "/var/lib/huohua/delivery-state.json",
    }
    for key, value in defaults.items():
        os.environ.setdefault(key, value)

    executable = os.getenv("PLAYWRIGHT_EXECUTABLE_PATH", "").strip()
    if executable:
        return
    # Use a system browser only when it exists. Otherwise Playwright's own
    # installed browser path remains available instead of forcing a bad path.
    for candidate in (
        "/usr/bin/chromium",
        "/usr/bin/chromium-browser",
        "/usr/bin/google-chrome",
    ):
        if Path(candidate).is_file():
            os.environ["PLAYWRIGHT_EXECUTABLE_PATH"] = candidate
            break


def main():
    tasks_file = os.getenv("HUOHUA_TASKS_FILE", "/etc/huohua-tasks.json")
    cookies_file = os.getenv("HUOHUA_COOKIES_FILE", "/etc/huohua-cookies.json")
    tasks_value = _read_json(tasks_file, "任务文件")
    cookies_value = _read_json(cookies_file, "Cookie 文件")
    tasks = _validate_tasks(tasks_value, tasks_file)

    os.environ["TASKS"] = json.dumps(tasks, ensure_ascii=False)
    _set_cookie_environment(tasks, cookies_value, cookies_file)
    _set_default_environment()
    os.environ.setdefault("PYTHONUNBUFFERED", "1")

    sys.path.insert(0, str(ROOT_DIR))
    import main as application

    application.main()


if __name__ == "__main__":
    main()
