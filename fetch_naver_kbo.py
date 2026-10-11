import argparse
import csv
import json
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

COLUMNS = [
    "날짜", "어웨이", "홈", "어웨이점수", "홈점수", "결과", "구장",
    "어웨이선발", "홈선발", "승리투수", "패전투수", "홀드", "세이브",
    "어웨이MVP", "홈MVP", "취소", "취소사유",
]


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


def main():
    today = date.today()
    ap = argparse.ArgumentParser(description="네이버 스포츠 KBO 결과 -> CSV")
    ap.add_argument("--from", dest="start", default=(today - timedelta(days=3)).isoformat(), help="시작일 YYYY-MM-DD (기본: 3일 전)")
    ap.add_argument("--to", dest="end", default=today.isoformat(), help="종료일 YYYY-MM-DD (기본: 오늘)")
    ap.add_argument("--out", default="kbo_results.csv", help="저장할 CSV 파일 (기본: kbo_results.csv)")
    ap.add_argument("--json", dest="json_out", default="", help="원하면 JSON도 같이 저장")
    ap.add_argument("--results-only", action="store_true", help="예정 경기는 제외하고 결과/취소 경기만 저장")
    ap.add_argument("--round-code", default="kbo_r", help="kbo_r=정규시즌(기본), all=전부(시범/포스트시즌 포함)")
    ap.add_argument("--with-stadium", action="store_true", help="네이버의 구장 이름도 저장 (기본: 비워서 기록판의 기본 구장명 사용)")
    ap.add_argument("--sleep", type=float, default=0.2, help="요청 사이 대기(초)")
    ap.add_argument("--strict", action="store_true", help="하루라도 받지 못하면 파일을 만들지 않고 실패로 종료 (자동 실행용)")
    ap.add_argument("--probe", default="", help="경기ID 하나의 원본 응답 확인")
    args = ap.parse_args()

    if args.probe:
        probe(args.probe)
        return

    start = datetime.strptime(args.start, "%Y-%m-%d").date()
    end = datetime.strptime(args.end, "%Y-%m-%d").date()
    if start > end:
        sys.exit("--from 이 --to 보다 늦어요.")

    rows, raw = [], []
    status_count, round_count, failed = {}, {}, []
    d = start
    while d <= end:
        try:
            games = with_retry(fetch_day, d)
        except Exception as e:
            failed.append(d.isoformat())
            print("  ! %s 실패: %s" % (d.isoformat(), e))
            d += timedelta(days=1)
            continue

        for g in games:
            if g.get("categoryId") not in (None, "kbo"):
                continue
            rc = g.get("roundCode") or ""
            round_count[rc or "(없음)"] = round_count.get(rc or "(없음)", 0) + 1
            if args.round_code != "all" and rc and rc != args.round_code:
                continue
            canceled = is_canceled(g)
            status = (g.get("statusCode") or "").upper()
            if args.results_only and not canceled and status != "RESULT":
                continue
            if (status == "RESULT" or canceled) and not (g.get("winPitcherName") and g.get("awayStarterName")):
                g = enrich(g)
            row, status, canceled = build_row(g, args.with_stadium)
            if not row["날짜"] or not row["어웨이"] or not row["홈"]:
                continue
            key = "취소" if canceled else status
            status_count[key] = status_count.get(key, 0) + 1
            rows.append(row)
            raw.append(g)
        d += timedelta(days=1)
        time.sleep(args.sleep)

    if not rows:
        print("받은 경기가 없어요. 날짜 범위를 확인하거나 --round-code all 로 다시 해보세요. (기존 파일은 그대로 둬요)")
        sys.exit(1)
    if args.strict and failed:
        print("받지 못한 날짜가 있어서 파일을 갱신하지 않았어요:", ", ".join(failed))
        sys.exit(1)
    rows.sort(key=lambda r: (r["날짜"], r["홈"]))
    with open(args.out, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=COLUMNS)
        w.writeheader()
        w.writerows(rows)
    if args.json_out:
        with open(args.json_out, "w", encoding="utf-8") as f:
            json.dump(raw, f, ensure_ascii=False, indent=2)

    print("저장 완료: %s (%d경기)" % (args.out, len(rows)))
    print("상태별:", status_count or "없음")
    print("라운드 코드별(필터 전):", round_count or "없음")
    if failed:
        print("받지 못한 날짜(다시 실행해 보세요):", ", ".join(failed))


if __name__ == "__main__":
    main()
