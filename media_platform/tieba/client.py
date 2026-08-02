# -*- coding: utf-8 -*-
# Copyright (c) 2025 relakkes@gmail.com
import os
# MediaCrawler project
# GitHub: https://github.com/NanmiCoder
# Licensed under NON-COMMERCIAL LEARNING LICENSE 1.1
#

# 声明：本代码仅供学习和研究目的使用。使用者应遵守以下原则：
# 1. 不得用于任何商业用途。
# 2. 使用时应遵守目标平台的使用条款和robots.txt规则。
# 3. 不得进行大规模爬取或对平台造成运营干扰。
# 4. 应合理控制请求频率，避免给目标平台带来不必要的负担。
# 5. 不得用于任何非法或不当的用途。
#
# 详细许可条款请参阅项目根目录下的LICENSE文件。
# 使用本代码即表示您同意遵守上述原则和LICENSE中的所有条款。

import asyncio
import hashlib
import html
import json
import re
from typing import Any, Callable, Dict, List, Optional, Union
from urllib.parse import urlencode, quote, parse_qs, unquote, urlparse

import requests
from playwright.async_api import BrowserContext, Page
from tenacity import RetryError, retry, stop_after_attempt, wait_fixed

import config
from base.base_crawler import AbstractApiClient
from model.m_baidu_tieba import (
    TiebaComment,
    TiebaCreator,
    TiebaNote,
    parse_reply_count,
)
from proxy.proxy_ip_pool import ProxyIpPool
from tools import utils
from tools.user_hash import anonymize_user_id

from .field import SearchNoteType, SearchSortType
from .help import TieBaExtractor

PC_SIGN_SECRET = os.getenv("TIEBA_SIGN_SECRET", "")


