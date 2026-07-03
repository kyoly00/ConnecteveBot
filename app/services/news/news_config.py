# -*- coding: utf-8 -*-
"""
뉴스 1차 키워드 필터 / 소스 tier 설정.
news_crawling.py SEARCH_QUERIES·MONITORING_FOCUS와 정합.
"""

from __future__ import annotations

# 레거시 NEWS_SOURCES 크롤 (news_summary 직접 크롤 시) — 현재는 deduped.json 입력 사용
NEWS_SOURCES: dict = {}

SITE_TIER_A = frozenset({"Google News"})
SITE_TIER_B = frozenset({
    "청년의사",
    "의학신문",
    "메디게이트뉴스",
    "메디컬타임스",
    "데일리메디",
})
SITE_TIER_C = frozenset({
    "네이버뉴스",
    "AI타임스",
    "로봇신문",
})

TIER_SOURCE_BONUS = {"A": 5, "B": 4, "C": 0}

TARGET_COMPANY = {
    "location": "서울 소재",
    "years_in_business": "7년 미만",
    "domain": "정형외과 AI SaMD 및 수술 로봇 연동 소프트웨어 개발",
    "core_products": [
        "CONNEVO KOA: 무릎 X-ray 기반 KL Grade / 퇴행성관절염 분석",
        "CONNEVO ALI/METRIC: 하지 X-ray 기반 valgus/varus, 다리 길이, HKAA 등 계측",
        "CONNEVO R: 무릎 임플란트 수술 로봇 시스템",
        "CONNEVO ASYST: 수술 중 motor 기반 다리 위치 조절 장치",
    ],
}

MONITORING_FOCUS = [
    "의료 AI",
    "의료기기 소프트웨어",
    "디지털헬스",
    "의료 로봇",
    "수술 로봇",
    "병원 AI/AX",
    "의료영상 AI",
    "X-ray AI",
    "AI 의료기기 인허가",
    "수가/비급여/혁신의료기술",
    "정부지원사업/바우처/R&D",
    "의료 AI 투자/M&A/IPO",
    "정형외과/근골격계/무릎/하지정렬/수술계획",
]

EMBED_TOPIC_ANCHORS = [
    "의료 AI 인공지능 의료기기 소프트웨어 SaMD 디지털헬스",
    "수술 로봇 의료 로봇 인공관절 정형외과 근골격계 무릎",
    "의료영상 X-ray AI 진단 판독 병원 AI 도입",
    "AI 의료기기 인허가 식약처 FDA 혁신의료기기",
    "건강보험 수가 비급여 보험등재 신의료기술",
    "의료 AI 투자 디지털헬스 스타트업 정부지원 R&D 실증",
    "정형외과 하지정렬 수술계획 퇴행성관절염 KL grade",
]

KEYWORD_SETS = {
    "primary_filter_kr": [
        "의료 AI", "의료 인공지능", "헬스케어 AI", "AI 의료기기", "의료영상 AI",
        "AI 진단", "AI 판독", "병원 AI", "병원 AX", "의료기기 AI",
        "의료기기 소프트웨어", "소프트웨어 의료기기", "디지털의료기기", "혁신의료기기",
        "의료 로봇", "수술 로봇", "수술로봇", "로봇수술", "재활 로봇",
        "정형외과 로봇", "인공관절 로봇", "병원 AI 도입", "의료 AI 도입",
        "스마트병원", "디지털치료기기", "의료 AI 실증", "의료 AI 상용화",
        "의료데이터", "의료 AI 바우처", "디지털헬스", "의료기기 R&D",
        "의료 AI 투자", "디지털헬스 투자", "의료기기 투자", "의료 로봇 투자",
        "정형외과 AI", "근골격계 AI", "무릎 AI", "인공관절 AI", "하지정렬 AI",
        "X-ray AI", "엑스레이 AI", "수술계획 AI", "퇴행성관절염",
        "의료", "병원", "헬스케어", "의료기기", "수술", "인허가", "수가",
        "급여", "비급여", "식약처", "보험등재", "임상", "진단", "영상",
        "환자", "의료정책", "건강보험",
    ],
    "primary_filter_en": [
        "SaMD", "medical AI", "healthcare AI", "digital health", "medical device",
        "surgical robot", "robotic surgery", "hospital AI", "FDA", "510(k)",
        "reimbursement", "orthopedic", "arthroplasty", "knee", "imaging AI",
        "clinical", "patient", "TKA", "radiology",
    ],
    "medical_gate_kr": [
        "의료", "병원", "헬스", "환자", "진료", "수술", "의료기기", "디지털헬스",
        "임상", "영상", "진단", "수가", "급여", "인허가", "식약처", "재활",
        "로봇수술", "의료정책", "건강보험", "의사", "간호", "치료", "검진",
        "MRI", "CT", "엑스레이", "X-ray",
    ],
    "medical_gate_en": [
        "medical", "hospital", "healthcare", "clinical", "surgery", "SaMD",
        "patient", "physician", "diagnosis",
    ],
    "boost_keywords_kr": [
        "정형외과", "근골격계", "무릎", "하지정렬", "인공관절", "수술계획",
        "퇴행성관절염", "KL", "HKAA", "외골격", "TKA", "사이배슬론",
        "의료영상", "판독", "병원 도입", "혁신의료기기", "바우처", "실증",
    ],
    "boost_keywords_en": [
        "orthopedic", "MSK", "knee", "arthroplasty", "alignment", "implant",
        "planning", "osteoarthritis", "radiology", "imaging",
    ],
    "company_keywords": [
        "크레스콤", "루닛", "ImageBiopsy Lab", "Radiobotics", "Gleamer", "AZmed",
        "PeekMed", "코넥티브", "이지메디봇", "잇피", "엑스큐브", "휴로틱스",
        "제이앤피메디", "디알젬", "레이", "코어라인", "뷰노", "딥노이드",
        "인티비전", "노을", "파로스", "다빈치",
    ],
    "opportunity_keywords_kr": [
        "바우처", "실증", "사업화", "허가", "인증", "수가", "보험등재",
        "혁신의료", "지원사업", "R&D", "투자", "M&A", "상용화",
    ],
    "opportunity_keywords_en": [
        "grant", "pilot", "clearance", "approval", "funding", "acquisition",
        "commercialization",
    ],
    "negative_keywords_soft": [
        "GTA", "게임", "자율주행", "배달로봇", "휴머노이드 IPO", "나스닥 상장",
        "반도체", "데이터센터", "자사주", "주가", "실적 급등", "태양광",
        "재생에너지", "ESS", "ERP", "클라우드", "데브옵스",
        "탈모", "백신", "신약", "제약", "희귀질환 치료제", "위암검진",
        "적십자", "정치", "인준 거부", "산모", "출산",
        "미용", "다이어트", "건강상식", "생활정보",
    ],
}
