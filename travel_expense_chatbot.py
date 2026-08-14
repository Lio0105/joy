import streamlit as st
import json
import requests
import urllib3
import datetime
import io
import os
from openai import OpenAI

# PDF 생성을 위한 reportlab 라이브러리 및 폰트 설정
from reportlab.lib.pagesizes import letter
from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle, HRFlowable
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib import colors
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont

# SSL 경고 메시지 비활성화 (수출입은행 API가 인증서 이슈로 verify=False가 필요한 경우 대비)
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# ---------------------------------------------------------
# 0. API 키 — 실제 배포 시에는 st.secrets 사용 권장
#    (지금은 무료 티어라 하드코딩 유지, 필요해지면
#     st.secrets["UPSTAGE_API_KEY"] 형태로 바꾸면 됨)
# ---------------------------------------------------------
UPSTAGE_API_KEY = "up_Y7OKHBUB2q7pi7C4E1ILIWItBAUOG"
EXIM_AUTH_KEY = "OnS9ZZMNvhJAtIOKWXbF6TDHydWXTL1B"
OWM_API_KEY = "93cc316fee1e1cf074a205add809d49e"

CURRENCY_KOR_MAP = {
    "USD": "달러", "JPY": "엔", "EUR": "유로",
    "THB": "바트", "TWD": "대만 달러", "VND": "동",
    "CNY": "위안", "GBP": "파운드", "AUD": "호주 달러",
}
FALLBACK_RATES = {
    "USD": 1350.0, "JPY": 9.1, "EUR": 1470.0, "THB": 37.0,
    "TWD": 42.0, "VND": 0.055, "CNY": 190.0, "GBP": 1720.0, "AUD": 890.0,
}

st.set_page_config(page_title="Upstage Solar - 여행 경비 계산 AI 챗봇", page_icon="☀️", layout="wide")

client = OpenAI(api_key=UPSTAGE_API_KEY, base_url="https://api.upstage.ai/v1/solar")

# ---------------------------------------------------------
# 1. 한글 폰트 등록 (실패해도 앱이 죽지 않도록 방어)
# ---------------------------------------------------------
FONT_NAME = "NanumGothic"


@st.cache_resource
def register_korean_font():
    font_path = "NanumGothic.ttf"
    try:
        if not os.path.exists(font_path):
            url = "https://github.com/google/fonts/raw/main/ofl/nanumgothic/NanumGothic-Regular.ttf"
            r = requests.get(url, timeout=10)
            r.raise_for_status()
            with open(font_path, "wb") as f:
                f.write(r.content)
        pdfmetrics.registerFont(TTFont(FONT_NAME, font_path))
        return FONT_NAME
    except Exception as e:
        print(f"[폰트 등록 실패] {e} — Helvetica로 대체 (한글이 깨질 수 있음)")
        return "Helvetica"


font_to_use = register_korean_font()

# ---------------------------------------------------------
# 2. 세션 기본값 초기화
# ---------------------------------------------------------
defaults = {
    "start_date": datetime.date.today() + datetime.timedelta(days=7),
    "travel_members": 2,
    "flight_class": "LCC (저가항공)",
    "hotel_type": "3성급 (가성비 호텔)",
}
for k, v in defaults.items():
    if k not in st.session_state:
        st.session_state[k] = v
if "end_date" not in st.session_state:
    st.session_state.end_date = st.session_state.start_date + datetime.timedelta(days=3)

# ---------------------------------------------------------
# 3. 유틸리티 함수들
# ---------------------------------------------------------


def safe_json_parse(raw_text: str):
    """LLM이 코드블록(```json ... ```)이나 앞뒤 설명을 붙여도 최대한 JSON을 뽑아낸다."""
    text = raw_text.strip()
    if text.startswith("```"):
        text = text.strip("`")
        if text.lower().startswith("json"):
            text = text[4:]
    start, end = text.find("{"), text.rfind("}")
    if start != -1 and end != -1:
        text = text[start:end + 1]
    return json.loads(text)


