/**
 * FURI — HUD Voice Orb
 *
 * A cinematic AI orb in the style of classic sci-fi interfaces:
 *
 *   • Dark spherical core with "FURI" centred in clean tracked lettering
 *   • Glowing cyan ring with a flowing energy hotspot that orbits the ring
 *   • Two white arcs that revolve around the ring — speeding up when the
 *     user or FURI is speaking, slowing to near-idle when quiet
 *   • 36 HUD tick marks distributed around the ring circumference
 *   • Ambient starfield scattered across the full canvas (2.8× the container)
 *
 * ─── PERFORMANCE ──────────────────────────────────────────────────────────────
 * • ONE rAF loop per mount. React re-renders only when `state` changes.
 * • All mutable animation state lives in plain variables (not React state).
 * • Canvas is 2.8× the container so dispersing elements never clip at the edge.
 */
import { useEffect, useRef } from 'react';
import { clsx } from 'clsx';
import { useVoiceStore } from '@/stores/voiceStore';
import { getOutputLevel } from '@/lib/voiceOutput';

export type OrbState = 'idle' | 'listening' | 'thinking' | 'speaking' | 'error';

// ─── State → colour mapping ───────────────────────────────────────────────────

interface RGB { r: number; g: number; b: number; }

const STATE_COLOR: Record<OrbState, RGB> = {
  idle:      { r: 34,  g: 211, b: 238 },   // cyan-400
  listening: { r: 125, g: 235, b: 252 },   // lighter cyan
  thinking:  { r: 99,  g: 102, b: 241 },   // indigo-500
  speaking:  { r: 220, g: 252, b: 255 },   // near-white cyan
  error:     { r: 239, g: 68,  b: 68  },   // red-500
};

function lerp(a: number, b: number, t: number) { return a + (b - a) * t; }

// ─── Background stars ─────────────────────────────────────────────────────────

interface Star {
  x: number; y: number;
  size: number;
  baseOpacity: number;
  twinklePhase: number;
  twinkleSpeed: number;
}

function makeStars(W: number, H: number, n: number): Star[] {
  const out: Star[] = [];
  for (let i = 0; i < n; i++) {
    out.push({
      x: Math.random() * W,
      y: Math.random() * H,
      size: 0.4 + Math.random() * 1.8,
      baseOpacity: 0.12 + Math.random() * 0.5,
      twinklePhase: Math.random() * Math.PI * 2,
      twinkleSpeed: 0.006 + Math.random() * 0.018,
    });
  }
  return out;
}

// ─── Component ────────────────────────────────────────────────────────────────

interface VoiceOrbProps {
  state: OrbState;
  className?: string;
}

