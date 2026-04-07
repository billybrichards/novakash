import React, { useRef, useEffect } from 'react';

/**
 * CanvasRiskSurface — Animated 3D wireframe surface showing VPIN x Delta parameter space.
 *
 * Props:
 *   currentT — Countdown seconds, used to position the evaluation plane
 */
export default function CanvasRiskSurface({ currentT }) {
  const canvasRef = useRef(null);

  useEffect(() => {
    const canvas = canvasRef.current;
    const ctx = canvas.getContext('2d');
    let animationFrameId;
    let rotation = 0;

    const draw = () => {
      const width = canvas.width = canvas.offsetWidth;
      const height = canvas.height = canvas.offsetHeight;

      ctx.clearRect(0, 0, width, height);
      ctx.fillStyle = '#0f172a';
      ctx.fillRect(0, 0, width, height);

      const cx = width / 2;
      const cy = height / 2 + 20;

      rotation += 0.005;
      const scale = Math.min(width, height) * 0.35;
      const gridSize = 12;
      const points = [];

      for (let x = -gridSize; x <= gridSize; x += 2) {
        for (let z = -gridSize; z <= gridSize; z += 2) {
          const y = Math.sin(x * 0.5 + rotation) * Math.cos(z * 0.5 - rotation) * 3 + Math.sin(x * z * 0.1) * 2;
          const rotX = x * Math.cos(rotation) - z * Math.sin(rotation);
          const rotZ = x * Math.sin(rotation) + z * Math.cos(rotation);

          const px = cx + (rotX - rotZ) * scale * 0.1;
          const py = cy + (rotX + rotZ) * scale * 0.05 - (y * scale * 0.05);

          points.push({ px, py, y });
        }
      }

      // Draw points and connections
      ctx.lineWidth = 1;
      points.forEach((p, i) => {
        ctx.beginPath();
        ctx.arc(p.px, p.py, 1.5, 0, Math.PI * 2);
        ctx.fillStyle = p.y > 1.5 ? 'rgb(255, 50, 50)' : p.y < -1.5 ? 'rgb(50, 255, 200)' : `rgb(50, 100, ${Math.floor(((p.y + 5) / 10) * 255)})`;
        ctx.fill();

        if (i > 0 && i % (gridSize + 1) !== 0) {
          ctx.beginPath();
          ctx.moveTo(points[i - 1].px, points[i - 1].py);
          ctx.lineTo(p.px, p.py);
          ctx.strokeStyle = `rgba(0, 255, 204, ${0.1 + (p.y + 5) / 20})`;
          ctx.stroke();
        }
      });

      // Evaluation plane
      const progress = (240 - currentT) / 180;
      ctx.beginPath();
      ctx.moveTo(cx - 100, cy - 80 + (progress * 160));
      ctx.lineTo(cx + 100, cy - 80 + (progress * 160));
      ctx.strokeStyle = '#ff003c';
      ctx.setLineDash([4, 4]);
      ctx.stroke();
      ctx.setLineDash([]);

      // Labels
      ctx.fillStyle = '#00ffcc';
      ctx.font = '10px monospace';
      ctx.fillText('Z: VPIN_CASCADE (0.70)', 10, 20);
      ctx.fillText('X: DELTA_PCT', 10, 35);
      ctx.fillText('Y: PARAM_ODE_OPT', 10, 50);

      animationFrameId = requestAnimationFrame(draw);
    };

    draw();
    return () => cancelAnimationFrame(animationFrameId);
  }, [currentT]);

  return <canvas ref={canvasRef} style={{ width: '100%', height: '100%', borderRadius: 2 }} />;
}
