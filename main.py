import os
import json
import requests
from bs4 import BeautifulSoup
from datetime import datetime, timedelta, timezone
from google import genai
import firebase_admin
from firebase_admin import credentials, firestore

print("🚀 스크립트 시작: 환경 변수 및 서비스 초기화를 진행합니다...")

# ==========================================
# 1. 환경 변수 및 서비스 초기화
# ==========================================
firebase_json_str = os.environ.get("FIREBASE_SERVICE_ACCOUNT")
if firebase_json_str:
    print("🔑 GitHub Secrets에서 FIREBASE_SERVICE_ACCOUNT를 불러왔습니다.")
    cred_dict = json.loads(firebase_json_str)
    cred = credentials.Certificate(cred_dict)
    firebase_admin.initialize_app(cred)
else:
    print("🔑 로컬 serviceAccountKey.json 파일을 사용합니다.")
    cred = credentials.Certificate("serviceAccountKey.json")
    firebase_admin.initialize_app(cred)

db = firestore.client()

gemini_api_key = os.environ.get("GEMINI_API_KEY")
if not gemini_api_key:
    print("⚠️ GEMINI_API_KEY가 등록되어 있지 않습니다!")
client = genai.Client(api_key=gemini_api_key)

HEADERS = {
    "User-Agent": "Mozilla/5.0 (iPhone; CPU iPhone OS 16_0 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/16.0 Mobile/15E148 Safari/604.1"
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
# 3. 크롤링 로직 (모바일/PC 하이브리드 네이버 수집)
# ==========================================
def fetch_naver_issue_articles(issue_url: str, category_name: str):
    print(f"\n[네이버 크롤링] {category_name} 수집 시작... ({issue_url})")
    
    # 모바일 주소 변환 (크롤링이 훨씬 잘 됨)
    m_url = issue_url.replace("media.naver.com", "m.news.naver.com")
    res = requests.get(m_url, headers=HEADERS)
    soup = BeautifulSoup(res.text, "html.parser")
    
    # 기사 링크 추출
    links = soup.find_all("a")
    valid_links = []
    
    for a in links:
        href = a.get("href", "")
        title = a.get_text(strip=True)
        if "/article/" in href and len(title) > 8:
            if not href.startswith("http"):
                href = "https://n.news.naver.com" + href if href.startswith("/") else "https://n.news.naver.com/" + href
            valid_links.append((title, href))

    print(f"🔎 발견된 기사 후보 수: {len(valid_links)}개")
    
    collected_count = 0
    saved_urls = set()

    for title, url in valid_links:
        if url in saved_urls:
            continue
        saved_urls.add(url)

        if is_already_collected(url):
            print(f"ℹ️ [중복 스킵] {title[:20]}...")
            continue

        try:
            print(f"📄 기사 본문 수집 중: {title[:25]}...")
            art_res = requests.get(url, headers=HEADERS)
            art_soup = BeautifulSoup(art_res.text, "html.parser")
            
            body = art_soup.find("article") or art_soup.find("div", id="newsct_article") or art_soup.find("div", id="dic_area")
            body_text = body.get_text(strip=True) if body else title

            print("🤖 Gemini 요약 요청 중...")
            summary = summarize_with_gemini(title, body_text)

            doc_data = {
                "title": title,
                "url": url,
                "summary": summary,
                "category": category_name,
                "is_bookmarked": False,
                "created_at": datetime.now(timezone.utc)
            }
            db.collection("news-summary").add(doc_data)
            print(f"✅ [Firestore 저장 완료] {title[:25]}...")
            
            collected_count += 1
            if collected_count >= 5: # 카테고리당 최대 5개 수집
                break

        except Exception as e:
            print(f"❌ 기사 처리 중 오류: {e}")

def fetch_semi_engineering():
    print("\n[SemiEngineering] 아티클 수집 시작...")
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
    print("\n🧹 오래된 기사 정리 시작...")
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
    print("\n📊 주간 종합 보고서 생성 시작...")
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
    print("\n🎉 모든 작업이 정상적으로 종료되었습니다.")
