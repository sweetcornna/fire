import traceback
import re
import requests
import json
import os
import tempfile
import hashlib
import inspect
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
# Numeric conversation title -> the contact it turned out to belong to.
placeholderIdentityDict = {}

CONVERSATION_ITEM_SELECTOR = ".conversationConversationItemwrapper"
CONVERSATION_TITLE_SELECTOR = ".conversationConversationItemtitle"
CONVERSATION_LIST_SELECTOR = ".conversationConversationListwrapper"
# Keep the legacy selector as the public/tested constant while using the
# current contenteditable marker as a fallback on the live page.
CHAT_EDITOR_SELECTOR = ".messageEditorimChatEditorContainer"
# Strict editable input selector for typing actions (must not include outer container)
CHAT_INPUT_SELECTOR_PARTS = (
    '[contenteditable="true"][data-placeholder="发送消息"]',
    '[contenteditable="true"][aria-label="发送消息"]',
    '[contenteditable="true"][placeholder="发送消息"]',
    '[role="textbox"][contenteditable="true"]',
    'div.zone-container[contenteditable="true"]',
    'div.messageEditorinputArea[contenteditable="true"]',
    'textarea[placeholder*="发送消息"]',
    '[contenteditable="true"]',
)
CHAT_INPUT_SELECTOR = ", ".join(CHAT_INPUT_SELECTOR_PARTS)
# Douyin has used several editor wrappers over time. Match the semantic
# "发送消息" marker first, then keep the legacy class for older revisions.
CHAT_EDITOR_FALLBACK_SELECTOR = (
    f"{CHAT_INPUT_SELECTOR_PARTS[0]}, {CHAT_EDITOR_SELECTOR}"
)
SEARCH_INPUT_SELECTORS = (
    'input[placeholder*="搜索"]',
    'input[aria-label*="搜索"]',
    '[contenteditable="true"][placeholder*="搜索"]',
    '[contenteditable="true"][aria-label*="搜索"]',
    'xpath=//*[self::input or @contenteditable="true"]'
    '[contains(@placeholder, "搜索") or contains(@aria-label, "搜索") '
    'or contains(@data-placeholder, "搜索")]',
)
SESSION_COOKIE_NAMES = ("sessionid", "sessionid_ss", "sid_guard", "passport_csrf_token")
USER_NUMBER_TARGET_RE = re.compile(r"^用户(\d+)$")
# Douyin keeps showing a conversation id until it fetches that profile.
PLACEHOLDER_TITLE_RE = re.compile(r"^\d{5,}$")
EMOJI_PATTERN = re.compile(r"[\U00010000-\U0010ffff☀-⟿️]")
MAX_USER_SEARCH_SNIPPETS = 40
MAX_EMPTY_SCROLLS = 10
DEFAULT_SEARCH_ACTION_TIMEOUT_MS = 5000
DEFAULT_CHAT_OPEN_TIMEOUT_MS = 10000
FALLBACK_ELEMENT_TIMEOUT_MS = 1500
DEFAULT_TARGET_RETRY_TIMES = 3
DEFAULT_SEARCH_RESULT_WAIT_SECONDS = 4
DEFAULT_CHAT_READY_TIMEOUT_MS = 30000
DEFAULT_LIST_GROWTH_WAIT_SECONDS = 5
DEFAULT_PLACEHOLDER_PROBE_LIMIT = 200
DEFAULT_PLACEHOLDER_PROBE_SECONDS = 900
CHAT_PAGE_URL = "https://www.douyin.com/chat"
CHAT_HEADER_SELECTOR = (
    '.RightPanelHeadertitle, [class*="chatHeader"], [class*="ChatHeader"], [class*="chat-header"], '
    '[class*="messageHeader"], header, [role="heading"]'
)
_unconfirmed_submissions = set()
# Bounded budget so a failing search documents itself without flooding logs.
_search_snapshot_budget = 6


class DeliveryUncertainError(RuntimeError):
    """Submission may have happened; replaying Enter could send a duplicate."""


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


def _is_placeholder_title(value):
    """Tell an unresolved conversation id apart from a real display name.

    Douyin keeps rendering a conversation's numeric id until it fetches that
    contact's profile, so such a title carries no identity of its own.
    """
    return bool(PLACEHOLDER_TITLE_RE.match(_norm_value(value)))


def resolve_placeholder_identity(title):
    """Return the display name a probed ID-only conversation belongs to."""
    return _norm_value(placeholderIdentityDict.get(_norm_value(title), ""))


def _iter_user_records():
    seen = set()
    for values in userIDDict.values():
        record = tuple(_norm_value(value) for value in values)
        if record in seen:
            continue
        seen.add(record)
        yield list(record)


def _user_record_identity(values):
    identifiers = tuple(values[:3])
    return identifiers if any(identifiers) else tuple(values)


def get_search_terms_for_target(target):
    """Resolve IDs first; names may only identify one known user."""
    target = _norm_value(target)
    if not target:
        return []
    terms = [target]
    user_number_match = USER_NUMBER_TARGET_RE.match(target)
    if user_number_match:
        terms.append(user_number_match.group(1))
    seeds = set(terms)
    records = [(values + [""] * 5)[:5] for values in _iter_user_records()]
    matches = [values for values in records if seeds.intersection(values[:3])]
    if not matches:
        matches = [values for values in records if seeds.intersection(values[3:])]
    if not matches:
        return _dedupe(terms)

    identities = {_user_record_identity(values) for values in matches}
    if len(identities) != 1:
        return []
    identity = identities.pop()
    own_ids = set()
    other_ids = set()
    other_aliases = set()
    for values in records:
        short_id, unique_id, sec_uid, nickname, remark_name = values
        if _user_record_identity(values) == identity:
            own_ids.update(value for value in values[:3] if value)
            terms.extend([remark_name, nickname, unique_id, short_id, sec_uid])
        else:
            other_ids.update(value for value in values[:3] if value)
            other_aliases.update(value for value in values if value)

    # Never follow an added nickname into another record. An exact stable ID
    # takes priority over another user's nickname, but a shared name cannot.
    return _dedupe(
        term for term in terms
        if term not in other_aliases or (term in own_ids and term not in other_ids)
    )


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
                    _locator_action(element.locator(CONVERSATION_TITLE_SELECTOR), "inner_text",
                                    timeout=FALLBACK_ELEMENT_TIMEOUT_MS)
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

        if _conversation_scroll_handle(page, username) is None:
            logger.error(f"账号 {username} 匹配诊断未找到滚动容器，退出")
            return found_titles

        if _scroll_conversation_list(page, username):
            logger.debug(
                f"账号 {username} 匹配诊断滚动好友列表，已收集 {len(found_titles)} 个标题"
            )
        else:
            empty_scroll_count += 2
            logger.debug(
                f"账号 {username} 匹配诊断列表不再滚动也不再加载，可能已到底 "
                f"(空滚动计数: {empty_scroll_count}/{MAX_EMPTY_SCROLLS})"
            )
        time.sleep(1.0)


