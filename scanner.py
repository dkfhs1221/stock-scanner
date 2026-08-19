#!/usr/bin/env python3
"""
미국 주식 스캐너 - GitHub Actions 버전
일반 실행 (오전 7시 KST) — 7개 메시지:
  1. 200일선 돌파 (인덱스별)
  2. 시장 브레드스 (50MA / 200MA 위 비율)
  3. 거래량 급증 (2배↑, 50MA위, +3%↑)
  4. 52주 신고가 돌파
  5. 거래량급증 + 신고가 교집합
  6. VIX Term Structure + PCR + Fear&Greed
  7. 50일 이격도 (S&P500선물 / 나스닥선물)
KOSPI_ONLY=true (오후 4시 10분 KST) — 1개 메시지:
  8. 50일 이격도 (코스피 당일 종가 기준)
WEEKLY_ONLY=true (월요일 오전 7시 KST) — 1개 메시지:
  9. 주간 유동성 보고 (TGA / RRP)
"""

import os, json, time, re, urllib.parse
import requests
from bs4 import BeautifulSoup
from datetime import date, timedelta

# ─── 설정 ────────────────────────────────────────────────────────────────────
TG_TOKEN    = os.environ["TELEGRAM_TOKEN"]
TG_CHAT_ID  = os.environ["TELEGRAM_CHAT_ID"]
KOSPI_ONLY   = os.environ.get("KOSPI_ONLY",  "").lower() in ("1", "true", "yes")
WEEKLY_ONLY  = os.environ.get("WEEKLY_ONLY", "").lower() in ("1", "true", "yes")
SNAPSHOT    = "data/snapshot.json"
TODAY       = date.today().isoformat()

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                  "AppleWebKit/537.36 (KHTML, like Gecko) "
                  "Chrome/120.0.0.0 Safari/537.36",
    "Accept-Language": "en-US,en;q=0.9",
}

IDX_ORDER  = ["dji", "ndx", "sp500"]
IDX_LABELS = {
    "dji":   "🔵 다우30",
    "ndx":   "🟣 나스닥100",
    "sp500": "🟢 S&P500",
}
IDX_SHORT  = {"dji": "DJI", "ndx": "NDX", "sp500": "SPX"}


# ─── 유틸 ────────────────────────────────────────────────────────────────────
def send_tg(text: str) -> bool:
    url  = f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage"
    resp = requests.post(url, json={"chat_id": TG_CHAT_ID,
                                    "text": text, "parse_mode": "HTML"}, timeout=30)
    ok = resp.json().get("ok", False)
    if not ok:
        print(f"  [TG 오류] {resp.text[:200]}")
    return ok


def fetch(url: str, retries: int = 3) -> str | None:
    for i in range(retries):
        try:
            r = requests.get(url, headers=HEADERS, timeout=30)
            if r.status_code == 200:
                return r.text
            print(f"  HTTP {r.status_code}: {url[:80]}")
        except Exception as e:
            print(f"  요청 오류({i+1}/{retries}): {e}")
        time.sleep(2 ** i)
    return None


def load_snapshot() -> dict:
    if os.path.exists(SNAPSHOT):
        with open(SNAPSHOT, encoding="utf-8") as f:
            return json.load(f)
    return {}


