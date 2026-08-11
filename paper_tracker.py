import base64
import hashlib
import hmac
import json
import os
import time
from datetime import date, timedelta
from xml.etree import ElementTree

import requests
from openai import OpenAI

LLM_API_KEY = os.getenv("LLM_API_KEY")
S2_API_KEY = os.getenv("S2_API_Key") or os.getenv("S2_API_KEY")
S2_PROXY_API_KEY = os.getenv("S2_PROXY_API_KEY")
FEISHU_BOT_WEBHOOK = os.getenv("FEISHU_BOT_WEBHOOK")
FEISHU_BOT_SIGNKEY = os.getenv("FEISHU_BOT_SIGNKEY")

HISTORY_FILE = "config/seen_papers.txt"
BLACKLIST_FILE = "config/blacklisted_venues.txt"
PREFERENCES_FILE = "config/paper_preferences.json"
SEMANTIC_SCHOLAR_SEARCH_URL = "https://api.semanticscholar.org/graph/v1/paper/search"
S2_PROXY_SEARCH_URL = "https://s2api.ominiai.cn/s2/graph/v1/paper/search"
ARXIV_SEARCH_URL = "https://export.arxiv.org/api/query"
LLM_BASE_URL = os.getenv("LLM_BASE_URL", "https://4router.net/v1").rstrip("/")
LLM_MODEL = "gpt-5.6-sol"
REQUEST_TIMEOUT_SECONDS = 30
REQUEST_RETRIES = 3
S2_MIN_REQUEST_INTERVAL_SECONDS = 1.0
LAST_S2_REQUEST_TIME = None
ARXIV_MIN_REQUEST_INTERVAL_SECONDS = 3.0
LAST_ARXIV_REQUEST_TIME = None


def read_list(file_path):
    """读取文件列表，忽略空行。"""
    if not os.path.exists(file_path):
        return []
    with open(file_path, "r", encoding="utf-8") as file:
        return [line.strip() for line in file if line.strip()]


def load_preferences():
    """加载关键词、来源优先级和检索窗口配置。"""
    with open(PREFERENCES_FILE, "r", encoding="utf-8") as file:
        preferences = json.load(file)

    required_keys = {
        "topics",
        "venue_metadata",
        "high_impact_journals",
        "search_lookback_days",
        "max_results_per_query",
        "min_papers_per_digest",
        "max_papers_per_digest",
        "extra_paper_min_query_matches",
    }
    missing_keys = required_keys - preferences.keys()
    if missing_keys:
        raise ValueError(f"论文偏好配置缺少字段: {', '.join(sorted(missing_keys))}")
    if not preferences["topics"]:
        raise ValueError("论文偏好配置中至少需要一个研究主题。")
    return preferences


def wait_for_s2_rate_limit():
    """确保同一进程内所有官方 S2 请求至少相隔一秒。"""
    global LAST_S2_REQUEST_TIME

    now = time.monotonic()
    if LAST_S2_REQUEST_TIME is not None:
        remaining_seconds = S2_MIN_REQUEST_INTERVAL_SECONDS - (now - LAST_S2_REQUEST_TIME)
        if remaining_seconds > 0:
            time.sleep(remaining_seconds)
    LAST_S2_REQUEST_TIME = time.monotonic()


