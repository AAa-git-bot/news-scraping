import os
import re
import time
from datetime import datetime, timedelta, timezone
from urllib.parse import urljoin, urlparse, urlunparse

import requests
from bs4 import BeautifulSoup

import firebase_admin
from firebase_admin import credentials, firestore
from google.cloud.firestore_v1.base_query import FieldFilter

from google import genai


# ============================================================
# 기본 설정
# ============================================================

KST = timezone(timedelta(hours=9))

GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-3.8-flash")

MAX_ARTICLES_PER_CATEGORY = 5
MIN_ARTICLE_LENGTH = 200
DELETE_AFTER_DAYS = 7

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


# ============================================================
# HTTP 설정
# ============================================================

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/131.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "ko-KR,ko;q=0.9,en-US;q=0.8,en;q=0.7",
}


session = requests.Session()
session.headers.update(HEADERS)


# ============================================================
# Firebase 초기화
# ============================================================

def init_firestore():
    service_account_json = os.getenv("FIREBASE_SERVICE_ACCOUNT")

    if not service_account_json:
        raise RuntimeError(
            "FIREBASE_SERVICE_ACCOUNT 환경변수가 없습니다."
        )

    if not firebase_admin._apps:
        cred = credentials.Certificate(
            eval_service_account_json(service_account_json)
        )

        firebase_admin.initialize_app(cred)

    return firestore.client()


def eval_service_account_json(value):
    """
    GitHub Secret에 저장된 Firebase Service Account JSON을
    안전하게 JSON으로 파싱한다.
    """

    import json

    try:
        return json.loads(value)
    except json.JSONDecodeError as e:
        raise RuntimeError(
            "FIREBASE_SERVICE_ACCOUNT가 올바른 JSON 형식이 아닙니다."
        ) from e


db = init_firestore()


# ============================================================
# Gemini 초기화
# ============================================================

GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")

if not GEMINI_API_KEY:
    raise RuntimeError(
        "GEMINI_API_KEY 환경변수가 없습니다."
    )

gemini_client = genai.Client(
    api_key=GEMINI_API_KEY
)


# ============================================================
# 공통 함수
# ============================================================

def now_kst():
    return datetime.now(KST)


def clean_url(url):
    """
    Markdown 링크 형태가 들어와도 실제 URL만 추출한다.

    예:
    [https://example.com](https://example.com)
    ->
    https://example.com
    """

    if not url:
        return ""

    url = url.strip()

    markdown_match = re.match(
        r"^\[.*?\]\((https?://[^)]+)\)$",
        url
    )

    if markdown_match:
        url = markdown_match.group(1)

    # 혹시 문자열 중간에 Markdown URL이 들어온 경우
    if "](" in url and url.endswith(")"):
        match = re.search(
            r"\((https?://[^)]+)\)$",
            url
        )

        if match:
            url = match.group(1)

    # Naver 모바일 URL 정규화
    url = url.replace(
        "https://m.news.naver.com",
        "https://n.news.naver.com"
    )

    return url


def fetch_page(url, timeout=30):
    """
    웹 페이지 요청.
    """

    url = clean_url(url)

    print(f"   🌐 요청: {url}")

    response = session.get(
        url,
        timeout=timeout,
        allow_redirects=True,
    )

    print(
        f"   HTTP {response.status_code} | "
        f"최종 URL: {response.url} | "
        f"HTML: {len(response.text):,} bytes"
    )

    response.raise_for_status()

    return response.text, response.url


# ============================================================
# Naver URL 필터
# ============================================================

