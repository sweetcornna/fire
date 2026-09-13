import traceback
import re
import requests
import json
import os
import tempfile
from datetime import date, datetime, timezone
from pathlib import Path
from urllib.parse import quote
from utils.logger import setup_logger
from utils.config import get_config, get_userData
from utils import norm
from core.msg_builder import build_message
from core.forms import resolve_templates, ai_enabled
from core.browser import get_browser
from playwright.sync_api import Response
import time

config = get_config()
userData = get_userData()
logger = setup_logger(level=config.get("logLevel", "Info"))
matchMode = config.get("matchMode", "nickname")
userIDDict = {}

CONVERSATION_ITEM_SELECTOR = ".conversationConversationItemwrapper"
CONVERSATION_TITLE_SELECTOR = ".conversationConversationItemtitle"
CONVERSATION_LIST_SELECTOR = ".conversationConversationListwrapper"
# Keep the legacy selector as the public/tested constant while using the
# current contenteditable marker as a fallback on the live page.
CHAT_EDITOR_SELECTOR = ".messageEditorimChatEditorContainer"
# Strict editable input selector for typing actions (must not include outer container)
CHAT_INPUT_SELECTOR_PARTS = (
    '[contenteditable="true"][data-placeholder*="发送消息"]',
    '[contenteditable="true"][aria-label*="发送消息"]',
    '[contenteditable="true"][placeholder*="发送消息"]',
    '[role="textbox"][contenteditable="true"]',
    'div.zone-container[contenteditable="true"]',
    'div.messageEditorinputArea[contenteditable="true"]',
    'textarea[placeholder*="发送消息"]',
    '[contenteditable="true"]',
)
CHAT_INPUT_SELECTOR = ", ".join(CHAT_INPUT_SELECTOR_PARTS)
# Douyin has used several editor wrappers over time. Match the semantic
# "发送消息" marker first, then keep the legacy class for older revisions.
CHAT_EDITOR_FALLBACK_SELECTOR = f"{CHAT_INPUT_SELECTOR}, {CHAT_EDITOR_SELECTOR}"
SEARCH_INPUT_SELECTORS = (
    'input[placeholder*="搜索"]',
    'input[aria-label*="搜索"]',
    '[contenteditable="true"][placeholder*="搜索"]',
    '[contenteditable="true"][aria-label*="搜索"]',
    'xpath=//*[self::input or @contenteditable="true"]'
    '[contains(@placeholder, "搜索") or contains(@aria-label, "搜索") '
    'or contains(@data-placeholder, "搜索")]',
)
USER_NUMBER_TARGET_RE = re.compile(r"^用户(\d+)$")
MAX_USER_SEARCH_SNIPPETS = 40
MAX_EMPTY_SCROLLS = 10
DEFAULT_SEARCH_ACTION_TIMEOUT_MS = 5000
DEFAULT_CHAT_OPEN_TIMEOUT_MS = 10000
FALLBACK_ELEMENT_TIMEOUT_MS = 1500
DEFAULT_TARGET_RETRY_TIMES = 3
DEFAULT_SEARCH_RESULT_WAIT_SECONDS = 4
DEFAULT_CHAT_READY_TIMEOUT_MS = 30000
CHAT_PAGE_URL = "https://www.douyin.com/chat"


def _norm_value(value) -> str:
    if value is None:
        return ""
    return norm(str(value))


def _dedupe(values):
    seen = set()
    result = []
    for value in values:
        value = _norm_value(value)
        if value and value not in seen:
            seen.add(value)
            result.append(value)
    return result


def _iter_user_records():
    seen = set()
    for values in userIDDict.values():
        record = tuple(_norm_value(value) for value in values)
        if record in seen:
            continue
        seen.add(record)
        yield list(record)


def get_search_terms_for_target(target):
    target = _norm_value(target)
    terms = [target]
    user_number_match = USER_NUMBER_TARGET_RE.match(target)
    if user_number_match:
        terms.append(user_number_match.group(1))
    term_set = set(terms)

    for values in _iter_user_records():
        short_id, unique_id, sec_uid, nickname, remark_name = (values + [""] * 5)[:5]
        if term_set & {short_id, unique_id, sec_uid, nickname, remark_name}:
            terms.extend([remark_name, nickname, unique_id, short_id])
            term_set.update(_dedupe(terms))

    return _dedupe(terms)


def get_user_search_url(term):
    return f"https://www.douyin.com/search/{quote(_norm_value(term))}?type=user"


def _safe_filename(value):
    filename = re.sub(r"[^0-9A-Za-z._-]+", "_", _norm_value(value)).strip("_")
    return filename[:80] or "target"


def collect_user_search_snippets(page, terms):
    return page.evaluate(
        """
        ({ terms, limit }) => {
            const normalizedTerms = terms.filter(Boolean);
            const seen = new Set();
            const snippets = [];
            const elements = document.querySelectorAll('a, [role="link"], div, span');

            for (const element of elements) {
                const rect = element.getBoundingClientRect();
                if (!rect || rect.width <= 0 || rect.height <= 0) {
                    continue;
                }

                const text = (element.innerText || element.textContent || '')
                    .replace(/\\s+/g, ' ')
                    .trim();
                const href = element.href || element.getAttribute('href') || '';
                const matchedTerm = normalizedTerms.some(
                    (term) => text.includes(term) || href.includes(term)
                );

                if (!matchedTerm && !href.includes('/user/')) {
                    continue;
                }
                if (!text && !href) {
                    continue;
                }

                const key = `${text}|${href}`;
                if (seen.has(key)) {
                    continue;
                }
                seen.add(key);
                snippets.push({
                    text: text.slice(0, 200),
                    href: href.slice(0, 300),
                });
                if (snippets.length >= limit) {
                    break;
                }
            }

            return snippets;
        }
        """,
        {"terms": _dedupe(terms), "limit": MAX_USER_SEARCH_SNIPPETS},
    )


def diagnose_user_search(page, username, targets):
    logs_dir = Path("logs")
    logs_dir.mkdir(exist_ok=True)

    for target in targets:
        terms = get_search_terms_for_target(target)
        for term in terms:
            url = get_user_search_url(term)
            logger.debug(
                f"账号 {username} 用户搜索诊断目标 {target}，搜索词: {term}，URL: {url}"
            )
            page.goto(url)
            time.sleep(config["friendListTimeout"] / 1000 + 2)

            screenshot_path = logs_dir / (
                f"user-search-{_safe_filename(target)}-{_safe_filename(term)}.png"
            )
            try:
                page.screenshot(path=str(screenshot_path), full_page=True)
                logger.debug(f"账号 {username} 用户搜索诊断截图: {screenshot_path}")
            except Exception:
                traceback.print_exc()

            try:
                snippets = collect_user_search_snippets(page, terms)
            except Exception:
                traceback.print_exc()
                snippets = []

            if not snippets:
                logger.debug(
                    f"账号 {username} 用户搜索诊断无候选结果，目标 {target}，搜索词 {term}"
                )
            for index, snippet in enumerate(snippets, start=1):
                logger.debug(
                    f"账号 {username} 用户搜索诊断候选 {index}: "
                    f"text={snippet.get('text', '')}, href={snippet.get('href', '')}"
                )


