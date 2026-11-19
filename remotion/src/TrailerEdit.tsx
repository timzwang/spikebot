import React from 'react';
import {
  AbsoluteFill, Easing, Freeze, Img, OffthreadVideo, Sequence, interpolate, spring, staticFile,
  useCurrentFrame, useVideoConfig,
} from 'remotion';

// fadeIn: this shot's crossfade from the previous one (s); tail: how long it runs on under the
// next one; hold: seconds of clean source, after which its last frame is held (never another
// scene of the trailer); focus: where the crop is centred across the frame (0.5 = middle).
export type Shot = {
  src: string; kind: 'video' | 'image'; start: number; dur: number; offset: number;
  pan: number; punch: boolean; flash: boolean; srcW: number; srcH: number;
  crop: [number, number, number, number] | null;
  fadeIn?: number; tail?: number; hold?: number | null; focus?: number;
};
export type Card = {from: number; to: number; text: string; ask: boolean};
export type Look = {
  saturation: number; contrast: number; brightness: number; warmth: number;
  grain: number; vignette: boolean; letterbox: boolean;
};
export type TextStyle = {font: string; sizePx: number; colour: string; highlight: string; y: number};
// motion: camera movement the editor adds ('none' = every shot completely still).
// framing: 'fill' crops to fill 9:16, 'fit' shows the whole frame over a blurred copy.
// fade: crossfade length in seconds (0 = hard cuts).
export type Motion = 'none' | 'subtle' | 'full';
export type Logo = {src: string; until: number; out: number; size: number};
export type EditProps = {
  fps: number; width: number; height: number; duration: number;
  shots: Shot[]; cards: Card[]; logo: Logo | null;
  look: Look; text: TextStyle; motion: Motion; framing: 'fill' | 'fit'; fade: number;
  endFade?: number;   // seconds the end fades to black
};

export const defaultProps: EditProps = {
  fps: 30, width: 1080, height: 1920, duration: 2, shots: [], cards: [], logo: null,
  motion: 'full', framing: 'fill', fade: 0,
  look: {saturation: 1, contrast: 1, brightness: 0, warmth: 0, grain: 0, vignette: false, letterbox: false},
  text: {font: 'Montserrat', sizePx: 96, colour: '#FFFFFF', highlight: '#FFE500', y: 0.3},
};

const smooth = Easing.bezier(0.45, 0, 0.2, 1); // gentle ease-in-out, like a hand-keyed move

const AMOUNT: Record<Motion, number> = {none: 0, subtle: 0.5, full: 1};

const Media: React.FC<{shot: Shot; style: React.CSSProperties}> = ({shot, style}) => {
  const {fps} = useVideoConfig();
  const frame = useCurrentFrame();
  if (shot.kind !== 'video') return <Img src={staticFile(shot.src)} style={style} />;
  const video = <OffthreadVideo src={staticFile(shot.src)} trimBefore={Math.round(shot.offset * fps)} muted style={style} />;
  if (shot.hold == null) return video;
  const last = Math.max(0, Math.floor(shot.hold * fps) - 1);
  return <Freeze frame={last} active={frame > last}>{video}</Freeze>;
};

