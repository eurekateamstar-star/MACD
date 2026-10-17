"""
台股新手學習站產生器（全部台股 + 搜尋版）
-------------------------------------------------
Python 負責：
  1. 取得全部上市、上櫃股票與 ETF 清單（證交所 / 櫃買中心）
  2. 批次下載股價（Yahoo Finance）
  3. 計算指標（均線、RSI、KDJ、MACD、量能）、白話解釋、簡單回測
  4. 注意股、處置股、可能處置名單、重大訊息、股東會（證交所 / 櫃買中心官方資料）
  5. 每檔存成 site/data/代號.json，並產生 site/list.json（搜尋用）

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
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd
import yfinance as yf

import fundamentals

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
def http_get(url: str, timeout: int = 30, retries: int = 3) -> bytes:
    """官方網站偶爾會連線失敗或回傳空白，失敗就等一下再試。"""
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
    n_tw = sum(1 for _, s in stocks.values() if s == ".TW")
    n_two = len(stocks) - n_tw
    # 上市正常約 1300 檔、上櫃約 1000 檔；任一邊明顯不足就用備援來源補齊
    if n_tw < 800 or n_two < 500:
        print(f"  清單不完整（上市 {n_tw}、上櫃 {n_two}），改用備援來源補齊…")
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
    df["MA10"] = df["Close"].rolling(10).mean()   # 雙週線
    df["MA20"] = df["Close"].rolling(20).mean()   # 月線
    std20 = df["Close"].rolling(20).std()
    df["BB_UP"] = df["MA20"] + 2 * std20           # 布林通道上緣
    df["BB_LOW"] = df["MA20"] - 2 * std20          # 布林通道下緣
    df["MA60"] = df["Close"].rolling(60).mean()   # 季線
    df["MA240"] = df["Close"].rolling(240).mean() # 年線

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
    df["J"] = 3 * df["K"] - 2 * df["D"]          # KDJ 的 J 線：比 K 更敏感

    # MACD(12,26,9)：快慢兩條指數均線的差距，看趨勢的動能
    ema12 = df["Close"].ewm(span=12, adjust=False).mean()
    ema26 = df["Close"].ewm(span=26, adjust=False).mean()
    df["DIF"] = ema12 - ema26                     # 快線
    df["DEA"] = df["DIF"].ewm(span=9, adjust=False).mean()   # 慢線（台灣常稱 MACD 線）
    df["OSC"] = df["DIF"] - df["DEA"]             # 柱狀體：正 = 紅柱、負 = 綠柱
    df.loc[df.index[:25], ["DIF", "DEA", "OSC"]] = float("nan")  # 前段資料不足，不顯示

    # 成交量（Yahoo 單位是「股」，1 張 = 1000 股）
    df["Lots"] = df["Volume"] / 1000
    df["LotsMA5"] = df["Lots"].rolling(5).mean()
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

    # --- MACD ---
    m = df[["DIF", "DEA", "OSC"]].tail(3)
    if m.notna().all().all():
        dif, dea, osc, osc_prev = last["DIF"], last["DEA"], last["OSC"], m["OSC"].iloc[-2]
        golden = any(m["DIF"].iloc[i] > m["DEA"].iloc[i] and m["DIF"].iloc[i - 1] <= m["DEA"].iloc[i - 1] for i in (1, 2))
        death = any(m["DIF"].iloc[i] < m["DEA"].iloc[i] and m["DIF"].iloc[i - 1] >= m["DEA"].iloc[i - 1] for i in (1, 2))
        if golden:
            tone, text = "up", "最近 DIF 由下往上穿過 MACD 線，稱為「黃金交叉」，代表上漲動能轉強。"
        elif death:
            tone, text = "down", "最近 DIF 由上往下穿過 MACD 線，稱為「死亡交叉」，代表上漲動能轉弱。"
        else:
            zone = ("DIF 和 MACD 都在 0 軸之上，屬於多方格局。" if dif > 0 and dea > 0 else
                    "DIF 和 MACD 都在 0 軸之下，屬於空方格局。" if dif < 0 and dea < 0 else
                    "DIF 和 MACD 在 0 軸附近，方向還不明確。")
            if osc > 0:
                bar = "紅柱變長，多方力道增強。" if osc > osc_prev else "紅柱縮短，漲勢的力道在減弱。"
            else:
                bar = "綠柱變長，空方力道增強。" if osc < osc_prev else "綠柱縮短，跌勢的力道在減弱。"
            tone = "up" if dif > 0 and dea > 0 else "down" if dif < 0 and dea < 0 else "neutral"
            text = zone + bar
        cards.append(dict(title="MACD(12,26,9)", value=f"DIF {dif:.2f} / MACD {dea:.2f}", tone=tone, text=text))

    # --- KDJ ---
    recent = df[["K", "D", "J"]].tail(3)
    if recent.notna().all().all():
        k, d, j = last["K"], last["D"], last["J"]
        golden = any(recent["K"].iloc[i] > recent["D"].iloc[i] and
                     recent["K"].iloc[i - 1] <= recent["D"].iloc[i - 1] for i in (1, 2))
        death = any(recent["K"].iloc[i] < recent["D"].iloc[i] and
                    recent["K"].iloc[i - 1] >= recent["D"].iloc[i - 1] for i in (1, 2))
        if golden:
            tone, text = "up", "最近 K 線由下往上穿過 D 線，稱為「黃金交叉」，短線轉強的訊號。"
        elif death:
            tone, text = "down", "最近 K 線由上往下穿過 D 線，稱為「死亡交叉」，短線轉弱的訊號。"
        elif j > 100:
            tone, text = "warn", "J 值超過 100，短線漲得太急，容易拉回。"
        elif j < 0:
            tone, text = "warn", "J 值低於 0，短線跌得太急，可能出現反彈。"
        elif k >= 80:
            tone, text = "warn", "K 值在 80 以上屬於高檔區，短線偏熱。"
        elif k <= 20:
            tone, text = "warn", "K 值在 20 以下屬於低檔區，短線偏冷。"
        else:
            tone, text = "neutral", "K、D 在 20~80 之間，J 也沒有過熱或過冷，沒有明顯訊號。"
        cards.append(dict(title="KDJ(9,3,3)", value=f"K {k:.0f} / D {d:.0f} / J {j:.0f}", tone=tone, text=text))

    # --- 量能 ---
    if ok(last["LotsMA5"]) and last["LotsMA5"] > 0:
        lots, ma5, ma20 = last["Lots"], last["LotsMA5"], last["LotsMA20"]
        r5 = lots / ma5
        chg = close - prev["Close"]
        if chg > 0 and r5 >= 1.2:
            tone, text = "up", "價漲量增：股價上漲而且量比平常大，代表買盤積極，屬於比較健康的上漲。"
        elif chg > 0 and r5 <= 0.8:
            tone, text = "warn", "價漲量縮：股價上漲但量變少，追價的人不多，要留意漲勢能不能延續。"
        elif chg < 0 and r5 >= 1.2:
            tone, text = "down", "價跌量增：股價下跌而且量變大，代表賣壓沉重，要小心。"
        elif chg < 0 and r5 <= 0.8:
            tone, text = "neutral", "價跌量縮：股價下跌但量變少，賣壓減輕，跌勢可能趨緩。"
        else:
            tone, text = "neutral", "今天的量和最近 5 天差不多，沒有明顯的價量訊號。"
        if ok(ma20) and ma20 > 0:
            diff = (ma5 / ma20 - 1) * 100
            if diff >= 30:
                text += f"另外，近 5 日均量比 20 日均量多 {diff:.0f}%，市場關注度明顯提高。"
            elif diff <= -30:
                text += f"另外，近 5 日均量比 20 日均量少 {-diff:.0f}%，交易變得清淡。"
        cards.append(dict(title="量能", value=f"{lots:,.0f} 張（5日均量 {r5:.1f} 倍）", tone=tone, text=text))

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


# 智慧選股用的欄位（順序要和 screen_row() 一致；網頁 screen.html 依名稱讀取）
BASE_COLS = [
    # 基本
    "code", "name", "market", "ind", "flag",
    "close", "chg", "ret5", "ret20", "amp",
    # 量
    "vol", "vr", "vma20", "amt",
    # 股本
    "cap", "mcap",
    # MACD / KD / RSI
    "dif", "dea", "osc", "macd_x", "osc_up",
    "k", "d", "j", "kd_x", "rsi", "wk_k", "wk_d", "wk_kd_x",
    # 均線
    "ma", "above20", "above60", "ma_x520", "ma_x2060", "ma_tight", "ma_break",
    # 新高新低
    "nh20", "nh60", "nh240", "nh_days", "nh_streak",
    "nl20", "nl60", "nl240", "nl_days", "nl_streak", "dist_hi",
    # 型態
    "box_break", "gap_up", "gap_down", "bb_up", "bb_down", "bb_width", "long_red", "long_black", "limit_up", "engulf",
    # 籌碼
    "f_net", "t_net", "d_net", "i_net", "f_5", "t_5", "f_streak", "t_streak", "d_streak", "i_ratio",
    "mg_bal", "mg_chg", "mg_streak", "sh_bal", "ss_ratio",
    "big1000", "chg1000", "chg_people",
    # 基本面
    "pe", "yield", "pb",
    "rev_yoy", "rev_mom", "rev_cum_yoy", "rev_hi12", "rev_hi24", "rev_yoy_streak",
    "eps", "gm", "opm", "roe",
]
EXTRA_COLS = [
    # 長期趨勢、乖離
    "above240", "ma240_up", "bias20", "bias60",
    # 相對強度、產業
    "rs", "rel3m", "ind_ret20", "ind_rank",
    # 體質
    "debt", "cur", "bvps", "peg",
    # 風險
    "dt_ratio", "altered", "mine",
    # 評分
    "s_tech", "s_chip", "s_fund", "s_risk", "s_total", "magic",
    # 社群熱度
    "ptt_posts", "ptt_push",
]
SCREEN_COLS = BASE_COLS + EXTRA_COLS


def _cross(a: pd.Series, b: pd.Series, days: int = 3) -> int:
    """最近幾天內 a 由下往上穿過 b → 1，由上往下 → -1，沒有 → 0"""
    x = (a - b).tail(days + 1)
    if x.isna().any() or len(x) < 2:
        return 0
    for i in range(len(x) - 1, 0, -1):
        if x.iloc[i] > 0 >= x.iloc[i - 1]:
            return 1
        if x.iloc[i] < 0 <= x.iloc[i - 1]:
            return -1
    return 0


def _streak(flags: pd.Series) -> int:
    n = 0
    for v in reversed(flags.tolist()):
        if v:
            n += 1
        else:
            break
    return n


def signals(df: pd.DataFrame) -> dict:
    """每天是否出現某個技術訊號（True / False 的時間序列）。選股看最後一天，回測看全部。"""
    c, o, h, l = df["Close"], df["Open"], df["High"], df["Low"]
    pc = c.shift(1)
    chg = (c / pc - 1) * 100
    hi = lambda w: c >= c.rolling(w, min_periods=w).max()
    lo = lambda w: c <= c.rolling(w, min_periods=w).min()
    up_x = lambda a, b: (a > b) & (a.shift(1) <= b.shift(1))
    ma3 = df[["MA5", "MA10", "MA20"]]
    spread = (ma3.max(axis=1) / ma3.min(axis=1) - 1) * 100            # 三條均線的距離（%）
    prev_high20 = h.shift(1).rolling(20).max()
    prev_low20 = l.shift(1).rolling(20).min()
    s = {
        "chg": chg,
        "hi20": hi(20), "hi60": hi(60), "hi240": hi(240),
        "lo20": lo(20), "lo60": lo(60), "lo240": lo(240),
        "macd_gx": up_x(df["DIF"], df["DEA"]),
        "kd_gx": up_x(df["K"], df["D"]),
        "ma_x520": up_x(df["MA5"], df["MA20"]),
        "ma_x2060": up_x(df["MA20"], df["MA60"]),
        "ma_tight": spread <= 2,
        # 均線糾結突破：昨天三線距離在 3% 以內，今天收盤站上三條均線而且收紅
        "ma_break": (spread.shift(1) <= 3) & (c > ma3.max(axis=1)) & (pc <= ma3.max(axis=1).shift(1)) & (chg > 0),
        # 箱型突破：過去 20 天高低差在 15% 以內，今天收盤突破這 20 天的最高價
        "box_break": (prev_high20 / prev_low20 - 1 <= 0.15) & (c > prev_high20),
        "gap_up": l > h.shift(1),
        "gap_down": h < l.shift(1),
        "bb_up": c > df["BB_UP"],
        "bb_down": c < df["BB_LOW"],
        "long_red": ((c - o) / pc * 100 >= 4) & (chg >= 4),
        "long_black": ((o - c) / pc * 100 >= 4) & (chg <= -4),
        "limit_up": chg >= 9.5,
        "engulf": (pc < o.shift(1)) & (c > o) & (o <= pc) & (c >= o.shift(1)),
        "vol_burst": (df["Lots"] / df["LotsMA5"] >= 2) & (chg >= 3) & (df["Lots"] >= 1000),
    }
    s["nh_days"] = s["hi60"].rolling(10).sum()
    s["nl_days"] = s["lo60"].rolling(10).sum()
    return s


def weekly_kd(df: pd.DataFrame):
    """把日線合成週線，再算週 KD。"""
    w = df.resample("W-FRI").agg({"High": "max", "Low": "min", "Close": "last"}).dropna()
    if len(w) < 12:
        return None, None, 0
    low9, high9 = w["Low"].rolling(9).min(), w["High"].rolling(9).max()
    rsv = (w["Close"] - low9) / (high9 - low9) * 100
    k = rsv.ewm(com=2, adjust=False).mean()
    d = k.ewm(com=2, adjust=False).mean()
    return k.iloc[-1], d.iloc[-1], _cross(k, d, 1)


# 一鍵策略的回測：在過去的每一天，如果出現這個訊號就「假裝買進」，看 5 天、20 天後的漲跌
BACKTESTS = {
    "持續創新高": lambda s, df: s["hi60"] & (s["nh_days"] >= 3),
    "創一年新高": lambda s, df: s["hi240"],
    "破底": lambda s, df: s["lo60"],
    "持續破底": lambda s, df: s["lo60"] & (s["nl_days"] >= 3),
    "MACD 黃金交叉": lambda s, df: s["macd_gx"],
    "MACD 紅柱放大": lambda s, df: (df["DIF"] >= 0) & (df["OSC"] >= 0) & (df["OSC"] > df["OSC"].shift(1)),
    "KD 低檔黃金交叉": lambda s, df: s["kd_gx"] & (df["K"] <= 35),
    "爆量長紅": lambda s, df: s["vol_burst"],
    "多頭排列": lambda s, df: (df["MA5"] > df["MA20"]) & (df["MA20"] > df["MA60"]) & (df["Close"] > df["MA20"]),
    "RSI 超賣": lambda s, df: df["RSI"] <= 30,
    "均線糾結突破": lambda s, df: s["ma_break"],
    "箱型突破": lambda s, df: s["box_break"],
    "跳空上漲": lambda s, df: s["gap_up"],
    "突破布林上緣": lambda s, df: s["bb_up"],
    "月線黃金交叉": lambda s, df: s["ma_x520"],
}


class BacktestStats:
    def __init__(self):
        self.acc = {k: [0, 0.0, 0, 0.0, 0] for k in list(BACKTESTS) + ["_base"]}   # n, sum5, win5, sum20, win20

    def add(self, df: pd.DataFrame, s: dict):
        c = df["Close"]
        f5 = (c.shift(-5) / c - 1) * 100
        f20 = (c.shift(-20) / c - 1) * 100
        valid = f20.notna() & (df["Lots"] >= 100)          # 太冷門（每天不到 100 張）的不算
        for name, rule in list(BACKTESTS.items()) + [("_base", lambda s, df: pd.Series(True, index=df.index))]:
            try:
                m = rule(s, df).fillna(False).astype(bool) & valid
            except Exception:
                continue
            if m.any():
                a = self.acc[name]
                a[0] += int(m.sum())
                a[1] += float(f5[m].sum()); a[2] += int((f5[m] > 0).sum())
                a[3] += float(f20[m].sum()); a[4] += int((f20[m] > 0).sum())

    def result(self) -> dict:
        out = {}
        for k, (n, s5, w5, s20, w20) in self.acc.items():
            if n:
                out[k] = {"n": n, "avg5": round(s5 / n, 2), "win5": round(w5 / n * 100, 1),
                          "avg20": round(s20 / n, 2), "win20": round(w20 / n * 100, 1)}
        return out


def screen_row(code, name, market, df, flag, profile, s, extra):
    """extra：籌碼面與基本面（inst, margin, val, rev, fin, holders）"""
    c = df["Close"]
    last = df.iloc[-1]
    close = float(last["Close"])
    n = len(c)

    def r(v, nd=2):
        return None if v is None or pd.isna(v) else round(float(v), nd)

    def b(key):
        v = s[key].iloc[-1]
        return None if pd.isna(v) else int(bool(v))

    w = min(n, 240)
    ma = (1 if last["MA5"] > last["MA20"] > last["MA60"] else -1 if last["MA5"] < last["MA20"] < last["MA60"] else 0) \
        if ok(last["MA60"]) else None
    cap, shares = profile.get("cap"), profile.get("shares")
    osc_prev = df["OSC"].iloc[-2] if n >= 2 else None
    wk_k, wk_d, wk_x = weekly_kd(df)
    bb_w = (last["BB_UP"] - last["BB_LOW"]) / last["MA20"] * 100 if ok(last["BB_UP"]) and last["MA20"] else None

    # 籌碼
    inst = extra.get("inst") or {}
    def last_of(key):
        vals = [v for v in inst.get(key, []) if v is not None]
        return vals[-1] if vals else None
    def sum_last(key, k):
        vals = [v for v in inst.get(key, []) if v is not None][-k:]
        return sum(vals) if vals else None
    i_net = last_of("all")
    mg = extra.get("margin") or {}
    mg_vals = [v for v in mg.get("mg", []) if v is not None]
    sh_vals = [v for v in mg.get("sh", []) if v is not None]
    mg_diff = [b2 - a2 for a2, b2 in zip(mg_vals, mg_vals[1:])]
    val = extra.get("val") or {}
    rev = extra.get("rev") or {}
    fin = extra.get("fin") or {}
    hd = extra.get("holders") or {}

    base = [
        code, name, market, profile.get("ind", "") or ("ETF" if code.startswith("00") else ""), flag,
        r(close), r(s["chg"].iloc[-1]),
        r((close / c.iloc[-6] - 1) * 100) if n > 5 else None,
        r((close / c.iloc[-21] - 1) * 100) if n > 20 else None,
        r((last["High"] - last["Low"]) / c.iloc[-2] * 100) if n > 1 else None,     # 振幅
        r(last["Lots"], 0),
        r(last["Lots"] / last["LotsMA5"]) if ok(last["LotsMA5"]) and last["LotsMA5"] > 0 else None,
        r(last["LotsMA20"], 0),
        r(last["Lots"] * 1000 * close / 1e8),
        r(cap, 1), r(close * shares / 1e8, 0) if shares else None,
        r(last["DIF"], 3), r(last["DEA"], 3), r(last["OSC"], 3),
        _cross(df["DIF"], df["DEA"]),
        (1 if ok(last["OSC"]) and ok(osc_prev) and last["OSC"] > osc_prev else 0),
        r(last["K"], 1), r(last["D"], 1), r(last["J"], 1), _cross(df["K"], df["D"]), r(last["RSI"], 1),
        r(wk_k, 1), r(wk_d, 1), wk_x,
        ma,
        (1 if close > last["MA20"] else 0) if ok(last["MA20"]) else None,
        (1 if close > last["MA60"] else 0) if ok(last["MA60"]) else None,
        _cross(df["MA5"], df["MA20"]), _cross(df["MA20"], df["MA60"]),
        b("ma_tight"), b("ma_break"),
        b("hi20"), b("hi60"), b("hi240") if n >= 240 else None,
        int(s["nh_days"].iloc[-1] or 0), _streak(s["hi20"]),
        b("lo20"), b("lo60"), b("lo240") if n >= 240 else None,
        int(s["nl_days"].iloc[-1] or 0), _streak(s["lo20"]),
        r((close / c.tail(w).max() - 1) * 100, 1),
        b("box_break"), b("gap_up"), b("gap_down"), b("bb_up"), b("bb_down"), r(bb_w, 1),
        b("long_red"), b("long_black"), b("limit_up"), b("engulf"),
        last_of("f"), last_of("t"), last_of("d"), i_net, sum_last("f", 5), sum_last("t", 5),
        fundamentals.streak(inst.get("f", [])) if inst else None,
        fundamentals.streak(inst.get("t", [])) if inst else None,
        fundamentals.streak(inst.get("d", [])) if inst else None,
        r(i_net / last["Lots"] * 100, 1) if i_net is not None and last["Lots"] else None,
        mg_vals[-1] if mg_vals else None,
        mg_diff[-1] if mg_diff else None,
        fundamentals.streak(mg_diff) if mg_diff else None,
        sh_vals[-1] if sh_vals else None,
        r(sh_vals[-1] / mg_vals[-1] * 100, 1) if sh_vals and mg_vals and mg_vals[-1] else None,
        hd.get("big1000"), hd.get("chg1000"), hd.get("chg_people"),
        val.get("pe") or None, val.get("yield"), val.get("pb"),
        r(rev.get("yoy"), 1), r(rev.get("mom"), 1), r(rev.get("cum_yoy"), 1),
        rev.get("hi12"), rev.get("hi24"), rev.get("yoy_streak"),
        fin.get("eps"), fin.get("gm"), fin.get("opm"), fin.get("roe"),
    ]
    d = dict(zip(BASE_COLS, base))

    # ---- 長期趨勢、乖離 ----
    d["above240"] = (1 if close > last["MA240"] else 0) if ok(last["MA240"]) else None
    d["ma240_up"] = (1 if last["MA240"] > df["MA240"].iloc[-21] else 0) if n > 21 and ok(last["MA240"]) and ok(df["MA240"].iloc[-21]) else None
    d["bias20"] = r((close / last["MA20"] - 1) * 100, 1) if ok(last["MA20"]) else None
    d["bias60"] = r((close / last["MA60"] - 1) * 100, 1) if ok(last["MA60"]) else None

    # ---- 相對強度、產業 ----
    rsi_ = extra.get("rs") or {}
    d["rs"], d["rel3m"] = rsi_.get("rs"), r(rsi_.get("rel3m"), 1)
    d["ind_ret20"], d["ind_rank"] = r(rsi_.get("ind_ret20"), 1), rsi_.get("ind_rank")

    # ---- 體質 ----
    d["debt"], d["cur"], d["bvps"] = fin.get("debt"), fin.get("cur"), fin.get("bvps")
    pe, cum = d["pe"], d["rev_cum_yoy"]
    d["peg"] = r(pe / cum, 2) if pe and pe > 0 and cum and cum > 0 else None

    # ---- 風險、地雷 ----
    risk = extra.get("risk") or {}
    d["dt_ratio"] = r(risk["dt_shares"] / (last["Lots"] * 1000) * 100, 1) if risk.get("dt_shares") and last["Lots"] else None
    d["altered"] = 1 if risk.get("altered") or risk.get("managed") or risk.get("suspended") else 0
    is_fin = d["ind"] == "金融保險"
    etf = code.startswith("00")
    reasons = []
    if risk.get("altered"):
        reasons.append("變更交易（含全額交割、分盤交易）")
    if risk.get("managed"):
        reasons.append("管理股票")
    if risk.get("suspended"):
        reasons.append("停止交易")
    if flag == "d":
        reasons.append("處置中")
    if d["eps"] is not None and d["eps"] < 0:
        reasons.append("今年累計虧損")
    if not etf and d["bvps"] is not None and d["bvps"] < 10:
        reasons.append("每股淨值低於 10 元")
    if not is_fin and d["debt"] is not None and d["debt"] >= 70:
        reasons.append("負債比超過 70%")
    d["mine"] = 1 if reasons else 0

    # ---- 綜合評分（0～100）----
    clip = lambda v: max(0.0, min(100.0, v))
    tech = 0.4 * (d["rs"] if d["rs"] is not None else 50) + 15 * (d["above20"] or 0) + 15 * (d["above60"] or 0) \
        + 10 * (d["above240"] or 0) + 10 * (1 if d["ma"] == 1 else 0) + 10 * (1 if (d["dif"] or 0) > 0 else 0)
    chip = None
    if inst:
        i5 = sum_last("all", 5) or 0
        base_vol = (d["vma20"] or 0) * 5
        chip = 50 + (max(-35, min(35, i5 / base_vol * 200)) if base_vol else 0)
        chip += 5 * ((d["t_streak"] or 0) >= 2) + 5 * ((d["f_streak"] or 0) >= 2) - 5 * ((d["mg_streak"] or 0) >= 3)
        if d["chg1000"] is not None:
            chip += 5 if d["chg1000"] > 0 else -5 if d["chg1000"] < 0 else 0
        chip = clip(chip)
    subs = []
    if d["roe"] is not None: subs.append(clip(d["roe"] * 5))
    if d["gm"] is not None and not is_fin: subs.append(clip(d["gm"] * 2.5))
    if d["rev_yoy"] is not None: subs.append(clip(50 + d["rev_yoy"] * 1.25))
    if d["eps"] is not None: subs.append(100.0 if d["eps"] > 0 else 0.0)
    if pe and pe > 0: subs.append(clip(100 - (pe - 10) * 3))
    if d["yield"] is not None: subs.append(clip(d["yield"] * 16))
    fund = sum(subs) / len(subs) if len(subs) >= 3 else None
    vol60 = df["Return"].tail(60).std() * (252 ** 0.5) * 100
    risk_s = 100.0
    risk_s -= 50 if d["altered"] else 0
    risk_s -= {"d": 30, "c": 20, "a": 10}.get(flag, 0)
    risk_s -= 20 if d["eps"] is not None and d["eps"] < 0 else 0
    risk_s -= 15 if not is_fin and d["debt"] is not None and d["debt"] > 60 else 0
    risk_s -= 10 if not is_fin and d["cur"] is not None and d["cur"] < 100 else 0
    risk_s -= 15 if ok(vol60) and vol60 > 60 else 0
    risk_s -= 15 if d["dt_ratio"] is not None and d["dt_ratio"] > 50 else 0
    risk_s -= 15 if (d["vma20"] or 0) < 100 else 0
    risk_s = clip(risk_s)
    parts = [x for x in (tech, chip, fund, risk_s) if x is not None]
    d["s_tech"], d["s_chip"], d["s_fund"], d["s_risk"] = [None if x is None else round(x) for x in (tech, chip, fund, risk_s)]
    d["s_total"] = round(sum(parts) / len(parts)) if parts else None
    d["magic"] = None          # 神奇公式排名：所有股票算完後再填
    pt = extra.get("ptt") or {}
    d["ptt_posts"], d["ptt_push"] = pt.get("posts", 0), pt.get("push", 0)
    d["_reasons"] = reasons
    return d


CHART_DAYS = 250   # 圖表與報告只顯示最近一年（約 250 個交易日）


def build_record(code, name, ticker, df, period, info=None, stats=None):
    info = info or {}
    full = add_indicators(df)              # 用完整資料（約兩年）計算，指標和新高新低比較準
    sig = signals(full)
    if stats is not None and not code.startswith("00"):
        stats.add(full, sig)
    df = full.tail(CHART_DAYS)
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
            "j": clean(df["J"], 1),
            "dif": clean(df["DIF"], 3), "dea": clean(df["DEA"], 3), "osc": clean(df["OSC"], 3),
            "lots": clean(df["Lots"], 0), "vma5": clean(df["LotsMA5"], 0), "vma20": clean(df["LotsMA20"], 0),
            "up": [1 if r >= 0 else 0 for r in df["Return"].fillna(0)],
        },
        "bt": bt,
        "alert": info.get("alert"),
        "meetings": info.get("meetings", []),
        "news": info.get("news", []),
        "media": info.get("media") or [],
        "media_checked": bool(info.get("media_checked")),
        "ptt": info.get("ptt"),
    }
    # 搜尋清單用的摘要：[代號, 名稱, 市場, 收盤, 漲跌%, 趨勢, 趨勢色, 警示]
    # 警示：d = 處置中、c = 可能處置、a = 今日注意
    alert = info.get("alert") or {}
    flag = {"disposed": "d", "candidate": "c", "attention": "a"}.get(alert.get("level"), "")
    summary = [code, name, market, round(close, 2), round(day_chg, 2),
               trend["value"] if trend else "", trend["tone"] if trend else "neutral", flag]
    extra = info.get("extra") or {}
    sd = screen_row(code, name, market, full, flag, info.get("profile", {}), sig, extra)
    srow = [sd.get(k) for k in SCREEN_COLS]
    record["score"] = {k: sd.get(k) for k in ("s_tech", "s_chip", "s_fund", "s_risk", "s_total", "rs", "rel3m",
                                               "ind_ret20", "ind_rank", "debt", "cur", "peg", "dt_ratio", "bias20", "above240", "ma240_up")}
    record["score"]["mine"] = sd["_reasons"]
    profile = info.get("profile") or {}
    if profile.get("cap"):
        record["cap"] = round(profile["cap"], 1)
        record["ind"] = profile.get("ind", "")
    # 籌碼面、基本面（報告頁顯示用）
    row = dict(zip(SCREEN_COLS, srow))
    record["chip"] = {
        "inst": extra.get("inst"), "margin": extra.get("margin"),
        "holders": extra.get("holders"),
        "sum": {k: row[k] for k in ("f_net", "t_net", "d_net", "i_net", "f_5", "t_5", "f_streak", "t_streak", "d_streak",
                                    "i_ratio", "mg_bal", "mg_chg", "mg_streak", "sh_bal", "ss_ratio")},
    }
    record["fund"] = {
        "val": extra.get("val"), "fin": extra.get("fin"),
        "rev": extra.get("rev"),
    }
    return record, summary, df.index[-1].strftime("%Y-%m-%d"), srow


# ============================================================
# 7. 市場資訊：注意股、處置股、可能處置、重大訊息、股東會
#    資料來源：證交所（上市）、櫃買中心（上櫃）官方公開資料
# ============================================================
STATE = HERE / "state"
TWSE_OPEN = "https://openapi.twse.com.tw/v1"
TPEX_OPEN = "https://www.tpex.org.tw/openapi/v1"


def get_json(url: str, timeout: int = 40):
    return json.loads(http_get(url, timeout))


def roc_to_iso(s) -> str:
    """民國日期（1151016、115/10/16、115.10.16）→ 2026-10-16"""
    m = re.search(r"(\d{2,3})\D?(\d{1,2})\D?(\d{1,2})", str(s or ""))
    if not m:
        return ""
    return f"{int(m.group(1)) + 1911:04d}-{int(m.group(2)):02d}-{int(m.group(3)):02d}"


def strip_tags(s) -> str:
    s = re.sub(r"<br\s*/?>", "\n", str(s or ""))
    return re.sub(r"<[^>]+>", "", s).replace("\r", "").strip()


def norm(row: dict) -> dict:
    return {str(k).strip(): v for k, v in row.items()}


def table_rows(fields, data):
    """把 [欄位名稱], [[值...]] 轉成 [{欄位: 值}]"""
    fields = [str(f).strip() for f in fields]
    return [dict(zip(fields, r)) for r in data]


def fetch_attention(start, end) -> dict:
    """一段期間內的注意股紀錄 → {代號: {日期: 原因}}"""
    out = {}

    def add(code, date, text):
        code = str(code).strip().upper()
        if CODE_RE.match(code) and date:
            out.setdefault(code, {})[date] = strip_tags(text)

    try:
        d = get_json(f"https://www.twse.com.tw/rwd/zh/announcement/notice?querytype=1"
                     f"&startDate={start:%Y%m%d}&endDate={end:%Y%m%d}&response=json", 60)
        rows = table_rows(d.get("fields", []), d.get("data", []))
        for r in rows:
            add(r.get("證券代號"), roc_to_iso(r.get("日期")), r.get("注意交易資訊"))
        print(f"  上市注意紀錄：{len(rows)} 筆")
    except Exception as e:
        print(f"  無法取得上市注意紀錄：{e}")
    try:
        d = get_json(f"https://www.tpex.org.tw/www/zh-tw/bulletin/attention?startDate={start:%Y/%m/%d}"
                     f"&endDate={end:%Y/%m/%d}&response=json", 60)
        t = d["tables"][0]
        rows = table_rows(t.get("fields", []), t.get("data", []))
        for r in rows:
            add(r.get("證券代號"), roc_to_iso(r.get("公告日期")), r.get("注意交易資訊"))
        print(f"  上櫃注意紀錄：{len(rows)} 筆")
    except Exception as e:
        print(f"  無法取得上櫃注意紀錄：{e}")
    return out


def _period(s):
    parts = re.split(r"[～~]", str(s or ""))
    return (roc_to_iso(parts[0]), roc_to_iso(parts[-1])) if parts else ("", "")


def fetch_disposition(today: str) -> dict:
    """目前（或即將開始）處置中的股票 → {代號: {...}}"""
    out = {}
    try:
        for r in map(norm, get_json(f"{TWSE_OPEN}/announcement/punish")):
            start, end = _period(r.get("DispositionPeriod"))
            code = str(r.get("Code", "")).strip()
            if CODE_RE.match(code) and end >= today:
                out[code] = {"start": start, "end": end, "reason": r.get("ReasonsOfDisposition", ""),
                             "measure": r.get("DispositionMeasures", ""), "detail": strip_tags(r.get("Detail", ""))}
    except Exception as e:
        print(f"  無法取得上市處置股：{e}")
    try:
        for r in map(norm, get_json(f"{TPEX_OPEN}/tpex_disposal_information")):
            start, end = _period(r.get("DispositionPeriod"))
            code = str(r.get("SecuritiesCompanyCode", "")).strip()
            if CODE_RE.match(code) and end >= today:
                detail = strip_tags(r.get("DisposalCondition", "")).replace("* ", "\n").strip()
                out[code] = {"start": start, "end": end, "reason": r.get("DispositionReasons", ""),
                             "measure": "", "detail": detail}
    except Exception as e:
        print(f"  無法取得上櫃處置股：{e}")
    print(f"  處置中：{len(out)} 檔")
    return out


def fetch_candidates() -> dict:
    """官方公布「注意累計次數可能達處置標準」→ {代號: 說明}"""
    out = {}
    for url, ck, tk in ((f"{TWSE_OPEN}/announcement/notetrans", "Code", "RecentlyMetAttentionSecuritiesCriteria"),
                        (f"{TPEX_OPEN}/tpex_trading_warning_note", "SecuritiesCompanyCode", "AccumulationSituation")):
        try:
            for r in map(norm, get_json(url)):
                code = str(r.get(ck, "")).strip()
                if CODE_RE.match(code):
                    out[code] = str(r.get(tk, "")).strip()
        except Exception as e:
            print(f"  無法取得可能處置名單（{url}）：{e}")
    print(f"  可能達處置標準：{len(out)} 檔")
    return out


def fetch_news() -> dict:
    """每日重大訊息（證交所、櫃買中心每天公布前一天的）→ {代號: [訊息]}"""
    out = {}
    for url, ck in ((f"{TWSE_OPEN}/opendata/t187ap04_L", "公司代號"),
                    (f"{TPEX_OPEN}/mopsfin_t187ap04_O", "SecuritiesCompanyCode")):
        try:
            rows = list(map(norm, get_json(url)))
            for r in rows:
                code = str(r.get(ck, "")).strip()
                if not CODE_RE.match(code):
                    continue
                t = str(r.get("發言時間", "")).zfill(6)
                out.setdefault(code, []).append({
                    "d": roc_to_iso(r.get("發言日期")), "t": f"{t[:2]}:{t[2:4]}",
                    "s": re.sub(r"\s*\n\s*", "", str(r.get("主旨", ""))).strip(),
                    "c": str(r.get("符合條款", "")).strip(),
                    "x": str(r.get("說明", "")).replace("\r", "").strip()[:2000],
                })
            print(f"  重大訊息（{'上市' if 'twse' in url else '上櫃'}）：{len(rows)} 則")
        except Exception as e:
            print(f"  無法取得重大訊息（{url}）：{e}")
    return out


def merge_news(new: dict, today: str) -> dict:
    """重大訊息官方只給當天的，所以每天累積存在 state/news.json（保留 90 天，每檔最多 30 則）"""
    path = STATE / "news.json"
    try:
        old = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        old = {}
    cutoff = (datetime.fromisoformat(today) - timedelta(days=90)).strftime("%Y-%m-%d")
    merged = {}
    for code in set(old) | set(new):
        seen, items = set(), []
        for it in new.get(code, []) + old.get(code, []):
            key = (it.get("d"), it.get("t"), it.get("s"))
            if key in seen or (it.get("d") or "") < cutoff:
                continue
            seen.add(key)
            items.append(it)
        items.sort(key=lambda it: (it.get("d", ""), it.get("t", "")), reverse=True)
        if items:
            merged[code] = items[:30]
    STATE.mkdir(exist_ok=True)
    path.write_text(json.dumps(merged, ensure_ascii=False, separators=(",", ":"), sort_keys=True), encoding="utf-8")
    return merged


def fetch_meetings() -> dict:
    """股東會：日期、地點、是否改選董監、電子投票，以及停止過戶期間 → {代號: [會議]}"""
    out, closure = {}, {}
    for url in (f"{TWSE_OPEN}/opendata/t187ap38_L", f"{TPEX_OPEN}/mopsfin_t187ap38_O"):
        try:
            for r in map(norm, get_json(url)):
                code = str(r.get("公司代號", r.get("SecuritiesCompanyCode", ""))).strip()
                date = roc_to_iso(r.get("股東常(臨時)會日期-日期"))
                if code and date:
                    closure[(code, date)] = (roc_to_iso(r.get("停止過戶起訖日期-起")),
                                             roc_to_iso(r.get("停止過戶起訖日期-訖")))
        except Exception as e:
            print(f"  停止過戶資料無法取得（{url}）：{e}")
    for url in (f"{TWSE_OPEN}/opendata/t187ap41_L", f"{TPEX_OPEN}/t187ap41_O"):
        try:
            rows = list(map(norm, get_json(url)))
            for r in rows:
                code = str(r.get("公司代號", "")).strip()
                date = roc_to_iso(r.get("開會日期"))
                if not (CODE_RE.match(code) and date):
                    continue
                c = closure.get((code, date), ("", ""))
                out.setdefault(code, []).append({
                    "type": str(r.get("股東常(臨時)會", "")).strip(), "date": date,
                    "place": str(r.get("開會地點", "")).strip(),
                    "reelect": str(r.get("是否改選董監", "")).strip(),
                    "evote": str(r.get("是否採電子投票", "")).strip(),
                    "agent": str(r.get("股務單位", "")).strip(),
                    "agent_tel": str(r.get("股務單位電話", "")).strip(),
                    "closure": [c[0], c[1]] if c[0] else [],
                })
            print(f"  股東會資料（{'上市' if 'twse' in url else '上櫃'}）：{len(rows)} 筆")
        except Exception as e:
            print(f"  無法取得股東會資料（{url}）：{e}")
    for code in out:
        uniq = {(m["type"], m["date"]): m for m in out[code]}
        out[code] = sorted(uniq.values(), key=lambda m: m["date"], reverse=True)[:4]
    return out


def build_alert(code, att, disp, cand, tdays):
    """整理一檔股票的注意 / 處置狀態，並估算離處置還有多遠。"""
    days = att.get(code, {})
    if not days and code not in disp and code not in cand:
        return None
    win10, win30 = tdays[-10:], tdays[-30:]
    c10 = sum(d in days for d in win10)
    c30 = sum(d in days for d in win30)
    streak = 0
    for d in reversed(tdays):
        if d in days:
            streak += 1
        else:
            break
    latest = tdays[-1] if tdays else ""
    if code in disp:
        level = "disposed"
    elif code in cand:
        level = "candidate"
    elif latest in days:
        level = "attention"
    else:
        level = "recent"
    # 估算：依「連續 3 個營業日」「10 個營業日內 6 個營業日」兩個常見條件，看還差幾次
    need = [n for n in ((3 - streak) if streak else None, (6 - c10) if c10 else None) if n is not None and n > 0]
    return {
        "level": level, "streak": streak, "c10": c10, "c30": c30,
        "need": min(need) if need else None,
        "today": latest in days,
        "records": [{"d": d, "x": days[d]} for d in sorted(days, reverse=True)[:10]],
        "disp": disp.get(code), "cand": cand.get(code),
    }


INDUSTRY = {
    "01": "水泥", "02": "食品", "03": "塑膠", "04": "紡織纖維", "05": "電機機械", "06": "電器電纜",
    "08": "玻璃陶瓷", "09": "造紙", "10": "鋼鐵", "11": "橡膠", "12": "汽車", "14": "建材營造",
    "15": "航運", "16": "觀光餐旅", "17": "金融保險", "18": "貿易百貨", "20": "其他", "21": "化學",
    "22": "生技醫療", "23": "油電燃氣", "24": "半導體", "25": "電腦及週邊", "26": "光電",
    "27": "通信網路", "28": "電子零組件", "29": "電子通路", "30": "資訊服務", "31": "其他電子",
    "32": "文化創意", "33": "農業科技", "34": "電子商務", "35": "綠能環保", "36": "數位雲端",
    "37": "運動休閒", "38": "居家生活", "91": "存託憑證",
}


def fetch_profiles() -> dict:
    """公司基本資料：股本（實收資本額）、已發行股數、產業別 → {代號: {...}}"""
    out = {}
    sources = ((f"{TWSE_OPEN}/opendata/t187ap03_L", "公司代號", "實收資本額", "已發行普通股數或TDR原股發行股數", "產業別"),
               (f"{TPEX_OPEN}/mopsfin_t187ap03_O", "SecuritiesCompanyCode", "Paidin.Capital.NTDollars", "IssueShares", "SecuritiesIndustryCode"))
    for url, ck, capk, shk, indk in sources:
        try:
            rows = list(map(norm, get_json(url, 60)))
            for r in rows:
                code = str(r.get(ck, "")).strip()
                if not CODE_RE.match(code):
                    continue
                try:
                    cap = float(str(r.get(capk, "")).replace(",", "")) / 1e8   # 億元
                except ValueError:
                    cap = None
                try:
                    shares = float(str(r.get(shk, "")).replace(",", ""))
                except ValueError:
                    shares = None
                ind = str(r.get(indk, "")).strip().zfill(2)
                out[code] = {"cap": cap, "shares": shares, "ind": INDUSTRY.get(ind, f"其他({ind})" if ind.strip("0") else "")}
            print(f"  公司基本資料（{'上市' if 'twse' in url else '上櫃'}）：{len(rows)} 筆")
        except Exception as e:
            print(f"  無法取得公司基本資料（{url}）：{e}")
    return out


def fetch_market_info(tdays: list, today: str):
    print("取得注意股、處置股、重大訊息、股東會資料…")
    end = datetime.now(TW).date()
    att = fetch_attention(end - timedelta(days=50), end)
    disp = fetch_disposition(today)
    cand = fetch_candidates()
    news = merge_news(fetch_news(), today)
    meetings = fetch_meetings()
    profiles = fetch_profiles()
    return att, disp, cand, news, meetings, profiles



# ============================================================
# 相對強度（RS）與產業強弱：要先看完全部股票才能排名
# ============================================================
def download_index(period: str):
    """加權指數（^TWII）收盤價，用來比較「是否強於大盤」"""
    try:
        data = yf.download("^TWII", period=period, auto_adjust=True, progress=False)
        if isinstance(data.columns, pd.MultiIndex):        # 新版 yfinance 的欄位有兩層
            lvl = next(i for i in range(data.columns.nlevels) if "Close" in data.columns.get_level_values(i))
            c = data.xs("Close", axis=1, level=lvl)
        else:
            c = data["Close"]
        if isinstance(c, pd.DataFrame):
            c = c.iloc[:, 0]
        c = c.dropna()
        if c.index.tz is not None:
            c.index = c.index.tz_localize(None)
        print(f"  加權指數：{len(c)} 天")
        return c
    except Exception as e:
        print(f"  無法取得加權指數：{e}")
        return None


def relative_strength(code_ticker: dict, prices: dict, profiles: dict, index_close) -> dict:
    """RS：參考美國 IBD 選股法，綜合近 3、6、9、12 個月漲幅（近 3 個月權重加倍），在全部股票中排名 1～99。"""
    def ret(c, k):
        return c.iloc[-1] / c.iloc[-1 - k] - 1 if len(c) > k and c.iloc[-1 - k] > 0 else None

    raw, ret20, rel = {}, {}, {}
    idx3 = ret(index_close, 63) if index_close is not None and len(index_close) > 63 else None
    for code, t in code_ticker.items():
        if t not in prices:
            continue
        c = prices[t]["Close"]
        parts = [(w, ret(c, k)) for w, k in ((0.4, 63), (0.2, 126), (0.2, 189), (0.2, 250))]
        parts = [(w, v) for w, v in parts if v is not None]
        if parts and not code.startswith("00"):
            raw[code] = sum(w * v for w, v in parts) / sum(w for w, _ in parts)
        r20 = ret(c, 20)
        if r20 is not None:
            ret20[code] = r20 * 100
        r3 = ret(c, 63)
        if r3 is not None and idx3 is not None:
            rel[code] = (r3 - idx3) * 100
    out = {code: {} for code in code_ticker}
    ranked = sorted(raw, key=raw.get)
    for i, code in enumerate(ranked):
        out[code]["rs"] = max(1, min(99, int(round((i + 1) / len(ranked) * 99))))
    for code, v in rel.items():
        out[code]["rel3m"] = v
    # 產業強弱：產業內股票近 20 日漲幅的中位數
    groups = {}
    for code, v in ret20.items():
        ind = (profiles.get(code) or {}).get("ind")
        if ind and not code.startswith("00"):
            groups.setdefault(ind, []).append(v)
    med = {g: sorted(v)[len(v) // 2] for g, v in groups.items() if len(v) >= 3}
    order = sorted(med, key=med.get, reverse=True)
    for code in out:
        ind = (profiles.get(code) or {}).get("ind")
        if ind in med:
            out[code]["ind_ret20"] = med[ind]
            out[code]["ind_rank"] = order.index(ind) + 1
    print(f"  RS 排名 {len(ranked)} 檔、產業 {len(order)} 個（最強：{'、'.join(order[:3])}）")
    return out


def magic_formula(screen_rows: list):
    """神奇公式（葛林布雷）：ROE 高（賺錢能力）＋ 本益比低（便宜），兩個排名相加，越前面越好。不含金融股與 ETF。"""
    C = {c: i for i, c in enumerate(SCREEN_COLS)}
    cand = [r for r in screen_rows if r[C["roe"]] and r[C["roe"]] > 0 and r[C["pe"]] and r[C["pe"]] > 0
            and r[C["ind"]] != "金融保險" and not r[C["code"]].startswith("00")]
    by_roe = sorted(cand, key=lambda r: -r[C["roe"]])
    by_ey = sorted(cand, key=lambda r: r[C["pe"]])
    rank = {id(r): 0 for r in cand}
    for i, r in enumerate(by_roe):
        rank[id(r)] += i
    for i, r in enumerate(by_ey):
        rank[id(r)] += i
    ordered = sorted(cand, key=lambda r: rank[id(r)])
    for i, r in enumerate(ordered):
        r[C["magic"]] = max(1, min(99, int(round((1 - i / len(ordered)) * 99))))
    return len(ordered)

# ============================================================
# 8. 本機預覽（網頁要用 http 才能讀 JSON）
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
    p.add_argument("--period", default="2y", help="下載期間（預設 2y；報告顯示最近一年，較長的資料用來算新高與回測）")
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
    for f in ("index.html", "report.html", "screen.html"):
        shutil.copy(WEB / f, SITE / f)

    # 交易日曆：用資料最完整的那一檔的日期
    ref = max(prices.values(), key=len) if prices else None
    tdays = [d.strftime("%Y-%m-%d") for d in ref.index] if ref is not None else []
    today = datetime.now(TW).strftime("%Y-%m-%d")
    att, disp, cand, news, meetings, profiles = fetch_market_info(tdays, today)
    print("取得籌碼面與基本面資料…")
    inst = fundamentals.fetch_institutional(tdays)
    margin = fundamentals.fetch_margin(tdays)
    valuation = fundamentals.fetch_valuation()
    revenue = fundamentals.fetch_revenue(STATE)
    financials = fundamentals.fetch_financials()
    holders = fundamentals.fetch_holders(STATE)
    risk = fundamentals.fetch_risk(tdays[-1] if tdays else "")
    print("計算相對強度與產業強弱…")
    code_ticker = {}
    for code, (_, suffix) in universe.items():
        cands = [code + suffix] if suffix else [code + ".TW", code + ".TWO"]
        t = next((t for t in cands if t in prices), None)
        if t:
            code_ticker[code] = t
    rs_map = relative_strength(code_ticker, prices, profiles, download_index(args.period))

    # 近期新聞：成交金額前 350 名 + 自選股 + 注意 / 處置 / 可能處置（全部抓太慢，也容易被擋）
    print("取得近期新聞…")
    amt = {c: float(prices[t]["Close"].iloc[-1] * prices[t]["Volume"].iloc[-1]) for c, t in code_ticker.items()}
    hot = sorted(amt, key=amt.get, reverse=True)[:350]
    latest = tdays[-1] if tdays else ""
    flagged = set(disp) | set(cand) | {c for c, d in att.items() if latest in d}
    news_codes = list(dict.fromkeys([c for c, _ in watch] + sorted(flagged) + hot))
    ptt = fundamentals.fetch_ptt({c: (universe.get(c, ("", None))[0] or "") for c in code_ticker})
    media = fundamentals.fetch_media_news([(c, universe.get(c, ("", None))[0] or c) for c in news_codes if c in code_ticker])
    stats = BacktestStats()

    rows, dates, failed, alert_notes, screen_rows = [], set(), [], {}, []
    for code, (name, suffix) in universe.items():
        candidates = [code + suffix] if suffix else [code + ".TW", code + ".TWO"]
        ticker = next((t for t in candidates if t in prices), None)
        if not ticker:
            failed.append(code)
            continue
        info = {
            "alert": build_alert(code, att, disp, cand, tdays),
            "meetings": meetings.get(code, []),
            "news": news.get(code, [])[:15],
            "media": media.get(code), "media_checked": code in news_codes, "ptt": ptt.get(code),
            "profile": profiles.get(code, {}),
            "extra": {"inst": inst.get(code), "margin": margin.get(code), "val": valuation.get(code),
                      "rev": revenue.get(code), "fin": financials.get(code), "holders": holders.get(code),
                      "risk": risk.get(code), "rs": rs_map.get(code), "ptt": ptt.get(code)},
        }
        try:
            record, summary, last_date, srow = build_record(code, name or code, ticker, prices[ticker], args.period, info, stats)
        except Exception as e:
            print(f"  {code} 分析失敗：{e}")
            failed.append(code)
            continue
        (SITE / "data" / f"{code}.json").write_text(
            json.dumps(record, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
        rows.append(summary)
        screen_rows.append(srow)
        a = info["alert"]
        if summary[7]:  # 首頁警示清單要顯示的一行說明
            if summary[7] == "d":
                dp = a["disp"]
                note = f"處置 {dp['start'][5:].replace('-', '/')}～{dp['end'][5:].replace('-', '/')}・{dp['reason']}"
            elif summary[7] == "c":
                note = a["cand"]
            else:
                note = re.sub(r"\s+", " ", a["records"][0]["x"])[:70] if a["records"] else ""
            alert_notes[code] = note
        dates.add(last_date)

    rows.sort(key=lambda r: r[0])
    listing = {
        "updated": now_tw(), "date": max(dates) if dates else "",
        "watch": [c for c, _ in watch], "stocks": rows, "alerts": alert_notes,
        "hot": [[c, v["posts"], v["push"]] for c, v in sorted(ptt.items(), key=lambda kv: (-kv[1]["posts"], -kv[1]["push"]))
                if c in {r[0] for r in rows}][:50],
    }
    (SITE / "list.json").write_text(
        json.dumps(listing, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    screen_rows.sort(key=lambda r: r[0])
    n_magic = magic_formula(screen_rows)
    (SITE / "screen.json").write_text(json.dumps(
        {"updated": listing["updated"], "date": listing["date"], "cols": SCREEN_COLS, "rows": screen_rows,
         "bt": stats.result(), "bt_from": tdays[0] if tdays else "", "bt_to": tdays[-21] if len(tdays) > 21 else ""},
        ensure_ascii=False, separators=(",", ":")), encoding="utf-8")

    flags = [r[7] for r in rows]
    n_tw = sum(r[2] == "上市" for r in rows)
    notice(f"成功 {len(rows)} 檔（上市 {n_tw}、上櫃 {len(rows) - n_tw}）、沒有資料 {len(failed)} 檔（資料日期 {listing['date']}）｜"
           f"處置中 {flags.count('d')}、可能處置 {flags.count('c')}、今日注意 {flags.count('a')}｜"
           f"有重大訊息紀錄 {len(news)} 檔、有股東會資料 {len(meetings)} 檔｜"
           f"有股本資料 {sum(1 for r in screen_rows if r[SCREEN_COLS.index('cap')] is not None)} 檔、"
           f"今日創一年新高 {sum(1 for r in screen_rows if r[SCREEN_COLS.index('nh240')] == 1)} 檔、"
           f"今日破 60 日新低 {sum(1 for r in screen_rows if r[SCREEN_COLS.index('nl60')] == 1)} 檔｜"
           f"法人 {len(inst)}、融資券 {len(margin)}、本益比 {len(valuation)}、月營收 {len(revenue)}、財報 {len(financials)}、大戶 {len(holders)} 檔｜"
           f"RS {sum(1 for r in screen_rows if r[SCREEN_COLS.index('rs')] is not None)}、"
           f"地雷股 {sum(1 for r in screen_rows if r[SCREEN_COLS.index('mine')] == 1)}、"
           f"負債比 {sum(1 for r in screen_rows if r[SCREEN_COLS.index('debt')] is not None)}、"
           f"當沖 {sum(1 for r in screen_rows if r[SCREEN_COLS.index('dt_ratio')] is not None)}、"
           f"新聞 {len(media)} 檔、PTT 熱門 {len(ptt)} 檔、神奇公式 {n_magic}、總分≥80 {sum(1 for r in screen_rows if (r[SCREEN_COLS.index('s_total')] or 0) >= 80)} 檔")
    if failed:
        print("沒有資料的代號（多半是新上市或暫停交易）：" + "、".join(failed[:50])
              + (" …" if len(failed) > 50 else ""))
    if not rows:
        sys.exit(1)  # 全部失敗就回報錯誤，GitHub 會寄信通知你
    if not args.no_open and not IN_CI:
        serve_locally()


if __name__ == "__main__":
    main()