def request_semantic_scholar(params):
    """通过官方 Semantic Scholar Graph API 请求；失败时返回 None。"""
    if not S2_API_KEY:
        print("缺少 S2_API_Key GitHub Secret，改用 arXiv 备用检索。")
        return None

    headers = {
        "x-api-key": S2_API_KEY,
        "User-Agent": "DailyPaper keyword tracker",
    }
    for attempt in range(REQUEST_RETRIES):
        try:
            wait_for_s2_rate_limit()
            response = requests.get(
                SEMANTIC_SCHOLAR_SEARCH_URL,
                params=params,
                headers=headers,
                timeout=REQUEST_TIMEOUT_SECONDS,
            )
        except requests.RequestException as error:
            print(f"Semantic Scholar 网络请求失败：{error}")
            return None

        if response.status_code == 200:
            return response.json()
        if response.status_code == 429:
            retry_after = response.headers.get("Retry-After")
            retry_message = f"，建议等待 {retry_after} 秒" if retry_after else ""
            wait_seconds = float(retry_after) if retry_after and retry_after.isdigit() else 2
            print(f"Semantic Scholar 请求被限流 (429){retry_message}，{wait_seconds} 秒后重试。")
            time.sleep(wait_seconds)
            continue
        if response.status_code not in {500, 502, 503, 504}:
            print(
                f"Semantic Scholar 请求失败 ({response.status_code})："
                f"{response.text[:300]}"
            )
            return None

        wait_seconds = 2**attempt
        print(
            f"Semantic Scholar 暂时不可用 ({response.status_code})，"
            f"{wait_seconds} 秒后重试..."
        )
        time.sleep(wait_seconds)

    print("Semantic Scholar 持续不可用，将改用 arXiv 备用检索。")
    return None


def request_s2_proxy(params):
    """当官方 S2 持续失败时，通过可选代理 API 请求同一 Graph 接口。"""
    if not S2_PROXY_API_KEY:
        print("未配置 S2_PROXY_API_KEY，跳过 S2 代理备用源。")
        return None

    headers = {
        "Authorization": f"Bearer {S2_PROXY_API_KEY}",
        "User-Agent": "DailyPaper keyword tracker",
    }
    for attempt in range(REQUEST_RETRIES):
        try:
            response = requests.get(
                S2_PROXY_SEARCH_URL,
                params=params,
                headers=headers,
                timeout=REQUEST_TIMEOUT_SECONDS,
            )
        except requests.RequestException as error:
            print(f"S2 代理网络请求失败：{error}")
            return None

        if response.status_code == 200:
            return response.json()
        if response.status_code not in {429, 500, 502, 503, 504}:
            print(f"S2 代理请求失败 ({response.status_code})：{response.text[:300]}")
            return None

        retry_after = response.headers.get("Retry-After")
        wait_seconds = float(retry_after) if retry_after and retry_after.isdigit() else 2**attempt
        print(f"S2 代理暂时不可用 ({response.status_code})，{wait_seconds} 秒后重试...")
        time.sleep(wait_seconds)

    print("S2 代理持续不可用，将改用 arXiv 备用检索。")
    return None


def wait_for_arxiv_rate_limit():
    """遵守 arXiv 公开 API 的保守请求间隔，减少 429。"""
    global LAST_ARXIV_REQUEST_TIME

    now = time.monotonic()
    if LAST_ARXIV_REQUEST_TIME is not None:
        remaining_seconds = ARXIV_MIN_REQUEST_INTERVAL_SECONDS - (
            now - LAST_ARXIV_REQUEST_TIME
        )
        if remaining_seconds > 0:
            time.sleep(remaining_seconds)
    LAST_ARXIV_REQUEST_TIME = time.monotonic()


