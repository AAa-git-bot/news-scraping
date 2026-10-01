import os
import re
import json
import html
from datetime import datetime, timedelta, timezone
from urllib.parse import urljoin, urlparse, urlunparse, parse_qsl, urlencode

import requests
from bs4 import BeautifulSoup

import firebase_admin
from firebase_admin import credentials, firestore

from google import genai


# ============================================================
# 기본 설정
# ============================================================

KST = timezone(timedelta(hours=9))

# 사용자가 요청한 Gemini 모델
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-3.8-flash")

# 카테고리별 최대 수집 기사 수
MAX_ARTICLES_PER_CATEGORY = 5

# 기사 본문 최소 길이
MIN_ARTICLE_LENGTH = 200

# 기사 오래된 데이터 삭제 기준
DELETE_AFTER_DAYS = 7


# ============================================================
# 수집 대상
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


# ============================================================
# HTTP 설정
# ============================================================

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/131.0.0.0 Safari/537.36"
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
# 환경변수 확인
# ============================================================

def require_env(name):
    value = os.getenv(name)

    if not value:
        raise RuntimeError(
            f"환경변수 {name} 이(가) 없습니다. "
            f"GitHub Secrets를 확인하세요."
        )

    return value


# ============================================================
# Firebase 초기화
# ============================================================

def init_firestore():
    if firebase_admin._apps:
        return firestore.client()

    service_account_json = require_env("FIREBASE_SERVICE_ACCOUNT")

    try:
        service_account_info = json.loads(service_account_json)
    except json.JSONDecodeError as e:
        raise RuntimeError(
            "FIREBASE_SERVICE_ACCOUNT가 올바른 JSON이 아닙니다."
        ) from e

    cred = credentials.Certificate(service_account_info)

    firebase_admin.initialize_app(cred)

    return firestore.client()


db = init_firestore()


# ============================================================
# Gemini 초기화
# ============================================================

gemini_api_key = require_env("GEMINI_API_KEY")

gemini_client = genai.Client(
    api_key=gemini_api_key
)


# ============================================================
# URL 정리
# ============================================================

def clean_url(url):
    """
    실수로 Markdown 링크가 들어와도 실제 URL만 추출한다.

    예:
    [https://example.com](https://example.com)
    ->
    https://example.com
    """

    if not url:
        return ""

    url = html.unescape(url).strip()

    # Markdown 링크:
    # [텍스트](URL)
    match = re.match(r"^\[.*?\]\((https?://[^)]+)\)$", url)

    if match:
        url = match.group(1)

    # 혹시 앞뒤에 따옴표가 있으면 제거
    url = url.strip('"').strip("'")

    # //example.com
    if url.startswith("//"):
        url = "https:" + url

    # Naver 모바일 URL 통일
    url = url.replace(
        "https://m.news.naver.com/",
        "https://n.news.naver.com/"
    )

    # URL fragment 제거
    try:
        parsed = urlparse(url)

        parsed = parsed._replace(fragment="")

        url = urlunparse(parsed)

    except Exception:
        pass

    return url


# ============================================================
# HTTP 요청
# ============================================================

def fetch_page(url, timeout=20):
    url = clean_url(url)

    print(f"🌐 요청: {url}")

    try:
        response = session.get(
            url,
            timeout=timeout,
            allow_redirects=True,
        )

        print(
            f"   HTTP {response.status_code} "
            f"| 최종 URL: {response.url} "
            f"| HTML: {len(response.text):,} bytes"
        )

        response.raise_for_status()

        response.encoding = response.apparent_encoding or response.encoding

        return response

    except requests.RequestException as e:
        print(f"❌ 요청 실패: {url}")
        print(f"   오류: {e}")

        return None


# ============================================================
# Firestore 기존 URL 확인
# ============================================================

def is_already_collected(url):
    url = clean_url(url)

    try:
        docs = (
            db.collection("news-summary")
            .where("url", "==", url)
            .limit(1)
            .stream()
        )

        for _ in docs:
            return True

        return False

    except Exception as e:
        print(f"⚠️ Firestore 중복 확인 실패: {e}")

        # 중복 확인이 안 되는 상황에서 같은 기사를
        # 여러 번 저장하는 것보다 안전하게 건너뛴다.
        return True


# ============================================================
# Naver 기사 URL 판별
# ============================================================

