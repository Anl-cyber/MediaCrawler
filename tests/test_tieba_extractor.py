# -*- coding: utf-8 -*-

from pathlib import Path

from media_platform.tieba.help import TieBaExtractor
from model.m_baidu_tieba import TiebaComment, TiebaNote, parse_reply_count


FIXTURE_DIR = Path(__file__).parent.parent / "media_platform" / "tieba" / "test_data"


def read_fixture(name: str) -> str:
    return (FIXTURE_DIR / name).read_text(encoding="utf-8")


# ── Bug2 回归：贴吧回复数 "22W"/"1.2W"/"3.5K" 缩写格式容错解析 ──
def test_parse_reply_count_formats():
    assert parse_reply_count("22W") == 220000
    assert parse_reply_count("1.2W") == 12000
    assert parse_reply_count("3.5K") == 3500
    assert parse_reply_count("1.2万") == 12000
    assert parse_reply_count("22万") == 220000
    assert parse_reply_count("12345") == 12345
    assert parse_reply_count(19) == 19
    assert parse_reply_count(None) == 0
    assert parse_reply_count("") == 0
    assert parse_reply_count("回复(37)") == 37
    assert parse_reply_count("1,234") == 1234


def test_parse_reply_count_hardening():
    # 加固3 fresh-eyes 返修：亿/前缀/混合/小写/千/负数/inf/nan/科学计数法
    assert parse_reply_count("1.2亿") == 120000000
    assert parse_reply_count("9999万") == 99990000
    assert parse_reply_count("1.8w") == 18000
    assert parse_reply_count("1.2k") == 1200
    assert parse_reply_count("12,345,678") == 12345678
    assert parse_reply_count("约1.2万") == 12000      # 前缀不破解析
    assert parse_reply_count("12万3000") == 120000    # 万+数字混合取下限
    assert parse_reply_count("22W回复贴") == 220000   # 后缀文本不再回落 22
    assert parse_reply_count("5千") == 5000
    assert parse_reply_count("1万2千") == 10000       # 混合取首个数字+单位
    assert parse_reply_count("-5") == 0               # 负数 clamp
    assert parse_reply_count("1.2e4") == 12000        # 科学计数法保留
    assert parse_reply_count("inf") == 0              # 不再 OverflowError 逃逸
    assert parse_reply_count("nan") == 0
    assert parse_reply_count(float("inf")) == 0
    assert parse_reply_count(float("nan")) == 0
    assert parse_reply_count(True) == 1
    assert parse_reply_count(False) == 0


def test_tieba_note_model_accepts_w_format_reply_count():
    # 模型构造时容错：total_replay_num="22W" 不再抛 pydantic ValidationError
    note = TiebaNote(
        note_id="10559655942",
        title="测试",
        note_url="https://tieba.baidu.com/p/10559655942",
        tieba_name="诸城吧",
        tieba_link="https://tieba.baidu.com/f?kw=%E8%AF%B8%E5%9F%8E",
        total_replay_num="22W",
    )
    assert note.total_replay_num == 220000


def test_tieba_comment_model_accepts_abbrev_counts():
    # 加固2：TiebaComment 的 sub_comment_count/floor/agree_num 同样容错，
    # 评论 API 若返回 '1.2W'/'3.5K' 不再整批崩溃
    comment = TiebaComment(
        comment_id="c1",
        content="测试评论",
        note_url="https://tieba.baidu.com/p/1",
        note_id="1",
        tieba_id="1",
        tieba_name="诸城吧",
        tieba_link="https://tieba.baidu.com",
        sub_comment_count="1.2W",
        floor="3.5K",
        agree_num="22W",
    )
    assert comment.sub_comment_count == 12000
    assert comment.floor == 3500
    assert comment.agree_num == 220000


def test_extract_search_note_list_from_api_accepts_w_format():
    # Bug2 实测路径：搜索 API post_num="22W" → 之前整页 ValidationError → 0 条
    api_data = {
        "data": {
            "card_list": [
                {
                    "cardInfo": "thread",
                    "cardStyle": "thread",
                    "data": {
                        "tid": "10559655942",
                        "title": "数，英，编程老师",
                        "content": "培训班需求",
                        "time": 1773552643,
                        "user": {"show_nickname": "754023117", "portrait": "x"},
                        "post_num": "22W",
                        "forum_name": "诸城",
                    },
                },
                {
                    "cardInfo": "thread",
                    "cardStyle": "thread",
                    "data": {
                        "tid": "10559655943",
                        "title": "另一个帖子",
                        "content": "内容",
                        "time": 1773552644,
                        "user": {"show_nickname": "a", "portrait": "y"},
                        "post_num": "1.2W",
                        "forum_name": "诸城",
                    },
                },
            ]
        },
    }
    notes = TieBaExtractor().extract_search_note_list_from_api(api_data)
    assert len(notes) == 2
    assert notes[0].total_replay_num == 220000
    assert notes[1].total_replay_num == 12000


