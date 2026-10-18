/*
 * 看盤 K 線圖（使用 TradingView Lightweight Charts™，Apache License 2.0）
 * Copyright (c) 2023 TradingView, Inc. https://www.tradingview.com/
 *
 * 功能：日 / 週 / 月、1 / 5 / 15 / 30 / 60 分 K；十字線即時顯示開高低收；均線；成交量；買賣標記
 */
(function () {
  const TZ = 8 * 3600;   // 圖表以 UTC 顯示，所以把時間加 8 小時 → 顯示台北時間

  const css = (n) => getComputedStyle(document.documentElement).getPropertyValue(n).trim();
  const fmt = (v, d = 2) => v == null || isNaN(v) ? '—' : (+v).toLocaleString(undefined, { minimumFractionDigits: d, maximumFractionDigits: d });
  const pad = (n) => String(n).padStart(2, '0');

  // ---------- 資料轉換 ----------
  function fromDaily(px) {
    return px.d.map((d, i) => ({ time: d, open: px.o[i], high: px.h[i], low: px.l[i], close: px.c[i], volume: px.v[i] }))
      .filter(b => b.open != null && b.close != null);
  }
  function fromIntraday(o) {
    if (!o) return [];
    let t = o.t0;
    return o.dt.map((d, i) => { t += d * 60; return { time: t + TZ, open: o.o[i], high: o.h[i], low: o.l[i], close: o.c[i], volume: o.v[i] || 0 }; })
      .filter(b => b.open != null && b.close != null);
  }
  // 5 分 K 合成 15 / 30 / 60 分 K（台股 09:00～13:30）
  function aggMinutes(bars, n) {
    const out = []; let cur = null, key = null;
    const maxB = Math.floor(269 / n);
    for (const b of bars) {
      const day = Math.floor(b.time / 86400), mins = Math.floor((b.time % 86400) / 60) - 540;
      const bk = Math.min(Math.floor(Math.max(mins, 0) / n), maxB), k = day * 1000 + bk;
      if (k !== key) {
        if (cur) out.push(cur);
        key = k; cur = { time: day * 86400 + (540 + bk * n) * 60, open: b.open, high: b.high, low: b.low, close: b.close, volume: b.volume };
      } else { cur.high = Math.max(cur.high, b.high); cur.low = Math.min(cur.low, b.low); cur.close = b.close; cur.volume += b.volume; }
    }
    if (cur) out.push(cur);
    return out;
  }
  // 日 K 合成週 K / 月 K
  function aggPeriod(daily, kind) {
    const out = []; let cur = null, key = null;
    for (const b of daily) {
      const dt = new Date(b.time + 'T00:00:00Z');
      let k;
      if (kind === 'M') k = b.time.slice(0, 7);
      else { const mon = new Date(dt); mon.setUTCDate(dt.getUTCDate() - ((dt.getUTCDay() + 6) % 7)); k = mon.toISOString().slice(0, 10); }
      if (k !== key) {
        if (cur) out.push(cur);
        key = k; cur = { time: b.time, open: b.open, high: b.high, low: b.low, close: b.close, volume: b.volume };
      } else { cur.high = Math.max(cur.high, b.high); cur.low = Math.min(cur.low, b.low); cur.close = b.close; cur.volume += b.volume; }
    }
    if (cur) out.push(cur);
    return out;
  }
  function sma(bars, n) {
    const out = []; let s = 0;
    for (let i = 0; i < bars.length; i++) {
      s += bars[i].close; if (i >= n) s -= bars[i - n].close;
      if (i >= n - 1) out.push({ time: bars[i].time, value: +(s / n).toFixed(2) });
    }
    return out;
  }
  const tkey = (t) => typeof t === 'object' && t ? `${t.year}-${pad(t.month)}-${pad(t.day)}` : String(t);

  // ---------- 圖表 ----------
  function create(el, opt = {}) {
    const C = { up: css('--up'), down: css('--down'), muted: css('--muted'), grid: css('--grid') || css('--line'), ink: css('--ink'),
                bg: css('--panel'), ma: [css('--ma5') || '#e08a1e', css('--ma20') || css('--accent') || '#2f6fd6', css('--ma60') || '#8a4fc7', '#20a39e'] };
    const mas = opt.mas || [5, 10, 20, 60];
    const chart = LightweightCharts.createChart(el, {
      autoSize: true,
      layout: { background: { type: 'solid', color: 'transparent' }, textColor: C.muted, fontSize: 11,
                fontFamily: '-apple-system, "PingFang TC", "Microsoft JhengHei", sans-serif', attributionLogo: true },
      grid: { vertLines: { color: C.grid }, horzLines: { color: C.grid } },
      crosshair: { mode: 0, vertLine: { color: C.muted, style: 3, labelBackgroundColor: C.ink }, horzLine: { color: C.muted, style: 3, labelBackgroundColor: C.ink } },
      rightPriceScale: { borderColor: C.grid, scaleMargins: { top: 0.08, bottom: opt.volume === false ? 0.05 : 0.24 } },
      timeScale: { borderColor: C.grid, rightOffset: 4, barSpacing: 7, minBarSpacing: 1.5, fixLeftEdge: false },
      handleScroll: { vertTouchDrag: false },        // 上下滑動是捲動網頁
      handleScale: { axisPressedMouseMove: { time: true, price: false } },
      localization: { locale: 'zh-TW', dateFormat: 'yyyy/MM/dd' },
    });
    const candle = chart.addCandlestickSeries({
      upColor: C.up, downColor: C.down, borderUpColor: C.up, borderDownColor: C.down, wickUpColor: C.up, wickDownColor: C.down,
      priceLineVisible: true, priceLineStyle: 2,
    });
    let vol = null;
    if (opt.volume !== false) {
      vol = chart.addHistogramSeries({ priceFormat: { type: 'volume' }, priceScaleId: 'vol', lastValueVisible: false, priceLineVisible: false });
      chart.priceScale('vol').applyOptions({ scaleMargins: { top: 0.8, bottom: 0 } });
    }
    const maSeries = mas.map((n, k) => chart.addLineSeries({ color: C.ma[k % 4], lineWidth: 1, priceLineVisible: false, lastValueVisible: false, crosshairMarkerVisible: false }));
    let extra = [], segs = [], bars = [], idx = {}, maData = [], extraData = [], intraday = false;

    function info(i) {
      if (!opt.info) return;
      const b = bars[i]; if (!b) { opt.info.innerHTML = ''; return; }
      const prev = bars[i - 1], chg = prev ? (b.close / prev.close - 1) * 100 : null;
      const t = intraday ? new Date(b.time * 1000).toISOString().slice(5, 16).replace('-', '/').replace('T', ' ') : String(b.time).replace(/-/g, '/');
      const col = (v) => v == null ? '' : v >= 0 ? C.up : C.down;
      const lines = mas.map((n, k) => { const p = maData[k][b._k]; return p != null ? `<span style="color:${C.ma[k % 4]}">MA${n} ${fmt(p)}</span>` : ''; }).join('')
        + extraData.map(e => { const p = e.map[tkey(b.time)]; return p != null ? `<span style="color:${e.color}">${e.name} ${fmt(p)}</span>` : ''; }).join('');
      opt.info.innerHTML = `<div class="ki-row"><b>${t}</b>
        <span>開 <em style="color:${col(prev ? b.open - prev.close : null)}">${fmt(b.open)}</em></span>
        <span>高 <em style="color:${col(prev ? b.high - prev.close : null)}">${fmt(b.high)}</em></span>
        <span>低 <em style="color:${col(prev ? b.low - prev.close : null)}">${fmt(b.low)}</em></span>
        <span>收 <em style="color:${col(chg)}">${fmt(b.close)}</em></span>
        <span style="color:${col(chg)}">${chg == null ? '' : (chg >= 0 ? '▲' : '▼') + Math.abs(chg).toFixed(2) + '%'}</span>
        <span>量 ${fmt(b.volume, intraday && b.volume < 100 ? 1 : 0)} 張</span></div><div class="ki-row ki-ma">${lines}</div>`;
    }
    chart.subscribeCrosshairMove((p) => {
      if (!p || !p.time || p.point === undefined || p.point.x < 0) { info(bars.length - 1); return; }
      const i = idx[tkey(p.time)]; info(i != null ? i : bars.length - 1);
    });

    return {
      chart, candle,
      setBars(list, o = {}) {
        bars = list; intraday = !!o.intraday; idx = {};
        bars.forEach((b, i) => { idx[tkey(b.time)] = i; b._k = i; });
        chart.applyOptions({ timeScale: { timeVisible: intraday, secondsVisible: false } });
        candle.setData(bars.map(({ time, open, high, low, close }) => ({ time, open, high, low, close })));
        if (vol) vol.setData(bars.map((b, i) => ({ time: b.time, value: b.volume || 0,
          color: (b.close >= (i ? bars[i - 1].close : b.open) ? C.up : C.down) + '66' })));
        maData = mas.map((n, k) => { const d = sma(bars, n); maSeries[k].setData(d); const arr = Array(bars.length).fill(null);
          d.forEach(p => { arr[idx[tkey(p.time)]] = p.value; }); return arr; });
        candle.setMarkers([]);
        info(bars.length - 1);
      },
      // 額外的線（例如布林通道、突破價位）：[{name, color, dash, data:[{time,value}]}]
      setLines(list) {
        extra.forEach(s => chart.removeSeries(s)); extra = []; extraData = [];
        for (const l of list) {
          const s = chart.addLineSeries({ color: l.color, lineWidth: 1, lineStyle: l.dash ? 2 : 0, priceLineVisible: false, lastValueVisible: false, crosshairMarkerVisible: false });
          const data = l.data.filter(p => p.value != null); s.setData(data); extra.push(s);
          extraData.push({ name: l.name, color: l.color, map: Object.fromEntries(data.map(p => [tkey(p.time), p.value])) });
        }
      },
      setMarkers(list) { candle.setMarkers(list.slice().sort((a, b) => (idx[tkey(a.time)] ?? 0) - (idx[tkey(b.time)] ?? 0))); },
      // 買點到賣點的連線：[{t0,v0,t1,v1,color,width}]
      setSegments(list) {
        segs.forEach(s => chart.removeSeries(s)); segs = [];
        for (const g of list) {
          const s = chart.addLineSeries({ color: g.color, lineWidth: g.width || 2, lineStyle: 2, priceLineVisible: false, lastValueVisible: false, crosshairMarkerVisible: false });
          s.setData([{ time: g.t0, value: g.v0 }, { time: g.t1, value: g.v1 }]); segs.push(s);
        }
      },
      showLast(n) {
        const ts = chart.timeScale();
        if (!n || n >= bars.length) ts.fitContent(); else ts.setVisibleLogicalRange({ from: bars.length - n, to: bars.length + 3 });
      },
      focus(t0, t1, padBars = 20) {
        const a = idx[tkey(t0)], b = idx[tkey(t1)];
        if (a == null || b == null) return;
        chart.timeScale().setVisibleLogicalRange({ from: a - padBars, to: b + padBars });
      },
      indexOf: (t) => idx[tkey(t)],
      get bars() { return bars; },
    };
  }

  window.KChart = { create, fromDaily, fromIntraday, aggMinutes, aggPeriod, sma, tkey,
    TF: [['1', '1分'], ['5', '5分'], ['15', '15分'], ['30', '30分'], ['60', '60分'], ['D', '日'], ['W', '週'], ['M', '月']] };
})();
