import React from 'react';
import {
  AbsoluteFill, Easing, Img, OffthreadVideo, Sequence, interpolate, spring, staticFile,
  useCurrentFrame, useVideoConfig,
} from 'remotion';

// layout (from spikebot/tokyo_girl.slot_layout): 'fill' full screen, 'box45' a 4:5 picture in `box` on
// black bars, 'box11' a picture in `box` over a blurred copy, 'stack' this clip in `box` and `second` in its own box below,
// 'fit' the whole frame over a blur. focus: where the subject is (fractions of the picture).
export type Layout = 'fill' | 'fit' | 'box45' | 'box11' | 'stack';
type Rect = [number, number, number, number];
export type Second = {
  src: string; kind: 'video' | 'image'; offset: number; srcW: number; srcH: number;
  crop: Rect | null; focus: [number, number] | null; box: Rect;
};
export type Shot = {
  src: string; kind: 'video' | 'image'; start: number; dur: number; offset: number;
  pan: number; punch: boolean; flash: boolean; srcW: number; srcH: number;
  crop: Rect | null; focus?: [number, number] | null; layout?: Layout; box?: Rect; second?: Second;
};
export type Card = {from: number; to: number; text: string; ask: boolean};
export type Look = {
  saturation: number; contrast: number; brightness: number; warmth: number;
  grain: number; vignette: boolean; letterbox: boolean;
};
export type TextStyle = {font: string; sizePx: number; colour: string; highlight: string; y: number};
// motion: camera movement the editor adds ('none' = every shot completely still).
// framing: the edit's framing (each shot's own `layout` wins: smart fill, stack fallback).
// fade: crossfade length in seconds (0 = hard cuts).
export type Motion = 'none' | 'subtle' | 'full';
export type Logo = {src: string; until: number; out: number; size: number};
export type EditProps = {
  fps: number; width: number; height: number; duration: number;
  shots: Shot[]; cards: Card[]; logo: Logo | null;
  look: Look; text: TextStyle; motion: Motion; framing: Layout; fade: number;
};

export const defaultProps: EditProps = {
  fps: 30, width: 1080, height: 1920, duration: 2, shots: [], cards: [], logo: null,
  motion: 'full', framing: 'fill', fade: 0,
  look: {saturation: 1, contrast: 1, brightness: 0, warmth: 0, grain: 0, vignette: false, letterbox: false},
  text: {font: 'Montserrat', sizePx: 96, colour: '#FFFFFF', highlight: '#FFE500', y: 0.3},
};

const smooth = Easing.bezier(0.45, 0, 0.2, 1); // gentle ease-in-out, like a hand-keyed move

const AMOUNT: Record<Motion, number> = {none: 0, subtle: 0.5, full: 1};

type Source = {src: string; kind: 'video' | 'image'; offset: number; srcW: number; srcH: number;
  crop: Rect | null; focus?: [number, number] | null};

const Media: React.FC<{shot: Source; style: React.CSSProperties}> = ({shot, style}) => {
  const {fps} = useVideoConfig();
  return shot.kind === 'video'
    ? <OffthreadVideo src={staticFile(shot.src)} trimBefore={Math.round(shot.offset * fps)} muted style={style} />
    : <Img src={staticFile(shot.src)} style={style} />;
};