def extract_destination_info(messages_history):
    full_text = "\n".join([f"{m['role']}: {m['content']}" for m in messages_history if m['role'] != 'system'])

    prompt_extract = f"""
    다음 대화 내역을 읽고 사용자가 가려는 '여행 국가/도시명', '해당 도시의 영문명(OpenWeatherMap API용)',
    '주요 통화코드(USD, JPY, EUR, THB, TWD, VND, CNY, GBP, AUD 중 하나)'를 JSON으로 추출해줘.
    사용자가 어디로 갈지 도시나 국가를 명확히 말하지 않았다면 country를 "미정"으로 반환해줘.

    예시: {{"country": "일본 도쿄", "city_en": "Tokyo", "currency": "JPY"}}

    대화 내역:
    {full_text}
    """
    try:
        res = client.chat.completions.create(
            model="solar-pro",
            messages=[{"role": "user", "content": prompt_extract}],
            response_format={"type": "json_object"},
        )
        extracted = safe_json_parse(res.choices[0].message.content)
        currency = extracted.get("currency", "JPY").upper()
        if currency not in FALLBACK_RATES:
            currency = "JPY"
        return (
            extracted.get("country", "미정"),
            extracted.get("city_en", "Tokyo"),
            currency,
            CURRENCY_KOR_MAP.get(currency, currency),
        )
    except Exception as e:
        print(f"[목적지 추출 오류] {e}")
        return ("미정", "Tokyo", "JPY", "엔")


def get_weather_info(city_en: str, start_date, end_date) -> str:
    """5일 이내면 실제 예보(forecast), 그 이후면 '오늘 기준 참고' 날씨임을 명시한다."""
    date_str = f"[{start_date.strftime('%Y/%m/%d')} ~ {end_date.strftime('%Y/%m/%d')}]"
    days_until_trip = (start_date - datetime.date.today()).days

    try:
        if 0 <= days_until_trip <= 5:
            url = (
                f"https://api.openweathermap.org/data/2.5/forecast"
                f"?q={city_en}&appid={OWM_API_KEY}&units=metric&lang=kr"
            )
            res = requests.get(url, timeout=5).json()
            if res.get("cod") == "200" and res.get("list"):
                target_str = start_date.strftime("%Y-%m-%d")
                match = next((item for item in res["list"] if item["dt_txt"].startswith(target_str)), res["list"][0])
                temp = match["main"]["temp"]
                desc = match["weather"][0]["description"]
                return f"여행 시작일 {date_str} 예보: {desc} (약 {temp:.1f}°C)"

        # 5일 초과 or 예보 조회 실패 → 현재 날씨를 '참고용'으로만 안내
        url = f"https://api.openweathermap.org/data/2.5/weather?q={city_en}&appid={OWM_API_KEY}&units=metric&lang=kr"
        res_curr = requests.get(url, timeout=5).json()
        if res_curr.get("cod") == 200:
            temp_curr = res_curr['main']['temp']
            desc_curr = res_curr['weather'][0]['description']
            return f"{date_str} 정확한 예보는 아직 어려워요. (오늘 기준 참고 날씨: {desc_curr}, {temp_curr:.1f}°C)"
        return f"{date_str}: 날씨 정보를 불러오지 못했어요. 출발 임박 시 다시 확인해주세요."
    except Exception as e:
        print(f"[날씨 API 예외] {e}")
        return f"{date_str}: 날씨 정보를 불러오는 중 오류가 발생했습니다."


def get_exchange_rate(target_currency: str) -> float:
    target_currency = target_currency.upper()
    url = "https://www.koreaexim.go.kr/site/program/financial/exchangeJSON"
    params = {"authkey": EXIM_AUTH_KEY, "searchdate": "", "data": "AP01"}
    try:
        response = requests.get(url, params=params, timeout=5, verify=False)
        data = response.json()
        for item in data:
            cur_unit = item.get("cur_unit", "")
            code = cur_unit.split("(")[0].strip()
            if code == target_currency:
                rate = float(item["deal_bas_r"].replace(",", ""))
                if "(100)" in cur_unit:
                    rate /= 100.0
                return rate
    except Exception as e:
        print(f"[환율 API 예외] {e}")
    return FALLBACK_RATES.get(target_currency, 1350.0)