def summarize_target_matches(friend_titles, targets):
    matched = {}
    for title in friend_titles:
        remaining_targets = [
            target for target in targets if target not in matched
        ]
        if not remaining_targets:
            break
        targetSymbol = checkTargetName(title, remaining_targets)
        if targetSymbol and targetSymbol not in matched:
            matched[targetSymbol] = _norm_value(title)

    unmatched = [target for target in targets if target not in matched]
    return matched, unmatched


def collect_friend_titles(page, username):
    found_titles = []
    found_set = set()
    empty_scroll_count = 0

    while True:
        prev_found_count = len(found_set)
        for element in page.locator(CONVERSATION_ITEM_SELECTOR).all():
            try:
                if hasattr(element, "is_visible") and not element.is_visible():
                    continue
                targetName = _norm_value(
                    element.locator(CONVERSATION_TITLE_SELECTOR).inner_text()
                )
                if targetName and targetName not in found_set:
                    found_set.add(targetName)
                    found_titles.append(targetName)
                    logger.debug(f"账号 {username} 匹配诊断发现好友 {targetName}")
            except Exception:
                traceback.print_exc()

        new_found = len(found_set) > prev_found_count
        if new_found:
            empty_scroll_count = 0
        else:
            empty_scroll_count += 1

        if empty_scroll_count >= MAX_EMPTY_SCROLLS:
            logger.warning(
                f"账号 {username} 匹配诊断连续 {MAX_EMPTY_SCROLLS} 次滚动未发现新好友，判定已到达底部"
            )
            return found_titles

        scrollable_element = page.locator(CONVERSATION_LIST_SELECTOR).element_handle()
        if not scrollable_element:
            logger.error(f"账号 {username} 匹配诊断未找到滚动容器，退出")
            return found_titles

        scroll_top_before = page.evaluate(
            "(element) => element.scrollTop", scrollable_element
        )
        page.evaluate("(element) => element.scrollTop += 800", scrollable_element)
        time.sleep(0.3)
        scroll_top_after = page.evaluate(
            "(element) => element.scrollTop", scrollable_element
        )

        if scroll_top_before == scroll_top_after:
            empty_scroll_count += 2
            logger.debug(
                f"账号 {username} 匹配诊断 scrollTop 未变化 ({scroll_top_before})，可能已到底 "
                f"(空滚动计数: {empty_scroll_count}/{MAX_EMPTY_SCROLLS})"
            )
        else:
            logger.debug(
                f"账号 {username} 匹配诊断滚动好友列表 (scrollTop: {scroll_top_before} -> {scroll_top_after})"
            )
        time.sleep(1.5)


def diagnose_friend_matching(page, username, targets):
    logger.info(f"账号 {username} 启用好友匹配诊断模式，不发送消息")
    retry_operation(
        "打开抖音网页聊天页面",
        page.goto,
        retries=config["taskRetryTimes"],
        delay=5,
        url="https://www.douyin.com/chat",
    )
    time.sleep(5)
    friend_titles = collect_friend_titles(page, username)
    matched, unmatched = summarize_target_matches(friend_titles, targets)

    logger.info(
        f"账号 {username} 匹配诊断完成: 目标 {len(targets)} 个，"
        f"列表好友 {len(friend_titles)} 个，匹配 {len(matched)} 个，未匹配 {len(unmatched)} 个"
    )
    for target, title in matched.items():
        logger.info(f"账号 {username} 匹配诊断已匹配: 目标 {target} -> 当前显示 {title}")
    for target in unmatched:
        logger.warning(
            f"账号 {username} 匹配诊断未匹配: 目标 {target}，搜索词 {get_search_terms_for_target(target)}"
        )
    return matched, unmatched


def handle_response(response: Response):
    """
    只监听你要的那个接口响应
    """
    global userIDDict
    # 精准匹配目标接口 URL
    if "aweme/v1/web/im/user/info" in response.url:
        # print(f"URL: {response.url}")
        # print(f"状态码: {response.status}")
        try:
            if getattr(response, "status", 200) >= 400:
                return
            # 获取接口返回的 JSON 数据（就是你在 Network 里看到的内容）
            json_data = response.json()
            # print("\n📦 响应 JSON 数据：")
            # print(json.dumps(json_data, indent=4, ensure_ascii=False))
            items = json_data.get("data") or json_data.get("user_list") or []
            if isinstance(items, dict):
                nested_items = next(
                    (
                        items.get(key)
                        for key in ("user_list", "users", "list")
                        if items.get(key) is not None
                    ),
                    None,
                )
                items = nested_items if nested_items is not None else [items]
            if isinstance(items, dict):
                items = [items]
            if not isinstance(items, list):
                items = []
            for item in items:
                if not isinstance(item, dict):
                    continue
                user = next(
                    (
                        item.get(key)
                        for key in ("user", "user_info", "userInfo")
                        if isinstance(item.get(key), dict)
                    ),
                    item,
                )
                short_id = _norm_value(item.get("short_id"))
                unique_id = _norm_value(item.get("unique_id"))
                sec_uid = _norm_value(item.get("sec_uid", ""))
                nickname = _norm_value(item.get("nickname"))
                remark_name = _norm_value(item.get("remark_name", nickname))
                short_id = short_id or _norm_value(user.get("short_id") or user.get("shortId"))
                unique_id = unique_id or _norm_value(
                    user.get("unique_id") or user.get("uniqueId") or item.get("user_id")
                )
                sec_uid = sec_uid or _norm_value(user.get("sec_uid"))
                nickname = nickname or _norm_value(user.get("nickname"))
                remark_name = remark_name or _norm_value(user.get("remark_name", nickname))
                values = [short_id, unique_id, sec_uid, nickname, remark_name]
                if config.get("debugUserIDMapping"):
                    logger.debug(
                        "好友API映射: "
                        f"short_id={short_id}, unique_id={unique_id}, "
                        f"nickname={nickname}, remark_name={remark_name}"
                    )
                for key in {nickname, remark_name, short_id, unique_id, sec_uid}:
                    if key:
                        userIDDict[key] = values
        except Exception as e:
            tb = traceback.extract_tb(e.__traceback__)
            last = tb[-1]
            print(f"解析响应失败: {e}")
            print(f"文件: {last.filename}, 行号: {last.lineno}, 函数: {last.name}")


