import traceback
import re
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
CHAT_EDITOR_SELECTOR = ".messageEditorimChatEditorContainer"
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
            # 获取接口返回的 JSON 数据（就是你在 Network 里看到的内容）
            json_data = response.json()
            # print("\n📦 响应 JSON 数据：")
            # print(json.dumps(json_data, indent=4, ensure_ascii=False))
            for item in json_data.get("data", []):
                short_id = _norm_value(item.get("short_id"))
                unique_id = _norm_value(item.get("unique_id"))
                sec_uid = _norm_value(item.get("sec_uid", ""))
                nickname = _norm_value(item.get("nickname"))
                remark_name = _norm_value(item.get("remark_name", nickname))
                values = [short_id, unique_id, sec_uid, nickname, remark_name]
                if config.get("debugUserIDMapping"):
                    logger.debug(
                        "好友API映射: "
                        f"short_id={short_id}, unique_id={unique_id}, "
                        f"nickname={nickname}, remark_name={remark_name}"
                    )
                for key in {nickname, remark_name}:
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
        list_box = page.locator(CONVERSATION_LIST_SELECTOR).bounding_box()
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
                const editor = document.querySelector(editorSelector);
                const listRect = list ? list.getBoundingClientRect() : null;
                const editorRect = editor ? editor.getBoundingClientRect() : null;
                const rightPaneStart = listRect ? listRect.right : window.innerWidth * 0.3;
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
                "editorSelector": CHAT_EDITOR_SELECTOR,
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
        page.wait_for_selector(CHAT_EDITOR_SELECTOR, timeout=timeout)
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
    for term in terms:
        try:
            search_input = find_search_input(page)
            if not search_input:
                logger.warning(f"账号 {username} 未找到聊天搜索框，无法搜索目标好友 {target}")
                return None
            logger.debug(f"账号 {username} 搜索目标好友 {target}，搜索词: {term}")
            fill_search_input(search_input, term)
            time.sleep(config["friendListTimeout"] / 1000)
            targetSymbol = click_matching_visible_user(page, username, [target])
            if targetSymbol:
                return targetSymbol
            targetSymbol = click_visible_text_result(page, username, target, terms)
            if targetSymbol:
                return targetSymbol
        except Exception:
            traceback.print_exc()

    return None


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
            scrollable_element = page.locator(
                scrollable_friends_selector
            ).element_handle()

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
                logger.error(f"账号 {username} 未找到滚动容器，退出")
                break


def do_user_task(browser, username, cookies, targets):
    context = browser.new_context()  # 每个任务使用独立的上下文
    context.set_default_navigation_timeout(
        config["browserTimeout"]
    )  # 设置导航超时时间为 120 秒
    context.set_default_timeout(
        config["browserTimeout"]
    )  # 设置所有操作的默认超时时间为 120 秒

    page = context.new_page()

    page.on("response", handle_response)  # 监听响应，收集好友完整信息用于匹配

    # 注入 Cookie
    context.add_cookies(cookies)

    if config.get("diagnoseUserSearch"):
        logger.info(f"账号 {username} 启用用户搜索诊断模式，不发送消息")
        diagnose_user_search(page, username, targets)
        context.close()
        return

    if config.get("diagnoseFriendMatching"):
        diagnose_friend_matching(page, username, targets)
        context.close()
        return

    # 打开抖音网页聊天页面
    retry_operation(
        "打开抖音网页聊天页面",
        page.goto,
        retries=config["taskRetryTimes"],
        delay=5,
        url="https://www.douyin.com/chat",
    )

    time.sleep(5)  # 等待5秒让过可能存在的弹窗

    logger.debug(f"账号 {username} 开始发送消息")
    # 滚动并选择用户
    for username in scroll_and_select_user(page, username, targets):
        logger.debug(f"账号 {username} 已选中好友 {username} 发送消息")
        # 等待聊天输入框元素加载完成，使用更稳定的属性选择器
        chat_input_selector = CHAT_EDITOR_SELECTOR
        if not wait_for_chat_editor(page, username, username):
            continue
        chat_input = page.locator(chat_input_selector)

        # 在 chat-input-dccKiL 中输入内容
        message = build_message()
        for line in message.split("\\n"):
            chat_input.type(line)  # 输入每一行
            # 如果不是最后一行，模拟 Shift+Enter 插入换行
            if line != message.split("\\n")[-1]:
                chat_input.press("Shift+Enter")  # 模拟 Shift+Enter 插入换行

        logger.debug(f"账号 {username} 准备发送消息给好友 {username}：\n\t{message}")
        logger.debug(f"账号 {username} 给好友 {username} 发送消息完成")
        # 模拟按下回车键发送消息
        chat_input.press("Enter")
        time.sleep(2)  # 发送完等待一会儿

    context.close()  # 任务完成后关闭上下文


def runTasks():
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

        for user in userData:
            cookies = user["cookies"]
            targets = user["targets"]
            username = user.get("username", "未知用户")
            logger.info(f"开始处理账号 {username}")
            # 创建任务
            do_user_task(browser, username, cookies, targets)
            logger.info(f"账号 {username} 任务完成")
    finally:
        # 关闭浏览器实例
        browser.close()

        playwright.stop()
