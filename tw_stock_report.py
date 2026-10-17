"""
台股新手學習報告產生器（多檔股票 + 首頁版）
-------------------------------------------------
Python 負責：抓台股資料、計算指標、白話解釋、簡單回測
輸出：site/ 資料夾，裡面有
  index.html          ← 首頁，列出所有股票
  report_代號.html     ← 每檔股票的互動報告

在 GitHub 上：GitHub Actions 每個交易日收盤後自動執行，並發佈到 GitHub Pages。
在自己電腦上也能跑：
  pip install yfinance pandas
  python tw_stock_report.py                 ← 讀 stocks.txt 裡的全部股票
  python tw_stock_report.py 2330 2317       ← 只跑指定的股票
  python tw_stock_report.py --period 2y     ← 改變期間：6mo / 1y / 2y / 5y

要追蹤哪些股票，改 stocks.txt 就好。

※ 本程式僅供學習，不構成任何投資建議。
"""

import argparse
import html
import json
import os
import sys
import time
import webbrowser
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd
import yfinance as yf

HERE = Path(__file__).resolve().parent
SITE = HERE / "site"
TW = ZoneInfo("Asia/Taipei")


def now_tw() -> str:
    return datetime.now(TW).strftime("%Y-%m-%d %H:%M")


def read_stock_list(path: Path) -> list:
    """讀 stocks.txt。每行：代號 名稱（名稱可省略），# 開頭是註解。"""
    stocks = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.split("#")[0].strip()
        if not line:
            continue
        parts = line.split(maxsplit=1)
        stocks.append((parts[0], parts[1].strip() if len(parts) > 1 else ""))
    return stocks


# ============================================================
# 1. 抓資料
# ============================================================
def get_data(code: str, period: str = "1y", name: str = "", retries: int = 3):
    """先試上市(.TW)，抓不到再試上櫃(.TWO)。網路不穩時自動重試。
    回傳 (資料, 代號, 名稱)。"""
    last_err = "抓不到資料"
    for attempt in range(1, retries + 1):
        try:
            for suffix in (".TW", ".TWO"):
                ticker = code + suffix
                t = yf.Ticker(ticker)
                df = t.history(period=period)
                if df.empty:
                    continue
                if not name:
                    name = ticker
                    try:
                        info = t.info or {}
                        name = info.get("longName") or info.get("shortName") or ticker
                    except Exception:
                        pass
                if df.index.tz is not None:
                    df.index = df.index.tz_localize(None)
                return df, ticker, name
        except Exception as e:  # 網路錯誤、被暫時擋住等
            last_err = str(e)
        if attempt < retries:
            print(f"  {code} 第 {attempt} 次下載失敗，{attempt * 5} 秒後重試…")
            time.sleep(attempt * 5)
    raise ValueError(f"{code} 下載失敗：可能是代號錯誤或資料來源暫時無法連線（{last_err}）")


# ============================================================
# 2. 計算指標
# ============================================================
def add_indicators(df: pd.DataFrame) -> pd.DataFrame:
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

    # 成交量（yfinance 單位是「股」，1 張 = 1000 股）
    df["Lots"] = df["Volume"] / 1000
    df["LotsMA20"] = df["Lots"].rolling(20).mean()

    # 每日報酬率
    df["Return"] = df["Close"].pct_change()
    return df


# ============================================================
# 3. 簡單回測：MA5 在 MA20 上方就持有，否則空手
# ============================================================
def backtest(df: pd.DataFrame) -> dict:
    d = df.dropna(subset=["MA5", "MA20"]).copy()
    # 用「前一天」的訊號決定今天要不要持有，避免偷看未來
    d["Hold"] = (d["MA5"] > d["MA20"]).shift(1, fill_value=False)
    strat_curve = (1 + d["Return"].where(d["Hold"], 0)).cumprod() - 1
    hold_curve = d["Close"] / d["Close"].iloc[0] - 1
    trades = int((d["Hold"].astype(int).diff() == 1).sum())
    return {
        "dates": [x.strftime("%Y-%m-%d") for x in d.index],
        "strat_curve": [round(v * 100, 2) for v in strat_curve],
        "hold_curve": [round(v * 100, 2) for v in hold_curve],
        "strat": strat_curve.iloc[-1] * 100,
        "hold": hold_curve.iloc[-1] * 100,
        "trades": trades,
    }


