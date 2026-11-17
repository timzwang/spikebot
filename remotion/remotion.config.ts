import {Config} from '@remotion/cli/config';

Config.setVideoImageFormat('jpeg');
Config.setConcurrency(1); // small VM: one frame at a time is slower but never runs out of memory
