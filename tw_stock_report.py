"""
台股新手學習站產生器（全部台股 + 搜尋版）
-------------------------------------------------
Python 負責：
  1. 取得全部上市、上櫃股票與 ETF 清單（證交所 / 櫃買中心）
  2. 批次下載股價（Yahoo Finance）
  3. 計算指標、白話解釋、簡單回測
  4. 每檔存成 site/data/代號.json，並產生 site/list.json（搜尋用）

網頁（web/ 資料夾）：
  index.html   ← 首頁：搜尋股票、我的自選
  report.html  ← 個股報告：讀取 data/代號.json 畫圖

在 GitHub 上：GitHub Actions 每個交易日收盤後自動執行，並發佈到 GitHub Pages。
在自己電腦上也能跑：
  pip install -r requirements.txt
  python tw_stock_report.py              ← 全部台股（約 2000 檔，需要幾分鐘）
  python tw_stock_report.py --list       ← 只跑 stocks.txt 裡的自選股
  python tw_stock_report.py 2330 2317    ← 只跑指定的股票
跑完會自動開一個本機網址讓你用瀏覽器看（按 Ctrl+C 結束）。

stocks.txt 是「預設自選股」：第一次打開網站時顯示在首頁。
在網頁上加入 / 移除自選，會記在你自己的手機或電腦上。

※ 本程式僅供學習，不構成任何投資建議。
"""

import argparse
import json
import os
import re
import shutil
import sys
import time
import urllib.request
import webbrowser
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd
import yfinance as yf

HERE = Path(__file__).resolve().parent
SITE = HERE / "site"
WEB = HERE / "web"
TW = ZoneInfo("Asia/Taipei")
IN_CI = bool(os.environ.get("GITHUB_ACTIONS"))

# 一般股票是 4 位數字；ETF 是 00 開頭（例如 0050、00878、00632R）
CODE_RE = re.compile(r"^(\d{4}|00\d{3,4}[A-Z]?)$")
TWSE_API = "https://openapi.twse.com.tw/v1/exchangeReport/STOCK_DAY_ALL"
TPEX_API = "https://www.tpex.org.tw/openapi/v1/tpex_mainboard_daily_close_quotes"
MARKET = {".TW": "上市", ".TWO": "上櫃"}


def now_tw() -> str:
    return datetime.now(TW).strftime("%Y-%m-%d %H:%M")


def notice(msg: str):
    """在 GitHub Actions 的執行結果頁面顯示一則訊息。"""
    print(msg)
    if IN_CI:
        print(f"::notice title=更新結果::{msg}")


def read_stock_list(path: Path) -> list:
    """讀 stocks.txt。每行：代號 名稱（名稱可省略），# 開頭是註解。"""
    if not path.exists():
        return []
    stocks = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.split("#")[0].strip()
        if not line:
            continue
        parts = line.split(maxsplit=1)
        stocks.append((parts[0].upper(), parts[1].strip() if len(parts) > 1 else ""))
    return stocks


# ============================================================
# 1. 全部台股清單
# ============================================================
def http_get(url: str, timeout: int = 30) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


def _pick(row: dict, keys) -> str:
    for k in keys:
        if row.get(k):
            return str(row[k]).strip()
    return ""


def list_from_openapi() -> dict:
    """證交所、櫃買中心的開放資料：每天所有交易中的股票。"""
    out = {}
    for url, suffix in ((TWSE_API, ".TW"), (TPEX_API, ".TWO")):
        try:
            rows = json.loads(http_get(url))
            n = 0
            for r in rows:
                code = _pick(r, ["Code", "SecuritiesCompanyCode", "公司代號"]).upper()
                name = _pick(r, ["Name", "CompanyName", "公司名稱"])
                if CODE_RE.match(code) and code not in out:
                    out[code] = (name or code, suffix)
                    n += 1
            print(f"  {MARKET[suffix]}：{n} 檔")
        except Exception as e:
            print(f"  無法取得{MARKET[suffix]}清單：{e}")
    return out