def retry_operation(name, operation, retries=3, delay=2, *args, **kwargs):
    """
    通用的重试逻辑
    :param name: 操作名称（用于日志记录）
    :param operation: 要执行的异步操作
    :param retries: 最大重试次数
    :param delay: 每次重试之间的延迟（秒）
    :param args: 传递给操作的参数
    :param kwargs: 传递给操作的关键字参数
    """
    retries = max(1, int(retries))
    for attempt in range(retries):
        try:
            return operation(*args, **kwargs)
        except Exception as e:
            if attempt < retries - 1:
                logger.warning(f"{name} 失败，正在重试第 {attempt + 1} 次，错误：{e}")
                time.sleep(delay)
            else:
                logger.error(f"{name} 失败，已达到最大重试次数，错误：{e}")
                raise

def checkTargetName(targetName, targets):
    """检查targetName是否为目标
    """
    
    targetSymbol = None
    
    targetName = _norm_value(targetName)
    target_aliases = [
        (_norm_value(target), set(get_search_terms_for_target(target)))
        for target in targets
    ]

    if targetName in userIDDict:
        values = {_norm_value(v) for v in userIDDict[targetName]}
        matched = next(
            (
                target
                for target, aliases in target_aliases
                if values & aliases or targetName in aliases
            ),
            None,
        )
        if matched:
            targetSymbol = matched
    else:
        targetSymbol = next(
            (
                target
                for target, aliases in target_aliases
                if targetName in aliases
            ),
            None,
        )
    return targetSymbol


def _box_value(box, key):
    return float(box.get(key, 0) if box else 0)


def _search_input_score(page, candidate):
    try:
        list_locator = page.locator(CONVERSATION_LIST_SELECTOR)
        try:
            list_box = list_locator.bounding_box(timeout=FALLBACK_ELEMENT_TIMEOUT_MS)
        except TypeError:
            list_box = list_locator.bounding_box()
        candidate_box = candidate.bounding_box()
    except Exception:
        return 1000

    if not list_box or not candidate_box:
        return 1000

    list_left = _box_value(list_box, "x")
    list_right = list_left + _box_value(list_box, "width")
    input_left = _box_value(candidate_box, "x")
    input_right = input_left + _box_value(candidate_box, "width")
    overlap = max(0, min(list_right, input_right) - max(list_left, input_left))

    list_top = _box_value(list_box, "y")
    input_bottom = _box_value(candidate_box, "y") + _box_value(candidate_box, "height")
    vertical_gap = abs(list_top - input_bottom)

    if overlap > 0:
        return vertical_gap
    return 500 + vertical_gap


def _element_handle_with_timeout(locator, timeout):
    """Get an element handle without breaking lightweight test doubles."""
    try:
        return locator.element_handle(timeout=timeout)
    except TypeError:
        return locator.element_handle()


def find_search_input(page):
    candidates = []
    candidate_order = 0
    for selector in SEARCH_INPUT_SELECTORS:
        try:
            locator = page.locator(selector)
            for index in range(locator.count()):
                candidate = locator.nth(index)
                if candidate.is_visible():
                    score = _search_input_score(page, candidate)
                    candidates.append((score, candidate_order, selector, index, candidate))
                    candidate_order += 1
        except Exception:
            continue

    if candidates:
        score, _, selector, index, candidate = min(candidates, key=lambda item: item[:2])
        logger.debug(f"找到聊天搜索框: {selector} #{index}，score={score}")
        return candidate
    return None


def _page_state(page):
    """Return a small, non-sensitive diagnostic snapshot for UI failures."""
    try:
        return page.evaluate(
            """() => ({
                url: location.href,
                title: document.title,
                body: (document.body?.innerText || '').replace(/\\s+/g, ' ').slice(0, 500)
            })"""
        )
    except Exception:
        return {"url": "", "title": "", "body": ""}


def _logged_out(page):
    body = _page_state(page).get("body", "")
    return any(marker in body for marker in ("扫码登录", "密码登录", "登录后免费畅享"))


def wait_for_chat_ready(page, username, timeout=None):
    """Wait for the authenticated chat shell before searching or sending.

    Douyin's chat bundle can take several seconds to hydrate on the fixed
    production host.  A single fixed sleep caused valid sessions to be
    rejected before the search input existed.  Poll for either the search box
    or a rendered conversation list, while failing quickly on a login page.
    """
    timeout = timeout or int(
        os.getenv("CHAT_READY_TIMEOUT", str(DEFAULT_CHAT_READY_TIMEOUT_MS))
    )
    deadline = time.monotonic() + max(1000, timeout) / 1000
    while time.monotonic() < deadline:
        _dismiss_login_prompt(page, username)
        if _logged_out(page):
            state = _page_state(page)
            raise RuntimeError(
                f"账号 {username} Cookie 已失效，抖音返回登录页；页面状态: {state}"
            )
        try:
            if find_search_input(page):
                return True
            if page.locator(CONVERSATION_ITEM_SELECTOR).count() > 0:
                return True
        except Exception:
            pass
        time.sleep(0.5)

    logger.warning(
        f"账号 {username} 聊天页面在 {timeout}ms 内未就绪；页面状态: {_page_state(page)}"
    )
    return False


def _dismiss_login_prompt(page, username):
    """Dismiss Douyin's optional "save login" prompt when it blocks the chat UI."""
    state = _page_state(page)
    body = state.get("body", "")
    if "是否保存登录信息" not in body:
        return False

    # The dialog title also contains the word "保存".  A global text locator
    # can therefore click the non-interactive title and leave the modal open.
    # Prefer the dialog's actual button classes and keep the text fallback for
    # lightweight test doubles and older page revisions.
    button_selectors = (
        ".trust-login-dialog-button-cancel",
        ".trust-login-dialog-button-confirm",
    )
    for selector in button_selectors:
        try:
            locator = page.locator(selector)
            for index in range(locator.count()):
                candidate = locator.nth(index)
                if not candidate.is_visible():
                    continue
                candidate.click()
                logger.debug(f"账号 {username} 已关闭抖音登录提示: {selector}")
                time.sleep(0.5)
                return True
        except Exception:
            continue
    for label in ("取消", "保存"):
        try:
            locator = page.get_by_text(label, exact=True)
            for index in range(locator.count()):
                candidate = locator.nth(index)
                if not candidate.is_visible():
                    continue
                candidate.click()
                logger.debug(f"账号 {username} 已关闭抖音登录提示: {label}")
                time.sleep(0.5)
                return True
        except Exception:
            continue
    logger.warning(f"账号 {username} 检测到登录提示但未找到可点击的关闭按钮")
    return False


