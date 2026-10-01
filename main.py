import json
import os
import re
import time
from datetime import datetime, timedelta, timezone
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup

import firebase_admin
from firebase_admin import credentials, firestore
from google.cloud.firestore_v1.base_query import FieldFilter

from google import genai
from google.genai import types


# ============================================================
# 기본 설정
# ============================================================

NAVER_ISSUES = [
    {
        "name": "반도체 전쟁",
        "url": "https://media.naver.com/issue/092/102",
        "query": "반도체",
    },
    {
        "name": "AI 핫트렌드",
        "url": "https://media.naver.com/issue/092/492",
        "query": "인공지능 AI",
    },
]

SEMIENGINEERING_AUTHOR_URL = (
    "https://semiengineering.com/author/se-staff/"
)

# Gemini 모델
GEMINI_MODEL = os.getenv(
    "GEMINI_MODEL",
    "gemini-3.8-flash",
)

# 카테고리별 저장 최대 개수
MAX_ARTICLES_PER_CATEGORY = int(
    os.getenv("MAX_ARTICLES_PER_CATEGORY", "5")
)

# 카테고리별 실제 Gemini 처리 시도 최대 개수
#
# 중요:
# 기존 코드는 "저장 성공 5개"를 만들기 위해
# 146개 후보를 계속 순회하면서 Gemini를 호출할 수 있었음.
#
# 이제는 새로운 기사 자체를 최대 5개까지만 시도한다.
MAX_NEW_ARTICLES_PER_CATEGORY = int(
    os.getenv("MAX_NEW_ARTICLES_PER_CATEGORY", "5")
)

# 한 번의 GitHub Actions 실행에서 Gemini API 호출 최대 횟수
#
# 503 재시도도 API 호출로 계산한다.
# 무료 quota가 20회인 환경을 고려하여 15회로 여유를 둔다.
MAX_GEMINI_CALLS_PER_RUN = int(
    os.getenv("MAX_GEMINI_CALLS_PER_RUN", "15")
)

# 기사 본문 최소 길이
MIN_ARTICLE_LENGTH = int(
    os.getenv("MIN_ARTICLE_LENGTH", "200")
)

# 7일보다 오래된 미북마크 기사 삭제
DELETE_AFTER_DAYS = int(
    os.getenv("DELETE_AFTER_DAYS", "7")
)

# HTTP timeout
HTTP_TIMEOUT = int(
    os.getenv("HTTP_TIMEOUT", "20")
)

# Gemini 503 재시도 횟수
MAX_GEMINI_503_RETRIES = int(
    os.getenv("MAX_GEMINI_503_RETRIES", "2")
)

# Gemini 503 재시도 간격
GEMINI_RETRY_DELAYS = [5, 10]


# ============================================================
# 전역 상태
# ============================================================

gemini_calls_used = 0

# 일일 quota 초과 등으로 Gemini를 더 이상 호출하지 않도록 하는 플래그
gemini_quota_exhausted = False


# ============================================================
# 예외 클래스
# ============================================================

class GeminiQuotaExceeded(Exception):
    """Gemini 일일 quota 또는 사용량 quota 초과."""

    pass


class GeminiCallBudgetExceeded(Exception):
    """이번 실행에서 허용한 Gemini API 호출 횟수 초과."""

    pass


class GeminiTemporaryError(Exception):
    """Gemini 일시적인 오류."""

    pass


# ============================================================
# HTTP 설정
# ============================================================

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/140.0.0.0 Safari/537.36"
    ),
    "Accept": (
        "text/html,application/xhtml+xml,application/xml;"
        "q=0.9,image/avif,image/webp,*/*;q=0.8"
    ),
    "Accept-Language": "ko-KR,ko;q=0.9,en-US;q=0.8,en;q=0.7",
    "Cache-Control": "no-cache",
}


session = requests.Session()
session.headers.update(HEADERS)


# ============================================================
# Firebase 초기화
# ============================================================

def init_firebase():
    """
    FIREBASE_SERVICE_ACCOUNT 환경변수의 JSON으로 Firebase 초기화.
    """

    service_account_json = os.getenv(
        "FIREBASE_SERVICE_ACCOUNT"
    )

    if not service_account_json:
        raise RuntimeError(
            "FIREBASE_SERVICE_ACCOUNT 환경변수가 없습니다."
        )

    try:
        service_account_info = json.loads(
            service_account_json
        )
    except json.JSONDecodeError as e:
        raise RuntimeError(
            "FIREBASE_SERVICE_ACCOUNT가 올바른 JSON이 아닙니다."
        ) from e

    if not firebase_admin._apps:
        cred = credentials.Certificate(
            service_account_info
        )

        firebase_admin.initialize_app(cred)

    return firestore.client()


