import argparse
import csv
import json
import os
import random
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, date, timedelta, timezone
import requests
from requests.adapters import HTTPAdapter
from urllib3.util import Retry

# 네이버 스포츠 API URL
BASE_URL = "https://api-gw.sports.naver.com/schedule/games"

# 네이버 차단 방지를 위한 브라우저 헤더 설정
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/130.0.0.0 Safari/537.36"
    ),
    "Referer": "https://m.sports.naver.com/kbo/schedule/index",
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "ko-KR,ko;q=0.9,en-US;q=0.8,en;q=0.7",
}

def create_session():
    """재시도 로직과 헤더가 설정된 HTTP Session 생성"""
    session = requests.Session()
    session.headers.update(HEADERS)
    
    # HTTP 429, 500, 502, 503, 504 발생 시 자동 재시도 설정 (2초, 4초, 8초 지연)
    retries = Retry(
        total=3,
        backoff_factor=2,
        status_forcelist=[429, 500, 502, 503, 504],
        raise_on_status=False
    )
    adapter = HTTPAdapter(max_retries=retries)
    session.mount("https://", adapter)
    session.mount("http://", adapter)
    return session

def fetch_games_for_date(session, date_str):
    """특정 날짜의 KBO 일정 및 경기 결과 가져오기"""
    # 네이버 봇 감지 방지용 랜덤 무작위 대기 (1.0 ~ 2.0초)
    time.sleep(random.uniform(1.0, 2.0))
    
    params = {
        "fromDate": date_str,
        "toDate": date_str,
        "category": "kbo"
