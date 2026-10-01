import os
import json
import requests
from bs4 import BeautifulSoup
from datetime import datetime, timedelta, timezone
from google import genai
import firebase_admin
from firebase_admin import credentials, firestore

# ==========================================
# 1. 환경 변수 및 서비스 초기화
# ==========================================
firebase_json_str = os.environ.get("FIREBASE_SERVICE_ACCOUNT")
if firebase_json_str:
    cred_dict = json.loads(firebase_json_str)
    cred = credentials.Certificate(cred_dict)
    firebase_admin.initialize_app(cred)
else:
    cred = credentials.Certificate("serviceAccountKey.json")
    firebase_admin.initialize_app(cred)

db = firestore.client()

gemini_api_key = os.environ.get("GEMINI_API_KEY")
client = genai.Client(api_key=gemini_api_key)

# 네이버 차단 회피용 Header 강화
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
    "Referer": "https://media.naver.com/"
}

# ==========================================
# 2. 헬퍼 함수 정의
# ==========================================
def is_already_collected(url: str) -> bool:
    """Firestore 'news-summary' 컬렉션에서 동일한 URL 문서가 있는지 검사합니다."""
    docs = db.collection("news-summary").where("url", "==", url).limit(1).stream()
    return any(docs)

def summarize_with_gemini(title: str, text: str, is_weekly_report: bool = False) -> str:
    """Gemini API를 이용해 요약 및 인사이트를 작성합니다."""
    if is_weekly_report:
        prompt = f"""
        다음은 지난 한 주간 수집된 주요 기사들의 요약 모음입니다.
        전체 기사 내용을 바탕으로 반도체 및 AI 산업의 [주간 종합 요약 보고서]를 작성해 주세요.
        
        [내용]
        {text}

        [응답 형식]
        1. 주간 핵심 요약 (3~5줄)
        2. 이번 주 산업 인사이트 및 전망 (2~3줄)
        """
    else:
        prompt = f"""
        다음 기사 본문을 읽고 한국어로 핵심 요약과 인사이트를 작성해 주세요.

        [기사 제목]: {title}
        [기사 본문]: {text[:3000]}

        [응답 형식]
        - 핵심 요약 (3줄):
        - 한 줄 인사이트:
        """

    response = client.models.generate_content(
        model="gemini-2.5-flash",
        contents=prompt
    )
    return response.text

# ==========================================
# 3. 크롤링 로직 (네이버 뉴스 태그 파싱 강화)
# ==========================================
def fetch_naver_issue_articles(issue_url: str, category_name: str):
    """네이버 뉴스 이슈 페이지에서 최근 기사를 다각도로 파싱합니다."""
    print(f"[{category_name}] 네이버 뉴스 수집 시작: {issue_url}")
    res = requests.get(issue_url, headers=HEADERS)
    soup = BeautifulSoup(res.text, "html.parser")
    
    # 다양한 네이버 이슈 페이지 구조 대응 선택자
    articles = soup.select("a[href*='article'], a.news_tit, a.cjs_news_a, div.news_text a, li a")
    
    collected_urls = set()
    collected_count = 0

    for a in articles:
        url = a.get("href")
        title = a.get_text(strip=True)
        
        # 유효한 기사 링크만 필터링 (네이버 뉴스 본문 URL 패턴 검사)
        if not url or not ("mnews.naver.com" in url or "news.naver.com" in url or "/article/" in url):
            continue
            
        if url.startswith("//"):
            url = "https:" + url
        elif url.startswith("/"):
            url = "https://media.naver.com" + url

        if url in collected_urls or len(title) < 5:
            continue

        collected_urls.add(url)

        # 중복 저장 여부 확인
        if is_already_collected(url):
            print(f"ℹ️ 이미 수집된 기사 스킵: {title}")
            continue

        # 기사 본문 가져오기
        try:
            art_res = requests.get(url, headers=HEADERS)
            art_soup = BeautifulSoup(art_res.text, "html.parser")
            body = art_soup.find("article") or art_soup.find("div", id="newsct_article") or art_soup.find("div", id="articleBodyContents")
            body_text = body.get_text(strip=True) if body else title

            # Gemini 요약 생성
            summary = summarize_with_gemini(title, body_text)

            # Firestore 저장
            doc_data = {
                "title": title,
                "url": url,
                "summary": summary,
                "category": category_name,
                "is_bookmarked": False,
                "created_at": datetime.now(timezone.utc)
            }
            db.collection("news-summary").add(doc_data)
            print(f"✅ 저장 완료: [{category_name}] {title}")
            
            collected_count += 1
            if collected_count >= 5: # 1회 수집 시 카테고리당 최대 5개 기사
                break

        except Exception as e:
            print(f"❌ 기사 처리 중 오류 발생 ({url}): {e}")