def _chat_header_names(page):
    """Read the open conversation's displayed names from its chat header."""
    try:
        names = page.evaluate(
            """
            ({ listSelector, editorSelector, headerSelector }) => {
                const normalize = (value) => (value || '')
                    .normalize('NFKC')
                    .replace(/[\\u3000\\u00a0]/g, ' ')
                    .replace(/[\\u200b\\ufeff]/g, '')
                    .replace(/\\s+/g, ' ')
                    .trim();
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
                const headers = Array.from(document.querySelectorAll(headerSelector))
                    .filter((header) => {
                        const rect = header.getBoundingClientRect();
                        return rect.width > 0 && rect.height > 0
                            && rect.left >= rightPaneStart - 4
                            && rect.bottom <= editorTop
                            && rect.height <= 160;
                    });
                if (!headers.length) {
                    return [];
                }
                const headerTop = Math.min(
                    ...headers.map((header) => header.getBoundingClientRect().top)
                );
                const titled = [];
                const others = [];
                for (const header of headers) {
                    if (header.getBoundingClientRect().top > headerTop + 24) continue;
                    const elements = [header].concat(
                        Array.from(header.querySelectorAll('h1,h2,h3,div,span,a,button'))
                    );
                    for (const element of elements) {
                        const rect = element.getBoundingClientRect();
                        if (!rect || rect.width <= 0 || rect.height <= 0) continue;
                        if (rect.right <= rightPaneStart || rect.bottom > editorTop + 24) continue;
                        const text = normalize(element.innerText || element.textContent || '');
                        if (!text || text.length > 80) continue;
                        // The title node names the contact; sibling nodes may
                        // carry counters or status text, so keep them last.
                        if (/title/i.test(String(element.className || ''))) {
                            titled.push(text);
                        } else {
                            others.push(text);
                        }
                    }
                }
                return titled.concat(others);
            }
            """,
            {
                "listSelector": CONVERSATION_LIST_SELECTOR,
                "editorSelector": CHAT_EDITOR_FALLBACK_SELECTOR,
                "headerSelector": CHAT_HEADER_SELECTOR,
            },
        )
    except Exception:
        traceback.print_exc()
        return []
    return _dedupe(names or [])


def _open_chat_names(page, exclude=(), timeout=None):
    """Wait for the open conversation to show real names instead of an id.

    Every header candidate is returned, title node first: a neighbouring node
    can glue status text onto the name, so the caller decides which candidate
    identifies the contact.  Names already on screen before the conversation
    was opened are excluded on purpose - a click that opens nothing must not
    hand the previous chat's identity to this one.
    """
    timeout = timeout if timeout is not None else config.get(
        "chatOpenTimeout", DEFAULT_CHAT_OPEN_TIMEOUT_MS
    )
    excluded = {_norm_value(value) for value in exclude if _norm_value(value)}
    deadline = time.monotonic() + max(0, int(timeout)) / 1000
    while True:
        names = [
            name for name in _chat_header_names(page)
            if name not in excluded and not _is_placeholder_title(name)
        ]
        if names:
            return names
        if time.monotonic() >= deadline:
            return []
        time.sleep(0.25)


def _conversation_scroll_handle(page, username):
    try:
        return _element_handle_with_timeout(
            page.locator(CONVERSATION_LIST_SELECTOR), FALLBACK_ELEMENT_TIMEOUT_MS
        )
    except Exception as error:
        logger.debug(f"账号 {username} 未找到好友列表滚动容器: {error}")
        return None


def _conversation_scroll_metrics(page, scrollable):
    metrics = page.evaluate(
        "(element) => ({ top: element.scrollTop, height: element.scrollHeight })",
        scrollable,
    )
    if not isinstance(metrics, dict):
        return {"top": 0, "height": 0}
    return {"top": metrics.get("top") or 0, "height": metrics.get("height") or 0}


def _scroll_conversation_list(page, username, delta=800, settle_seconds=None):
    """Scroll the conversation list, waiting for it to load the next page.

    Douyin fetches more conversations only once the scroll reaches the end of
    what is already rendered, so an unchanged scrollTop means "still loading"
    at least as often as it means "bottom".  Reading it as the bottom is what
    made a run walk 45 of 101 conversations and declare the rest missing.
    """
    scrollable = _conversation_scroll_handle(page, username)
    if scrollable is None:
        return False
    settle = settle_seconds if settle_seconds is not None else config.get(
        "listGrowthWaitSeconds", DEFAULT_LIST_GROWTH_WAIT_SECONDS
    )
    try:
        before = _conversation_scroll_metrics(page, scrollable)
        deadline = time.monotonic() + max(0.0, float(settle))
        while True:
            page.evaluate(
                f"(element) => {{ element.scrollTop += {int(delta)}; }}", scrollable
            )
            time.sleep(0.3)
            after = _conversation_scroll_metrics(page, scrollable)
            if after["top"] != before["top"] or after["height"] > before["height"]:
                return True
            if time.monotonic() >= deadline:
                logger.debug(
                    f"账号 {username} 好友列表 {settle}s 内既未滚动也未加载更多 "
                    f"(scrollTop {before['top']}, scrollHeight {before['height']})"
                )
                return False
    except Exception as error:
        logger.debug(f"账号 {username} 滚动好友列表失败: {error}")
        return False


def _reset_conversation_scroll(page, username):
    scrollable = _conversation_scroll_handle(page, username)
    if scrollable is None:
        return False
    try:
        page.evaluate("element => { element.scrollTop = 0; }", scrollable)
    except Exception as error:
        logger.debug(f"账号 {username} 复位好友列表失败: {error}")
        return False
    return True