# ============================================================
# Gemini 초기화
# ============================================================

def init_gemini():
    """
    Gemini Client 초기화.

    중요:
    google-genai SDK 자체의 자동 retry를 끈다.

    그래야 우리가
    - 429 quota
    - 429 rate limit
    - 503
    를 직접 구분하고 제어할 수 있다.
    """

    api_key = os.getenv("GEMINI_API_KEY")

    if not api_key:
        raise RuntimeError(
            "GEMINI_API_KEY 환경변수가 없습니다."
        )

    http_options = types.HttpOptions(
        retry_options=types.HttpRetryOptions(
            attempts=1
        )
    )

    client = genai.Client(
        api_key=api_key,
        http_options=http_options,
    )

    return client


# ============================================================
# URL 정리
# ============================================================

def clean_url(url):
    """
    URL을 비교/저장하기 위한 기본 정리.
    """

    if not url:
        return ""

    url = url.strip()

    if not url:
        return ""

    parsed = urlparse(url)

    # fragment 제거
    parsed = parsed._replace(fragment="")

    # Naver에서 간혹 ?iid& 형태가 나오므로
    # query 마지막의 &만 제거
    query = parsed.query.rstrip("&")
    parsed = parsed._replace(query=query)

    cleaned = parsed.geturl()

    # 마지막 / 제거
    if cleaned.endswith("/"):
        cleaned = cleaned[:-1]

    return cleaned


# ============================================================
# 페이지 가져오기
# ============================================================

def fetch_page(url):
    """
    HTTP GET.
    """

    try:
        response = session.get(
            url,
            timeout=HTTP_TIMEOUT,
            allow_redirects=True,
        )

        print(
            f"   HTTP {response.status_code}: "
            f"{response.url}"
        )

        response.raise_for_status()

        return response.text

    except requests.RequestException as e:
        print(
            f"   HTTP 요청 실패: {e}"
        )

        return None


# ============================================================
# Firestore 중복 확인
# ============================================================

def is_already_collected(db, url):
    """
    Firestore news-summary 컬렉션에
    동일 URL이 있는지 확인.
    """

    url = clean_url(url)

    if not url:
        return True

    try:
        docs = (
            db.collection("news-summary")
            .where(
                filter=FieldFilter(
                    "url",
                    "==",
                    url,
                )
            )
            .limit(1)
            .stream()
        )

        for _ in docs:
            return True

        return False

    except Exception as e:
        print(
            f"   Firestore 중복 확인 오류: {e}"
        )

        # Firestore 확인 자체가 실패하면
        # 중복 저장을 방지하기 위해 True 처리
        return True


# ============================================================
# Naver 기사 URL 판별
# ============================================================

def is_naver_article_url(url):
    """
    Naver 뉴스 실제 기사 URL만 통과.

    제외:
    - 댓글 URL
    - 기타 페이지
    """

    if not url:
        return False

    url = clean_url(url)

    parsed = urlparse(url)

    if parsed.netloc not in {
        "n.news.naver.com",
        "news.naver.com",
    }:
        return False

    path = parsed.path

    # 댓글 페이지 제외
    if "/article/comment/" in path:
        return False

    # 일반 기사
    if "/article/" in path:
        return True

    # 구형 /mnews/article/
    if "/mnews/article/" in path:
        return True

    return False


# ============================================================
# Naver 이슈 페이지 기사 링크 추출
# ============================================================

def extract_naver_issue_links(html, issue_url):
    """
    Naver 이슈 페이지에서 실제 기사 URL 추출.
    """

    if not html:
        return []

    soup = BeautifulSoup(
        html,
        "html.parser",
    )

    links = []
    seen = set()

    for a in soup.find_all("a", href=True):

        href = a.get("href")

        if not href:
            continue

        href = urljoin(
            issue_url,
            href,
        )

        href = clean_url(href)

        if not is_naver_article_url(href):
            continue

        if href in seen:
            continue

        seen.add(href)

        links.append(href)

    return links


# ============================================================
# Naver News API fallback
# ============================================================