# ============================================================
# 4. 白話解釋：產生一張張「分析卡片」
#    tone: up = 偏多(紅)  down = 偏空(綠)  neutral = 中性  warn = 提醒
# ============================================================
def analyze(df: pd.DataFrame, bt: dict) -> list:
    last, prev = df.iloc[-1], df.iloc[-2]
    close = last["Close"]
    cards = []

    # --- 趨勢（均線排列）---
    if not pd.isna(last["MA60"]):
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
    above = [n for ma, n in (("MA20", "月線"), ("MA60", "季線"))
             if not pd.isna(last[ma]) and close > last[ma]]
    below = [n for ma, n in (("MA20", "月線"), ("MA60", "季線"))
             if not pd.isna(last[ma]) and close <= last[ma]]
    if above and not below:
        tone, val = "up", "站上月線與季線"
    elif below and not above:
        tone, val = "down", "跌破月線與季線"
    else:
        tone, val = "neutral", f"在{'、'.join(above)}之上，{'、'.join(below)}之下"
    cards.append(dict(title="股價位置", value=val, tone=tone,
        text=f"收盤 {close:.2f}，月線 {last['MA20']:.2f}、季線 {last['MA60']:.2f}。"
             "股價在均線之上，代表最近這段時間買進的人平均是賺錢的。"))

    # --- RSI ---
    rsi = last["RSI"]
    if rsi >= 70:
        tone, text = "warn", "超過 70 通常視為「過熱」，短線漲多，要留意回檔。"
    elif rsi <= 30:
        tone, text = "warn", "低於 30 通常視為「超賣」，短線跌深，可能出現反彈。"
    else:
        tone, text = "neutral", "介於 30~70 之間，屬於正常區間，沒有過熱或超賣。"
    cards.append(dict(title="RSI(14)", value=f"{rsi:.1f}", tone=tone, text=text))

    # --- KD ---
    k, d = last["K"], last["D"]
    recent = df.tail(3)
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
    cards.append(dict(title="風險", value=f"波動度 {vol:.1f}%｜最大回檔 {mdd:.1f}%", tone="neutral",
        text="波動度越高，股價上下震盪越劇烈。最大回檔是期間內從高點最多跌了多少，"
             "可以想像：如果在最高點買進，最慘會虧多少。"))

    # --- 回測 ---
    win = bt["strat"] > bt["hold"]
    cards.append(dict(title="小回測", value=f"策略 {bt['strat']:+.1f}%｜持有 {bt['hold']:+.1f}%",
        tone="neutral",
        text=f"「週線在月線上方才持有」的策略進場 {bt['trades']} 次，"
             f"{'贏過' if win else '輸給'}「買了就放著」。未計手續費與交易稅，實際會更差一點。"))
    return cards


# ============================================================
# 5. 組成 HTML
# ============================================================
def clean(series, nd=2):
    return [None if pd.isna(x) else round(float(x), nd) for x in series]


def build_html(df, ticker, name, cards, bt, period) -> str:
    last = df.iloc[-1]
    first_close = df["Close"].iloc[0]
    change = (last["Close"] / first_close - 1) * 100
    day_chg = (last["Close"] / df["Close"].iloc[-2] - 1) * 100

    data = {
        "dates": [x.strftime("%Y-%m-%d") for x in df.index],
        "open": clean(df["Open"]), "high": clean(df["High"]),
        "low": clean(df["Low"]), "close": clean(df["Close"]),
        "ma5": clean(df["MA5"]), "ma20": clean(df["MA20"]), "ma60": clean(df["MA60"]),
        "rsi": clean(df["RSI"], 1), "k": clean(df["K"], 1), "d": clean(df["D"], 1),
        "lots": clean(df["Lots"], 0),
        "volColor": ["up" if r >= 0 else "down" for r in df["Return"].fillna(0)],
        "bt": {k: bt[k] for k in ("dates", "strat_curve", "hold_curve")},
    }

    tone_label = {"up": "偏多", "down": "偏空", "neutral": "中性", "warn": "留意"}
    cards_html = "\n".join(
        f'<div class="card tone-{c["tone"]}">'
        f'<div class="card-head"><span class="card-title">{html.escape(c["title"])}</span>'
        f'<span class="tag">{tone_label[c["tone"]]}</span></div>'
        f'<div class="card-value">{html.escape(c["value"])}</div>'
        f'<p>{html.escape(c["text"])}</p></div>'
        for c in cards
    )

    sign = "up" if day_chg >= 0 else "down"
    replacements = {
        "__TITLE__": html.escape(f"{ticker} 學習報告"),
        "__NAME__": html.escape(name),
        "__TICKER__": html.escape(ticker),
        "__CLOSE__": f"{last['Close']:.2f}",
        "__DAYCHG__": f"{day_chg:+.2f}%",
        "__SIGN__": sign,
        "__PERIOD__": html.escape(period),
        "__RANGE__": f"{df.index[0]:%Y-%m-%d} ～ {df.index[-1]:%Y-%m-%d}",
        "__CHANGE__": f"{change:+.1f}%",
        "__CHANGESIGN__": "up" if change >= 0 else "down",
        "__HIGH__": f"{df['High'].max():.2f}",
        "__LOW__": f"{df['Low'].min():.2f}",
        "__CARDS__": cards_html,
        "__GENERATED__": now_tw(),
        "__DATA__": json.dumps(data, ensure_ascii=False),
    }
    out = HTML_TEMPLATE
    for k, v in replacements.items():
        out = out.replace(k, v)
    return out


