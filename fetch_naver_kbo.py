#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
네이버 스포츠에서 KBO 경기 결과를 받아서, 기록판의 '일정 가져오기' 탭에 올릴 수 있는 CSV로 저장합니다.

필요한 것: Python 3.8 이상 (추가 설치 없음 - 기본 라이브러리만 사용)

사용 예
  # 시즌 전체(일정 + 지난 경기 결과) 한 번에
  python fetch_naver_kbo.py --from 2026-03-28 --to 2026-11-15

  # 매일 업데이트용 (옵션 없이 실행하면 최근 3일 ~ 오늘)
  python fetch_naver_kbo.py

  # ★ 창단 이후 모든 시즌을 연도별 파일로 보관 (GitHub Actions에서 쓰는 방식)
  #    최신 시즌부터 받고, 지난 시즌은 한 번만 받아 data/kbo_results_2013.csv 처럼 저장해요.
  python fetch_naver_kbo.py --archive-dir data --founding 2011-03-31 --max-minutes 150

실행하면 CSV가 만들어져요. 기록판 > '일정 가져오기' 탭에서 그 파일을 선택하세요.

※ 네이버의 비공식(공개 문서 없음) 주소를 사용해요. 예고 없이 바뀔 수 있어요.
   - 응답이 없는 연도(오래된 시즌 등)는 몇 초 안에 포기하고 건너뛰어요.
   - --max-minutes 를 주면 그 시간 안에 멈추고, 받은 시즌은 저장해요. 다음 실행에서 이어서 받아요.
   - 문제가 생기면 --probe 경기ID 로 원본 응답을 확인할 수 있어요.