export function VoiceOrb({ state, className }: VoiceOrbProps) {
  const containerRef = useRef<HTMLDivElement>(null);
  const canvasRef    = useRef<HTMLCanvasElement>(null);
  const stateRef     = useRef<OrbState>(state);

  useEffect(() => { stateRef.current = state; }, [state]);

  useEffect(() => {
    const canvas    = canvasRef.current;
    const container = containerRef.current;
    if (!canvas || !container) return;
    const ctx = canvas.getContext('2d');
    if (!ctx) return;

    // ── Animation state ─────────────────────────────────────────────────────
    let raf       = 0;
    let stars: Star[] = [];
    let arc1Angle = 0;              // main long arc
    let arc2Angle = Math.PI * 0.6;  // secondary short arc (phase offset)
    let hotAngle  = Math.PI * 1.4;  // energy hotspot on the ring
    let smoothed  = 0;              // smoothed voice level
    let curR      = STATE_COLOR.idle.r;
    let curG      = STATE_COLOR.idle.g;
    let curB      = STATE_COLOR.idle.b;

    const startedAt = performance.now();
    const reduce    = window.matchMedia('(prefers-reduced-motion: reduce)');
    const CANVAS_SCALE = 2.8;

    // ── Setup (called on mount and resize) ───────────────────────────────────
    const setup = () => {
      const dpr  = window.devicePixelRatio || 1;
      const rect = container.getBoundingClientRect();
      const cs   = Math.round(rect.width || container.clientWidth || 300); // container px
      const cvs  = Math.round(cs * CANVAS_SCALE);                          // canvas px
      const off  = -Math.round((cvs - cs) / 2);

      canvas.width  = cvs * dpr;
      canvas.height = cvs * dpr;
      canvas.style.width    = `${cvs}px`;
      canvas.style.height   = `${cvs}px`;
      canvas.style.left     = `${off}px`;
      canvas.style.top      = `${off}px`;
      canvas.style.position = 'absolute';

      ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
      stars = makeStars(cvs, cvs, 95);
    };

    setup();
    const ro = new ResizeObserver(setup);
    ro.observe(container);

    // ── Draw helpers ─────────────────────────────────────────────────────────

    /** Draws a single canvas arc with optional glow. */
    const glowArc = (
      x: number, y: number, radius: number,
      startA: number, endA: number, cw: boolean,
      color: string, lineW: number, blur: number, blurColor: string
    ) => {
      ctx.save();
      ctx.beginPath();
      ctx.arc(x, y, radius, startA, endA, cw);
      ctx.strokeStyle   = color;
      ctx.lineWidth     = lineW;
      ctx.lineCap       = 'round';
      ctx.shadowBlur    = blur;
      ctx.shadowColor   = blurColor;
      ctx.stroke();
      ctx.restore();
    };

    /** Draws "FURI" centred at (x, y) with letter-spacing. */
    const drawFuri = (x: number, y: number, orbR: number, r: number, g: number, b: number, level: number, pulse: number) => {
      const fontSize   = Math.max(12, Math.round(orbR * 0.4));
      const spacing    = Math.max(2, Math.round(fontSize * 0.28));
      ctx.save();
      ctx.font         = `200 ${fontSize}px 'Inter', 'Segoe UI', sans-serif`;
      ctx.textBaseline = 'middle';
      ctx.textAlign    = 'left';
      ctx.shadowBlur   = 18 + level * 14 + pulse * 8;
      ctx.shadowColor  = `rgba(${r},${g},${b},0.9)`;

      const text   = 'FURI';
      const charWs = Array.from(text).map(c => ctx.measureText(c).width);
      const total  = charWs.reduce((s, w) => s + w, 0) + spacing * (text.length - 1);
      let cx2      = x - total / 2;

      const alpha  = 0.82 + pulse * 0.1 + level * 0.08;
      ctx.fillStyle = `rgba(255,255,255,${alpha.toFixed(3)})`;

      for (let i = 0; i < text.length; i++) {
        ctx.fillText(text[i], cx2, y);
        cx2 += charWs[i] + spacing;
      }
      ctx.restore();
    };

    // ── Main rAF tick ────────────────────────────────────────────────────────
    const tick = (now: number) => {
      const dpr = window.devicePixelRatio || 1;
      const W   = canvas.width  / dpr;   // CSS px
      const H   = canvas.height / dpr;
      const cx  = W / 2;
      const cy  = H / 2;

      // Voice level
      const vs    = useVoiceStore.getState();
      const lvTgt = vs.phase === 'recording'
        ? vs.level
        : vs.speaking ? getOutputLevel() : 0;
      smoothed += (lvTgt - smoothed) * (lvTgt > smoothed ? 0.45 : 0.08);
      const level = smoothed;

      // Colour lerp
      const tgt = STATE_COLOR[stateRef.current];
      curR = lerp(curR, tgt.r, 0.04);
      curG = lerp(curG, tgt.g, 0.04);
      curB = lerp(curB, tgt.b, 0.04);
      const r = Math.round(curR), g = Math.round(curG), b = Math.round(curB);

      // Gentle idle breathe (keeps orb alive when silent)
      const pulse = (Math.sin((now - startedAt) / 2200) * 0.5 + 0.5); // 0..1

      // ── Rotation speed: idle + voice boost ─────────────────────────────
      // Base speed gives one full revolution every ~21 s.
      // Each unit of voice level adds up to ~4× extra speed.
      const baseRot  = reduce.matches ? 0 : 0.003;
      const voiceRot = level * 0.018;
      const speed    = baseRot + voiceRot;

      arc1Angle += speed;
      arc2Angle += speed * 1.35;  // second arc moves a bit faster
      hotAngle  += speed * 0.6;   // hotspot moves slower → looks independent

      // ── Geometry (based on container, not canvas size) ──────────────────
      const cs    = container.clientWidth  || 300;
      const orbR  = cs * 0.21;      // core radius
      const ringR = orbR * 1.24;    // glow ring
      const arcR  = ringR * 1.13;   // orbiting arcs
      const auraR = arcR  * 1.25;   // outermost ambient glow

      // ═══════════════════════════════════════════════════════════════════
      // CLEAR
      // ═══════════════════════════════════════════════════════════════════
      ctx.clearRect(0, 0, W, H);

      // ═══════════════════════════════════════════════════════════════════
      // 1. BACKGROUND STARFIELD
      // ═══════════════════════════════════════════════════════════════════
      for (const s of stars) {
        const tw    = Math.sin(s.twinklePhase + (now - startedAt) * s.twinkleSpeed);
        const alpha = s.baseOpacity * (0.55 + tw * 0.45) * (0.65 + level * 0.35);
        ctx.beginPath();
        ctx.fillStyle = `rgba(${r},${g},${b},${alpha.toFixed(3)})`;
        ctx.arc(s.x, s.y, s.size, 0, Math.PI * 2);
        ctx.fill();
      }

      // ═══════════════════════════════════════════════════════════════════
      // 2. AMBIENT OUTER GLOW (aura that "flows" — the hotspot sweeps it)
      // ═══════════════════════════════════════════════════════════════════
      // Ambient halo
      const ambGrd = ctx.createRadialGradient(cx, cy, ringR * 0.9, cx, cy, auraR);
      ambGrd.addColorStop(0,   `rgba(${r},${g},${b},${(0.07 + pulse * 0.04 + level * 0.08).toFixed(3)})`);
      ambGrd.addColorStop(0.5, `rgba(${r},${g},${b},${(0.03 + level * 0.03).toFixed(3)})`);
      ambGrd.addColorStop(1,   'transparent');
      ctx.fillStyle = ambGrd;
      ctx.beginPath();
      ctx.arc(cx, cy, auraR, 0, Math.PI * 2);
      ctx.fill();

      // Flowing energy hotspot — a bright radial blob that orbits the ring
      const hx        = cx + Math.cos(hotAngle) * ringR;
      const hy        = cy + Math.sin(hotAngle) * ringR;
      const hotRadius = ringR * (0.35 + pulse * 0.1 + level * 0.2);
      const hotGrd    = ctx.createRadialGradient(hx, hy, 0, hx, hy, hotRadius);
      hotGrd.addColorStop(0,   `rgba(255,255,255,${(0.18 + level * 0.15 + pulse * 0.08).toFixed(3)})`);
      hotGrd.addColorStop(0.3, `rgba(${r},${g},${b},${(0.12 + level * 0.1).toFixed(3)})`);
      hotGrd.addColorStop(1,   'transparent');
      ctx.fillStyle = hotGrd;
      ctx.beginPath();
      ctx.arc(hx, hy, hotRadius, 0, Math.PI * 2);
      ctx.fill();

      // ═══════════════════════════════════════════════════════════════════
      // 3. DARK CORE
      // ═══════════════════════════════════════════════════════════════════
      const coreGrd = ctx.createRadialGradient(cx, cy, 0, cx, cy, orbR);
      coreGrd.addColorStop(0,   'rgba(2, 5, 14, 0.98)');
      coreGrd.addColorStop(0.65,'rgba(3, 8, 20, 0.96)');
      coreGrd.addColorStop(1,   `rgba(${Math.round(r * 0.08)},${Math.round(g * 0.08)},${Math.round(b * 0.08)},0.90)`);
      ctx.fillStyle = coreGrd;
      ctx.beginPath();
      ctx.arc(cx, cy, orbR, 0, Math.PI * 2);
      ctx.fill();

      // ═══════════════════════════════════════════════════════════════════
      // 4. GLOW RING
      // ═══════════════════════════════════════════════════════════════════
      const ringGlow  = 0.65 + pulse * 0.25 + level * 0.35;
      const ringBlur  = 22 + pulse * 12 + level * 28;

      // Thick outer glow stroke
      glowArc(
        cx, cy, ringR, 0, Math.PI * 2, false,
        `rgba(${r},${g},${b},${ringGlow.toFixed(2)})`,
        3.5 + level * 2,
        ringBlur,
        `rgba(${r},${g},${b},0.85)`
      );

      // Thin bright inner stroke (white core of the ring)
      glowArc(
        cx, cy, ringR, 0, Math.PI * 2, false,
        `rgba(220,245,255,${(0.45 + pulse * 0.3 + level * 0.25).toFixed(2)})`,
        0.8,
        6,
        'rgba(255,255,255,0.7)'
      );

      // ═══════════════════════════════════════════════════════════════════
      // 5. HUD TICK MARKS (36 around the ring)
      // ═══════════════════════════════════════════════════════════════════
      const TICKS    = 36;
      const majorEvr = 9; // 4 major ticks at cardinal points
      for (let i = 0; i < TICKS; i++) {
        const ang     = (i / TICKS) * Math.PI * 2 - Math.PI / 2;
        const major   = i % majorEvr === 0;
        const len     = major ? ringR * 0.11 : ringR * 0.045;
        const alpha   = major ? 0.75 : 0.28;
        const lw      = major ? 1.4 : 0.7;
        const innerR2 = ringR - len * 0.5;
        const outerR2 = ringR + len * 0.5;

        ctx.beginPath();
        ctx.moveTo(cx + Math.cos(ang) * innerR2, cy + Math.sin(ang) * innerR2);
        ctx.lineTo(cx + Math.cos(ang) * outerR2, cy + Math.sin(ang) * outerR2);
        ctx.strokeStyle = `rgba(${r},${g},${b},${alpha})`;
        ctx.lineWidth   = lw;
        ctx.lineCap     = 'round';
        ctx.stroke();
      }

      // ═══════════════════════════════════════════════════════════════════
      // 6. ORBITING WHITE ARCS
      //    Arc 1 — long (~270°), main revolving arc
      //    Arc 2 — short (~80°), counter-direction feel
      // ═══════════════════════════════════════════════════════════════════
      const arcBlur = 14 + level * 18;

      // Arc 1 — 270° sweep, bright white
      glowArc(
        cx, cy, arcR,
        arc1Angle, arc1Angle + Math.PI * 1.52, false,
        `rgba(255,255,255,${(0.82 + pulse * 0.12 + level * 0.06).toFixed(2)})`,
        1.8 + level * 1.2,
        arcBlur,
        `rgba(255,255,255,0.75)`
      );

      // Arc 1 — leading tip brighter flash
      glowArc(
        cx, cy, arcR,
        arc1Angle + Math.PI * 1.52 - 0.15, arc1Angle + Math.PI * 1.52, false,
        `rgba(255,255,255,0.95)`,
        2.5,
        20,
        'rgba(255,255,255,0.9)'
      );

      // Arc 2 — 75° sweep, slightly further out, softer
      glowArc(
        cx, cy, arcR * 1.065,
        arc2Angle, arc2Angle + Math.PI * 0.42, false,
        `rgba(180,240,255,${(0.55 + level * 0.2).toFixed(2)})`,
        1.2,
        10,
        `rgba(${r},${g},${b},0.6)`
      );

      // ═══════════════════════════════════════════════════════════════════
      // 7. "FURI" TEXT
      // ═══════════════════════════════════════════════════════════════════
      drawFuri(cx, cy, orbR, r, g, b, level, pulse);

      raf = requestAnimationFrame(tick);
    };

    raf = requestAnimationFrame(tick);

    // ── Hover: just track for potential future use (no dispersal) ────────────
    const onEnter = () => {};
    const onLeave = () => {};
    canvas.addEventListener('mouseenter', onEnter);
    canvas.addEventListener('mouseleave', onLeave);

    return () => {
      cancelAnimationFrame(raf);
      ro.disconnect();
      canvas.removeEventListener('mouseenter', onEnter);
      canvas.removeEventListener('mouseleave', onLeave);
    };
  }, []);

  return (
    <div
      ref={containerRef}
      data-state={state}
      aria-hidden
      className={clsx('jarvis-orb relative aspect-square', className)}
      style={{ overflow: 'visible', cursor: 'inherit' }}
    >
      {/* Canvas is CANVAS_SCALE × container size, centered via negative offset.
          Sized and positioned entirely by the ResizeObserver in the effect. */}
      <canvas ref={canvasRef} />
    </div>
  );
}