HTML_TEMPLATE = r"""<!doctype html>
<html lang="zh-Hant">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>__TITLE__</title>
<script src="https://cdn.plot.ly/plotly-2.35.2.min.js"></script>
<style>
  :root {
    --bg: #f6f5f1; --panel: #ffffff; --ink: #1d1d1b; --muted: #6b6a65;
    --line: #e4e2db; --grid: #ecebe5;
    --up: #d1342c; --down: #1a8a4a; --warn: #c27c0e; --neutral: #5b6b7a;
    --ma5: #e08a1e; --ma20: #2f6fd6; --ma60: #8a4fc7;
  }
  @media (prefers-color-scheme: dark) {
    :root {
      --bg: #141413; --panel: #1e1e1c; --ink: #ecebe6; --muted: #9c9a92;
      --line: #2e2d2a; --grid: #2a2926;
      --up: #f0574e; --down: #3cbf72; --warn: #e8a53a; --neutral: #8fa1b3;
      --ma5: #f0a34a; --ma20: #5b92ec; --ma60: #b07fe6;
    }
  }
  * { box-sizing: border-box; }
  body {
    margin: 0; background: var(--bg); color: var(--ink);
    font-family: -apple-system, "PingFang TC", "Microsoft JhengHei", "Noto Sans TC", sans-serif;
    line-height: 1.6;
  }
  .wrap { max-width: 1100px; margin: 0 auto; padding: 16px 16px 48px; }
  .back { display: inline-block; color: var(--muted); text-decoration: none; font-size: 14px; padding: 6px 0; margin-bottom: 8px; }
  .back:hover { color: var(--ink); }
  header { display: flex; flex-wrap: wrap; align-items: flex-end; justify-content: space-between; gap: 16px; margin-bottom: 20px; }
  .ticker { color: var(--muted); font-size: 14px; letter-spacing: .04em; }
  h1 { margin: 2px 0 0; font-size: 26px; font-weight: 700; }
  .price { text-align: right; }
  .price .big { font-size: 34px; font-weight: 700; font-variant-numeric: tabular-nums; }
  .price .chg { font-size: 16px; font-weight: 600; margin-left: 8px; }
  .up { color: var(--up); } .down { color: var(--down); }
  .meta { color: var(--muted); font-size: 13px; }

  .stats { display: grid; grid-template-columns: repeat(auto-fit, minmax(150px, 1fr)); gap: 12px; margin-bottom: 20px; }
  .stat { background: var(--panel); border: 1px solid var(--line); border-radius: 10px; padding: 12px 14px; }
  .stat .label { font-size: 12px; color: var(--muted); }
  .stat .num { font-size: 20px; font-weight: 700; font-variant-numeric: tabular-nums; }

  section { background: var(--panel); border: 1px solid var(--line); border-radius: 12px; padding: 18px; margin-bottom: 20px; }
  h2 { font-size: 17px; margin: 0 0 4px; }
  .hint { color: var(--muted); font-size: 13px; margin: 0 0 12px; }

  .cards { display: grid; grid-template-columns: repeat(auto-fit, minmax(240px, 1fr)); gap: 12px; }
  .card { border: 1px solid var(--line); border-left: 4px solid var(--neutral); border-radius: 8px; padding: 12px 14px; }
  .card.tone-up { border-left-color: var(--up); }
  .card.tone-down { border-left-color: var(--down); }
  .card.tone-warn { border-left-color: var(--warn); }
  .card-head { display: flex; justify-content: space-between; align-items: center; }
  .card-title { font-size: 13px; color: var(--muted); }
  .tag { font-size: 11px; padding: 1px 8px; border-radius: 99px; background: var(--grid); color: var(--muted); }
  .tone-up .tag { color: var(--up); } .tone-down .tag { color: var(--down); } .tone-warn .tag { color: var(--warn); }
  .card-value { font-size: 17px; font-weight: 700; margin: 4px 0 6px; font-variant-numeric: tabular-nums; }
  .card p { margin: 0; font-size: 14px; color: var(--ink); opacity: .85; }

  .chart { width: 100%; }
  #price { height: 520px; } #ind { height: 380px; } #bt { height: 300px; }

  /* 圖表期間切換（捲動時黏在畫面上方） */
  .seg-bar { position: sticky; top: 0; z-index: 10; background: var(--bg); padding: 8px 0; margin-bottom: 12px; }
  .seg { display: flex; background: var(--panel); border: 1px solid var(--line); border-radius: 10px; padding: 3px; }
  .seg button { flex: 1; border: 0; background: transparent; color: var(--muted); font: inherit; font-size: 14px;
                padding: 8px 0; border-radius: 7px; cursor: pointer; }
  .seg button.on { background: var(--ink); color: var(--bg); font-weight: 600; }

  /* 圖例 */
  .lg { display: flex; flex-wrap: wrap; gap: 4px 14px; font-size: 13px; color: var(--muted); margin-bottom: 4px; }
  .lg i { display: inline-block; width: 14px; height: 3px; border-radius: 2px; vertical-align: middle; margin-right: 5px; }
  .lg i.box { width: 9px; height: 9px; border-radius: 2px; }
  .lg i.dot { background: none; border-top: 2px dotted; height: 0; }
  .sub { font-size: 12px; color: var(--muted); margin: 6px 0 2px; }
  .bt-sum { display: flex; gap: 10px; margin: 0 0 8px; }
  .bt-sum div { flex: 1; background: var(--grid); border-radius: 8px; padding: 8px 10px; }
  .bt-sum .label { font-size: 12px; color: var(--muted); }
  .bt-sum .num { font-size: 18px; font-weight: 700; font-variant-numeric: tabular-nums; }

  details { border-top: 1px solid var(--line); padding: 10px 0; }
  details:first-of-type { border-top: none; }
  summary { cursor: pointer; font-weight: 600; }
  details p { margin: 8px 0 0; font-size: 14px; color: var(--ink); opacity: .85; }

  footer { color: var(--muted); font-size: 12px; text-align: center; margin-top: 8px; }
  @media (max-width: 640px) {
    .wrap { padding: 8px 12px 40px; }
    header { gap: 6px; margin-bottom: 14px; }
    h1 { font-size: 22px; }
    .price { text-align: left; }
    .price .big { font-size: 30px; }
    .stats { grid-template-columns: repeat(3, minmax(0, 1fr)); gap: 8px; margin-bottom: 14px; }
    .stat { padding: 10px; }
    .stat .num { font-size: 16px; }
    section { padding: 14px 12px; border-radius: 10px; margin-bottom: 14px; }
    .cards { grid-template-columns: 1fr; gap: 10px; }
    .card-value { font-size: 16px; }
    #price { height: 400px; } #ind { height: 300px; } #bt { height: 240px; }
  }
</style>
</head>
<body>
<div class="wrap">
  <a class="back" href="index.html">← 股票列表</a>
  <header>
    <div>
      <div class="ticker">__TICKER__</div>
      <h1>__NAME__</h1>
      <div class="meta">資料期間 __RANGE__（__PERIOD__）</div>
    </div>
    <div class="price">
      <span class="big">__CLOSE__</span><span class="chg __SIGN__">__DAYCHG__</span>
      <div class="meta">最新收盤價（元）與當日漲跌</div>
    </div>
  </header>

  <div class="stats">
    <div class="stat"><div class="label">期間漲跌幅</div><div class="num __CHANGESIGN__">__CHANGE__</div></div>
    <div class="stat"><div class="label">期間最高</div><div class="num">__HIGH__</div></div>
    <div class="stat"><div class="label">期間最低</div><div class="num">__LOW__</div></div>
  </div>

  <section>
    <h2>白話分析</h2>
    <p class="hint">左邊色條：紅 = 偏多、綠 = 偏空、橘 = 需要留意、灰 = 中性（台股習慣紅漲綠跌）</p>
    <div class="cards">
__CARDS__
    </div>
  </section>

  <div class="seg-bar">
    <div class="seg" id="seg">
      <button data-n="21">1 個月</button><button data-n="63">3 個月</button>
      <button data-n="126">6 個月</button><button data-n="100000">全部</button>
    </div>
  </div>

  <section>
    <h2>股價與均線</h2>
    <p class="hint">點一下圖（電腦用滑鼠移上去）可以看當天的數字。上方按鈕切換要看多久。</p>
    <div class="lg">
      <span><i class="box" style="background:var(--up)"></i>漲</span>
      <span><i class="box" style="background:var(--down)"></i>跌</span>
      <span><i style="background:var(--ma5)"></i>MA5 週線</span>
      <span><i style="background:var(--ma20)"></i>MA20 月線</span>
      <span><i style="background:var(--ma60)"></i>MA60 季線</span>
    </div>
    <div id="price" class="chart"></div>
    <div class="sub">下方柱狀：成交量（張）</div>
  </section>

  <section>
    <h2>RSI 與 KD</h2>
    <p class="hint">RSI 超過 70 偏熱、低於 30 偏冷；KD 超過 80 偏熱、低於 20 偏冷。</p>
    <div class="lg">
      <span><i style="background:var(--ma60)"></i>RSI</span>
      <span><i style="background:var(--ma5)"></i>K</span>
      <span><i style="background:var(--ma20)"></i>D</span>
    </div>
    <div id="ind" class="chart"></div>
  </section>

  <section>
    <h2>小回測：均線策略 vs 買進持有</h2>
    <p class="hint">策略規則：週線(MA5)在月線(MA20)上方就持有，否則空手。從所選期間的第一天開始算。</p>
    <div class="bt-sum">
      <div><div class="label">均線策略</div><div class="num" id="bt-s">—</div></div>
      <div><div class="label">買進持有</div><div class="num" id="bt-h">—</div></div>
    </div>
    <div class="lg">
      <span><i style="background:var(--ma20)"></i>均線策略</span>
      <span><i class="dot" style="border-color:var(--muted)"></i>買進持有</span>
    </div>
    <div id="bt" class="chart"></div>
  </section>

  <section>
    <h2>名詞小教室</h2>
    <details><summary>K 線（蠟燭圖）</summary>
      <p>每一根代表一天。實體的上下兩端是開盤價與收盤價，細線的頂端是最高價、底端是最低價。紅色代表收盤比開盤高（漲），綠色代表收盤比開盤低（跌）。</p></details>
    <details><summary>均線 MA</summary>
      <p>過去 N 天收盤價的平均。MA5 約一週、MA20 約一個月、MA60 約一季。可以把均線想成「最近這段時間大家的平均買進成本」。</p></details>
    <details><summary>RSI 相對強弱指標</summary>
      <p>比較最近 14 天上漲和下跌的力道，數值在 0~100。數字越高代表最近漲得越兇。它只說明「漲多了」或「跌多了」，不代表一定會反轉。</p></details>
    <details><summary>KD 隨機指標</summary>
      <p>看今天收盤價落在最近 9 天高低區間的哪個位置。K 線比較敏感、D 線比較平滑。K 由下往上穿過 D 叫黃金交叉，反過來叫死亡交叉。在台灣很常用，但盤整時容易反覆出現假訊號。</p></details>
    <details><summary>成交量</summary>
      <p>當天成交了多少張（1 張 = 1000 股）。量代表市場的關注度，「價漲量增」通常比「價漲量縮」更健康。</p></details>
    <details><summary>波動度與最大回檔</summary>
      <p>波動度是股價每天漲跌幅度換算成一年的數字，越大代表越刺激也越危險。最大回檔是從期間最高點往下跌的最大幅度，用來想像最壞情況。</p></details>
    <details><summary>回測</summary>
      <p>把一套買賣規則套用到過去的資料，看看會賺還是賠。過去有效不代表未來有效，而且真實交易還有手續費、交易稅和滑價。</p></details>
  </section>

  <footer>報告產生時間 __GENERATED__ ・ 資料來源 Yahoo Finance ・ 僅供學習參考，不構成投資建議</footer>
</div>

<script>
const D = __DATA__;
const css = getComputedStyle(document.documentElement);
const v = (n) => css.getPropertyValue(n).trim();
const C = { ink: v('--ink'), muted: v('--muted'), grid: v('--grid'), up: v('--up'), down: v('--down'),
            ma5: v('--ma5'), ma20: v('--ma20'), ma60: v('--ma60'), warn: v('--warn'), neutral: v('--neutral') };
const mobile = window.matchMedia('(max-width: 640px)').matches;
const font = { family: '-apple-system, "PingFang TC", "Microsoft JhengHei", sans-serif', color: C.muted, size: mobile ? 11 : 12 };

// 日期標籤：短期間顯示「月/日」，長期間顯示「年/月」
const shortDate = (d, long) => long ? d.slice(2, 7).replace('-', '/') : d.slice(5).replace('-', '/');
const xaxis = (x) => {
  const want = mobile ? 4 : 7, step = Math.max(1, Math.ceil(x.length / want)), long = x.length > 140;
  const off = Math.floor(step / 2);  // 第一個標籤往內縮，避免被左邊切掉
  const vals = x.filter((_, i) => i % step === off);
  return { type: 'category', tickvals: vals, ticktext: vals.map(d => shortDate(d, long)), tickangle: 0,
           showgrid: false, linecolor: C.grid, tickfont: font, rangeslider: { visible: false }, fixedrange: true,
           showspikes: true, spikemode: 'across', spikethickness: 1, spikecolor: C.muted, spikedash: 'dot' };
};
const yax = (extra) => Object.assign({ gridcolor: C.grid, zeroline: false, tickfont: font, side: 'right',
                                       fixedrange: true, automargin: true, nticks: mobile ? 5 : 7 }, extra);
const base = { paper_bgcolor: 'rgba(0,0,0,0)', plot_bgcolor: 'rgba(0,0,0,0)', font, showlegend: false,
               margin: mobile ? { l: 2, r: 6, t: 6, b: 24 } : { l: 6, r: 10, t: 8, b: 28 },
               hovermode: 'x unified', dragmode: false,
               hoverlabel: { font: { size: mobile ? 12 : 13 }, namelength: -1 } };
// 不顯示工具列、不攔截捲動：手機上滑過圖表可以正常往下捲
const cfg = { responsive: true, displayModeBar: false, scrollZoom: false };
const hline = (y, color, yref) => ({ type: 'line', xref: 'paper', x0: 0, x1: 1, y0: y, y1: y, yref,
                                    line: { color, width: 1, dash: 'dash' } });
const fmt = (v) => (v >= 0 ? '+' : '') + v.toFixed(1) + '%';

function render(n) {
  const N = D.dates.length, s = Math.max(0, N - n);
  const cut = (a) => a.slice(s);
  const x = cut(D.dates);
  const ohlc = x.map((_, i) => `開 ${D.open[s+i]}　高 ${D.high[s+i]}<br>低 ${D.low[s+i]}　收 ${D.close[s+i]}`);

  // 股價 + 均線 + 成交量
  Plotly.react('price', [
    { type: 'candlestick', name: 'K線', x, open: cut(D.open), high: cut(D.high), low: cut(D.low), close: cut(D.close),
      text: ohlc, hoverinfo: 'text',
      increasing: { line: { color: C.up, width: 1 }, fillcolor: C.up },
      decreasing: { line: { color: C.down, width: 1 }, fillcolor: C.down } },
    { type: 'scatter', mode: 'lines', name: 'MA5', x, y: cut(D.ma5), line: { color: C.ma5, width: 1.3 }, hovertemplate: '%{y:.2f}' },
    { type: 'scatter', mode: 'lines', name: 'MA20', x, y: cut(D.ma20), line: { color: C.ma20, width: 1.5 }, hovertemplate: '%{y:.2f}' },
    { type: 'scatter', mode: 'lines', name: 'MA60', x, y: cut(D.ma60), line: { color: C.ma60, width: 1.5 }, hovertemplate: '%{y:.2f}' },
    { type: 'bar', name: '量', x, y: cut(D.lots), yaxis: 'y2', hovertemplate: '%{y:,.0f} 張',
      marker: { color: cut(D.volColor).map(c => c === 'up' ? C.up : C.down), opacity: 0.55 } },
  ], Object.assign({}, base, {
    xaxis: xaxis(x), yaxis: yax({ domain: [0.25, 1] }),
    yaxis2: yax({ domain: [0, 0.19], showgrid: false, nticks: 3, tickformat: '~s' }),
  }), cfg);

  // RSI + KD
  Plotly.react('ind', [
    { type: 'scatter', mode: 'lines', name: 'RSI', x, y: cut(D.rsi), line: { color: C.ma60, width: 1.5 }, hovertemplate: '%{y:.1f}' },
    { type: 'scatter', mode: 'lines', name: 'K', x, y: cut(D.k), yaxis: 'y2', line: { color: C.ma5, width: 1.5 }, hovertemplate: '%{y:.1f}' },
    { type: 'scatter', mode: 'lines', name: 'D', x, y: cut(D.d), yaxis: 'y2', line: { color: C.ma20, width: 1.5 }, hovertemplate: '%{y:.1f}' },
  ], Object.assign({}, base, {
    xaxis: xaxis(x),
    yaxis: yax({ domain: [0.56, 1], range: [0, 100], tickvals: [30, 70] }),
    yaxis2: yax({ domain: [0, 0.44], range: [0, 100], tickvals: [20, 80] }),
    shapes: [hline(70, C.up, 'y'), hline(30, C.down, 'y'), hline(80, C.up, 'y2'), hline(20, C.down, 'y2')],
    annotations: [
      { text: 'RSI', xref: 'paper', yref: 'paper', x: 0, y: 1, xanchor: 'left', yanchor: 'top', showarrow: false, font },
      { text: 'KD', xref: 'paper', yref: 'paper', x: 0, y: 0.44, xanchor: 'left', yanchor: 'top', showarrow: false, font },
    ],
  }), cfg);

  // 回測：從所選期間的第一天重新起算
  let b = D.bt.dates.findIndex(d => d >= x[0]);
  if (b < 0) b = 0;
  const rebase = (arr) => { const b0 = 1 + arr[b] / 100; return arr.slice(b).map(v => +(((1 + v / 100) / b0 - 1) * 100).toFixed(2)); };
  const bx = D.bt.dates.slice(b), st = rebase(D.bt.strat_curve), ho = rebase(D.bt.hold_curve);
  const setNum = (id, v) => { const el = document.getElementById(id); el.textContent = fmt(v); el.className = 'num ' + (v >= 0 ? 'up' : 'down'); };
  setNum('bt-s', st[st.length - 1]); setNum('bt-h', ho[ho.length - 1]);
  Plotly.react('bt', [
    { type: 'scatter', mode: 'lines', name: '均線策略', x: bx, y: st, line: { color: C.ma20, width: 2 }, hovertemplate: '%{y:+.1f}%' },
    { type: 'scatter', mode: 'lines', name: '買進持有', x: bx, y: ho, line: { color: C.muted, width: 1.5, dash: 'dot' }, hovertemplate: '%{y:+.1f}%' },
  ], Object.assign({}, base, {
    xaxis: xaxis(bx), yaxis: yax({ ticksuffix: '%', zeroline: true, zerolinecolor: C.muted }),
  }), cfg);
}

// 期間按鈕：手機預設 3 個月（K 線才看得清楚），電腦預設全部
const buttons = document.querySelectorAll('#seg button');
function choose(btn) {
  buttons.forEach(b => b.classList.toggle('on', b === btn));
  render(+btn.dataset.n);
}
buttons.forEach(b => b.addEventListener('click', () => choose(b)));
choose(buttons[mobile ? 1 : 3]);
</script>
</body>
</html>
"""