def _delivery_state_path():
    """Return the optional persistent delivery state path.

    GitHub Actions is intentionally stateless; the fixed production runner sets
    this variable so a failed run can resume without sending successful targets
    a second time.
    """
    value = os.getenv("DELIVERY_STATE_FILE", "").strip()
    return Path(value).expanduser() if value else None


def _delivery_state_key(value):
    return _norm_value(value)


def _load_delivery_state():
    path = _delivery_state_path()
    if not path:
        return {}
    try:
        with path.open("r", encoding="utf-8") as handle:
            data = json.load(handle)
        if not isinstance(data, dict):
            raise RuntimeError(f"续火状态文件格式错误：顶层必须是对象: {path}")
        return data
    except FileNotFoundError:
        return {}
    except (OSError, ValueError) as error:
        # Treat a corrupt state file as a hard failure.  Silently treating it
        # as empty would resend every target after a disk or deployment issue,
        # which defeats the only durable duplicate-send protection we have.
        raise RuntimeError(f"续火状态文件读取失败，请先修复或恢复该文件: {path}: {error}") from error


def _today_delivery_state(state=None):
    state = state if state is not None else _load_delivery_state()
    if not isinstance(state, dict):
        raise RuntimeError("续火状态文件格式错误：顶层必须是对象")
    days = state.setdefault("days", {})
    if not isinstance(days, dict):
        raise RuntimeError("续火状态文件格式错误：days 必须是对象")
    today = date.today().isoformat()
    day_state = days.setdefault(today, {})
    if not isinstance(day_state, dict):
        raise RuntimeError("续火状态文件格式错误：当天状态必须是对象")
    for account_key, account_state in day_state.items():
        if not isinstance(account_state, dict):
            raise RuntimeError(
                f"续火状态文件格式错误：账号 {account_key} 的状态必须是对象"
            )
    # Keep only a small rolling window; old entries cannot affect today's run.
    for key in list(days):
        if key != today:
            del days[key]
    return state, day_state


def _persist_delivery_state(state):
    path = _delivery_state_path()
    if not path:
        return
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, temp_name = tempfile.mkstemp(
            prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent)
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(state, handle, ensure_ascii=False, indent=2, sort_keys=True)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.chmod(temp_name, 0o600)
            os.replace(temp_name, path)
        finally:
            if os.path.exists(temp_name):
                os.unlink(temp_name)
    except OSError as error:
        raise RuntimeError(f"续火状态文件写入失败: {path}: {error}") from error


def _persist_cookie_snapshot(cookies, account_key=None):
    """Persist refreshed browser cookies without truncating other accounts."""
    value = os.getenv("HUOHUA_COOKIE_PERSIST_FILE", "").strip()
    if not value or not cookies:
        return
    path = Path(value).expanduser()
    try:
        existing = None
        try:
            with path.open("r", encoding="utf-8") as handle:
                existing = json.load(handle)
        except FileNotFoundError:
            pass
        except (OSError, ValueError) as error:
            raise RuntimeError(f"Cookie 刷新文件读取失败: {path}: {error}") from error

        account_key = _norm_value(
            account_key or os.getenv("HUOHUA_COOKIE_ACCOUNT_KEY", "")
        )
        if isinstance(existing, dict):
            if not account_key:
                raise RuntimeError(
                    f"Cookie 文件是多账号映射，但没有账号标识，拒绝覆盖: {path}"
                )
            key_candidates = (
                account_key,
                f"COOKIES_{account_key}",
                f"cookies_{account_key}",
            )
            mapping_key = next(
                (key for key in key_candidates if key in existing),
                f"COOKIES_{account_key}",
            )
            payload = dict(existing)
            payload[mapping_key] = cookies
        elif existing is None or isinstance(existing, list):
            # A flat cookie list is the supported single-account format.
            payload = cookies
        else:
            raise RuntimeError(f"Cookie 文件格式不受支持，拒绝覆盖: {path}")

        path.parent.mkdir(parents=True, exist_ok=True)
        fd, temp_name = tempfile.mkstemp(
            prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent)
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(payload, handle, ensure_ascii=False, indent=2)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.chmod(temp_name, 0o600)
            os.replace(temp_name, path)
        finally:
            if os.path.exists(temp_name):
                os.unlink(temp_name)
    except OSError as error:
        raise RuntimeError(f"Cookie 刷新文件写入失败: {path}: {error}") from error


def _completed_targets_for_today(username, targets, aliases=()):
    state = _load_delivery_state()
    _, day_state = _today_delivery_state(state)
    account_keys = [_delivery_state_key(username)] + [
        _delivery_state_key(alias) for alias in aliases
    ]
    completed = {
        _delivery_state_key(target)
        for target in targets
        if any(
            _delivery_state_key(target)
            in day_state.get(account_key, {})
            for account_key in account_keys
        )
    }
    return completed


def _mark_target_sent_today(username, target):
    state = _load_delivery_state()
    state, day_state = _today_delivery_state(state)
    account_key = _delivery_state_key(username)
    account_state = day_state.setdefault(account_key, {})
    account_state[_delivery_state_key(target)] = {
        "sent_at": datetime.now(timezone.utc).isoformat(timespec="seconds")
    }
    _persist_delivery_state(state)


def _chat_input_is_search_like(candidate):
    for attribute in ("placeholder", "aria-label", "data-placeholder", "title"):
        try:
            value = candidate.get_attribute(attribute) or ""
        except Exception:
            value = ""
        if "搜索" in value:
            return True
    return False


def _chat_input_score(page, candidate, selector_index):
    """Prefer the semantic send editor and avoid the left search box."""
    if _chat_input_is_search_like(candidate):
        return 10000 + selector_index
    if selector_index < 3:
        return selector_index

    # Marker-free fallbacks are only safe when they are in the right-hand chat
    # pane. Geometry is best-effort so test doubles and older Playwright
    # versions can still use the selector order.
    try:
        list_box = page.locator(CONVERSATION_LIST_SELECTOR).bounding_box()
        candidate_box = candidate.bounding_box()
        if list_box and candidate_box:
            list_right = _box_value(list_box, "x") + _box_value(list_box, "width")
            candidate_left = _box_value(candidate_box, "x")
            if candidate_left >= list_right - 40:
                return 50 + selector_index
            return 500
    except Exception:
        pass
    return 100 + selector_index


def _wait_for_chat_input(page, timeout=FALLBACK_ELEMENT_TIMEOUT_MS):
    deadline = time.monotonic() + max(0, timeout) / 1000
    while True:
        candidate = _find_chat_input(page)
        if candidate is not None:
            return candidate
        if time.monotonic() >= deadline:
            return None
        time.sleep(0.1)