def probe_placeholder_identities(page, username, remaining_targets, limit=None, timeout=None):
    """Open ID-only conversations so pending targets can be recognised.

    Douyin renders a numeric id for every conversation whose profile it never
    fetched, and a numeric title matches no target by name.  Opening such a
    conversation forces that fetch and reveals the contact in the chat header,
    which is the only identity this run can trust.  Probing never types or
    sends anything.
    """
    pending = _dedupe(remaining_targets)
    if not pending:
        return {}
    limit = max(1, int(
        limit if limit is not None
        else config.get("placeholderProbeLimit", DEFAULT_PLACEHOLDER_PROBE_LIMIT)
    ))
    seconds = max(1, int(
        timeout if timeout is not None
        else config.get("placeholderProbeSeconds", DEFAULT_PLACEHOLDER_PROBE_SECONDS)
    ))
    deadline = time.monotonic() + seconds
    logger.info(
        f"账号 {username} 开始探测未解析会话身份，待匹配目标 {len(pending)} 个，"
        f"最多探测 {limit} 个会话"
    )
    resolved = {}
    matched = {}
    probed = set()
    seen_titles = set()
    # Seed the exclusion with the chat that is already open, so a click that
    # opens nothing cannot hand its names to the conversation being probed.
    open_names = set(_chat_header_names(page))
    idle_scrolls = 0
    _reset_conversation_scroll(page, username)
    while pending and len(probed) < limit and time.monotonic() < deadline:
        candidate = None
        titles_before = len(seen_titles)
        for element in page.locator(CONVERSATION_ITEM_SELECTOR).all():
            try:
                if hasattr(element, "is_visible") and not element.is_visible():
                    continue
                title = _norm_value(
                    _locator_action(
                        element.locator(CONVERSATION_TITLE_SELECTOR),
                        "inner_text",
                        timeout=FALLBACK_ELEMENT_TIMEOUT_MS,
                    )
                )
            except Exception:
                continue
            if not title:
                continue
            seen_titles.add(title)
            if title in probed or not _is_placeholder_title(title):
                continue
            if resolve_placeholder_identity(title):
                probed.add(title)
                continue
            candidate = (element, title)
            break

        if candidate is None:
            # The list is virtualized: stop once scrolling stops revealing
            # conversations, so an endless list never stalls the run.
            moved = _scroll_conversation_list(page, username)
            if moved and len(seen_titles) > titles_before:
                idle_scrolls = 0
            else:
                idle_scrolls += 1 if moved else 2
                if idle_scrolls >= MAX_EMPTY_SCROLLS:
                    break
            time.sleep(1.0)
            continue

        idle_scrolls = 0
        element, title = candidate
        probed.add(title)
        try:
            _click_chat_candidate(element)
        except Exception as error:
            logger.debug(f"账号 {username} 打开未解析会话 {title} 失败: {error}")
            continue
        names = _open_chat_names(page, exclude=open_names)
        if not names:
            logger.warning(
                f"账号 {username} 未解析会话 {title} 打开后仍无法确认身份，"
                f"当前标题候选 {_chat_header_names(page)[:5]}"
            )
            continue
        open_names = set(names)
        # The header may expose the name both alone and wrapped in status
        # text; keep the candidate that identifies a pending target.
        name = names[0]
        target = None
        for candidate in names:
            candidate_target = checkTargetName(candidate, pending)
            if candidate_target:
                name, target = candidate, candidate_target
                break
        placeholderIdentityDict[_norm_value(title)] = name
        resolved[_norm_value(title)] = name
        if target:
            matched[target] = name
            pending = [value for value in pending if value != target]
            logger.info(
                f"账号 {username} 未解析会话 {title} 确认为待发送目标 {target}（显示名 {name}）"
            )
        else:
            logger.debug(f"账号 {username} 未解析会话 {title} 实为 {name}，不在待发送目标内")

    logger.info(
        f"账号 {username} 未解析会话探测结束: 探测 {len(probed)} 个，解析 {len(resolved)} 个，"
        f"命中待发送目标 {len(matched)} 个，仍未匹配 {len(pending)} 个"
    )
    _reset_conversation_scroll(page, username)
    return resolved


def _preload_friend_list(page, username):
    """Load profile names before message activity starts reordering the list."""
    titles = collect_friend_titles(page, username)
    try:
        scrollable = _element_handle_with_timeout(
            page.locator(CONVERSATION_LIST_SELECTOR), FALLBACK_ELEMENT_TIMEOUT_MS
        )
        if scrollable is not None:
            page.evaluate("element => { element.scrollTop = 0; }", scrollable)
    except Exception as error:
        logger.warning(f"账号 {username} 预加载后复位好友列表失败: {error}")
    logger.info(
        f"账号 {username} 发送前列表预加载完成: {len(titles)} 个标题，"
        f"其中纯数字标题 {sum(title.isdigit() for title in titles)} 个"
    )
    return titles