def generate_itinerary_text(destination_city, nights):
    prompt = f"""
    {destination_city} {nights}박 {nights + 1}일 여행에 적합한 추천 일정표를 작성해줘.

    [작성 규칙]
    1. 별표 문자는 절대 사용하지 말 것. (볼드체 강조 표현 완전히 금지)
    2. 지명, 맛집, 관광지 이름은 실제로 존재하는 정확한 명칭만 사용할 것.
    3. 시간대와 활동이 현실적이어야 함 (낮 시간 야경 투어 금지).
    4. 대시(-) 및 일반 텍스트만 사용하여 깔끔하게 작성할 것.
    """
    try:
        res = client.chat.completions.create(model="solar-pro", messages=[{"role": "user", "content": prompt}])
        return res.choices[0].message.content.replace("*", "")
    except Exception as e:
        print(f"[일정 생성 오류] {e}")
        return "일정을 불러오는 중 오류가 발생했습니다. 잠시 후 다시 시도해주세요."


def scaled_defaults(members: int, full_days: int) -> dict:
    """LLM 호출이 실패했을 때 쓰는 폴백 값. 인원수·기간에 비례하도록 계산."""
    return {
        "flight_min": members * 200000, "flight_max": members * 300000,
        "hotel_min": full_days * 100000, "hotel_max": full_days * 180000,
        "food_min": members * full_days * 30000, "food_max": members * full_days * 60000,
        "transport_min": members * full_days * 15000, "transport_max": members * full_days * 25000,
        "activity_min": members * full_days * 20000, "activity_max": members * full_days * 40000,
        "extra_min": members * full_days * 15000, "extra_max": members * full_days * 25000,
        "advice": "AI 응답을 일시적으로 받지 못해 인원/기간 기반 추정치로 대체했어요. 다시 질문하시면 더 정확한 값을 받을 수 있어요.",
    }