def save_snapshot(data: dict):
    os.makedirs("data", exist_ok=True)
    with open(SNAPSHOT, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def vvix_label(v: float) -> str:
    if v < 80:  return "매우 안정"
    if v < 100: return "정상"
    if v < 120: return "경계구간 ⚡"
    if v < 140: return "위험 🔴"
    if v < 160: return "강한공포"
    return "금융스트레스 🆘"


# ─── Finviz 스크래핑 ──────────────────────────────────────────────────────────
def get_count(filter_str: str) -> int:
    html = fetch(f"https://finviz.com/screener.ashx?v=111&f={filter_str}&r=1")
    if not html:
        return 0
    m = re.search(r"#1[^/]*/\s*([\d,]+)", html)
    if not m:
        m = re.search(r"Total:?\s*([\d,]+)", html, re.I)
    if m:
        return int(m.group(1).replace(",", ""))
    if "No results" in html:
        return 0
    soup = BeautifulSoup(html, "html.parser")
    return len(soup.find_all("tr", valign="top"))


def scrape_technical(idx_code: str) -> dict:
    results = {}
    r = 1
    while True:
        html = fetch(f"https://finviz.com/screener.ashx?v=171&f=idx_{idx_code}&r={r}")
        if not html:
            break
        soup = BeautifulSoup(html, "html.parser")
        rows = soup.find_all("tr", valign="top")
        if not rows:
            break
        for row in rows:
            tds = row.find_all("td")
            if len(tds) < 11:
                continue
            anchors = tds[1].find_all("a")
            ticker  = anchors[-1].text.strip() if anchors else tds[1].text.strip()
            try:
                sma200 = float(tds[6].text.strip().replace("%", ""))
                price  = tds[10].text.strip()
                results[ticker] = {"sma200": sma200, "price": price}
            except Exception:
                pass
        if len(rows) < 20:
            break
        r += 20
        time.sleep(0.6)
    return results


def _detect_col(html: str) -> tuple[int, int]:
    soup = BeautifulSoup(html, "html.parser")
    rows = soup.find_all("tr", valign="top")
    if not rows:
        return 8, 9
    for row in rows:
        tds = row.find_all("td")
        for i, td in enumerate(tds):
            t = td.text.strip()
            if re.match(r"^[+-]\d+\.\d{2}%$", t) and 6 < i < 14:
                return i - 1, i
    return 8, 9


def scrape_overview(idx_code: str, extra: str = "") -> list[dict]:
    results   = []
    price_idx = 8
    chg_idx   = 9
    r = 1
    f = f"idx_{idx_code},{extra}" if extra else f"idx_{idx_code}"
    while True:
        html = fetch(f"https://finviz.com/screener.ashx?v=111&f={f}&r={r}")
        if not html:
            break
        if r == 1:
            price_idx, chg_idx = _detect_col(html)
        soup = BeautifulSoup(html, "html.parser")
        rows = soup.find_all("tr", valign="top")
        if not rows:
            break
        for row in rows:
            tds = row.find_all("td")
            if len(tds) <= chg_idx:
                continue
            anchors = tds[1].find_all("a")
            ticker  = anchors[-1].text.strip() if anchors else tds[1].text.strip()
            try:
                price  = float(tds[price_idx].text.strip())
                change = float(tds[chg_idx].text.strip().replace("%", ""))
                results.append({"ticker": ticker, "price": price,
                                 "change": change, "idx": idx_code})
            except Exception:
                pass
        if len(rows) < 20:
            break
        r += 20
        time.sleep(0.6)
    return results


# ─── 50일 이격도 ──────────────────────────────────────────────────────────────
DISPARITY_CFG = {
    "sp500":  {"symbol": "ES=F",  "name": "S&P500 선물",  "overheat": 110.0, "caution": 105.0, "cooldown": 95.0},
    "nasdaq": {"symbol": "NQ=F",  "name": "나스닥 선물",  "overheat": 110.0, "caution": 105.0, "cooldown": 95.0},
    "kospi":  {"symbol": "^KS11", "name": "코스피",       "overheat": 130.0, "caution": 120.0, "cooldown": 105.0},
}

def get_disparity(symbol: str, cfg: dict) -> dict | None:
    enc = urllib.parse.quote(symbol, safe="")
    url = f"https://query1.finance.yahoo.com/v8/finance/chart/{enc}"
    try:
        r = requests.get(url, params={"range": "6mo", "interval": "1d"},
                         headers={**HEADERS, "Accept": "application/json"}, timeout=20)
        res = r.json()["chart"]["result"][0]
        closes = [c for c in res["indicators"]["quote"][0]["close"] if c is not None]
        if len(closes) < 50:
            return None
        current   = closes[-1]
        prev      = closes[-2] if len(closes) >= 2 else current
        ma50      = sum(closes[-50:]) / 50
        disparity = current / ma50 * 100
        change    = current - prev
        chg_pct   = change / prev * 100 if prev else 0
        ov, cau, cd = cfg["overheat"], cfg["caution"], cfg["cooldown"]
        if   disparity >= ov:  zone, label = "overheat", f"과열권 (≥{ov:.0f}%)"
        elif disparity >= cau: zone, label = "caution",  f"과열 경계 ({cau:.0f}~{ov:.0f}%)"
        elif disparity <= cd:  zone, label = "cooldown", f"과열 해소 (≤{cd:.0f}%)"
        else:                  zone, label = "normal",   f"정상 범위 ({cd:.0f}~{ov:.0f}%)"
        return {"current": current, "prev": prev, "ma50": ma50,
                "disparity": disparity, "change": change, "chg_pct": chg_pct,
                "zone": zone, "label": label}
    except Exception as e:
        print(f"  이격도 오류 ({symbol}): {e}")
        return None

ZONE_EMOJI = {"overheat": "🔴", "caution": "🟠", "normal": "🟢", "cooldown": "🔵"}


# ─── CBOE Put/Call Ratio ─────────────────────────────────────────────────────
def get_equity_pcr() -> dict | None:
    """CBOE 일별 통계 페이지에서 Equity PCR 스크래핑"""
    url = "https://www.cboe.com/markets/us/options/market-statistics/daily/"
    try:
        r = requests.get(url, headers=HEADERS, timeout=20)
        if r.status_code != 200:
            print(f"  PCR HTTP {r.status_code}")
            return None
        soup = BeautifulSoup(r.text, "html.parser")
        # 테이블에서 "EQUITY PUT/CALL RATIO" 행 찾기
        for td in soup.find_all("td"):
            if "EQUITY PUT/CALL RATIO" in td.get_text(strip=True).upper():
                sib = td.find_next_sibling("td")
                if sib:
                    pcr = float(sib.get_text(strip=True))
                    if   pcr >= 1.3: zone, lbl = "fear",    "공포 🔴  (Put 과잉, 역발상 매수 고려)"
                    elif pcr >= 1.0: zone, lbl = "caution", "주의 🟠  (하락 우려 우세)"
                    elif pcr <= 0.6: zone, lbl = "greed",   "탐욕 🟢  (Call 과잉, 역발상 매도 고려)"
                    else:            zone, lbl = "normal",  "중립 ⚪"
                    return {"pcr": pcr, "zone": zone, "label": lbl}
        # 정규식 백업
        m = re.search(r"EQUITY PUT/CALL RATIO\D+([\d.]+)", r.text, re.I)
        if m:
            pcr = float(m.group(1))
            if   pcr >= 1.3: zone, lbl = "fear",    "공포 🔴  (Put 과잉, 역발상 매수 고려)"
            elif pcr >= 1.0: zone, lbl = "caution", "주의 🟠  (하락 우려 우세)"
            elif pcr <= 0.6: zone, lbl = "greed",   "탐욕 🟢  (Call 과잉, 역발상 매도 고려)"
            else:            zone, lbl = "normal",  "중립 ⚪"
            return {"pcr": pcr, "zone": zone, "label": lbl}
        print("  PCR: EQUITY PUT/CALL RATIO 항목을 찾을 수 없음")
        return None
    except Exception as e:
        print(f"  PCR 오류: {e}")
        return None


# ─── Fear & Greed Index ───────────────────────────────────────────────────────
def get_fear_greed() -> dict | None:
    url = "https://production.dataviz.cnn.io/index/fearandgreed/graphdata"
    try:
        r = requests.get(url, headers=HEADERS, timeout=15)
        fg = r.json()["fear_and_greed"]
        score = fg["score"]
        rating = fg["rating"]
        label_map = {
            "Extreme Fear":  "극도의 공포 😱",
            "Fear":          "공포 🔴",
            "Neutral":       "중립 ⚪",
            "Greed":         "탐욕 🟢",
            "Extreme Greed": "극도의 탐욕 🤑",
        }
        label = label_map.get(rating, rating)
        return {"score": score, "label": label}
    except Exception as e:
        print(f"  Fear&Greed 오류: {e}")
        return None


# ─── TGA (재무부 일반계좌) ────────────────────────────────────────────────────
def _nearest(by_date: dict, target: str) -> float | None:
    """target 날짜 이하 중 가장 가까운 값 반환"""
    cands = [dt for dt in by_date if dt <= target]
    if not cands:
        return None
    return by_date[max(cands)]


def get_tga() -> dict | None:
    """Treasury FiscalData DTS — TGA 마감잔고 (백만달러→십억달러)"""
    url = (
        "https://api.fiscaldata.treasury.gov/services/api/fiscal_service"
        "/v1/accounting/dts/operating_cash_balance"
    )
    today = date.today()
    try:
        r = requests.get(url, params={
            "fields":     "record_date,account_type,open_today_bal",
            "filter":     "account_type:eq:Treasury General Account (TGA) Closing Balance",
            "sort":       "-record_date",
            "page[size]": "100",
        }, headers={**HEADERS, "Accept": "application/json"}, timeout=25)
        r.raise_for_status()
        data = r.json().get("data", [])
        print(f"  TGA rows: {len(data)}")
        if not data:
            return None
        by_date: dict[str, float] = {}
        for row in data:
            dt  = row["record_date"]
            by_date[dt] = float(row["open_today_bal"]) / 1_000  # 십억달러
        dates = sorted(by_date.keys(), reverse=True)
        if not dates:
            return None
        cur = by_date[dates[0]]
        return {
            "date": dates[0], "current": cur,
            "w1": _nearest(by_date, (today - timedelta(weeks=1)).isoformat()),
            "w2": _nearest(by_date, (today - timedelta(weeks=2)).isoformat()),
            "m1": _nearest(by_date, (today - timedelta(days=30)).isoformat()),
            "m3": _nearest(by_date, (today - timedelta(days=91)).isoformat()),
        }
    except Exception as e:
        print(f"  TGA 오류: {e}")
        return None


# ─── RRP (역레포) — NY Fed API ────────────────────────────────────────────────
def get_rrp() -> dict | None:
    """NY Fed 역레포 — totalAmtAccepted 달러→십억달러 (최근 3개월)"""
    url = "https://markets.newyorkfed.org/api/rp/reverserepo/all/results/lastTwoWeeks.json"
    today = date.today()
    try:
        r = requests.get(url, headers={**HEADERS, "Accept": "application/json"}, timeout=20)
        r.raise_for_status()
        ops = r.json().get("repo", {}).get("operations", [])
        print(f"  RRP ops: {len(ops)}")
        daily: dict[str, float] = {}
        for op in ops:
            dt  = op.get("operationDate", "")[:10]
            amt = float(op.get("totalAmtAccepted", 0)) / 1_000_000_000
            if dt:
                daily[dt] = daily.get(dt, 0) + amt
        dates = sorted(daily.keys())
        print(f"  RRP dates: {dates[-5:]}")
        if not dates:
            return None
        latest = dates[-1]
        cur = daily[latest]
        return {
            "date": latest, "current": cur,
            "w1": _nearest(daily, (today - timedelta(weeks=1)).isoformat()),
            "w2": _nearest(daily, (today - timedelta(weeks=2)).isoformat()),
            "m1": _nearest(daily, (today - timedelta(days=30)).isoformat()),
            "m3": _nearest(daily, (today - timedelta(days=91)).isoformat()),
        }
    except Exception as e:
        print(f"  RRP 오류: {e}")
        return None


# ─── Yahoo Finance ────────────────────────────────────────────────────────────
def get_yf_price(symbol: str) -> dict | None:
    """VIX 계열도 장후 현재가가 아닌 Yahoo 확정 일봉 종가를 사용."""
    url = f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}"
    try:
        r = requests.get(url, params={"interval": "1d", "range": "5d"},
                         headers={**HEADERS, "Accept": "application/json"}, timeout=15)
        closes = [c for c in r.json()["chart"]["result"][0]
                  ["indicators"]["quote"][0]["close"] if c is not None]
        if not closes:
            return None
        price = closes[-1]
        prev = closes[-2] if len(closes) >= 2 else price
        return {"price": price, "chg_pct": (price - prev) / prev * 100 if prev else 0}
    except Exception as e:
        print(f"  YF 오류 ({symbol}): {e}")
        return None