def request_arxiv(query, max_results):
    """通过 arXiv 的公开 API 搜索备用论文，并统一成 Semantic Scholar 字段。"""
    for attempt in range(REQUEST_RETRIES):
        try:
            wait_for_arxiv_rate_limit()
            response = requests.get(
                ARXIV_SEARCH_URL,
                params={
                    "search_query": f'all:"{query.replace(chr(34), "")}"',
                    "start": 0,
                    "max_results": max_results,
                    "sortBy": "submittedDate",
                    "sortOrder": "descending",
                },
                headers={"User-Agent": "DailyPaper keyword tracker"},
                timeout=REQUEST_TIMEOUT_SECONDS,
            )
        except requests.RequestException as error:
            print(f"arXiv 备用检索网络失败：{error}")
            return []

        if response.status_code == 200:
            break
        if response.status_code not in {429, 500, 502, 503, 504}:
            print(f"arXiv 备用检索失败 ({response.status_code})：{response.text[:300]}")
            return []

        retry_after = response.headers.get("Retry-After")
        wait_seconds = float(retry_after) if retry_after and retry_after.isdigit() else 3 * (attempt + 1)
        print(f"arXiv 暂时不可用 ({response.status_code})，{wait_seconds} 秒后重试...")
        time.sleep(wait_seconds)
    else:
        print("arXiv 连续请求失败，本次跳过该关键词。")
        return []

    atom_namespace = {"atom": "http://www.w3.org/2005/Atom"}
    try:
        root = ElementTree.fromstring(response.content)
    except ElementTree.ParseError as error:
        print(f"arXiv 返回内容无法解析：{error}")
        return []

    papers = []
    for entry in root.findall("atom:entry", atom_namespace):
        arxiv_url = (entry.findtext("atom:id", default="", namespaces=atom_namespace)).strip()
        arxiv_id = arxiv_url.rsplit("/", maxsplit=1)[-1]
        if not arxiv_id:
            continue
        published = entry.findtext("atom:published", default="", namespaces=atom_namespace)
        published_date = published[:10] if published else None
        authors = [
            {"name": (author.findtext("atom:name", default="未知", namespaces=atom_namespace)).strip()}
            for author in entry.findall("atom:author", atom_namespace)
        ]
        papers.append(
            {
                "paperId": f"ARXIV:{arxiv_id}",
                "title": " ".join(
                    entry.findtext("atom:title", default="无标题", namespaces=atom_namespace).split()
                ),
                "abstract": " ".join(
                    entry.findtext("atom:summary", default="", namespaces=atom_namespace).split()
                ),
                "authors": authors,
                "url": arxiv_url,
                "venue": "arXiv",
                "externalIds": {"ArXiv": arxiv_id},
                "publicationDate": published_date,
                "year": int(published_date[:4]) if published_date else None,
                "citationCount": 0,
            }
        )
    return papers


def paper_date(paper):
    """将论文日期标准化为 date；没有完整日期时返回 None。"""
    publication_date = paper.get("publicationDate")
    if not publication_date:
        return None
    try:
        return date.fromisoformat(publication_date)
    except ValueError:
        return None


def get_venue_name(paper):
    """兼容 Semantic Scholar 的 venue 与 publicationVenue 字段。"""
    publication_venue = paper.get("publicationVenue") or {}
    alternate_names = publication_venue.get("alternate_names") or []
    return (
        paper.get("venue")
        or publication_venue.get("name")
        or (alternate_names[0] if alternate_names else "")
        or "未知会议/期刊"
    ).strip()


def matches_any(venue_name, aliases):
    normalized_venue = venue_name.lower()
    return any(alias.lower() in normalized_venue for alias in aliases)


def get_venue_metadata(venue_name, preferences):
    """按最长别名匹配会议/期刊的缩写、CCF 等级和来源优先级。"""
    venue_metadata = preferences["venue_metadata"]
    for metadata in venue_metadata:
        aliases = metadata.get("aliases", [])
        if any(
            alias.lower() in venue_name.lower()
            for alias in sorted(aliases, key=len, reverse=True)
        ):
            return metadata
    return None


def get_venue_details(paper, preferences):
    """返回显示在日报中的会议/期刊名称、缩写与 CCF 等级。"""
    venue_name = get_venue_name(paper)
    metadata = get_venue_metadata(venue_name, preferences)
    if metadata:
        return venue_name, metadata["abbreviation"], metadata["ccf_rank"]
    return venue_name, "—", "未收录"


def source_priority(paper, preferences):
    """按照用户指定的来源优先级返回可排序的分组和中文标签。"""
    venue_name = get_venue_name(paper)
    external_ids = paper.get("externalIds") or {}
    metadata = get_venue_metadata(venue_name, preferences)

    if metadata and "source_priority" in metadata:
        return metadata["source_priority"], metadata["source_label"]
    if matches_any(venue_name, preferences["high_impact_journals"]):
        return 2, "高影响力期刊"
    if "ArXiv" in external_ids or "arxiv" in venue_name.lower():
        return 0, "arXiv 预印本"
    return 1, "其他会议或期刊"