def test_extract_note_detail_dom_accepts_w_format():
    # DOM 路径：<span class="red">22W</span> 回复贴 → total_replay_num 容错为 220000
    page_content = """
    <html><body>
      <div id="thread_theme_5">
        <li class="l_reply_num"><span class="red">22W</span>回复贴，共<span class="red">1100</span>页</li>
      </div>
    </body></html>
    """
    note = TieBaExtractor().extract_note_detail(page_content)
    assert note.total_replay_num == 220000
    assert note.total_replay_page == 1100


def test_extract_search_note_list_from_keyword_page():
    notes = TieBaExtractor.extract_search_note_list(read_fixture("search_keyword_notes.html"))

    assert len(notes) == 10
    assert notes[0].note_id == "9117888152"
    assert notes[0].title.startswith("武汉交互空间科技")
    assert notes[0].tieba_name == "武汉交互空间"
    assert notes[0].user_nickname == "V***人"


def test_extract_search_note_list_from_current_pc_card_page():
    page_content = """
    <html>
      <body>
        <div class="threadcardclass thread-new3 index-feed-cards">
          <a class="action-link-bg" href="https://tieba.baidu.com/p/10559655942?fr=undefined"></a>
          <div class="thread-forum-name display-flex align-center">
            <span class="forum-name-text">诸城吧</span>
          </div>
          <div class="top-title">
            <span class="forum-attention user">754023117</span>
            <span>发布于 2026-3-15</span>
          </div>
          <div class="title-wrap"><span>数，英，编程老师</span></div>
          <div class="abstract-wrap">
            <span>培训班需求，数学，英语，编程老师，专职兼职都可</span>
          </div>
          <a class="comment-link-zone" href="https://tieba.baidu.com/p/10559655942?showComment=1">
            <span class="action-number">19</span>
          </a>
        </div>
      </body>
    </html>
    """

    notes = TieBaExtractor.extract_search_note_list(page_content)

    assert len(notes) == 1
    assert notes[0].note_id == "10559655942"
    assert notes[0].title == "数，英，编程老师"
    assert notes[0].desc == "培训班需求，数学，英语，编程老师，专职兼职都可"
    assert notes[0].tieba_name == "诸城吧"
    assert notes[0].tieba_link.endswith("kw=%E8%AF%B8%E5%9F%8E")
    assert notes[0].user_nickname == "7***7"
    assert notes[0].publish_time == "2026-3-15"
    assert notes[0].total_replay_num == 19


def test_extract_search_note_list_from_current_pc_api():
    api_data = {
        "no": 0,
        "error": "success",
        "data": {
            "card_list": [
                {"cardInfo": "related_user", "cardStyle": "related_user", "data": {}},
                {
                    "cardInfo": "thread",
                    "cardStyle": "thread",
                    "data": {
                        "tid": "10559655942",
                        "title": "数，英，编程老师",
                        "content": "培训班需求，数学，英语，编程老师，专职兼职都可",
                        "time": 1773552643,
                        "user": {
                            "show_nickname": "754023117",
                            "portrait": "https://example.com/avatar.jpg",
                        },
                        "post_num": 19,
                        "forum_name": "诸城",
                    },
                },
            ]
        },
    }

    notes = TieBaExtractor().extract_search_note_list_from_api(api_data)

    assert len(notes) == 1
    assert notes[0].note_id == "10559655942"
    assert notes[0].title == "数，英，编程老师"
    assert notes[0].tieba_name == "诸城吧"
    assert notes[0].total_replay_num == 19
    assert notes[0].publish_time


def test_extract_note_detail_and_comments_from_current_pc_api():
    api_data = {
        "error_code": 0,
        "thread": {
            "id": 10451142633,
            "title": "这X尔斯对比巴尔斯，我只能说ID正确，允许居功自傲",
            "reply_num": 15,
            "create_time": 1769951446,
        },
        "forum": {"id": 1627732, "name": "dota2"},
        "page": {"total_page": 1},
        "first_floor": {
            "id": 153154064746,
            "author_id": 4089186644,
            "time": 1769951446,
            "content": [{"type": 0, "text": "皮队败决处刑德国编程钢琴师兼职数学家"}],
        },
        "post_list": [
            {
                "id": 153154097267,
                "author_id": 6614897968,
                "time": 1769952062,
                "content": [{"type": 0, "text": "xg现在大树阵容另一个辅助不选控制"}],
                "sub_post_number": 4,
            }
        ],
        "user_list": [
            {
                "id": 4089186644,
                "name_show": "泰高祖蒙斯克",
                "portrait": "tb.1.f893a7af",
                "ip_address": "广东",
            },
            {
                "id": 6614897968,
                "name_show": "期胡希3",
                "portrait": "tb.1.4d0471d4",
                "ip_address": "河北",
            },
        ],
    }

    extractor = TieBaExtractor()
    note = extractor.extract_note_detail_from_api(api_data)
    comments = extractor.extract_tieba_note_parent_comments_from_api(api_data, note)

    assert note.note_id == "10451142633"
    assert note.title == "这X尔斯对比巴尔斯，我只能说ID正确，允许居功自傲"
    assert note.desc == "皮队败决处刑德国编程钢琴师兼职数学家"
    assert note.user_nickname == "泰***克"
    assert note.tieba_name == "dota2吧"
    assert note.total_replay_num == 15
    assert note.total_replay_page == 1
    # 教学版已移除 ip_location 等可定位真人字段
    assert len(comments) == 1
    assert comments[0].comment_id == "153154097267"
    assert comments[0].content == "xg现在大树阵容另一个辅助不选控制"
    assert comments[0].user_nickname == "期***3"
    assert comments[0].sub_comment_count == 4
    # 教学版已移除 ip_location 等可定位真人字段