def list_from_isin() -> dict:
    """備援：證交所的「有價證券代號」公告頁面。"""
    out = {}
    for mode, suffix in ((2, ".TW"), (4, ".TWO")):
        try:
            text = http_get(f"https://isin.twse.com.tw/isin/C_public.jsp?strMode={mode}", 60)
            text = text.decode("cp950", errors="ignore")
            n = 0
            for code, name in re.findall(r"<td[^>]*>\s*([0-9A-Z]{4,6})　([^<]+?)\s*</td>", text):
                if CODE_RE.match(code) and code not in out:
                    out[code] = (name.strip(), suffix)
                    n += 1
            print(f"  {MARKET[suffix]}（備援）：{n} 檔")
        except Exception as e:
            print(f"  備援來源也無法取得{MARKET[suffix]}清單：{e}")
    return out


def get_all_stocks() -> dict:
    """回傳 {代號: (名稱, '.TW' 或 '.TWO')}"""
    print("取得全部台股清單…")
    stocks = list_from_openapi()
    if len(stocks) < 500:
        print("  清單不完整，改用備援來源補齊…")
        for code, v in list_from_isin().items():
            stocks.setdefault(code, v)
    return stocks


# ============================================================
# 2. 批次下載股價
# ============================================================
def download_prices(tickers: list, period: str, chunk: int = 50) -> dict:
    """一次下載一批，比一檔一檔快很多。回傳 {ticker: DataFrame}"""
    result = {}
    total = len(tickers)
    for i in range(0, total, chunk):
        part = tickers[i:i + chunk]
        data = None
        for attempt in range(1, 4):
            try:
                data = yf.download(part, period=period, group_by="ticker", auto_adjust=True,
                                   threads=True, progress=False)
                break
            except Exception as e:
                print(f"  第 {i + 1} 批下載失敗（{e}），{attempt * 15} 秒後重試…")
                time.sleep(attempt * 15)
        if data is not None and not data.empty:
            multi = isinstance(data.columns, pd.MultiIndex)
            have = set(data.columns.get_level_values(0)) if multi else set()
            for t in part:
                if multi and t not in have:
                    continue
                df = (data[t] if multi else data).dropna(subset=["Close"])
                if len(df) >= 30:
                    if df.index.tz is not None:
                        df.index = df.index.tz_localize(None)
                    result[t] = df.copy()
        print(f"  已下載 {min(i + chunk, total)}/{total}，有資料 {len(result)} 檔")
        time.sleep(1)  # 每批之間停一下，避免被資料來源擋
    return result