def is_recent_enough(paper, earliest_date):
    """仅保留检索窗口内的论文；没有精确日期的当年论文保守保留。"""
    publication_date = paper_date(paper)
    if publication_date:
        return publication_date >= earliest_date
    return paper.get("year") == earliest_date.year


def rank_paper(paper, preferences):
    """先按来源优先级和关键词相似度，再按发表日期和引用数排序。"""
    priority, _ = source_priority(paper, preferences)
    publication_date = paper_date(paper) or date.min
    citation_count = paper.get("citationCount") or 0
    topic_matches = len(paper.get("matchedTopics", []))
    query_matches = len(paper.get("matchedQueries", []))
    return priority, query_matches, topic_matches, publication_date, citation_count


def select_papers_for_digest(ranked_papers, preferences):
    """保底选择三篇，只有多关键词匹配的论文才扩展到第四、第五篇。"""
    minimum_count = preferences["min_papers_per_digest"]
    maximum_count = preferences["max_papers_per_digest"]
    extra_match_threshold = preferences["extra_paper_min_query_matches"]
    selected_papers = ranked_papers[:minimum_count]

    for paper in ranked_papers[minimum_count:maximum_count]:
        if len(paper.get("matchedQueries", [])) >= extra_match_threshold:
            selected_papers.append(paper)
    return selected_papers


def get_paper_recommendations():
    """按研究关键词搜索最新论文，并按来源优先级挑选未读论文。"""
    preferences = load_preferences()
    seen_papers = set(read_list(HISTORY_FILE))
    blacklisted_venues = [venue.lower() for venue in read_list(BLACKLIST_FILE)]
    earliest_date = date.today() - timedelta(days=preferences["search_lookback_days"])
    papers_by_id = {}
    fields = (
        "paperId,title,abstract,authors,url,venue,publicationVenue,externalIds,"
        "publicationDate,year,citationCount"
    )

    print(f"检索 {earliest_date.isoformat()} 以来的关键词论文...")
    semantic_scholar_available = True
    s2_proxy_available = bool(S2_PROXY_API_KEY)
    for topic in preferences["topics"]:
        topic_name = topic["name"]
        for query in topic["queries"]:
            print(f"检索主题：{topic_name} ({query})")
            if semantic_scholar_available:
                print("来源：官方 Semantic Scholar API")
                result = request_semantic_scholar(
                    {
                        "query": query,
                        "limit": preferences["max_results_per_query"],
                        "fields": fields,
                    }
                )
                if result is None:
                    semantic_scholar_available = False
                    if s2_proxy_available:
                        print("切换到 S2 代理备用检索。")
                        result = request_s2_proxy(
                            {
                                "query": query,
                                "limit": preferences["max_results_per_query"],
                                "fields": fields,
                            }
                        )
                        if result is None:
                            s2_proxy_available = False
                            print("切换到 arXiv 免 Key 备用检索。")
                            raw_papers = request_arxiv(
                                query, preferences["max_results_per_query"]
                            )
                        else:
                            raw_papers = result.get("data", [])
                    else:
                        print("切换到 arXiv 免 Key 备用检索。")
                        raw_papers = request_arxiv(
                            query, preferences["max_results_per_query"]
                        )
                else:
                    raw_papers = result.get("data", [])
            elif s2_proxy_available:
                print("来源：S2 代理备用服务")
                result = request_s2_proxy(
                    {
                        "query": query,
                        "limit": preferences["max_results_per_query"],
                        "fields": fields,
                    }
                )
                if result is None:
                    s2_proxy_available = False
                    print("切换到 arXiv 免 Key 备用检索。")
                    raw_papers = request_arxiv(query, preferences["max_results_per_query"])
                else:
                    raw_papers = result.get("data", [])
            else:
                print("来源：arXiv 免 Key 备用检索")
                raw_papers = request_arxiv(query, preferences["max_results_per_query"])

            for paper in raw_papers:
                paper_id = paper.get("paperId")
                if not paper_id or paper_id in seen_papers:
                    continue
                if not (paper.get("abstract") or "").strip():
                    continue
                if not is_recent_enough(paper, earliest_date):
                    continue

                venue_name = get_venue_name(paper).lower()
                if any(blocked_venue in venue_name for blocked_venue in blacklisted_venues):
                    continue

                stored_paper = papers_by_id.setdefault(paper_id, dict(paper))
                matched_topics = stored_paper.setdefault("matchedTopics", [])
                if topic_name not in matched_topics:
                    matched_topics.append(topic_name)
                matched_queries = stored_paper.setdefault("matchedQueries", [])
                if query not in matched_queries:
                    matched_queries.append(query)

    ranked_papers = sorted(
        papers_by_id.values(),
        key=lambda paper: rank_paper(paper, preferences),
        reverse=True,
    )
    selected_papers = select_papers_for_digest(ranked_papers, preferences)
    print(f"筛选得到 {len(selected_papers)} 篇未读论文。")
    return selected_papers