// One shot, drawn by its layout (see Layout). 'fit': the whole frame, still, over a
// blurred copy. `fadeIn` frames: crossfade in over the previous shot.
const ShotView: React.FC<{shot: Shot; motion: Motion; framing: Layout; fadeIn: number}> = (
  {shot, motion, framing, fadeIn},
) => {
  const frame = useCurrentFrame();
  const {width: W, height: H} = useVideoConfig();
  const opacity = fadeIn > 0 ? interpolate(frame, [0, fadeIn], [0, 1], {extrapolateRight: 'clamp'}) : 1;
  const [cw, ch, cx, cy] = shot.crop ?? [shot.srcW, shot.srcH, 0, 0];
  const layout: Layout = shot.layout ?? (framing === 'fit' ? 'fit' : framing === 'fill' ? 'fill' : framing);
  if (layout === 'fit') {
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
  if (layout === 'fill') {
    return (
      <AbsoluteFill style={{overflow: 'hidden', backgroundColor: 'black', opacity}}>
        <Cover m={shot} tw={W} th={H} pan={shot.pan} punch={shot.punch} motion={motion} />
        {shot.flash ? <Flash /> : null}
      </AbsoluteFill>
    );
  }
  // a boxed picture (or two stacked) over a blurred, darker copy of the clip; 4:5 on plain black bars
  const cover = Math.max(W / cw, H / ch);
  const boxes: [Source, Rect, number][] = [[shot, shot.box ?? [0, 0, W, H], shot.pan]];
  if (layout === 'stack' && shot.second) boxes.push([shot.second, shot.second.box, -shot.pan]);
  return (
    <AbsoluteFill style={{overflow: 'hidden', backgroundColor: 'black', opacity}}>
      {layout === 'box45' ? null : (
        <AbsoluteFill style={{filter: 'blur(36px) brightness(0.82) saturate(0.85)', transform: 'scale(1.08)'}}>
          <Media shot={shot} style={{position: 'absolute', width: shot.srcW * cover, height: shot.srcH * cover,
            left: W / 2 - (cx + cw / 2) * cover, top: H / 2 - (cy + ch / 2) * cover}} />
        </AbsoluteFill>
      )}
      {boxes.map(([m, [bx, by, bw, bh], pan], i) => (
        <div key={i} style={{position: 'absolute', left: bx, top: by, width: bw, height: bh, overflow: 'hidden'}}>
          <Cover m={m} tw={bw} th={bh} pan={pan} punch={shot.punch} motion={motion} />
        </div>
      ))}
      {shot.flash ? <Flash /> : null}
    </AbsoluteFill>
  );
};

// A clip cover-cropped to tw x th: a slow eased push + drift scaled by `motion` (none with
// motion 'none'), an optional punch-in, and the crop centred on the subject when known.
const Cover: React.FC<{m: Source; tw: number; th: number; pan: number; punch: boolean; motion: Motion}> = (
  {m, tw, th, pan, punch, motion},
) => {
  const frame = useCurrentFrame();
  const {fps, durationInFrames} = useVideoConfig();
  const amount = AMOUNT[motion] ?? 1;
  const [cw, ch, cx, cy] = m.crop ?? [m.srcW, m.srcH, 0, 0];
  const p = interpolate(frame, [0, Math.max(1, durationInFrames - 1)], [0, 1], {
    easing: smooth, extrapolateLeft: 'clamp', extrapolateRight: 'clamp',
  });
  const extra = (m.kind === 'image' ? 0.12 : 0.06) * amount;
  const base = Math.max(tw / cw, th / ch) * (1 + extra);
  const range = 0.06 * amount;
  const push = pan > 0 ? interpolate(p, [0, 1], [1.0, 1 + range]) : interpolate(p, [0, 1], [1 + range, 1.0]);
  let scale = base * push;
  let blur = 0;
  if (punch && amount > 0) {
    const s = spring({frame, fps, config: {damping: 18, stiffness: 180, mass: 0.6}});
    scale *= interpolate(s, [0, 1], [1.22, 1]);
    blur = interpolate(frame, [0, 6], [10, 0], {extrapolateRight: 'clamp'});
  }
  const [fx, fy] = m.focus ?? [0.5, 0.5];
  const room = Math.max(0, cw * scale - tw);
  const drift = Math.min(140 * tw / 1080, room / 2) * pan * (p - 0.5) * amount;
  const clampX = (x: number) => Math.min(-cx * scale, Math.max(tw - (cx + cw) * scale, x));
  const clampY = (y: number) => Math.min(-cy * scale, Math.max(th - (cy + ch) * scale, y));
  const left = clampX(tw / 2 - (cx + fx * cw) * scale + drift);
  const top = clampY(th / 2 - (cy + fy * ch) * scale);
  const style: React.CSSProperties = {
    position: 'absolute', left, top, width: m.srcW * scale, height: m.srcH * scale,
    filter: blur > 0.1 ? `blur(${blur}px)` : undefined,
  };
  return <Media shot={m} style={style} />;
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
            durationInFrames={Math.max(1, f(s.start + s.dur) - f(s.start) + (i < shots.length - 1 ? fadeF : 0))}>
            <ShotView shot={s} motion={motion ?? 'full'} framing={framing ?? 'fill'} fadeIn={i > 0 ? fadeF : 0} />
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
    </AbsoluteFill>
  );
};