def is_naver_article_url(url):
    """
    실제 네이버 뉴스 기사 URL만 허용한다.

    허용:
      /article/092/000...
      /mnews/article/...

    제외:
      /article/comment/...
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

    # 댓글 URL은 반드시 제외
    if "/article/comment/" in parsed.path:
        return False

    # 실제 기사 URL
    if "/article/" in parsed.path:
        return True

    if "/mnews/article/" in parsed.path:
        return True

    return False


# ============================================================
# Firestore 중복 확인
# ============================================================

def is_already_collected(url):
    """
    URL이 이미 news-summary 컬렉션에 있는지 확인한다.
    """

    url = clean_url(url)

    query = (
        db.collection("news-summary")
        .where(
            filter=FieldFilter(
                "url",
                "==",
                url,
            )
        )
        .limit(1)
    )

    docs = list(query.stream())

    return len(docs) > 0


# ============================================================
# Naver 이슈 페이지에서 기사 URL 추출
# ============================================================

def extract_naver_issue_links(issue_url):
    print()
    print("🔎 네이버 이슈 페이지 분석")

    html, final_url = fetch_page(issue_url)

    soup = BeautifulSoup(
        html,
        "html.parser"
    )

    found_urls = []

    # --------------------------------------------------------
    # HTML <a> 태그 분석
    # --------------------------------------------------------

    for a in soup.find_all("a", href=True):

        href = a.get("href", "").strip()

        if not href:
            continue

        href = clean_url(
            urljoin(final_url, href)
        )

        if is_naver_article_url(href):
            found_urls.append(href)

    # --------------------------------------------------------
    # Raw HTML에서 URL 패턴 추가 탐색
    # --------------------------------------------------------

    raw_urls = re.findall(
        r'https?://(?:n\.news\.naver\.com|news\.naver\.com)'
        r'/[^"\'>\s]+',
        html,
    )

    for url in raw_urls:

        url = clean_url(url)

        if is_naver_article_url(url):
            found_urls.append(url)

    # --------------------------------------------------------
    # 중복 제거
    # --------------------------------------------------------

    unique_urls = []

    seen = set()

    for url in found_urls:

        url = clean_url(url)

        if not is_naver_article_url(url):
            continue

        # query parameter에서 iid 등은 유지하되
        # 동일 기사 URL은 중복 제거
        parsed = urlparse(url)

        normalized = urlunparse(
            (
                parsed.scheme,
                parsed.netloc,
                parsed.path,
                "",
                parsed.query,
                "",
            )
        )

        if normalized not in seen:
            seen.add(normalized)
            unique_urls.append(normalized)

    print(
        f"   ✅ 발견한 Naver 기사 URL: "
        f"{len(unique_urls)}개"
    )

    for i, url in enumerate(
        unique_urls[:10],
        start=1
    ):
        print(f"      {i}. {url}")

    return unique_urls


# ============================================================
# Naver Search API fallback
# ============================================================

def search_naver_news_api(query, display=20):
    """
    Naver 뉴스 검색 API fallback.
    이슈 페이지에서 URL을 충분히 가져오지 못하는 경우 사용한다.
    """

    client_id = os.getenv("NAVER_CLIENT_ID")
    client_secret = os.getenv("NAVER_CLIENT_SECRET")

    if not client_id or not client_secret:
        print(
            "   ⚠️ NAVER_CLIENT_ID / "
            "NAVER_CLIENT_SECRET이 없어 API 검색을 건너뜁니다."
        )
        return []

    print(
        f"   🔎 Naver Search API 검색: {query}"
    )

    api_url = "https://openapi.naver.com/v1/search/news.json"

    headers = {
        "X-Naver-Client-Id": client_id,
        "X-Naver-Client-Secret": client_secret,
    }

    params = {
        "query": query,
        "display": display,
        "sort": "date",
    }

    response = session.get(
        api_url,
        headers=headers,
        params=params,
        timeout=30,
    )

    response.raise_for_status()

    data = response.json()

    results = []

    for item in data.get("items", []):

        link = clean_url(
            item.get("originallink")
            or item.get("link")
            or ""
        )

        if is_naver_article_url(link):
            results.append(link)

    return results


# ============================================================
# Naver 기사 본문 추출
# ============================================================

def extract_article(url):
    print()
    print("📄 기사 본문 추출")
    print(f"   URL: {url}")

    html, final_url = fetch_page(url)

    soup = BeautifulSoup(
        html,
        "html.parser"
    )

    # --------------------------------------------------------
    # 제목
    # --------------------------------------------------------

    title = ""

    title_selectors = [
        "h2#title_area",
        "h2.media_end_head_headline",
        "h1#title_area",
        "h1",
    ]

    for selector in title_selectors:

        element = soup.select_one(selector)

        if element:
            title = element.get_text(
                " ",
                strip=True
            )

            if title:
                break

    # --------------------------------------------------------
    # 본문
    # --------------------------------------------------------

    body = ""

    body_selectors = [
        "article#dic_area",
        "div#dic_area",
        "div._article_content",
        "div.article_body",
        "article",
    ]

    for selector in body_selectors:

        element = soup.select_one(selector)

        if not element:
            continue

        # 불필요한 요소 제거
        for tag in element.select(
            "script, style, iframe, figure, "
            "button, aside, nav"
        ):
            tag.decompose()

        text = element.get_text(
            "\n",
            strip=True
        )

        if len(text) > len(body):
            body = text

    # --------------------------------------------------------
    # 불필요한 공백 정리
    # --------------------------------------------------------

    body = re.sub(
        r"\n{3,}",
        "\n\n",
        body
    )

    body = re.sub(
        r"[ \t]{2,}",
        " ",
        body
    )

    print(f"   제목: {title}")
    print(f"   본문 길이: {len(body):,}자")

    if not title:
        raise RuntimeError(
            "기사 제목을 찾지 못했습니다."
        )

    if len(body) < MIN_ARTICLE_LENGTH:
        raise RuntimeError(
            f"기사 본문이 너무 짧습니다. "
            f"현재 {len(body)}자"
        )

    return title, body


# ============================================================
# SemiEngineering 링크 추출
# ============================================================

def extract_semiengineering_links():
    print()
    print("📰 SemiEngineering 수집 시작")
    print()
    print("=" * 60)

    html, final_url = fetch_page(
        SEMIENGINEERING_AUTHOR_URL
    )

    soup = BeautifulSoup(
        html,
        "html.parser"
    )

    results = []

    for a in soup.find_all("a", href=True):

        title = a.get_text(
            " ",
            strip=True
        )

        href = clean_url(
            urljoin(
                final_url,
                a.get("href")
            )
        )

        if (
            "Chip Industry Week In Review"
            in title
        ):
            results.append(
                {
                    "title": title,
                    "url": href,
                }
            )

    # 중복 제거
    unique = []

    seen = set()

    for item in results:

        url = item["url"]

        if url in seen:
            continue

        seen.add(url)
        unique.append(item)

    print(
        f"   ✅ 'Chip Industry Week In Review' "
        f"후보: {len(unique)}개"
    )

    for i, item in enumerate(
        unique[:10],
        start=1
    ):
        print(f"      {i}. {item['title']}")
        print(f"         {item['url']}")

    return unique


# ============================================================
# Gemini 요약
# ============================================================

def summarize_with_gemini(title, body):
    """
    Gemini 503 / 일시적 서버 오류 발생 시
    최대 3회 재시도한다.
    """

    prompt = f"""