def _find_chat_input(page):
    """Find an editable message target, with a legacy wrapper fallback."""
    candidates = []
    for selector_index, selector in enumerate(CHAT_INPUT_SELECTOR_PARTS):
        try:
            locator = page.locator(selector)
            count = locator.count()
        except Exception:
            continue
        for index in range(count):
            try:
                candidate = locator.nth(index)
                if not candidate.is_visible() or _chat_input_is_search_like(candidate):
                    continue
                candidates.append(
                    (
                        _chat_input_score(page, candidate, selector_index),
                        selector_index,
                        index,
                        candidate,
                    )
                )
            except Exception:
                continue

    if candidates:
        return min(candidates, key=lambda item: item[:3])[3]

    # Older Douyin builds made the wrapper itself editable and did not expose
    # a semantic placeholder.  Use it only after all strict candidates fail.
    try:
        wrapper_locator = page.locator(CHAT_EDITOR_SELECTOR)
        for index in range(wrapper_locator.count()):
            wrapper = wrapper_locator.nth(index)
            if wrapper.is_visible():
                return wrapper
    except Exception:
        pass
    return None


def _submit_chat_message(page, account_name, target, message=None, delivery_key=None):
    """Type and submit message into the currently confirmed chat editor."""
    chat_input = _wait_for_chat_input(page)
    if chat_input is None:
        raise RuntimeError("当前聊天没有可用的消息输入框")
    message = build_message() if message is None else str(message)
    lines = re.split(r"\\n|\r?\n", message)
    for index, line in enumerate(lines):
        chat_input.type(line)
        if index < len(lines) - 1:
            chat_input.press("Shift+Enter")

    logger.debug(
        f"账号 {account_name} 准备发送消息给好友 {target}：\n\t{message}"
    )
    chat_input.press("Enter")
    time.sleep(2)
    _mark_target_sent_today(delivery_key or account_name, target)
    logger.debug(f"账号 {account_name} 给好友 {target} 发送消息完成")
    return target


def _send_message_to_target(
    page, account_name, target, message=None, delivery_key=None
):
    """Select, verify, and send one target's message.

    A target is considered delivered only after the chat editor is confirmed
    to belong to that target and the message submission completes.  Keeping
    this unit small lets the caller retry one failed target without replaying
    targets that already succeeded earlier in the same run.
    """
    # Prefer the already-loaded conversation list.  Searching globally can
    # return a text-only result that does not open a chat (and therefore has
    # no editor), while the conversation item is the reliable send path.
    selected_target = click_matching_visible_user(page, account_name, [target])
    if not selected_target:
        selected_target = search_and_select_target(page, account_name, target)
    if not selected_target:
        raise RuntimeError(f"未找到目标好友 {target}")
    if not wait_for_chat_editor(page, account_name, selected_target):
        raise RuntimeError(f"目标好友 {target} 的聊天输入框未确认")

    _submit_chat_message(page, account_name, target, message, delivery_key)
    return selected_target


def _send_target_with_retries(
    page, account_name, target, message=None, delivery_key=None
):
    """Send one target with a fresh chat page between bounded attempts."""
    retry_times = max(1, int(config.get("taskRetryTimes", DEFAULT_TARGET_RETRY_TIMES)))
    for attempt in range(1, retry_times + 1):
        try:
            _dismiss_login_prompt(page, account_name)
            return _send_message_to_target(
                page, account_name, target, message, delivery_key
            )
        except Exception as error:
            logger.warning(
                f"账号 {account_name} 发送目标 {target} 第 {attempt}/{retry_times} 次失败: {error}"
            )
            if attempt >= retry_times:
                break
            try:
                _open_chat_page_for_retry(page, account_name)
            except Exception as reset_error:
                logger.warning(
                    f"账号 {account_name} 重置聊天页面失败，稍后继续重试目标 {target}: {reset_error}"
                )
    return None


def _fallback_search_targets(page, username, remaining_targets):
    """Use the page search UI when the virtualized list container is unavailable."""
    if not remaining_targets:
        return set()
    logger.warning(
        f"账号 {username} 好友列表容器不可用，切换搜索兜底；页面状态: {_page_state(page)}"
    )
    found = set()
    for target in list(remaining_targets):
        try:
            target_symbol = search_and_select_target(page, username, target)
        except Exception as error:
            logger.warning(f"账号 {username} 搜索兜底目标 {target} 失败: {error}")
            continue
        if target_symbol:
            found.add(target_symbol)
            yield target_symbol
    return found


def _chat_target_match(page, target):
    terms = get_search_terms_for_target(target)
    try:
        result = page.evaluate(
            """
            ({ listSelector, editorSelector, terms }) => {
                const normalize = (value) => (value || '')
                    .normalize('NFKC')
                    .replace(/[\\u3000\\u00a0]/g, ' ')
                    .replace(/[\\u200b\\ufeff]/g, '')
                    .replace(/\\s+/g, ' ')
                    .trim();
                const normalizedTerms = terms.map(normalize).filter(Boolean);
                const list = document.querySelector(listSelector);
                const listRect = list ? list.getBoundingClientRect() : null;
                const rightPaneStart = listRect ? listRect.right : window.innerWidth * 0.3;
                const editor = Array.from(document.querySelectorAll(editorSelector))
                    .find((element) => {
                        const marker = [
                            element.getAttribute('placeholder'),
                            element.getAttribute('aria-label'),
                            element.getAttribute('data-placeholder'),
                            element.getAttribute('title'),
                        ].filter(Boolean).join(' ');
                        const rect = element.getBoundingClientRect();
                        return !marker.includes('搜索')
                            && rect
                            && rect.right > rightPaneStart;
                    }) || null;
                const editorRect = editor ? editor.getBoundingClientRect() : null;
                const editorTop = editorRect ? editorRect.top : window.innerHeight;
                const snippets = [];

                for (const element of document.querySelectorAll('h1,h2,h3,div,span,a,button')) {
                    const rect = element.getBoundingClientRect();
                    if (!rect || rect.width <= 0 || rect.height <= 0) {
                        continue;
                    }
                    if (rect.right <= rightPaneStart || rect.bottom > editorTop + 24) {
                        continue;
                    }

                    const text = normalize(element.innerText || element.textContent || '');
                    if (!text || text.length > 160) {
                        continue;
                    }
                    if (normalizedTerms.some((term) => text.includes(term))) {
                        snippets.push(text.slice(0, 160));
                        if (snippets.length >= 5) {
                            break;
                        }
                    }
                }

                return { matched: snippets.length > 0, snippets };
            }
            """,
            {
                "listSelector": CONVERSATION_LIST_SELECTOR,
                "editorSelector": CHAT_EDITOR_FALLBACK_SELECTOR,
                "terms": terms,
            },
        )
    except Exception:
        traceback.print_exc()
        return False, []
    return bool(result.get("matched")), result.get("snippets", [])