YF_DAILY_CACHE: dict[str, list[float] | None] = {}


def get_yf_closes(ticker: str) -> list[float] | None:
    """Yahoo 일봉 종가를 조회·캐시한다. 장후 가격 대신 확정 일봉 종가만 사용."""
    if ticker in YF_DAILY_CACHE:
        return YF_DAILY_CACHE[ticker]
    symbol = urllib.parse.quote(ticker.replace(".", "-"), safe="")
    url = f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}"
    try:
        r = requests.get(url, params={"range": "2y", "interval": "1d"},
                         headers={**HEADERS, "Accept": "application/json"}, timeout=15)
        result = r.json()["chart"]["result"][0]
        closes = [c for c in result["indicators"]["quote"][0]["close"] if c is not None]
        YF_DAILY_CACHE[ticker] = closes if len(closes) >= 201 else None
    except Exception as e:
        print(f"  YF 종가 조회 오류 ({ticker}): {e}")
        YF_DAILY_CACHE[ticker] = None
    return YF_DAILY_CACHE[ticker]


def get_yf_close_metrics(ticker: str) -> dict | None:
    closes = get_yf_closes(ticker)
    if not closes or len(closes) < 201:
        return None
    return {
        "last": closes[-1],
        "prev": closes[-2],
        "ma50": sum(closes[-50:]) / 50,
        "ma200": sum(closes[-200:]) / 200,
        "ma200_prev": sum(closes[-201:-1]) / 200,
        "high52_prev": max(closes[-253:-1]) if len(closes) >= 253 else None,
    }