def diagnose_friend_matching(page, username, targets):
    logger.info(f"账号 {username} 启用好友匹配诊断模式，不发送消息")
    retry_operation(
        "打开抖音网页聊天页面",
        page.goto,
        retries=config["taskRetryTimes"],
        delay=5,
        url="https://www.douyin.com/chat",
        wait_until="commit",
    )
    if not wait_for_chat_ready(page, username):
        raise RuntimeError(f"账号 {username} 诊断时聊天页面未就绪")
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
    # List membership alone does not validate the live chat/search selectors.
    # Probe one listed contact and one search-only contact without typing a
    # message or creating a delivery-state entry.
    probes = list(matched)[:1] + unmatched[:1]
    for target in probes:
        try:
            _open_chat_page_for_retry(page, username)
            selected = click_matching_visible_user(page, username, [target])
            if not selected and target in unmatched:
                search_input = find_search_input(page)
                if search_input is not None:
                    fill_search_input(search_input, target)
                    time.sleep(2)
                    selected = click_matching_visible_user(page, username, [target])
                    if not selected:
                        selected = click_visible_text_result(page, username, target, [target])
            structure = page.evaluate(r"""() => {
                const visible = element => {
                    const r = element.getBoundingClientRect();
                    return r.width > 0 && r.height > 0;
                };
                const describe = element => ({
                    tag: element.tagName, css: String(element.className || ''),
                    marker: element.getAttribute('data-e2e'),
                    placeholder: element.getAttribute('data-placeholder')
                        || element.getAttribute('placeholder'),
                    x: Math.round(element.getBoundingClientRect().x),
                    y: Math.round(element.getBoundingClientRect().y)
                });
                return {
                    inputs: [...document.querySelectorAll('input,[contenteditable="true"]')]
                        .filter(visible).map(describe).slice(0, 12),
                    chat: [...document.querySelectorAll('[class]')].filter(element => {
                        const r = element.getBoundingClientRect();
                        return visible(element) && r.left > innerWidth * 0.25
                            && /header|title|editor/i.test(String(element.className))
                            && r.top < innerHeight;
                    }).map(describe).slice(0, 20)
                };
            }""")
            logger.info(f"账号 {username} 诊断页面结构: {json.dumps(structure, ensure_ascii=False)}")
            logger.info(
                f"账号 {username} 诊断会话抽查: 目标 {target}，"
                f"身份已确认={bool(selected)}，编辑器可见={_find_chat_input(page) is not None}"
            )
        except Exception as error:
            logger.warning(f"账号 {username} 诊断会话抽查失败: 目标 {target}，{error}")

    # Delivery decides who a chat belongs to by reading its header, and ID-only
    # conversations are resolved the same way.  Exercise that reading against
    # the live page here, where nothing is typed or sent.
    placeholder_titles = [title for title in friend_titles if _is_placeholder_title(title)]
    logger.info(
        f"账号 {username} 匹配诊断发现仅显示 ID 的会话 {len(placeholder_titles)} 个"
    )
    try:
        _open_chat_page_for_retry(page, username)
        previous = next(
            (name for name in _chat_header_names(page) if not _is_placeholder_title(name)),
            "",
        )
        checked = 0
        for element in page.locator(CONVERSATION_ITEM_SELECTOR).all():
            if checked >= 3:
                break
            try:
                if hasattr(element, "is_visible") and not element.is_visible():
                    continue
                title = _norm_value(
                    _locator_action(
                        element.locator(CONVERSATION_TITLE_SELECTOR),
                        "inner_text",
                        timeout=FALLBACK_ELEMENT_TIMEOUT_MS,
                    )
                )
                if not title:
                    continue
                _click_chat_candidate(element)
                names = _open_chat_names(page, exclude=(previous,))
                logger.info(
                    f"账号 {username} 标题读取抽查: 列表标题 {title} -> 聊天标题候选 {names}"
                )
                if names:
                    previous = names[0]
                checked += 1
            except Exception as error:
                logger.warning(f"账号 {username} 标题读取抽查失败: {error}")
        if placeholder_titles:
            resolved = probe_placeholder_identities(page, username, targets)
            logger.info(
                f"账号 {username} 诊断占位会话解析结果: {json.dumps(resolved, ensure_ascii=False)}"
            )
    except Exception as error:
        logger.warning(f"账号 {username} 标题读取抽查未完成: {error}")
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
    """Match only exact, unambiguous target aliases or safe emoji/bracket-free variants."""
    targetName = _norm_value(targetName)
    if not targetName:
        return None

    # A conversation that only rendered its id was probed earlier; match the
    # name that probe read from the chat header, never the id itself.
    probed_name = resolve_placeholder_identity(targetName)
    if probed_name:
        targetName = probed_name

    # Filter out ambiguous targets whose alias cannot be uniquely resolved
    valid_targets = [
        target for target in targets
        if get_search_terms_for_target(target)
    ]
    if not valid_targets:
        return None

    # 1. Exact match via search terms
    for target in valid_targets:
        if targetName in get_search_terms_for_target(target):
            return _norm_value(target)

    # 2. Match without trailing/contained emojis (unambiguous candidate only)
    stripped_name = EMOJI_PATTERN.sub("", targetName).strip()
    if stripped_name:
        candidates = []
        for target in valid_targets:
            target_norm = _norm_value(target)
            stripped_target = EMOJI_PATTERN.sub("", target_norm).strip()
            # Either side may carry the decoration: a contact can drop the
            # emoji from a nickname the target list still spells with it.
            decorated = stripped_name != targetName or stripped_target != target_norm
            if decorated and (stripped_name == stripped_target or stripped_name == target_norm):
                candidates.append(target_norm)
        if len(candidates) == 1:
            return candidates[0]

    # 3. Match prefix before bracket notes like (xxx or （xxx
    base_name = re.sub(r"[\(（].*$", "", targetName).strip()
    if base_name and len(base_name) >= 2:
        candidates = []
        for target in valid_targets:
            target_norm = _norm_value(target)
            if base_name != targetName and (base_name == target_norm or base_name == EMOJI_PATTERN.sub("", target_norm).strip()):
                candidates.append(target_norm)
        if len(candidates) == 1:
            return candidates[0]

    return None


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
    rejected before the search input existed. The login prompt can also appear
    briefly before the session hydrates. Require it to disappear before using
    the chat shell, and classify persistent login failure only at the deadline.
    """
    timeout = timeout or int(
        os.getenv("CHAT_READY_TIMEOUT", str(DEFAULT_CHAT_READY_TIMEOUT_MS))
    )
    deadline = time.monotonic() + max(1000, timeout) / 1000
    while time.monotonic() < deadline:
        _dismiss_login_prompt(page, username)
        if not _logged_out(page):
            try:
                if find_search_input(page):
                    return True
                if page.locator(CONVERSATION_ITEM_SELECTOR).count() > 0:
                    return True
            except Exception:
                pass
        time.sleep(0.5)

    if _logged_out(page):
        raise RuntimeError(
            f"账号 {username} 聊天页面在 {timeout}ms 后仍显示登录页，"
            f"请检查登录状态；页面状态: {_page_state(page)}"
        )
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

    Production runners may override the path; local runs also persist attempts
    so restarting after an uncertain submission cannot silently replay it.
    """
    value = os.getenv("DELIVERY_STATE_FILE", "logs/delivery-state.json").strip()
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


def _cookie_fingerprint(cookies):
    """Fingerprint the login cookies so rotation is visible in a shared log.

    Only a truncated digest is kept: the log artifact of a public repository
    must never carry the session value itself.
    """
    summary = {}
    try:
        entries = list(cookies or [])
    except TypeError:
        return summary
    for cookie in entries:
        if not isinstance(cookie, dict):
            continue
        name = _norm_value(cookie.get("name"))
        if name not in SESSION_COOKIE_NAMES:
            continue
        value = str(cookie.get("value", ""))
        summary[name] = (
            hashlib.sha256(value.encode("utf-8")).hexdigest()[:8] if value else ""
        )
    return summary


def _usable_cookies(cookies):
    """Keep only entries a browser can restore.

    Douyin serves one nameless cookie; carrying it into the next run's jar
    gains nothing and risks the whole restore being rejected.
    """
    usable = []
    for cookie in cookies or []:
        if not isinstance(cookie, dict):
            continue
        if not _norm_value(cookie.get("name")) or not str(cookie.get("value", "")).strip():
            continue
        usable.append(cookie)
    return usable


def _persist_cookie_snapshot(cookies, account_key=None):
    """Persist refreshed browser cookies without truncating other accounts."""
    value = os.getenv("HUOHUA_COOKIE_PERSIST_FILE", "").strip()
    if not value or not cookies:
        return
    dropped = len(cookies) - len(_usable_cookies(cookies))
    cookies = _usable_cookies(cookies)
    if not cookies:
        return
    if dropped:
        logger.debug(f"会话快照忽略了 {dropped} 条无法还原的 Cookie")
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
    return {
        _delivery_state_key(target)
        for target, status in _target_delivery_statuses(username, targets, aliases).items()
        if status in {"sent", "submitted"}
    }


def _delivery_recipient_identity(target):
    """Use only the stable IDs accepted by the current unambiguous alias map."""
    terms = set(get_search_terms_for_target(target))
    identities = {
        tuple((values + [""] * 3)[:3])
        for values in _iter_user_records()
        if terms.intersection(value for value in values[:3] if value)
    }
    if len(identities) != 1:
        return {}
    return {
        field: value
        for field, value in zip(("short_id", "unique_id", "sec_uid"), identities.pop())
        if value
    }


def _delivery_entry_identity(entry):
    identity = entry.get("recipient_identity")
    if identity is None:
        return {}
    if (
        not isinstance(identity, dict)
        or not identity
        or any(field not in {"short_id", "unique_id", "sec_uid"}
               or not isinstance(value, str) or not _norm_value(value)
               for field, value in identity.items())
    ):
        raise RuntimeError("续火状态文件格式错误：收件人稳定身份无效")
    return {field: _norm_value(value) for field, value in identity.items()}


def _same_delivery_identity(left, right):
    shared = left.keys() & right.keys()
    return bool(shared) and all(left[field] == right[field] for field in shared)


def _delivery_recipient_key(target, identity):
    for field in ("sec_uid", "unique_id", "short_id"):
        if identity.get(field):
            return f"@recipient:{field}:{identity[field]}"
    return _delivery_state_key(target)