def search_naver_news_api(query, display=20):
    """
    Naver 검색 API fallback.

    NAVER_CLIENT_ID / NAVER_CLIENT_SECRET이 없는 경우
    빈 리스트 반환.
    """

    client_id = os.getenv(
        "NAVER_CLIENT_ID"
    )

    client_secret = os.getenv(
        "NAVER_CLIENT_SECRET"
    )

    if not client_id or not client_secret:
        return []

    api_url = (
        "https://openapi.naver.com/v1/search/news.json"
    )

    headers = {
        "X-Naver-Client-Id": client_id,
        "X-Naver-Client-Secret": client_secret,
    }

    params = {
        "query": query,
        "display": display,
        "sort": "date",
    }

    try:
        response = requests.get(
            api_url,
            headers=headers,
            params=params,
            timeout=HTTP_TIMEOUT,
        )

        response.raise_for_status()

        data = response.json()

        results = []

        for item in data.get("items", []):
            link = item.get("link")

            if not link:
                continue

            link = clean_url(link)

            if not is_naver_article_url(link):
                continue

            results.append(link)

        return results

    except Exception as e:
        print(
            f"   Naver API fallback 실패: {e}"
        )

        return []


# ============================================================
# Naver 기사 본문 추출
# ============================================================

def extract_naver_article(html):
    """
    Naver 뉴스 기사 제목/본문 추출.
    """

    if not html:
        return None, None

    soup = BeautifulSoup(
        html,
        "html.parser",
    )

    # 제목
    title = None

    title_selectors = [
        "h2#title_area",
        "h2.media_end_head_headline",
        "h2#title_area",
        "meta[property='og:title']",
        "title",
    ]

    for selector in title_selectors:

        element = soup.select_one(selector)

        if not element:
            continue

        if element.name == "meta":
            title = element.get("content")
        else:
            title = element.get_text(
                " ",
                strip=True,
            )

        if title:
            break

    # 본문
    body = None

    body_selectors = [
        "#dic_area",
        "article#dic_area",
        "div#dic_area",
        "div._article_content",
        "article",
    ]

    for selector in body_selectors:

        element = soup.select_one(selector)

        if not element:
            continue

        text = element.get_text(
            "\n",
            strip=True,
        )

        if len(text) >= MIN_ARTICLE_LENGTH:
            body = text
            break

    if not body:
        # 최후 fallback
        paragraphs = []

        for p in soup.find_all("p"):
            text = p.get_text(
                " ",
                strip=True,
            )

            if len(text) >= 20:
                paragraphs.append(text)

        body = "\n".join(paragraphs)

    if title:
        title = re.sub(
            r"\s+",
            " ",
            title,
        ).strip()

    if body:
        body = re.sub(
            r"\n{3,}",
            "\n\n",
            body,
        )

        body = re.sub(
            r"[ \t]+",
            " ",
            body,
        ).strip()

    return title, body


# ============================================================
# SemiEngineering 링크 추출
# ============================================================

def extract_semiengineering_links(html):
    """
    SemiEngineering author 페이지에서
    기사 링크 추출.
    """

    if not html:
        return []

    soup = BeautifulSoup(
        html,
        "html.parser",
    )

    links = []
    seen = set()

    for a in soup.find_all("a", href=True):

        href = a.get("href")

        if not href:
            continue

        href = urljoin(
            SEMIENGINEERING_AUTHOR_URL,
            href,
        )

        href = clean_url(href)

        parsed = urlparse(href)

        if parsed.netloc not in {
            "semiengineering.com",
            "www.semiengineering.com",
        }:
            continue

        path = parsed.path.lower()

        if not path or path == "/":
            continue

        # author 페이지 자체 제외
        if "/author/" in path:
            continue

        # category/tag/search 등 제외
        excluded_paths = [
            "/category/",
            "/tag/",
            "/search/",
            "/page/",
            "/contact/",
            "/about/",
        ]

        if any(
            path.startswith(prefix)
            for prefix in excluded_paths
        ):
            continue

        if href in seen:
            continue

        seen.add(href)

        links.append(href)

    return links


# ============================================================
# 기사 본문 일반 추출
# ============================================================

def extract_generic_article(html):
    """
    SemiEngineering 등 일반 사이트 기사 본문 추출.
    """

    if not html:
        return None, None

    soup = BeautifulSoup(
        html,
        "html.parser",
    )

    # 제목
    title = None

    title_selectors = [
        "h1.entry-title",
        "h1.post-title",
        "article h1",
        "h1",
        "meta[property='og:title']",
        "title",
    ]

    for selector in title_selectors:

        element = soup.select_one(selector)

        if not element:
            continue

        if element.name == "meta":
            title = element.get("content")
        else:
            title = element.get_text(
                " ",
                strip=True,
            )

        if title:
            break

    # 본문
    article = None

    body_selectors = [
        "article",
        ".entry-content",
        ".post-content",
        ".article-content",
        ".td-post-content",
        "main",
    ]

    for selector in body_selectors:

        element = soup.select_one(selector)

        if not element:
            continue

        text = element.get_text(
            "\n",
            strip=True,
        )

        if len(text) >= MIN_ARTICLE_LENGTH:
            article = text
            break

    if not article:
        paragraphs = []

        for p in soup.find_all("p"):

            text = p.get_text(
                " ",
                strip=True,
            )

            if len(text) >= 20:
                paragraphs.append(text)

        article = "\n".join(paragraphs)

    if title:
        title = re.sub(
            r"\s+",
            " ",
            title,
        ).strip()

    if article:
        article = re.sub(
            r"\n{3,}",
            "\n\n",
            article,
        )

        article = re.sub(
            r"[ \t]+",
            " ",
            article,
        ).strip()

    return title, article


