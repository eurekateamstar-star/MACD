"""
籌碼面與基本面資料（全部來自官方免費開放資料）
-------------------------------------------------
  三大法人買賣超 ← 證交所 T86、櫃買中心三大法人買賣明細（每日，可查歷史）
  融資融券餘額   ← 證交所 MI_MARGN、櫃買中心融資融券餘額（每日，可查歷史）
  本益比、殖利率、股價淨值比 ← 證交所 BWIBBU_ALL、櫃買中心本益比分析（每日）
  月營收         ← 證交所 / 櫃買中心開放資料（最新月份）＋ 公開資訊觀測站（補歷史月份）
  EPS、毛利率、ROE ← 綜合損益表、資產負債表（最新一季，今年累計）
  千張大戶持股   ← 集保結算所「集保戶股權分散表」（每週）

月營收和大戶持股需要歷史資料才能比較，所以存在 state/ 資料夾，每天累積。
"""

import csv
import io
import json
import re
import time
import urllib.request
from datetime import date
from pathlib import Path

CODE_RE = re.compile(r"^(\d{4}|00\d{3,4}[A-Z]?)$")
TWSE_OPEN = "https://openapi.twse.com.tw/v1"
TPEX_OPEN = "https://www.tpex.org.tw/openapi/v1"


def http_get(url: str, timeout: int = 40, retries: int = 3) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0", "Accept": "application/json,text/html,*/*"})
    for attempt in range(1, retries + 1):
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                data = r.read()
            if data.strip():
                return data
            raise ValueError("回傳內容是空的")
        except Exception:
            if attempt == retries:
                raise
            time.sleep(attempt * 5)


def get_json(url: str, timeout: int = 40):
    return json.loads(http_get(url, timeout))


def num(s):
    """'1,234' / '12.5' / '' → 數字或 None"""
    try:
        v = float(str(s).replace(",", "").strip())
        return v
    except (ValueError, TypeError):
        return None


def norm(row: dict) -> dict:
    return {str(k).strip(): v for k, v in row.items()}


def code_of(row: dict) -> str:
    return str(row.get("公司代號") or row.get("Code") or row.get("SecuritiesCompanyCode") or "").strip().upper()


def _ymd(d: str):
    """'2026-10-16' → ('20261016', '2026/10/16')"""
    return d.replace("-", ""), d.replace("-", "/")


# ============================================================
# 1. 三大法人買賣超（張）
# ============================================================
def fetch_institutional(tdays: list, n: int = 20, pause: float = 2.0) -> dict:
    """最近 n 個交易日的三大法人買賣超 → {代號: {"dates": [...], "f": 外資, "t": 投信, "d": 自營商, "all": 合計}}"""
    days = {}   # 日期 → {代號: (外資, 投信, 自營, 合計)}
    for d in tdays[-n:]:
        a, b = _ymd(d)
        day = {}
        try:
            j = get_json(f"https://www.twse.com.tw/rwd/zh/fund/T86?date={a}&selectType=ALLBUT0999&response=json", 60)
            fields = [str(x).strip() for x in j.get("fields", [])]
            ix = {k: fields.index(k) for k in ("外陸資買賣超股數(不含外資自營商)", "投信買賣超股數", "自營商買賣超股數", "三大法人買賣超股數")}
            for r in j.get("data", []):
                code = str(r[0]).strip()
                if CODE_RE.match(code):
                    day[code] = tuple(round((num(r[ix[k]]) or 0) / 1000) for k in ix)
        except Exception as e:
            print(f"  {d} 上市法人資料無法取得：{e}")
        time.sleep(pause)
        try:
            j = get_json(f"https://www.tpex.org.tw/www/zh-tw/insti/dailyTrade?type=Daily&sect=EW&date={b}&response=json", 60)
            for r in j["tables"][0]["data"]:
                code = str(r[0]).strip()
                if CODE_RE.match(code) and len(r) >= 24:
                    # 欄位順序：外資(不含外資自營商) 2-4、…、投信 11-13、…、自營商合計 20-22、三大法人合計 23
                    day[code] = tuple(round((num(r[i]) or 0) / 1000) for i in (4, 13, 22, 23))
        except Exception as e:
            print(f"  {d} 上櫃法人資料無法取得：{e}")
        time.sleep(pause)
        if day:
            days[d] = day
    dates = sorted(days)
    out = {}
    for code in set().union(*[set(v) for v in days.values()]) if days else set():
        rows = [days[d].get(code) for d in dates]
        out[code] = {
            "dates": dates,
            "f": [r[0] if r else None for r in rows], "t": [r[1] if r else None for r in rows],
            "d": [r[2] if r else None for r in rows], "all": [r[3] if r else None for r in rows],
        }
    print(f"  三大法人：{len(dates)} 天、{len(out)} 檔")
    return out


def streak(values: list) -> int:
    """連續買超（正數）或連續賣超（負數）的天數；最後一天是 0 或沒資料 → 0"""
    vals = [v for v in values if v is not None]
    if not vals or vals[-1] == 0:
        return 0
    sign = 1 if vals[-1] > 0 else -1
    n = 0
    for v in reversed(vals):
        if v * sign > 0:
            n += 1
        else:
            break
    return n * sign


# ============================================================
# 2. 融資融券餘額（張）
# ============================================================
def fetch_margin(tdays: list, n: int = 10, pause: float = 2.0) -> dict:
    """→ {代號: {"dates": [...], "mg": 融資餘額, "sh": 融券餘額}}"""
    days = {}
    for d in tdays[-n:]:
        a, b = _ymd(d)
        day = {}
        try:
            j = get_json(f"https://www.twse.com.tw/rwd/zh/marginTrading/MI_MARGN?date={a}&selectType=ALL&response=json", 60)
            t = next(t for t in j.get("tables", []) if "彙總" in str(t.get("title", "")))
            for r in t["data"]:
                code = str(r[0]).strip()
                if CODE_RE.match(code):
                    day[code] = (num(r[6]), num(r[12]))      # 融資今日餘額、融券今日餘額
        except Exception as e:
            print(f"  {d} 上市融資融券無法取得：{e}")
        time.sleep(pause)
        try:
            j = get_json(f"https://www.tpex.org.tw/www/zh-tw/margin/balance?date={b}&response=json", 60)
            for r in j["tables"][0]["data"]:
                code = str(r[0]).strip()
                if CODE_RE.match(code):
                    day[code] = (num(r[6]), num(r[14]))      # 資餘額、券餘額
        except Exception as e:
            print(f"  {d} 上櫃融資融券無法取得：{e}")
        time.sleep(pause)
        if day:
            days[d] = day
    dates = sorted(days)
    out = {}
    for code in set().union(*[set(v) for v in days.values()]) if days else set():
        rows = [days[d].get(code) for d in dates]
        out[code] = {"dates": dates, "mg": [r[0] if r else None for r in rows], "sh": [r[1] if r else None for r in rows]}
    print(f"  融資融券：{len(dates)} 天、{len(out)} 檔")
    return out


# ============================================================
# 3. 本益比、殖利率、股價淨值比
# ============================================================
def fetch_valuation() -> dict:
    out = {}
    for url, pe, yd, pb in ((f"{TWSE_OPEN}/exchangeReport/BWIBBU_ALL", "PEratio", "DividendYield", "PBratio"),
                            (f"{TPEX_OPEN}/tpex_mainboard_peratio_analysis", "PriceEarningRatio", "YieldRatio", "PriceBookRatio")):
        try:
            rows = list(map(norm, get_json(url, 60)))
            for r in rows:
                code = code_of(r)
                if CODE_RE.match(code):
                    out[code] = {"pe": num(r.get(pe)), "yield": num(r.get(yd)), "pb": num(r.get(pb))}
            print(f"  本益比 / 殖利率（{'上市' if 'twse' in url else '上櫃'}）：{len(rows)} 筆")
        except Exception as e:
            print(f"  無法取得本益比資料（{url}）：{e}")
    return out


# ============================================================
# 4. 月營收（千元）
# ============================================================
def _ym_add(ym: str, k: int) -> str:
    y, m = int(ym[:4]), int(ym[5:])
    t = y * 12 + (m - 1) + k
    return f"{t // 12:04d}-{t % 12 + 1:02d}"


def _parse_mops_revenue(html: str):
    """公開資訊觀測站月營收表格 → [(代號, 當月營收, 去年當月營收)]"""
    out = []
    for row in re.findall(r"<tr[^>]*>(.*?)</tr>", html, re.S | re.I):
        cells = [re.sub(r"<[^>]+>|&nbsp;", "", c).strip() for c in re.findall(r"<td[^>]*>(.*?)</td>", row, re.S | re.I)]
        if len(cells) >= 5 and CODE_RE.match(cells[0]):
            out.append((cells[0], num(cells[2]), num(cells[4])))
    return out


def fetch_revenue(state_dir: Path, months: int = 24, pause: float = 1.0) -> dict:
    path = state_dir / "revenue.json"
    try:
        cache = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        cache = {}

    def put(code, ym, v):
        if v is not None and v > 0:
            cache.setdefault(code, {})[ym] = v

    latest_info, latest_ym = {}, None
    for url in (f"{TWSE_OPEN}/opendata/t187ap05_L", f"{TPEX_OPEN}/mopsfin_t187ap05_O"):
        try:
            rows = list(map(norm, get_json(url, 60)))
            for r in rows:
                code = code_of(r)
                m = re.match(r"(\d{2,3})(\d{2})$", str(r.get("資料年月", "")).strip())
                if not (CODE_RE.match(code) and m):
                    continue
                ym = f"{int(m.group(1)) + 1911:04d}-{m.group(2)}"
                latest_ym = max(latest_ym or ym, ym)
                put(code, ym, num(r.get("營業收入-當月營收")))
                put(code, _ym_add(ym, -1), num(r.get("營業收入-上月營收")))
                put(code, _ym_add(ym, -12), num(r.get("營業收入-去年當月營收")))
                latest_info[code] = {"ym": ym, "yoy": num(r.get("營業收入-去年同月增減(%)")),
                                     "mom": num(r.get("營業收入-上月比較增減(%)")),
                                     "cum_yoy": num(r.get("累計營業收入-前期比較增減(%)"))}
            print(f"  月營收（{'上市' if 'twse' in url else '上櫃'}）：{len(rows)} 筆")
        except Exception as e:
            print(f"  無法取得月營收（{url}）：{e}")

    # 補歷史：公開資訊觀測站每月一頁，同時有「當月」和「去年當月」，抓 12 頁就有 24 個月
    if latest_ym:
        fetched = 0
        n_latest = sum(1 for v in latest_info.values() if v["ym"] == latest_ym) or 1
        for k in range(0, 12):
            ym = _ym_add(latest_ym, -k)
            have = sum(1 for v in cache.values() if ym in v and _ym_add(ym, -12) in v)
            if have >= 0.8 * n_latest:      # 大部分公司都有這個月和去年同月的資料，就不用再抓
                continue
            y, m = int(ym[:4]) - 1911, int(ym[5:])
            for mkt in ("sii", "otc"):
                try:
                    html = http_get(f"https://mopsov.twse.com.tw/nas/t21/{mkt}/t21sc03_{y}_{m}_0.html", 60).decode("cp950", "ignore")
                    for code, cur, last in _parse_mops_revenue(html):
                        put(code, ym, cur)
                        put(code, _ym_add(ym, -12), last)
                    fetched += 1
                except Exception as e:
                    print(f"  {ym} 歷史月營收（{mkt}）無法取得：{e}")
                time.sleep(pause)
        if fetched:
            print(f"  補抓歷史月營收：{fetched} 頁")

    # 只保留最近 30 個月
    if latest_ym:
        keep = {_ym_add(latest_ym, -k) for k in range(30)}
        cache = {c: {ym: v for ym, v in d.items() if ym in keep} for c, d in cache.items()}
        cache = {c: d for c, d in cache.items() if d}
    state_dir.mkdir(exist_ok=True)
    path.write_text(json.dumps(cache, ensure_ascii=False, separators=(",", ":"), sort_keys=True), encoding="utf-8")

    out = {}
    for code, hist in cache.items():
        info = latest_info.get(code)
        ym = info["ym"] if info else max(hist)
        cur = hist.get(ym)
        if cur is None:
            continue
        series = [(_ym_add(ym, -k), hist.get(_ym_add(ym, -k))) for k in range(23, -1, -1)]
        prev12 = [v for m, v in series[-12:-1] if v]
        prev24 = [v for m, v in series[:-1] if v]
        yoy_streak = 0
        for k in range(0, 24):
            a, b = hist.get(_ym_add(ym, -k)), hist.get(_ym_add(ym, -k - 12))
            if a and b and a > b:
                yoy_streak += 1
            else:
                break
        last_year = hist.get(_ym_add(ym, -12))
        out[code] = {
            "ym": ym,
            "yoy": info["yoy"] if info and info["yoy"] is not None else ((cur / last_year - 1) * 100 if last_year else None),
            "mom": info["mom"] if info else None,
            "cum_yoy": info["cum_yoy"] if info else None,
            "hi12": (1 if cur >= max(prev12) else 0) if len(prev12) >= 11 else None,
            "hi24": (1 if cur >= max(prev24) else 0) if len(prev24) >= 22 else None,
            "yoy_streak": yoy_streak,
            "months": [m for m, _ in series], "v": [v for _, v in series],
        }
    print(f"  月營收整理完成：{len(out)} 檔（最新月份 {latest_ym}）")
    return out


# ============================================================
# 5. 財報：EPS、毛利率、營益率、ROE（最新一季，今年累計）
# ============================================================
def _first(row: dict, *patterns):
    for p in patterns:
        for k, v in row.items():
            if re.search(p, k) and num(v) is not None:
                return num(v)
    return None


def fetch_financials() -> dict:
    inc, bal = {}, {}
    kinds = ("ci", "basi", "bd", "fh", "ins", "mim")
    for k in kinds:
        for url, store in ((f"{TWSE_OPEN}/opendata/t187ap06_L_{k}", inc), (f"{TPEX_OPEN}/mopsfin_t187ap06_O_{k}", inc),
                           (f"{TWSE_OPEN}/opendata/t187ap07_L_{k}", bal), (f"{TPEX_OPEN}/mopsfin_t187ap07_O_{k}", bal)):
            try:
                for r in map(norm, get_json(url, 60)):
                    code = code_of(r)
                    if CODE_RE.match(code):
                        store[code] = r
            except Exception as e:
                print(f"  財報資料無法取得（{url}）：{e}")
    out = {}
    for code, r in inc.items():
        year = num(r.get("年度") or r.get("Year"))
        q = num(r.get("季別") or r.get("Season"))
        eps = _first(r, r"每股盈餘")
        rev = _first(r, r"^營業收入$", r"^收益合計$", r"^淨收益$", r"收入合計", r"^收益$")
        gp = _first(r, r"^營業毛利（毛損）淨額$", r"^營業毛利（毛損）$")
        op = _first(r, r"^營業利益（損失）$")
        ni = _first(r, r"淨利（淨損）歸屬於母公司業主", r"^本期淨利（淨損）$", r"本期稅後淨利")
        b = bal.get(code, {})
        equity = _first(b, r"歸屬於母公司業主之權益", r"^權益總計$", r"^權益總額$")
        bvps = _first(b, r"每股參考淨值")
        assets, liab = _first(b, r"^資產總計$", r"^資產總額$"), _first(b, r"^負債總計$", r"^負債總額$")
        ca, cl = _first(b, r"^流動資產$"), _first(b, r"^流動負債$")
        rec = {"year": int(year + 1911) if year else None, "q": int(q) if q else None, "eps": eps, "bvps": bvps}
        if rev:
            rec["gm"] = round(gp / rev * 100, 2) if gp is not None else None
            rec["opm"] = round(op / rev * 100, 2) if op is not None else None
        if ni is not None and equity and q:
            rec["roe"] = round(ni * (4 / q) / equity * 100, 2)   # 換算成一年
        if assets and liab is not None:
            rec["debt"] = round(liab / assets * 100, 1)           # 負債比
        if ca and cl:
            rec["cur"] = round(ca / cl * 100, 0)                  # 流動比率
        out[code] = rec
    print(f"  財報：{len(out)} 檔")
    return out


# ============================================================
# 6. 千張大戶持股（集保結算所，每週）
# ============================================================
def fetch_holders(state_dir: Path) -> dict:
    """持股分級 15 = 1,000,001 股以上（千張大戶）；12~15 = 400 張以上；17 = 合計"""
    path = state_dir / "holders.json"
    try:
        hist = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        hist = {}
    try:
        text = http_get("https://opendata.tdcc.com.tw/getOD.ashx?id=1-5", 120).decode("utf-8-sig", "ignore")
        week, cur = None, {}
        for row in csv.reader(io.StringIO(text)):
            if len(row) < 6 or not row[0].strip().isdigit():
                continue
            d, code, lv = row[0].strip(), row[1].strip(), int(row[2])
            if not CODE_RE.match(code):
                continue
            week = f"{d[:4]}-{d[4:6]}-{d[6:8]}"
            c = cur.setdefault(code, [0.0, 0.0, 0])
            if lv == 15:
                c[0] = num(row[5]) or 0.0
            if 12 <= lv <= 15:
                c[1] += num(row[5]) or 0.0
            if lv == 17:
                c[2] = int(num(row[3]) or 0)
        if week and cur:
            hist[week] = {k: [round(v[0], 2), round(v[1], 2), v[2]] for k, v in cur.items()}
        print(f"  集保持股分級：{len(cur)} 檔（{week}）")
    except Exception as e:
        print(f"  無法取得集保持股分級：{e}")
    weeks = sorted(hist)[-8:]
    hist = {w: hist[w] for w in weeks}
    state_dir.mkdir(exist_ok=True)
    path.write_text(json.dumps(hist, ensure_ascii=False, separators=(",", ":"), sort_keys=True), encoding="utf-8")

    out = {}
    if not weeks:
        return out
    last = hist[weeks[-1]]
    prev = hist[weeks[-2]] if len(weeks) >= 2 else {}
    for code, (b1000, b400, people) in last.items():
        p = prev.get(code)
        out[code] = {
            "week": weeks[-1], "big1000": b1000, "big400": b400, "people": people,
            "chg1000": round(b1000 - p[0], 2) if p else None,
            "chg_people": round((people / p[2] - 1) * 100, 2) if p and p[2] else None,
            "weeks": [w for w in weeks if code in hist[w]],
            "hist1000": [hist[w][code][0] for w in weeks if code in hist[w]],
        }
    return out


# ============================================================
# 7. 風險：變更交易（含全額交割）、管理股票、停止交易、當沖比例
# ============================================================
def fetch_risk(last_day: str) -> dict:
    """→ {代號: {"altered": 變更交易/分盤, "managed": 管理股票, "suspended": 停止交易, "dt_shares": 當沖成交股數}}"""
    out = {}
    try:
        for r in map(norm, get_json(f"{TWSE_OPEN}/exchangeReport/TWT85U", 60)):
            code = code_of(r)
            if CODE_RE.match(code):
                out.setdefault(code, {})["altered"] = 1
    except Exception as e:
        print(f"  無法取得上市變更交易：{e}")
    try:
        yes = lambda v: str(v or "").strip() in ("Y", "Ｙ")
        for r in map(norm, get_json(f"{TPEX_OPEN}/tpex_cmode", 60)):
            code = code_of(r)
            if not CODE_RE.match(code):
                continue
            d = out.setdefault(code, {})
            if yes(r.get("AlteredTrading")) or yes(r.get("PeriodicTrading")):
                d["altered"] = 1
            if yes(r.get("ManagedStock")):
                d["managed"] = 1
            if yes(r.get("SuspensionOfTrading")):
                d["suspended"] = 1
    except Exception as e:
        print(f"  無法取得上櫃變更交易：{e}")
    n_flag = len(out)
    if last_day:
        try:
            a, _ = _ymd(last_day)
            j = get_json(f"https://www.twse.com.tw/rwd/zh/dayTrading/TWTB4U?date={a}&selectType=All&response=json", 60)
            t = next(t for t in j.get("tables", []) if "成交量值" in str(t.get("title", "")))
            for r in t["data"]:
                code = str(r[0]).strip()
                if CODE_RE.match(code):
                    out.setdefault(code, {})["dt_shares"] = num(r[3])
        except Exception as e:
            print(f"  無法取得當沖資料：{e}")
    print(f"  風險資料：變更交易 / 管理 / 停止交易 {n_flag} 檔，當沖資料 {sum(1 for v in out.values() if 'dt_shares' in v)} 檔")
    return out


# ============================================================
# 8. 近期新聞（Google 新聞 RSS：只取標題、來源、時間和連結）
# ============================================================
def fetch_media_news(targets: list, days: int = 7, max_items: int = 8, pause: float = 0.6) -> dict:
    """targets：[(代號, 名稱)] → {代號: [{"t": 標題, "s": 來源, "d": 時間, "u": 連結}]}"""
    import xml.etree.ElementTree as ET
    from email.utils import parsedate_to_datetime
    from urllib.parse import quote
    from datetime import datetime, timedelta, timezone

    out, fail = {}, 0
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    for code, name in targets:
        q = quote(f"{name} {code} when:{days}d")
        url = f"https://news.google.com/rss/search?q={q}&hl=zh-TW&gl=TW&ceid=TW:zh-Hant"
        try:
            root = ET.fromstring(http_get(url, 30, retries=2))
            items, seen = [], set()
            for it in root.iter("item"):
                title = (it.findtext("title") or "").strip()
                src = (it.findtext("source") or "").strip()
                if src and title.endswith(" - " + src):
                    title = title[: -len(src) - 3].strip()
                if not title or title in seen:
                    continue
                try:
                    dt = parsedate_to_datetime(it.findtext("pubDate") or "")
                except Exception:
                    dt = None
                if dt and dt < cutoff:
                    continue
                seen.add(title)
                items.append({"t": title, "s": src, "u": (it.findtext("link") or "").strip(),
                              "d": dt.astimezone(timezone(timedelta(hours=8))).strftime("%Y-%m-%d %H:%M") if dt else ""})
            items.sort(key=lambda x: x["d"], reverse=True)
            if items:
                out[code] = items[:max_items]
        except Exception:
            fail += 1
            if fail >= 15 and not out:           # 一開始就連續失敗，多半是被擋，不要再浪費時間
                print("  新聞來源連續失敗，這次先略過")
                break
        time.sleep(pause)
    print(f"  近期新聞：查詢 {len(targets)} 檔，有新聞 {len(out)} 檔（失敗 {fail}）")
    return out


# ============================================================
# 9. 社群熱度：PTT 股票版（公開網頁，只讀標題、推文數與連結）
# ============================================================
# 兩個字、容易跟一般詞彙混淆的公司簡稱，標題裡出現不算（除非同時寫了代號）
_PTT_STOP = {"大同", "統一", "中華", "台灣", "第一", "開發", "華南", "台新", "永豐", "國泰", "富邦", "元大", "新光",
             "中信", "玉山", "凱基", "群益", "全新", "正新", "聯合", "大成", "東元", "光寶", "神達", "力成",
             "台開", "三商", "同欣", "天鈺", "強茂", "大立", "中興", "新產", "台光", "精華", "美時", "長興"}


def fetch_ptt(names: dict, days: int = 3, max_pages: int = 80, pause: float = 0.5) -> dict:
    """names：{代號: 簡稱} → {代號: {"posts": 文章數, "push": 推文數合計, "items": [{"t","u","p","d"}]}}"""
    from datetime import datetime, timedelta, timezone
    tz = timezone(timedelta(hours=8))
    cutoff = datetime.now(tz) - timedelta(days=days)
    base = "https://www.ptt.cc"
    url = f"{base}/bbs/Stock/index.html"
    posts = []
    for _ in range(max_pages):
        try:
            html = http_get(url, 30, retries=2).decode("utf-8", "ignore")
        except Exception as e:
            print(f"  PTT 無法讀取：{e}")
            break
        page_old = True
        for m in re.finditer(r'<div class="r-ent">(.*?)<div class="mark">', html, re.S):
            blk = m.group(1)
            a = re.search(r'<a href="(/bbs/Stock/M\.(\d+)\.[^"]+)">(.*?)</a>', blk, re.S)
            if not a:
                continue                                   # 已刪除的文章
            ts = datetime.fromtimestamp(int(a.group(2)), tz)
            if ts >= cutoff:
                page_old = False
            n = re.search(r'<div class="nrec">(?:<span[^>]*>)?([^<]*)', blk)
            nrec = (n.group(1).strip() if n else "")
            push = 100 if nrec == "爆" else (-10 if nrec.startswith("X") else int(nrec) if nrec.isdigit() else 0)
            title = re.sub(r"<[^>]+>", "", a.group(3)).replace("&amp;", "&").replace("&lt;", "<").replace("&gt;", ">").strip()
            posts.append((ts, title, base + a.group(1), push))
        prev = re.search(r'<a class="btn wide" href="(/bbs/Stock/index\d+\.html)">&lsaquo; 上頁</a>', html)
        if page_old or not prev:
            break
        url = base + prev.group(1)
        time.sleep(pause)
    posts = [p for p in posts if p[0] >= cutoff]
    # 公告、盤中閒聊等固定文章不算
    posts = [p for p in posts if not re.match(r"^\[(公告|閒聊)\]", p[1]) and "盤中閒聊" not in p[1] and "盤後閒聊" not in p[1]]

    code_re = re.compile(r"(?<!\d)(\d{4,6}[A-Z]?)(?!\d)")
    by_name = sorted(((n, c) for c, n in names.items() if n and len(n) >= 2 and n not in _PTT_STOP), key=lambda x: -len(x[0]))
    out = {}
    for ts, title, url, push in posts:
        hit = {c for c in code_re.findall(title) if c in names and not (2000 <= int(c[:4]) <= 2035 and len(c) == 4 and names[c] not in title)}
        for n, c in by_name:
            if n in title:
                hit.add(c)
        for c in hit:
            d = out.setdefault(c, {"posts": 0, "push": 0, "items": []})
            d["posts"] += 1
            d["push"] += max(push, 0)
            d["items"].append({"t": title, "u": url, "p": push, "d": ts.strftime("%m/%d %H:%M")})
    for d in out.values():
        d["items"] = sorted(d["items"], key=lambda x: x["d"], reverse=True)[:8]
    print(f"  PTT 股票版：最近 {days} 天 {len(posts)} 篇文章，提到 {len(out)} 檔股票")
    return out