def generate_travel_consulting_response(messages_history):
    destination, city_en, current_currency, currency_kor_name = extract_destination_info(messages_history)

    if destination == "미정":
        return "안녕하세요! ✈️ 어느 나라나 도시로 떠나실 예정이신가요? 가고 싶으신 곳을 편하게 말씀해 주세요!", "미정"

    start_d = st.session_state.start_date
    end_d = st.session_state.end_date
    t_members = st.session_state.travel_members
    f_class = st.session_state.flight_class
    h_type = st.session_state.hotel_type

    t_nights = (end_d - start_d).days
    t_full_days = t_nights + 1

    rate = get_exchange_rate(current_currency)
    weather_summary = get_weather_info(city_en, start_d, end_d)

    system_prompt = f"""
    당신은 해외여행 경비 산정 전문 컨설턴트입니다.
    사용자의 입력과 조건을 바탕으로 '현실적이고 정확한 경비 데이터'를 JSON 규격으로만 작성해야 합니다.
    설명, 코드블록 표시 없이 순수 JSON 객체 하나만 출력하세요.

    [조건]
    - 목적지: {destination}
    - 기간: {t_nights}박 {t_full_days}일
    - 인원: {t_members}명 (모든 금액은 {t_members}명 전체 기준 총액)
    - 항공 등급: {f_class}
    - 숙소 등급: {h_type}
    - 환율: 1 {currency_kor_name} = {rate:.2f} 원

    반드시 아래 JSON 형식으로만 응답하세요:
    {{
        "flight_min": 숫자, "flight_max": 숫자,
        "hotel_min": 숫자, "hotel_max": 숫자,
        "food_min": 숫자, "food_max": 숫자,
        "transport_min": 숫자, "transport_max": 숫자,
        "activity_min": 숫자, "activity_max": 숫자,
        "extra_min": 숫자, "extra_max": 숫자,
        "advice": "날씨 및 여행 팁 2~3줄 요약"
    }}
    """

    used_fallback = False
    try:
        res = client.chat.completions.create(
            model="solar-pro",
            messages=[{"role": "system", "content": system_prompt}, {"role": "user", "content": "경비 산정해줘"}],
            response_format={"type": "json_object"},
        )
        data = safe_json_parse(res.choices[0].message.content)
        required_keys = ["flight_min", "flight_max", "hotel_min", "hotel_max", "food_min", "food_max",
                          "transport_min", "transport_max", "activity_min", "activity_max",
                          "extra_min", "extra_max", "advice"]
        if not all(k in data for k in required_keys):
            raise ValueError("응답에 필요한 키가 누락됨")
    except Exception as e:
        print(f"[경비 산정 LLM 오류] {e}")
        data = scaled_defaults(t_members, t_full_days)
        used_fallback = True

    st.session_state['cost_data'] = data
    st.session_state['currency_info'] = (current_currency, currency_kor_name, rate)

    total_min = sum(data[k] for k in ['flight_min', 'hotel_min', 'food_min', 'transport_min', 'activity_min', 'extra_min'])
    total_max = sum(data[k] for k in ['flight_max', 'hotel_max', 'food_max', 'transport_max', 'activity_max', 'extra_max'])

    def to_curr(krw_val):
        val = krw_val / rate
        val = round(val, -2) if current_currency == "JPY" else round(val)
        return f"{int(val):,} {currency_kor_name}"

    warning_line = "\n\n⚠️ AI 응답을 일시적으로 받지 못해 인원/기간 기반 추정치를 보여드리고 있어요. 다시 물어보시면 더 정확해질 수 있어요." if used_fallback else ""

    formatted_response = f"""
### 🗓️ {destination} 여행 개요
- 일정: {start_d.strftime('%Y/%m/%d')} ~ {end_d.strftime('%Y/%m/%d')} ({t_nights}박 {t_full_days}일)
- 인원: {t_members}명
- 날씨 안내: {weather_summary}

---

### 💼 예상 경비 내역 (전체 총액, {t_members}인 기준)

| 항목 | 원화 (KRW) | {currency_kor_name} ({current_currency}) |
| :--- | :--- | :--- |
| 항공료 | {data['flight_min']:,} ~ {data['flight_max']:,}원 | {to_curr(data['flight_min'])} ~ {to_curr(data['flight_max'])} |
| 숙박비 | {data['hotel_min']:,} ~ {data['hotel_max']:,}원 | {to_curr(data['hotel_min'])} ~ {to_curr(data['hotel_max'])} |
| 식비 | {data['food_min']:,} ~ {data['food_max']:,}원 | {to_curr(data['food_min'])} ~ {to_curr(data['food_max'])} |
| 교통비 | {data['transport_min']:,} ~ {data['transport_max']:,}원 | {to_curr(data['transport_min'])} ~ {to_curr(data['transport_max'])} |
| 액티비티 | {data['activity_min']:,} ~ {data['activity_max']:,}원 | {to_curr(data['activity_min'])} ~ {to_curr(data['activity_max'])} |
| 비상금 | {data['extra_min']:,} ~ {data['extra_max']:,}원 | {to_curr(data['extra_min'])} ~ {to_curr(data['extra_max'])} |
| 총 예상 비용 | {total_min:,} ~ {total_max:,}원 | {to_curr(total_min)} ~ {to_curr(total_max)} |

---

### 📌 여행 팁 & 참고사항
- {data['advice']}{warning_line}
    """
    return formatted_response, destination