def is_naver_article_url(url):
    if not url:
        return False

    url = clean_url(url)

    parsed = urlparse(url)

    if parsed.netloc not in {
        "n.news.naver.com",
        "news.naver.com",
    }:
        return False

    article_patterns = [
        "/article/",
        "/mnews/article/",
    ]

    return any(
        pattern in parsed.path
        for pattern in article_patterns
    )


# ============================================================
# Naver 이슈 페이지에서 기사 URL 추출
# ============================================================

def extract_naver_issue_links(issue_url):
    print(f"\n🔎 네이버 이슈 페이지 분석")
    print(f"   URL: {issue_url}")

    response = fetch_page(issue_url)

    if response is None:
        return []

    soup = BeautifulSoup(response.text, "html.parser")

    urls = []
    seen = set()

    # --------------------------------------------------------
    # 1차: a[href]
    # --------------------------------------------------------

    for a in soup.select("a[href]"):
        href = a.get("href", "").strip()

        if not href:
            continue

        absolute_url = urljoin(response.url, href)

        absolute_url = clean_url(absolute_url)

        if not is_naver_article_url(absolute_url):
            continue

        if absolute_url not in seen:
            seen.add(absolute_url)
            urls.append(absolute_url)

    # --------------------------------------------------------
    # 2차: HTML 안에 직접 들어있는 Naver article URL 검색
    # --------------------------------------------------------

    patterns = [
        r'https?://n\.news\.naver\.com/article/\d+/\d+',
        r'https?://n\.news\.naver\.com/mnews/article/\d+/\d+',
        r'https?://news\.naver\.com/article/\d+/\d+',
        r'https?://news\.naver\.com/mnews/article/\d+/\d+',
    ]

    for pattern in patterns:
        matches = re.findall(pattern, response.text)

        for match in matches:
            match = clean_url(match)

            if match not in seen:
                seen.add(match)
                urls.append(match)

    print(f"   ✅ 발견한 Naver 기사 URL: {len(urls)}개")

    for i, url in enumerate(urls[:10], start=1):
        print(f"      {i}. {url}")

    return urls


# ============================================================
# Naver Search API fallback
# ============================================================

def search_naver_news_api(query, display=30):
    """
    이슈 페이지에서 기사 URL을 하나도 찾지 못했을 때
    선택적으로 Naver Search API를 사용한다.

    NAVER_CLIENT_ID
    NAVER_CLIENT_SECRET
    이 두 Secret이 없으면 그냥 [] 반환.
    """

    client_id = os.getenv("NAVER_CLIENT_ID")
    client_secret = os.getenv("NAVER_CLIENT_SECRET")

    if not client_id or not client_secret:
        print(
            "   ℹ️ Naver Search API Secret이 없어 "
            "API fallback을 건너뜁니다."
        )
        return []

    print(f"   🔁 Naver Search API fallback: {query}")

    url = "https://openapi.naver.com/v1/search/news.json"

    headers = {
        "X-Naver-Client-Id": client_id,
        "X-Naver-Client-Secret": client_secret,
        "User-Agent": HEADERS["User-Agent"],
    }

    params = {
        "query": query,
        "display": display,
        "start": 1,
        "sort": "date",
    }

    try:
        response = requests.get(
            url,
            headers=headers,
            params=params,
            timeout=20,
        )

        print(
            f"   Search API HTTP {response.status_code}"
        )

        response.raise_for_status()

        data = response.json()

    except Exception as e:
        print(f"❌ Naver Search API 실패: {e}")
        return []

    results = []

    for item in data.get("items", []):
        title = BeautifulSoup(
            item.get("title", ""),
            "html.parser"
        ).get_text(" ", strip=True)

        article_url = (
            item.get("originallink")
            or item.get("link")
            or ""
        )

        article_url = clean_url(article_url)

        if not article_url:
            continue

        results.append(
            {
                "url": article_url,
                "title": title,
            }
        )

    print(f"   ✅ Search API 결과: {len(results)}개")

    return results


# ============================================================
# Naver 기사 본문 추출
# ============================================================

