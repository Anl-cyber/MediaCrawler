# -*- coding: utf-8 -*-
# Copyright (c) 2025 relakkes@gmail.com
#
# This file is part of MediaCrawler project.
# Repository: https://github.com/NanmiCoder/MediaCrawler/blob/main/media_platform/tieba/login.py
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
import functools
import sys
from typing import Optional

from playwright.async_api import BrowserContext, Page
from tenacity import (RetryError, retry, retry_if_result, stop_after_attempt,
                      wait_fixed)

import config
from base.base_crawler import AbstractLogin
from tools import utils


class BaiduTieBaLogin(AbstractLogin):

    def __init__(self,
                 login_type: str,
                 browser_context: BrowserContext,
                 context_page: Page,
                 login_phone: Optional[str] = "",
                 cookie_str: str = ""
                 ):
        config.LOGIN_TYPE = login_type
        self.browser_context = browser_context
        self.context_page = context_page
        self.login_phone = login_phone
        self.cookie_str = cookie_str

    @retry(stop=stop_after_attempt(600), wait=wait_fixed(1), retry=retry_if_result(lambda value: value is False))
    async def check_login_state(self) -> bool:
        """
        Poll to check if login status is successful, return True if successful, otherwise return False

        Returns:

        """
        current_cookie = await self.browser_context.cookies()
        _, cookie_dict = utils.convert_cookies(current_cookie)
        stoken = cookie_dict.get("STOKEN")
        ptoken = cookie_dict.get("PTOKEN")
        if stoken or ptoken:
            return True
        return False

    async def _quick_login_state(self) -> bool:
        """不带重试的一次性登录态检查（check_login_state 带 600 次重试装饰器，不能直接复用做快速判断）"""
        current_cookie = await self.browser_context.cookies()
        _, cookie_dict = utils.convert_cookies(current_cookie)
        return bool(cookie_dict.get("STOKEN") or cookie_dict.get("PTOKEN"))

    async def _is_captcha_page(self) -> bool:
        """检测当前页面是否处于百度安全验证页（滑块验证）。

        2026-08-03 贴吧登录加固：自动登录点击按钮时可能被百度安全验证页拦截，
        原实现 30s locator 超时直接崩溃。此方法识别验证页，让上层转入人工等待。
        """
        try:
            title = await self.context_page.title()
            if title and ("安全验证" in title or "captcha" in title.lower()):
                return True
            content = await self.context_page.content()
            if "拖动滑块完成验证" in content or "百度安全验证" in content:
                return True
            if "captcha" in content[:3000].lower():
                return True
        except Exception:
            # 页面跳转/加载中读取失败，保守视为非验证页，让上层按原逻辑重试
            pass
        return False

    async def _wait_human_captcha(self, timeout_seconds: int = 180) -> bool:
        """检测到百度安全验证页时，提示用户在浏览器中手动完成滑块，轮询等待通过。

        Args:
            timeout_seconds: 最长等待秒数，默认 180s（与 core.py 的 _CAPTCHA_WAIT_SEC 对齐）

        Returns:
            True: 验证页已消失（用户完成滑块）
            False: 等待超时，验证页仍存在
        """
        import time as _time
        utils.logger.warning(
            "[BaiduTieBaLogin] 检测到百度安全验证页，请在弹出的浏览器窗口中手动完成滑块验证，"
            f"等待最长 {timeout_seconds}s ..."
        )
        start = _time.time()
        while _time.time() - start < timeout_seconds:
            await asyncio.sleep(2)
            if not await self._is_captcha_page():
                utils.logger.info("[BaiduTieBaLogin] 百度安全验证已通过，继续登录流程 ...")
                return True
        utils.logger.warning("[BaiduTieBaLogin] 等待人工滑块验证超时，登录流程退出 ...")
        return False

    async def _click_login_button_with_captcha(self) -> bool:
        """点击登录按钮，遇到百度安全验证页（滑块）时转入人工等待。

        2026-08-03 贴吧登录加固：原实现点击登录按钮 30s 超时直接崩溃，
        对滑块验证无感知。改造后：
        - 点击前先检测验证页，命中则直接提示人工处理
        - 点击超时/异常后复查是否转入验证页，命中则提示人工处理
        - 人工完成滑块且登录态已就绪 → 返回 True（调用方跳过扫码）
        - 正常点击成功 → 返回 False（调用方继续找二维码）

        Returns:
            True: 人工处理完成且已登录（跳过扫码）
            False: 正常点击路径，继续原扫码流程
        """
        if await self._is_captcha_page():
            if not await self._wait_human_captcha():
                sys.exit("[BaiduTieBaLogin] 滑块验证等待超时，退出")
            return await self._quick_login_state()
        login_button_ele = self.context_page.locator("xpath=//li[@class='u_login']")
        try:
            await login_button_ele.click(timeout=30000)
        except Exception as exc:
            # 点击过程/点击后页面可能转入安全验证页（滑块），转为人工等待而不是崩溃
            utils.logger.warning(
                f"[BaiduTieBaLogin._click_login_button_with_captcha] 点击登录按钮异常: {exc}，"
                "检查是否触发安全验证页 ..."
            )
            if await self._is_captcha_page():
                if not await self._wait_human_captcha():
                    sys.exit("[BaiduTieBaLogin] 滑块验证等待超时，退出")
                return await self._quick_login_state()
            raise
        return False

    async def begin(self):
        """Start login baidutieba"""
        utils.logger.info("[BaiduTieBaLogin.begin] Begin login baidutieba ...")
        if config.LOGIN_TYPE == "qrcode":
            await self.login_by_qrcode()
        elif config.LOGIN_TYPE == "phone":
            await self.login_by_mobile()
        elif config.LOGIN_TYPE == "cookie":
            await self.login_by_cookies()
        else:
            raise ValueError("[BaiduTieBaLogin.begin]Invalid Login Type Currently only supported qrcode or phone or cookies ...")

    async def login_by_mobile(self):
        """Login baidutieba by mobile"""
        pass

    async def login_by_qrcode(self):
        """login baidutieba website and keep webdriver login state"""
        utils.logger.info("[BaiduTieBaLogin.login_by_qrcode] Begin login baidutieba by qrcode ...")
        qrcode_img_selector = "xpath=//img[@class='tang-pass-qrcode-img']"
        # find login qrcode
        base64_qrcode_img = await utils.find_login_qrcode(
            self.context_page,
            selector=qrcode_img_selector
        )
        if not base64_qrcode_img:
            utils.logger.info("[BaiduTieBaLogin.login_by_qrcode] login failed , have not found qrcode please check ....")
            # if this website does not automatically popup login dialog box, we will manual click login button
            # 2026-08-03 贴吧登录加固：百度安全验证页（滑块）会拦截登录按钮点击，
            # 原实现 locator 30s 超时直接崩溃；改为先识别验证页 → 提示人工处理 → 轮询等待
            await asyncio.sleep(0.5)
            if await self._click_login_button_with_captcha():
                # 人工完成滑块后可能已直接登录，跳过后续扫码
                utils.logger.info("[BaiduTieBaLogin.login_by_qrcode] 滑块验证后已登录，跳过扫码 ...")
                await asyncio.sleep(5)
                return
            base64_qrcode_img = await utils.find_login_qrcode(
                self.context_page,
                selector=qrcode_img_selector
            )
            if not base64_qrcode_img:
                utils.logger.info("[BaiduTieBaLogin.login_by_qrcode] login failed , have not found qrcode please check ....")
                sys.exit()

        # show login qrcode
        # fix issue #12
        # we need to use partial function to call show_qrcode function and run in executor
        # then current asyncio event loop will not be blocked
        partial_show_qrcode = functools.partial(utils.show_qrcode, base64_qrcode_img)
        asyncio.get_running_loop().run_in_executor(executor=None, func=partial_show_qrcode)

        utils.logger.info(f"[BaiduTieBaLogin.login_by_qrcode] waiting for scan code login, remaining time is 120s")
        try:
            await self.check_login_state()
        except RetryError:
            utils.logger.info("[BaiduTieBaLogin.login_by_qrcode] Login baidutieba failed by qrcode login method ...")
            sys.exit()

        wait_redirect_seconds = 5
        utils.logger.info(f"[BaiduTieBaLogin.login_by_qrcode] Login successful then wait for {wait_redirect_seconds} seconds redirect ...")
        await asyncio.sleep(wait_redirect_seconds)

    async def login_by_cookies(self):
        """login baidutieba website by cookies"""
        utils.logger.info("[BaiduTieBaLogin.login_by_cookies] Begin login baidutieba by cookie ...")
        for key, value in utils.convert_str_cookie_to_dict(self.cookie_str).items():
            await self.browser_context.add_cookies([{
                'name': key,
                'value': value,
                'domain': ".baidu.com",
                'path': "/"
            }])
