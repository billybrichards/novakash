import React, { useRef, useEffect } from 'react';

/**
 * CanvasPriceChart — Candlestick chart for past windows + live price line for current window.
 *
 * Props:
 *   currentT       — Countdown seconds (240 → 60)
 *   currentPrices  — [{t, price}] array for the live window
 *   pastCandles    — [{open, high, low, close}] for completed windows
 */
export default function CanvasPriceChart({ currentT, currentPrices, pastCandles }) {
  const canvasRef = useRef(null);

  useEffect(() => {
    const canvas = canvasRef.current;
    if (!canvas) return;
    const ctx = canvas.getContext('2d');
    const width = canvas.width = canvas.offsetWidth;
    const height = canvas.height = canvas.offsetHeight;

    ctx.clearRect(0, 0, width, height);

    let allPrices = pastCandles.flatMap(c => [c.high, c.low]);
    if (currentPrices.length > 0) {
      allPrices = [...allPrices, ...currentPrices.map(p => p.price)];
    }
    if (allPrices.length === 0) return;

    const rawMin = Math.min(...allPrices);
    const rawMax = Math.max(...allPrices);
    const padding = (rawMax - rawMin) * 0.1 || 0.01;
    const minP = rawMin - padding;
    const maxP = rawMax + padding;

    const getY = (price) => height - 15 - ((price - minP) / (maxP - minP)) * (height - 30);

    // Grid lines
    ctx.strokeStyle = '#1e293b';
    ctx.lineWidth = 1;
    ctx.fillStyle = '#64748b';
    ctx.font = '9px monospace';
    ctx.textAlign = 'right';

    for (let i = 0; i <= 4; i++) {
      const p = minP + (maxP - minP) * (i / 4);
      const y = getY(p);
      ctx.beginPath();
      ctx.moveTo(0, y);
      ctx.lineTo(width - 40, y);
      ctx.stroke();
      ctx.fillText(p.toFixed(3), width - 5, y + 3);
    }

    // Past candles
    const candleSpacing = 16;
    const candleWidth = 6;
    const chartLeft = 10;

    pastCandles.forEach((candle, i) => {
      const x = chartLeft + i * candleSpacing;
      const isBull = candle.close >= candle.open;

      ctx.strokeStyle = isBull ? '#10b981' : '#ef4444';
      ctx.fillStyle = isBull ? '#10b981' : '#ef4444';

      ctx.beginPath();
      ctx.moveTo(x + candleWidth / 2, getY(candle.high));
      ctx.lineTo(x + candleWidth / 2, getY(candle.low));
      ctx.stroke();

      const yOpen = getY(candle.open);
      const yClose = getY(candle.close);
      ctx.fillRect(x, Math.min(yOpen, yClose), candleWidth, Math.abs(yOpen - yClose) || 1);
    });

    // Current window zone
    const currentWindowStartX = chartLeft + pastCandles.length * candleSpacing + 20;
    const currentWindowWidth = width - 40 - currentWindowStartX;

    ctx.fillStyle = 'rgba(6, 182, 212, 0.05)';
    ctx.fillRect(currentWindowStartX, 0, currentWindowWidth, height);

    ctx.fillStyle = '#06b6d4';
    ctx.textAlign = 'left';
    ctx.fillText('LIVE 5M WINDOW', currentWindowStartX + 5, 12);

    // Live price line
    if (currentPrices.length > 0) {
      ctx.beginPath();
      ctx.strokeStyle = '#22d3ee';
      ctx.lineWidth = 2;
      ctx.lineJoin = 'round';
      ctx.shadowColor = '#22d3ee';
      ctx.shadowBlur = 8;

      currentPrices.forEach((point, i) => {
        const progress = (240 - point.t) / 180;
        const x = currentWindowStartX + progress * currentWindowWidth;
        const y = getY(point.price);
        if (i === 0) ctx.moveTo(x, y);
        else ctx.lineTo(x, y);
      });
      ctx.stroke();

      // Current price dot
      const lastPoint = currentPrices[currentPrices.length - 1];
      const lastProgress = (240 - lastPoint.t) / 180;
      const lastX = currentWindowStartX + lastProgress * currentWindowWidth;
      const lastY = getY(lastPoint.price);

      ctx.beginPath();
      ctx.arc(lastX, lastY, 3, 0, Math.PI * 2);
      ctx.fillStyle = '#fff';
      ctx.fill();
      ctx.shadowBlur = 0;

      // Price level line
      ctx.beginPath();
      ctx.setLineDash([2, 2]);
      ctx.strokeStyle = 'rgba(255,255,255,0.3)';
      ctx.moveTo(0, lastY);
      ctx.lineTo(width, lastY);
      ctx.stroke();
      ctx.setLineDash([]);
    }
  }, [currentT, currentPrices, pastCandles]);

  return <canvas ref={canvasRef} style={{ width: '100%', height: '100%', borderRadius: 2 }} />;
}