def extract_article(url, fallback_title=""):
    url = clean_url(url)

    print(f"\n📄 기사 본문 추출")
    print(f"   URL: {url}")

    response = fetch_page(url)

    if response is None:
        return None

    soup = BeautifulSoup(response.text, "html.parser")

    # --------------------------------------------------------
    # 제목
    # --------------------------------------------------------

    title = ""

    title_selectors = [
        "h2#title_area",
        "h2.media_end_head_headline",
        "h1",
        "meta[property='og:title']",
        "title",
    ]

    for selector in title_selectors:
        element = soup.select_one(selector)

        if not element:
            continue

        if element.name == "meta":
            value = element.get("content", "")
        else:
            value = element.get_text(" ", strip=True)

        if value:
            title = value.strip()
            break

    if not title:
        title = fallback_title or "제목 없음"

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
        element = soup.select_one(selector)

        if element:
            body = element
            break

    # --------------------------------------------------------
    # 본문 제거 요소
    # --------------------------------------------------------

    if body:
        remove_selectors = [
            "script",
            "style",
            "iframe",
            "figure",
            "button",
            "aside",
            ".byline",
            ".copyright",
            ".reporter",
            ".media_end_head_journalist",
            ".media_end_head_info_datestamp",
        ]

        for selector in remove_selectors:
            for element in body.select(selector):
                element.decompose()

        text = body.get_text(
            "\n",
            strip=True
        )

    else:
        text = ""

    # --------------------------------------------------------
    # 본문이 너무 짧으면 OG description 사용
    # --------------------------------------------------------

    if len(text) < 100:
        description = soup.select_one(
            "meta[property='og:description']"
        )

        if description:
            text = description.get("content", "").strip()

    # --------------------------------------------------------
    # 공백 정리
    # --------------------------------------------------------

    text = re.sub(
        r"\n{3,}",
        "\n\n",
        text
    )

    text = re.sub(
        r"[ \t]+",
        " ",
        text
    ).strip()

    print(f"   제목: {title}")
    print(f"   본문 길이: {len(text):,}자")

    if len(text) < MIN_ARTICLE_LENGTH:
        print(
            f"   ⚠️ 본문이 너무 짧아 건너뜁니다."
        )
        return None

    return {
        "title": title,
        "url": url,
        "text": text,
    }


# ============================================================
# SemiEngineering 기사 목록 추출
# ============================================================

def extract_semiengineering_links():
    print("\n" + "=" * 60)
    print("📰 SemiEngineering 수집 시작")
    print("=" * 60)

    response = fetch_page(SEMIENGINEERING_AUTHOR_URL)

    if response is None:
        return []

    soup = BeautifulSoup(response.text, "html.parser")

    candidates = []
    seen = set()

    # --------------------------------------------------------
    # 모든 링크 중 "Chip Industry Week In Review" 포함 링크
    # --------------------------------------------------------

    for a in soup.select("a[href]"):
        href = a.get("href", "").strip()

        if not href:
            continue

        title = a.get_text(" ", strip=True)

        absolute_url = urljoin(
            response.url,
            href
        )

        absolute_url = clean_url(absolute_url)

        combined = f"{title} {absolute_url}".lower()

        if "chip industry week in review" not in combined:
            continue

        if absolute_url in seen:
            continue

        # author 페이지 자체는 제외
        if absolute_url.rstrip("/") == SEMIENGINEERING_AUTHOR_URL.rstrip("/"):
            continue

        seen.add(absolute_url)

        candidates.append(
            {
                "url": absolute_url,
                "title": title,
            }
        )

    print(
        f"   ✅ 'Chip Industry Week In Review' 후보: "
        f"{len(candidates)}개"
    )

    for i, item in enumerate(candidates[:10], start=1):
        print(
            f"      {i}. {item['title']}\n"
            f"         {item['url']}"
        )

    return candidates


# ============================================================
# Gemini 요약
# ============================================================

def summarize_with_gemini(title, text):
    print("🤖 Gemini 요약 시작")

    # 너무 긴 기사는 일정 길이까지만 전송
    article_text = text[:12000]

    prompt = f"""
다음 뉴스 기사를 한국어로 요약해 주세요.

[기사 제목]
{title}

[기사 본문]
{article_text}

반드시 아래 형식을 정확하게 지켜주세요.

[요약 1]
한 문장

[요약 2]
한 문장

[요약 3]
한 문장

[인사이트]
이 기사가 반도체/AI/기술 산업에 주는 의미를 한 문장으로 작성

규칙:
1. 반드시 한국어로 작성합니다.
2. 요약은 기사에 실제로 나온 내용만 사용합니다.
3. 기사에 없는 숫자, 사실, 전망을 임의로 만들지 않습니다.
4. 각 요약은 핵심 내용 위주로 간결하게 작성합니다.
5. 인사이트는 단순한 기사 반복이 아니라 산업적 의미를 설명합니다.
6. 제목이나 서론을 추가하지 않습니다.
"""

    try:
        response = gemini_client.models.generate_content(
            model=GEMINI_MODEL,
            contents=prompt,
        )

        result = (response.text or "").strip()

        if not result:
            raise RuntimeError(
                "Gemini 응답이 비어 있습니다."
            )

        print("   ✅ Gemini 요약 완료")

        return result

    except Exception as e:
        print(f"❌ Gemini 요약 실패: {e}")
        raise