def wait_for_chat_editor(page, username, target, timeout=None):
    timeout = timeout or config.get("chatOpenTimeout", DEFAULT_CHAT_OPEN_TIMEOUT_MS)
    try:
        page.wait_for_selector(CHAT_EDITOR_FALLBACK_SELECTOR, timeout=timeout)
    except Exception as error:
        logger.warning(f"账号 {username} 选择好友 {target} 后聊天输入框未出现: {error}")
        return False
    matched, snippets = _chat_target_match(page, target)
    if matched:
        logger.debug(f"账号 {username} 已确认当前聊天为 {target}: {snippets}")
        return True
    logger.warning(
        f"账号 {username} 选择好友 {target} 后当前聊天标题未匹配目标，"
        f"搜索词 {get_search_terms_for_target(target)}，可见候选 {snippets}"
    )
    return False


def _locator_action(locator, action, *args, timeout=None):
    method = getattr(locator, action)
    try:
        if timeout is not None:
            return method(*args, timeout=timeout)
        return method(*args)
    except TypeError:
        return method(*args)


def fill_search_input(search_input, value):
    timeout = config.get("chatSearchActionTimeout", DEFAULT_SEARCH_ACTION_TIMEOUT_MS)
    _locator_action(search_input, "click", timeout=timeout)
    try:
        _locator_action(search_input, "fill", value, timeout=timeout)
    except Exception:
        _locator_action(search_input, "press", "Control+A", timeout=timeout)
        _locator_action(search_input, "type", value, timeout=timeout)
    try:
        _locator_action(search_input, "press", "Enter", timeout=timeout)
    except Exception:
        pass


def click_matching_visible_user(page, username, targets):
    for element in page.locator(CONVERSATION_ITEM_SELECTOR).all():
        try:
            if hasattr(element, "is_visible") and not element.is_visible():
                continue
            targetName = _norm_value(
                element.locator(CONVERSATION_TITLE_SELECTOR).inner_text()
            )
            if not targetName:
                continue
            logger.debug(f"账号 {username} 搜索结果好友 {targetName}")
            targetSymbol = checkTargetName(targetName, targets)
            if targetSymbol:
                element.click()
                if wait_for_chat_editor(page, username, targetSymbol):
                    return targetSymbol
        except Exception:
            traceback.print_exc()
    return None


def click_visible_text_result(page, username, target, terms):
    for term in terms:
        try:
            locator = page.get_by_text(term, exact=True)
        except Exception:
            continue

        try:
            count = locator.count()
        except Exception:
            count = 0

        for index in range(count):
            try:
                candidate = locator.nth(index)
                if not candidate.is_visible():
                    continue
                logger.debug(
                    f"账号 {username} 点击搜索文本结果 {term} 以选择目标好友 {target}"
                )
                candidate.click()
                if wait_for_chat_editor(page, username, target):
                    return target
            except Exception:
                traceback.print_exc()
    return None


def search_and_select_target(page, username, target):
    terms = get_search_terms_for_target(target)
    wait_seconds = max(
        1,
        int(os.getenv("CHAT_SEARCH_RESULT_WAIT_SECONDS", str(DEFAULT_SEARCH_RESULT_WAIT_SECONDS))),
    )
    for term in terms:
        try:
            _dismiss_login_prompt(page, username)
            search_input = None
            input_deadline = time.monotonic() + wait_seconds
            while time.monotonic() < input_deadline:
                search_input = find_search_input(page)
                if search_input:
                    break
                if _logged_out(page):
                    return None
                time.sleep(0.5)
            if not search_input:
                logger.warning(f"账号 {username} 未找到聊天搜索框，无法搜索目标好友 {target}")
                continue
            logger.debug(f"账号 {username} 搜索目标好友 {target}，搜索词: {term}")
            fill_search_input(search_input, term)
            deadline = time.monotonic() + wait_seconds
            while time.monotonic() < deadline:
                targetSymbol = click_matching_visible_user(page, username, [target])
                if targetSymbol:
                    return targetSymbol
                # A global user-search result is not necessarily an existing
                # conversation and often opens a profile without an editor.
                # Keep it opt-in so production never treats a text-only hit as
                # a sendable chat by accident.
                if os.getenv("ALLOW_GLOBAL_USER_SEARCH", "0").strip().lower() in {
                    "1",
                    "true",
                    "yes",
                    "on",
                }:
                    targetSymbol = click_visible_text_result(page, username, target, terms)
                    if targetSymbol:
                        return targetSymbol
                time.sleep(0.5)
        except Exception:
            traceback.print_exc()

    return None


def _open_chat_page_for_retry(page, username):
    """Rebuild the chat UI after a search leaves a stale/empty virtual list."""
    retry_operation(
        "重新打开抖音网页聊天页面",
        page.goto,
        retries=max(1, int(config.get("taskRetryTimes", DEFAULT_TARGET_RETRY_TIMES))),
        delay=2,
        url=CHAT_PAGE_URL,
        wait_until="commit",
    )
    time.sleep(3)
    if not wait_for_chat_ready(page, username):
        raise RuntimeError(f"账号 {username} 聊天页面未就绪")


def reliable_target_selections(page, username, targets):
    """Select each target from a fresh search context.

    The conversation list is virtualized and reorders itself after every sent
    message.  Iterating that live list while clicking it skips contacts (the
    previous production run sent only 11/78).  Searching one target at a time
    makes selection independent of list reordering and allows bounded retries.
    """
    retry_times = max(
        1, int(config.get("taskRetryTimes", DEFAULT_TARGET_RETRY_TIMES))
    )
    for index, target in enumerate(targets):
        selected = None
        for attempt in range(1, retry_times + 1):
            try:
                # Sending a message reorders Douyin's virtualized conversation
                # list.  Rebuild the page before every target after the first
                # so selection never depends on that mutable list state.
                if index > 0 and attempt == 1:
                    _open_chat_page_for_retry(page, username)
                _dismiss_login_prompt(page, username)
                selected = search_and_select_target(page, username, target)
                if selected and wait_for_chat_editor(page, username, selected):
                    break
            except Exception as error:
                logger.warning(
                    f"账号 {username} 选择目标 {target} 第 {attempt}/{retry_times} 次失败: {error}"
                )
            if attempt < retry_times:
                try:
                    _open_chat_page_for_retry(page, username)
                except Exception as error:
                    logger.warning(
                        f"账号 {username} 重置聊天页面失败，稍后继续重试目标 {target}: {error}"
                    )
        if selected:
            yield selected
        else:
            logger.error(
                f"账号 {username} 多次尝试后仍未找到目标 {target}，将由本次运行的失败状态触发下次续传"
            )