# ---------------------------------------------------------
# 4. PDF 생성
# ---------------------------------------------------------
def generate_pdf_bytes(dest, start_date, end_date, members, cost_data, itinerary_text):
    buffer = io.BytesIO()
    doc = SimpleDocTemplate(buffer, pagesize=letter, rightMargin=40, leftMargin=40, topMargin=40, bottomMargin=40)

    PRIMARY_COLOR = colors.HexColor("#1E293B")
    ACCENT_COLOR = colors.HexColor("#2563EB")
    BG_LIGHT = colors.HexColor("#F8FAFC")
    TEXT_DARK = colors.HexColor("#334155")

    title_style = ParagraphStyle('CustomTitle', fontName=font_to_use, fontSize=22, leading=26, textColor=PRIMARY_COLOR, spaceAfter=6)
    subtitle_style = ParagraphStyle('CustomSubtitle', fontName=font_to_use, fontSize=11, leading=15, textColor=TEXT_DARK, spaceAfter=15)
    section_heading = ParagraphStyle('SectionHeading', fontName=font_to_use, fontSize=14, leading=18, textColor=ACCENT_COLOR, spaceBefore=12, spaceAfter=8)
    body_style = ParagraphStyle('CustomBody', fontName=font_to_use, fontSize=9.5, leading=14, textColor=TEXT_DARK)
    table_header_style = ParagraphStyle('TableHeader', fontName=font_to_use, fontSize=10, leading=12, textColor=colors.whitesmoke, alignment=1)
    table_cell_style = ParagraphStyle('TableCell', fontName=font_to_use, fontSize=9, leading=12, textColor=TEXT_DARK, alignment=1)

    elements = [Paragraph(f"<b>{dest} 여행 경비 견적 및 일정표</b>", title_style)]

    t_days = (end_date - start_date).days
    info_str = f"<b>여행 기간:</b> {start_date.strftime('%Y년 %m월 %d일')} ~ {end_date.strftime('%Y년 %m월 %d일')} ({t_days}박 {t_days+1}일) &nbsp;|&nbsp; <b>인원:</b> {members}명"
    elements.append(Paragraph(info_str, subtitle_style))
    elements.append(HRFlowable(width="100%", thickness=1.5, color=ACCENT_COLOR, spaceBefore=0, spaceAfter=15))

    if cost_data:
        elements.append(Paragraph("<b>💰 예상 경비 세부 내역</b>", section_heading))
        rows = [("항공료", "flight"), ("숙박비", "hotel"), ("식비", "food"),
                ("교통비", "transport"), ("액티비티", "activity"), ("비상금", "extra")]
        table_data = [[
            Paragraph("<b>항목</b>", table_header_style),
            Paragraph("<b>최소 경비 (원)</b>", table_header_style),
            Paragraph("<b>최대 경비 (원)</b>", table_header_style),
        ]]
        for label, key in rows:
            table_data.append([
                Paragraph(label, table_cell_style),
                Paragraph(f"{cost_data.get(f'{key}_min', 0):,} 원", table_cell_style),
                Paragraph(f"{cost_data.get(f'{key}_max', 0):,} 원", table_cell_style),
            ])

        tot_min = sum(cost_data.get(f"{k}_min", 0) for _, k in rows)
        tot_max = sum(cost_data.get(f"{k}_max", 0) for _, k in rows)
        total_header_style = ParagraphStyle('TotH', parent=table_header_style, textColor=PRIMARY_COLOR)
        table_data.append([
            Paragraph("<b>총 예상 비용</b>", total_header_style),
            Paragraph(f"<b>{tot_min:,} 원</b>", total_header_style),
            Paragraph(f"<b>{tot_max:,} 원</b>", total_header_style),
        ])

        t = Table(table_data, colWidths=[160, 180, 180])
        t.setStyle(TableStyle([
            ('BACKGROUND', (0, 0), (-1, 0), PRIMARY_COLOR),
            ('ALIGN', (0, 0), (-1, -1), 'CENTER'),
            ('VALIGN', (0, 0), (-1, -1), 'MIDDLE'),
            ('BOTTOMPADDING', (0, 0), (-1, 0), 6),
            ('TOPPADDING', (0, 0), (-1, 0), 6),
            ('GRID', (0, 0), (-1, -2), 0.5, colors.HexColor("#CBD5E1")),
            ('ROWBACKGROUNDS', (0, 1), (-1, -2), [colors.white, BG_LIGHT]),
            ('BACKGROUND', (0, -1), (-1, -1), colors.HexColor("#E2E8F0")),
            ('LINEABOVE', (0, -1), (-1, -1), 1.5, PRIMARY_COLOR),
            ('BOTTOMPADDING', (0, -1), (-1, -1), 8),
            ('TOPPADDING', (0, -1), (-1, -1), 8),
        ]))
        elements.append(t)
        elements.append(Spacer(1, 15))

    elements.append(Paragraph("<b>🗺️ 추천 여행 코스 및 일정</b>", section_heading))
    clean_itinerary = (itinerary_text or "작성된 일정이 없습니다.").replace('*', '').replace('\n', '<br/>')
    elements.append(Paragraph(clean_itinerary, body_style))

    doc.build(elements)
    buffer.seek(0)
    return buffer.getvalue()