# ============================================================
# Gemini 오류 문자열 분석
# ============================================================

def get_gemini_error_text(error):
    """
    Gemini 예외를 문자열로 안전하게 변환.
    """

    try:
        return str(error)
    except Exception:
        return repr(error)


def is_quota_exceeded_error(error):
    """
    429 중에서도 일일 quota / 무료 quota 초과인지 판별.
    """

    text = get_gemini_error_text(error).lower()

    quota_keywords = [
        "quota exceeded",
        "quota_exceeded",
        "daily quota",
        "free_tier_requests",
        "per day",
        "daily limit",
        "exceeded your current quota",
        "resource_exhausted",
    ]

    return any(
        keyword in text
        for keyword in quota_keywords
    )


def is_rate_limit_error(error):
    """
    짧은 시간 내 요청량 제한인지 판별.
    """

    text = get_gemini_error_text(error).lower()

    rate_keywords = [
        "rate limit",
        "rate_limit_exceeded",
        "too many requests",
        "too_many_requests",
        "requests per minute",
        "rpm",
        "requests per second",
    ]

    return any(
        keyword in text
        for keyword in rate_keywords
    )


def is_503_error(error):
    """
    Gemini 503 / UNAVAILABLE 여부.
    """

    text = get_gemini_error_text(error).lower()

    return (
        "503" in text
        or "unavailable" in text
        or "service unavailable" in text
    )


# ============================================================
# Gemini API 호출
# ============================================================

def call_gemini_once(client, prompt):
    """
    Gemini API를 딱 한 번 호출.

    이 함수에서 호출 횟수를 전역으로 관리한다.
    """

    global gemini_calls_used
    global gemini_quota_exhausted

    if gemini_quota_exhausted:
        raise GeminiQuotaExceeded(
            "이번 실행에서는 Gemini quota가 이미 "
            "초과되어 추가 호출을 하지 않습니다."
        )

    if gemini_calls_used >= MAX_GEMINI_CALLS_PER_RUN:
        raise GeminiCallBudgetExceeded(
            f"이번 실행의 Gemini 호출 제한 "
            f"{MAX_GEMINI_CALLS_PER_RUN}회에 도달했습니다."
        )

    # 실제 API 호출 직전에 카운트
    gemini_calls_used += 1

    print(
        f"   Gemini API 호출 "
        f"{gemini_calls_used}/"
        f"{MAX_GEMINI_CALLS_PER_RUN}"
    )

    try:
        response = client.models.generate_content(
            model=GEMINI_MODEL,
            contents=prompt,
        )

        return response

    except Exception as e:

        # ----------------------------------------------------
        # 429 quota 초과
        # ----------------------------------------------------
        if is_quota_exceeded_error(e):

            gemini_quota_exhausted = True

            print(
                "   ❌ Gemini 429 quota 초과 감지"
            )

            print(
                "   ❌ 이번 실행의 Gemini 호출을 "
                "즉시 중단합니다."
            )

            raise GeminiQuotaExceeded(
                get_gemini_error_text(e)
            ) from e

        # ----------------------------------------------------
        # 429 단기 rate limit
        # ----------------------------------------------------
        if is_rate_limit_error(e):

            print(
                "   ⚠️ Gemini 429 단기 rate limit 감지"
            )

            raise GeminiTemporaryError(
                get_gemini_error_text(e)
            ) from e

        # ----------------------------------------------------
        # 503
        # ----------------------------------------------------
        if is_503_error(e):

            print(
                "   ⚠️ Gemini 503 UNAVAILABLE 감지"
            )

            raise GeminiTemporaryError(
                get_gemini_error_text(e)
            ) from e

        # ----------------------------------------------------
        # 기타 오류
        # ----------------------------------------------------
        raise


# ============================================================
# Gemini 요약
# ============================================================