def search_remaining_targets(page, username, remaining_targets):
    for target in list(remaining_targets):
        targetSymbol = search_and_select_target(page, username, target)
        if targetSymbol:
            yield targetSymbol


def scroll_and_select_user(page, username, targets):
    """尝试滚动并查找用户名"""
    # 定义目标元素和滚动容器的选择器
    target_selector = CONVERSATION_ITEM_SELECTOR
    scrollable_friends_selector = CONVERSATION_LIST_SELECTOR

    # [修复] 使用模糊匹配 no-more-tip- 前缀，不再依赖精确哈希后缀
    # 同时增加文本匹配作为兜底
    # no_more_selector = 'xpath=//div[contains(@class, "no-more-tip-")]'
    # loading_selector = 'xpath=//div[contains(@class, "semi-spin")]'

    logger.debug(f"账号 {username} 开始查找目标好友列表")
    logger.debug(f"账号 {username} 目标好友列表: {targets}")

    found_targets = set()
    # [修改] 复制一份目标列表用于追踪进度
    remaining_targets = set(targets)

    # [修复] 新增：连续空滚动计数器（滚动后没有发现新好友的次数）
    empty_scroll_count = 0
    MAX_EMPTY_SCROLLS = 10  # 连续10次滚动没有新好友，认为到底了

    while True:
        # 查找所有目标元素
        target_elements = page.locator(target_selector).all()

        # [修复] 记录本轮循环前已发现的好友数，用于判断是否有新发现
        prev_found_count = len(found_targets)

        for element in target_elements:
            try:
                # 查找子元素 span，模糊匹配 class
                span = element.locator(CONVERSATION_TITLE_SELECTOR)
                targetName = span.inner_text()

                if targetName not in found_targets:
                    found_targets.add(targetName)
                    logger.debug(f"账号 {username} 找到好友 {targetName}")
                
                targetSymbol = checkTargetName(targetName, remaining_targets)

                if targetSymbol:
                    element.click()
                    if wait_for_chat_editor(page, username, targetSymbol):
                        yield targetSymbol

                        # [修改] 标记已找到，如果全找到了直接退出
                        if targetSymbol in remaining_targets:
                            remaining_targets.remove(targetSymbol)
                        if len(remaining_targets) == 0:
                            logger.debug(f"账号 {username} 所有目标好友均已找到，停止搜索")
                            return
                        break
                    continue
            except Exception as e:
                traceback.print_exc()
        else:
            # [修复] 检查本轮是否有新好友被发现
            new_found = len(found_targets) > prev_found_count
            if new_found:
                empty_scroll_count = 0  # 有新发现，重置计数器
            else:
                empty_scroll_count += 1  # 无新发现，递增计数器

            # [修复] 状态检测逻辑（多重兜底）

            # # 1. 检查是否到底（"没有更多了" —— 使用模糊类名匹配）
            # if page.locator(no_more_selector).count() > 0:
            #     logger.info(f"账号 {username} 检测到'没有更多了'标志，已到达底部")
            #     if len(remaining_targets) > 0:
            #         logger.warning(
            #             f"账号 {username} 搜索结束，仍有以下好友未找到: {remaining_targets}"
            #         )
            #     break

            # 2. [修复] 检查连续空滚动次数，防止死循环
            if empty_scroll_count >= MAX_EMPTY_SCROLLS:
                logger.warning(
                    f"账号 {username} 连续 {MAX_EMPTY_SCROLLS} 次滚动未发现新好友，判定已到达底部"
                )
                for targetSymbol in search_remaining_targets(
                    page, username, remaining_targets
                ):
                    yield targetSymbol
                    if targetSymbol in remaining_targets:
                        remaining_targets.remove(targetSymbol)
                    if len(remaining_targets) == 0:
                        logger.debug(f"账号 {username} 所有目标好友均已找到，停止搜索")
                        return
                if len(remaining_targets) > 0:
                    logger.warning(
                        f"账号 {username} 搜索结束，仍有以下好友未找到: {remaining_targets}"
                    )
                break

            # 3. 检查是否正在加载
            # if page.locator(loading_selector).count() > 0:
            #     logger.debug(f"账号 {username} 列表正在加载中 (Loading)...")
            #     time.sleep(1.5)  # 给加载留点时间
            #     # 不 break，继续去滚动以触发后续内容

            # 4. 滚动容器
            try:
                scrollable_element = _element_handle_with_timeout(
                    page.locator(scrollable_friends_selector),
                    FALLBACK_ELEMENT_TIMEOUT_MS,
                )
            except Exception as error:
                logger.warning(
                    f"账号 {username} 未找到好友列表滚动容器 ({error})，"
                    "立即使用搜索兜底"
                )
                for target_symbol in _fallback_search_targets(
                    page, username, remaining_targets
                ):
                    if target_symbol in remaining_targets:
                        remaining_targets.remove(target_symbol)
                    yield target_symbol
                if remaining_targets:
                    logger.error(
                        f"账号 {username} 搜索兜底后仍未找到好友: {remaining_targets}"
                    )
                return

            if scrollable_element:
                # [修复] 记录滚动前的 scrollTop，用于检测是否真的滚动了
                scroll_top_before = page.evaluate(
                    "(element) => element.scrollTop", scrollable_element
                )

                page.evaluate(
                    "(element) => element.scrollTop += 800", scrollable_element
                )

                # [修复] 检测滚动后的 scrollTop
                time.sleep(0.3)
                scroll_top_after = page.evaluate(
                    "(element) => element.scrollTop", scrollable_element
                )

                if scroll_top_before == scroll_top_after:
                    # scrollTop 没有变化，说明已经到底了
                    empty_scroll_count += 2  # 加速判定到底
                    logger.debug(
                        f"账号 {username} scrollTop 未变化 ({scroll_top_before})，可能已到底 (空滚动计数: {empty_scroll_count}/{MAX_EMPTY_SCROLLS})"
                    )
                else:
                    logger.debug(
                        f"账号 {username} 滚动好友列表以加载更多好友 (scrollTop: {scroll_top_before} -> {scroll_top_after})"
                    )

                time.sleep(1.5)
            else:
                logger.warning(
                    f"账号 {username} 未找到好友列表滚动容器，立即使用搜索兜底"
                )
                for target_symbol in _fallback_search_targets(
                    page, username, remaining_targets
                ):
                    if target_symbol in remaining_targets:
                        remaining_targets.remove(target_symbol)
                    yield target_symbol
                if remaining_targets:
                    logger.error(
                        f"账号 {username} 搜索兜底后仍未找到好友: {remaining_targets}"
                    )
                return