def fetch_semi_engineering():
    """SemiEngineering에서 아티클 수집"""
    print("[SemiEngineering] 아티클 수집 시작...")
    list_url = "https://semiengineering.com/author/se-staff/"
    res = requests.get(list_url, headers=HEADERS)
    soup = BeautifulSoup(res.text, "html.parser")
    
    for article in soup.select("article, h3.entry-title a, h2 a"):
        title = article.get_text(strip=True)
        url = article.get("href") if article.name == "a" else (article.find("a").get("href") if article.find("a") else None)
        
        if url and "Chip Industry Week In Review" in title:
            if is_already_collected(url):
                print(f"ℹ️ 이미 수집된 아티클입니다: {title}")
                break
            
            art_res = requests.get(url, headers=HEADERS)
            art_soup = BeautifulSoup(art_res.text, "html.parser")
            body = art_soup.find("div", class_="entry-content")
            body_text = body.get_text(strip=True) if body else title
            
            summary = summarize_with_gemini(title, body_text)
            
            doc_data = {
                "title": title,
                "url": url,
                "summary": summary,
                "category": "Chip Industry Week In Review",
                "is_bookmarked": True,
                "created_at": datetime.now(timezone.utc)
            }
            db.collection("news-summary").add(doc_data)
            print(f"✅ 해외 주간 아티클 저장 완료: {title}")
            break

# ==========================================
# 4. 부가 기능
# ==========================================
def cleanup_old_articles():
    """7일 이상 지난 미북마크 기사 정리"""
    print("🧹 오래된 기사 정리를 시작합니다...")
    seven_days_ago = datetime.now(timezone.utc) - timedelta(days=7)
    
    docs = db.collection("news-summary") \
             .where("created_at", "<", seven_days_ago) \
             .stream()
    
    count = 0
    for doc in docs:
        data = doc.to_dict()
        if not data.get("is_bookmarked", False):
            doc.reference.delete()
            count += 1
    print(f"🧹 총 {count}개의 오래된 기사가 삭제되었습니다.")

def generate_weekly_report():
    """주간 보고서 생성"""
    print("📊 주간 종합 보고서 생성을 시작합니다...")
    seven_days_ago = datetime.now(timezone.utc) - timedelta(days=7)
    
    docs = db.collection("news-summary") \
             .where("created_at", ">=", seven_days_ago) \
             .stream()
    
    summaries_text = ""
    for doc in docs:
        data = doc.to_dict()
        summaries_text += f"- [{data.get('category')}] {data.get('title')}\n  요약: {data.get('summary')}\n\n"
    
    if summaries_text:
        weekly_summary = summarize_with_gemini("주간 종합 리포트", summaries_text, is_weekly_report=True)
        
        report_data = {
            "title": f"주간 반도체 & AI 종합 리포트 ({datetime.now().strftime('%Y-%m-%d')})",
            "url": "",
            "summary": weekly_summary,
            "category": "주간 보고서",
            "is_bookmarked": True,
            "created_at": datetime.now(timezone.utc)
        }
        db.collection("news-summary").add(report_data)
        print("✅ 주간 종합 보고서 저장 완료!")

# ==========================================
# 5. 메인 실행
# ==========================================
if __name__ == "__main__":
    now_utc = datetime.now(timezone.utc)
    is_saturday = (now_utc.weekday() == 5)

    fetch_naver_issue_articles("https://media.naver.com/issue/092/102", "반도체 전쟁")
    fetch_naver_issue_articles("https://media.naver.com/issue/092/492", "AI 핫트렌드")
    
    if is_saturday:
        fetch_semi_engineering()
        generate_weekly_report()
        
    cleanup_old_articles()