def _matching_delivery_entries(target, account_state):
    target_key = _delivery_state_key(target)
    terms = set(get_search_terms_for_target(target))
    identity = _delivery_recipient_identity(target)
    # An unconfirmed name (including 用户123) retains only its original key.
    legacy_keys = terms if identity else ({target_key} if terms else set())
    entries = []
    for key, entry in account_state.items():
        if not isinstance(entry, dict):
            if _delivery_state_key(key) in legacy_keys:
                raise RuntimeError(f"续火状态文件格式错误：目标 {target} 的状态必须是对象")
            continue
        entries.append((key, entry, _delivery_entry_identity(entry)))

    if not identity and terms:
        # Persisted IDs remain useful before the API hydrates. Never infer a
        # different name from old display text, or override a live conflict.
        recorded = [
            saved for key, entry, saved in entries if saved and (
                target_key in saved.values()
                or target_key == _delivery_state_key(entry.get("target", key))
            )
        ]
        if recorded and all(_same_delivery_identity(left, right)
                            for left in recorded for right in recorded):
            identity = {field: value for saved in recorded for field, value in saved.items()}

    return [
        (key, entry) for key, entry, saved in entries
        if (_same_delivery_identity(identity, saved) if saved
            else _delivery_state_key(key) in legacy_keys)
    ]


def _target_delivery_statuses(username, targets, aliases=()):
    state = _load_delivery_state()
    _, day_state = _today_delivery_state(state)
    account_keys = [_delivery_state_key(username)] + [
        _delivery_state_key(alias) for alias in aliases
    ]
    statuses = {}
    today = date.today().isoformat()
    for target in targets:
        target_key = _delivery_state_key(target)
        for account_key in account_keys:
            account_state = day_state.get(account_key, {})
            for key, entry in _matching_delivery_entries(target, account_state):
                status = entry.get("status", "sent" if entry.get("sent_at") else "")
                if status not in {"pending", "submitted", "sent"}:
                    raise RuntimeError(f"续火状态文件格式错误：目标 {target} 的提交状态无效")
                if (today, account_key, key) in _unconfirmed_submissions:
                    status = "pending"
                if statuses.get(target_key) != "pending":
                    statuses[target_key] = status
            # Preserve in-process protection when persistence was explicitly
            # disabled or its file has disappeared after a pending write.
            identity = _delivery_recipient_identity(target)
            memory_keys = {_delivery_recipient_key(target, identity)}
            if identity:
                memory_keys.update(get_search_terms_for_target(target))
            for key in memory_keys - account_state.keys():
                if (today, account_key, key) in _unconfirmed_submissions:
                    statuses[target_key] = "pending"
    return statuses


def _mark_target_pending_today(username, target, message):
    state, day_state = _today_delivery_state()
    account_key = _delivery_state_key(username)
    if _target_delivery_statuses(username, [target]):
        raise DeliveryUncertainError(f"目标 {target} 已有提交记录，暂停自动重发")
    identity = _delivery_recipient_identity(target)
    record_key = _delivery_recipient_key(target, identity)
    entry = {
        "status": "pending",
        "attempted_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "message_hash": hashlib.sha256(message.encode("utf-8")).hexdigest(),
        "target": _delivery_state_key(target),
    }
    if identity:
        entry["recipient_identity"] = identity
    account_state = day_state.setdefault(account_key, {})
    if record_key in account_state:
        raise DeliveryUncertainError(f"目标 {target} 的稳定身份与已有状态冲突，暂停自动重发")
    account_state[record_key] = entry
    # Persist before pressing Enter, so a crash or a failed final write cannot
    # turn an uncertain submission into an automatic resend on process restart.
    _persist_delivery_state(state)
    _unconfirmed_submissions.add(
        (date.today().isoformat(), account_key, record_key)
    )


