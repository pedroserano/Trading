import { useMemo, useRef, useState } from "react";
import { num, signed } from "./format";
import type { Analysis } from "./types";

const W = 640;
const H = 260;
const PAD = { l: 52, r: 12, t: 12, b: 26 };

function ticks(lo: number, hi: number, n: number): number[] {
  const raw = (hi - lo) / n;
  const mag = 10 ** Math.floor(Math.log10(raw));
  const step = [1, 2, 2.5, 5, 10].map((m) => m * mag).find((s) => s >= raw) ?? raw;
  const out = [];
  for (let v = Math.ceil(lo / step) * step; v <= hi + 1e-9; v += step) out.push(v);
  return out;
}

export default function PayoffChart({ a, spot }: { a: Analysis; spot: number }) {
  const { x, expiry, today } = a.curve;
  const ref = useRef<SVGSVGElement>(null);
  const [hover, setHover] = useState<number | null>(null);

  const scale = useMemo(() => {
    const ys = [...expiry, ...today, 0];
    let lo = Math.min(...ys);
    let hi = Math.max(...ys);
    const padY = (hi - lo) * 0.08 || 10;
    lo -= padY;
    hi += padY;
    const x0 = x[0];
    const x1 = x[x.length - 1];
    const sx = (v: number) => PAD.l + ((v - x0) / (x1 - x0)) * (W - PAD.l - PAD.r);
    const sy = (v: number) => PAD.t + (1 - (v - lo) / (hi - lo)) * (H - PAD.t - PAD.b);
    return { lo, hi, x0, x1, sx, sy };
  }, [x, expiry, today]);

  const { sx, sy } = scale;
  const line = (ys: number[]) => ys.map((y, i) => `${i ? "L" : "M"}${sx(x[i]).toFixed(1)},${sy(y).toFixed(1)}`).join("");
  const area =
    line(expiry) +
    `L${sx(x[x.length - 1]).toFixed(1)},${sy(0).toFixed(1)}L${sx(x[0]).toFixed(1)},${sy(0).toFixed(1)}Z`;
  const y0 = sy(0);
  const em = a.expected_move;

  const onMove = (e: React.PointerEvent) => {
    const svg = ref.current;
    if (!svg) return;
    const r = svg.getBoundingClientRect();
    const px = ((e.clientX - r.left) / r.width) * W;
    const v = scale.x0 + ((px - PAD.l) / (W - PAD.l - PAD.r)) * (scale.x1 - scale.x0);
    let best = 0;
    for (let i = 1; i < x.length; i++) if (Math.abs(x[i] - v) < Math.abs(x[best] - v)) best = i;
    setHover(best);
  };

  return (
    <div className="chart">
      <svg
        ref={ref}
        viewBox={`0 0 ${W} ${H}`}
        role="img"
        aria-label="Profit and loss by underlying price"
        onPointerMove={onMove}
        onPointerLeave={() => setHover(null)}
      >
        <defs>
          <clipPath id="above">
            <rect x={0} y={0} width={W} height={Math.max(y0, 0)} />
          </clipPath>
          <clipPath id="below">
            <rect x={0} y={y0} width={W} height={Math.max(H - y0, 0)} />
          </clipPath>
        </defs>
        {em ? (
          <rect
            x={sx(Math.max(spot - em, scale.x0))}
            width={sx(Math.min(spot + em, scale.x1)) - sx(Math.max(spot - em, scale.x0))}
            y={PAD.t}
            height={H - PAD.t - PAD.b}
            className="em-band"
          />
        ) : null}
        {ticks(scale.lo, scale.hi, 4).map((v) => (
          <g key={`y${v}`}>
            <line x1={PAD.l} x2={W - PAD.r} y1={sy(v)} y2={sy(v)} className="grid" />
            <text x={PAD.l - 6} y={sy(v) + 4} className="axis" textAnchor="end">
              {Math.abs(v) >= 1000 ? `${(v / 1000).toFixed(1)}k` : v.toFixed(0)}
            </text>
          </g>
        ))}
        {ticks(scale.x0, scale.x1, 6).map((v) => (
          <text key={`x${v}`} x={sx(v)} y={H - 8} className="axis" textAnchor="middle">
            {v.toFixed(v < 50 ? 1 : 0)}
          </text>
        ))}
        <path d={area} className="area-pos" clipPath="url(#above)" />
        <path d={area} className="area-neg" clipPath="url(#below)" />
        <line x1={PAD.l} x2={W - PAD.r} y1={y0} y2={y0} className="zero" />
        <path d={line(today)} className="today" />
        <path d={line(expiry)} className="expiry" />
        <line x1={sx(spot)} x2={sx(spot)} y1={PAD.t} y2={H - PAD.b} className="spot" />
        {a.breakevens
          .filter((b) => b >= scale.x0 && b <= scale.x1)
          .map((b) => (
            <circle key={b} cx={sx(b)} cy={y0} r={3.5} className="be" />
          ))}
        {hover !== null ? (
          <g>
            <line x1={sx(x[hover])} x2={sx(x[hover])} y1={PAD.t} y2={H - PAD.b} className="cross" />
            <circle cx={sx(x[hover])} cy={sy(expiry[hover])} r={3} className="dot-exp" />
            <circle cx={sx(x[hover])} cy={sy(today[hover])} r={3} className="dot-today" />
          </g>
        ) : null}
      </svg>
      <div className="chart-legend">
        {hover !== null ? (
          <>
            <span>At {num(x[hover], 2)}</span>
            <span>
              Expiry <b className={expiry[hover] >= 0 ? "pos" : "neg"}>{signed(expiry[hover])}</b>
            </span>
            <span>
              Today <b className={today[hover] >= 0 ? "pos" : "neg"}>{signed(today[hover])}</b>
            </span>
          </>
        ) : (
          <>
            <span><i className="key expiry" /> At expiration</span>
            <span><i className="key today" /> Today</span>
            <span><i className="key em" /> ±1σ to {a.dte} DTE</span>
          </>
        )}
      </div>
    </div>
  );
}