다음 뉴스 기사를 한국어로 요약해줘.

제목:
{title}

본문:
{body}

다음 형식을 정확히 지켜줘.

[요약 1]
핵심 내용을 한 문장으로 작성

[요약 2]
중요한 내용을 한 문장으로 작성

[요약 3]
향후 영향이나 의미를 한 문장으로 작성

[인사이트]
이 뉴스가 반도체/AI 산업에 어떤 의미가 있는지 한 문장으로 작성
"""

    max_retries = 3

    for attempt in range(
        1,
        max_retries + 1
    ):

        try:

            print(
                f"   🤖 Gemini 요약 시도 "
                f"{attempt}/{max_retries}"
            )

            response = (
                gemini_client.models.generate_content(
                    model=GEMINI_MODEL,
                    contents=prompt,
                )
            )

            if not response.text:
                raise RuntimeError(
                    "Gemini 응답이 비어 있습니다."
                )

            print("   ✅ Gemini 요약 완료")

            return response.text.strip()

        except Exception as e:

            error_text = str(e)

            print(
                f"   ⚠️ Gemini 오류 "
                f"({attempt}/{max_retries}): "
                f"{error_text}"
            )

            # ------------------------------------------------
            # 503 / UNAVAILABLE은 일시적 오류로 보고 재시도
            # ------------------------------------------------

            if (
                "503" in error_text
                or "UNAVAILABLE" in error_text
            ):

                if attempt < max_retries:

                    wait_seconds = attempt * 10

                    print(
                        f"   ⏳ Gemini 서버가 바쁩니다. "
                        f"{wait_seconds}초 후 재시도합니다."
                    )

                    time.sleep(
                        wait_seconds
                    )

                    continue

            # ------------------------------------------------
            # 다른 오류 또는 재시도 횟수 초과
            # ------------------------------------------------

            raise

    raise RuntimeError(
        "Gemini 요약에 실패했습니다."
    )


# ============================================================
# Firestore 저장
# ============================================================

def save_article(
    category,
    title,
    url,
    summary,
):
    """
    news-summary 컬렉션에 저장한다.
    """

    url = clean_url(url)

    doc_ref = db.collection(
        "news-summary"
    ).document()

    data = {
        "category": category,
        "title": title,
        "url": url,
        "summary": summary,
        "bookmarked": False,
        "createdAt": firestore.SERVER_TIMESTAMP,
        "collectedAt": firestore.SERVER_TIMESTAMP,
    }

    doc_ref.set(data)

    print(
        f"   💾 Firestore 저장 완료: {title}"
    )


# ============================================================
# Naver 카테고리 수집
# ============================================================

def collect_naver_category(
    category_name,
    issue_url,
    search_query,
):
    print()
    print("=" * 60)
    print(
        f"🔥 [{category_name}] "
        f"네이버 뉴스 수집 시작"
    )
    print(
        f"   URL: {issue_url}"
    )
    print("=" * 60)

    try:

        article_urls = extract_naver_issue_links(
            issue_url
        )

        # ----------------------------------------------------
        # URL이 충분하지 않으면 Search API fallback
        # ----------------------------------------------------

        if len(article_urls) == 0:

            print(
                "   ⚠️ 이슈 페이지에서 기사 URL을 찾지 못했습니다."
            )

            api_urls = search_naver_news_api(
                search_query
            )

            article_urls.extend(
                api_urls
            )

        # ----------------------------------------------------
        # 중복 제거
        # ----------------------------------------------------

        unique_urls = []

        seen = set()

        for url in article_urls:

            url = clean_url(url)

            if not is_naver_article_url(url):
                continue

            if url in seen:
                continue

            seen.add(url)

            unique_urls.append(url)

        article_urls = unique_urls

        print()
        print(
            f"   📌 최종 처리 대상 URL: "
            f"{len(article_urls)}개"
        )

        saved_count = 0

        # ----------------------------------------------------
        # 기사 처리
        # ----------------------------------------------------

        for index, url in enumerate(
            article_urls,
            start=1
        ):

            print()
            print(
                f"--- [{category_name}] "
                f"{index}/{len(article_urls)} ---"
            )

            # 이미 저장된 기사인지 확인
            try:

                if is_already_collected(url):

                    print(
                        "   ⏭️ 이미 수집된 기사 → 스킵"
                    )

                    continue

            except Exception as e:

                print(
                    f"   ⚠️ Firestore 중복 확인 실패: {e}"
                )

                continue

            try:

                title, body = extract_article(
                    url
                )

                print()
                print("🤖 Gemini 요약 시작")

                summary = summarize_with_gemini(
                    title,
                    body,
                )

                save_article(
                    category=category_name,
                    title=title,
                    url=url,
                    summary=summary,
                )

                saved_count += 1

            except Exception as e:

                print()
                print(
                    f"   ❌ 기사 처리 실패: {e}"
                )

                # 한 기사 실패 때문에
                # 다음 기사까지 중단하지 않는다.
                continue

            # API 서버에 너무 빠르게 요청하지 않도록
            time.sleep(2)

            # 이번 실행에서 너무 많은 기사 저장 방지
            if saved_count >= MAX_ARTICLES_PER_CATEGORY:
                print()
                print(
                    f"   📌 카테고리별 최대 "
                    f"{MAX_ARTICLES_PER_CATEGORY}개 "
                    f저장 완료"
                )
                break

        print()
        print(
            f"✅ [{category_name}] "
            f"수집 종료 - 새로 저장: "
            f"{saved_count}개"
        )

        return saved_count

    except Exception as e:

        print()
        print(
            f"❌ [{category_name}] "
            f"처리 실패"
        )

        print(
            f"   오류: {e}"
        )

        return 0


# ============================================================
# SemiEngineering 수집
# ============================================================

def collect_semiengineering():
    print()
    print("=" * 60)
    print("📰 SemiEngineering 수집 시작")
    print("=" * 60)

    try:

        candidates = (
            extract_semiengineering_links()
        )

        if not candidates:
            print(
                "   ⚠️ 수집할 SemiEngineering "
                "기사가 없습니다."
            )
            return 0

        # 가장 최신 후보부터 확인
        latest = candidates[0]

        url = clean_url(
            latest["url"]
        )

        print()
        print("------------------------------------------------------------")
        print("📌 SemiEngineering 기사")
        print(url)
        print("------------------------------------------------------------")

        # 중복 확인
        if is_already_collected(url):

            print(
                "   ⏭️ 이미 수집된 기사 → 스킵"
            )

            return 0

        try:

            title, body = extract_article(
                url
            )

            print()
            print("🤖 Gemini 요약 시작")

            summary = summarize_with_gemini(
                title,
                body,
            )

            save_article(
                category="SemiEngineering",
                title=title,
                url=url,
                summary=summary,
            )

            return 1

        except Exception as e:

            print()
            print(
                f"   ❌ [SemiEngineering] "
                f"기사 처리 실패"
            )

            print(
                f"   오류: {e}"
            )

            return 0

    except Exception as e:

        print()
        print(
            "❌ [SemiEngineering] "
            "수집 실패"
        )

        print(
            f"   오류: {e}"
        )

        return 0


# ============================================================
# 오래된 기사 삭제
# ============================================================

def cleanup_old_articles():
    print()
    print("=" * 60)
    print("🧹 오래된 기사 정리 시작")
    print(
        f"   기준: {DELETE_AFTER_DAYS}일 경과 "
        "& 북마크 안 됨"
    )
    print("=" * 60)

    cutoff = (
        datetime.now(timezone.utc)
        - timedelta(
            days=DELETE_AFTER_DAYS
        )
    )

    deleted_count = 0

    try:

        docs = (
            db.collection("news-summary")
            .where(
                filter=FieldFilter(
                    "bookmarked",
                    "==",
                    False,
                )
            )
            .stream()
        )

        for doc in docs:

            data = doc.to_dict()

            created_at = (
                data.get("createdAt")
                or data.get("collectedAt")
            )

            if not created_at:
                continue

            try:

                # Firestore Timestamp
                created_datetime = (
                    created_at.replace(
                        tzinfo=timezone.utc
                    )
                    if created_at.tzinfo is None
                    else created_at
                )

            except Exception:
                continue

            if created_datetime < cutoff:

                doc.reference.delete()

                deleted_count += 1

                print(
                    f"   🗑️ 삭제: "
                    f"{data.get('title', '(제목 없음)')}"
                )

        print()
        print(
            f"🧹 총 {deleted_count}개의 "
            f"오래된 기사를 삭제했습니다."
        )

        return deleted_count

    except Exception as e:

        print()
        print(
            f"❌ 오래된 기사 정리 실패: {e}"
        )

        return 0


# ============================================================
# 메인
# ============================================================

def main():

    print()
    print("=" * 70)
    print("🚀 뉴스 자동 수집 시스템 시작")
    print("=" * 70)

    print(
        f"🕐 실행 시각: "
        f"{now_kst().strftime('%Y-%m-%d %H:%M:%S')} KST"
    )

    print(
        f"🤖 Gemini 모델: {GEMINI_MODEL}"
    )

    print("=" * 70)

    total_saved = 0

    # --------------------------------------------------------
    # Naver 뉴스
    # --------------------------------------------------------

    for issue in NAVER_ISSUES:

        saved = collect_naver_category(
            category_name=issue["name"],
            issue_url=issue["url"],
            search_query=issue["query"],
        )

        total_saved += saved

    # --------------------------------------------------------
    # SemiEngineering
    # --------------------------------------------------------

    total_saved += (
        collect_semiengineering()
    )

    # --------------------------------------------------------
    # 오래된 기사 정리
    # --------------------------------------------------------

    cleanup_old_articles()

    # --------------------------------------------------------
    # 종료
    # --------------------------------------------------------

    print()
    print("=" * 70)
    print("🏁 뉴스 자동 수집 시스템 종료")
    print("=" * 70)

    print(
        f"📥 이번 실행에서 새로 저장한 기사: "
        f"{total_saved}개"
    )

    print("=" * 70)


# ============================================================
# 실행
# ============================================================

if __name__ == "__main__":

    try:
        main()

    except Exception as e:

        print()
        print("=" * 70)
        print("💥 프로그램 치명적 오류")
        print("=" * 70)

        print(e)

        raise