def format_authors(authors):
    author_names = [author.get("name", "未知") for author in authors]
    if len(author_names) > 5:
        return ", ".join(author_names[:3]) + ", ... , " + author_names[-1]
    return ", ".join(author_names) or "未知作者"


def get_paper_url(paper):
    external_ids = paper.get("externalIds") or {}
    doi = (external_ids.get("DOI") or "").strip()
    if doi:
        return f"https://doi.org/{doi}"
    return paper.get("url") or f"https://www.semanticscholar.org/paper/{paper['paperId']}"


def validate_llm_summary(content):
    """确保模型输出是正文而不是网关返回的 HTML 页面。"""
    if not isinstance(content, str) or not content.strip():
        raise RuntimeError("LLM 响应中缺少可用的 message.content。")

    summary = content.strip()
    html_markers = ("<!doctype html", "<html", "<head", "<body")
    if any(marker in summary.lower() for marker in html_markers):
        raise RuntimeError(
            "LLM 返回了 HTML 页面而非模型正文；请检查 LLM_BASE_URL 和代理鉴权配置。"
        )
    return summary


def extract_llm_summary(response):
    """兼容 OpenAI SDK 对象、OpenAI JSON 和代理直接返回的文本。"""
    if isinstance(response, str):
        text_response = response.strip()
        if not text_response:
            raise RuntimeError("LLM 返回了空字符串。")
        try:
            response = json.loads(text_response)
        except json.JSONDecodeError:
            return validate_llm_summary(text_response)

    model_dump = getattr(response, "model_dump", None)
    if callable(model_dump):
        dumped_response = model_dump()
        if isinstance(dumped_response, dict):
            response = dumped_response

    if isinstance(response, dict):
        choices = response.get("choices") or []
        if not choices:
            raise RuntimeError("LLM JSON 响应中缺少 choices。")
        message = choices[0].get("message") or {}
        content = message.get("content")
    else:
        choices = getattr(response, "choices", None) or []
        if not choices:
            raise RuntimeError("LLM 响应中缺少 choices。")
        message = getattr(choices[0], "message", None)
        content = getattr(message, "content", None)

    if isinstance(content, str):
        return validate_llm_summary(content)
    if isinstance(content, list):
        text_parts = [
            item.get("text", "") if isinstance(item, dict) else getattr(item, "text", "")
            for item in content
        ]
        joined_text = "".join(text_parts).strip()
        if joined_text:
            return validate_llm_summary(joined_text)
    raise RuntimeError("LLM 响应中缺少可用的 message.content。")