# ============================================================
# 6. 產生單一股票報告，回傳首頁要用的摘要
# ============================================================
def generate_one(code: str, name: str, period: str) -> dict:
    df, ticker, name = get_data(code, period, name)
    if len(df) < 70:
        print(f"  提醒：{code} 資料天數太少，季線等指標可能無法計算。")
    df = add_indicators(df)
    bt = backtest(df)
    cards = analyze(df, bt)

    out = SITE / f"report_{code}.html"
    out.write_text(build_html(df, ticker, name, cards, bt, period), encoding="utf-8")

    last = df.iloc[-1]
    trend = cards[0] if cards and cards[0]["title"] == "趨勢" else {"value": "—", "tone": "neutral"}
    return {
        "code": code, "name": name, "file": out.name, "ok": True,
        "close": f"{last['Close']:.2f}",
        "day_chg": (last["Close"] / df["Close"].iloc[-2] - 1) * 100,
        "period_chg": (last["Close"] / df["Close"].iloc[0] - 1) * 100,
        "rsi": last["RSI"], "k": last["K"],
        "trend": trend["value"], "trend_tone": trend["tone"],
        "date": f"{df.index[-1]:%Y-%m-%d}",
    }


# ============================================================
# 7. 首頁
# ============================================================
def build_index(rows: list, period: str) -> str:
    items = []
    for r in rows:
        if not r["ok"]:
            items.append(
                f'<div class="row failed"><div class="who"><span class="code">{html.escape(r["code"])}</span>'
                f'<span class="name">{html.escape(r["name"] or "")}</span></div>'
                f'<div class="err">這次沒有抓到資料，下次更新會再試一次</div></div>')
            continue
        sign = "up" if r["day_chg"] >= 0 else "down"
        psign = "up" if r["period_chg"] >= 0 else "down"
        items.append(
            f'<a class="row" href="{r["file"]}">'
            f'<div class="who"><span class="code">{html.escape(r["code"])}</span>'
            f'<span class="name">{html.escape(r["name"])}</span></div>'
            f'<div class="px"><span class="close">{r["close"]}</span>'
            f'<span class="chg {sign}">{r["day_chg"]:+.2f}%</span></div>'
            f'<div class="tags"><span class="tag tone-{r["trend_tone"]}">{html.escape(r["trend"])}</span>'
            f'<span class="tag">RSI {r["rsi"]:.0f}</span><span class="tag">K {r["k"]:.0f}</span>'
            f'<span class="tag {psign}">{period} {r["period_chg"]:+.1f}%</span></div>'
            f'<span class="arrow">›</span></a>')
    dates = sorted({r["date"] for r in rows if r["ok"]})
    return (INDEX_TEMPLATE
            .replace("__ROWS__", "\n".join(items))
            .replace("__DATE__", dates[-1] if dates else "—")
            .replace("__GENERATED__", now_tw()))