def test_extract_creator_info_and_threads_from_current_pc_api():
    creator_api = {
        "error_code": 0,
        "data": {
            "user": {
                "id": 3546493137,
                "name": "拜月教Alice",
                "name_show": "米米世界大手子",
                "portrait": "tb.1.6ad0cd4a.7ZcjVYWa7UpHttCld2OppA?t=1777543466",
                "fans_num": 58,
                "concern_num": 1,
                "sex": 1,
                "tb_age": "7.8",
                "ip_address": "广东",
            }
        },
    }
    feed_api = {
        "error_code": 0,
        "data": {
            "list": [
                {"type": 1, "thread_info": {"id": 10208192951, "tid": 10208192951}},
                {"type": 1, "thread_info": {"id": 9835114923}},
            ]
        },
    }

    extractor = TieBaExtractor()
    creator = extractor.extract_creator_info_from_api(creator_api)
    thread_ids = extractor.extract_creator_thread_id_list_from_api(feed_api)

    assert creator.user_nickname == "米***子"
    assert creator.fans == 58
    assert creator.follows == 1
    # 教学版已移除 user_id、user_name、ip_location 等可定位真人字段
    assert creator.registration_duration == "7.8"
    assert thread_ids == ["10208192951", "9835114923"]


def test_extract_tieba_note_list_from_current_frs_api():
    api_data = {
        "error_code": 0,
        "forum": {
            "id": 351091,
            "name": "加工中心",
            "tids": "10376710029,10636556989,",
        },
    }

    notes = TieBaExtractor().extract_tieba_note_list_from_frs_api(api_data)

    assert [note.note_id for note in notes] == ["10376710029", "10636556989"]
    assert notes[0].note_url == "https://tieba.baidu.com/p/10376710029"
    assert notes[0].tieba_name == "加工中心吧"
    assert notes[0].tieba_link.endswith("kw=%E5%8A%A0%E5%B7%A5%E4%B8%AD%E5%BF%83")


def test_extract_tieba_note_list_from_bigpipe_thread_page():
    notes = TieBaExtractor().extract_tieba_note_list(read_fixture("tieba_note_list.html"))

    assert len(notes) == 48
    assert notes[0].note_id == "9079949995"
    assert notes[0].title == "盗墓笔记全集+txt小说，已整理"
    assert notes[0].user_nickname == "公***仲"
    assert notes[0].tieba_name == "盗墓笔记吧"
    assert notes[0].tieba_link.endswith("kw=%E7%9B%97%E5%A2%93%E7%AC%94%E8%AE%B0&ie=utf-8")


def test_extract_note_detail_from_post_page():
    note = TieBaExtractor().extract_note_detail(read_fixture("note_detail.html"))

    assert note.note_id == "9117905169"
    assert note.title == "对于一个父亲来说，这个女儿14岁就死了"
    assert note.user_nickname == "章***轩"
    assert note.tieba_name == "以太比特吧"
    assert note.total_replay_num == 786
    assert note.total_replay_page == 13
    # 教学版已移除 ip_location 等可定位真人字段


def test_extract_parent_comments_from_post_page():
    comments = TieBaExtractor().extract_tieba_note_parment_comments(
        read_fixture("note_comments.html"),
        "9119688421",
    )

    assert len(comments) == 30
    assert comments[0].comment_id == "150726491368"
    assert comments[0].content == "中国队第22金！无悬念！"
    assert comments[0].user_nickname == "h***n"
    assert comments[0].tieba_name == "网球风云吧"
    # 教学版已移除 ip_location 等可定位真人字段


def test_extract_sub_comments_with_class_token_matching():
    parent = TiebaComment(
        comment_id="150726496253",
        content="parent",
        note_id="9119688421",
        note_url="https://tieba.baidu.com/p/9119688421",
        tieba_id="4513750",
        tieba_name="网球风云吧",
        tieba_link="https://tieba.baidu.com/f?kw=%E7%BD%91%E7%90%83%E9%A3%8E%E4%BA%91",
    )

    comments = TieBaExtractor().extract_tieba_note_sub_comments(
        read_fixture("note_sub_comments.html"),
        parent,
    )

    assert len(comments) >= 10
    assert comments[0].comment_id
    assert comments[0].parent_comment_id == parent.comment_id
    # 教学版已移除 user_link 等可定位真人字段的采集