class BaiduTieBaClient(AbstractApiClient):

    def __init__(
        self,
        timeout=10,
        ip_pool=None,
        default_ip_proxy=None,
        headers: Dict[str, str] = None,
        playwright_page: Optional[Page] = None,
    ):
        self.ip_pool: Optional[ProxyIpPool] = ip_pool
        self.timeout = timeout
        # Use provided headers (including real browser UA) or default headers
        self.headers = headers or {
            "User-Agent": utils.get_user_agent(),
            "Cookie": "",
        }
        self._host = "https://tieba.baidu.com"
        self.cookie_urls = [self._host]
        self._page_extractor = TieBaExtractor()
        self.default_ip_proxy = default_ip_proxy
        self.playwright_page = playwright_page  # Playwright page object
        self._pc_tbs = ""

    @staticmethod
    def _sign_pc_params(params: Dict[str, Any]) -> str:
        sign_text = ""
        for key in sorted(params):
            if key in {"sign", "sig"} or params[key] is None:
                continue
            sign_text += f"{key}={params[key]}"
        sign_text += PC_SIGN_SECRET
        return hashlib.md5(sign_text.encode("utf-8")).hexdigest()

    async def _ensure_tieba_origin(self) -> None:
        if not self.playwright_page:
            raise Exception("playwright_page is required for tieba PC API requests")
        if not self.playwright_page.url.startswith(self._host):
            await self.playwright_page.goto(self._host, wait_until="domcontentloaded")

    async def _fetch_json_by_browser(
        self,
        uri: str,
        method: str = "GET",
        params: Optional[Dict[str, Any]] = None,
        data: Optional[Dict[str, Any]] = None,
        use_sign: bool = False,
    ) -> Dict:
        """
        Fetch current Tieba PC JSON APIs from the browser context.
        These APIs rely on logged-in browser cookies and Baidu's PC signing
        convention, while Python requests can be blocked by local proxy/TLS.
        """
        await self._ensure_tieba_origin()
        params = {k: v for k, v in (params or {}).items() if v is not None}
        data = {k: v for k, v in (data or {}).items() if v is not None}
        if use_sign:
            sign_source = data if method.upper() == "POST" else params
            sign_source.setdefault("subapp_type", "pc")
            sign_source.setdefault("_client_type", "20")
            # 尝试从浏览器页面提取签名密钥，否则回退到 Python 计算
            try:
                sign_secret = await self.playwright_page.evaluate(
                    """() => {
                        return window.__TIEBA_SIGN_SECRET__ || 
                               window.PageData?.sign_secret || 
                               window._sign_secret || "";
                    }"""
                )
            except Exception:
                sign_secret = ""
            
            if sign_secret:
                import hashlib as _hl
                sign_text = ""
                for key in sorted(sign_source):
                    if key in {"sign", "sig"} or sign_source[key] is None:
                        continue
                    sign_text += f"{key}={sign_source[key]}"
                sign_text += sign_secret
                sign_source["sign"] = _hl.md5(sign_text.encode("utf-8")).hexdigest()
            else:
                # 回退到 Python 计算（PC_SIGN_SECRET 为空时 sign 无效，但请求仍发出去）
                sign_source["sign"] = self._sign_pc_params(sign_source)

        url = f"{self._host}{uri}"
        if params:
            url = f"{url}?{urlencode(params)}"
        body = urlencode(data) if data else ""
        response = await self.playwright_page.evaluate(
            """async ({ url, method, body }) => {
                const headers = { "Accept": "application/json, text/plain, */*" };
                const options = { method, credentials: "include", headers };
                if (method === "POST") {
                    headers["Content-Type"] = "application/x-www-form-urlencoded;charset=UTF-8";
                    options.body = body;
                }
                const resp = await fetch(url, options);
                const text = await resp.text();
                return { status: resp.status, text };
            }""",
            {"url": url, "method": method.upper(), "body": body},
        )
        if response["status"] != 200:
            raise Exception(f"Tieba PC API failed, status={response['status']}, url={url}")
        try:
            json_data = json.loads(response["text"])
        except json.JSONDecodeError as exc:
            raise Exception(f"Tieba PC API returned non-JSON, url={url}, body={response['text'][:500]}") from exc
        error_code = json_data.get("error_code", json_data.get("no", 0))
        if str(error_code) not in {"0", "None"}:
            raise Exception(f"Tieba PC API error, url={url}, response={json_data}")
        return json_data

    async def _get_pc_tbs(self) -> str:
        if self._pc_tbs:
            return self._pc_tbs
        # 先尝试无签名请求（手机版 API 不需要 PC 签名）
        try:
            sync_data = await self._fetch_json_by_browser(
                "/c/s/pc/sync",
                params={"subapp_type": "pc", "_client_type": "20"},
                use_sign=False,  # 无签名试一次
            )
        except Exception:
            # 回退：带签名再试
            sync_data = await self._fetch_json_by_browser(
                "/c/s/pc/sync",
                params={"subapp_type": "pc", "_client_type": "20"},
                use_sign=True,
            )
        self._pc_tbs = (
            sync_data.get("data", {})
            .get("anti", {})
            .get("tbs", "")
        )
        if not self._pc_tbs:
            raise Exception(f"Can not get Tieba tbs from pc sync API: {sync_data}")
        return self._pc_tbs

    @staticmethod
    def _extract_creator_portrait(creator_url: str) -> str:
        creator_url = (creator_url or "").strip()
        if not creator_url:
            return ""
        if not creator_url.startswith(("http://", "https://")):
            return creator_url.split("?")[0]
        parsed = urlparse(creator_url)
        query = parse_qs(parsed.query)
        portrait = (
            query.get("id", [""])[0]
            or query.get("portrait", [""])[0]
            or query.get("un", [""])[0]
        )
        return unquote(portrait).split("?")[0]

    def _sync_request(self, method, url, proxy=None, **kwargs):
        """
        Synchronous requests method
        Args:
            method: Request method
            url: Request URL
            proxy: Proxy IP
            **kwargs: Other request parameters

        Returns:
            Response object
        """
        # Construct proxy dictionary
        proxies = None
        if proxy:
            proxies = {
                "http": proxy,
                "https": proxy,
            }

        # Send request
        response = requests.request(
            method=method,
            url=url,
            headers=self.headers,
            proxies=proxies,
            timeout=self.timeout,
            **kwargs
        )
        return response

    async def _refresh_proxy_if_expired(self) -> None:
        """
        Check if proxy is expired and automatically refresh if necessary
        """
        if self.ip_pool is None:
            return

        if self.ip_pool.is_current_proxy_expired():
            utils.logger.info(
                "[BaiduTieBaClient._refresh_proxy_if_expired] Proxy expired, refreshing..."
            )
            new_proxy = await self.ip_pool.get_or_refresh_proxy()
            # Update proxy URL
            _, self.default_ip_proxy = utils.format_proxy_info(new_proxy)
            utils.logger.info(
                f"[BaiduTieBaClient._refresh_proxy_if_expired] New proxy: {new_proxy.ip}:{new_proxy.port}"
            )

    @retry(stop=stop_after_attempt(3), wait=wait_fixed(1))
    async def request(self, method, url, return_ori_content=False, proxy=None, **kwargs) -> Union[str, Any]:
        """
        Common request method wrapper for requests, handles request responses
        Args:
            method: Request method
            url: Request URL
            return_ori_content: Whether to return original content
            proxy: Proxy IP
            **kwargs: Other request parameters, such as headers, request body, etc.

        Returns:

        """
        # Check if proxy is expired before each request
        await self._refresh_proxy_if_expired()

        actual_proxy = proxy if proxy else self.default_ip_proxy

        # Execute synchronous requests in thread pool
        response = await asyncio.to_thread(
            self._sync_request,
            method,
            url,
            actual_proxy,
            **kwargs
        )

        if response.status_code != 200:
            utils.logger.error(f"Request failed, method: {method}, url: {url}, status code: {response.status_code}")
            utils.logger.error(f"Request failed, response: {response.text}")
            raise Exception(f"Request failed, method: {method}, url: {url}, status code: {response.status_code}")

        if response.text == "" or response.text == "blocked":
            utils.logger.error(f"request params incorrect, response.text: {response.text}")
            raise Exception("account blocked")

        if return_ori_content:
            return response.text

        return response.json()

    async def get(self, uri: str, params=None, return_ori_content=False, **kwargs) -> Any:
        """
        GET request with header signing
        Args:
            uri: Request route
            params: Request parameters
            return_ori_content: Whether to return original content

        Returns:

        """
        final_uri = uri
        if isinstance(params, dict):
            final_uri = (f"{uri}?"
                         f"{urlencode(params)}")
        try:
            res = await self.request(method="GET", url=f"{self._host}{final_uri}", return_ori_content=return_ori_content, **kwargs)
            return res
        except RetryError as e:
            if self.ip_pool:
                proxie_model = await self.ip_pool.get_proxy()
                _, proxy = utils.format_proxy_info(proxie_model)
                res = await self.request(method="GET", url=f"{self._host}{final_uri}", return_ori_content=return_ori_content, proxy=proxy, **kwargs)
                self.default_ip_proxy = proxy
                return res

            utils.logger.error(f"[BaiduTieBaClient.get] Reached maximum retry attempts, IP is blocked, please try a new IP proxy: {e}")
            raise Exception(f"[BaiduTieBaClient.get] Reached maximum retry attempts, IP is blocked, please try a new IP proxy: {e}")

    async def post(self, uri: str, data: dict, **kwargs) -> Dict:
        """
        POST request with header signing
        Args:
            uri: Request route
            data: Request body parameters

        Returns:

        """
        json_str = json.dumps(data, separators=(',', ':'), ensure_ascii=False)
        return await self.request(method="POST", url=f"{self._host}{uri}", data=json_str, **kwargs)

    async def pong(self, browser_context: BrowserContext = None) -> bool:
        """
        Check if login state is still valid
        Uses Cookie detection instead of API calls to avoid detection
        Args:
            browser_context: Browser context object

        Returns:
            bool: True if logged in, False if not logged in
        """
        utils.logger.info("[BaiduTieBaClient.pong] Begin to check tieba login state by cookies...")

        if not browser_context:
            utils.logger.warning("[BaiduTieBaClient.pong] browser_context is None, assume not logged in")
            return False

        try:
            # Get cookies from browser and check key login cookies
            _, cookie_dict = await utils.convert_browser_context_cookies(
                browser_context,
                urls=self.cookie_urls,
            )

            # Baidu Tieba login identifiers: STOKEN or PTOKEN
            stoken = cookie_dict.get("STOKEN")
            ptoken = cookie_dict.get("PTOKEN")
            bduss = cookie_dict.get("BDUSS")  # Baidu universal login cookie

            if stoken or ptoken or bduss:
                utils.logger.info(f"[BaiduTieBaClient.pong] Login state verified by cookies (STOKEN: {bool(stoken)}, PTOKEN: {bool(ptoken)}, BDUSS: {bool(bduss)})")
                return True
            else:
                utils.logger.info("[BaiduTieBaClient.pong] No valid login cookies found, need to login")
                return False

        except Exception as e:
            utils.logger.error(f"[BaiduTieBaClient.pong] Check login state failed: {e}, assume not logged in")
            return False

    async def update_cookies(self, browser_context: BrowserContext, urls: Optional[list[str]] = None):
        """
        Update cookies method provided by API client, usually called after successful login
        Args:
            browser_context: Browser context object

        Returns:

        """
        cookie_str, cookie_dict = await utils.convert_browser_context_cookies(
            browser_context,
            urls=urls or self.cookie_urls,
        )
        self.headers["Cookie"] = cookie_str
        utils.logger.info("[BaiduTieBaClient.update_cookies] Cookie has been updated")

    async def get_notes_by_keyword(
        self,
        keyword: str,
        page: int = 1,
        page_size: int = 10,
        sort: SearchSortType = SearchSortType.TIME_DESC,
        note_type: SearchNoteType = SearchNoteType.FIXED_THREAD,
    ) -> List[TiebaNote]:
        """
        Search Tieba posts by keyword (uses Playwright to access page, avoiding API detection)
        Args:
            keyword: Keyword
            page: Page number
            page_size: Page size
            sort: Result sort method
            note_type: Post type (main thread | main thread + reply mixed mode)
        Returns:

        """
        if not self.playwright_page:
            utils.logger.error("[BaiduTieBaClient.get_notes_by_keyword] playwright_page is None, cannot use browser mode")
            raise Exception("playwright_page is required for browser-based search")

        params = {
            "rn": max(page_size, 20),
            "st": sort.value,
            "word": keyword,
            "needbrand": 1,
            "sug_type": 2,
            "pn": page,
            "come_from": "search",
            "subapp_type": "pc",
            "_client_type": "20",
        }
        utils.logger.info(
            f"[BaiduTieBaClient.get_notes_by_keyword] Accessing search API: "
            f"{self._host}/mo/q/search/multsearch?{urlencode(params)}"
        )

        try:
            api_data = await self._fetch_json_by_browser(
                "/mo/q/search/multsearch",
                params=params,
                use_sign=True,
            )
            notes = self._page_extractor.extract_search_note_list_from_api(api_data)[:page_size]
            utils.logger.info(f"[BaiduTieBaClient.get_notes_by_keyword] Extracted {len(notes)} posts")
            return notes

        except Exception as e:
            utils.logger.error(f"[BaiduTieBaClient.get_notes_by_keyword] Search failed: {e}")
            raise

    async def get_note_by_id(self, note_id: str) -> TiebaNote:
        """
        Get post details by post ID (uses browser navigation).

        The legacy PC JSON API chain (/c/s/pc/sync -> /c/f/pb/page_pc) requires a
        `sign` secret that is no longer shipped in the current tieba web frontend
        (verified 2026-07: no-sign / garbage-sign / mobile-secret all return
        error_code=110001). Browser navigation + in-page JS/DOM extraction is the
        only reliable path and avoids the dead API round-trips.
        Args:
            note_id: Post ID

        Returns:
            TiebaNote: Post detail object
        """
        if not self.playwright_page:
            utils.logger.error("[BaiduTieBaClient.get_note_by_id] playwright_page is None, cannot use browser mode")
            raise Exception("playwright_page is required for browser-based note detail fetching")

        utils.logger.info(f"[BaiduTieBaClient.get_note_by_id] Browser fetching post page, note_id: {note_id}")

        try:
            post_url = f"{self._host}/p/{note_id}"
            await self.playwright_page.goto(post_url, wait_until="domcontentloaded", timeout=15000)
            await asyncio.sleep(2)
            page_html = await self.playwright_page.content()

            # Detect captcha page and retry once
            if "百度安全验证" in page_html or "captcha" in page_html.lower():
                utils.logger.warning(f"[BaiduTieBaClient.get_note_by_id] Captcha page detected for note {note_id}, waiting 10s and retrying...")
                await asyncio.sleep(10)
                await self.playwright_page.goto(post_url, wait_until="domcontentloaded", timeout=15000)
                await asyncio.sleep(3)
                page_html = await self.playwright_page.content()

            # Extract from page's embedded JS data first, fallback to DOM selectors
            note_detail = await self._extract_note_from_page(note_id, page_html)
            return note_detail

        except Exception as e:
            utils.logger.error(f"[BaiduTieBaClient.get_note_by_id] Failed to get post details: {e}")
            raise

    async def _extract_note_from_page(self, note_id: str, page_html: str):
        """Extract note detail from page HTML and embedded JS data."""
        
        # Comprehensive JS data extraction from page context
        js_data = await self.playwright_page.evaluate("""() => {
            const dom = {};
            
            // Title from h1 or title tag
            const titleEl = document.querySelector('h1[class*="title"], .core_title_txt, title');
            if (titleEl) dom.title = (titleEl.textContent || '').trim();
            
            // First post - get ALL data-field elements, pick first one
            const postDivs = document.querySelectorAll('[data-field]');
            let firstPostDiv = null;
            for (const div of postDivs) {
                const field = div.getAttribute('data-field');
                if (field) {
                    try {
                        const data = JSON.parse(field);
                        if (data.content) {
                            firstPostDiv = div;
                            dom.first_post_data = data;
                            // Extract author from data-field JSON
                            const author = data.author || {};
                            dom.author_name = author.user_name || author.name_show || author.name || '';
                            dom.author_id = author.user_id || author.portrait || '';
                            // Extract timestamp from data-field
                            dom.time_unix = data.time || 0;
                            dom.time_date = data.date || '';
                            // Extract post content from data-field
                            if (typeof data.content === 'string') {
                                dom.post_text = data.content.substring(0, 3000);
                            } else if (data.content && data.content.text) {
                                dom.post_text = data.content.text.substring(0, 3000);
                            }
                            break;
                        }
                    } catch(e) {}
                }
            }
            
            // Fallback first post text from DOM (wider selectors that worked in v3)
            if (!dom.post_text) {
                const contentEls = document.querySelectorAll('.d_post_content, [class*="post_content"], [class*="content"]');
                if (contentEls.length > 0) {
                    // Take only first content element
                    const clone = contentEls[0].cloneNode(true);
                    // Remove nested reply elements
                    clone.querySelectorAll('[class*="lzl"], [class*="comment"], [class*="reply_"]').forEach(el => el.remove());
                    dom.first_post_text = (clone.textContent || '').trim().substring(0, 3000);
                }
            }
            
            // Forum/Ba name (wider selectors from v3 that worked)
            const forumEl = document.querySelector('[class*="ba_name"], [class*="forum"], [class*="Forum"], .card_title_fname');
            if (forumEl) dom.forum_name = (forumEl.textContent || '').trim();
            
            // Reply count (wider selectors from v3 that worked)
            const replyEl = document.querySelector('[class*="reply"], [class*="Reply"], .l_reply_num, [class*="post_num"]');
            if (replyEl) {
                const replyNums = (replyEl.textContent || '').match(/\\d+/g);
                if (replyNums) dom.reply_count = parseInt(replyNums[0]);
            }
            
            // Author from DOM (if not found in data-field)
            if (!dom.author_name) {
                const authorEl = document.querySelector('[class*="author"], [class*="Author"], .p_author_name');
                if (authorEl) dom.author_name = (authorEl.getAttribute('data-username') || authorEl.textContent || '').trim();
            }
            
            // Time - look in the first post area for date patterns
            if (!dom.time_unix && firstPostDiv) {
                const tailInfos = firstPostDiv.querySelectorAll('[class*="tail"]');
                tailInfos.forEach(el => {
                    const t = (el.textContent || '').trim();
                    if (/\\d{4}[-/]\\d{2}[-/]\\d{2}/.test(t) || /\\d{2}:\\d{2}/.test(t)) {
                        dom.time_text = t;
                    }
                });
            }
            
            return dom;
        }""")
        
        utils.logger.info(f"[_extract_note_from_page] js_data type: {type(js_data).__name__}, "
                         f"keys: {list((js_data or {}).keys()) if isinstance(js_data, dict) else 'N/A'}")
        
        # First try the old HTML extractor; never let a parser failure discard the
        # JS-extracted data we already have above.
        try:
            note_detail = self._page_extractor.extract_note_detail(page_html)
        except Exception as html_e:
            utils.logger.warning(
                f"[_extract_note_from_page] HTML extractor failed ({html_e}); "
                f"building note from JS-extracted data only"
            )
            note_detail = TiebaNote(
                note_id=note_id,
                title="",
                note_url=f"https://tieba.baidu.com/p/{note_id}",
                tieba_name="",
                tieba_link="https://tieba.baidu.com",
            )
        
        # Override with JS data as primary source
        if isinstance(js_data, dict):
            # Handle wrapped format {dom: ..., full_data: ...}
            dom = js_data.get("dom") or js_data  # both flat and wrapped
            
            if "full_data" in js_data and js_data["full_data"]:
                full_data = js_data["full_data"]
                thread = full_data.get("thread") or {}
                forum = full_data.get("forum") or {}
                
                note_detail.note_id = note_id
                note_detail.note_url = f"https://tieba.baidu.com/p/{note_id}"
                note_detail.desc = html.unescape(
                    thread.get("first_post_content") or thread.get("abstract") or ""
                )[:2000]
                note_detail.publish_time = utils.get_time_str_from_unix_time(
                    thread.get("create_time") or thread.get("first_post_time") or 0
                )
                note_detail.total_replay_num = parse_reply_count(thread.get("reply_num"))
                note_detail.total_replay_page = full_data.get("page", {}).get("total_page") or 1
                
                fname = forum.get("name") or ""
                if fname:
                    note_detail.tieba_name = fname + "吧" if not fname.endswith("吧") else fname
                    note_detail.tieba_link = f"https://tieba.baidu.com/f?kw={fname}"
                
                user_map = full_data.get("user_list") or {}
                if user_map:
                    first_author = next(iter(user_map.values()), {})
                    note_detail.user_nickname = first_author.get("name_show") or first_author.get("name") or ""
                    note_detail.creator_hash = anonymize_user_id(first_author.get("id") or first_author.get("portrait") or "")
            
            # DOM-based data
            note_detail.note_id = note_id
            note_detail.note_url = f"https://tieba.baidu.com/p/{note_id}"
            
            if dom.get("title") and (not note_detail.title or '安全验证' in note_detail.title or 'Internal Server Error' in note_detail.title):
                note_detail.title = dom["title"]
            
            # Post text from data-field (clean) or DOM (broader, works)
            post_text = dom.get("post_text") or dom.get("first_post_text") or ""
            if post_text:
                note_detail.desc = post_text[:2000]
            
            if dom.get("time_text") and not note_detail.publish_time:
                note_detail.publish_time = dom["time_text"]
            if dom.get("time_date") and not note_detail.publish_time:
                note_detail.publish_time = dom["time_date"]
            if dom.get("time_unix") and not note_detail.publish_time:
                note_detail.publish_time = utils.get_time_str_from_unix_time(dom["time_unix"])
                
            if dom.get("author_name") and (not note_detail.user_nickname or note_detail.user_nickname == "*"):
                note_detail.user_nickname = dom["author_name"]
            if dom.get("author_id") and note_detail.creator_hash == "7291ca9e236ddafc":
                note_detail.creator_hash = anonymize_user_id(dom["author_id"])
            if dom.get("forum_name") and not note_detail.tieba_name:
                fname = dom["forum_name"]
                note_detail.tieba_name = fname + "吧" if not fname.endswith("吧") else fname
                note_detail.tieba_link = f"https://tieba.baidu.com/f?kw={fname}"
            if dom.get("reply_count") and not note_detail.total_replay_num:
                note_detail.total_replay_num = parse_reply_count(dom["reply_count"])
            
            # Parse data-field JSON for structured data (more precise)
            fp = dom.get("first_post_data")
            if fp and isinstance(fp, dict):
                if not note_detail.publish_time and fp.get("date"):
                    note_detail.publish_time = fp["date"]
                elif not note_detail.publish_time:
                    note_detail.publish_time = utils.get_time_str_from_unix_time(fp.get("time") or 0)
                if (not note_detail.user_nickname or note_detail.user_nickname == "*") and fp.get("author"):
                    author = fp["author"]
                    note_detail.user_nickname = author.get("user_name") or author.get("name_show") or ""
                    note_detail.creator_hash = anonymize_user_id(author.get("user_id") or author.get("portrait") or "")
                if not note_detail.desc and fp.get("content"):
                    content = fp["content"]
                    note_detail.desc = (content if isinstance(content, str) else content.get("text", ""))[:2000]
        
        # Final fallback: ensure minimum required fields from params
        if not note_detail.note_id:
            note_detail.note_id = note_id
        if not note_detail.note_url or note_detail.note_url.endswith("/"):
            note_detail.note_url = f"https://tieba.baidu.com/p/{note_id}"
        if not note_detail.total_replay_page:
            note_detail.total_replay_page = 1  # Always try page 1
        
        # Post-process: clean desc and extract missing fields from desc text
        # (never let a post-processing failure kill an already-extracted note)
        try:
            self._post_process_note(note_detail)
        except Exception as pp_e:
            utils.logger.warning(f"[_extract_note_from_page] Post-process note failed: {pp_e}")
            
        utils.logger.info(
            f"[_extract_note_from_page] Extracted - title: {note_detail.title[:40] if note_detail.title else 'EMPTY'}, "
            f"desc: {len(note_detail.desc or '')} chars, time: {note_detail.publish_time or 'EMPTY'}, "
            f"author: {note_detail.user_nickname or 'EMPTY'}, forum: {note_detail.tieba_name or 'EMPTY'}, "
            f"replies: {note_detail.total_replay_num}"
        )
        return note_detail

    def _post_process_note(self, note_detail: TiebaNote):
        """Clean desc and extract missing fields from desc text."""
        if not note_detail.desc:
            return
        
        desc = note_detail.desc
        
        # Remove navigation/sidebar boilerplate
        desc = re.sub(r'首页\s+我的\s+我常逛的吧.*?展开全部', '', desc, flags=re.DOTALL)
        desc = re.sub(r'我玩过的游戏\s+.*?热门游戏\s+折叠导航栏', '', desc, flags=re.DOTALL)
        desc = re.sub(r'全部回复\s*\(\d+\).*?去APP回复经验更多', '', desc, flags=re.DOTALL)
        desc = re.sub(r'进吧看看.*$', '', desc, flags=re.DOTALL)
        desc = re.sub(r'百度版权声明.*$', '', desc, flags=re.DOTALL)
        desc = re.sub(r'\S+吧\s+关注[\d.]+[W万K千].*$', '', desc)
        
        # Extract publish time from text patterns like "07-25 山东" or "2025-09-23"
        if not note_detail.publish_time:
            time_patterns = [
                r'(\d{4}-\d{2}-\d{2}\s*\d{2}:\d{2})',  # 2025-09-23 14:30
                r'(\d{2}-\d{2})\s+\S+',  # 07-25 山东
                r'(\d{4}-\d{2}-\d{2})',  # 2025-09-23
                r'(\d{4}年\d{1,2}月\d{1,2}日)',  # 2025年9月23日
            ]
            for pattern in time_patterns:
                match = re.search(pattern, desc)
                if match:
                    note_detail.publish_time = match.group(1).strip()
                    break
        
        # Extract author name from text (appears before post title/date)
        if not note_detail.user_nickname or note_detail.user_nickname == "*":
            # P2 fix: the raw page text starts with navigation boilerplate
            # ("首页 我的 我常逛的吧…折叠导航栏"), which a loose regex mistakes for
            # an author block (10 条里 9 条 user_nickname 被提取成整段导航文本).
            # Three guards:
            #   1) search only the content region AFTER the navigation markers;
            #   2) the name class contains NO spaces, so it cannot span the
            #      space-separated navigation tokens ("XXX 吧 YYY 吧 …");
            #   3) sanity-check the extracted name (short, no nav words).
            content_desc = desc
            for nav_marker in ("折叠导航栏", "热门游戏"):
                marker_idx = content_desc.rfind(nav_marker)
                if marker_idx != -1:
                    content_desc = content_desc[marker_idx + len(nav_marker):]
            author_match = re.search(
                r'((?:贴吧用户_\w+|[\u4e00-\u9fff\w·◎☜🌙🍺🔥🌈]+?))\s*(?:贴吧(?:SVIP|VIP|成长等级)|吧主|小吧主|楼主)',
                content_desc
            )
            if author_match:
                name = author_match.group(1).strip()
                nav_words = ("首页", "我的", "折叠", "导航", "登录", "注册", "我常逛的", "大家都在逛的", "热门游戏", "展开", "进吧", "去APP")
                if name and len(name) <= 20 and not any(word in name for word in nav_words):
                    note_detail.user_nickname = name
                    note_detail.creator_hash = anonymize_user_id(name)
        
        # Trim desc
        desc = desc.strip()
        if len(desc) > 2000:
            # Try to find a natural break point
            cut = desc.rfind('。', 1000, 2000)
            if cut < 1000:
                cut = desc.rfind('\n', 1000, 2000)
            note_detail.desc = desc[:cut if cut > 500 else 2000]
        else:
            note_detail.desc = desc

    async def get_note_all_comments(
        self,
        note_detail: TiebaNote,
        crawl_interval: float = 1.0,
        callback: Optional[Callable] = None,
        max_count: int = 10,
    ) -> List[TiebaComment]:
        """
        Get all first-level comments by intercepting the page_pc API response.

        Strategy: Navigate to the post page, let the page's own JavaScript call
        c/f/pb/page_pc (with correct signing), capture the response JSON,
        and extract post_list from it. This bypasses the 110001 signature error.
        """
        if not self.playwright_page:
            utils.logger.error("[BaiduTieBaClient.get_note_all_comments] playwright_page is None")
            raise Exception("playwright_page is required for browser-based comment fetching")

        result: List[TiebaComment] = []
        current_page = 1
        max_pages = max(note_detail.total_replay_page or 1, 1)

        while current_page <= max_pages and len(result) < max_count:
            utils.logger.info(
                f"[get_note_all_comments] page_pc interception, "
                f"note_id: {note_detail.note_id}, page: {current_page}"
            )

            try:
                api_response_data = {}

                async def capture_page_pc(response):
                    url = response.url
                    if "/c/f/pb/page_pc" in url and response.status == 200:
                        try:
                            body = await response.text()
                            data = json.loads(body)
                            if data.get("post_list") and len(data.get("post_list", [])) > 0:
                                # Only capture first valid response per page
                                if "data" not in api_response_data:
                                    api_response_data["data"] = data
                                    utils.logger.info(
                                        f"[capture_page_pc] Got {len(data['post_list'])} posts "
                                        f"(page {data.get('page', {}).get('current_page', '?')})"
                                    )
                        except Exception:
                            pass

                self.playwright_page.on("response", capture_page_pc)

                try:
                    post_url = f"{self._host}/p/{note_detail.note_id}?pn={current_page}"
                    await self.playwright_page.goto(post_url, wait_until="domcontentloaded", timeout=20000)

                    # Wait for page_pc response
                    for _ in range(20):
                        if "data" in api_response_data:
                            break
                        await asyncio.sleep(0.5)

                    if "data" not in api_response_data:
                        await self.playwright_page.evaluate(
                            "window.scrollTo(0, document.body.scrollHeight)"
                        )
                        await asyncio.sleep(3)
                finally:
                    self.playwright_page.remove_listener("response", capture_page_pc)

                if "data" not in api_response_data:
                    utils.logger.warning("[get_note_all_comments] No page_pc response captured")
                    break

                api_data = api_response_data["data"]
                post_list = api_data.get("post_list", [])
                page_info = api_data.get("page", {})

                utils.logger.info(
                    f"[get_note_all_comments] post_list length: {len(post_list)}, "
                    f"page {page_info.get('current_page', '?')}/{page_info.get('total_page', '?')}"
                )

                if len(post_list) <= 1:
                    break  # Only OP, no comments

                actual_total_page = page_info.get("total_page", 0)
                if actual_total_page > max_pages:
                    max_pages = min(actual_total_page, 20)

                user_map = {
                    str(u.get("id", "")): u
                    for u in api_data.get("user_list", [])
                    if u.get("id")
                }

                forum = api_data.get("forum") or api_data.get("display_forum") or {}
                tieba_name = note_detail.tieba_name
                if not tieba_name and forum.get("name"):
                    tieba_name = forum.get("name")
                    if not tieba_name.endswith("吧"):
                        tieba_name += "吧"

                # Update note_detail with thread-level data from page_pc API
                thread = api_data.get("thread") or {}
                if not note_detail.total_replay_num and thread.get("reply_num"):
                    note_detail.total_replay_num = parse_reply_count(thread["reply_num"])
                if not note_detail.total_replay_page and page_info.get("total_page"):
                    note_detail.total_replay_page = page_info["total_page"]
                if thread.get("create_time") and not note_detail.create_time_unix:
                    note_detail.create_time_unix = thread["create_time"]
                    if not note_detail.publish_time or "-" not in (note_detail.publish_time or ""):
                        note_detail.publish_time = utils.get_time_str_from_unix_time(thread["create_time"])
                if thread.get("share_num"):
                    note_detail.share_num = thread["share_num"]
                if thread.get("agree_num"):
                    note_detail.agree_num = thread["agree_num"]
                if forum.get("first_class"):
                    note_detail.forum_first_class = forum["first_class"]
                if forum.get("second_class"):
                    note_detail.forum_second_class = forum["second_class"]

                comments = []
                for item in post_list[1:]:
                    if len(comments) + len(result) >= max_count:
                        break

                    comment_id = str(item.get("id", ""))
                    if not comment_id:
                        continue

                    content = item.get("content", "")
                    if isinstance(content, list):
                        content = "".join(
                            c.get("text", "") for c in content if isinstance(c, dict)
                        )
                    content = (content or "").strip()
                    if not content:
                        continue

                    author_id = str(item.get("author_id", ""))
                    author = user_map.get(author_id, {})
                    author_name = author.get("name_show") or author.get("name") or "*"

                    comment = TiebaComment(
                        comment_id=comment_id,
                        content=content[:2000],
                        note_url=note_detail.note_url,
                        creator_hash=anonymize_user_id(
                            author.get("portrait") or author_id
                        ),
                        user_nickname=author_name,
                        tieba_id=str(forum.get("id", "")),
                        tieba_name=tieba_name,
                        tieba_link=note_detail.tieba_link or (
                            f"https://tieba.baidu.com/f?kw={tieba_name}"
                            if tieba_name else ""
                        ),
                        publish_time=utils.get_time_str_from_unix_time(
                            item.get("time", 0)
                        ),
                        note_id=note_detail.note_id,
                        sub_comment_count=item.get("sub_post_number", 0),
                        # New fields from page_pc API
                        floor=item.get("floor", 0),
                        agree_num=item.get("agree", {}).get("agree_num", 0) if isinstance(item.get("agree"), dict) else 0,
                        author_level_name=author.get("level_name", ""),
                        author_ip_address=author.get("ip_address", ""),
                        author_gender=author.get("gender", 0),
                        author_is_bawu=author.get("is_bawu", 0),
                    )
                    comments.append(comment)

                if not comments:
                    utils.logger.info(f"[get_note_all_comments] No comments on page {current_page}, stopping")
                    break

                if callback:
                    await callback(note_detail.note_id, comments)

                result.extend(comments)
                utils.logger.info(
                    f"[get_note_all_comments] Added {len(comments)} comments from page {current_page}"
                )

                # Get sub-comments
                await self.get_comments_all_sub_comments(
                    comments, crawl_interval=crawl_interval, callback=callback
                )

                await asyncio.sleep(crawl_interval)
                current_page += 1

            except Exception as e:
                utils.logger.error(
                    f"[get_note_all_comments] Error on page {current_page}: {e}"
                )
                break

        utils.logger.info(
            f"[get_note_all_comments] Done: {len(result)} comments total"
        )
        return result

    async def get_comments_all_sub_comments(
        self,
        comments: List[TiebaComment],
        crawl_interval: float = 1.0,
        callback: Optional[Callable] = None,
    ) -> List[TiebaComment]:
        """
        Get all sub-comments for specified comments (uses Playwright to access page, avoiding API detection)
        Args:
            comments: Comment list
            crawl_interval: Crawl delay interval in seconds
            callback: Callback function after one post crawl completes

        Returns:
            List[TiebaComment]: Sub-comment list
        """
        if not config.ENABLE_GET_SUB_COMMENTS:
            return []

        if not self.playwright_page:
            utils.logger.error("[BaiduTieBaClient.get_comments_all_sub_comments] playwright_page is None, cannot use browser mode")
            raise Exception("playwright_page is required for browser-based sub-comment fetching")

        all_sub_comments: List[TiebaComment] = []

        for parment_comment in comments:
            if parment_comment.sub_comment_count == 0:
                continue

            current_page = 1
            max_sub_page_num = parment_comment.sub_comment_count // 10 + 1

            while max_sub_page_num >= current_page:
                # Construct sub-comment URL
                sub_comment_url = (
                    f"{self._host}/p/comment?"
                    f"tid={parment_comment.note_id}&"
                    f"pid={parment_comment.comment_id}&"
                    f"fid={parment_comment.tieba_id}&"
                    f"pn={current_page}"
                )
                utils.logger.info(f"[BaiduTieBaClient.get_comments_all_sub_comments] Accessing sub-comment page: {sub_comment_url}")

                try:
                    # Use Playwright to access sub-comment page
                    await self.playwright_page.goto(sub_comment_url, wait_until="domcontentloaded")

                    # Wait for page loading, using delay setting from config file
                    await asyncio.sleep(config.CRAWLER_MAX_SLEEP_SEC)

                    # Get page HTML content
                    page_content = await self.playwright_page.content()

                    # Extract sub-comments
                    sub_comments = self._page_extractor.extract_tieba_note_sub_comments(
                        page_content, parent_comment=parment_comment
                    )

                    if not sub_comments:
                        utils.logger.info(
                            f"[BaiduTieBaClient.get_comments_all_sub_comments] "
                            f"Comment {parment_comment.comment_id} page {current_page} has no sub-comments, stopping crawl"
                        )
                        break

                    if callback:
                        await callback(parment_comment.note_id, sub_comments)

                    all_sub_comments.extend(sub_comments)
                    await asyncio.sleep(crawl_interval)
                    current_page += 1

                except Exception as e:
                    utils.logger.error(
                        f"[BaiduTieBaClient.get_comments_all_sub_comments] "
                        f"Failed to get comment {parment_comment.comment_id} page {current_page} sub-comments: {e}"
                    )
                    break

        utils.logger.info(f"[BaiduTieBaClient.get_comments_all_sub_comments] Total retrieved {len(all_sub_comments)} sub-comments")
        return all_sub_comments

    async def get_notes_by_tieba_name(self, tieba_name: str, page_num: int) -> List[TiebaNote]:
        """
        Get post list by Tieba name from current PC forum JSON API.
        Args:
            tieba_name: Tieba name
            page_num: Page number

        Returns:
            List[TiebaNote]: Post list
        """
        if not self.playwright_page:
            utils.logger.error("[BaiduTieBaClient.get_notes_by_tieba_name] playwright_page is None, cannot use browser mode")
            raise Exception("playwright_page is required for browser-based tieba note fetching")

        page_size = 30
        api_page = page_num // page_size + 1
        tbs = await self._get_pc_tbs()
        utils.logger.info(
            f"[BaiduTieBaClient.get_notes_by_tieba_name] Accessing Tieba FRS API, "
            f"tieba_name: {tieba_name}, page: {api_page}"
        )

        try:
            api_data = await self._fetch_json_by_browser(
                "/c/f/frs/page_pc",
                method="POST",
                data={
                    "kw": quote(tieba_name),
                    "pn": api_page,
                    "sort_type": -1,
                    "is_newfrs": 1,
                    "is_newfeed": 1,
                    "rn": page_size,
                    "rn_need": 10,
                    "tbs": tbs,
                    "subapp_type": "pc",
                    "_client_type": "20",
                },
                use_sign=True,
            )
            notes = self._page_extractor.extract_tieba_note_list_from_frs_api(api_data)[:page_size]
            utils.logger.info(f"[BaiduTieBaClient.get_notes_by_tieba_name] Extracted {len(notes)} posts")
            return notes

        except Exception as e:
            utils.logger.error(f"[BaiduTieBaClient.get_notes_by_tieba_name] Failed to get Tieba post list: {e}")
            raise

    async def get_creator_info_by_url(self, creator_url: str) -> TiebaCreator:
        """
        Get creator information by creator URL from current PC JSON API.
        Args:
            creator_url: Creator homepage URL

        Returns:
            TiebaCreator: Creator information
        """
        if not self.playwright_page:
            utils.logger.error("[BaiduTieBaClient.get_creator_info_by_url] playwright_page is None, cannot use browser mode")
            raise Exception("playwright_page is required for browser-based creator info fetching")

        portrait = self._extract_creator_portrait(creator_url)
        if not portrait:
            raise Exception(f"Can not extract Tieba creator portrait from url: {creator_url}")

        utils.logger.info(
            f"[BaiduTieBaClient.get_creator_info_by_url] Accessing creator info API, portrait: {portrait}"
        )

        try:
            api_data = await self._fetch_json_by_browser(
                "/c/u/pc/homeSidebarRight",
                params={
                    "portrait": portrait,
                    "un": "",
                    "subapp_type": "pc",
                    "_client_type": "20",
                },
                use_sign=True,
            )
            return self._page_extractor.extract_creator_info_from_api(api_data)

        except Exception as e:
            utils.logger.error(f"[BaiduTieBaClient.get_creator_info_by_url] Failed to get creator info: {e}")
            raise

    async def get_notes_by_creator_portrait(
        self, portrait: str, page_number: int, page_size: int = 20
    ) -> Dict:
        """
        Get creator's thread feed by creator portrait from current PC JSON API.
        """
        if not self.playwright_page:
            utils.logger.error("[BaiduTieBaClient.get_notes_by_creator_portrait] playwright_page is None, cannot use browser mode")
            raise Exception("playwright_page is required for browser-based creator notes fetching")

        utils.logger.info(
            f"[BaiduTieBaClient.get_notes_by_creator_portrait] Accessing creator feed API, "
            f"portrait: {portrait}, page: {page_number}"
        )
        return await self._fetch_json_by_browser(
            "/c/u/feed/myThread",
            params={
                "pn": page_number,
                "rn": page_size,
                "portrait": portrait,
                "type": 1,
                "un": "",
                "subapp_type": "pc",
                "_client_type": "20",
            },
            use_sign=True,
        )

    async def get_notes_by_creator(self, user_name: str, page_number: int) -> Dict:
        """
        Get creator's posts by creator (uses Playwright to access page, avoiding API detection)
        Args:
            user_name: Creator username
            page_number: Page number

        Returns:
            Dict: Dictionary containing post data
        """
        if not self.playwright_page:
            utils.logger.error("[BaiduTieBaClient.get_notes_by_creator] playwright_page is None, cannot use browser mode")
            raise Exception("playwright_page is required for browser-based creator notes fetching")

        # Construct creator post list URL
        creator_url = f"{self._host}/home/get/getthread?un={quote(user_name)}&pn={page_number}&id=utf-8&_={utils.get_current_timestamp()}"
        utils.logger.info(f"[BaiduTieBaClient.get_notes_by_creator] Accessing creator post list: {creator_url}")

        try:
            # Use Playwright to access creator post list page
            await self.playwright_page.goto(creator_url, wait_until="domcontentloaded")

            # Wait for page loading, using delay setting from config file
            await asyncio.sleep(config.CRAWLER_MAX_SLEEP_SEC)

            # Get page content (this API returns JSON)
            page_content = await self.playwright_page.content()

            # Extract JSON data (page will contain <pre> tag or is directly JSON)
            try:
                # Try to extract JSON from page
                json_text = await self.playwright_page.evaluate("() => document.body.innerText")
                result = json.loads(json_text)
                utils.logger.info(f"[BaiduTieBaClient.get_notes_by_creator] Successfully retrieved creator post data")
                return result
            except json.JSONDecodeError as e:
                utils.logger.error(f"[BaiduTieBaClient.get_notes_by_creator] JSON parsing failed: {e}")
                utils.logger.error(f"[BaiduTieBaClient.get_notes_by_creator] Page content: {page_content[:500]}")
                raise Exception(f"Failed to parse JSON from creator notes page: {e}")

        except Exception as e:
            utils.logger.error(f"[BaiduTieBaClient.get_notes_by_creator] Failed to get creator post list: {e}")
            raise

    async def get_all_notes_by_creator_user_name(
        self,
        user_name: str,
        crawl_interval: float = 1.0,
        callback: Optional[Callable] = None,
        max_note_count: int = 0,
        creator_page_html_content: str = None,
    ) -> List[TiebaNote]:
        """
        Get all creator posts by creator username
        Args:
            user_name: Creator username
            crawl_interval: Crawl delay interval in seconds
            callback: Callback function after one post crawl completes, an awaitable function
            max_note_count: Maximum number of posts to retrieve, if 0 then get all
            creator_page_html_content: Creator homepage HTML content

        Returns:

        """
        # Baidu Tieba is special, the first 10 posts are directly displayed on the homepage and need special handling, cannot be obtained through API
        result: List[TiebaNote] = []
        if creator_page_html_content:
            thread_id_list = (self._page_extractor.extract_tieba_thread_id_list_from_creator_page(creator_page_html_content))
            utils.logger.info(f"[BaiduTieBaClient.get_all_notes_by_creator] got user_name:{user_name} thread_id_list len : {len(thread_id_list)}")
            note_detail_task = [self.get_note_by_id(thread_id) for thread_id in thread_id_list]
            notes = await asyncio.gather(*note_detail_task)
            if callback:
                await callback(notes)
            result.extend(notes)

        notes_has_more = 1
        page_number = 1
        page_per_count = 20
        total_get_count = 0
        while notes_has_more == 1 and (max_note_count == 0 or total_get_count < max_note_count):
            notes_res = await self.get_notes_by_creator(user_name, page_number)
            if not notes_res or notes_res.get("no") != 0:
                utils.logger.error(f"[TieBaClient.get_notes_by_creator] got user_name:{user_name} notes failed, notes_res: {notes_res}")
                break
            notes_data = notes_res.get("data")
            notes_has_more = notes_data.get("has_more")
            notes = notes_data["thread_list"]
            utils.logger.info(f"[TieBaClient.get_all_notes_by_creator] got user_name:{user_name} notes len : {len(notes)}")

            note_detail_task = [self.get_note_by_id(note['thread_id']) for note in notes]
            notes = await asyncio.gather(*note_detail_task)
            if callback:
                await callback(notes)
            await asyncio.sleep(crawl_interval)
            result.extend(notes)
            page_number += 1
            total_get_count += page_per_count
        return result

    async def get_all_notes_by_creator_url(
        self,
        creator_url: str,
        crawl_interval: float = 1.0,
        callback: Optional[Callable] = None,
        max_note_count: int = 0,
    ) -> List[TiebaNote]:
        """
        Get all creator posts by current PC creator feed API.
        """
        portrait = self._extract_creator_portrait(creator_url)
        if not portrait:
            raise Exception(f"Can not extract Tieba creator portrait from url: {creator_url}")

        result: List[TiebaNote] = []
        page_number = 1
        page_size = 20

        while max_note_count == 0 or len(result) < max_note_count:
            notes_res = await self.get_notes_by_creator_portrait(
                portrait=portrait,
                page_number=page_number,
                page_size=page_size,
            )
            thread_id_list = self._page_extractor.extract_creator_thread_id_list_from_api(notes_res)
            if not thread_id_list:
                utils.logger.info(
                    f"[BaiduTieBaClient.get_all_notes_by_creator_url] "
                    f"Creator portrait:{portrait} page:{page_number} has no threads"
                )
                break

            if max_note_count:
                thread_id_list = thread_id_list[: max_note_count - len(result)]

            utils.logger.info(
                f"[BaiduTieBaClient.get_all_notes_by_creator_url] "
                f"got portrait:{portrait} thread ids len: {len(thread_id_list)}"
            )
            note_detail_task = [self.get_note_by_id(thread_id) for thread_id in thread_id_list]
            notes = await asyncio.gather(*note_detail_task)
            notes = [note for note in notes if note]
            if callback and notes:
                await callback(notes)
            result.extend(notes)

            data = notes_res.get("data", {})
            has_more = int(data.get("has_more") or 0)
            if not has_more:
                break

            await asyncio.sleep(crawl_interval)
            page_number += 1

        return result
