#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
네이버 스포츠에서 KBO 경기 결과를 받아서, 기록판의 '일정 가져오기' 탭에 올릴 수 있는 CSV로 저장합니다.

필요한 것: Python 3.8 이상 (추가 설치 없음 - 기본 라이브러리만 사용)

사용 예
  # 시즌 전체(일정 + 지난 경기 결과) 한 번에
  python fetch_naver_kbo.py --from 2026-03-28 --to 2026-10-12

  # 매일 업데이트용 (옵션 없이 실행하면 최근 3일 ~ 오늘)
  python fetch_naver_kbo.py

  # 결과가 나온 경기/취소 경기만 (예정 경기는 제외)
  python fetch_naver_kbo.py --results-only

  # 정규시즌 + 포스트시즌을 한 파일에 담아요 (CSV의 '시즌구분', '라운드' 칸으로 구분)
  python fetch_naver_kbo.py --from 2026-03-28 --to 2026-11-15

  # 포스트시즌은 빼고 정규시즌만 / 시범경기·올스타전도 함께(기록 반영 X로 표시)
  python fetch_naver_kbo.py --no-postseason
  python fetch_naver_kbo.py --include-exhibition

  # ★ 창단 이후 모든 시즌을 연도별 파일로 보관 (GitHub Actions에서 쓰는 방식)
  #    지난 시즌은 한 번만 받아서 data/kbo_results_2013.csv 처럼 저장해두고, 올해만 매번 갱신해요.
  python fetch_naver_kbo.py --archive-dir data --founding 2011-03-31

실행하면 kbo_results.csv 가 만들어져요. 기록판 > '일정 가져오기' 탭에서 그 파일을 선택하세요.

※ 네이버의 비공식(공개 문서 없음) 주소를 사용해요. 예고 없이 바뀔 수 있어요.
   문제가 생기면 --probe 경기ID 로 원본 응답을 확인할 수 있어요.
