/* ==========================================================================
   MAVRICK — Procedural Web Audio Night-Air Drone & Wind Engine
   Muted by default · Generated 100% via native Web Audio API
   ========================================================================== */

export class NightAudioEngine {
  constructor() {
    this.ctx = null;
    this.masterGain = null;
    this.lowpass = null;
    this.windFilter = null;
    this.isPlaying = false;
    this.isHidden = document.hidden;
    this.togglePending = false;
    this.suspensionTimer = 0;
    this.currentAct = 1;
    this.isHovered = false;
    this.nodes = [];
    this.hoverResonance = null;
    this.lastHoverChime = -Infinity;
  }

  _initContext() {
    const AudioCtx = window.AudioContext || window.webkitAudioContext;
    if (!AudioCtx) return false;

    const ctx = new AudioCtx();
    this.ctx = ctx;

    const masterGain = ctx.createGain();
    masterGain.gain.value = 0.0;

    const lowpass = ctx.createBiquadFilter();
    lowpass.type = "lowpass";
    lowpass.frequency.value = 920;
    lowpass.Q.value = 0.7;

    lowpass.connect(masterGain);
    masterGain.connect(ctx.destination);

    this.masterGain = masterGain;
    this.lowpass = lowpass;
    this._initHoverResonance(ctx, masterGain);

    // Warm harmonic night-air drone (A1, E2, A2, E3, B3 subtle fifths)
    const partials = [
      { freq: 55.0, type: "sine", gain: 0.16 },
      { freq: 82.41, type: "triangle", gain: 0.045 },
      { freq: 110.0, type: "sine", gain: 0.10 },
      { freq: 164.81, type: "sine", gain: 0.04 },
      { freq: 246.94, type: "sine", gain: 0.018 },
    ];

    partials.forEach((p, idx) => {
      const osc = ctx.createOscillator();
      const oscGain = ctx.createGain();
      osc.type = p.type;
      osc.frequency.value = p.freq;
      oscGain.gain.value = p.gain;

      // Ultra-slow pitch breath LFO per partial
      const lfo = ctx.createOscillator();
      const lfoDepth = ctx.createGain();
      lfo.type = "sine";
      lfo.frequency.value = 0.07 + idx * 0.023;
      lfoDepth.gain.value = 0.35;
      lfo.connect(lfoDepth);
      lfoDepth.connect(osc.frequency);

      osc.connect(oscGain);
      oscGain.connect(lowpass);

      osc.start();
      lfo.start();
      this.nodes.push(osc, lfo);
    });

    // Procedural pink/brown noise night wind buffer
    const durationSec = 4;
    const sampleCount = ctx.sampleRate * durationSec;
    const noiseBuffer = ctx.createBuffer(1, sampleCount, ctx.sampleRate);
    const channel = noiseBuffer.getChannelData(0);
    let lastOut = 0;
    for (let i = 0; i < sampleCount; i += 1) {
      const white = Math.random() * 2 - 1;
      lastOut = (lastOut + 0.028 * white) / 1.028;
      channel[i] = lastOut * 2.2;
    }

    const seam = Math.round(ctx.sampleRate * 0.045);
    const tail = sampleCount - seam;
    for (let i = 0; i < seam; i += 1) {
      const blend = i / (seam - 1);
      channel[tail + i] += (channel[0] - channel[tail + i]) * blend * blend;
    }

    const noiseSrc = ctx.createBufferSource();
    noiseSrc.buffer = noiseBuffer;
    noiseSrc.loop = true;

    const windFilter = ctx.createBiquadFilter();
    windFilter.type = "bandpass";
    windFilter.frequency.value = 320;
    windFilter.Q.value = 1.4;
    this.windFilter = windFilter;

    const windLfo = ctx.createOscillator();
    const windLfoGain = ctx.createGain();
    windLfo.type = "sine";
    windLfo.frequency.value = 0.11;
    windLfoGain.gain.value = 95;
    windLfo.connect(windLfoGain);
    windLfoGain.connect(windFilter.frequency);

    const windGain = ctx.createGain();
    windGain.gain.value = 0.18;

    noiseSrc.connect(windFilter);
    windFilter.connect(windGain);
    windGain.connect(lowpass);

    noiseSrc.start();
    windLfo.start();
    this.nodes.push(noiseSrc, windLfo);

    return true;
  }