# ============================================================
# Firestore 저장
# ============================================================

def save_article(
    article,
    summary,
    category,
    source,
):
    data = {
        "title": article["title"],
        "url": article["url"],
        "summary": summary,
        "category": category,
        "source": source,
        "is_bookmarked": False,
        "created_at": firestore.SERVER_TIMESTAMP,
    }

    db.collection("news-summary").add(data)

    print(
        f"   💾 Firestore 저장 완료: "
        f"{article['title']}"
    )


# ============================================================
# Naver 카테고리 수집
# ============================================================

def collect_naver_category(
    issue,
):
    category_name = issue["name"]
    issue_url = issue["url"]
    fallback_query = issue["query"]

    print("\n" + "=" * 60)
    print(f"🔥 [{category_name}] 네이버 뉴스 수집 시작")
    print(f"   URL: {issue_url}")
    print("=" * 60)

    article_urls = extract_naver_issue_links(
        issue_url
    )

    search_results = []

    # --------------------------------------------------------
    # 이슈 페이지에서 0개면 Search API fallback
    # --------------------------------------------------------

    if not article_urls:
        print(
            "⚠️ 이슈 페이지에서 기사 URL을 찾지 못했습니다."
        )

        search_results = search_naver_news_api(
            fallback_query,
            display=30,
        )

        article_urls = [
            item["url"]
            for item in search_results
        ]

    if not article_urls:
        print(
            f"❌ [{category_name}] 수집할 기사 URL이 없습니다."
        )
        return 0

    # 중복 제거
    unique_urls = []

    for url in article_urls:
        url = clean_url(url)

        if url and url not in unique_urls:
            unique_urls.append(url)

    print(
        f"📌 최종 처리 대상 URL: "
        f"{len(unique_urls)}개"
    )

    saved_count = 0

    for index, url in enumerate(
        unique_urls,
        start=1,
    ):
        if saved_count >= MAX_ARTICLES_PER_CATEGORY:
            break

        print(
            f"\n--- [{category_name}] "
            f"{index}/{len(unique_urls)} ---"
        )

        # ----------------------------------------------------
        # Firestore 중복 확인
        # ----------------------------------------------------

        if is_already_collected(url):
            print("⏭️ 이미 Firestore에 존재 → 건너뜀")
            continue

        # ----------------------------------------------------
        # Search API에서 제목 가져오기
        # ----------------------------------------------------

        fallback_title = ""

        for item in search_results:
            if clean_url(item["url"]) == url:
                fallback_title = item.get(
                    "title",
                    ""
                )
                break

        # ----------------------------------------------------
        # 본문 추출
        # ----------------------------------------------------

        article = extract_article(
            url,
            fallback_title=fallback_title,
        )

        if not article:
            continue

        # ----------------------------------------------------
        # Gemini 요약
        # ----------------------------------------------------

        summary = summarize_with_gemini(
            article["title"],
            article["text"],
        )

        # ----------------------------------------------------
        # Firestore 저장
        # ----------------------------------------------------

        save_article(
            article=article,
            summary=summary,
            category=category_name,
            source="Naver",
        )

        saved_count += 1

    print(
        f"\n✅ [{category_name}] "
        f"새 기사 {saved_count}개 저장"
    )

    return saved_count


# ============================================================
# SemiEngineering 수집
# ============================================================

def collect_semiengineering():
    candidates = extract_semiengineering_links()

    if not candidates:
        print(
            "❌ SemiEngineering에서 "
            "'Chip Industry Week In Review'를 찾지 못했습니다."
        )
        return 0

    saved_count = 0

    for item in candidates:

        if saved_count >= MAX_ARTICLES_PER_CATEGORY:
            break

        url = clean_url(item["url"])

        print("\n" + "-" * 60)
        print(f"📌 SemiEngineering 기사")
        print(f"   {url}")

        # ----------------------------------------------------
        # Firestore 중복 확인
        # ----------------------------------------------------

        if is_already_collected(url):
            print(
                "⏭️ 이미 Firestore에 존재 → 건너뜀"
            )
            continue

        # ----------------------------------------------------
        # 본문
        # ----------------------------------------------------

        article = extract_article(
            url,
            fallback_title=item.get(
                "title",
                "",
            ),
        )

        if not article:
            continue

        # ----------------------------------------------------
        # Gemini
        # ----------------------------------------------------

        summary = summarize_with_gemini(
            article["title"],
            article["text"],
        )

        # ----------------------------------------------------
        # Firestore
        # ----------------------------------------------------

        save_article(
            article=article,
            summary=summary,
            category="SemiEngineering",
            source="SemiEngineering",
        )

        saved_count += 1

    print(
        f"\n✅ [SemiEngineering] "
        f"새 기사 {saved_count}개 저장"
    )

    return saved_count


