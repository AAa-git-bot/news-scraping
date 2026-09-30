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
# Firebase 인증 (GitHub Secrets의 FIREBASE_SERVICE_ACCOUNT 활용)
firebase_json_str = os.environ.get("FIREBASE_SERVICE_ACCOUNT")
if firebase_json_str:
    cred_dict = json.loads(firebase_json_str)
    cred = credentials.Certificate(cred_dict)
    firebase_admin.initialize_app(cred)
else:
    # 로컬 테스트 시 serviceAccountKey.json 파일 이용 가능
    cred = credentials.Certificate("serviceAccountKey.json")
    firebase_admin.initialize_app(cred)

db = firestore.client()

# Google Gemini API 클라이언트 초기화
gemini_api_key = os.environ.get("GEMINI_API_KEY")
client = genai.Client(api_key=gemini_api_key)

# 웹 크롤링용 Request Header
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
}

# ==========================================
# 2. 헬퍼 함수 정의
# ==========================================
def is_already_collected(url: str) -> bool:
    """Firestore 'news-summary' 컬렉션에서 동일한 URL 문서가 이미 존재하는지 체크합니다."""
    docs = db.collection("news-summary").where("url", "==", url).limit(1).stream()
    return any(docs)

def summarize_with_gemini(title: str, text: str, is_weekly_report: bool = False) -> dict:
    """Gemini API를 이용해 핵심 요약 3줄과 한 줄 인사이트를 생성합니다."""
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
        [기사 본문]: {text[:3000]} # 글자 수 제한 고려

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
# 3. 크롤링 로직
# ==========================================
def fetch_naver_issue_articles(issue_url: str, category_name: str):
    """네이버 뉴스 이슈 페이지에서 최근 기사를 수집합니다."""
    print(f"[{category_name}] 네이버 뉴스 수집 시작: {issue_url}")
    res = requests.get(issue_url, headers=HEADERS)
    soup = BeautifulSoup(res.text, "html.parser")
    
    # 이슈 페이지 내 기사 링크 추출
    articles = soup.select("a.news_tit, a.cjs_news_a, li.news_item a")
    
    for a in articles[:10]: # 하루 상위 10개 기사만 대상으로 검사
        url = a.get("href")
        title = a.get_text(strip=True)
        if not url or not url.startswith("http") or is_already_collected(url):
            continue
        
        # 기사 상세 본문 수집
        try:
            art_res = requests.get(url, headers=HEADERS)
            art_soup = BeautifulSoup(art_res.text, "html.parser")
            body = art_soup.find("article") or art_soup.find("div", id="newsct_article")
            body_text = body.get_text(strip=True) if body else title
            
            # Gemini 요약
            summary = summarize_with_gemini(title, body_text)
            
            # Firestore 저장
            doc_data = {
                "title": title,
                "url": url,
                "summary": summary,
                "category": category_name,
                "is_bookmarked": False, # 기본값은 북마크 안 됨
                "created_at": datetime.now(timezone.utc)
            }
            db.collection("news-summary").add(doc_data)
            print(f"✅ 저장 완료: [{category_name}] {title}")
        except Exception as e:
            print(f"❌ 기사 수집 중 오류 발생 ({url}): {e}")

def fetch_semi_engineering():
    """SemiEngineering에서 'Chip Industry Week In Review' 아티클을 수집합니다."""
    print("[SemiEngineering] 아티클 수집 시작...")
    list_url = "https://semiengineering.com/author/se-staff/"
    res = requests.get(list_url, headers=HEADERS)
    soup = BeautifulSoup(res.text, "html.parser")
    
    # 'Chip Industry Week In Review'가 포함된 링크 찾기
    for article in soup.select("article, h3.entry-title a, h2 a"):
        title = article.get_text(strip=True)
        url = article.get("href") if article.name == "a" else (article.find("a").get("href") if article.find("a") else None)
        
        if url and "Chip Industry Week In Review" in title:
            if is_already_collected(url):
                print(f"ℹ️ 이미 수집된 아티클입니다: {title}")
                break
            
            # 본문 수집
            art_res = requests.get(url, headers=HEADERS)
            art_soup = BeautifulSoup(art_res.text, "html.parser")
            body = art_soup.find("div", class_="entry-content")
            body_text = body.get_text(strip=True) if body else title
            
            # Gemini 요약 및 인사이트 작성
            summary = summarize_with_gemini(title, body_text)
            
            doc_data = {
                "title": title,
                "url": url,
                "summary": summary,
                "category": "Chip Industry Week In Review",
                "is_bookmarked": True, # 해외 주간 리뷰는 자동 보관 처리
                "created_at": datetime.now(timezone.utc)
            }
            db.collection("news-summary").add(doc_data)
            print(f"✅ 해외 주간 아티클 저장 완료: {title}")
            break

# ==========================================
# 4. 부가 기능 (7일 이상 지난 기사 삭제 & 주간 보고서 생성)
# ==========================================
def cleanup_old_articles():
    """북마크 처리되지 않은 7일 이전 기사들을 삭제합니다."""
    print("🧹 오래된 기사 정리를 시작합니다 (7일 경과 & 북마크 안 됨)...")
    seven_days_ago = datetime.now(timezone.utc) - timedelta(days=7)
    
    # 7일 이전 + 북마크되지 않은 문서 조회
    docs = db.collection("news-summary") \
             .where("is_bookmarked", "==", False) \
             .where("created_at", "<", seven_days_ago) \
             .stream()
    
    count = 0
    for doc in docs:
        doc.reference.delete()
        count += 1
    print(f"🧹 총 {count}개의 오래된 기사가 삭제되었습니다.")

def generate_weekly_report():
    """지난 일주일간의 기사 요약본을 모아 주간 보고서를 작성합니다."""
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
            "is_bookmarked": True, # 주간 보고서는 항상 삭제 방지
            "created_at": datetime.now(timezone.utc)
        }
        db.collection("news-summary").add(report_data)
        print("✅ 주간 종합 보고서 저장 완료!")

# ==========================================
# 5. 실행 제어 (메인 함수)
# ==========================================
if __name__ == "__main__":
    now_utc = datetime.now(timezone.utc)
    is_saturday = (now_utc.weekday() == 5) # 토요일 검사 (0:월 ~ 5:토)

    # 1. 매일 수행하는 작업: 네이버 뉴스 수집
    fetch_naver_issue_articles("https://media.naver.com/issue/092/102", "반도체 전쟁")
    fetch_naver_issue_articles("https://media.naver.com/issue/092/492", "AI 핫트렌드")
    
    # 2. 토요일에만 수행하는 작업
    if is_saturday:
        fetch_semi_engineering() # 해외 주간 아티클 수집
        generate_weekly_report()  # 주간 종합 보고서 생성
        
    # 3. 매일 7일 지난 기사 정리 (북마크 기사는 제외)
    cleanup_old_articles()