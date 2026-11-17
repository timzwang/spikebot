import React from 'react';
import {
  AbsoluteFill, Easing, Img, OffthreadVideo, Sequence, interpolate, spring, staticFile,
  useCurrentFrame, useVideoConfig,
} from 'remotion';

export type Shot = {
  src: string; kind: 'video' | 'image'; start: number; dur: number; offset: number;
  pan: number; punch: boolean; flash: boolean; srcW: number; srcH: number;
  crop: [number, number, number, number] | null;
};
export type Card = {from: number; to: number; text: string; ask: boolean};
export type Look = {
  saturation: number; contrast: number; brightness: number; warmth: number;
  grain: number; vignette: boolean; letterbox: boolean;
};
export type TextStyle = {font: string; sizePx: number; colour: string; highlight: string; y: number};
export type EditProps = {
  fps: number; width: number; height: number; duration: number;
  shots: Shot[]; cards: Card[]; logo: {src: string; until: number} | null;
  look: Look; text: TextStyle;
};

export const defaultProps: EditProps = {
  fps: 30, width: 1080, height: 1920, duration: 2, shots: [], cards: [], logo: null,
  look: {saturation: 1, contrast: 1, brightness: 0, warmth: 0, grain: 0, vignette: false, letterbox: false},
  text: {font: 'Montserrat', sizePx: 96, colour: '#FFFFFF', highlight: '#FFE500', y: 0.3},
};

const smooth = Easing.bezier(0.45, 0, 0.2, 1); // gentle ease-in-out, like a hand-keyed move

// One shot: cover-cropped, slow eased push + drift; optional punch-in with motion blur.
const ShotView: React.FC<{shot: Shot}> = ({shot}) => {
  const frame = useCurrentFrame();
  const {fps, width: W, height: H, durationInFrames} = useVideoConfig();
  const p = interpolate(frame, [0, Math.max(1, durationInFrames - 1)], [0, 1], {
    easing: smooth, extrapolateLeft: 'clamp', extrapolateRight: 'clamp',
  });
  const [cw, ch, cx, cy] = shot.crop ?? [shot.srcW, shot.srcH, 0, 0];
  const base = Math.max(W / cw, H / ch) * (shot.kind === 'image' ? 1.12 : 1.06);
  const push = shot.pan > 0 ? interpolate(p, [0, 1], [1.0, 1.06]) : interpolate(p, [0, 1], [1.06, 1.0]);
  let scale = base * push;
  let blur = 0;
  if (shot.punch) {
    const s = spring({frame, fps, config: {damping: 18, stiffness: 180, mass: 0.6}});
    scale *= interpolate(s, [0, 1], [1.22, 1]);
    blur = interpolate(frame, [0, 6], [10, 0], {extrapolateRight: 'clamp'});
  }
  const room = Math.max(0, cw * scale - W);
  const drift = Math.min(140, room / 2) * shot.pan * (p - 0.5);
  const left = W / 2 - (cx + cw / 2) * scale + drift;
  const top = H / 2 - (cy + ch / 2) * scale;
  const style: React.CSSProperties = {
    position: 'absolute', left, top, width: shot.srcW * scale, height: shot.srcH * scale,
    filter: blur > 0.1 ? `blur(${blur}px)` : undefined,
  };
  return (
    <AbsoluteFill style={{overflow: 'hidden', backgroundColor: 'black'}}>
      {shot.kind === 'video'
        ? <OffthreadVideo src={staticFile(shot.src)} trimBefore={Math.round(shot.offset * fps)} muted style={style} />
        : <Img src={staticFile(shot.src)} style={style} />}
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

// Logo reveal: blurred and large -> sharp, holds, then scales up and fades away into the edit.
const LogoView: React.FC<{src: string}> = ({src}) => {
  const frame = useCurrentFrame();
  const {fps, durationInFrames: d, width: W} = useVideoConfig();
  const inS = spring({frame, fps, config: {damping: 20, stiffness: 90}});
  const outP = interpolate(frame, [d - Math.round(fps * 0.5), d], [0, 1], {
    easing: Easing.in(Easing.cubic), extrapolateLeft: 'clamp', extrapolateRight: 'clamp',
  });
  const scale = interpolate(inS, [0, 1], [1.18, 1]) * interpolate(outP, [0, 1], [1, 1.35]);
  const blur = interpolate(inS, [0, 1], [14, 0]) + outP * 10;
  const opacity = Math.min(inS, 1 - outP);
  const dim = interpolate(frame, [0, d - Math.round(fps * 0.4), d], [0.55, 0.55, 0], {extrapolateRight: 'clamp'});
  return (
    <AbsoluteFill style={{alignItems: 'center', justifyContent: 'center'}}>
      <AbsoluteFill style={{backgroundColor: 'black', opacity: dim}} />
      <Img src={staticFile(src)} style={{
        width: W * 0.78, transform: `scale(${scale})`, filter: `blur(${blur}px)`, opacity,
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
  const {fps, shots, cards, logo, look, text, height: H} = props;
  const f = (s: number) => Math.round(s * fps);
  const grade = `saturate(${look.saturation}) contrast(${look.contrast}) brightness(${1 + look.brightness})`
    + (look.warmth > 0 ? ` sepia(${Math.min(0.35, look.warmth * 2.5)})` : '')
    + (look.warmth < 0 ? ` hue-rotate(${look.warmth * 60}deg)` : '');
  const bar = Math.round(H * 0.055);
  return (
    <AbsoluteFill style={{backgroundColor: 'black'}}>
      <AbsoluteFill style={{filter: grade}}>
        {shots.map((s, i) => (
          <Sequence key={i} from={f(s.start)} durationInFrames={Math.max(1, f(s.start + s.dur) - f(s.start))}>
            <ShotView shot={s} />
          </Sequence>
        ))}
      </AbsoluteFill>
      {look.vignette ? (
        <AbsoluteFill style={{background: 'radial-gradient(ellipse at center, rgba(0,0,0,0) 55%, rgba(0,0,0,0.55) 100%)'}} />
      ) : null}
      {look.grain > 0 ? <Grain amount={look.grain} /> : null}
      {logo ? (
        <Sequence from={0} durationInFrames={Math.max(1, f(logo.until))}><LogoView src={logo.src} /></Sequence>
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