  _initHoverResonance(ctx, destination) {
    const envelope = ctx.createGain();
    envelope.gain.value = 0;
    envelope.connect(destination);

    const fundamental = ctx.createOscillator();
    fundamental.frequency.value = 1120;
    fundamental.connect(envelope);
    fundamental.start();

    const overtone = ctx.createOscillator();
    const overtoneGain = ctx.createGain();
    overtone.frequency.value = 1624;
    overtoneGain.gain.value = 0.16;
    overtone.connect(overtoneGain);
    overtoneGain.connect(envelope);
    overtone.start();

    this.hoverResonance = { envelope, fundamental, overtone };
  }

  playGlassHover(shardIndex) {
    if (!this.isPlaying || this.isHidden || shardIndex < 0 || !this.hoverResonance) return;
    const now = this.ctx.currentTime;
    if (now - this.lastHoverChime < 0.22) return;
    this.lastHoverChime = now;

    const resonance = this.hoverResonance;
    const pitch = 960 + (shardIndex % 6) * 56;
    const gain = resonance.envelope.gain;
    resonance.fundamental.frequency.setTargetAtTime(pitch, now, 0.012);
    resonance.overtone.frequency.setTargetAtTime(pitch * 1.45, now, 0.012);
    gain.cancelScheduledValues(now);
    gain.setTargetAtTime(0.012, now, 0.008);
    gain.setTargetAtTime(0, now + 0.025, 0.095);
  }

  _updateFilterTarget() {
    if (!this.ctx || !this.lowpass) return;
    const now = this.ctx.currentTime;
    let targetFreq = 920;
    if (this.currentAct === 2) {
      targetFreq = 520;
    } else if (this.currentAct === 3) {
      targetFreq = 360;
    } else if (this.isHovered) {
      targetFreq = 290;
    }
    this.lowpass.frequency.setTargetAtTime(targetFreq, now, 0.28);
  }

  async toggle() {
    if (this.togglePending) return this.isPlaying;
    this.togglePending = true;
    try {
      if (!this.ctx && !this._initContext()) return false;
      if (this.ctx.state === "suspended") await this.ctx.resume();
      this.isPlaying = !this.isPlaying;
      this._updateGain();
      this._updateFilterTarget();
      return this.isPlaying;
    } catch {
      return this.isPlaying;
    } finally {
      this.togglePending = false;
    }
  }

  _updateGain() {
    if (!this.masterGain) return;
    const target = this.isPlaying && !this.isHidden ? 0.22 : 0;
    this.masterGain.gain.setTargetAtTime(target, this.ctx.currentTime, 0.18);
  }

  setHidden(hidden) {
    this.isHidden = hidden;
    window.clearTimeout(this.suspensionTimer);
    if (!this.ctx) return;
    this._updateGain();
    if (hidden) {
      // Let the gain reach silence before stopping the audio graph.
      this.suspensionTimer = window.setTimeout(async () => {
        try {
          await this.ctx.suspend();
          if (!this.isHidden && this.isPlaying) await this.ctx.resume();
        } catch {
          // The browser can close the context while suspending it.
        }
      }, 740);
    } else if (this.isPlaying && this.ctx.state === "suspended") {
      this.ctx.resume().catch(() => {});
    }
  }

  setHoverState(hovered) {
    this.isHovered = Boolean(hovered);
    this._updateFilterTarget();
  }

  setAct(act) {
    this.currentAct = act;
    this._updateFilterTarget();
  }

  playGlassChime(pitchMultiplier = 1.0) {
    if (!this.isPlaying || this.isHidden || !this.ctx || !this.masterGain) return;
    const now = this.ctx.currentTime;
    const osc = this.ctx.createOscillator();
    const overtone = this.ctx.createOscillator();
    const overtoneGain = this.ctx.createGain();
    const env = this.ctx.createGain();
    const pitch = 528 * pitchMultiplier;

    osc.frequency.setValueAtTime(pitch, now);
    overtone.frequency.setValueAtTime(pitch * 2.27, now);
    overtoneGain.gain.setValueAtTime(0.14, now);
    overtoneGain.gain.setTargetAtTime(0, now + 0.018, 0.09);

    env.gain.setValueAtTime(0, now);
    env.gain.linearRampToValueAtTime(0.049, now + 0.012);
    env.gain.setTargetAtTime(0, now + 0.024, 0.20);

    osc.connect(env);
    overtone.connect(overtoneGain);
    overtoneGain.connect(env);
    env.connect(this.masterGain);

    osc.start(now);
    overtone.start(now);
    osc.stop(now + 1.05);
    overtone.stop(now + 0.65);
    osc.onended = () => env.disconnect();
  }

  playShatter() {
    if (!this.isPlaying || this.isHidden || !this.ctx || !this.masterGain) return;
    [1.0, 1.33, 1.78].forEach((mult, idx) => {
      window.setTimeout(() => this.playGlassChime(mult * 1.4), idx * 35);
    });
  }
}