INDEX_TEMPLATE = r"""<!doctype html>
<html lang="zh-Hant">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>我的台股學習站</title>
<style>
  :root {
    --bg: #f6f5f1; --panel: #ffffff; --ink: #1d1d1b; --muted: #6b6a65; --line: #e4e2db; --soft: #efeee8;
    --up: #d1342c; --down: #1a8a4a; --warn: #c27c0e;
  }
  @media (prefers-color-scheme: dark) {
    :root {
      --bg: #141413; --panel: #1e1e1c; --ink: #ecebe6; --muted: #9c9a92; --line: #2e2d2a; --soft: #2a2926;
      --up: #f0574e; --down: #3cbf72; --warn: #e8a53a;
    }
  }
  * { box-sizing: border-box; }
  body { margin: 0; background: var(--bg); color: var(--ink); line-height: 1.5;
         font-family: -apple-system, "PingFang TC", "Microsoft JhengHei", "Noto Sans TC", sans-serif; }
  .wrap { max-width: 760px; margin: 0 auto; padding: 28px 16px 48px; }
  h1 { font-size: 24px; margin: 0; }
  .meta { color: var(--muted); font-size: 13px; margin: 4px 0 20px; }
  .list { background: var(--panel); border: 1px solid var(--line); border-radius: 12px; overflow: hidden; }
  .row { position: relative; display: grid; grid-template-columns: 1fr auto; gap: 6px 12px;
         padding: 14px 36px 14px 16px; border-top: 1px solid var(--line); color: inherit; text-decoration: none; }
  .row:first-child { border-top: none; }
  a.row:hover { background: var(--soft); }
  .who { display: flex; align-items: baseline; gap: 8px; min-width: 0; }
  .code { font-weight: 700; font-size: 17px; font-variant-numeric: tabular-nums; }
  .name { color: var(--muted); font-size: 14px; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
  .px { text-align: right; font-variant-numeric: tabular-nums; }
  .close { font-weight: 700; font-size: 17px; }
  .chg { font-size: 14px; font-weight: 600; margin-left: 8px; }
  .tags { grid-column: 1 / -1; display: flex; flex-wrap: wrap; gap: 6px; }
  .tag { font-size: 12px; padding: 1px 8px; border-radius: 99px; background: var(--soft); color: var(--muted); }
  .up { color: var(--up); } .down { color: var(--down); }
  .tag.tone-up { color: var(--up); } .tag.tone-down { color: var(--down); }
  .arrow { position: absolute; right: 14px; top: 50%; transform: translateY(-50%); color: var(--muted); font-size: 22px; }
  .failed .err { grid-column: 1 / -1; color: var(--warn); font-size: 13px; }
  .note { color: var(--muted); font-size: 13px; margin-top: 16px; }
  footer { color: var(--muted); font-size: 12px; text-align: center; margin-top: 28px; }
</style>
</head>
<body>
<div class="wrap">
  <h1>我的台股學習站</h1>
  <div class="meta">資料日期 __DATE__ ・ 每個交易日收盤後自動更新</div>
  <div class="list">
__ROWS__
  </div>
  <p class="note">點任一檔股票看完整報告：K 線圖、均線、RSI、KD、回測與白話解釋。紅色代表上漲 / 偏多，綠色代表下跌 / 偏空。</p>
  <footer>更新時間 __GENERATED__（台北時間）・ 資料來源 Yahoo Finance ・ 僅供學習參考，不構成投資建議</footer>
</div>
</body>
</html>
"""