def _mark_target_sent_today(username, target):
    state = _load_delivery_state()
    state, day_state = _today_delivery_state(state)
    account_key = _delivery_state_key(username)
    account_state = day_state.setdefault(account_key, {})
    target_key = _delivery_state_key(target)
    today = date.today().isoformat()
    # Finish the attempt captured before Enter, even if its nickname was
    # reassigned or API mappings changed while submission was in progress.
    active = [
        key for key, entry in account_state.items()
        if (today, account_key, key) in _unconfirmed_submissions
        and _delivery_state_key(entry.get("target", key)) == target_key
    ]
    identity = _delivery_recipient_identity(target)
    record_key = _delivery_recipient_key(target, identity)
    matches = dict(_matching_delivery_entries(target, account_state))
    if len(active) == 1:
        record_key = active[0]
    elif len(active) > 1:
        raise DeliveryUncertainError(f"目标 {target} 存在多个待核验提交，拒绝批量确认")
    elif record_key not in matches and matches:
        if len(matches) != 1:
            raise DeliveryUncertainError(f"目标 {target} 存在多个身份状态，拒绝批量确认")
        record_key = next(iter(matches))
    elif record_key in account_state and record_key not in matches:
        raise DeliveryUncertainError(f"目标 {target} 的稳定身份与已有状态冲突")
    entry = account_state.setdefault(record_key, {"target": target_key})
    if identity and not entry.get("recipient_identity"):
        entry["recipient_identity"] = identity
    entry.update({
        "status": "submitted",
        "sent_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    })
    _persist_delivery_state(state)
    _unconfirmed_submissions.discard(
        (today, account_key, record_key)
    )


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
    # The exact data-placeholder selector is the path verified by the last
    # successful production run.  Prefer it before marker-free contenteditables
    # so a search/result textbox cannot receive the message.
    preferred = []
    for selector_index, selector in enumerate(CHAT_INPUT_SELECTOR_PARTS[:3]):
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
                preferred.append((selector_index, index, candidate))
            except Exception:
                continue

    if preferred:
        return min(preferred, key=lambda item: item[:2])[2]

    # The wrapper was the reliable fallback in older Douyin builds and is safer
    # than selecting an arbitrary marker-free contenteditable.
    try:
        wrapper_locator = page.locator(CHAT_EDITOR_SELECTOR)
        for index in range(wrapper_locator.count()):
            wrapper = wrapper_locator.nth(index)
            if wrapper.is_visible():
                return wrapper
    except Exception:
        pass

    candidates = []
    for selector_index, selector in enumerate(CHAT_INPUT_SELECTOR_PARTS[3:], start=3):
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
    return None


def _chat_submission_snapshot(page, message):
    """Read a rendered-message baseline without treating a keypress as delivery.

    This is a UI confirmation, not a server receipt. Missing or unrecognizable
    UI leaves the durable attempt pending instead of authorizing another send.
    """
    return page.evaluate(
        r"""({message, editorSelector, listSelector}) => {
            const normalize = value => (value || '').normalize('NFKC')
                .replace(/[\u200b\ufeff]/g, '').replace(/\s+/g, ' ').trim();
            const visible = element => {
                const r = element.getBoundingClientRect();
                return r.width > 0 && r.height > 0;
            };
            const list = document.querySelector(listSelector);
            const paneStart = list ? list.getBoundingClientRect().right : innerWidth * 0.3;
            const editor = [...document.querySelectorAll(editorSelector)].find(element =>
                visible(element) && element.getBoundingClientRect().left >= paneStart - 40
                && !/搜索/.test(['placeholder', 'aria-label', 'data-placeholder']
                    .map(name => element.getAttribute(name) || '').join(' ')));
            if (!editor) throw new Error('message editor unavailable');
            const editorTop = editor.getBoundingClientRect().top;
            const expected = normalize(message);
            const renderedText = node => {
                if (node.nodeType === 3) return node.textContent || '';
                if (node.nodeName === 'BR') return '\n';
                if (node.nodeName === 'IMG') {
                    const label = node.getAttribute('alt') || node.getAttribute('title') || '';
                    return label && !label.startsWith('[') ? `[${label}]` : label;
                }
                const value = [...node.childNodes].map(renderedText).join('');
                return value + (/^(DIV|P|LI)$/.test(node.nodeName) ? '\n' : '');
            };
            const textOf = element => normalize(renderedText(element));
            const nodes = [...document.querySelectorAll('div,span,p')].filter(element => {
                if (!visible(element) || editor.contains(element) || element.contains(editor)) return false;
                const r = element.getBoundingClientRect();
                return r.left >= paneStart - 4 && r.bottom <= editorTop && r.top >= 0;
            });
            const matching = nodes.filter(element => textOf(element) === expected);
            const leafMatches = matching.filter(element =>
                !matching.some(other => other !== element && element.contains(other)));
            const failures = nodes.filter(element => /^(发送失败|重新发送|未发送)$/.test(textOf(element)));
            return {
                editor_text: normalize(editor.value || renderedText(editor)),
                message_count: leafMatches.length,
                failure_count: failures.length,
            };
        }""",
        {
            "message": message,
            "editorSelector": CHAT_INPUT_SELECTOR,
            "listSelector": CONVERSATION_LIST_SELECTOR,
        },
    )


def _wait_for_submission_confirmation(page, target, message, before, timeout):
    deadline = time.monotonic() + max(1000, timeout) / 1000
    observed_at = None
    while time.monotonic() < deadline:
        current = _chat_submission_snapshot(page, message)
        if current["failure_count"] > before["failure_count"]:
            raise DeliveryUncertainError("页面显示发送失败，需核对本次消息后再决定是否重试")
        matched, _ = _chat_target_match(page, target)
        if (
            matched
            and not current["editor_text"]
            and current["message_count"] > before["message_count"]
        ):
            observed_at = time.monotonic() if observed_at is None else observed_at
            if time.monotonic() - observed_at >= 1:
                return
        else:
            observed_at = None
        time.sleep(0.25)
    raise DeliveryUncertainError("未确认输入框清空且当前聊天出现本次消息，已保留待核验状态")


def _submit_chat_message(page, account_name, target, message=None, delivery_key=None):
    """Type and submit message into the currently confirmed chat editor."""
    account_key = delivery_key or account_name
    status = _target_delivery_statuses(
        account_key, [target], aliases=(account_name,)
    ).get(_delivery_state_key(target))
    if status in {"sent", "submitted"}:
        return target
    if status == "pending":
        raise DeliveryUncertainError(f"目标 {target} 存在待核验提交，暂停自动重发")
    if not _chat_target_match(page, target)[0]:
        raise RuntimeError(f"目标 {target} 的当前聊天身份未确认")
    chat_input = _wait_for_chat_input(page)
    if chat_input is None:
        raise RuntimeError("当前聊天没有可用的消息输入框")
    # The placeholder may disappear after the first typed character. Keep the
    # selected node for this transaction instead of resolving that selector on
    # every Shift+Enter/Enter. A replaced node fails rather than targeting a
    # different editor.
    if callable(getattr(chat_input, "element_handle", None)):
        chat_input = _element_handle_with_timeout(chat_input, FALLBACK_ELEMENT_TIMEOUT_MS)
        if chat_input is None:
            raise RuntimeError("当前消息输入框已失效")
    message = build_message() if message is None else str(message)
    lines = re.split(r"\\n|\r?\n", message)
    rendered_message = "\n".join(lines)
    if not _norm_value(rendered_message):
        raise RuntimeError("本次消息为空，拒绝提交")
    before = _chat_submission_snapshot(page, rendered_message)
    if before["editor_text"]:
        raise RuntimeError("消息输入框中已有草稿，拒绝追加本次消息")
    send_timeout = max(1000, int(config.get("chatSendActionTimeout", 10000)))
    for index, line in enumerate(lines):
        _locator_action(chat_input, "type", line, timeout=send_timeout)
        if index < len(lines) - 1:
            _locator_action(chat_input, "press", "Shift+Enter", timeout=send_timeout)

    prepared = _chat_submission_snapshot(page, rendered_message)
    if prepared["editor_text"] != _norm_value(rendered_message):
        raise RuntimeError("输入框内容与本次消息不一致，拒绝提交")
    if not _chat_target_match(page, target)[0]:
        raise RuntimeError(f"输入期间当前聊天已改变，拒绝向目标 {target} 提交")
    logger.debug(
        f"账号 {account_name} 准备发送消息给好友 {target}：\n\t{message}"
    )
    _mark_target_pending_today(account_key, target, rendered_message)
    try:
        _locator_action(chat_input, "press", "Enter", timeout=send_timeout)
        _wait_for_submission_confirmation(page, target, rendered_message, before, send_timeout)
        # Retrying only the durable write is safe; never replay Enter here.
        for attempt in range(3):
            try:
                _mark_target_sent_today(account_key, target)
                break
            except RuntimeError:
                if attempt == 2:
                    raise
                time.sleep(0.1)
    except Exception as error:
        raise DeliveryUncertainError(
            f"目标 {target} 已尝试提交，结果待核验，暂停自动重发: {error}"
        ) from error
    logger.debug(f"账号 {account_name} 给好友 {target} 的消息已在页面显示（非服务端送达回执）")
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
    selected_target = None
    current_match, _ = _chat_target_match(page, target)
    if current_match:
        selected_target = target
    if not selected_target:
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
        except DeliveryUncertainError as error:
            logger.error(f"账号 {account_name} 目标 {target} 待核验: {error}")
            return None
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
            ({ listSelector, editorSelector, headerSelector, terms }) => {
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

                // Only an explicit chat header can establish identity. Text
                // inside a message (including a quoted friend's name) cannot.
                const headers = Array.from(document.querySelectorAll(headerSelector))
                    .filter((header) => {
                        const rect = header.getBoundingClientRect();
                        return rect.width > 0 && rect.height > 0
                            && rect.left >= rightPaneStart - 4
                            && rect.bottom <= editorTop
                            && rect.height <= 160;
                    });
                const headerTop = Math.min(...headers.map(header => header.getBoundingClientRect().top));
                const candidates = new Set();
                for (const header of headers) {
                    if (header.getBoundingClientRect().top > headerTop + 24) continue;
                    candidates.add(header);
                    for (const child of header.querySelectorAll('h1,h2,h3,div,span,a,button')) {
                        candidates.add(child);
                    }
                }
                for (const element of candidates) {
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
                    const cleanText = text.replace(/[\uD800-\uDBFF][\uDC00-\uDFFF]|[☀-⟿️]/g, '').trim();
                    const baseText = text.replace(/[\(（].*$/, '').trim();
                    if (
                        normalizedTerms.some((term) => text === term)
                        || normalizedTerms.some((term) => {
                            const cleanTerm = term.replace(/[\uD800-\uDBFF][\uDC00-\uDFFF]|[☀-⟿️]/g, '').trim();
                            return (cleanText && (cleanText === cleanTerm || cleanText === term))
                                || (baseText && (baseText === cleanTerm || baseText === term));
                        })
                    ) {
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
                "headerSelector": CHAT_HEADER_SELECTOR,
                "terms": terms,
            },
        )
    except Exception:
        traceback.print_exc()
        return False, []
    snippets = result.get("snippets", [])
    # Recheck candidates against the account-specific ID/alias rules. The DOM
    # must not bypass rejection of an ambiguous nickname.
    matched = any(checkTargetName(snippet, [target]) == _norm_value(target) for snippet in snippets)
    return bool(result.get("matched")) and matched, snippets


def wait_for_chat_editor(page, username, target, timeout=None):
    timeout = timeout or config.get("chatOpenTimeout", DEFAULT_CHAT_OPEN_TIMEOUT_MS)
    try:
        page.wait_for_selector(CHAT_EDITOR_FALLBACK_SELECTOR, timeout=timeout)
    except Exception as error:
        logger.warning(f"账号 {username} 选择好友 {target} 后聊天输入框未出现: {error}")
        return False
    for attempt in range(max(1, int(timeout / 250))):
        matched, snippets = _chat_target_match(page, target)
        if matched:
            logger.debug(f"账号 {username} 已确认当前聊天为 {target}: {snippets}")
            return True
        if attempt + 1 < max(1, int(timeout / 250)):
            time.sleep(0.25)
    logger.warning(
        f"账号 {username} 选择好友 {target} 后当前聊天标题未匹配目标，"
        f"搜索词 {get_search_terms_for_target(target)}，可见候选 {snippets}"
    )
    return False


def _locator_action(locator, action, *args, timeout=None):
    method = getattr(locator, action)
    if timeout is not None:
        try:
            parameters = inspect.signature(method).parameters.values()
            accepts_timeout = any(
                parameter.name == "timeout" or parameter.kind == inspect.Parameter.VAR_KEYWORD
                for parameter in parameters
            )
        except (TypeError, ValueError):
            accepts_timeout = True
        if accepts_timeout:
            return method(*args, timeout=timeout)
    # Decide compatibility before invoking an action. A TypeError raised after
    # a keypress must propagate, not cause the keypress to run a second time.
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


def _click_chat_candidate(candidate):
    timeout = min(
        DEFAULT_SEARCH_ACTION_TIMEOUT_MS,
        max(1, int(config.get("chatSearchActionTimeout", DEFAULT_SEARCH_ACTION_TIMEOUT_MS))),
    )
    return _locator_action(candidate, "click", timeout=timeout)


def click_matching_visible_user(page, username, targets):
    for element in page.locator(CONVERSATION_ITEM_SELECTOR).all():
        try:
            if hasattr(element, "is_visible") and not element.is_visible():
                continue
            targetName = _norm_value(
                _locator_action(element.locator(CONVERSATION_TITLE_SELECTOR), "inner_text",
                                timeout=FALLBACK_ELEMENT_TIMEOUT_MS)
            )
            if not targetName:
                continue
            logger.debug(f"账号 {username} 搜索结果好友 {targetName}")
            targetSymbol = checkTargetName(targetName, targets)
            if targetSymbol:
                _click_chat_candidate(element)
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
                _click_chat_candidate(candidate)
                if wait_for_chat_editor(page, username, target):
                    return target
            except Exception:
                traceback.print_exc()
    return None


def _search_result_snapshot(page, limit=10):
    """Describe what the conversation column rendered after a chat search."""
    return page.evaluate(
        """
        ({ listSelector, limit }) => {
            const list = document.querySelector(listSelector);
            const listRect = list ? list.getBoundingClientRect() : null;
            const rightPaneStart = listRect ? listRect.right : window.innerWidth * 0.3;
            const seen = new Set();
            const entries = [];
            for (const element of document.querySelectorAll('div,span,li,a,p')) {
                const rect = element.getBoundingClientRect();
                if (!rect || rect.width <= 0 || rect.height <= 0) continue;
                if (rect.left >= rightPaneStart) continue;
                // Leaf nodes carry the labels; ancestors only repeat them.
                if (element.children.length) continue;
                const text = (element.innerText || element.textContent || '')
                    .replace(/\\s+/g, ' ')
                    .trim();
                if (!text || text.length > 40) continue;
                const css = String(element.className || '');
                const parent = element.parentElement
                    ? String(element.parentElement.className || '')
                    : '';
                const key = css + '|' + parent + '|' + text;
                if (seen.has(key)) continue;
                seen.add(key);
                entries.push({ css, parent, text });
                if (entries.length >= limit) break;
            }
            return entries;
        }
        """,
        {"listSelector": CONVERSATION_LIST_SELECTOR, "limit": limit},
    )


def _log_search_result_snapshot(page, username, target):
    """Record a bounded sample of a fruitless search so it can be diagnosed."""
    global _search_snapshot_budget
    if _search_snapshot_budget <= 0:
        return
    _search_snapshot_budget -= 1
    try:
        entries = _search_result_snapshot(page)
    except Exception:
        traceback.print_exc()
        return
    logger.warning(
        f"账号 {username} 搜索目标 {target} 未产生可点击候选，会话栏可见条目: "
        f"{json.dumps(entries, ensure_ascii=False)}"
    )
    # An empty column reads the same whether the search found nothing or the
    # results render somewhere this snapshot cannot see; a picture settles it.
    try:
        logs_dir = Path("logs")
        logs_dir.mkdir(exist_ok=True)
        shot = logs_dir / f"search-empty-{_safe_filename(target)}.png"
        page.screenshot(path=str(shot))
        logger.warning(f"账号 {username} 搜索目标 {target} 的页面截图: {shot}")
    except Exception:
        traceback.print_exc()


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

    _log_search_result_snapshot(page, username, target)
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
    failed_selections = set()
    # [修改] 复制一份目标列表用于追踪进度
    remaining_targets = set(targets)

    # [修复] 新增：连续空滚动计数器（滚动后没有发现新好友的次数）
    empty_scroll_count = 0
    placeholder_probe_done = False
    MAX_EMPTY_SCROLLS = 10  # 连续10次滚动没有新好友，认为到底了

    while True:
        # 查找所有目标元素
        target_elements = page.locator(target_selector).all()

        # [修复] 记录本轮循环前已发现的好友数，用于判断是否有新发现
        prev_found_count = len(found_targets)

        for element in target_elements:
            targetSymbol = None
            try:
                if hasattr(element, "is_visible") and not element.is_visible():
                    continue
                # 查找子元素 span，模糊匹配 class
                span = element.locator(CONVERSATION_TITLE_SELECTOR)
                targetName = _locator_action(span, "inner_text", timeout=FALLBACK_ELEMENT_TIMEOUT_MS)

                if targetName not in found_targets:
                    found_targets.add(targetName)
                    logger.debug(f"账号 {username} 找到好友 {targetName}")
                
                targetSymbol = checkTargetName(targetName, remaining_targets)

                if targetSymbol and targetSymbol not in failed_selections:
                    _click_chat_candidate(element)
                    if wait_for_chat_editor(page, username, targetSymbol):
                        yield targetSymbol

                        # [修改] 标记已找到，如果全找到了直接退出
                        if targetSymbol in remaining_targets:
                            remaining_targets.remove(targetSymbol)
                        if len(remaining_targets) == 0:
                            logger.debug(f"账号 {username} 所有目标好友均已找到，停止搜索")
                            return
                        break
                    failed_selections.add(targetSymbol)
                    continue
            except Exception as e:
                if targetSymbol:
                    failed_selections.add(targetSymbol)
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
                # Contacts whose profile never loaded sit in the list as bare
                # ids and can only be recognised by opening them.  Resolve
                # those identities once, then walk the list again with them.
                if remaining_targets and not placeholder_probe_done:
                    placeholder_probe_done = True
                    if probe_placeholder_identities(page, username, remaining_targets):
                        found_targets.clear()
                        empty_scroll_count = 0
                        continue
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
                # An unchanged scrollTop is only the bottom once the list has
                # also stopped loading more conversations.
                if _scroll_conversation_list(page, username):
                    logger.debug(
                        f"账号 {username} 滚动好友列表以加载更多好友，"
                        f"已发现 {len(found_targets)} 个标题"
                    )
                else:
                    empty_scroll_count += 2  # 加速判定到底
                    logger.debug(
                        f"账号 {username} 列表不再滚动也不再加载，可能已到底 "
                        f"(空滚动计数: {empty_scroll_count}/{MAX_EMPTY_SCROLLS})"
                    )

                time.sleep(1.0)
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
    placeholderIdentityDict.clear()

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
            # The upstream request may already have been accepted, including a
            # send-message POST. Replaying it via the browser after a timeout
            # or fulfill failure can duplicate the submission.
            route.abort("failed")

    page.route("https://imapi.douyin.com/**", proxy_conversation_api)

    page.on("response", handle_response)  # 监听响应，收集好友完整信息用于匹配

    # 注入 Cookie
    context.add_cookies(cookies)
    injected_fingerprint = _cookie_fingerprint(cookies)
    logger.info(f"账号 {account_name} 注入的会话指纹: {injected_fingerprint}")

    try:
        # A diagnostic also uses up the login session, so its refreshed
        # cookies have to be carried forward exactly like a delivery's.
        if config.get("diagnoseUserSearch"):
            logger.info(f"账号 {username} 启用用户搜索诊断模式，不发送消息")
            try:
                diagnose_user_search(page, username, targets)
            finally:
                session_authenticated = not _logged_out(page)
            return

        if config.get("diagnoseFriendMatching"):
            try:
                diagnose_friend_matching(page, username, targets)
            finally:
                session_authenticated = not _logged_out(page)
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

        # Fetch names before the first submission: the live IM client may leave
        # off-screen contacts as numeric placeholders during message activity.
        preloaded_titles = _preload_friend_list(page, account_name)

        delivery_statuses = _target_delivery_statuses(
            delivery_account_key, all_targets, aliases=(account_name,)
        )
        completed = {
            target for target, status in delivery_statuses.items()
            if status in {"sent", "submitted"}
        }
        uncertain_targets = {
            target for target, status in delivery_statuses.items() if status == "pending"
        }
        pending_targets = [
            target for target in all_targets
            if _delivery_state_key(target) not in completed | uncertain_targets
        ]
        if uncertain_targets:
            logger.error(
                f"账号 {account_name} 有 {len(uncertain_targets)} 个目标的提交结果待核验，"
                f"暂停自动重发: {sorted(uncertain_targets)}"
            )
        if completed:
            logger.info(
                f"账号 {account_name} 今日已有 {len(completed)} 个目标的页面提交记录，跳过重复发送"
            )
        if not pending_targets and not uncertain_targets:
            logger.info(f"账号 {account_name} 今日目标已全部完成: {len(all_targets)}/{len(all_targets)}")
            return

        logger.debug(
            f"账号 {account_name} 开始发送消息，本次待处理 {len(pending_targets)}/{len(all_targets)} 个目标"
        )
        # A conversation Douyin never resolved renders as a bare id and can
        # never be matched by name.  Resolve those identities up front instead
        # of after the delivery pass has already walked the whole list.
        placeholder_titles = [
            title for title in (preloaded_titles or []) if _is_placeholder_title(title)
        ]
        if pending_targets and placeholder_titles:
            logger.info(
                f"账号 {account_name} 预加载发现 {len(placeholder_titles)} 个仅显示 ID 的会话，"
                "先确认身份再开始发送"
            )
            probe_placeholder_identities(page, account_name, pending_targets)

        completed_targets = set(completed)
        message = build_message() if pending_targets else ""
        selections = scroll_and_select_user(page, account_name, pending_targets) if pending_targets else ()
        for target in selections:
            delivered = _send_target_with_retries(
                page,
                account_name,
                target,
                message,
                delivery_account_key,
            )
            if delivered:
                completed_targets.add(_delivery_state_key(target))

        # Mapping may arrive while traversing the virtual list. Count an alias
        # of a completed identity without submitting to that person again.
        final_statuses = _target_delivery_statuses(
            delivery_account_key, all_targets, aliases=(account_name,)
        )
        completed_targets.update(
            target for target, status in final_statuses.items()
            if status in {"sent", "submitted"}
        )
        completed_targets.difference_update(
            target for target, status in final_statuses.items() if status == "pending"
        )
        completed_count = len(completed_targets)
        failed_targets = [
            target for target in all_targets if _delivery_state_key(target) not in completed_targets
        ]
        missing_count = len(all_targets) - completed_count
        if missing_count:
            logger.warning(f"账号 {account_name} 本次有目标未完成: {failed_targets}")
            raise RuntimeError(
                f"账号 {account_name} 未完成全部发送: {completed_count}/{len(all_targets)}；"
                "本次任务标记为失败；未提交目标可重试，待核验提交须先检查聊天记录"
            )
        logger.info(
            f"账号 {account_name} 本次完成页面提交: {completed_count}/{len(all_targets)} 个目标"
        )
    finally:
        try:
            if session_authenticated:
                final_cookies = context.cookies()
                final_fingerprint = _cookie_fingerprint(final_cookies)
                rotated = sorted(
                    name for name, digest in final_fingerprint.items()
                    if injected_fingerprint.get(name) != digest
                )
                logger.info(
                    f"账号 {account_name} 结束时的会话指纹: {final_fingerprint}，"
                    + (f"本次轮换: {rotated}" if rotated else "本次未轮换")
                )
                _persist_cookie_snapshot(final_cookies, delivery_account_key)
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
