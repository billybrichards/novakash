import React, { useRef, useEffect } from 'react';

/**
 * CanvasRetrospective — Missed opportunity matrix showing price action + gate logic per checkpoint.
 *
 * Props:
 *   data — Array of checkpoint data: [{t, v2Agree, delta, vpin, regime, reason, price}]
 *          Must include a t=0 entry for the resolution point.
 *   deltaThreshold — The delta threshold used (default 0.02%)
 */
export default function CanvasRetrospective({ data, deltaThreshold = 0.0200 }) {
  const canvasRef = useRef(null);

  useEffect(() => {
    const canvas = canvasRef.current;
    if (!canvas || !data || data.length < 2) return;
    const ctx = canvas.getContext('2d');
    const width = canvas.width = canvas.offsetWidth;
    const height = canvas.height = canvas.offsetHeight;

    ctx.clearRect(0, 0, width, height);

    const topHeight = height * 0.60;
    const matrixTop = topHeight + 10;
    const bottomHeight = height - matrixTop;
    const rowHeight = bottomHeight / 3;

    const chartLeft = 70;
    const chartRight = width - 50;
    const chartWidth = chartRight - chartLeft;

    const prices = data.map(d => d.price).filter(Boolean);
    if (prices.length === 0) return;
    const minPrice = Math.min(...prices) - 20;
    const maxPrice = Math.max(...prices) + 50;

    const maxT = Math.max(...data.map(d => d.t));
    const getX = (t) => chartLeft + ((maxT - t) / maxT) * chartWidth;
    const getYPrice = (p) => 20 + ((maxPrice - p) / (maxPrice - minPrice)) * (topHeight - 40);

    // Background panes
    ctx.fillStyle = '#0a0f1c';
    ctx.fillRect(chartLeft, 0, chartWidth, topHeight);

    ctx.fillStyle = '#0f172a';
    ctx.fillRect(chartLeft, matrixTop, chartWidth, rowHeight);
    ctx.fillStyle = '#0b1120';
    ctx.fillRect(chartLeft, matrixTop + rowHeight, chartWidth, rowHeight);
    ctx.fillStyle = '#0f172a';
    ctx.fillRect(chartLeft, matrixTop + rowHeight * 2, chartWidth, rowHeight);

    // Row labels
    ctx.fillStyle = '#94a3b8';
    ctx.textAlign = 'right';
    ctx.font = 'bold 9px monospace';
    ctx.fillText('v2.2 GATE', chartLeft - 10, matrixTop + rowHeight / 2 + 3);
    ctx.fillText(`DELTA > ${(deltaThreshold * 100).toFixed(2)}%`, chartLeft - 10, matrixTop + rowHeight * 1.5 + 3);
    ctx.fillText('DECISION', chartLeft - 10, matrixTop + rowHeight * 2.5 + 3);

    // Separator
    ctx.beginPath();
    ctx.moveTo(chartLeft, topHeight);
    ctx.lineTo(chartRight, topHeight);
    ctx.strokeStyle = '#1e293b';
    ctx.lineWidth = 2;
    ctx.stroke();

    // Y-axis labels
    ctx.fillStyle = '#64748b';
    ctx.textAlign = 'right';
    ctx.font = '9px monospace';
    ctx.fillText(maxPrice.toFixed(0), chartLeft - 5, 20);
    ctx.fillText(minPrice.toFixed(0), chartLeft - 5, topHeight - 10);

    // Price line
    ctx.beginPath();
    ctx.strokeStyle = '#fff';
    ctx.lineWidth = 2;
    ctx.shadowColor = 'rgba(255,255,255,0.5)';
    ctx.shadowBlur = 4;

    data.forEach((point, i) => {
      if (!point.price) return;
      const x = getX(point.t);
      const y = getYPrice(point.price);
      if (i === 0) ctx.moveTo(x, y);
      else ctx.lineTo(x, y);
    });
    ctx.stroke();
    ctx.shadowBlur = 0;

    // Find closest miss (highest delta that still failed)
    const candidates = data.filter(d => d.t > 0 && d.v2Agree && d.delta !== null && d.delta < deltaThreshold);
    const closestMiss = candidates.length > 0
      ? candidates.reduce((best, d) => (!best || d.delta > best.delta) ? d : best, null)
      : null;
    const closestT = closestMiss?.t;

    // Gate matrix blocks
    const boxW = Math.max(chartWidth / (data.length * 1.5) - 2, 8);

    data.forEach((point) => {
      if (point.t === 0) return;
      const x = getX(point.t);
      const isClosest = point.t === closestT;

      // Vertical sync line
      ctx.strokeStyle = isClosest ? 'rgba(34, 211, 238, 0.4)' : 'rgba(255, 255, 255, 0.05)';
      ctx.lineWidth = isClosest ? 2 : 1;
      if (isClosest) ctx.setLineDash([4, 4]);
      ctx.beginPath();
      ctx.moveTo(x, point.price ? getYPrice(point.price) : topHeight);
      ctx.lineTo(x, height);
      ctx.stroke();
      ctx.setLineDash([]);

      // Price node
      if (point.price && (isClosest || point.t % 60 === 0)) {
        ctx.beginPath();
        ctx.arc(x, getYPrice(point.price), isClosest ? 5 : 3, 0, Math.PI * 2);
        ctx.fillStyle = isClosest ? '#22d3ee' : '#64748b';
        ctx.fill();
        if (isClosest) {
          ctx.strokeStyle = '#fff';
          ctx.lineWidth = 1;
          ctx.stroke();
          ctx.fillStyle = '#22d3ee';
          ctx.textAlign = 'center';
          ctx.fillText(`$${point.price.toFixed(0)}`, x, getYPrice(point.price) - 15);
        }
      }

      // Row 1: v2.2 Logic Block
      ctx.fillStyle = point.v2Agree ? '#10b981' : '#ef4444';
      ctx.fillRect(x - boxW / 2, matrixTop + 4, boxW, rowHeight - 8);
      ctx.fillStyle = '#fff';
      ctx.textAlign = 'center';
      ctx.font = 'bold 8px monospace';
      ctx.fillText(point.v2Agree ? 'PASS' : 'FAIL', x, matrixTop + rowHeight / 2 + 3);

      // Row 2: Delta Logic Block
      let deltaColor = '#1e293b';
      let deltaText = 'N/A';

      if (point.delta !== null) {
        const deltaPass = point.delta >= deltaThreshold;
        deltaColor = deltaPass ? '#10b981' : (isClosest ? '#f59e0b' : '#ef4444');
        deltaText = `${(point.delta * 100).toFixed(2)}%`;
      }

      ctx.fillStyle = deltaColor;
      ctx.fillRect(x - boxW / 2, matrixTop + rowHeight + 4, boxW, rowHeight - 8);
      ctx.fillStyle = deltaColor === '#1e293b' ? '#64748b' : '#fff';
      ctx.fillText(deltaText, x, matrixTop + rowHeight * 1.5 + 3);

      // Row 3: Decision Block
      const tradePass = point.v2Agree && (point.delta !== null && point.delta >= deltaThreshold);
      ctx.fillStyle = tradePass ? '#10b981' : '#ef4444';
      ctx.fillRect(x - boxW / 2, matrixTop + rowHeight * 2 + 4, boxW, rowHeight - 8);
      ctx.fillStyle = '#fff';
      ctx.fillText(tradePass ? 'TRADE' : 'SKIP', x, matrixTop + rowHeight * 2.5 + 3);

      // Time labels
      if (point.t % 30 === 0 || isClosest) {
        ctx.fillStyle = isClosest ? '#22d3ee' : '#64748b';
        ctx.fillText(`T-${point.t}`, x, height - 5);
      }
    });

    // Resolution annotation
    const resPoint = data.find(d => d.t === 0);
    if (resPoint && resPoint.price && closestMiss && closestMiss.price) {
      const resX = getX(0);
      const resY = getYPrice(resPoint.price);
      const closestX = getX(closestT);
      const closestY = getYPrice(closestMiss.price);

      // Dashed line from closest entry to resolution
      ctx.beginPath();
      ctx.moveTo(closestX, closestY);
      ctx.lineTo(resX, resY);
      ctx.strokeStyle = '#10b981';
      ctx.setLineDash([4, 4]);
      ctx.lineWidth = 2;
      ctx.stroke();
      ctx.setLineDash([]);

      // Annotation
      ctx.fillStyle = '#10b981';
      ctx.textAlign = 'right';
      ctx.font = 'bold 12px monospace';
      ctx.fillText('MISSED WIN', resX - 10, resY - 10);
      ctx.font = '9px monospace';
      ctx.fillText('Actual Resolution', resX - 10, resY + 5);
    }
  }, [data, deltaThreshold]);

  return <canvas ref={canvasRef} style={{ width: '100%', height: '100%', borderRadius: 2 }} />;
}
