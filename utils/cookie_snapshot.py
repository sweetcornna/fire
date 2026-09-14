"""Validate a refreshed Douyin cookie snapshot before it replaces a secret.

A run that ends half authenticated must never overwrite a working session, so
the snapshot is checked for the cookies the chat page actually needs.  Values
are never printed: only names and counts appear in the output.
"""
import json
import sys
from pathlib import Path

SESSION_COOKIES = ("sessionid", "sessionid_ss", "sid_guard")
REQUIRED_FIELDS = ("name", "value", "domain")


def load_snapshot(path):
    try:
        with Path(path).open("r", encoding="utf-8") as handle:
            return json.load(handle)
    except FileNotFoundError:
        raise SystemExit(f"会话快照不存在: {path}")
    except (OSError, ValueError) as error:
        raise SystemExit(f"会话快照无法解析: {path}: {error}")


def validate(snapshot):
    """Return the session cookie names found, or raise SystemExit."""
    if not isinstance(snapshot, list) or not snapshot:
        raise SystemExit("会话快照不是非空的 Cookie 列表，拒绝回写")

    names = set()
    for index, cookie in enumerate(snapshot):
        if not isinstance(cookie, dict):
            raise SystemExit(f"会话快照第 {index} 项不是 Cookie 对象，拒绝回写")
        name = str(cookie.get("name", "")).strip()
        if not name:
            # Douyin serves a nameless cookie; it restores nothing and must
            # not be read as a damaged snapshot.
            continue
        missing = [field for field in REQUIRED_FIELDS if not str(cookie.get(field, "")).strip()]
        if missing and name in SESSION_COOKIES:
            raise SystemExit(f"会话快照的 {name} 缺少字段 {missing}，拒绝回写")
        if not missing:
            names.add(name)

    found = [name for name in SESSION_COOKIES if name in names]
    if not found:
        raise SystemExit(f"会话快照缺少登录态 Cookie {list(SESSION_COOKIES)}，拒绝回写")
    return found


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if len(argv) != 1:
        raise SystemExit("用法: python utils/cookie_snapshot.py <cookies.json>")
    snapshot = load_snapshot(argv[0])
    found = validate(snapshot)
    print(f"会话快照校验通过: {len(snapshot)} 条 Cookie，登录态字段 {found}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