# ============================================================
# 3. 計算指標
# ============================================================
def add_indicators(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()
    # 均線：過去 N 天收盤價的平均
    df["MA5"] = df["Close"].rolling(5).mean()     # 週線
    df["MA20"] = df["Close"].rolling(20).mean()   # 月線
    df["MA60"] = df["Close"].rolling(60).mean()   # 季線

    # RSI(14)：最近漲勢與跌勢的力道比較，0~100
    delta = df["Close"].diff()
    gain = delta.clip(lower=0).rolling(14).mean()
    loss = (-delta.clip(upper=0)).rolling(14).mean()
    df["RSI"] = 100 - 100 / (1 + gain / loss)

    # KD(9,3,3)：今天收盤價落在最近 9 天高低區間的哪個位置
    low9 = df["Low"].rolling(9).min()
    high9 = df["High"].rolling(9).max()
    rsv = (df["Close"] - low9) / (high9 - low9) * 100
    df["K"] = rsv.ewm(com=2, adjust=False).mean()
    df["D"] = df["K"].ewm(com=2, adjust=False).mean()

    # 成交量（Yahoo 單位是「股」，1 張 = 1000 股）
    df["Lots"] = df["Volume"] / 1000
    df["LotsMA20"] = df["Lots"].rolling(20).mean()

    # 每日報酬率
    df["Return"] = df["Close"].pct_change()
    return df


def ok(v) -> bool:
    return v is not None and not pd.isna(v)


# ============================================================
# 4. 簡單回測：MA5 在 MA20 上方就持有，否則空手
# ============================================================
def backtest(df: pd.DataFrame) -> dict:
    valid = df["MA20"].notna() & df["MA5"].notna()
    if valid.sum() < 2:
        return {"start": 0, "strat": [], "hold": [], "trades": 0}
    start = int(valid.values.argmax())
    d = df.iloc[start:].copy()
    # 用「前一天」的訊號決定今天要不要持有，避免偷看未來
    d["Hold"] = (d["MA5"] > d["MA20"]).shift(1, fill_value=False)
    strat = (1 + d["Return"].where(d["Hold"], 0).fillna(0)).cumprod() - 1
    hold = d["Close"] / d["Close"].iloc[0] - 1
    return {
        "start": start,
        "strat": [round(v * 100, 2) for v in strat],
        "hold": [round(v * 100, 2) for v in hold],
        "trades": int((d["Hold"].astype(int).diff() == 1).sum()),
    }


# ============================================================
# 5. 白話解釋：產生一張張「分析卡片」
#    tone: up = 偏多(紅)  down = 偏空(綠)  neutral = 中性  warn = 提醒
# ============================================================
def analyze(df: pd.DataFrame, bt: dict) -> list:
    last, prev = df.iloc[-1], df.iloc[-2]
    close = last["Close"]
    cards = []

    # --- 趨勢（均線排列）---
    if ok(last["MA5"]) and ok(last["MA20"]) and ok(last["MA60"]):
        if last["MA5"] > last["MA20"] > last["MA60"]:
            cards.append(dict(title="趨勢", value="多頭排列", tone="up",
                text="週線 > 月線 > 季線，短中長期的平均成本一路墊高，代表趨勢往上。"))
        elif last["MA5"] < last["MA20"] < last["MA60"]:
            cards.append(dict(title="趨勢", value="空頭排列", tone="down",
                text="週線 < 月線 < 季線，平均成本一路往下，代表趨勢偏弱。"))
        else:
            cards.append(dict(title="趨勢", value="均線糾結", tone="neutral",
                text="三條均線交錯，方向還不明確，常見於盤整期。"))

    # --- 股價 vs 月線、季線 ---
    mas = [(n, last[ma]) for ma, n in (("MA20", "月線"), ("MA60", "季線")) if ok(last[ma])]
    if mas:
        above = [n for n, v in mas if close > v]
        below = [n for n, v in mas if close <= v]
        if above and not below:
            tone, val = "up", f"站上{'與'.join(above)}"
        elif below and not above:
            tone, val = "down", f"跌破{'與'.join(below)}"
        else:
            tone, val = "neutral", f"在{'、'.join(above)}之上，{'、'.join(below)}之下"
        levels = "、".join(f"{n} {v:.2f}" for n, v in mas)
        cards.append(dict(title="股價位置", value=val, tone=tone,
            text=f"收盤 {close:.2f}，{levels}。股價在均線之上，代表最近這段時間買進的人平均是賺錢的；"
                 "在均線之下，則代表平均是虧錢的，上漲時容易遇到解套賣壓。"))

    # --- RSI ---
    rsi = last["RSI"]
    if ok(rsi):
        if rsi >= 70:
            tone, text = "warn", "超過 70 通常視為「過熱」，短線漲多，要留意回檔。"
        elif rsi <= 30:
            tone, text = "warn", "低於 30 通常視為「超賣」，短線跌深，可能出現反彈。"
        else:
            tone, text = "neutral", "介於 30~70 之間，屬於正常區間，沒有過熱或超賣。"
        cards.append(dict(title="RSI(14)", value=f"{rsi:.1f}", tone=tone, text=text))

    # --- KD ---
    recent = df[["K", "D"]].tail(3)
    if recent.notna().all().all():
        k, d = last["K"], last["D"]
        golden = any(recent["K"].iloc[i] > recent["D"].iloc[i] and
                     recent["K"].iloc[i - 1] <= recent["D"].iloc[i - 1] for i in (1, 2))
        death = any(recent["K"].iloc[i] < recent["D"].iloc[i] and
                    recent["K"].iloc[i - 1] >= recent["D"].iloc[i - 1] for i in (1, 2))
        if golden:
            tone, text = "up", "最近 K 線由下往上穿過 D 線，稱為「黃金交叉」，短線轉強的訊號。"
        elif death:
            tone, text = "down", "最近 K 線由上往下穿過 D 線，稱為「死亡交叉」，短線轉弱的訊號。"
        elif k >= 80:
            tone, text = "warn", "K 值在 80 以上屬於高檔區，短線偏熱。"
        elif k <= 20:
            tone, text = "warn", "K 值在 20 以下屬於低檔區，短線偏冷。"
        else:
            tone, text = "neutral", "K、D 在 20~80 之間，沒有明顯訊號。"
        cards.append(dict(title="KD(9,3,3)", value=f"K {k:.1f} / D {d:.1f}", tone=tone, text=text))

    # --- 成交量 ---
    if ok(last["LotsMA20"]) and last["LotsMA20"] > 0:
        ratio = last["Lots"] / last["LotsMA20"]
        up_day = close >= prev["Close"]
        if ratio > 1.5:
            tone = "up" if up_day else "down"
            text = ("明顯放量。上漲時放量代表買盤積極，比較健康。" if up_day
                    else "明顯放量下跌，代表有人急著賣，要小心。")
        elif ratio < 0.6:
            tone, text = "neutral", "量縮，交易清淡，市場在觀望。"
        else:
            tone, text = "neutral", "量能和平常差不多。"
        cards.append(dict(title="成交量", value=f"{last['Lots']:,.0f} 張（均量 {ratio:.1f} 倍）",
                          tone=tone, text=text))

    # --- 風險 ---
    vol = df["Return"].std() * (252 ** 0.5) * 100
    mdd = ((df["Close"] / df["Close"].cummax()) - 1).min() * 100
    if ok(vol):
        cards.append(dict(title="風險", value=f"波動度 {vol:.1f}%｜最大回檔 {mdd:.1f}%", tone="neutral",
            text="波動度越高，股價上下震盪越劇烈。最大回檔是期間內從高點最多跌了多少，"
                 "可以想像：如果在最高點買進，最慘會虧多少。"))
    return cards


# ============================================================
# 6. 每檔股票存成 JSON
# ============================================================
def clean(series, nd=2):
    return [None if pd.isna(x) else round(float(x), nd) for x in series]


def build_record(code, name, ticker, df, period):
    df = add_indicators(df)
    bt = backtest(df)
    cards = analyze(df, bt)
    last = df.iloc[-1]
    close = float(last["Close"])
    day_chg = (close / df["Close"].iloc[-2] - 1) * 100
    trend = next((c for c in cards if c["title"] == "趨勢"), None)
    market = MARKET[ticker[len(code):]]

    record = {
        "code": code, "name": name, "ticker": ticker, "market": market, "period": period,
        "updated": now_tw(),
        "close": round(close, 2), "day_chg": round(day_chg, 2),
        "period_chg": round((close / df["Close"].iloc[0] - 1) * 100, 2),
        "high": round(float(df["High"].max()), 2), "low": round(float(df["Low"].min()), 2),
        "cards": cards,
        "d": {
            "dates": [x.strftime("%Y-%m-%d") for x in df.index],
            "open": clean(df["Open"]), "high": clean(df["High"]),
            "low": clean(df["Low"]), "close": clean(df["Close"]),
            "ma5": clean(df["MA5"]), "ma20": clean(df["MA20"]), "ma60": clean(df["MA60"]),
            "rsi": clean(df["RSI"], 1), "k": clean(df["K"], 1), "d": clean(df["D"], 1),
            "lots": clean(df["Lots"], 0),
            "up": [1 if r >= 0 else 0 for r in df["Return"].fillna(0)],
        },
        "bt": bt,
    }
    # 搜尋清單用的摘要：[代號, 名稱, 市場, 收盤, 漲跌%, 趨勢, 趨勢色]
    summary = [code, name, market, round(close, 2), round(day_chg, 2),
               trend["value"] if trend else "", trend["tone"] if trend else "neutral"]
    return record, summary, df.index[-1].strftime("%Y-%m-%d")


# ============================================================
# 7. 本機預覽（網頁要用 http 才能讀 JSON）
# ============================================================
def serve_locally():
    import functools
    import http.server
    import threading

    handler = functools.partial(http.server.SimpleHTTPRequestHandler, directory=str(SITE))
    handler.log_message = lambda *a, **k: None
    for port in range(8000, 8020):
        try:
            server = http.server.ThreadingHTTPServer(("127.0.0.1", port), handler)
            break
        except OSError:
            continue
    else:
        print("找不到可用的連接埠，請手動開啟 site/index.html。")
        return
    url = f"http://127.0.0.1:{port}/"
    print(f"\n本機預覽：{url}（按 Ctrl+C 結束）")
    threading.Timer(0.5, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("已結束。")


# ============================================================
def main():
    p = argparse.ArgumentParser(description="台股新手學習站產生器")
    p.add_argument("codes", nargs="*", help="股票代號，不填就跑全部台股")
    p.add_argument("--list", action="store_true", help="只跑 stocks.txt 裡的自選股")
    p.add_argument("--period", default="1y", help="期間：6mo / 1y / 2y / 5y（預設 1y）")
    p.add_argument("--no-open", action="store_true", help="產生後不要開瀏覽器")
    args = p.parse_args()

    watch = read_stock_list(HERE / "stocks.txt")
    watch_names = dict(watch)

    if args.codes:
        universe = {c.upper(): (watch_names.get(c.upper(), ""), None) for c in args.codes}
    elif args.list:
        universe = {c: (n, None) for c, n in watch}
    else:
        universe = get_all_stocks()
        if not universe:
            notice("無法取得全部台股清單，這次只更新 stocks.txt 裡的股票。")
        for c, n in watch:  # 自選股一定要包含在內
            universe.setdefault(c, (n, None))
    if not universe:
        sys.exit("沒有要分析的股票。")

    # 不知道是上市還是上櫃的，兩個都試
    tickers = []
    for code, (_, suffix) in universe.items():
        tickers += [code + suffix] if suffix else [code + ".TW", code + ".TWO"]
    print(f"下載 {len(universe)} 檔股票的股價…")
    prices = download_prices(tickers, args.period)

    # 準備輸出資料夾
    if SITE.exists():
        shutil.rmtree(SITE)
    (SITE / "data").mkdir(parents=True)
    for f in ("index.html", "report.html"):
        shutil.copy(WEB / f, SITE / f)

    rows, dates, failed = [], set(), []
    for code, (name, suffix) in universe.items():
        candidates = [code + suffix] if suffix else [code + ".TW", code + ".TWO"]
        ticker = next((t for t in candidates if t in prices), None)
        if not ticker:
            failed.append(code)
            continue
        try:
            record, summary, last_date = build_record(code, name or code, ticker, prices[ticker], args.period)
        except Exception as e:
            print(f"  {code} 分析失敗：{e}")
            failed.append(code)
            continue
        (SITE / "data" / f"{code}.json").write_text(
            json.dumps(record, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
        rows.append(summary)
        dates.add(last_date)

    rows.sort(key=lambda r: r[0])
    listing = {
        "updated": now_tw(), "date": max(dates) if dates else "",
        "watch": [c for c, _ in watch], "stocks": rows,
    }
    (SITE / "list.json").write_text(
        json.dumps(listing, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")

    notice(f"成功 {len(rows)} 檔、沒有資料 {len(failed)} 檔（資料日期 {listing['date']}）")
    if failed:
        print("沒有資料的代號（多半是新上市或暫停交易）：" + "、".join(failed[:50])
              + (" …" if len(failed) > 50 else ""))
    if not rows:
        sys.exit(1)  # 全部失敗就回報錯誤，GitHub 會寄信通知你
    if not args.no_open and not IN_CI:
        serve_locally()


if __name__ == "__main__":
    main()
