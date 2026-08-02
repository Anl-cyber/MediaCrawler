# -*- coding: utf-8 -*-
# Copyright (c) 2025 relakkes@gmail.com
#
# This file is part of MediaCrawler project.
# Repository: https://github.com/NanmiCoder/MediaCrawler/blob/main/model/m_baidu_tieba.py
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


# -*- coding: utf-8 -*-
import math
import re
from typing import Optional

from pydantic import BaseModel, Field, field_validator


def parse_reply_count(value) -> int:
    """把贴吧回复数/页数的各种显示格式容错转成 int。

    贴吧页面 DOM 与接口中回复数可能显示为 "22W"（万）、"1.2W"、"3.5K"、"1.2万"
    等缩写格式（实测 DOM 与搜索 API 均会出现）。TiebaNote.total_replay_num 声明为
    int，直接传 "22W" 会让 pydantic 校验失败 → ValidationError → 整页解析中断
    （2026-08-02 实测：BaiduTieBaCrawler.search 整页 0 条）。本函数在塞进模型前
    统一转换：22W→220000、1.2W→12000、3.5K→3500、1.2万→12000、1.2亿→120000000。

    加固3（2026-08-02 fresh-eyes 返修）：
    - '亿' 单位支持（旧实现只认 W/万/K，'1.2亿' 会回落首整数 → 1）；
    - 前缀/混合格式支持（'约1.2万'→12000、'12万3000'→120000、'22W回复贴'→220000），
      由"endswith 截尾"改为"regex 提取 数字+单位 组合"；
    - 负数 clamp 0；'inf'/'nan' 回落 0（旧实现 'inf' 会 OverflowError 逃逸）；
    - 科学计数法 '1.2e4'→12000 行为保留。
    """
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, int):
        return max(value, 0)
    if isinstance(value, float):
        if not math.isfinite(value):
            return 0
        return max(int(value), 0)
    if value is None:
        return 0
    s = str(value).strip().replace(",", "").replace("，", "")
    if not s:
        return 0
    upper = s.upper()
    # 数字 + 单位组合（覆盖 W/万/亿/K/千，含前缀 '约1.2万'、混合 '12万3000'、
    # 后缀 '22W回复贴' 等实际显示文本）
    m = re.search(r"(\d+(?:\.\d+)?)\s*([亿万千WK])", upper)
    if m:
        try:
            num = float(m.group(1))
        except (TypeError, ValueError):
            num = 0.0
        mult = {
            "亿": 100000000, "万": 10000, "W": 10000,
            "千": 1000, "K": 1000,
        }.get(m.group(2), 1)
        try:
            return max(int(num * mult), 0)
        except OverflowError:
            return 0
    # 无单位：纯数字 / 科学计数法 / 小数（'12345'、'1.2e4'、'3.9'）
    try:
        f = float(upper)
    except (TypeError, ValueError):
        m2 = re.search(r"-?\d+", upper)
        if not m2:
            return 0
        try:
            return max(int(m2.group(0)), 0)
        except (TypeError, ValueError):
            return 0
    if not math.isfinite(f):
        return 0
    return max(int(f), 0)


class TiebaNote(BaseModel):
    """
    Baidu Tieba post
    """
    note_id: str = Field(..., description="Post ID")
    title: str = Field(..., description="Post title")
    desc: str = Field(default="", description="Post description")
    note_url: str = Field(..., description="Post link")
    publish_time: str = Field(default="", description="Publish time")
    create_time_unix: int = Field(default=0, description="Create time (Unix timestamp, from page_pc API)")
    creator_hash: str = Field(default="", description="Creator anonymized hash")
    user_nickname: str = Field(default="", description="User nickname (masked)")
    tieba_name: str = Field(..., description="Tieba name")
    tieba_link: str = Field(..., description="Tieba link")
    total_replay_num: int = Field(default=0, description="Total reply count")
    total_replay_page: int = Field(default=0, description="Total reply pages")
    source_keyword: str = Field(default="", description="Source keyword")
    # New fields from page_pc API
    share_num: int = Field(default=0, description="Share count")
    agree_num: int = Field(default=0, description="Agree/Like count")
    forum_first_class: str = Field(default="", description="Forum first-level category")
    forum_second_class: str = Field(default="", description="Forum second-level category")

    # Bug2 修复：贴吧回复数/页数可能以 "22W"/"1.2W"/"3.5K" 等缩写格式出现（DOM 与
    # 搜索 API 均实测出现），模型构造时统一容错转换，防止 ValidationError 中断整页解析。
    # 必须用 mode="before"：int 字段的 core 校验会在 after validator 之前失败，拦截不到。
    @field_validator("total_replay_num", "total_replay_page", mode="before")
    @classmethod
    def _validate_reply_count(cls, v):
        return parse_reply_count(v)


class TiebaComment(BaseModel):
    """
    Baidu Tieba comment
    """

    comment_id: str = Field(..., description="Comment ID")
    parent_comment_id: str = Field(default="", description="Parent comment ID")
    content: str = Field(..., description="Comment content")
    creator_hash: str = Field(default="", description="Creator anonymized hash")
    user_nickname: str = Field(default="", description="User nickname (masked)")
    publish_time: str = Field(default="", description="Publish time")
    sub_comment_count: int = Field(default=0, description="Sub-comment count")
    note_id: str = Field(..., description="Post ID")
    note_url: str = Field(..., description="Post link")
    tieba_id: str = Field(..., description="Tieba ID")
    tieba_name: str = Field(..., description="Tieba name")
    tieba_link: str = Field(..., description="Tieba link")
    # New fields from page_pc API
    floor: int = Field(default=0, description="Floor number")
    agree_num: int = Field(default=0, description="Agree/Like count")
    author_level_name: str = Field(default="", description="Author level name")
    author_ip_address: str = Field(default="", description="Author IP location")
    author_gender: int = Field(default=0, description="Author gender (1=male, 2=female)")
    author_is_bawu: int = Field(default=0, description="Is forum moderator (1=yes)")

    # 加固2：评论 API/DOM 若返回 '1.2W'/'1,234' 等缩写/格式化字符串，int 字段构造时
    # 会 ValidationError → 整批评论采集崩溃（core.py get_note_all_comments 无 try/except、
    # batch gather 无 return_exceptions，search 流程会 break 掉整个关键词循环）。
    # 与 TiebaNote 同模式容错（mode="before" 在 int core 校验前拦截）。
    @field_validator("sub_comment_count", "floor", "agree_num", mode="before")
    @classmethod
    def _validate_comment_count(cls, v):
        return parse_reply_count(v)


class TiebaCreator(BaseModel):
    """
    Baidu Tieba creator（教学版：个人资料不再落库，仅作内存对象）
    """
    creator_hash: str = Field(default="", description="创作者匿名哈希(不存原始用户链接)")
    user_nickname: str = Field(default="", description="User nickname (已脱敏)")
    follows: int = Field(default=0, description="Follows count")
    fans: int = Field(default=0, description="Fans count")
    registration_duration: str = Field(default="", description="Registration duration")