def summarize_with_gemini(client, title, body):
    """
    기사 본문을 한국어로 요약.

    출력:
    - 요약 3줄
    - 핵심 인사이트 1줄

    429 quota:
        즉시 중단

    429 단기 rate limit:
        제한적으로 재시도

    503:
        제한적으로 재시도
    """

    if not title:
        title = "제목 없음"

    if not body:
        raise ValueError(
            "본문이 없습니다."
        )

    # 지나치게 긴 본문은 일정 길이까지만 전달
    #
    # 기사 전체를 무조건 전송하지 않고
    # 요약에 필요한 충분한 분량만 전달한다.
    max_body_chars = 12000

    body_for_prompt = body[:max_body_chars]

    prompt = f"""
다음 뉴스 기사를 한국어로 요약해 주세요.

[기사 제목]
{title}

[기사 본문]
{body_for_prompt}

반드시 아래 형식으로만 작성하세요.

요약:
1. 첫 번째 핵심 내용
2. 두 번째 핵심 내용
3. 세 번째 핵심 내용

인사이트:
한 줄로 작성하되, 이 기사가 반도체/AI 산업 또는 관련 기업에 어떤 의미가 있는지 설명하세요.

주의:
- 기사에 없는 사실을 만들어내지 마세요.
- 과도한 추측은 하지 마세요.
- 각 요약은 한 문장 중심으로 간결하게 작성하세요.
- 반드시 한국어로 작성하세요.
"""

    last_error = None

    # 총 시도 횟수는 API budget 안에서만 수행
    #
    # 503 / 단기 429 재시도도 각각 API 호출로 계산됨.
    max_attempts = 1 + MAX_GEMINI_503_RETRIES

    for attempt in range(1, max_attempts + 1):

        print(
            f"   Gemini 요약 시도 "
            f"{attempt}/{max_attempts}"
        )

        try:
            response = call_gemini_once(
                client,
                prompt,
            )

            text = getattr(
                response,
                "text",
                None,
            )

            if not text:
                raise RuntimeError(
                    "Gemini 응답에 text가 없습니다."
                )

            text = text.strip()

            if not text:
                raise RuntimeError(
                    "Gemini 응답이 비어 있습니다."
                )

            return text

        except GeminiQuotaExceeded:
            # 절대 재시도하지 않는다.
            raise

        except GeminiCallBudgetExceeded:
            # 전체 실행 예산 초과
            raise

        except GeminiTemporaryError as e:

            last_error = e

            # 마지막 시도면 종료
            if attempt >= max_attempts:
                break

            # 503 / rate limit에 대해서만 재시도
            delay_index = min(
                attempt - 1,
                len(GEMINI_RETRY_DELAYS) - 1,
            )

            delay = GEMINI_RETRY_DELAYS[
                delay_index
            ]

            print(
                f"   ⏳ Gemini 재시도 전 "
                f"{delay}초 대기"
            )

            time.sleep(delay)

            continue

        except Exception as e:

            # 알 수 없는 오류는 재시도하지 않는다.
            last_error = e

            print(
                f"   ❌ Gemini 오류: {e}"
            )

            break

    if last_error:
        raise last_error

    raise RuntimeError(
        "Gemini 요약에 실패했습니다."
    )


# ============================================================
# Firestore 저장
# ============================================================

def save_article(
    db,
    category,
    title,
    url,
    summary,
    source,
):
    """
    news-summary 컬렉션에 기사 저장.
    """

    url = clean_url(url)

    data = {
        "category": category,
        "title": title,
        "url": url,
        "summary": summary,
        "source": source,
        "bookmarked": False,
        "createdAt": firestore.SERVER_TIMESTAMP,
        "collectedAt": firestore.SERVER_TIMESTAMP,
    }

    db.collection(
        "news-summary"
    ).add(data)

    print(
        "   ✅ Firestore 저장 완료"
    )


# ============================================================
# Naver 카테고리 수집
# ============================================================

