import React from 'react';
import {Composition} from 'remotion';
import {TrailerEdit, EditProps, defaultProps} from './TrailerEdit';

// Props come from spikebot/remotion.py (--props=props.json); duration and size follow them.
export const Root: React.FC = () => (
  <Composition
    id="TrailerEdit"
    component={TrailerEdit}
    defaultProps={defaultProps}
    fps={30}
    width={1080}
    height={1350}
    durationInFrames={60}
    calculateMetadata={({props}: {props: EditProps}) => ({
      durationInFrames: Math.max(1, Math.round(props.duration * props.fps)),
      fps: props.fps,
      width: props.width,
      height: props.height,
    })}
  />
);