# ---------------------------------------------------------
# 5. 메인 레이아웃
# ---------------------------------------------------------
st.title("☀️ Upstage Solar - 여행 경비 계산 AI 챗봇")
st.caption("실시간 환율 API와 Upstage Solar AI 모델을 연동하여 맞춤형 여행 경비를 원화(KRW) 및 현지 통화로 계산해 드립니다.")

tab1, tab2, tab3 = st.tabs(["💬 Solar AI 대화형 질의", "🗺️ 여행 코스 편집", "🎛️ 수동 상세 설정"])

# ---------------------------------------------------------
# TAB 1: 대화형 챗봇
# ---------------------------------------------------------
with tab1:
    if "messages" not in st.session_state or len(st.session_state.messages) == 0:
        st.session_state.messages = [
            {"role": "assistant", "content": "안녕하세요! ✈️ 어느 나라나 도시로 떠나실 예정이신가요?\n\n"
                                              "채팅으로 '도쿄 3박 4일', '방콕 맛집 여행'처럼 가고 싶으신 곳을 편하게 말씀해 주세요!"}
        ]

    for message in st.session_state.messages:
        with st.chat_message(message["role"]):
            st.markdown(message["content"])

    if prompt := st.chat_input("여행지나 추가 요구사항을 입력하세요..."):
        st.chat_message("user").markdown(prompt)
        st.session_state.messages.append({"role": "user", "content": prompt})

        with st.chat_message("assistant"):
            with st.spinner("목적지 분석 및 경비 산정 중..."):
                answer, detected_dest = generate_travel_consulting_response(st.session_state.messages)
                st.markdown(answer)
                if detected_dest != "미정":
                    st.session_state['current_dest'] = detected_dest

        st.session_state.messages.append({"role": "assistant", "content": answer})

    current_dest = st.session_state.get('current_dest', None)
    if current_dest and current_dest != "미정":
        st.success(f"📍 목적지 [{current_dest}] 분석 완료! 상단 '🗺️ 여행 코스 편집' 탭에서 코스를 확인하세요.")