# ============================================================
def main():
    p = argparse.ArgumentParser(description="台股新手學習報告產生器")
    p.add_argument("codes", nargs="*", help="股票代號，不填就讀 stocks.txt")
    p.add_argument("--period", default="1y", help="期間：6mo / 1y / 2y / 5y（預設 1y）")
    p.add_argument("--no-open", action="store_true", help="產生後不要自動打開瀏覽器")
    args = p.parse_args()

    if args.codes:
        stocks = [(c, "") for c in args.codes]
    else:
        list_file = HERE / "stocks.txt"
        if not list_file.exists():
            sys.exit("找不到 stocks.txt，請建立這個檔案，或在指令後面直接打股票代號。")
        stocks = read_stock_list(list_file)
    if not stocks:
        sys.exit("沒有要分析的股票。")

    SITE.mkdir(exist_ok=True)
    rows = []
    for i, (code, name) in enumerate(stocks):
        print(f"[{i + 1}/{len(stocks)}] {code} {name}")
        try:
            rows.append(generate_one(code, name, args.period))
        except Exception as e:
            print(f"  ✗ {e}")
            rows.append({"code": code, "name": name, "ok": False})
        time.sleep(1)  # 每檔之間稍微停一下，避免被資料來源擋

    index = SITE / "index.html"
    index.write_text(build_index(rows, args.period), encoding="utf-8")
    ok = sum(r["ok"] for r in rows)
    print(f"\n完成：成功 {ok} 檔、失敗 {len(rows) - ok} 檔。首頁：{index}")

    if ok == 0:
        sys.exit(1)  # 全部失敗就回報錯誤，GitHub 會寄信通知你
    if not args.no_open and not os.environ.get("GITHUB_ACTIONS"):
        webbrowser.open(index.as_uri())


if __name__ == "__main__":
    main()
