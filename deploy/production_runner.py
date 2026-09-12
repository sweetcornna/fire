"""Entry point for the fixed production host.

The regular project entry point reads GitHub Actions environment variables.  A
server has durable, root-owned JSON files instead, so this adapter loads those
files and then delegates to the exact same application code.
"""

import json
import os
import sys
from pathlib import Path


def _read(path_value):
    path = Path(path_value).expanduser()
    return path.read_text(encoding="utf-8")


def main():
    tasks_file = os.getenv("HUOHUA_TASKS_FILE", "/etc/huohua-tasks.json")
    cookies_file = os.getenv("HUOHUA_COOKIES_FILE", "/etc/huohua-cookies.json")
    tasks_text = _read(tasks_file)
    cookies_text = _read(cookies_file)
    tasks = json.loads(tasks_text)
    if not isinstance(tasks, list) or not tasks:
        raise RuntimeError(f"任务文件为空或格式错误: {tasks_file}")

    os.environ["TASKS"] = tasks_text
    for task in tasks:
        unique_id = task.get("unique_id") if isinstance(task, dict) else None
        if unique_id:
            os.environ[f"COOKIES_{unique_id}".upper()] = cookies_text

    # Production defaults are intentionally explicit and can still be
    # overridden by the wrapper when a one-off diagnostic is requested.
    os.environ.setdefault("MESSAGE_AI_ENABLE", "0")
    os.environ.setdefault("HEADLESS", "1")
    os.environ.setdefault("PLAYWRIGHT_EXECUTABLE_PATH", "/usr/bin/chromium")
    os.environ.setdefault("BROWSER_TIMEOUT", "60000")
    os.environ.setdefault("CHAT_OPEN_TIMEOUT", "10000")
    os.environ.setdefault("LOG_LEVEL", "INFO")
    os.environ.setdefault("REQUIRE_ALL_TARGETS", "1")
    os.environ.setdefault(
        "DELIVERY_STATE_FILE", "/var/lib/huohua/delivery-state.json"
    )

    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    import main as application

    application.main()


if __name__ == "__main__":
    main()