# ---------------------------------------------------------
# TAB 2: 여행 코스 편집 및 다운로드
# ---------------------------------------------------------
with tab2:
    current_dest = st.session_state.get('current_dest', None)
    if current_dest and current_dest != "미정":
        st.subheader(f"🗺️ [{current_dest}] 일자별 추천 코스 작성 & 내보내기")

        travel_nights = (st.session_state.end_date - st.session_state.start_date).days
        if st.button("✨ AI 추천 코스 불러오기"):
            with st.spinner(f"{current_dest} 맞춤 코스 생성 중..."):
                st.session_state['custom_itinerary'] = generate_itinerary_text(current_dest, travel_nights)

        default_itinerary_text = st.session_state.get(
            'custom_itinerary',
            f"위 [✨ AI 추천 코스 불러오기] 버튼을 누르시거나, 여기에 직접 일정을 자유롭게 작성해 보세요!\n\n📍 Day 1: {current_dest} 도착\n- "
        )
        user_edited_itinerary = st.text_area(
            "나만의 코스 작성 및 수정", value=default_itinerary_text.replace("*", ""), height=300
        )
        st.session_state['custom_itinerary'] = user_edited_itinerary

        st.markdown("---")
        st.subheader("📥 견적서 & 코스 저장")

        col_dl1, col_dl2 = st.columns(2)
        with col_dl1:
            download_text = (
                f"=== {current_dest} 여행 경비 견적서 및 코스 ===\n"
                f"- 기간: {st.session_state.start_date.strftime('%Y/%m/%d')} ~ {st.session_state.end_date.strftime('%Y/%m/%d')} "
                f"/ 인원: {st.session_state.travel_members}명\n\n"
                f"{st.session_state.get('custom_itinerary', '코스 미작성').replace('*', '')}"
            )
            st.download_button(
                label="📄 텍스트 파일 (.txt) 다운로드", data=download_text,
                file_name=f"{current_dest}_여행_견적서.txt", mime="text/plain", use_container_width=True,
            )

        with col_dl2:
            # 버튼을 누를 때만 PDF를 생성해서 매 rerun마다 재생성되는 낭비를 없앰
            if st.button("📕 PDF 견적서 생성", use_container_width=True):
                with st.spinner("PDF 생성 중..."):
                    st.session_state['pdf_bytes'] = generate_pdf_bytes(
                        current_dest, st.session_state.start_date, st.session_state.end_date,
                        st.session_state.travel_members, st.session_state.get('cost_data', {}),
                        st.session_state.get('custom_itinerary', '').replace("*", ""),
                    )
            if 'pdf_bytes' in st.session_state:
                st.download_button(
                    label="⬇️ PDF 다운로드", data=st.session_state['pdf_bytes'],
                    file_name=f"{current_dest}_여행_견적서.pdf", mime="application/pdf", use_container_width=True,
                )
    else:
        st.info("💡 대화 창에서 목적지를 입력하시면 맞춤 일정 코스가 생성됩니다.")

# ---------------------------------------------------------
# TAB 3: 수동 상세 설정
# ---------------------------------------------------------
with tab3:
    st.subheader("⚙️ 여행 조건 세부 설정")
    st.caption("AI 산정 시 반영할 세부 옵션을 설정하세요.")

    col_a, col_b = st.columns(2)
    with col_a:
        st.session_state.start_date = st.date_input("🛫 출발일", value=st.session_state.start_date, format="YYYY/MM/DD")
    with col_b:
        st.session_state.end_date = st.date_input("🛬 도착일", value=st.session_state.end_date, format="YYYY/MM/DD")

    if st.session_state.end_date <= st.session_state.start_date:
        st.warning("도착일은 출발일보다 뒤여야 해요. 자동으로 다음 날로 보정했습니다.")
        st.session_state.end_date = st.session_state.start_date + datetime.timedelta(days=1)

    t_days = (st.session_state.end_date - st.session_state.start_date).days
    st.info(f"🗓️ 현재 선택 기간: {st.session_state.start_date.strftime('%Y/%m/%d')} ~ {st.session_state.end_date.strftime('%Y/%m/%d')} ({t_days}박 {t_days+1}일)")

    st.markdown("---")
    col_c, col_d, col_e = st.columns(3)
    with col_c:
        st.number_input("👥 여행 인원 (명)", min_value=1, max_value=20, step=1, key="travel_members")
    with col_d:
        flight_options = ["LCC (저가항공)", "FSC (일반 국적기)", "비즈니스석"]
        st.session_state.flight_class = st.selectbox("✈️ 항공권 등급", flight_options, index=flight_options.index(st.session_state.flight_class))
    with col_e:
        hotel_options = ["게스트하우스/호스텔", "3성급 (가성비 호텔)", "4~5성급 (고급 호텔/리조트)"]
        st.session_state.hotel_type = st.selectbox("🏨 숙소 등급", hotel_options, index=hotel_options.index(st.session_state.hotel_type))

    st.markdown("---")
    if st.button("🔄 전체 대화 내용 초기화"):
        st.session_state.messages = []
        for key in ['current_dest', 'custom_itinerary', 'cost_data', 'pdf_bytes']:
            if key in st.session_state:
                del st.session_state[key]
        st.success("대화 내역이 초기화되었습니다.")
        st.rerun()