"""
import argparse
import csv
import json
import os
import sys
import time
from datetime import date, datetime, timedelta
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

BASE = "https://api-gw.sports.naver.com/schedule/games"
HEADERS = {
    "User-Agent": "Mozilla/5.0",
    "Referer": "https://m.sports.naver.com/",
}

# 기록판 '일정 가져오기' 탭이 인식하는 제목 (순서 그대로)
COLUMNS = [
    "날짜", "어웨이", "홈", "어웨이점수", "홈점수", "결과", "구장",
    "어웨이선발", "홈선발", "승리투수", "패전투수", "홀드", "세이브",
    "어웨이MVP", "홈MVP", "취소", "취소사유",
    "기록제외", "구분", "시즌구분", "라운드",
]

# 네이버의 라운드 코드 (오픈소스 샘플 데이터에서 확인한 값)
ROUND_MAP = {
    "kbo_r": ("regular", ""),
    "kbo_ps_wd": ("post", "와일드카드 결정전"),
    "kbo_ps_sp": ("post", "준플레이오프"),
    "kbo_ps_po": ("post", "플레이오프"),
    "kbo_ps_ks": ("post", "한국시리즈"),
}
EXTRA_MAP = {"kbo_e": "시범경기", "kbo_as": "올스타전"}


def fetch_json(url, timeout=20):
    req = Request(url, headers=HEADERS)
    with urlopen(req, timeout=timeout) as res:
        return json.loads(res.read().decode("utf-8"))


def fetch_day(d):
    qs = urlencode({
        "fields": "basic,schedule,baseball,manualRelayUrl",
        "upperCategoryId": "kbaseball",
        "fromDate": d.isoformat(),
        "toDate": d.isoformat(),
        "size": 500,
    })
    payload = fetch_json("%s?%s" % (BASE, qs))
    return payload["result"]["games"]


def fetch_game(game_id):
    payload = fetch_json("%s/%s" % (BASE, game_id))
    return payload["result"]["game"]


def with_retry(fn, *args, tries=3, wait=1.0):
    last = None
    for i in range(tries):
        try:
            return fn(*args)
        except (HTTPError, URLError, TimeoutError, KeyError, ValueError) as e:
            last = e
            time.sleep(wait * (i + 1))
    raise last


def is_canceled(g):
    code = (g.get("statusCode") or "").upper()
    info = (g.get("statusInfo") or "").strip()
    return "CANCEL" in code or "취소" in info


def clean(v):
    return "" if v is None else str(v).strip()


def build_row(g, with_stadium):
    status = (g.get("statusCode") or "").upper()
    canceled = is_canceled(g)
    row = {c: "" for c in COLUMNS}
    row["날짜"] = clean(g.get("gameDate"))
    row["어웨이"] = clean(g.get("awayTeamName"))
    row["홈"] = clean(g.get("homeTeamName"))
    row["어웨이선발"] = clean(g.get("awayStarterName"))
    row["홈선발"] = clean(g.get("homeStarterName"))
    if with_stadium:
        row["구장"] = clean(g.get("stadium"))

    if canceled:
        row["취소"] = "O"
        row["취소사유"] = clean(g.get("statusInfo")) or "취소"
    elif status == "RESULT":
        a, h = g.get("awayTeamScore"), g.get("homeTeamScore")
        if a is not None and h is not None:
            row["어웨이점수"], row["홈점수"] = int(a), int(h)
        row["승리투수"] = clean(g.get("winPitcherName"))
        row["패전투수"] = clean(g.get("losePitcherName"))
    return row, status, canceled


def enrich(g):
    """목록 응답에 선발/승패 투수가 비어 있으면 경기 단건 응답으로 보강"""
    try:
        detail = with_retry(fetch_game, g["gameId"])
    except Exception:
        return g
    merged = dict(g)
    for k in ("awayStarterName", "homeStarterName", "winPitcherName", "losePitcherName", "stadium"):
        if not merged.get(k) and detail.get(k):
            merged[k] = detail[k]
    return merged


def probe(game_id):
    print("== 경기 단건 응답 ==")
    try:
        data = fetch_json("%s/%s" % (BASE, game_id))
        game = data.get("result", {}).get("game", {})
        print("항목:", ", ".join(sorted(game.keys())))
        path = "probe_%s.json" % game_id
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        print("원본 저장:", path)
    except Exception as e:
        print("실패:", e)
    print("== 기록(박스스코어) 시도 ==")
    try:
        data = fetch_json("%s/%s/record" % (BASE, game_id))
        path = "probe_%s_record.json" % game_id
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        print("원본 저장:", path)
    except Exception as e:
        print("실패(없는 주소일 수 있어요):", e)


def fetch_range(a, b):
    qs = urlencode({
        "fields": "basic,schedule,baseball,manualRelayUrl",
        "upperCategoryId": "kbaseball",
        "fromDate": a.isoformat(),
        "toDate": b.isoformat(),
        "size": 500,
    })
    payload = fetch_json("%s?%s" % (BASE, qs))
    return payload["result"]["games"]


def detect_chunk_days(sleep):
    """여러 날을 한 번에 조회해도 하루씩 조회한 것과 같은 결과가 나오는지 확인해서 묶음 크기를 정해요."""
    cur = date.today().year
    for y in (cur - 1, cur - 2, cur - 3):
        a, b = date(y, 4, 7), date(y, 4, 13)
        try:
            ranged = {g.get("gameId") for g in with_retry(fetch_range, a, b)}
            perday = set()
            d = a
            while d <= b:
                perday |= {g.get("gameId") for g in with_retry(fetch_day, d)}
                d += timedelta(days=1)
                time.sleep(sleep)
        except Exception:
            continue
        if perday:
            if ranged == perday:
                print("범위 조회 확인 완료: 7일씩 묶어서 받아요. (%d년 샘플 %d경기 일치)" % (y, len(perday)))
                return 7
            print("범위 조회 결과가 하루씩 받은 결과와 달라서, 안전하게 하루씩 받아요.")
            return 1
    print("범위 조회를 확인할 샘플이 없어서, 안전하게 하루씩 받아요.")
    return 1


def convert_game(g, args, enrich_ok, counters):
    """네이버 경기 하나를 CSV 한 줄로 바꿔요. 건너뛸 경기는 None."""
    if g.get("categoryId") not in (None, "kbo"):
        return None
    rc = g.get("roundCode") or ""
    counters["round"][rc or "(없음)"] = counters["round"].get(rc or "(없음)", 0) + 1
    phase, rnd, extra_tag = "regular", "", ""
    if rc in ROUND_MAP:
        phase, rnd = ROUND_MAP[rc]
    elif rc in EXTRA_MAP:
        if not args.include_exhibition:
            return None
        extra_tag = EXTRA_MAP[rc]
    elif rc.startswith("kbo_ps"):
        phase = "post"              # 새로운 포스트시즌 코드: 라운드 이름만 비워둠
    elif rc:
        return None                 # 알 수 없는 코드는 건너뜀
    else:
        counters["nocode"] += 1     # 라운드 코드가 없으면 정규/포스트를 구분할 수 없어 건너뜀
        return None
    if phase == "post" and args.no_postseason:
        return None
    canceled = is_canceled(g)
    status = (g.get("statusCode") or "").upper()
    if args.results_only and not canceled and status != "RESULT":
        return None
    if enrich_ok and (status == "RESULT" or canceled) and not (g.get("winPitcherName") and g.get("awayStarterName")):
        g = enrich(g)
    row, status, canceled = build_row(g, not args.no_stadium)
    if not row["날짜"] or not row["어웨이"] or not row["홈"]:
        return None
    if extra_tag:
        row["기록제외"], row["구분"] = "O", extra_tag
    else:
        row["시즌구분"] = "포스트시즌" if phase == "post" else "정규시즌"
        row["라운드"] = rnd
    key = "취소" if canceled else status
    counters["status"][key] = counters["status"].get(key, 0) + 1
    return row, g


def collect(start, end, args, chunk_days, enrich_ok):
    """start~end 기간의 경기를 받아 (rows, raw, counters, failed_days)로 돌려줘요."""
    rows, raw, failed = [], [], []
    counters = {"status": {}, "round": {}, "nocode": 0}
    seen = set()

    def take(games):
        for g in games:
            gid = g.get("gameId")
            if gid in seen:
                continue
            seen.add(gid)
            res = convert_game(g, args, enrich_ok, counters)
            if res:
                rows.append(res[0])
                raw.append(res[1])

    d = start
    while d <= end:
        span_end = min(end, d + timedelta(days=chunk_days - 1))
        done = False
        if chunk_days > 1:
            try:
                games = with_retry(fetch_range, d, span_end)
                take([g for g in games if d.isoformat() <= (g.get("gameDate") or "") <= span_end.isoformat()])
                done = True
            except Exception as e:
                print("  ! %s~%s 묶음 조회 실패, 하루씩 다시 시도: %s" % (d.isoformat(), span_end.isoformat(), e))
        if not done:
            dd = d
            while dd <= span_end:
                try:
                    take(with_retry(fetch_day, dd))
                except Exception as e:
                    failed.append(dd.isoformat())
                    print("  ! %s 실패: %s" % (dd.isoformat(), e))
                dd += timedelta(days=1)
                time.sleep(args.sleep)
        d = span_end + timedelta(days=1)
        time.sleep(args.sleep)
    return rows, raw, counters, failed


def write_csv(path, rows):
    rows = sorted(rows, key=lambda r: (r["날짜"], r["홈"]))
    with open(path, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=COLUMNS)
        w.writeheader()
        w.writerows(rows)


def count_rows(path):
    try:
        with open(path, newline="", encoding="utf-8-sig") as f:
            return max(0, sum(1 for _ in csv.reader(f)) - 1)
    except OSError:
        return 0


def parse_window(text):
    try:
        a, b = text.split(":")
        m1, d1 = [int(x) for x in a.split("-")]
        m2, d2 = [int(x) for x in b.split("-")]
        return (m1, d1), (m2, d2)
    except Exception:
        sys.exit("--season-window 형식이 잘못됐어요. 예: 03-01:11-30")


def run_archive(args):
    founding = datetime.strptime(args.founding, "%Y-%m-%d").date()
    cur = date.today().year
    (m1, d1), (m2, d2) = parse_window(args.season_window)
    os.makedirs(args.archive_dir, exist_ok=True)
    if args.refresh_years.strip().lower() == "all":
        args.refresh_all = True
    refresh = {int(x) for x in args.refresh_years.split(",") if x.strip().isdigit()}

    chunk = args.chunk_days
    if chunk <= 0:
        chunk = detect_chunk_days(args.sleep)

    failed_years = []
    for y in range(founding.year, cur + 1):
        path = os.path.join(args.archive_dir, "kbo_results_%d.csv" % y)
        is_past = y < cur
        if is_past and os.path.exists(path) and not args.refresh_all and y not in refresh:
            print("%d년: 이미 보관돼 있어서 건너뛰어요. (%d경기)" % (y, count_rows(path)))
            continue
        start, end = date(y, m1, d1), date(y, m2, d2)
        print("%d년: %s ~ %s 받는 중..." % (y, start.isoformat(), end.isoformat()))
        rows, _raw, counters, failed = collect(start, end, args, chunk, enrich_ok=(not is_past) or args.enrich_history)
        if failed:
            print("  → 받지 못한 날짜가 있어서 %d년 파일은 갱신하지 않았어요: %s" % (y, ", ".join(failed[:8]) + (" ..." if len(failed) > 8 else "")))
            failed_years.append(y)
            continue
        if counters["nocode"]:
            print("  ⚠ 라운드 코드가 없는 경기 %d개는 정규/포스트를 구분할 수 없어 제외했어요." % counters["nocode"])
        if not rows:
            if is_past:
                write_csv(path, [])      # 경기가 없는 해는 빈 파일로 표시해서 다음부터 다시 받지 않아요
                print("  → %d년은 받은 경기가 없어요. (빈 파일로 표시)" % y)
            else:
                print("  → 올해는 아직 받은 경기가 없어요. 기존 파일은 그대로 둬요.")
            continue
        write_csv(path, rows)
        print("  → 저장: %s (%d경기) 상태별 %s" % (path, len(rows), counters["status"]))

    # 올해 파일을 '최신' 파일로도 복사 (기록판의 '자동 수집된 최신 CSV 불러오기'가 읽는 파일)
    cur_path = os.path.join(args.archive_dir, "kbo_results_%d.csv" % cur)
    if os.path.exists(cur_path) and count_rows(cur_path) > 0:
        with open(cur_path, "rb") as src, open(os.path.join(args.archive_dir, "kbo_results.csv"), "wb") as dst:
            dst.write(src.read())

    years = [y for y in range(founding.year, cur + 1)
             if count_rows(os.path.join(args.archive_dir, "kbo_results_%d.csv" % y)) > 0]
    manifest = {
        "founding": founding.isoformat(),
        "currentYear": cur,
        "years": years,
        "updated": datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ"),
    }
    with open(os.path.join(args.archive_dir, "index.json"), "w", encoding="utf-8") as f:
        json.dump(manifest, f, ensure_ascii=False, indent=2)
    print("보관된 시즌: %s" % (", ".join(str(y) for y in years) or "없음"))
    if failed_years:
        print("다시 실행이 필요한 해:", ", ".join(str(y) for y in failed_years))
        if args.strict:
            sys.exit(1)


def main():
    today = date.today()
    ap = argparse.ArgumentParser(description="네이버 스포츠 KBO 결과 -> CSV")
    ap.add_argument("--from", dest="start", default=(today - timedelta(days=3)).isoformat(), help="시작일 YYYY-MM-DD (기본: 3일 전)")
    ap.add_argument("--to", dest="end", default=today.isoformat(), help="종료일 YYYY-MM-DD (기본: 오늘)")
    ap.add_argument("--out", default="kbo_results.csv", help="저장할 CSV 파일 (기본: kbo_results.csv)")
    ap.add_argument("--json", dest="json_out", default="", help="원하면 JSON도 같이 저장")
    ap.add_argument("--results-only", action="store_true", help="예정 경기는 제외하고 결과/취소 경기만 저장")
    ap.add_argument("--no-postseason", action="store_true", help="포스트시즌 경기는 제외 (기본: 정규시즌 + 포스트시즌)")
    ap.add_argument("--include-exhibition", action="store_true", help="시범경기·올스타전도 포함 (기록 반영 X 경기로 표시)")
    ap.add_argument("--no-stadium", action="store_true", help="구장 이름을 저장하지 않음 (기본: 네이버의 구장 지역명을 저장하고, 기록판이 연도별 정식 구장명으로 바꿔요)")
    ap.add_argument("--with-stadium", action="store_true", help=argparse.SUPPRESS)   # 예전 옵션 (이제 기본값)
    ap.add_argument("--sleep", type=float, default=0.2, help="요청 사이 대기(초)")
    ap.add_argument("--strict", action="store_true", help="하루라도 받지 못하면 파일을 만들지 않고 실패로 종료 (자동 실행용)")
    ap.add_argument("--probe", default="", help="경기ID 하나의 원본 응답 확인")
    # 시즌별 보관 모드
    ap.add_argument("--archive-dir", default="", help="지정하면 창단 연도부터 올해까지 연도별 파일(kbo_results_YYYY.csv)로 보관")
    ap.add_argument("--founding", default="", help="창단일 YYYY-MM-DD (보관 모드에서 이 날짜가 속한 해부터 받아요)")
    ap.add_argument("--season-window", default="03-01:11-30", help="매년 받을 기간 월-일:월-일 (기본 03-01:11-30)")
    ap.add_argument("--refresh-all", action="store_true", help="이미 보관된 지난 시즌도 모두 다시 받기")
    ap.add_argument("--refresh-years", default="", help="다시 받을 연도 목록 (예: 2024,2025)")
    ap.add_argument("--chunk-days", type=int, default=0, help="한 번에 조회할 일수 (0=자동 확인, 1=하루씩)")
    ap.add_argument("--enrich-history", action="store_true", help="지난 시즌도 경기마다 선발·투수 정보를 추가 조회 (오래 걸려요)")
    args = ap.parse_args()

    if args.probe:
        probe(args.probe)
        return
    if args.archive_dir:
        if not args.founding:
            sys.exit("--archive-dir 를 쓰려면 --founding 창단일이 필요해요.")
        run_archive(args)
        return

    start = datetime.strptime(args.start, "%Y-%m-%d").date()
    end = datetime.strptime(args.end, "%Y-%m-%d").date()
    if start > end:
        sys.exit("--from 이 --to 보다 늦어요.")

    chunk = args.chunk_days if args.chunk_days > 0 else 1
    rows, raw, counters, failed = collect(start, end, args, chunk, enrich_ok=True)
    if not rows:
        print("받은 경기가 없어요. 날짜 범위를 확인해보세요. (기존 파일은 그대로 둬요)")
        sys.exit(1)
    if args.strict and failed:
        print("받지 못한 날짜가 있어서 파일을 갱신하지 않았어요:", ", ".join(failed))
        sys.exit(1)
    write_csv(args.out, rows)
    if args.json_out:
        with open(args.json_out, "w", encoding="utf-8") as f:
            json.dump(raw, f, ensure_ascii=False, indent=2)

    print("저장 완료: %s (%d경기)" % (args.out, len(rows)))
    print("상태별:", counters["status"] or "없음")
    print("라운드 코드별(필터 전):", counters["round"] or "없음")
    if counters["nocode"]:
        print("라운드 코드가 없어 제외한 경기: %d개" % counters["nocode"])
    if failed:
        print("받지 못한 날짜(다시 실행해 보세요):", ", ".join(failed))


if __name__ == "__main__":
    main()