"""
import argparse
import csv
import json
import os
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
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

START_TS = time.time()
TIMEOUT = 10          # 요청 하나를 기다리는 최대 시간(초)
DEADLINE = None       # --max-minutes 로 정해지는 전체 제한 시각


def log(msg):
    print("[%5ds] %s" % (time.time() - START_TS, msg), flush=True)


def time_up():
    return DEADLINE is not None and time.time() > DEADLINE


def fetch_json(url, timeout=None):
    req = Request(url, headers=HEADERS)
    with urlopen(req, timeout=timeout or TIMEOUT) as res:
        return json.loads(res.read().decode("utf-8"))


def with_retry(fn, *args, tries=2, wait=1.0):
    """일시적인 오류(응답 없음, 5xx, 429)만 다시 시도해요. 4xx 는 다시 해도 소용없어서 바로 포기."""
    last = None
    for i in range(tries):
        try:
            return fn(*args)
        except HTTPError as e:
            last = e
            if 400 <= e.code < 500 and e.code != 429:
                raise
            time.sleep(wait * (i + 1))
        except (URLError, OSError, ValueError, KeyError) as e:
            last = e
            time.sleep(wait * (i + 1))
    raise last


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


# ---------------------------------------------------------------- 수집

def classify(g, args, counters):
    """경기 하나가 어떤 종류인지 판단. 건너뛸 경기는 None, 아니면 (phase, 라운드, 기타구분)."""
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
    if args.results_only and not is_canceled(g) and (g.get("statusCode") or "").upper() != "RESULT":
        return None
    return phase, rnd, extra_tag


def needs_detail(g):
    status = (g.get("statusCode") or "").upper()
    return (status == "RESULT" or is_canceled(g)) and not (g.get("winPitcherName") and g.get("awayStarterName"))


def collect_days(days, args, label):
    """여러 날을 동시에 받아요. 처음부터 응답이 없으면 빨리 포기해요."""
    games, failed, skipped = {}, [], []
    lock = threading.Lock()
    st = {"ok": 0, "fail": 0, "streak": 0, "done": 0, "abort": None}
    total = len(days)

    def work(d):
        if st["abort"] or time_up():
            return d, None, "skip"
        try:
            res, err = with_retry(fetch_day, d), None
        except Exception as e:
            res, err = None, e
        time.sleep(args.sleep)
        with lock:
            st["done"] += 1
            if err is None:
                st["ok"] += 1
                st["streak"] = 0
            else:
                st["fail"] += 1
                st["streak"] += 1
                if st["ok"] == 0 and st["fail"] >= args.fail_fast:
                    st["abort"] = "응답 없음"
                elif st["streak"] >= args.fail_streak:
                    st["abort"] = "연속 실패"
                if st["fail"] <= 3:
                    log("  ! %s %s 실패: %s" % (label, d.isoformat(), err))
            if st["done"] % 50 == 0:
                log("  … %s %d/%d일 (성공 %d · 실패 %d)" % (label, st["done"], total, st["ok"], st["fail"]))
        return d, res, err

    with ThreadPoolExecutor(max_workers=max(1, args.workers)) as pool:
        for d, res, err in pool.map(work, days):
            if isinstance(err, str):
                skipped.append(d)
            elif err is not None:
                failed.append(d)
            else:
                for g in res:
                    games.setdefault(g.get("gameId"), g)
    return games, failed, skipped, st


def collect(start, end, args, enrich_ok, label=""):
    """start~end 기간의 경기를 받아 (rows, raw, counters, 못 받은 날짜, 중단 사유, 성공 요청 수)를 돌려줘요."""
    days = []
    d = start
    while d <= end:
        days.append(d)
        d += timedelta(days=1)
    games, failed, skipped, st = collect_days(days, args, label)
    # 일시적으로 실패한 날짜는 한 번 더 시도
    if failed and not st["abort"] and not time_up() and len(failed) <= 60:
        g2, f2, s2, st2 = collect_days(failed, args, label + "(재시도)")
        games.update({k: v for k, v in g2.items() if k not in games})
        failed, skipped = f2, skipped + s2
        st["ok"] += st2["ok"]

    counters = {"status": {}, "round": {}, "nocode": 0}
    picked = []
    for g in games.values():
        c = classify(g, args, counters)
        if c:
            picked.append((g, c))

    details = {}
    if enrich_ok:
        need = [g for g, _ in picked if needs_detail(g)]
        if need:
            def fetch_detail(g):
                if time_up():
                    return
                try:
                    details[g["gameId"]] = with_retry(fetch_game, g["gameId"], tries=1)
                except Exception:
                    pass
            with ThreadPoolExecutor(max_workers=max(1, args.workers)) as pool:
                list(pool.map(fetch_detail, need))

    rows, raw = [], []
    for g, (phase, rnd, extra_tag) in picked:
        det = details.get(g.get("gameId"))
        if det:
            g = dict(g)
            for k in ("awayStarterName", "homeStarterName", "winPitcherName", "losePitcherName", "stadium"):
                if not g.get(k) and det.get(k):
                    g[k] = det[k]
        row, status, canceled = build_row(g, not args.no_stadium)
        if not row["날짜"] or not row["어웨이"] or not row["홈"]:
            continue
        if extra_tag:
            row["기록제외"], row["구분"] = "O", extra_tag
        else:
            row["시즌구분"] = "포스트시즌" if phase == "post" else "정규시즌"
            row["라운드"] = rnd
        key = "취소" if canceled else status
        counters["status"][key] = counters["status"].get(key, 0) + 1
        rows.append(row)
        raw.append(g)
    incomplete = sorted(set(d.isoformat() for d in failed + skipped))
    return rows, raw, counters, incomplete, st["abort"], st["ok"]


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


def load_manifest(path):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def run_archive(args):
    founding = datetime.strptime(args.founding, "%Y-%m-%d").date()
    cur = date.today().year
    (m1, d1), (m2, d2) = parse_window(args.season_window)
    os.makedirs(args.archive_dir, exist_ok=True)
    manifest_path = os.path.join(args.archive_dir, "index.json")

    if args.refresh_years.strip().lower() == "all":
        args.refresh_all = True
    refresh = {int(x) for x in args.refresh_years.split(",") if x.strip().isdigit()}
    prior_unavailable = set() if args.refresh_all else set(load_manifest(manifest_path).get("unavailable", []))
    unavailable = set()

    log("시작: %d년 ~ %d년 (최신 시즌부터), 동시 요청 %d개, 요청 제한시간 %d초%s" % (
        founding.year, cur, args.workers, TIMEOUT,
        (", 전체 제한 %d분" % args.max_minutes) if args.max_minutes else ""))

    failed_years, stopped_early, consecutive_unavailable = [], False, 0
    for y in range(cur, founding.year - 1, -1):
        if time_up():
            log("시간 제한에 도달했어요. 남은 시즌은 다음 실행에서 이어서 받아요.")
            stopped_early = True
            break
        path = os.path.join(args.archive_dir, "kbo_results_%d.csv" % y)
        is_past = y < cur
        if is_past and os.path.exists(path) and not args.refresh_all and y not in refresh:
            log("%d년: 이미 보관돼 있어서 건너뛰어요. (%d경기)" % (y, count_rows(path)))
            consecutive_unavailable = 0
            continue
        if is_past and y in prior_unavailable and y not in refresh:
            log("%d년: 네이버가 데이터를 주지 않았던 해라 건너뛰어요. (다시 시도하려면 다시 받을 연도에 %d 를 적어주세요)" % (y, y))
            unavailable.add(y)
            continue

        start, end = date(y, m1, d1), date(y, m2, d2)
        log("%d년: %s ~ %s (%d일) 받는 중..." % (y, start.isoformat(), end.isoformat(), (end - start).days + 1))
        t0 = time.time()
        rows, _raw, counters, incomplete, aborted, ok_reqs = collect(
            start, end, args, enrich_ok=(not is_past) or args.enrich_history, label="%d년" % y)

        if aborted == "응답 없음" and ok_reqs == 0:
            if not is_past:
                log("  → 올해 데이터에 응답이 없어요. 네이버가 막고 있거나 일시적인 장애일 수 있어요. 중단합니다.")
                failed_years.append(y)
                break
            log("  → %d년은 응답이 없어서 건너뛰어요. (네이버가 이 시즌 데이터를 주지 않는 것 같아요)" % y)
            unavailable.add(y)
            consecutive_unavailable += 1
            if consecutive_unavailable >= args.stop_after_unavailable:
                log("  → %d해 연속으로 응답이 없어서, 더 오래된 시즌도 제공되지 않는 것으로 보고 거슬러 올라가기를 멈춰요." % consecutive_unavailable)
                break
            continue
        consecutive_unavailable = 0

        if incomplete:
            kind = "시간 제한 때문에" if time_up() else "오류로"
            log("  → %s 못 받은 날짜가 %d일 있어서 %d년 파일은 갱신하지 않았어요. (%s%s)" % (
                kind, len(incomplete), y, ", ".join(incomplete[:5]), " ..." if len(incomplete) > 5 else ""))
            if time_up():
                stopped_early = True
            else:
                failed_years.append(y)
            continue
        if counters["nocode"]:
            log("  ⚠ 라운드 코드가 없는 경기 %d개는 정규/포스트를 구분할 수 없어 제외했어요." % counters["nocode"])
        if not rows:
            if is_past:
                write_csv(path, [])      # 경기가 없는 해는 빈 파일로 표시해서 다음부터 다시 받지 않아요
                log("  → %d년은 받은 경기가 없어요. (빈 파일로 표시)" % y)
            else:
                log("  → 올해는 아직 받은 경기가 없어요. 기존 파일은 그대로 둬요.")
            continue
        write_csv(path, rows)
        log("  → 저장: %s (%d경기, %d초) 상태별 %s" % (path, len(rows), time.time() - t0, counters["status"]))

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
        "unavailable": sorted(unavailable),
        "updated": datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ"),
    }
    with open(manifest_path, "w", encoding="utf-8") as f:
        json.dump(manifest, f, ensure_ascii=False, indent=2)
    log("보관된 시즌: %s" % (", ".join(str(y) for y in years) or "없음"))
    if unavailable:
        log("네이버가 주지 않는 해: %s" % ", ".join(str(y) for y in sorted(unavailable)))
    if stopped_early:
        log("※ 아직 다 받지 못했어요. 다시 실행하면 이어서 받아요.")
    if failed_years:
        log("다시 실행이 필요한 해: %s" % ", ".join(str(y) for y in failed_years))
        if args.strict:
            sys.exit(1)


def main():
    global TIMEOUT, DEADLINE
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
    ap.add_argument("--sleep", type=float, default=0.05, help="요청 사이 대기(초)")
    ap.add_argument("--strict", action="store_true", help="하루라도 받지 못하면 파일을 만들지 않고 실패로 종료 (자동 실행용)")
    ap.add_argument("--probe", default="", help="경기ID 하나의 원본 응답 확인")
    ap.add_argument("--workers", type=int, default=6, help="동시에 보낼 요청 수 (기본 6)")
    ap.add_argument("--timeout", type=int, default=10, help="요청 하나를 기다리는 최대 시간(초, 기본 10)")
    ap.add_argument("--fail-fast", type=int, default=6, help="한 번도 성공하지 못한 채 이만큼 실패하면 그 해를 포기 (기본 6)")
    ap.add_argument("--fail-streak", type=int, default=40, help="연속으로 이만큼 실패하면 그 해를 포기 (기본 40)")
    ap.add_argument("--max-minutes", type=float, default=0, help="전체 제한 시간(분). 넘으면 멈추고 받은 시즌은 저장 (0=제한 없음)")
    # 시즌별 보관 모드
    ap.add_argument("--archive-dir", default="", help="지정하면 창단 연도부터 올해까지 연도별 파일(kbo_results_YYYY.csv)로 보관")
    ap.add_argument("--founding", default="", help="창단일 YYYY-MM-DD (보관 모드에서 이 날짜가 속한 해부터 받아요)")
    ap.add_argument("--season-window", default="03-01:11-30", help="매년 받을 기간 월-일:월-일 (기본 03-01:11-30)")
    ap.add_argument("--refresh-all", action="store_true", help="이미 보관된 지난 시즌도 모두 다시 받기")
    ap.add_argument("--refresh-years", default="", help="다시 받을 연도 목록 (예: 2024,2025 / 전부는 all)")
    ap.add_argument("--stop-after-unavailable", type=int, default=2, help="응답이 없는 해가 이만큼 연속이면 더 오래된 해는 받지 않음 (기본 2)")
    ap.add_argument("--chunk-days", type=int, default=0, help=argparse.SUPPRESS)      # 예전 옵션 (사용 안 함)
    ap.add_argument("--enrich-history", action="store_true", help="지난 시즌도 경기마다 선발·투수 정보를 추가 조회 (오래 걸려요)")
    args = ap.parse_args()

    TIMEOUT = max(3, args.timeout)
    if args.max_minutes and args.max_minutes > 0:
        DEADLINE = time.time() + args.max_minutes * 60

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

    rows, raw, counters, incomplete, aborted, _ok = collect(start, end, args, enrich_ok=True)
    if not rows:
        print("받은 경기가 없어요. 날짜 범위를 확인해보세요. (기존 파일은 그대로 둬요)")
        sys.exit(1)
    if args.strict and incomplete:
        print("받지 못한 날짜가 있어서 파일을 갱신하지 않았어요:", ", ".join(incomplete))
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
    if incomplete:
        print("받지 못한 날짜(다시 실행해 보세요):", ", ".join(incomplete))


if __name__ == "__main__":
    main()