def collect_naver_category(
    db,
    gemini_client,
    issue,
):
    """
    Naver 이슈 카테고리 하나 수집.

    중요한 변경점:

    기존:
        성공 5개를 채울 때까지
        수십~수백 개 URL을 순회할 수 있음.

    현재:
        새로운 기사 최대 5개까지만 실제 처리.
    """

    global gemini_quota_exhausted

    category_name = issue["name"]
    issue_url = issue["url"]
    query = issue["query"]

    print()
    print("=" * 70)
    print(
        f"[{category_name}] 수집 시작"
    )
    print("=" * 70)

    html = fetch_page(issue_url)

    links = extract_naver_issue_links(
        html,
        issue_url,
    )

    print(
        f"   Naver 이슈 페이지에서 "
        f"{len(links)}개 기사 후보 발견"
    )

    # 페이지 자체에서 링크를 못 찾은 경우
    # Naver API fallback
    if not links:

        print(
            "   ⚠️ 이슈 페이지에서 기사를 찾지 못했습니다."
        )

        print(
            "   Naver News API fallback 시도"
        )

        links = search_naver_news_api(
            query,
            display=20,
        )

        print(
            f"   API에서 "
            f"{len(links)}개 기사 후보 발견"
        )

    if not links:
        print(
            "   ❌ 수집 가능한 기사가 없습니다."
        )
        return 0

    saved_count = 0
    attempted_new_count = 0

    for index, url in enumerate(
        links,
        start=1,
    ):

        print()
        print(
            f"--- [{category_name}] "
            f"{index}/{len(links)} ---"
        )

        print(
            f"URL: {url}"
        )

        # ----------------------------------------------------
        # 이번 실행에서 Gemini quota가 소진되었다면
        # 더 이상 기사 처리하지 않는다.
        # ----------------------------------------------------
        if gemini_quota_exhausted:

            print(
                "   🛑 Gemini quota 초과 상태이므로 "
                "카테고리 수집 중단"
            )

            break

        # ----------------------------------------------------
        # 저장 성공 개수 제한
        # ----------------------------------------------------
        if saved_count >= MAX_ARTICLES_PER_CATEGORY:

            print(
                f"   📌 카테고리별 최대 "
                f"{MAX_ARTICLES_PER_CATEGORY}개 "
                "저장 완료"
            )

            break

        # ----------------------------------------------------
        # 새 기사 처리 시도 횟수 제한
        #
        # 중요:
        # 146개 URL을 끝까지 훑지 않는다.
        # ----------------------------------------------------
        if (
            attempted_new_count
            >= MAX_NEW_ARTICLES_PER_CATEGORY
        ):

            print(
                f"   📌 새 기사 처리 최대 "
                f"{MAX_NEW_ARTICLES_PER_CATEGORY}개 "
                "도달"
            )

            break

        url = clean_url(url)

        if not url:
            continue

        # ----------------------------------------------------
        # Firestore 중복 확인
        # ----------------------------------------------------
        if is_already_collected(
            db,
            url,
        ):

            print(
                "   ⏭️ 이미 수집된 기사 → 건너뜀"
            )

            continue

        # 여기부터는 실제 신규 기사 처리
        attempted_new_count += 1

        # ----------------------------------------------------
        # 기사 페이지 가져오기
        # ----------------------------------------------------
        article_html = fetch_page(url)

        if not article_html:
            print(
                "   ❌ 기사 HTML을 가져오지 못했습니다."
            )
            continue

        # ----------------------------------------------------
        # 기사 추출
        # ----------------------------------------------------
        title, body = extract_naver_article(
            article_html
        )

        print(
            f"   제목: {title}"
        )

        body_length = len(body or "")

        print(
            f"   본문 길이: "
            f"{body_length}자"
        )

        if not title:
            print(
                "   ❌ 제목 추출 실패"
            )
            continue

        if not body:
            print(
                "   ❌ 본문 추출 실패"
            )
            continue

        if body_length < MIN_ARTICLE_LENGTH:
            print(
                f"   ⏭️ 본문이 너무 짧음 "
                f"({MIN_ARTICLE_LENGTH}자 미만)"
            )
            continue

        # ----------------------------------------------------
        # Gemini 요약
        # ----------------------------------------------------
        print(
            "   Gemini 요약 시작"
        )

        try:
            summary = summarize_with_gemini(
                gemini_client,
                title,
                body,
            )

        except GeminiQuotaExceeded:

            print(
                "   🛑 Gemini quota 초과."
            )

            print(
                "   🛑 이후 기사에서는 "
                "Gemini를 호출하지 않습니다."
            )

            break

        except GeminiCallBudgetExceeded:

            print(
                "   🛑 이번 실행의 Gemini 호출 "
                "예산에 도달했습니다."
            )

            break

        except Exception as e:

            print(
                f"   ❌ Gemini 요약 실패: {e}"
            )

            continue

        # ----------------------------------------------------
        # Firestore 저장
        # ----------------------------------------------------
        try:

            save_article(
                db=db,
                category=category_name,
                title=title,
                url=url,
                summary=summary,
                source="Naver",
            )

            saved_count += 1

        except Exception as e:

            print(
                f"   ❌ Firestore 저장 실패: {e}"
            )

            continue

    print()
    print(
        f"[{category_name}] "
        f"수집 완료: "
        f"{saved_count}개 저장"
    )

    return saved_count


# ============================================================
# SemiEngineering 수집
# ============================================================