// One shot. 'fill': cover-cropped, with a slow eased push + drift scaled by `motion` (none
// with motion 'none') and an optional punch-in. 'fit': the whole frame, still, over a
// blurred copy. `fadeIn` frames: crossfade in over the previous shot.
const ShotView: React.FC<{shot: Shot; motion: Motion; framing: 'fill' | 'fit'; fadeIn: number}> = (
  {shot, motion, framing, fadeIn},
) => {
  const frame = useCurrentFrame();
  const {fps, width: W, height: H, durationInFrames} = useVideoConfig();
  const amount = AMOUNT[motion] ?? 1;
  const opacity = fadeIn > 0 ? interpolate(frame, [0, fadeIn], [0, 1], {extrapolateRight: 'clamp'}) : 1;
  const [cw, ch, cx, cy] = shot.crop ?? [shot.srcW, shot.srcH, 0, 0];
  if (framing === 'fit') {
    const fit = Math.min(W / cw, H / ch);
    const cover = Math.max(W / cw, H / ch);
    const place = (k: number): React.CSSProperties => ({
      position: 'absolute', width: shot.srcW * k, height: shot.srcH * k,
      left: W / 2 - (cx + cw / 2) * k, top: H / 2 - (cy + ch / 2) * k,
    });
    return (
      <AbsoluteFill style={{overflow: 'hidden', backgroundColor: 'black', opacity}}>
        <AbsoluteFill style={{filter: 'blur(36px) brightness(0.82) saturate(0.85)', transform: 'scale(1.08)'}}>
          <Media shot={shot} style={place(cover)} />
        </AbsoluteFill>
        <Media shot={shot} style={place(fit)} />
        {shot.flash ? <Flash /> : null}
      </AbsoluteFill>
    );
  }
  const p = interpolate(frame, [0, Math.max(1, durationInFrames - 1)], [0, 1], {
    easing: smooth, extrapolateLeft: 'clamp', extrapolateRight: 'clamp',
  });
  const extra = (shot.kind === 'image' ? 0.12 : 0.06) * amount;
  const base = Math.max(W / cw, H / ch) * (1 + extra);
  const range = 0.06 * amount;
  const push = shot.pan > 0 ? interpolate(p, [0, 1], [1.0, 1 + range]) : interpolate(p, [0, 1], [1 + range, 1.0]);
  let scale = base * push;
  let blur = 0;
  if (shot.punch && amount > 0) {
    const s = spring({frame, fps, config: {damping: 18, stiffness: 180, mass: 0.6}});
    scale *= interpolate(s, [0, 1], [1.22, 1]);
    blur = interpolate(frame, [0, 6], [10, 0], {extrapolateRight: 'clamp'});
  }
  const room = Math.max(0, cw * scale - W);
  const focus = shot.focus ?? 0.5;
  const framed = Math.abs(focus - 0.5) > 0.001;
  const drift = Math.min(framed ? 60 : 140, room / 2) * shot.pan * (p - 0.5) * amount;
  // centre the crop on `focus` (the subject), never past the picture's edges
  const want = W / 2 - (cx + cw * focus) * scale + drift;
  const left = Math.min(-cx * scale, Math.max(W - (cx + cw) * scale, want));
  const top = H / 2 - (cy + ch / 2) * scale;
  const style: React.CSSProperties = {
    position: 'absolute', left, top, width: shot.srcW * scale, height: shot.srcH * scale,
    filter: blur > 0.1 ? `blur(${blur}px)` : undefined,
  };
  return (
    <AbsoluteFill style={{overflow: 'hidden', backgroundColor: 'black', opacity}}>
      <Media shot={shot} style={style} />
      {shot.flash ? <Flash /> : null}
    </AbsoluteFill>
  );
};

const Flash: React.FC = () => {
  const frame = useCurrentFrame();
  const o = interpolate(frame, [0, 5], [0.85, 0], {easing: Easing.out(Easing.quad), extrapolateRight: 'clamp'});
  return <AbsoluteFill style={{backgroundColor: 'white', opacity: o}} />;
};

// Text card: words pop in one after another on a spring, *starred* words in the highlight colour.
const CardView: React.FC<{card: Card; text: TextStyle}> = ({card, text}) => {
  const frame = useCurrentFrame();
  const {fps, height: H, durationInFrames} = useVideoConfig();
  const words = card.text.split(/\s+/).filter(Boolean);
  let hot = false;
  const out = interpolate(frame, [durationInFrames - 5, durationInFrames], [1, 0], {
    extrapolateLeft: 'clamp', extrapolateRight: 'clamp',
  });
  const size = card.ask ? text.sizePx * 1.05 : text.sizePx;
  return (
    <AbsoluteFill style={{alignItems: 'center', opacity: out}}>
      <div style={{
        position: 'absolute', top: H * text.y, width: '84%', textAlign: 'center',
        fontFamily: `${text.font}, 'DejaVu Sans', sans-serif`, fontWeight: 800, fontSize: size,
        lineHeight: 1.12, color: text.colour, letterSpacing: -1,
        textShadow: '0 4px 18px rgba(0,0,0,0.65), 0 0 3px rgba(0,0,0,0.9)',
        background: card.ask ? 'rgba(0,0,0,0.55)' : undefined, borderRadius: 18,
        padding: card.ask ? '18px 26px' : undefined,
      }}>
        {words.map((w, i) => {
          const starts = w.startsWith('*');
          const ends = w.endsWith('*');
          if (starts) hot = true;
          const isHot = hot;
          if (ends) hot = false;
          const s = spring({frame: frame - i * 2, fps, config: {damping: 14, stiffness: 220, mass: 0.5}});
          return (
            <span key={i} style={{
              display: 'inline-block', marginRight: '0.28em', color: isHot ? text.highlight : undefined,
              transform: `translateY(${interpolate(s, [0, 1], [28, 0])}px) scale(${interpolate(s, [0, 1], [0.7, 1])})`,
              opacity: s,
            }}>{w.replace(/\*/g, '')}</span>
          );
        })}
      </div>
    </AbsoluteFill>
  );
};