# ============================================================
# 오래된 기사 삭제
# ============================================================

def cleanup_old_articles():
    print("\n" + "=" * 60)
    print("🧹 오래된 기사 정리 시작")
    print(
        f"   기준: {DELETE_AFTER_DAYS}일 경과 "
        f"& 북마크 안 됨"
    )
    print("=" * 60)

    cutoff = datetime.now(
        timezone.utc
    ) - timedelta(
        days=DELETE_AFTER_DAYS
    )

    deleted_count = 0

    # --------------------------------------------------------
    # composite index가 필요하지 않도록
    # Firestore 전체 문서를 가져와 Python에서 필터링
    # --------------------------------------------------------

    try:
        docs = (
            db.collection("news-summary")
            .stream()
        )

        for doc in docs:
            data = doc.to_dict()

            is_bookmarked = data.get(
                "is_bookmarked",
                False,
            )

            created_at = data.get(
                "created_at"
            )

            if is_bookmarked:
                continue

            if not created_at:
                continue

            # Firestore Timestamp -> datetime
            try:
                created_at_dt = created_at.replace(
                    tzinfo=timezone.utc
                )
            except Exception:
                created_at_dt = created_at

            if created_at_dt < cutoff:
                print(
                    f"   🗑️ 삭제: "
                    f"{data.get('title', '제목 없음')}"
                )

                doc.reference.delete()

                deleted_count += 1

    except Exception as e:
        print(
            f"❌ 오래된 기사 삭제 중 오류: {e}"
        )
        raise

    print(
        f"\n🧹 총 {deleted_count}개의 "
        f"오래된 기사를 삭제했습니다."
    )

    return deleted_count


# ============================================================
# 메인
# ============================================================

def main():
    print("\n")
    print("=" * 70)
    print("🚀 뉴스 자동 수집 시스템 시작")
    print("=" * 70)

    print(
        f"🕐 실행 시각: "
        f"{datetime.now(KST).strftime('%Y-%m-%d %H:%M:%S KST')}"
    )

    print(
        f"🤖 Gemini 모델: {GEMINI_MODEL}"
    )

    total_saved = 0

    # --------------------------------------------------------
    # Naver 2개
    # --------------------------------------------------------

    for issue in NAVER_ISSUES:
        try:
            count = collect_naver_category(issue)
            total_saved += count

        except Exception as e:
            print(
                f"\n❌ [{issue['name']}] 처리 실패"
            )
            print(f"   오류: {e}")

            # 한 카테고리 실패 때문에
            # 다른 카테고리까지 죽지 않게 한다.
            continue

    # --------------------------------------------------------
    # SemiEngineering
    # --------------------------------------------------------

    try:
        count = collect_semiengineering()
        total_saved += count

    except Exception as e:
        print(
            "\n❌ [SemiEngineering] 처리 실패"
        )
        print(f"   오류: {e}")

    # --------------------------------------------------------
    # 오래된 기사 삭제
    # --------------------------------------------------------

    cleanup_old_articles()

    # --------------------------------------------------------
    # 결과
    # --------------------------------------------------------

    print("\n" + "=" * 70)
    print("🏁 뉴스 자동 수집 시스템 종료")
    print("=" * 70)
    print(
        f"📥 이번 실행에서 새로 저장한 기사: "
        f"{total_saved}개"
    )
    print("=" * 70)

    # --------------------------------------------------------
    # 중요:
    #
    # 새 기사가 0개인 것은 정상일 수 있다.
    # 이미 전부 Firestore에 있다면 0개가 된다.
    #
    # 따라서 total_saved == 0이라고 해서
    # GitHub Actions를 실패시키지 않는다.
    # --------------------------------------------------------

    return total_saved


# ============================================================
# 실행
# ============================================================

if __name__ == "__main__":
    try:
        main()

    except Exception as e:
        print("\n" + "=" * 70)
        print("💥 치명적인 오류로 실행 실패")
        print("=" * 70)
        print(e)
        print("=" * 70)

        raise
