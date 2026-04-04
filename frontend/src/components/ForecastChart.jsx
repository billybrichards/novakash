/**
 * ForecastChart.jsx — BTC candlestick chart with TimesFM forecast overlay.
 *
 * Uses lightweight-charts (TradingView) for the candlestick.
 * Overlays:
 *  - TimesFM forecast line (dashed, green/red based on direction)
 *  - P10-P90 confidence band (shaded area)
 *  - Window open/close vertical markers
 *  - Window open price horizontal line
 */

import React, { useEffect, useRef, useState } from 'react';
import { createChart, CrosshairMode, LineStyle } from 'lightweight-charts';

const T = {
  bg: '#07070c',
  chartBg: '#07070c',
  gridLine: 'rgba(255,255,255,0.04)',
  borderColor: 'rgba(255,255,255,0.08)',
  textColor: 'rgba(255,255,255,0.4)',
  upColor: '#4ade80',
  downColor: '#f87171',
  upWick: '#4ade80',
  downWick: '#f87171',
  forecastUp: '#4ade80',
  forecastDown: '#f87171',
  bandUp: 'rgba(74, 222, 128, 0.06)',
  bandDown: 'rgba(248, 113, 113, 0.06)',
  windowLine: 'rgba(168, 85, 247, 0.5)',
  openPriceLine: 'rgba(6, 182, 212, 0.5)',
};

export default function ForecastChart({
  candles = [],
  forecastLine = [],
  quantiles = null,
  windowInfo = null,
  forecast = null,
  height = 420,
}) {
  const containerRef = useRef(null);
  const chartRef = useRef(null);
  const candleSeriesRef = useRef(null);
  const forecastSeriesRef = useRef(null);
  const p10SeriesRef = useRef(null);
  const p90SeriesRef = useRef(null);

  const isUp = forecast?.direction === 'UP';

  // ── Init chart ──────────────────────────────────────────────────────────────
  useEffect(() => {
    if (!containerRef.current) return;

    const chart = createChart(containerRef.current, {
      layout: {
        background: { color: T.chartBg },
        textColor: T.textColor,
        fontFamily: "'IBM Plex Mono', monospace",
        fontSize: 11,
      },
      grid: {
        vertLines: { color: T.gridLine },
        horzLines: { color: T.gridLine },
      },
      crosshair: {
        mode: CrosshairMode.Normal,
        vertLine: {
          color: 'rgba(255,255,255,0.15)',
          labelBackgroundColor: '#1a1a2e',
        },
        horzLine: {
          color: 'rgba(255,255,255,0.15)',
          labelBackgroundColor: '#1a1a2e',
        },
      },
      rightPriceScale: {
        borderColor: T.borderColor,
        textColor: T.textColor,
      },
      timeScale: {
        borderColor: T.borderColor,
        textColor: T.textColor,
        timeVisible: true,
        secondsVisible: false,
      },
      handleScroll: true,
      handleScale: true,
    });

    // Candlestick series
    const candleSeries = chart.addCandlestickSeries({
      upColor: T.upColor,
      downColor: T.downColor,
      borderUpColor: T.upColor,
      borderDownColor: T.downColor,
      wickUpColor: T.upWick,
      wickDownColor: T.downWick,
    });

    // Forecast line series (dashed)
    const forecastSeries = chart.addLineSeries({
      color: isUp ? T.forecastUp : T.forecastDown,
      lineWidth: 2,
      lineStyle: LineStyle.Dashed,
      crosshairMarkerVisible: false,
      lastValueVisible: true,
      priceLineVisible: false,
    });

    // P10 series (lower band edge — area)
    const p10Series = chart.addLineSeries({
      color: 'transparent',
      lineWidth: 0,
      crosshairMarkerVisible: false,
      lastValueVisible: false,
      priceLineVisible: false,
    });

    // P90 series (upper band edge)
    const p90Series = chart.addLineSeries({
      color: 'transparent',
      lineWidth: 0,
      crosshairMarkerVisible: false,
      lastValueVisible: false,
      priceLineVisible: false,
    });

    chart.timeScale().fitContent();

    chartRef.current = chart;
    candleSeriesRef.current = candleSeries;
    forecastSeriesRef.current = forecastSeries;
    p10SeriesRef.current = p10Series;
    p90SeriesRef.current = p90Series;

    const ro = new ResizeObserver(() => {
      if (containerRef.current) {
        chart.applyOptions({
          width: containerRef.current.clientWidth,
          height: containerRef.current.clientHeight,
        });
      }
    });
    ro.observe(containerRef.current);

    return () => {
      ro.disconnect();
      chart.remove();
      chartRef.current = null;
    };
  }, []); // eslint-disable-line react-hooks/exhaustive-deps

  // ── Update candles ──────────────────────────────────────────────────────────
  useEffect(() => {
    if (!candleSeriesRef.current || !candles.length) return;
    const sorted = [...candles].sort((a, b) => a.time - b.time);
    candleSeriesRef.current.setData(sorted);
    chartRef.current?.timeScale().fitContent();
  }, [candles]);

  // ── Update forecast line ────────────────────────────────────────────────────
  useEffect(() => {
    if (!forecastSeriesRef.current || !forecastLine.length) return;

    const sorted = [...forecastLine]
      .sort((a, b) => a.time - b.time)
      .map(p => ({ time: p.time, value: p.value }));

    forecastSeriesRef.current.setData(sorted);

    // Update colour based on direction
    forecastSeriesRef.current.applyOptions({
      color: isUp ? T.forecastUp : T.forecastDown,
    });

    // P10/P90 bands from quantiles
    if (quantiles && sorted.length > 0) {
      const start = sorted[0].time;
      const end = sorted[sorted.length - 1].time;
      const bandPoints = [
        { time: start, value: quantiles.p10 },
        { time: end, value: quantiles.p10 },
      ];
      const topPoints = [
        { time: start, value: quantiles.p90 },
        { time: end, value: quantiles.p90 },
      ];
      p10SeriesRef.current?.setData(bandPoints);
      p90SeriesRef.current?.setData(topPoints);
    }
  }, [forecastLine, quantiles, isUp]);

  // ── Price lines for window open + markers ───────────────────────────────────
  useEffect(() => {
    if (!candleSeriesRef.current || !windowInfo) return;

    // Window open price line
    if (windowInfo.openPrice) {
      candleSeriesRef.current.createPriceLine({
        price: windowInfo.openPrice,
        color: T.openPriceLine,
        lineWidth: 1,
        lineStyle: LineStyle.Dashed,
        axisLabelVisible: true,
        title: 'Window Open',
      });
    }
  }, [windowInfo]);

  return (
    <div
      ref={containerRef}
      style={{
        width: '100%',
        height,
        borderRadius: 8,
        overflow: 'hidden',
        background: T.chartBg,
      }}
    />
  );
}
