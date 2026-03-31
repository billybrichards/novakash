/** @type {import('tailwindcss').Config} */
export default {
  content: ['./index.html', './src/**/*.{js,jsx,ts,tsx}'],
  darkMode: 'class',
  theme: {
    extend: {
      colors: {
        // ── Dark Trading Theme ──
        background: {
          DEFAULT: '#07070c',
          secondary: '#0d0d15',
          elevated: '#12121e',
        },
        surface: {
          DEFAULT: '#16162a',
          hover: '#1e1e35',
          border: '#2a2a45',
        },
        accent: {
          green: '#00ff88',
          'green-dim': '#00cc6a',
          red: '#ff4466',
          'red-dim': '#cc3355',
          blue: '#4488ff',
          'blue-dim': '#3366cc',
          yellow: '#ffcc00',
          purple: '#9966ff',
        },
        text: {
          primary: '#e8e8f0',
          secondary: '#9898b8',
          muted: '#5a5a7a',
        },
      },
      fontFamily: {
        mono: ['JetBrains Mono', 'Fira Code', 'Consolas', 'monospace'],
        sans: ['Inter', 'system-ui', 'sans-serif'],
      },
      animation: {
        'pulse-slow': 'pulse 3s cubic-bezier(0.4, 0, 0.6, 1) infinite',
        'fade-in': 'fadeIn 200ms ease-out',
        'slide-up': 'slideUp 250ms ease-out',
      },
      keyframes: {
        fadeIn: {
          '0%': { opacity: '0', transform: 'scale(0.95)' },
          '100%': { opacity: '1', transform: 'scale(1)' },
        },
        slideUp: {
          '0%': { opacity: '0', transform: 'translateY(8px)' },
          '100%': { opacity: '1', transform: 'translateY(0)' },
        },
      },
      boxShadow: {
        'glow-green': '0 0 20px rgba(0, 255, 136, 0.15)',
        'glow-red': '0 0 20px rgba(255, 68, 102, 0.15)',
        'glow-blue': '0 0 20px rgba(68, 136, 255, 0.15)',
      },
    },
  },
  plugins: [],
}