def summarize_papers_with_llm(papers):
    """使用 gpt-5.6-sol 为每篇论文生成结构化中文阅读笔记。"""
    if not LLM_API_KEY:
        raise RuntimeError("缺少 LLM_API_KEY GitHub Secret。")

    client = OpenAI(api_key=LLM_API_KEY, base_url=LLM_BASE_URL)
    preferences = load_preferences()
    report_parts = []
    for index, paper in enumerate(papers, start=1):
        title = paper.get("title", "无标题")
        abstract = (paper.get("abstract") or "无摘要").strip()
        venue_name, venue_abbreviation, ccf_rank = get_venue_details(paper, preferences)
        source_label = source_priority(paper, preferences)[1]
        authors = format_authors(paper.get("authors", []))
        publication_date = paper.get("publicationDate") or paper.get("year") or "未知日期"
        topics = "、".join(paper.get("matchedTopics", []))
        url = get_paper_url(paper)

        prompt = f"""你是一名严谨的科研助理。只能依据给出的标题和摘要，用中文生成一份简洁、具体的论文阅读笔记。不得补造实验设置、数值、结论或局限；信息不足时明确写“摘要未说明”。不要输出任何客套话、前言或额外小标题。

严格使用以下五个 Markdown 字段，每个字段 1–3 句：
**问题**：
**方法**：
**结果**：
**局限**：
**可复用点**：

标题：{title}
摘要：{abstract}
"""
        response = client.chat.completions.create(
            model=LLM_MODEL,
            messages=[{"role": "user", "content": prompt}],
        )
        summary = extract_llm_summary(response)
        report_parts.append(
            f"## {index}. [{title}]({url})\n"
            f"**作者：** {authors}\n\n"
            f"**会议/期刊：** {venue_name}"
            f"{f'（{venue_abbreviation}）' if venue_abbreviation != '—' else ''}\n\n"
            f"**CCF：** {ccf_rank}\n\n"
            f"**发表时间：** {publication_date}\n\n"
            f"**来源优先级：** {source_label}\n\n"
            f"**匹配研究方向：** {topics}\n\n"
            f"{summary}"
        )

    return "\n\n---\n\n".join(report_parts)


def update_history(papers):
    """将已成功推送的论文 ID 追加到历史记录。"""
    if not papers:
        return

    os.makedirs(os.path.dirname(HISTORY_FILE), exist_ok=True)
    with open(HISTORY_FILE, "a", encoding="utf-8") as file:
        for paper in papers:
            file.write(f"{paper['paperId']}\n")


def build_feishu_payload(content, timestamp=None):
    """构造带签名的飞书机器人交互卡片消息。"""
    if not FEISHU_BOT_SIGNKEY:
        raise RuntimeError("缺少 FEISHU_BOT_SIGNKEY GitHub Secret。")

    timestamp = str(timestamp or int(time.time()))
    string_to_sign = f"{timestamp}\n{FEISHU_BOT_SIGNKEY}".encode("utf-8")
    sign = base64.b64encode(
        hmac.new(string_to_sign, digestmod=hashlib.sha256).digest()
    ).decode("utf-8")
    return {
        "timestamp": timestamp,
        "sign": sign,
        "msg_type": "interactive",
        "card": {
            "config": {"wide_screen_mode": True},
            "header": {
                "title": {"tag": "plain_text", "content": "📚 每日论文追踪"},
                "template": "blue",
            },
            "elements": [{"tag": "markdown", "content": content}],
        },
    }


def push_to_feishu(content):
    """通过飞书机器人 Webhook 推送带签名的交互卡片。"""
    if not FEISHU_BOT_WEBHOOK:
        raise RuntimeError("缺少 FEISHU_BOT_WEBHOOK GitHub Secret。")

    response = requests.post(
        FEISHU_BOT_WEBHOOK,
        json=build_feishu_payload(content),
        timeout=REQUEST_TIMEOUT_SECONDS,
    )
    response.raise_for_status()
    response_body = response.json()
    if response_body.get("code") not in {None, 0}:
        raise RuntimeError(f"飞书机器人推送失败：{response_body}")


def build_empty_report():
    """生成无新论文时仍需发送的日报内容。"""
    return "## 今日论文追踪\n\n本次检索未发现符合条件且未推送过的新论文。"


if __name__ == "__main__":
    print("正在按关键词寻找最新论文...")
    new_papers = get_paper_recommendations()
    if new_papers:
        print(f"找到 {len(new_papers)} 篇论文，正在使用 {LLM_MODEL} 总结...")
        report = summarize_papers_with_llm(new_papers)
    else:
        print("今天没有发现未读的最新相关论文，仍将发送状态通知。")
        report = build_empty_report()

    print("正在推送到飞书...")
    push_to_feishu(report)
    if new_papers:
        update_history(new_papers)
    print("全部完成！")
