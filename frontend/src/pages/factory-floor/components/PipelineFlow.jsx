import React from 'react';

/**
 * PipelineFlow -- Animated connecting lines between pipeline stages.
 *
 * Renders a vertical connector (mobile) or horizontal connector (desktop)
 * with flowing dots that indicate data movement between stages.
 */

export default function PipelineFlow({
  color = '#00ccff',
  active = true,
  vertical = false,
  label,
}) {
  const dim = active ? 1.0 : 0.3;

  if (vertical) {
    return (
      <div style={{
        display: 'flex',
        flexDirection: 'column',
        alignItems: 'center',
        justifyContent: 'center',
        height: 32,
        position: 'relative',
        overflow: 'hidden',
        flexShrink: 0,
        opacity: dim,
      }}>
        {/* Vertical line */}
        <div style={{
          position: 'absolute',
          left: '50%',
          top: 4,
          bottom: 4,
          width: 1,
          background: `${color}44`,
        }} />
        {/* Flowing dots */}
        {active && [0, 1, 2].map(i => (
          <div key={i} style={{
            position: 'absolute',
            left: '50%',
            top: 0,
            width: 4,
            height: 4,
            borderRadius: '50%',
            background: color,
            transform: 'translateX(-50%)',
            animation: `ffFlowVert 1.8s ${i * 0.6}s ease-in-out infinite`,
            opacity: 0,
          }} />
        ))}
        {/* Label */}
        {label && (
          <span style={{
            position: 'absolute',
            right: 'calc(50% + 10px)',
            fontSize: 7,
            color: `${color}88`,
            letterSpacing: '0.06em',
            fontFamily: "'JetBrains Mono', monospace",
            whiteSpace: 'nowrap',
          }}>
            {label}
          </span>
        )}
      </div>
    );
  }

  return (
    <div style={{
      display: 'flex',
      alignItems: 'center',
      justifyContent: 'center',
      width: 40,
      flexShrink: 0,
      position: 'relative',
      overflow: 'hidden',
      opacity: dim,
    }}>
      {/* Horizontal line */}
      <div style={{
        position: 'absolute',
        top: '50%',
        left: 4,
        right: 4,
        height: 1,
        background: `${color}44`,
      }} />
      {/* Flowing dots */}
      {active && [0, 1, 2].map(i => (
        <div key={i} style={{
          position: 'absolute',
          top: '50%',
          left: 0,
          width: 4,
          height: 4,
          borderRadius: '50%',
          background: color,
          transform: 'translateY(-50%)',
          animation: `ffFlowHoriz 1.8s ${i * 0.6}s ease-in-out infinite`,
          opacity: 0,
        }} />
      ))}
      {/* Label */}
      {label && (
        <span style={{
          position: 'absolute',
          bottom: 'calc(50% + 8px)',
          fontSize: 7,
          color: `${color}88`,
          letterSpacing: '0.06em',
          fontFamily: "'JetBrains Mono', monospace",
          whiteSpace: 'nowrap',
        }}>
          {label}
        </span>
      )}
    </div>
  );
}