def collect_semiengineering(
    db,
    gemini_client,
):
    """
    SemiEngineering author 페이지 수집.

    페이지에서 최신 기사 후보를 찾고
    신규 기사 중 첫 번째 처리 가능한 기사만 저장.
    """

    global gemini_quota_exhausted

    print()
    print("=" * 70)
    print("[SemiEngineering] 수집 시작")
    print("=" * 70)

    if gemini_quota_exhausted:

        print(
            "   🛑 Gemini quota 초과 상태이므로 "
            "SemiEngineering 수집을 건너뜁니다."
        )

        return 0

    html = fetch_page(
        SEMIENGINEERING_AUTHOR_URL
    )

    links = extract_semiengineering_links(
        html
    )

    print(
        f"   기사 후보 "
        f"{len(links)}개 발견"
    )

    if not links:
        print(
            "   ❌ SemiEngineering 기사를 찾지 못했습니다."
        )
        return 0

    attempted = 0

    for index, url in enumerate(
        links,
        start=1,
    ):

        print()
        print(
            f"--- [SemiEngineering] "
            f"{index}/{len(links)} ---"
        )

        print(
            f"URL: {url}"
        )

        if gemini_quota_exhausted:

            print(
                "   🛑 Gemini quota 초과 상태."
            )

            break

        # 신규 기사만 처리
        if is_already_collected(
            db,
            url,
        ):

            print(
                "   ⏭️ 이미 수집된 기사 → 건너뜀"
            )

            continue

        # SemiEngineering에서는
        # 너무 많은 후보를 계속 처리하지 않는다.
        attempted += 1

        if attempted > 1:

            print(
                "   📌 신규 기사 1개 처리 완료/시도. "
                "이번 실행에서는 종료."
            )

            break

        # ----------------------------------------------------
        # 기사 가져오기
        # ----------------------------------------------------
        article_html = fetch_page(
            url
        )

        if not article_html:
            continue

        title, body = extract_generic_article(
            article_html
        )

        print(
            f"   제목: {title}"
        )

        print(
            f"   본문 길이: "
            f"{len(body or '')}자"
        )

        if not title:
            print(
                "   ❌ 제목 추출 실패"
            )
            continue

        if not body:
            print(
                "   ❌ 본문 추출 실패"
            )
            continue

        if len(body) < MIN_ARTICLE_LENGTH:
            print(
                f"   ⏭️ 본문이 너무 짧음 "
                f"({MIN_ARTICLE_LENGTH}자 미만)"
            )
            continue

        # ----------------------------------------------------
        # Gemini 요약
        # ----------------------------------------------------
        print(
            "   Gemini 요약 시작"
        )

        try:

            summary = summarize_with_gemini(
                gemini_client,
                title,
                body,
            )

        except GeminiQuotaExceeded:

            print(
                "   🛑 Gemini quota 초과."
            )

            break

        except GeminiCallBudgetExceeded:

            print(
                "   🛑 Gemini 호출 예산 초과."
            )

            break

        except Exception as e:

            print(
                f"   ❌ Gemini 요약 실패: {e}"
            )

            continue

        # ----------------------------------------------------
        # Firestore 저장
        # ----------------------------------------------------
        try:

            save_article(
                db=db,
                category="SemiEngineering",
                title=title,
                url=url,
                summary=summary,
                source="SemiEngineering",
            )

            return 1

        except Exception as e:

            print(
                f"   ❌ Firestore 저장 실패: {e}"
            )

            return 0

    return 0


# ============================================================
# 오래된 기사 삭제
# ============================================================

def cleanup_old_articles(db):
    """
    7일보다 오래된 기사 중
    bookmarked == True가 아닌 기사 삭제.

    createdAt 또는 collectedAt을 사용.
    """

    print()
    print("=" * 70)
    print("[Cleanup] 오래된 기사 정리 시작")
    print("=" * 70)

    cutoff = datetime.now(
        timezone.utc
    ) - timedelta(
        days=DELETE_AFTER_DAYS
    )

    deleted_count = 0

    try:

        docs = (
            db.collection("news-summary")
            .where(
                filter=FieldFilter(
                    "createdAt",
                    "<",
                    cutoff,
                )
            )
            .stream()
        )

        for doc in docs:

            data = doc.to_dict()

            bookmarked = data.get(
                "bookmarked",
                False,
            )

            # 북마크된 기사는 절대 삭제하지 않음
            if bookmarked:
                continue

            db.collection(
                "news-summary"
            ).document(
                doc.id
            ).delete()

            deleted_count += 1

        print(
            f"   🗑️ 삭제 완료: "
            f"{deleted_count}개"
        )

    except Exception as e:

        print(
            f"   ❌ 오래된 기사 삭제 실패: {e}"
        )

    return deleted_count


# ============================================================
# 실행 정보 출력
# ============================================================