def do_user_task(browser, username, cookies, targets, unique_id=None):
    account_name = username
    delivery_account_key = _norm_value(unique_id or account_name)
    all_targets = list(
        dict.fromkeys(_norm_value(target) for target in targets if _norm_value(target))
    )
    if not all_targets:
        raise RuntimeError(f"账号 {account_name} 没有有效续火目标")

    # API mappings are account-specific.  Never let a previous account's
    # nickname/ID aliases select a similarly named contact in this account.
    userIDDict.clear()

    context = browser.new_context()  # 每个任务使用独立的上下文
    session_authenticated = False
    context.set_default_navigation_timeout(
        config["browserTimeout"]
    )  # 设置导航超时时间为 120 秒
    context.set_default_timeout(
        config["browserTimeout"]
    )  # 设置所有操作的默认超时时间为 120 秒

    page = context.new_page()

    # The chat page's conversation search API currently omits CORS headers.
    # Proxy only that API through the already-authenticated runner so the
    # browser can receive the same response and search older contacts.
    def proxy_conversation_api(route):
        request = route.request
        if "imapi.douyin.com" not in request.url:
            route.continue_()
            return
        try:
            headers = request.all_headers()
            headers.pop("host", None)
            headers.pop("content-length", None)
            upstream = requests.request(
                request.method,
                request.url,
                headers=headers,
                # post_data decodes the body as UTF-8 and fails for Douyin's
                # binary/compressed request payload.  Keep the exact bytes.
                data=request.post_data_buffer,
                timeout=30,
            )
            response_headers = {
                key: value
                for key, value in upstream.headers.items()
                if key.lower() in {"content-type", "cache-control"}
            }
            route.fulfill(
                status=upstream.status_code,
                headers=response_headers,
                body=upstream.content,
            )
            logger.debug(
                f"账号 {username} 已代理抖音会话搜索接口: "
                f"status={upstream.status_code} url={request.url.split('?')[0]}"
            )
        except Exception as error:
            logger.warning(f"账号 {username} 代理抖音会话搜索接口失败: {error}")
            route.continue_()

    page.route("https://imapi.douyin.com/**", proxy_conversation_api)

    page.on("response", handle_response)  # 监听响应，收集好友完整信息用于匹配

    # 注入 Cookie
    context.add_cookies(cookies)

    try:
        if config.get("diagnoseUserSearch"):
            logger.info(f"账号 {username} 启用用户搜索诊断模式，不发送消息")
            diagnose_user_search(page, username, targets)
            return

        if config.get("diagnoseFriendMatching"):
            diagnose_friend_matching(page, username, targets)
            return

        # 打开抖音网页聊天页面
        retry_operation(
            "打开抖音网页聊天页面",
            page.goto,
            retries=config["taskRetryTimes"],
            delay=5,
            url=CHAT_PAGE_URL,
            wait_until="commit",
        )

        if not wait_for_chat_ready(page, account_name):
            raise RuntimeError(f"账号 {account_name} 聊天页面未就绪")
        session_authenticated = True

        completed = _completed_targets_for_today(
            delivery_account_key, all_targets, aliases=(account_name,)
        )
        pending_targets = [
            target for target in all_targets if _delivery_state_key(target) not in completed
        ]
        if completed:
            logger.info(
                f"账号 {account_name} 今日已成功发送 {len(completed)} 个目标，跳过重复发送"
            )
        if not pending_targets:
            logger.info(f"账号 {account_name} 今日目标已全部完成: {len(all_targets)}/{len(all_targets)}")
            return

        logger.debug(
            f"账号 {account_name} 开始发送消息，本次待处理 {len(pending_targets)}/{len(all_targets)} 个目标"
        )
        sent_count = 0
        failed_targets = []
        message = build_message()
        reset_before_next = False
        for target in pending_targets:
            if reset_before_next:
                try:
                    _open_chat_page_for_retry(page, account_name)
                except Exception as error:
                    logger.warning(
                        f"账号 {account_name} 重置聊天页面失败，继续尝试目标 {target}: {error}"
                    )
            delivered = _send_target_with_retries(
                page,
                account_name,
                target,
                message,
                delivery_account_key,
            )
            reset_before_next = not delivered
            if delivered:
                sent_count += 1
            else:
                failed_targets.append(target)

        completed_count = len(all_targets) - len(pending_targets) + sent_count
        missing_count = len(all_targets) - completed_count
        if missing_count:
            logger.warning(f"账号 {account_name} 本次有目标未完成: {failed_targets}")
            raise RuntimeError(
                f"账号 {account_name} 未完成全部发送: {completed_count}/{len(all_targets)}；"
                "本次任务标记为失败，下一次运行将从未完成目标继续"
            )
        logger.info(
            f"账号 {account_name} 本次发送完成: {completed_count}/{len(all_targets)} 个目标"
        )
    finally:
        try:
            if session_authenticated:
                _persist_cookie_snapshot(context.cookies(), delivery_account_key)
        finally:
            context.close()


def runTasks():
    if not userData:
        raise RuntimeError("没有加载到任何有效账号，拒绝将空任务标记为成功")

    playwright, browser = get_browser()
    try:
        # 检查是否启用多任务和任务数量
        # 创建信号量以限制并发任务数量
        logger.info("开始执行任务")
        logger.debug(f"当前配置如下：")
        logger.debug(f"形式选择模式: {config.get('messageSelectionMode', 'daily-rotate')}")
        logger.debug(f"模板池数量: {len(resolve_templates(config))}")
        logger.debug(f"AI 生成: {'启用' if ai_enabled(config) else '未启用'}")
        logger.debug(f"一言类型: {config['hitokotoTypes']}")
        for user in userData:
            logger.debug(
                f"用户: {user.get('username', '未知用户')}, 目标好友: {user['targets']}"
            )

        failed_accounts = []
        for user in userData:
            cookies = user["cookies"]
            targets = user["targets"]
            username = user.get("username", "未知用户")
            logger.info(f"开始处理账号 {username}")
            try:
                do_user_task(
                    browser,
                    username,
                    cookies,
                    targets,
                    user.get("unique_id") or username,
                )
            except Exception as error:
                failed_accounts.append((username, error))
                logger.error(f"账号 {username} 任务失败，将继续处理其他账号: {error}")
                continue
            logger.info(f"账号 {username} 任务完成")

        if failed_accounts:
            details = "; ".join(
                f"{username}: {error}" for username, error in failed_accounts
            )
            raise RuntimeError(f"以下账号续火失败: {details}")
    finally:
        # 关闭浏览器实例
        browser.close()

        playwright.stop()
