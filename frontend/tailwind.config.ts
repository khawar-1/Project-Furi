import type { Config } from 'tailwindcss';

const config: Config = {
  content: ['./index.html', './src/**/*.{ts,tsx}'],
  theme: {
    extend: {
      colors: {
        // ---- Jarvis OS Dark Palette
        surface: {
          DEFAULT: '#0A0A0F',
          1: '#0F0F17',
          2: '#14141E',
          3: '#1A1A26',
          4: '#20202E',
          border: '#2A2A3E',
        },
        // ---- Cyan accent (primary)
        cyan: {
          DEFAULT: '#06B6D4',
          50: '#ECFEFF',
          100: '#CFFAFE',
          200: '#A5F3FC',
          300: '#67E8F9',
          400: '#22D3EE',
          500: '#06B6D4',
          600: '#0891B2',
          700: '#0E7490',
          800: '#155E75',
          900: '#164E63',
          glow: 'rgba(6, 182, 212, 0.15)',
          'glow-md': 'rgba(6, 182, 212, 0.25)',
          'glow-lg': 'rgba(6, 182, 212, 0.4)',
        },
        // ---- Blue accent (secondary)
        blue: {
          DEFAULT: '#3B82F6',
          glow: 'rgba(59, 130, 246, 0.15)',
          'glow-md': 'rgba(59, 130, 246, 0.25)',
        },
        // ---- Status colors
        success: '#10B981',
        warning: '#F59E0B',
        danger: '#EF4444',
        muted: '#64748B',
      },
      fontFamily: {
        sans: ['Inter', 'system-ui', 'sans-serif'],
        mono: ['JetBrains Mono', 'Fira Code', 'Consolas', 'monospace'],
      },
      animation: {
        'fade-in': 'fadeIn 0.2s ease-out',
        'slide-up': 'slideUp 0.3s ease-out',
        'pulse-cyan': 'pulseCyan 2s ease-in-out infinite',
        'glow-pulse': 'glowPulse 2s ease-in-out infinite',
        'typing': 'typing 1.4s ease-in-out infinite',
        'stream-in': 'streamIn 0.15s ease-out',
        // ---- Voice-mode sphere. ROTATION ONLY, and deliberately so: it is
        // compositor-work, and the reduced-motion rule in index.css can switch
        // it all off with one selector. The level response and the idle breathe
        // are driven by the orb's rAF loop through CSS variables instead, so
        // they can share a single transform without fighting a keyframe.
        'orb-spin': 'orbSpin 60s linear infinite',
        'orb-spin-fast': 'orbSpin 24s linear infinite',
        'orb-spin-reverse': 'orbSpinReverse 90s linear infinite',
      },
      keyframes: {
        fadeIn: {
          from: { opacity: '0' },
          to: { opacity: '1' },
        },
        slideUp: {
          from: { opacity: '0', transform: 'translateY(8px)' },
          to: { opacity: '1', transform: 'translateY(0)' },
        },
        pulseCyan: {
          '0%, 100%': { opacity: '1' },
          '50%': { opacity: '0.5' },
        },
        glowPulse: {
          '0%, 100%': { boxShadow: '0 0 8px rgba(6, 182, 212, 0.3)' },
          '50%': { boxShadow: '0 0 20px rgba(6, 182, 212, 0.6)' },
        },
        typing: {
          '0%, 60%, 100%': { opacity: '0' },
          '30%': { opacity: '1' },
        },
        streamIn: {
          from: { opacity: '0' },
          to: { opacity: '1' },
        },
        orbSpin: {
          from: { transform: 'rotate(0deg)' },
          to: { transform: 'rotate(360deg)' },
        },
        orbSpinReverse: {
          from: { transform: 'rotate(360deg)' },
          to: { transform: 'rotate(0deg)' },
        },
      },
      boxShadow: {
        'glow-cyan': '0 0 12px rgba(6, 182, 212, 0.4)',
        'glow-blue': '0 0 12px rgba(59, 130, 246, 0.4)',
        'glow-sm': '0 0 6px rgba(6, 182, 212, 0.25)',
        'inner-top': 'inset 0 1px 0 rgba(255,255,255,0.05)',
      },
      borderColor: {
        DEFAULT: '#2A2A3E',
      },
    },
  },
  plugins: [],
};

export default config;