def print_config():
    print()
    print("=" * 70)
    print("뉴스 수집기 실행 설정")
    print("=" * 70)

    print(
        f"Gemini 모델: {GEMINI_MODEL}"
    )

    print(
        f"카테고리별 저장 최대: "
        f"{MAX_ARTICLES_PER_CATEGORY}"
    )

    print(
        f"카테고리별 신규 기사 처리 최대: "
        f"{MAX_NEW_ARTICLES_PER_CATEGORY}"
    )

    print(
        f"실행당 Gemini API 최대 호출: "
        f"{MAX_GEMINI_CALLS_PER_RUN}"
    )

    print(
        f"본문 최소 길이: "
        f"{MIN_ARTICLE_LENGTH}"
    )

    print(
        f"삭제 기준: "
        f"{DELETE_AFTER_DAYS}일"
    )

    print(
        "=" * 70
    )


# ============================================================
# 메인
# ============================================================

def main():

    global gemini_quota_exhausted

    start_time = time.time()

    print()
    print("=" * 70)
    print("뉴스 수집기 시작")
    print(
        datetime.now().strftime(
            "%Y-%m-%d %H:%M:%S"
        )
    )
    print("=" * 70)

    print_config()

    # --------------------------------------------------------
    # Firebase
    # --------------------------------------------------------
    try:

        db = init_firebase()

        print(
            "✅ Firebase 초기화 완료"
        )

    except Exception as e:

        print(
            f"❌ Firebase 초기화 실패: {e}"
        )

        raise

    # --------------------------------------------------------
    # Gemini
    # --------------------------------------------------------
    try:

        gemini_client = init_gemini()

        print(
            "✅ Gemini 초기화 완료"
        )

    except Exception as e:

        print(
            f"❌ Gemini 초기화 실패: {e}"
        )

        raise

    total_saved = 0

    # --------------------------------------------------------
    # Naver
    # --------------------------------------------------------
    for issue in NAVER_ISSUES:

        # Gemini quota가 이미 소진되었다면
        # 뒤의 카테고리에서는 Gemini 호출하지 않음.
        if gemini_quota_exhausted:

            print()
            print(
                "🛑 Gemini quota 초과 상태."
            )

            print(
                "🛑 남은 Naver 카테고리는 "
                "Gemini 호출 없이 건너뜁니다."
            )

            break

        try:

            saved = collect_naver_category(
                db=db,
                gemini_client=gemini_client,
                issue=issue,
            )

            total_saved += saved

        except GeminiQuotaExceeded:

            gemini_quota_exhausted = True

            print(
                "🛑 Gemini quota 초과."
            )

            break

        except GeminiCallBudgetExceeded:

            print(
                "🛑 Gemini 호출 예산 초과."
            )

            break

        except Exception as e:

            print(
                f"❌ [{issue['name']}] "
                f"수집 중 오류: {e}"
            )

            continue

    # --------------------------------------------------------
    # SemiEngineering
    # --------------------------------------------------------
    if not gemini_quota_exhausted:

        try:

            saved = collect_semiengineering(
                db=db,
                gemini_client=gemini_client,
            )

            total_saved += saved

        except GeminiQuotaExceeded:

            gemini_quota_exhausted = True

            print(
                "🛑 Gemini quota 초과."
            )

        except GeminiCallBudgetExceeded:

            print(
                "🛑 Gemini 호출 예산 초과."
            )

        except Exception as e:

            print(
                f"❌ SemiEngineering 수집 오류: {e}"
            )

    else:

        print(
            "🛑 Gemini quota 초과로 "
            "SemiEngineering 수집을 건너뜁니다."
        )

    # --------------------------------------------------------
    # Cleanup
    #
    # Gemini quota와 관계없이 실행
    # --------------------------------------------------------
    try:

        cleanup_old_articles(db)

    except Exception as e:

        print(
            f"❌ Cleanup 오류: {e}"
        )

    # --------------------------------------------------------
    # 결과
    # --------------------------------------------------------
    elapsed = time.time() - start_time

    print()
    print("=" * 70)
    print("뉴스 수집기 종료")
    print("=" * 70)

    print(
        f"총 저장 기사: {total_saved}개"
    )

    print(
        f"Gemini API 호출: "
        f"{gemini_calls_used}/"
        f"{MAX_GEMINI_CALLS_PER_RUN}"
    )

    if gemini_quota_exhausted:

        print(
            "Gemini 상태: "
            "429 quota 초과로 중단"
        )

    else:

        print(
            "Gemini 상태: 정상"
        )

    print(
        f"실행 시간: {elapsed:.1f}초"
    )

    print("=" * 70)


# ============================================================
# 프로그램 실행
# ============================================================

if __name__ == "__main__":
    main()