def verify_breakout_close(ticker: str) -> bool:
    """종가가 전일 200MA 이하에서 당일 200MA 위로 올라섰는지 검증."""
    m = get_yf_close_metrics(ticker)
    return bool(m and m["prev"] <= m["ma200_prev"] and m["last"] > m["ma200"])


def verify_above_ma_close(ticker: str, days: int) -> bool:
    """Yahoo 확정 종가가 해당 이동평균선 위인지 검증."""
    m = get_yf_close_metrics(ticker)
    return bool(m and m["last"] > m[f"ma{days}"])


def verify_volume_scanner_close(ticker: str) -> bool:
    """거래량 후보의 +3%와 50MA 위 조건을 Yahoo 종가로 재검증."""
    m = get_yf_close_metrics(ticker)
    return bool(m and m["last"] > m["ma50"] and m["prev"] and
                (m["last"] / m["prev"] - 1) >= 0.03)


def verify_52week_high_close(ticker: str) -> bool:
    """당일 종가가 직전 252거래일 종가 최고치를 넘었는지 검증."""
    m = get_yf_close_metrics(ticker)
    return bool(m and m["high52_prev"] is not None and m["last"] >= m["high52_prev"])

# ─── 메인 ────────────────────────────────────────────────────────────────────
def main():
    print(f"=== 스캐너 시작: {TODAY} ===")

    if KOSPI_ONLY:
        print("[KOSPI] 이격도 수집")
        cfg = DISPARITY_CFG["kospi"]
        d = get_disparity(cfg["symbol"], cfg)
        if d:
            arrow = "▲" if d["change"] > 0 else "▼"
            emoji = ZONE_EMOJI.get(d["zone"], "⚪")
            L = [f"📐 <b>50일선 이격도 (코스피)</b> | {TODAY}", "",
                 f"{emoji} <b>{cfg['name']}</b>",
                 f"  이격도: {d['disparity']:.1f}%  ·  {d['label']}",
                 f"  현재가: {d['current']:,.2f}  {arrow} {abs(d['change']):,.2f} ({d['chg_pct']:+.2f}%)",
                 f"  50일선: {d['ma50']:,.2f}"]
            if d["zone"] == "overheat":
                L.append(f"  ⚠️ Panic Buying 자제 구간")
            elif d["zone"] == "cooldown":
                L.append(f"  🔵 Panic Selling 자제, 이격 조정 끝난 업종 관심")
            L += ["", "이격도 = 현재가 ÷ 50일선 × 100"]
        else:
            L = [f"📐 <b>50일선 이격도 (코스피)</b> | {TODAY}", "⚠️ 데이터 수집 실패"]
        send_tg("\n".join(L))
        print("=== 완료 ===")
        return

    if WEEKLY_ONLY:
        print("[WEEKLY] TGA / RRP 수집")
        tga = get_tga()
        rrp = get_rrp()
        def _chg_line(cur, ref, lbl):
            if ref is None:
                return f"  {lbl}: 데이터 없음"
            chg = cur - ref
            arrow = "▼" if chg < 0 else "▲"
            return f"  {lbl}: {arrow} ${abs(chg):.1f}B"

        L = [f"💧 <b>주간 유동성 보고</b> | {TODAY}", ""]
        if tga:
            cur = tga["current"]
            supply = (tga["w1"] or cur) > cur
            L += [f"🏦 <b>TGA (재무부 일반계좌)</b>  <i>{tga['date']}</i>",
                  f"  잔고: ${cur:.1f}B",
                  _chg_line(cur, tga["w1"], "1주 전 대비"),
                  _chg_line(cur, tga["w2"], "2주 전 대비"),
                  _chg_line(cur, tga["m1"], "1개월 전 대비"),
                  _chg_line(cur, tga["m3"], "3개월 전 대비"),
                  f"  → {'유동성 공급 ✅  (TGA↓ = 달러 시장 방출)' if supply else '유동성 흡수 ⚠️  (TGA↑ = 달러 시장 회수)'}",
                  ""]
        else:
            L += ["🏦 TGA: 데이터 수집 실패", ""]
        if rrp:
            cur = rrp["current"]
            supply = (rrp["w1"] or cur) > cur
            L += [f"💰 <b>RRP (역레포)</b>  <i>{rrp['date']}</i>",
                  f"  잔고: ${cur:.1f}B",
                  _chg_line(cur, rrp["w1"], "1주 전 대비"),
                  _chg_line(cur, rrp["w2"], "2주 전 대비"),
                  _chg_line(cur, rrp["m1"], "1개월 전 대비"),
                  _chg_line(cur, rrp["m3"], "3개월 전 대비"),
                  f"  → {'유동성 공급 ✅  (RRP↓ = MMF 자금 시장 이동)' if supply else '유동성 흡수 ⚠️  (RRP↑ = MMF 자금 안전자산 이동)'}",
                  ""]
        else:
            L += ["💰 RRP: 데이터 수집 실패", ""]
        if tga and rrp:
            ts = (tga["w1"] or tga["current"]) > tga["current"]
            rs = (rrp["w1"] or rrp["current"]) > rrp["current"]
            if ts and rs:   judge = "TGA↓ + RRP↓  →  이중 공급 ✅✅  시장에 긍정적"
            elif ts or rs:  judge = "혼재 신호  →  효과 제한적 ⚠️"
            else:           judge = "TGA↑ + RRP↑  →  이중 흡수 ⛔  시장에 부정적"
            L += [f"📊 <b>종합 판단</b>", f"  {judge}", ""]
        L += ["TGA↓·RRP↓ = 유동성 공급 | TGA↑·RRP↑ = 유동성 흡수",
              "출처: US Treasury FiscalData · NY Fed"]
        send_tg("\n".join(L))
        print("=== 완료 ===")
        return

    snap      = load_snapshot()
    prev_date = snap.get("date")
    prev_sma  = snap.get("sma200", {})

    print("[1] Technical 데이터 수집")
    all_data: dict[str, dict] = {}
    for idx in IDX_ORDER:
        print(f"  {idx}...")
        data = scrape_technical(idx)
        for ticker, d in data.items():
            if ticker not in all_data:
                all_data[ticker] = {**d, "indices": []}
            if idx not in all_data[ticker]["indices"]:
                all_data[ticker]["indices"].append(idx)
        time.sleep(1)
    total = len(all_data)
    print(f"  총 {total}개")

    bo_by_idx: dict[str, list] = {i: [] for i in IDX_ORDER}
    bo_all: set[str] = set()
    if prev_date and prev_date != TODAY:
        for ticker, d in all_data.items():
            pv = prev_sma.get(ticker)
            cv = d["sma200"]
            if pv is not None and pv <= 0 < cv:
                if verify_breakout_close(ticker):
                    bo_all.add(ticker)
                    for idx in d["indices"]:
                        bo_by_idx[idx].append(
                            {"ticker": ticker, "sma200": f"{cv:+.2f}", "price": d["price"]}
                        )
                time.sleep(0.3)
        for idx in IDX_ORDER:
            bo_by_idx[idx].sort(key=lambda x: float(x["sma200"]), reverse=True)

    save_snapshot({"date": TODAY, "sma200": {t: d["sma200"] for t, d in all_data.items()}})
    print("[3] 스냅샷 저장 완료")

    print("[4] 브레드스 수집 및 Yahoo 종가 검증")
    breadth: dict[str, dict] = {}
    for idx in IDX_ORDER:
        n = get_count(f"idx_{idx}")
        finviz_50 = scrape_overview(idx, "ta_sma50_pa")
        finviz_200 = scrape_overview(idx, "ta_sma200_pa")
        a50 = sum(verify_above_ma_close(s["ticker"], 50) for s in finviz_50)
        a200 = sum(verify_above_ma_close(s["ticker"], 200) for s in finviz_200)
        breadth[idx] = {
            "n": n,
            "p50":  f"{a50 / n * 100:.1f}" if n else "?",
            "p200": f"{a200 / n * 100:.1f}" if n else "?",
        }
        time.sleep(0.5)

    print("[5] 거래량 급증 스캐너")
    vol_by_idx: dict[str, list] = {i: [] for i in IDX_ORDER}
    for idx in IDX_ORDER:
        stocks = scrape_overview(idx, "sh_relvol_o2,ta_sma50_pa")
        vol_by_idx[idx] = sorted(
            [s for s in stocks if s["change"] >= 3.0 and
             verify_volume_scanner_close(s["ticker"])],
            key=lambda x: x["change"], reverse=True
        )
        time.sleep(1)

    print("[6] 52주 신고가 스캐너")
    hi_by_idx: dict[str, list] = {i: [] for i in IDX_ORDER}
    for idx in IDX_ORDER:
        stocks = scrape_overview(idx, "ta_highlow52w_nh")
        hi_by_idx[idx] = sorted(
            [s for s in stocks if verify_52week_high_close(s["ticker"])],
            key=lambda x: x["change"], reverse=True
        )
        time.sleep(1)

    vol_set = {s["ticker"] for v in vol_by_idx.values() for s in v}
    seen: set[str] = set()
    both: list[dict] = []
    for s in (s for v in hi_by_idx.values() for s in v):
        if s["ticker"] in vol_set and s["ticker"] not in seen:
            seen.add(s["ticker"])
            both.append(s)

    print("[8] VIX / PCR / Fear&Greed 수집")
    vix   = get_yf_price("%5EVIX")
    vix1m = get_yf_price("%5EVIX1M")
    vxmt  = get_yf_price("%5EVXMT")
    vvix  = get_yf_price("%5EVVIX")
    pcr   = get_equity_pcr()
    fg    = get_fear_greed()

    print("[9] 텔레그램 발송")

    # 메시지1: 200일선 돌파
    L = [f"📊 <b>200일선 돌파 스캐너</b> | {TODAY}",
         f"총 스캔: {total:,}개  |  돌파: {len(bo_all)}개"]
    if not prev_date:
        L.append("\n⚠️ 전일 데이터 없음 — 오늘 베이스라인 저장 완료.\n내일부터 정상 작동합니다.")
    elif not bo_all:
        L.append("\n오늘 새로 200일선을 돌파한 종목이 없습니다.")
    else:
        for idx in IDX_ORDER:
            items = bo_by_idx[idx]
            if not items: continue
            L.append(f"\n{IDX_LABELS[idx]} ({len(items)}개)")
            for e in items[:50]:
                L.append(f"  <code>{e['ticker']}</code>  ${e['price']}  ({e['sma200']}%)")
            if len(items) > 50:
                L.append(f"  ... 외 {len(items)-50}개")
    send_tg("\n".join(L));  time.sleep(1)

    # 메시지2: 브레드스
    L = [f"📈 <b>시장 브레드스</b> | {TODAY}", "", "         50MA위   200MA위"]
    for idx in IDX_ORDER:
        b = breadth[idx]
        L.append(f"{IDX_SHORT[idx]:>4}    {b['p50']:>5}%    {b['p200']:>5}%")
    L += ["", "⚠️ 기준: 50MA / 200MA 40% 이하 = 시장 약세 신호"]
    send_tg("\n".join(L));  time.sleep(1)

    # 메시지3: 거래량 급증
    vol_total = sum(len(v) for v in vol_by_idx.values())
    L = [f"🔥 <b>거래량 급증 스캐너</b> | {TODAY}",
         f"조건: 거래량2배↑ · 50MA위 · 당일+3%↑  |  총 {vol_total}개"]
    if vol_total == 0:
        L.append("\n오늘 조건 충족 종목 없음")
    else:
        for idx in IDX_ORDER:
            items = vol_by_idx[idx]
            if not items: continue
            L.append(f"\n{IDX_LABELS[idx]} ({len(items)}개)")
            for s in items[:50]:
                L.append(f"  <code>{s['ticker']}</code>  ${s['price']:.2f}  +{s['change']:.2f}%")
            if len(items) > 50:
                L.append(f"  ... 외 {len(items)-50}개")
    send_tg("\n".join(L));  time.sleep(1)

    # 메시지4: 52주 신고가
    hi_total = sum(len(v) for v in hi_by_idx.values())
    L = [f"🏆 <b>52주 신고가 돌파</b> | {TODAY}", f"총 {hi_total}개"]
    if hi_total == 0:
        L.append("\n오늘 52주 신고가 종목 없음")
    else:
        for idx in IDX_ORDER:
            items = hi_by_idx[idx]
            if not items: continue
            L.append(f"\n{IDX_LABELS[idx]} ({len(items)}개)")
            for s in items[:50]:
                L.append(f"  <code>{s['ticker']}</code>  ${s['price']:.2f}  +{s['change']:.2f}%")
            if len(items) > 50:
                L.append(f"  ... 외 {len(items)-50}개")
    send_tg("\n".join(L));  time.sleep(1)

    # 메시지5: 교집합
    L = [f"⭐ <b>거래량급증 + 신고가 동시 돌파</b> | {TODAY}",
         f"강력 모멘텀 신호  |  총 {len(both)}개"]
    if not both:
        L.append("\n오늘 두 조건 동시 충족 종목 없음")
    else:
        L.append("")
        for s in both[:50]:
            L.append(f"  <code>{s['ticker']}</code>  ${s['price']:.2f}  +{s['change']:.2f}%")
    send_tg("\n".join(L));  time.sleep(1)

    # 메시지6: VIX + PCR + Fear&Greed
    if vix and vxmt and vvix:
        v1 = vix1m["price"] if vix1m else vix["price"]
        v1_note = "" if vix1m else " (Spot VIX)"
        v3 = vxmt["price"]
        diff = v1 - v3
        if   diff > 0: ts = "Backwardation ⚠️\n→ 단기공포 신호 (급락 전 선행 패턴)"
        elif diff < 0: ts = "Contango ✅\n→ 정상 구조 (장기불확실성 > 단기)"
        else:          ts = "Flat ➡️ 구조 중립"
        extra = f"\n⚠️ 단기공포 강함 (spread {diff:+.1f}pt)" if diff > 2 else ""
        vv = vvix["price"]
        L = [
            f"🌡️ <b>VIX Term Structure</b> | {TODAY}", "",
            f"VIX:    {vix['price']:.2f}  ({vix['chg_pct']:+.1f}%)",
            f"VIX1M:  {v1:.2f}{v1_note}",
            f"VIX3M:  {v3:.2f}  (VXMT 93일)", "",
            f"Term Structure: {ts}{extra}",
            f"Spread: {diff:+.2f}pt (VIX1M - VIX3M)", "",
            f"VVIX: {vv:.0f}  {vvix_label(vv)}",
            "80↓안정 | 80~100정상 | 100~120경계⚡",
            "120~140위험🔴 | 140~160강한공포 | 160↑금융스트레스🆘",
        ]
    else:
        L = [f"🌡️ <b>VIX Term Structure</b> | {TODAY}",
             "⚠️ 데이터 수집 실패 — Yahoo Finance 응답 없음"]
    L.append("")
    if pcr:
        L += [f"📊 <b>Equity Put/Call Ratio (PCR)</b>",
              f"  PCR: {pcr['pcr']:.2f}  →  {pcr['label']}",
              "  기준: ≤0.6 탐욕 | 0.6~1.0 중립 | 1.0~1.3 주의 | ≥1.3 공포"]
    else:
        L.append("📊 PCR: 데이터 수집 실패")
    L.append("")
    if fg:
        L += [f"🧭 <b>공포탐욕지수 (Fear & Greed)</b>",
              f"  지수: {fg['score']:.0f}  →  {fg['label']}",
              "  0~24 극도의공포 | 25~44 공포 | 45~55 중립 | 56~74 탐욕 | 75~100 극도의탐욕"]
    else:
        L.append("🧭 Fear & Greed: 데이터 수집 실패")
    send_tg("\n".join(L))

    # 메시지7: 50일 이격도
    print("[10] 50일 이격도 수집 (미국 선물)")
    US_KEYS = ["sp500", "nasdaq"]
    disp_results = {}
    for key in US_KEYS:
        disp_results[key] = get_disparity(DISPARITY_CFG[key]["symbol"], DISPARITY_CFG[key])
        time.sleep(0.5)
    L = [f"📐 <b>50일선 이격도</b> | {TODAY}", ""]
    for key in US_KEYS:
        cfg = DISPARITY_CFG[key]
        d = disp_results.get(key)
        if not d:
            L.append(f"<b>{cfg['name']}</b>: 데이터 없음\n")
            continue
        arrow = "▲" if d["change"] > 0 else "▼"
        emoji = ZONE_EMOJI.get(d["zone"], "⚪")
        L.append(f"{emoji} <b>{cfg['name']}</b>")
        L.append(f"  이격도: {d['disparity']:.1f}%  ·  {d['label']}")
        L.append(f"  현재가: {d['current']:,.2f}  {arrow} {abs(d['change']):,.2f} ({d['chg_pct']:+.2f}%)")
        L.append(f"  50일선: {d['ma50']:,.2f}")
        if d["zone"] == "overheat":
            L.append(f"  ⚠️ Panic Buying 자제 구간")
        elif d["zone"] == "cooldown":
            L.append(f"  🔵 Panic Selling 자제, 이격 조정 끝난 업종 관심")
        L.append("")
    L += ["이격도 = 현재가 ÷ 50일선 × 100"]
    send_tg("\n".join(L));  time.sleep(1)

    print("=== 완료 ===")


if __name__ == "__main__":
    main()