// Logo: builds up gradually (fades in and sharpens) until the drop, then on the drop grows
// quickly while fading to transparent. Never wider than `size` of the screen.
const GROW = 0.35;
const LogoView: React.FC<{logo: Logo}> = ({logo}) => {
  const frame = useCurrentFrame();
  const {fps, width: W} = useVideoConfig();
  const drop = Math.round(logo.until * fps);
  const out = Math.max(1, Math.round(logo.out * fps));
  const build = interpolate(frame, [0, Math.max(1, drop * 0.85)], [0, 1], {
    easing: Easing.inOut(Easing.cubic), extrapolateLeft: 'clamp', extrapolateRight: 'clamp',
  });
  const burst = interpolate(frame, [drop, drop + out], [0, 1], {
    easing: Easing.out(Easing.cubic), extrapolateLeft: 'clamp', extrapolateRight: 'clamp',
  });
  const scale = 1 + GROW * burst;
  const opacity = build * (1 - burst);
  const blur = (1 - build) * 12;
  const dim = interpolate(frame, [0, drop, drop + out], [0.55, 0.55, 0], {
    extrapolateLeft: 'clamp', extrapolateRight: 'clamp',
  });
  return (
    <AbsoluteFill style={{alignItems: 'center', justifyContent: 'center'}}>
      <AbsoluteFill style={{backgroundColor: 'black', opacity: dim}} />
      <Img src={staticFile(logo.src)} style={{
        width: (W * logo.size) / (1 + GROW), transform: `scale(${scale})`,
        filter: blur > 0.1 ? `blur(${blur}px)` : undefined, opacity,
      }} />
    </AbsoluteFill>
  );
};

// The last seconds fade slowly to black (end_fade_s).
const EndFade: React.FC = () => {
  const frame = useCurrentFrame();
  const {durationInFrames} = useVideoConfig();
  const o = interpolate(frame, [0, Math.max(1, durationInFrames - 1)], [0, 1], {
    easing: Easing.inOut(Easing.quad), extrapolateLeft: 'clamp', extrapolateRight: 'clamp',
  });
  return <AbsoluteFill style={{backgroundColor: 'black', opacity: o}} />;
};

const Grain: React.FC<{amount: number}> = ({amount}) => {
  const frame = useCurrentFrame();
  const seed = frame % 7;
  return (
    <AbsoluteFill style={{opacity: Math.min(0.5, amount / 40), mixBlendMode: 'overlay', pointerEvents: 'none'}}>
      <svg width="100%" height="100%">
        <filter id={`g${seed}`}>
          <feTurbulence type="fractalNoise" baseFrequency="0.9" numOctaves={2} seed={seed} />
        </filter>
        <rect width="100%" height="100%" filter={`url(#g${seed})`} />
      </svg>
    </AbsoluteFill>
  );
};

export const TrailerEdit: React.FC<EditProps> = (props) => {
  const {fps, shots, cards, logo, look, text, height: H, motion, framing, fade} = props;
  const f = (s: number) => Math.round(s * fps);
  const fadeF = fade > 0 ? Math.max(1, f(fade)) : 0;
  // each cut's own fade (never longer than half of either clip: only two clips ever blend)
  const fadeIn = (i: number) => (i === 0 ? 0 : (shots[i].fadeIn != null ? f(shots[i].fadeIn as number) : fadeF));
  const tail = (i: number) => (i < shots.length - 1 ? fadeIn(i + 1) : 0);
  const endF = f(props.endFade ?? 0);
  const total = f(props.duration);
  const grade = `saturate(${look.saturation}) contrast(${look.contrast}) brightness(${1 + look.brightness})`
    + (look.warmth > 0 ? ` sepia(${Math.min(0.35, look.warmth * 2.5)})` : '')
    + (look.warmth < 0 ? ` hue-rotate(${look.warmth * 60}deg)` : '');
  const bar = Math.round(H * 0.055);
  return (
    <AbsoluteFill style={{backgroundColor: 'black'}}>
      <AbsoluteFill style={{filter: grade}}>
        {shots.map((s, i) => (
          // with a crossfade each shot runs on under the next one while it fades in
          <Sequence key={i} from={f(s.start)}
            durationInFrames={Math.max(1, f(s.start + s.dur) - f(s.start) + tail(i))}>
            <ShotView shot={s} motion={motion ?? 'full'} framing={framing ?? 'fill'} fadeIn={fadeIn(i)} />
          </Sequence>
        ))}
      </AbsoluteFill>
      {look.vignette ? (
        <AbsoluteFill style={{background: 'radial-gradient(ellipse at center, rgba(0,0,0,0) 55%, rgba(0,0,0,0.55) 100%)'}} />
      ) : null}
      {look.grain > 0 ? <Grain amount={look.grain} /> : null}
      {logo ? (
        <Sequence from={0} durationInFrames={Math.max(1, f(logo.until + logo.out))}><LogoView logo={logo} /></Sequence>
      ) : null}
      {cards.map((c, i) => (
        <Sequence key={`c${i}`} from={f(c.from)} durationInFrames={Math.max(1, f(c.to) - f(c.from))}>
          <CardView card={c} text={text} />
        </Sequence>
      ))}
      {look.letterbox ? (
        <>
          <div style={{position: 'absolute', top: 0, left: 0, right: 0, height: bar * 2, background: 'black'}} />
          <div style={{position: 'absolute', bottom: 0, left: 0, right: 0, height: bar * 2, background: 'black'}} />
        </>
      ) : null}
      {endF > 0 ? (
        <Sequence from={Math.max(0, total - endF)} durationInFrames={endF}><EndFade /></Sequence>
      ) : null}
    </AbsoluteFill>
  );
};
