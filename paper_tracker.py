import json
import os
import time
from datetime import date, timedelta
from xml.etree import ElementTree

import requests
from openai import OpenAI

LLM_API_KEY = os.getenv("LLM_API_KEY")
SERVERCHAN_KEY = os.getenv("SERVERCHAN_KEY")
S2_PROXY_API_KEY = os.getenv(
    "S2_PROXY_API_KEY", "sk-EjxPNyglgeZrjtHZ1cAAZHUpZr5HHuYPutfkCrOz8sGnAfij"
)

HISTORY_FILE = "config/seen_papers.txt"
BLACKLIST_FILE = "config/blacklisted_venues.txt"
PREFERENCES_FILE = "config/paper_preferences.json"
S2_PROXY_BASE_URL = os.getenv("S2_PROXY_BASE_URL", "https://s2api.ominiai.cn/s2").rstrip(
    "/"
)
SEMANTIC_SCHOLAR_SEARCH_URL = f"{S2_PROXY_BASE_URL}/graph/v1/paper/search"
ARXIV_SEARCH_URL = "https://export.arxiv.org/api/query"
LLM_BASE_URL = "https://4Router.net"
LLM_MODEL = "gpt-5.6-sol"
REQUEST_TIMEOUT_SECONDS = 30
REQUEST_RETRIES = 3


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
        "preferred_security_venues",
        "preferred_ai_venues",
        "high_impact_journals",
        "search_lookback_days",
        "max_results_per_query",
        "max_papers_per_digest",
    }
    missing_keys = required_keys - preferences.keys()
    if missing_keys:
        raise ValueError(f"论文偏好配置缺少字段: {', '.join(sorted(missing_keys))}")
    if not preferences["topics"]:
        raise ValueError("论文偏好配置中至少需要一个研究主题。")
    return preferences


def request_semantic_scholar(params):
    """通过 S2 代理请求 Semantic Scholar；失败时返回 None 以启用备用源。"""
    headers = {
        "Authorization": f"Bearer {S2_PROXY_API_KEY}",
        "User-Agent": "DailyPaper keyword tracker",
    }
    for attempt in range(REQUEST_RETRIES):
        try:
            response = requests.get(
                SEMANTIC_SCHOLAR_SEARCH_URL,
                params=params,
                headers=headers,
                timeout=REQUEST_TIMEOUT_SECONDS,
            )
        except requests.RequestException as error:
            print(f"S2 代理网络请求失败：{error}")
            return None

        if response.status_code == 200:
            return response.json()
        if response.status_code == 429:
            retry_after = response.headers.get("Retry-After")
            retry_message = f"，建议等待 {retry_after} 秒" if retry_after else ""
            print(f"S2 代理请求被限流 (429){retry_message}。")
            return None
        if response.status_code not in {500, 502, 503, 504}:
            print(
                f"S2 代理请求失败 ({response.status_code})："
                f"{response.text[:300]}"
            )
            return None

        wait_seconds = 2**attempt
        print(
            f"S2 代理暂时不可用 ({response.status_code})，"
            f"{wait_seconds} 秒后重试..."
        )
        time.sleep(wait_seconds)

    print("S2 代理持续不可用，将改用 arXiv 备用检索。")
    return None


def request_arxiv(query, max_results):
    """通过 arXiv 的公开 API 搜索备用论文，并统一成 Semantic Scholar 字段。"""
    try:
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
        response.raise_for_status()
    except requests.RequestException as error:
        print(f"arXiv 备用检索失败：{error}")
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
    return (
        paper.get("venue")
        or publication_venue.get("name")
        or publication_venue.get("alternate_names", [""])[0]
        or "未知会议/期刊"
    ).strip()


def matches_any(venue_name, aliases):
    normalized_venue = venue_name.lower()
    return any(alias.lower() in normalized_venue for alias in aliases)


def source_priority(paper, preferences):
    """按照用户指定的来源优先级返回可排序的分组和中文标签。"""
    venue_name = get_venue_name(paper)
    external_ids = paper.get("externalIds") or {}

    if matches_any(venue_name, preferences["preferred_security_venues"]):
        return 4, "网络安全 CCF A"
    if matches_any(venue_name, preferences["preferred_ai_venues"]):
        return 3, "人工智能 CCF A / EMNLP"
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
    """先按来源优先级，再按发表日期和引用数排序。"""
    priority, _ = source_priority(paper, preferences)
    publication_date = paper_date(paper) or date.min
    citation_count = paper.get("citationCount") or 0
    topic_matches = len(paper.get("matchedTopics", []))
    return priority, topic_matches, publication_date, citation_count


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
    s2_proxy_available = True
    for topic in preferences["topics"]:
        topic_name = topic["name"]
        query = topic["query"]
        print(f"检索主题：{topic_name} ({query})")
        if s2_proxy_available:
            print("来源：S2 代理服务")
            result = request_semantic_scholar(
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
            stored_paper.setdefault("matchedTopics", []).append(topic_name)

    ranked_papers = sorted(
        papers_by_id.values(),
        key=lambda paper: rank_paper(paper, preferences),
        reverse=True,
    )
    selected_papers = ranked_papers[: preferences["max_papers_per_digest"]]
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


def extract_llm_summary(response):
    """兼容 OpenAI SDK 对象、OpenAI JSON 和代理直接返回的文本。"""
    if isinstance(response, str):
        text_response = response.strip()
        if not text_response:
            raise RuntimeError("LLM 返回了空字符串。")
        try:
            response = json.loads(text_response)
        except json.JSONDecodeError:
            return text_response

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

    if isinstance(content, str) and content.strip():
        return content.strip()
    if isinstance(content, list):
        text_parts = [
            item.get("text", "") if isinstance(item, dict) else getattr(item, "text", "")
            for item in content
        ]
        joined_text = "".join(text_parts).strip()
        if joined_text:
            return joined_text
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
        venue_name = get_venue_name(paper)
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
            f"**会议/期刊：** {venue_name}\n\n"
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


def push_to_wechat(content):
    """通过 Server 酱推送到微信。"""
    if not SERVERCHAN_KEY:
        raise RuntimeError("缺少 SERVERCHAN_KEY GitHub Secret。")

    response = requests.post(
        f"https://sctapi.ftqq.com/{SERVERCHAN_KEY}.send",
        data={"title": "📚 每日论文追踪", "desp": content},
        timeout=REQUEST_TIMEOUT_SECONDS,
    )
    response.raise_for_status()


if __name__ == "__main__":
    print("正在按关键词寻找最新论文...")
    new_papers = get_paper_recommendations()
    if new_papers:
        print(f"找到 {len(new_papers)} 篇论文，正在使用 {LLM_MODEL} 总结...")
        report = summarize_papers_with_llm(new_papers)
        print("正在推送到微信...")
        push_to_wechat(report)
        update_history(new_papers)
        print("全部完成！")
    else:
        print("今天没有发现未读的最新相关论文。")
