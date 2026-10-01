```python
import os
import json
import re
import html
import requests

from bs4 import BeautifulSoup
from datetime import datetime, timedelta, timezone
from urllib.parse import urljoin

from google import genai

import firebase_admin
from firebase_admin import credentials, firestore


# ============================================================
# 1. 수집 대상 설정
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

MAX_ARTICLES_PER_CATEGORY = 5


# ============================================================
# 2. HTTP 설정
# ============================================================

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/140.0.0.0 Safari/537.36"
    ),
    "Accept": (
        "text/html,application/xhtml+xml,"
        "application/xml;q=0.9,image/avif,image/webp,"
        "*/*;q=0.8"
    ),
    "Accept-Language": (
        "ko-KR,ko;q=0.9,en-US;q=0.8,en;q=0.7"
    ),
    "Connection": "keep-alive",
}

session = requests.Session()
session.headers.update(HEADERS)


# ============================================================
# 3. Firebase 초기화
# ============================================================

firebase_json_str = os.environ.get(
    "FIREBASE_SERVICE_ACCOUNT"
)

if firebase_json_str:
    try:
        cred_dict = json.loads(firebase_json_str)
        cred = credentials.Certificate(cred_dict)
    except json.JSONDecodeError as e:
        raise RuntimeError(
            "FIREBASE_SERVICE_ACCOUNT가 올바른 JSON이 아닙니다."
        ) from e

else:
    if not os.path.exists("serviceAccountKey.json"):
        raise FileNotFoundError(
            "FIREBASE_SERVICE_ACCOUNT 환경변수도 없고 "
            "serviceAccountKey.json 파일도 없습니다."
        )

    cred = credentials.Certificate(
        "serviceAccountKey.json"
    )


if not firebase_admin._apps:
    firebase_admin.initialize_app(cred)

db = firestore.client()


# ============================================================
# 4. Gemini 초기화
# ============================================================

gemini_api_key = os.environ.get("GEMINI_API_KEY")

if not gemini_api_key:
    raise RuntimeError(
        "GEMINI_API_KEY 환경변수가 없습니다."
    )

client = genai.Client(
    api_key=gemini_api_key
)


# ============================================================
# 5. URL 정규화
# ============================================================

def clean_url(url: str) -> str:
    """
    URL에 섞여 들어온 HTML entity, Markdown 등을 제거하고
    네이버 뉴스 URL을 정리한다.
    """

    if not url:
        return ""

    url = html.unescape(url).strip()

    # 혹시 Markdown 링크가 들어온 경우 방어
    markdown_match = re.match(
        r"\[.*?\]\((https?://[^)]+)\)",
        url
    )

    if markdown_match:
        url = markdown_match.group(1)

    # //example.com 형태
    if url.startswith("//"):
        url = "https:" + url

    # 모바일 네이버 뉴스 → n.news.naver.com
    url = url.replace(
        "https://m.news.naver.com",
        "https://n.news.naver.com"
    )

    url = url.replace(
        "http://m.news.naver.com",
        "https://n.news.naver.com"
    )

    return url


# ============================================================
# 6. HTTP GET 공통 함수
# ============================================================

def get_page(url: str, timeout: int = 15):
    """
    HTTP GET 요청.
    상태 코드와 최종 URL을 확인한다.
    """

    url = clean_url(url)

    if not url.startswith(("http://", "https://")):
        raise ValueError(
            f"잘못된 URL입니다: {url}"
        )

    response = session.get(
        url,
        timeout=timeout,
        allow_redirects=True,
    )

    response.raise_for_status()

    # 한글 인코딩 보정
    if not response.encoding:
        response.encoding = response.apparent_encoding

    return response


# ============================================================
# 7. 네이버 이슈 페이지 기사 URL 추출
# ============================================================

def extract_naver_article_links(issue_url: str):

    issue_url = clean_url(issue_url)

    print()
    print("🌐 네이버 이슈 페이지 요청")
    print(f"   URL: {issue_url}")

    try:
        response = get_page(issue_url)

    except requests.HTTPError as e:
        print(f"❌ HTTP 오류: {e}")
        return []

    except requests.RequestException as e:
        print(f"❌ 네트워크 오류: {e}")
        return []

    except Exception as e:
        print(f"❌ 페이지 요청 오류: {e}")
        return []

    print(
        f"   HTTP 상태: {response.status_code}"
    )

    print(
        f"   최종 URL: {response.url}"
    )

    print(
        f"   HTML 크기: {len(response.text):,} bytes"
    )

    soup = BeautifulSoup(
        response.text,
        "html.parser"
    )

    results = []
    seen = set()

    # --------------------------------------------------------
    # 방법 1
    # HTML의 모든 a[href] 검사
    # --------------------------------------------------------

    for a in soup.select("a[href]"):

        href = a.get("href", "").strip()

        title = a.get_text(
            " ",
            strip=True
        )

        if not href:
            continue

        href = html.unescape(href)

        # 상대 URL → 절대 URL
        href = urljoin(
            response.url,
            href
        )

        href = clean_url(href)

        # 네이버 기사 URL인지 확인
        is_naver_article = any(
            pattern in href
            for pattern in [
                "n.news.naver.com/article/",
                "news.naver.com/article/",
                "n.news.naver.com/mnews/article/",
                "news.naver.com/mnews/article/",
            ]
        )

        if not is_naver_article:
            continue

        if len(title) < 5:
            continue

        if href in seen:
            continue

        seen.add(href)

        results.append({
            "title": title,
            "url": href,
        })

    # --------------------------------------------------------
    # 방법 2
    # HTML 문자열 안에 직접 들어 있는 URL 검사
    # --------------------------------------------------------

    url_pattern = re.compile(
        r'https?://(?:n\.)?news\.naver\.com/'
        r'(?:mnews/)?article/\d+/\d+'
    )

    for match in url_pattern.findall(
        response.text
    ):

        url = clean_url(match)

        if url in seen:
            continue

        seen.add(url)

        results.append({
            "title": "",
            "url": url,
        })

    print(
        f"🔎 발견된 네이버 기사 링크: "
        f"{len(results)}개"
    )

    # 디버깅을 위해 처음 10개 출력
    for i, article in enumerate(
        results[:10],
        start=1
    ):
        print(
            f"   {i}. "
            f"{article['title'][:60]} "
            f"→ {article['url']}"
        )

    return results


# ============================================================
# 8. 네이버 뉴스 검색 API
# ============================================================

def search_naver_news_api(
    query: str,
    display: int = 30
):

    client_id = os.environ.get(
        "NAVER_CLIENT_ID"
    )

    client_secret = os.environ.get(
        "NAVER_CLIENT_SECRET"
    )

    # API 설정이 없으면 fallback 사용 안 함
    if not client_id or not client_secret:

        print(
            "ℹ️ NAVER_CLIENT_ID / "
            "NAVER_CLIENT_SECRET가 없습니다."
        )

        print(
            "   네이버 뉴스 API fallback을 "
            "건너뜁니다."
        )

        return []

    api_url = (
        "https://openapi.naver.com/"
        "v1/search/news.json"
    )

    headers = {
        "X-Naver-Client-Id": client_id,
        "X-Naver-Client-Secret": client_secret,
    }

    params = {
        "query": query,
        "display": min(display, 100),
        "start": 1,
        "sort": "date",
    }

    print()
    print(
        f"🔄 네이버 뉴스 API 요청: "
        f"{query}"
    )

    try:

        response = requests.get(
            api_url,
            headers=headers,
            params=params,
            timeout=15,
        )

        response.raise_for_status()

        data = response.json()

    except requests.HTTPError as e:

        print(
            f"❌ 네이버 API HTTP 오류: {e}"
        )

        return []

    except Exception as e:

        print(
            f"❌ 네이버 API 요청 실패: {e}"
        )

        return []

    results = []

    for item in data.get(
        "items",
        []
    ):

        title = BeautifulSoup(
            item.get("title", ""),
            "html.parser"
        ).get_text(
            " ",
            strip=True
        )

        link = clean_url(
            item.get("link", "")
        )

        description = BeautifulSoup(
            item.get("description", ""),
            "html.parser"
        ).get_text(
            " ",
            strip=True
        )

        if not link:
            continue

        results.append({
            "title": title,
            "url": link,
            "description": description,
        })

    print(
        f"🔎 네이버 뉴스 API 결과: "
        f"{len(results)}개"
    )

    return results


# ============================================================
# 9. 기사 제목 / 본문 추출
# ============================================================

def extract_article(
    url: str,
    fallback_title: str = ""
):

    url = clean_url(url)

    print()
    print(
        f"   📰 기사 페이지 요청: {url}"
    )

    try:

        response = get_page(url)

    except requests.HTTPError as e:

        print(
            f"   ❌ 기사 HTTP 오류: {e}"
        )

        return None

    except Exception as e:

        print(
            f"   ❌ 기사 요청 실패: {e}"
        )

        return None

    soup = BeautifulSoup(
        response.text,
        "html.parser"
    )

    # --------------------------------------------------------
    # 제목
    # --------------------------------------------------------

    title = fallback_title

    title_selectors = [
        "h2#title_area",
        "h2.media_end_head_headline",
        "h1",
        "meta[property='og:title']",
        "title",
    ]

    for selector in title_selectors:

        element = soup.select_one(
            selector
        )

        if not element:
            continue

        if element.name == "meta":

            candidate = element.get(
                "content",
                ""
            ).strip()

        else:

            candidate = element.get_text(
                " ",
                strip=True
            )

        if candidate:

            title = candidate

            break

    # --------------------------------------------------------
    # 본문
    # --------------------------------------------------------

    body_selectors = [
        "div#dic_area",
        "div#newsct_article",
        "article#dic_area",
        "div.newsct_article",
        "div.article_body",
        "article",
    ]

    body = None

    for selector in body_selectors:

        element = soup.select_one(
            selector
        )

        if element:

            body = element

            break

    body_text = ""

    if body:

        # 불필요한 요소 제거
        for tag in body.select(
            "script, style, iframe, "
            "figure, button, "
            ".byline, .copyright, "
            ".media_end_head_info_datestamp"
        ):

            tag.decompose()

        body_text = body.get_text(
            "\n",
            strip=True
        )

    # --------------------------------------------------------
    # og:description 보조
    # --------------------------------------------------------

    if len(body_text) < 100:

        meta = soup.select_one(
            "meta[property='og:description']"
        )

        if meta:

            description = meta.get(
                "content",
                ""
            ).strip()

            if len(description) > len(
                body_text
            ):
                body_text = description

    # --------------------------------------------------------
    # 공백 정리
    # --------------------------------------------------------

    body_text = re.sub(
        r"\n{3,}",
        "\n\n",
        body_text
    )

    body_text = re.sub(
        r"[ \t]+",
        " ",
        body_text
    )

    print(
        f"   제목: {title[:100]}"
    )

    print(
        f"   본문 길이: "
        f"{len(body_text):,}자"
    )

    if not title:

        print(
            "   ⚠️ 기사 제목을 찾지 못했습니다."
        )

        return None

    if len(body_text) < 100:

        print(
            "   ⚠️ 기사 본문이 너무 짧습니다."
        )

        return None

    return {
        "title": title,
        "url": url,
        "body": body_text,
    }


# ============================================================
# 10. Firestore 중복 검사
# ============================================================

def is_already_collected(
    url: str
) -> bool:

    url = clean_url(url)

    try:

        docs = (
            db.collection("news-summary")
            .where(
                "url",
                "==",
                url
            )
            .limit(1)
            .stream()
        )

        return any(docs)

    except Exception as e:

        print(
            f"⚠️ Firestore 중복 검사 실패: {e}"
        )

        # 중복 여부를 확인하지 못했으므로
        # 안전하게 True 처리하여 중복 저장 방지
        return True


# ============================================================
# 11. Gemini 요약
# ============================================================

def summarize_with_gemini(
    title: str,
    text: str
) -> str:

    prompt = f"""
당신은 한국어 뉴스 분석가입니다.

아래 뉴스 기사를 읽고 반드시 다음 형식으로 작성하세요.

[요약 1]
핵심 사실을 한 문장으로 작성

[요약 2]
중요한 변화, 수치 또는 사건을 한 문장으로 작성

[요약 3]
산업 또는 시장에 미치는 영향을 한 문장으로 작성

[인사이트]
이 기사가 반도체/AI 산업에 의미하는 바를 한 문장으로 작성

규칙:
- 한국어로 작성하세요.
- 기사에 없는 사실을 추가하지 마세요.
- 추측은 사실처럼 표현하지 마세요.
- 불필요한 서론은 작성하지 마세요.
- 전체적으로 간결하게 작성하세요.

[제목]
{title}

[본문]
{text[:12000]}
"""

    response = client.models.generate_content(
        model="gemini-2.5-flash",
        contents=prompt,
    )

    if not response.text:

        raise RuntimeError(
            "Gemini 응답이 비어 있습니다."
        )

    return response.text.strip()


# ============================================================
# 12. 네이버 이슈 수집
# ============================================================

def fetch_naver_issue_articles(
    issue_url: str,
    category_name: str,
    fallback_query: str,
):

    print()
    print("=" * 70)

    print(
        f"[{category_name}] "
        f"네이버 뉴스 수집 시작"
    )

    print(
        f"URL: {issue_url}"
    )

    print("=" * 70)

    # --------------------------------------------------------
    # 1차: 이슈 페이지 직접 크롤링
    # --------------------------------------------------------

    articles = extract_naver_article_links(
        issue_url
    )

    # --------------------------------------------------------
    # 2차: 기사 0개이면 뉴스 API fallback
    # --------------------------------------------------------

    if not articles:

        print()
        print(
            "⚠️ 이슈 페이지에서 "
            "기사 링크를 찾지 못했습니다."
        )

        print(
            f"🔄 뉴스 API fallback: "
            f"{fallback_query}"
        )

        articles = search_naver_news_api(
            fallback_query,
            display=30
        )

    if not articles:

        print(
            f"❌ [{category_name}] "
            f"수집 가능한 기사가 없습니다."
        )

        return 0

    collected_count = 0

    seen_urls = set()

    # --------------------------------------------------------
    # 기사별 처리
    # --------------------------------------------------------

    for candidate in articles:

        if (
            collected_count
            >= MAX_ARTICLES_PER_CATEGORY
        ):
            break

        raw_url = candidate.get(
            "url",
            ""
        )

        url = clean_url(raw_url)

        if not url:
            continue

        if url in seen_urls:
            continue

        seen_urls.add(url)

        # ----------------------------------------------------
        # Firestore 중복 검사
        # ----------------------------------------------------

        if is_already_collected(url):

            print(
                f"⏭️ 이미 수집된 기사: {url}"
            )

            continue

        # ----------------------------------------------------
        # 기사 본문 가져오기
        # ----------------------------------------------------

        article = extract_article(
            url=url,
            fallback_title=candidate.get(
                "title",
                ""
            )
        )

        if not article:
            continue

        try:

            # ------------------------------------------------
            # Gemini
            # ------------------------------------------------

            print(
                "   🤖 Gemini 요약 시작..."
            )

            summary = summarize_with_gemini(
                title=article["title"],
                text=article["body"],
            )

            # ------------------------------------------------
            # Firestore 저장
            # ------------------------------------------------

            doc_data = {
                "title": article["title"],
                "url": article["url"],
                "summary": summary,
                "category": category_name,
                "is_bookmarked": False,
                "created_at": datetime.now(
                    timezone.utc
                ),
            }

            db.collection(
                "news-summary"
            ).add(doc_data)

            print(
                "   ✅ Firestore 저장 완료"
            )

            print(
                f"      {article['title'][:100]}"
            )

            collected_count += 1

        except Exception as e:

            print(
                f"   ❌ 기사 처리 중 오류: {e}"
            )

    print()
    print(
        f"📊 [{category_name}] "
        f"신규 수집: "
        f"{collected_count}건"
    )

    return collected_count


# ============================================================
# 13. 7일 지난 미북마크 기사 삭제
# ============================================================

def cleanup_old_articles():

    print()
    print(
        "🧹 오래된 기사 정리를 시작합니다 "
        "(7일 경과 & 북마크 안 됨)..."
    )

    cutoff = (
        datetime.now(timezone.utc)
        - timedelta(days=7)
    )

    try:

        docs = (
            db.collection("news-summary")
            .where(
                "is_bookmarked",
                "==",
                False
            )
            .where(
                "created_at",
                "<",
                cutoff
            )
            .stream()
        )

        deleted_count = 0

        for doc in docs:

            print(
                f"   🗑️ 삭제: {doc.id}"
            )

            doc.reference.delete()

            deleted_count += 1

        print(
            f"🧹 총 {deleted_count}개의 "
            f"오래된 기사가 삭제되었습니다."
        )

    except Exception as e:

        print(
            f"❌ 오래된 기사 삭제 실패: {e}"
        )


# ============================================================
# 14. Main
# ============================================================

def main():

    total_collected = 0

    print()
    print("=" * 70)
    print("🚀 뉴스 수집 프로그램 시작")
    print("=" * 70)

    # --------------------------------------------------------
    # 네이버 이슈 수집
    # --------------------------------------------------------

    for issue in NAVER_ISSUES:

        count = fetch_naver_issue_articles(
            issue_url=issue["url"],
            category_name=issue["name"],
            fallback_query=issue["query"],
        )

        total_collected += count

    # --------------------------------------------------------
    # 오래된 기사 삭제
    # --------------------------------------------------------

    cleanup_old_articles()

    # --------------------------------------------------------
    # 최종 결과
    # --------------------------------------------------------

    print()
    print("=" * 70)

    print(
        f"🏁 전체 신규 수집: "
        f"{total_collected}건"
    )

    print("=" * 70)

    # 주의:
    # 0건이라고 해서 반드시 오류는 아니다.
    #
    # 이미 Firestore에 모든 기사가 들어있을 경우
    # 정상적으로 0건이 될 수 있다.
    #
    # 따라서 여기서는 exit code 1을 발생시키지 않는다.


# ============================================================
# 실행
# ============================================================

if __name__ == "__main__":
    main()
```
