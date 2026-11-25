/* ==========================================================================
   MAVRICK — Act I: Procedural Three.js Night Botanical Valley
   Sculpted Obsidian Terrain · 13,600+ GLSL Blooms & Blades · Closed Spline
   ========================================================================== */

import * as THREE from "three";

const BOTANICAL_COUNT = 13600;
const DUST_COUNT = 1100;
const BOTANICAL_SECTORS = 24;
const SCATTER_MARGIN = 30;

const plantMotion = /* glsl */ `
  vec2 plantDrift(float time, float phase, float tile) {
    return vec2(sin(time * 0.72 + phase) * (tile == 1.0 ? 0.38 : 0.21),
      cos(time * 0.56 + phase) * 0.10);
  }

  vec2 pointerRepulsion(vec2 ndc, float dist, vec2 pointer, float aspect, out float highlight) {
    vec2 offset = (ndc - pointer) * vec2(aspect, 1.0);
    float cursorDist = length(offset);
    highlight = (1.0 - smoothstep(0.0, 0.27, cursorDist))
      * (1.0 - smoothstep(3.0, 40.0, dist));
    return offset / max(cursorDist, 0.0001) * vec2(1.0 / aspect, 1.0) * highlight * 0.13;
  }
`;

function seededRandom(seed) {
  let state = seed;
  return () => {
    state ^= state << 13;
    state ^= state >>> 17;
    state ^= state << 5;
    return (state >>> 0) / 4294967296;
  };
}

function createBotanicalAtlas() {
  const size = 256;
  const resolution = 2;
  const half = size / 2;
  const canvas = document.createElement("canvas");
  canvas.width = size * resolution;
  canvas.height = size * resolution;
  const ctx = canvas.getContext("2d");
  ctx.scale(resolution, resolution);

  // Tile 0 (Top-Left): 6-petal alpine night bloom (warm ivory + pale amber stamen)
  {
    const cx = half * 0.5;
    const cy = half * 0.5;
    ctx.save();
    ctx.translate(cx, cy);
    for (let i = 0; i < 6; i += 1) {
      ctx.save();
      ctx.rotate((i * Math.PI * 2) / 6 + Math.sin(i * 1.6) * 0.095);
      ctx.scale(0.92 + 0.10 * Math.cos(i * 2.3), 0.90 + 0.10 * Math.sin(i * 2.2));
      const grad = ctx.createRadialGradient(0, -14, 2, 0, -24, 38);
      grad.addColorStop(0, "rgba(245, 239, 226, 0.98)");
      grad.addColorStop(0.56, "rgba(239, 234, 222, 0.74)");
      grad.addColorStop(0.82, "rgba(222, 224, 221, 0.34)");
      grad.addColorStop(1, "rgba(245, 239, 226, 0.0)");
      ctx.fillStyle = grad;
      ctx.beginPath();
      ctx.moveTo(0, -3);
      ctx.bezierCurveTo(-15, -21, -11, -46, 2, -54);
      ctx.bezierCurveTo(16, -43, 13, -19, 0, -3);
      ctx.fill();
      ctx.strokeStyle = "rgba(255, 250, 239, 0.18)";
      ctx.lineWidth = 0.65;
      ctx.beginPath();
      ctx.moveTo(0, -9);
      ctx.quadraticCurveTo(-2, -25, 1, -43);
      ctx.stroke();
      ctx.restore();
    }
    const core = ctx.createRadialGradient(0, 0, 1, 0, 0, 15);
    core.addColorStop(0, "rgba(242, 216, 168, 0.98)");
    core.addColorStop(0.55, "rgba(232, 207, 160, 0.45)");
    core.addColorStop(1, "rgba(245, 239, 226, 0)");
    ctx.fillStyle = core;
    ctx.beginPath();
    ctx.arc(0, 0, 15, 0, Math.PI * 2);
    ctx.fill();
    ctx.fillStyle = "rgba(255, 241, 210, 0.72)";
    for (let seed = 0; seed < 10; seed += 1) {
      const a = seed * 2.39996;
      const r = 3.0 + seed * 0.48;
      ctx.beginPath();
      ctx.arc(Math.cos(a) * r, Math.sin(a) * r, 0.9, 0, Math.PI * 2);
      ctx.fill();
    }
    ctx.restore();
  }

  // Tile 1 (Top-Right): Slender curved botanical grass/fern blade
  {
    const cx = half * 1.5;
    const cy = half * 0.5;
    ctx.save();
    ctx.translate(cx, cy + 40);
    ctx.rotate(-0.18);
    const grad = ctx.createLinearGradient(0, 0, 0, -94);
    grad.addColorStop(0, "rgba(215, 218, 222, 0.16)");
    grad.addColorStop(0.45, "rgba(238, 235, 228, 0.78)");
    grad.addColorStop(1, "rgba(245, 239, 226, 0.0)");
    ctx.fillStyle = grad;
    ctx.beginPath();
    ctx.moveTo(0, 0);
    ctx.quadraticCurveTo(-15, -46, 3, -94);
    ctx.quadraticCurveTo(13, -46, 0, 0);
    ctx.fill();
    ctx.strokeStyle = "rgba(245, 239, 226, 0.27)";
    ctx.lineWidth = 0.8;
    ctx.beginPath();
    ctx.moveTo(0, -6);
    ctx.quadraticCurveTo(-2, -42, 2, -82);
    ctx.stroke();
    ctx.strokeStyle = "rgba(230, 229, 221, 0.15)";
    ctx.lineWidth = 0.55;
    for (let y = -20; y > -75; y -= 10) {
      ctx.beginPath();
      ctx.moveTo(0, y);
      ctx.lineTo(-4, y - 10);
      ctx.moveTo(0, y);
      ctx.lineTo(4, y - 9);
      ctx.stroke();
    }
    ctx.restore();
  }

  // Tile 2 (Bottom-Left): Delicate 4-petal wildflower star cluster
  {
    const cx = half * 0.5;
    const cy = half * 1.5;
    ctx.save();
    ctx.translate(cx, cy);
    for (let i = 0; i < 4; i += 1) {
      ctx.save();
      ctx.rotate((i * Math.PI) / 2 + 0.35);
      const grad = ctx.createLinearGradient(0, 0, 0, -50);
      grad.addColorStop(0, "rgba(245, 239, 226, 0.94)");
      grad.addColorStop(1, "rgba(205, 210, 218, 0)");
      ctx.fillStyle = grad;
      ctx.beginPath();
      ctx.moveTo(0, 0);
      ctx.quadraticCurveTo(-10, -25, 0, -50);
      ctx.quadraticCurveTo(10, -25, 0, 0);
      ctx.fill();
      ctx.restore();
    }
    ctx.fillStyle = "rgba(245, 239, 226, 0.95)";
    ctx.beginPath();
    ctx.arc(0, 0, 5.5, 0, Math.PI * 2);
    ctx.fill();
    ctx.restore();
  }

  // Tile 3 (Bottom-Right): Soft luminous pollen spore / mist mote
  {
    const cx = half * 1.5;
    const cy = half * 1.5;
    const grad = ctx.createRadialGradient(cx, cy, 1, cx, cy, 48);
    grad.addColorStop(0, "rgba(245, 239, 226, 0.96)");
    grad.addColorStop(0.35, "rgba(225, 224, 220, 0.45)");
    grad.addColorStop(1, "rgba(245, 239, 226, 0)");
    ctx.fillStyle = grad;
    ctx.beginPath();
    ctx.arc(cx, cy, 48, 0, Math.PI * 2);
    ctx.fill();
  }

  const tex = new THREE.CanvasTexture(canvas);
  tex.minFilter = THREE.LinearMipmapLinearFilter;
  tex.magFilter = THREE.LinearFilter;
  tex.needsUpdate = true;
  return tex;
}

function terrainNoise(x, z) {
  return (
    Math.sin(x * 0.045) * Math.cos(z * 0.042) * 3.2 +
    Math.sin(x * 0.11 + z * 0.09) * 1.15 +
    Math.cos(x * 0.23 - z * 0.18) * 0.35
  );
}

function valleyFloorY(spineY, lat, x, z) {
  const lateral = Math.abs(lat) / 34;
  const bankHeight = lat > 0 ? 11.8 : 14.6;
  const slopeLift = Math.pow(lateral, 1.58) * bankHeight;
  const erosion = Math.sin(lateral * 8.0 + x * 0.071 + z * 0.039) * lateral * 0.64;
  return spineY - 3.35 + slopeLift + terrainNoise(x, z) * 0.45 + erosion;
}

export class NightBotanicalValley {
  constructor(scene) {
    this.scene = scene;
    this.group = new THREE.Group();
    this.scene.add(this.group);

    this.spline = this._buildSpline();
    this.splineT = 0.0;
    this.cameraPosition = new THREE.Vector3();
    this.cameraAim = new THREE.Vector3();
    this.cameraAhead = new THREE.Vector3();
    this.bankIncoming = new THREE.Vector3();
    this.bankOutgoing = new THREE.Vector3();
    this.scrollVelocity = 0.0;
    this.idleSpeed = 0.0052;
    this.hoverTarget = 0.0;
    this.hoverMix = 0.0;
    this.handoffTarget = 0.0;
    this.handoffMix = 0.0;
    this.qualityTarget = 1.0;
    this.quality = 1.0;
    this.botanicalSectors = [];
    this.fadeTarget = 1.0;
    this.sceneFade = 1.0;

    this.pointerNDC = new THREE.Vector2(9, 9);
    this.smoothedPointer = new THREE.Vector2(0, 0);
    this.targetParallax = new THREE.Vector2(0, 0);

    this.atlas = createBotanicalAtlas();
    this._buildValleyTerrain();
    this._buildBotanicalField();
    this._buildDustMotes();
    this._buildLightShafts();
  }

  _buildSpline() {
    const points = [];
    const count = 18;
    for (let i = 0; i < count; i += 1) {
      const angle = (i / count) * Math.PI * 2;
      const radius = 128 + Math.sin(angle * 3.0) * 18 + Math.cos(angle * 2.0) * 11;
      const x = Math.cos(angle) * radius;
      const z = Math.sin(angle) * radius;
      const y = terrainNoise(x, z) * 0.42 + 3.4 + Math.sin(angle * 4.0) * 1.1;
      points.push(new THREE.Vector3(x, y, z));
    }
    return new THREE.CatmullRomCurve3(points, true, "catmullrom", 0.5);
  }

  _buildValleyTerrain() {
    const rings = 220;
    const cols = 25;
    const halfWidth = 34;

    const vertices = [];
    const elevations = [];
    const lateralOffsets = [];
    const indices = [];

    const up = new THREE.Vector3(0, 1, 0);
    const side = new THREE.Vector3();

    for (let r = 0; r <= rings; r += 1) {
      const s = (r % rings) / rings;
      const spinePt = this.spline.getPointAt(s);
      const tangent = this.spline.getTangentAt(s);
      side.crossVectors(tangent, up).normalize();

      for (let c = 0; c < cols; c += 1) {
        const u = c / (cols - 1);
        const lat = (u - 0.5) * 2 * halfWidth;
        const px = spinePt.x + side.x * lat;
        const pz = spinePt.z + side.z * lat;
        const py = valleyFloorY(spinePt.y, lat, px, pz) - 0.15;

        vertices.push(px, py, pz);
        elevations.push(py - (spinePt.y - 3.35));
        lateralOffsets.push(lat / halfWidth);
      }
    }

    for (let r = 0; r < rings; r += 1) {
      for (let c = 0; c < cols - 1; c += 1) {
        const a = r * cols + c;
        const b = a + 1;
        const d = (r + 1) * cols + c;
        const e = d + 1;
        indices.push(a, b, d);
        indices.push(b, e, d);
      }
    }

    const geo = new THREE.BufferGeometry();
    geo.setAttribute("position", new THREE.Float32BufferAttribute(vertices, 3));
    geo.setAttribute("aElev", new THREE.Float32BufferAttribute(elevations, 1));
    geo.setAttribute("aLateral", new THREE.Float32BufferAttribute(lateralOffsets, 1));
    geo.setIndex(indices);
    geo.computeVertexNormals();

    this.terrainUniforms = {
      uSceneFade: { value: 1.0 },
    };

    const mat = new THREE.ShaderMaterial({
      uniforms: this.terrainUniforms,
      vertexShader: /* glsl */ `
        attribute float aElev;
        attribute float aLateral;
        varying float vElev;
        varying float vLateral;
        varying float vDist;
        varying vec3 vNormalView;
        void main() {
          vElev = aElev;
          vLateral = aLateral;
          vec4 mv = modelViewMatrix * vec4(position, 1.0);
          vDist = -mv.z;
          vNormalView = normalize(normalMatrix * normal);
          gl_Position = projectionMatrix * mv;
        }
      `,
      fragmentShader: /* glsl */ `
        uniform float uSceneFade;
        varying float vElev;
        varying float vLateral;
        varying float vDist;
        varying vec3 vNormalView;
        void main() {
          float fog = exp(-max(vDist - 6.0, 0.0) * 0.036);
          float lateral = abs(vLateral);
          float ridgeEdge = 1.0 - smoothstep(0.76, 1.0, lateral);
          float rim = 1.0 - clamp(abs(vNormalView.z), 0.0, 1.0);
          rim *= rim;
          float ridgeGlow = smoothstep(-0.4, 7.8, vElev) * 0.101;
          float gully = smoothstep(0.04, 0.34, lateral) * 0.019;

          float level = vElev * 0.86;
          float contourWave = abs(fract(level) - 0.5);
          float contourWidth = max(0.024, fwidth(level) * 0.96);
          float contour = (1.0 - smoothstep(0.0, contourWidth, contourWave))
            * (1.0 - smoothstep(0.14, 0.42, contourWidth)) * 0.039;
          float relief = (ridgeGlow + rim * 0.033 + contour + gully) * ridgeEdge;
          float haze = smoothstep(14.0, 56.0, vDist) * (1.0 - smoothstep(62.0, 118.0, vDist));
          vec3 abyss = vec3(5.0, 5.0, 7.0) / 255.0;
          vec3 graphite = vec3(0.78, 0.735, 0.668) * (relief * fog + haze * ridgeEdge * 0.017);
          gl_FragColor = vec4(abyss + graphite * uSceneFade, 1.0);
        }
      `,
    });

    this.terrainMesh = new THREE.Mesh(geo, mat);
    this.group.add(this.terrainMesh);
  }

  _createStemMaterial() {
    return new THREE.ShaderMaterial({
      uniforms: this.botanicalUniforms,
      transparent: true,
      depthWrite: false,
      blending: THREE.AdditiveBlending,
      vertexShader: /* glsl */ `
        attribute vec3 aTip;
        attribute vec3 aStem;
        uniform float uTime;
        uniform vec2 uPointerNDC;
        uniform float uAspect;
        uniform float uReach;
        uniform float uHoverMix;
        varying float vOpacity;

        ${plantMotion}

        void main() {
          vec4 tip = modelViewMatrix * vec4(aTip, 1.0);
          float depth = length(tip.xyz);
          float reach = min(31.0, uReach * 0.4);
          if (depth > reach || tip.z > -0.4 || uHoverMix >= 0.8) {
            vOpacity = 0.0;
            gl_Position = vec4(2.0, 2.0, 2.0, 1.0);
            return;
          }

          float bend = aStem.x * aStem.x;
          float phase = aStem.y * 6.28318 + aTip.x * 0.14 + aTip.z * 0.11;
          vec2 sway = plantDrift(uTime, phase, aStem.z);
          tip.xyz += mat3(modelViewMatrix) * vec3(sway, 0.0);
          vec4 tipClip = projectionMatrix * tip;
          vec2 ndc = tipClip.xy / max(abs(tipClip.w), 0.001);
          float highlight;
          vec2 push = pointerRepulsion(ndc, -tip.z, uPointerNDC, uAspect, highlight);

          vec3 stem = position;
          stem.xy += sway * bend;
          gl_Position = projectionMatrix * modelViewMatrix * vec4(stem, 1.0);
          gl_Position.xy += push * bend * gl_Position.w;
          float tipFalloff = 0.25 + aStem.x * 0.69;
          float distanceFade = 1.0 - smoothstep(reach * 0.42, reach, depth);
          float release = 1.0 - smoothstep(0.0, 0.64, uHoverMix);
          vOpacity = tipFalloff * distanceFade * release * (0.23 + highlight * 0.14);
        }
      `,
      fragmentShader: /* glsl */ `
        uniform float uSceneFade;
        varying float vOpacity;
        void main() {
          gl_FragColor = vec4(0.74, 0.70, 0.61, vOpacity * uSceneFade);
        }
      `,
    });
  }

  _buildBotanicalField() {
    this.botanicalUniforms = {
      uTime: { value: 0 },
      uPixelRatio: { value: Math.min(window.devicePixelRatio || 1, 2) },
      uAtlas: { value: this.atlas },
      uPointerNDC: { value: new THREE.Vector2(9, 9) },
      uAspect: { value: window.innerWidth / Math.max(window.innerHeight, 1) },
      uHoverMix: { value: 0 },
      uReach: { value: 88 },
      uSceneFade: { value: 1 },
    };

    const material = new THREE.ShaderMaterial({
      uniforms: this.botanicalUniforms,
      transparent: true,
      depthWrite: false,
      blending: THREE.AdditiveBlending,
      vertexShader: /* glsl */ `
        attribute float aSeed;
        attribute float aTile;
        attribute float aColorMix;
        attribute vec3 aScatter;

        uniform float uTime;
        uniform float uPixelRatio;
        uniform vec2 uPointerNDC;
        uniform float uAspect;
        uniform float uHoverMix;
        uniform float uReach;

        varying float vTile;
        varying float vAlpha;
        varying vec3 vTint;
        varying float vHighlight;
        varying float vDisperse;

        ${plantMotion}

        void main() {
          float phase = aSeed * 6.28318 + position.x * 0.14 + position.z * 0.11;
          vec3 worldPos = position;
          vec2 sway = plantDrift(uTime, phase, aTile);
          worldPos.xy += sway;

          vec4 mvPosition = modelViewMatrix * vec4(worldPos, 1.0);
          float dist = -mvPosition.z;

          float fgFactor = 1.0 - smoothstep(3.0, 37.0, dist);
          vDisperse = uHoverMix * fgFactor;
          if (vDisperse > 0.001) {
            float lift = vDisperse * (7.0 + aSeed * 5.0);
            vec3 drift = vec3(sway.x * 2.15, sin(uTime * 0.49 + phase) * 0.55, 0.0);
            mvPosition.xyz += mat3(modelViewMatrix) * (aScatter * lift + drift * vDisperse);
            dist = -mvPosition.z;
          }

          float nearClip = smoothstep(0.85, 3.0, dist);
          float farCull = 1.0 - smoothstep(uReach * 0.58, uReach, length(mvPosition.xyz));
          if (farCull < 0.005 || nearClip < 0.001) {
            vAlpha = 0.0;
            gl_Position = vec4(2.0, 2.0, 2.0, 1.0);
            gl_PointSize = 1.0;
            return;
          }

          vTile = aTile;
          vec3 silver = vec3(0.86, 0.86, 0.85);
          vec3 amber = vec3(0.94, 0.86, 0.73);
          vTint = mix(vec3(0.98, 0.96, 0.91), silver, smoothstep(0.24, 0.86, aColorMix));
          vTint = mix(vTint, amber, smoothstep(0.84, 1.0, aColorMix));

          vec4 clipPos = projectionMatrix * mvPosition;
          vec2 ndc = clipPos.xy / max(abs(clipPos.w), 0.001);

          clipPos.xy += pointerRepulsion(ndc, dist, uPointerNDC, uAspect, vHighlight) * clipPos.w;

          float depthGrowth = mix(0.48, 1.48, 1.0 - smoothstep(3.8, 56.0, dist));
          float fogFade = exp(-max(dist - 4.0, 0.0) * 0.036);
          float titleSpace = smoothstep(-0.02, 0.51, ndc.y) * (1.0 - smoothstep(-0.18, 0.34, ndc.x));
          vAlpha = nearClip * fogFade * farCull * (0.48 + aSeed * 0.48) * (1.0 - titleSpace * 0.31);
          float baseSize = mix(21.0, 52.0, aSeed);
          if (aTile == 1.0) baseSize *= 1.03;
          if (aTile == 3.0) baseSize *= 0.56;
          float disperseScale = mix(1.0, 0.29, vDisperse);

          gl_Position = clipPos;
          gl_PointSize = clamp((baseSize * depthGrowth * disperseScale * uPixelRatio) / max(dist * 0.14, 1.0), 1.0, 118.0);
        }
      `,
      fragmentShader: /* glsl */ `
        uniform sampler2D uAtlas;
        uniform float uSceneFade;

        varying float vTile;
        varying float vAlpha;
        varying vec3 vTint;
        varying float vHighlight;
        varying float vDisperse;

        void main() {
          if (vAlpha <= 0.005 || uSceneFade <= 0.005) discard;

          float col = floor(mod(vTile + 0.1, 2.0));
          float row = floor((vTile + 0.1) * 0.5);

          // The atlas is padded inside each quadrant for clean mip sampling.
          vec2 spriteCoord = mix(vec2(0.045), vec2(0.955), gl_PointCoord);
          vec2 uv = vec2(
            (col + spriteCoord.x) * 0.5,
            1.0 - (row + spriteCoord.y) * 0.5
          );
          float disperse = smoothstep(0.10, 0.72, vDisperse);
          vec4 texel = texture2D(uAtlas, uv);
          if (disperse > 0.001) {
            vec2 pollenUV = vec2(0.5 + spriteCoord.x * 0.5, 0.5 - spriteCoord.y * 0.5);
            texel = mix(texel, texture2D(uAtlas, pollenUV), disperse);
          }
          if (texel.a < 0.022) discard;
          vec3 tint = mix(vTint, vec3(1.0, 0.98, 0.94), vHighlight * 0.74);
          float alpha = texel.a * vAlpha * uSceneFade * (1.0 + vHighlight * 0.6);
          gl_FragColor = vec4(texel.rgb * tint, clamp(alpha, 0.0, 1.0));
        }
      `,
    });

    const stemMaterial = this._createStemMaterial();
    const random = seededRandom(0x681f45);
    const spine = new THREE.Vector3();
    const tangent = new THREE.Vector3();
    const side = new THREE.Vector3();
    const up = new THREE.Vector3(0, 1, 0);
    // Cache the closed path frame so planting does not evaluate the spline
    // three times for every individual stem.
    const samples = 720;
    const pathFrames = new Float32Array((samples + 1) * 5);
    for (let i = 0; i <= samples; i += 1) {
      const t = (i % samples) / samples;
      this.spline.getPointAt(t, spine);
      this.spline.getTangentAt(t, tangent);
      side.crossVectors(tangent, up).normalize();
      pathFrames[i * 5] = spine.x;
      pathFrames[i * 5 + 1] = spine.y;
      pathFrames[i * 5 + 2] = spine.z;
      pathFrames[i * 5 + 3] = side.x;
      pathFrames[i * 5 + 4] = side.z;
    }

    for (let sector = 0; sector < BOTANICAL_SECTORS; sector += 1) {
      const start = Math.floor(sector * BOTANICAL_COUNT / BOTANICAL_SECTORS);
      const end = Math.floor((sector + 1) * BOTANICAL_COUNT / BOTANICAL_SECTORS);
      const count = end - start;
      const positions = new Float32Array(count * 3);
      const seeds = new Float32Array(count);
      const tiles = new Float32Array(count);
      const colorMix = new Float32Array(count);
      const scatter = new Float32Array(count * 3);
      const stemPositions = [];
      const stemTips = [];
      const stemMotion = [];
      for (let i = 0; i < count; i += 1) {
        const position = (start + i + random()) * samples / BOTANICAL_COUNT;
        const sample = Math.floor(position);
        const fraction = position - sample;
        const offset = sample * 5;
        spine.set(
          THREE.MathUtils.lerp(pathFrames[offset], pathFrames[offset + 5], fraction),
          THREE.MathUtils.lerp(pathFrames[offset + 1], pathFrames[offset + 6], fraction),
          THREE.MathUtils.lerp(pathFrames[offset + 2], pathFrames[offset + 7], fraction)
        );
        side.set(
          THREE.MathUtils.lerp(pathFrames[offset + 3], pathFrames[offset + 8], fraction),
          0,
          THREE.MathUtils.lerp(pathFrames[offset + 4], pathFrames[offset + 9], fraction)
        );

        // A close understory, flowering slopes and a sparse outer ridge.
        const stratum = random();
        const spread = random();
        const sign = random() < 0.5 ? -1 : 1;
        const lat = sign * (stratum < 0.39
          ? 1.65 + spread * spread * 10.5
          : stratum < 0.82 ? 7 + spread * 17 : 20 + spread * 13);
        const x = spine.x + side.x * lat + (random() - 0.5) * 1.5;
        const z = spine.z + side.z * lat + (random() - 0.5) * 1.5;
        const species = random();
        const tile = species < 0.34 ? 0 : species < 0.70 ? 1 : species < 0.94 ? 2 : 3;
        const stemHeight = tile === 1 ? 0.24 + random() * 0.94 : 0.32 + random() * 1.48;
        const ground = valleyFloorY(spine.y, lat, x, z);
        const tip = ground + stemHeight;
        const vertex = i * 3;
        positions[vertex] = x;
        positions[vertex + 1] = tip;
        positions[vertex + 2] = z;
        seeds[i] = random();
        tiles[i] = tile;
        colorMix[i] = random();
        scatter[vertex] = side.x * sign * (0.65 + random() * 0.82) + (random() - 0.5) * 0.45;
        scatter[vertex + 1] = 0.56 + random() * 1.12;
        scatter[vertex + 2] = side.z * sign * (0.65 + random() * 0.82) + (random() - 0.5) * 0.45;

        if (tile !== 3 && (stratum < 0.53 || tile === 1 && stratum < 0.64)) {
          const direction = 1 - seeds[i] * 2;
          const leanX = direction * (0.17 + 0.10 * tile);
          const leanZ = direction * 0.13;
          const node = ground + stemHeight * 0.53;
          stemPositions.push(
            x - leanX, ground - 0.06, z - leanZ,
            x - leanX * 0.79, node, z - leanZ * 0.66,
            x - leanX * 0.79, node, z - leanZ * 0.66,
            x, tip, z
          );
          stemTips.push(x, tip, z, x, tip, z, x, tip, z, x, tip, z);
          stemMotion.push(0, seeds[i], tile, 0.53, seeds[i], tile, 0.53, seeds[i], tile, 1, seeds[i], tile);
        }
      }

      const geometry = new THREE.BufferGeometry();
      geometry.setAttribute("position", new THREE.BufferAttribute(positions, 3));
      geometry.setAttribute("aSeed", new THREE.BufferAttribute(seeds, 1));
      geometry.setAttribute("aTile", new THREE.BufferAttribute(tiles, 1));
      geometry.setAttribute("aColorMix", new THREE.BufferAttribute(colorMix, 1));
      geometry.setAttribute("aScatter", new THREE.BufferAttribute(scatter, 3));
      geometry.computeBoundingSphere();
      geometry.boundingSphere.radius += SCATTER_MARGIN;
      const stems = new THREE.BufferGeometry();
      stems.setAttribute("position", new THREE.Float32BufferAttribute(stemPositions, 3));
      stems.setAttribute("aTip", new THREE.Float32BufferAttribute(stemTips, 3));
      stems.setAttribute("aStem", new THREE.Float32BufferAttribute(stemMotion, 3));
      stems.computeBoundingSphere();
      stems.boundingSphere.radius += 1.2;

      const points = new THREE.Points(geometry, material);
      points.add(new THREE.LineSegments(stems, stemMaterial));
      this.botanicalSectors.push(points);
      this.group.add(points);
    }
  }

  _buildDustMotes() {
    const positions = new Float32Array(DUST_COUNT * 3);
    const seeds = new Float32Array(DUST_COUNT);
    const random = seededRandom(0xcb23a1);
    const pt = new THREE.Vector3();

    for (let i = 0; i < DUST_COUNT; i += 1) {
      this.spline.getPointAt(random(), pt);
      positions[i * 3] = pt.x + (random() - 0.5) * 48;
      positions[i * 3 + 1] = pt.y + (random() - 0.35) * 16;
      positions[i * 3 + 2] = pt.z + (random() - 0.5) * 48;
      seeds[i] = random();
    }

    const geometry = new THREE.BufferGeometry();
    geometry.setAttribute("position", new THREE.BufferAttribute(positions, 3));
    geometry.setAttribute("aSeed", new THREE.BufferAttribute(seeds, 1));

    this.dustUniforms = {
      uTime: { value: 0 },
      uPixelRatio: { value: Math.min(window.devicePixelRatio || 1, 2) },
      uSceneFade: { value: 1 },
    };

    const material = new THREE.ShaderMaterial({
      uniforms: this.dustUniforms,
      transparent: true,
      depthWrite: false,
      blending: THREE.AdditiveBlending,
      vertexShader: /* glsl */ `
        attribute float aSeed;
        uniform float uTime;
        uniform float uPixelRatio;
        varying float vAlpha;

        void main() {
          vec3 p = position;
          float breath = uTime * (0.13 + aSeed * 0.12) + aSeed * 18.0;
          p.y += sin(breath) * 1.65;
          p.x += cos(breath * 1.14) * 0.9;
          p.z += sin(breath * 0.53) * 0.8;

          vec4 mv = modelViewMatrix * vec4(p, 1.0);
          float dist = -mv.z;
          vAlpha = smoothstep(1.0, 4.0, dist) * exp(-max(dist, 0.0) * 0.032) * (0.22 + aSeed * 0.24);
          if (dist > 100.0 || dist < 0.8) {
            vAlpha = 0.0;
            gl_Position = vec4(2.0, 2.0, 2.0, 1.0);
            gl_PointSize = 1.0;
            return;
          }
          gl_Position = projectionMatrix * mv;
          gl_PointSize = clamp((7.0 + aSeed * 9.0) * uPixelRatio / max(dist * 0.14, 1.0), 1.0, 15.0);
        }
      `,
      fragmentShader: /* glsl */ `
        uniform float uSceneFade;
        varying float vAlpha;
        void main() {
          vec2 c = gl_PointCoord - 0.5;
          float r = dot(c, c);
          if (r > 0.25) discard;
          float soft = 1.0 - smoothstep(0.0, 0.25, r);
          soft *= soft;
          gl_FragColor = vec4(0.96, 0.94, 0.89, soft * vAlpha * uSceneFade);
        }
      `,
    });

    this.dustPoints = new THREE.Points(geometry, material);
    this.dustPoints.frustumCulled = false;
    this.group.add(this.dustPoints);
  }

  _buildLightShafts() {
    this.shaftGroup = new THREE.Group();
    const shaftGeo = new THREE.PlaneGeometry(18, 46);

    this.shaftUniform = { uSceneFade: { value: 1 } };
    const shaftMat = new THREE.ShaderMaterial({
      uniforms: this.shaftUniform,
      transparent: true,
      depthWrite: false,
      side: THREE.DoubleSide,
      forceSinglePass: true,
      blending: THREE.AdditiveBlending,
      vertexShader: /* glsl */ `
        varying vec2 vUv;
        void main() {
          vUv = uv;
          gl_Position = projectionMatrix * modelViewMatrix * vec4(position, 1.0);
        }
      `,
      fragmentShader: /* glsl */ `
        uniform float uSceneFade;
        varying vec2 vUv;
        void main() {
          float axis = abs(vUv.x - 0.5);
          float width = mix(0.46, 0.19, vUv.y);
          float softEdge = 1.0 - smoothstep(0.0, width, axis);
          float vert = smoothstep(0.0, 0.38, vUv.y) * (1.0 - smoothstep(0.58, 1.0, vUv.y));
          float alpha = softEdge * softEdge * vert * 0.04 * uSceneFade;
          gl_FragColor = vec4(0.95, 0.92, 0.86, alpha);
        }
      `,
    });

    for (let i = 0; i < 8; i += 1) {
      const mesh = new THREE.Mesh(shaftGeo, shaftMat);
      const pt = this.spline.getPointAt(i / 8);
      mesh.position.set(pt.x, pt.y + 11, pt.z);
      mesh.rotation.z = -0.32 + (i % 2) * 0.14;
      mesh.rotation.y = (i / 8) * Math.PI * 2;
      this.shaftGroup.add(mesh);
    }

    this.group.add(this.shaftGroup);
  }

  addScrollImpulse(delta) {
    this.scrollVelocity += delta * 0.000032;
    this.scrollVelocity = Math.max(-0.08, Math.min(0.08, this.scrollVelocity));
  }

  setPointerNDC(x, y) {
    this.pointerNDC.set(x, y);
    this.targetParallax.set(Math.abs(x) <= 1 ? x : 0, Math.abs(y) <= 1 ? y : 0);
  }

  startHoverTransition(active = true) {
    this.hoverTarget = active ? 1.0 : 0.0;
  }

  setAct(act) {
    this.handoffTarget = act === 1 ? 0 : 1;
    this.fadeTarget = act === 1 ? 1.0 : act === 2 ? 0.16 : 0.025;
  }

  setQuality(quality) {
    this.qualityTarget = THREE.MathUtils.clamp(quality, 0.65, 1);
  }

  onResize(width, height, pixelRatio) {
    if (this.botanicalUniforms) {
      this.botanicalUniforms.uAspect.value = width / Math.max(height, 1);
      this.botanicalUniforms.uPixelRatio.value = pixelRatio;
    }
    if (this.dustUniforms) {
      this.dustUniforms.uPixelRatio.value = pixelRatio;
    }
  }

  update(dt, elapsed, camera) {
    const hoverDamp = 1 - Math.exp(-dt * 6.8);
    const handoffDamp = 1 - Math.exp(-dt * 7.5);
    this.hoverMix += (this.hoverTarget - this.hoverMix) * hoverDamp;
    this.handoffMix += (this.handoffTarget - this.handoffMix) * handoffDamp;
    this.sceneFade += (this.fadeTarget - this.sceneFade) * (1 - Math.exp(-dt * 3.45));
    this.quality += (this.qualityTarget - this.quality) * (1 - Math.exp(-dt * 2.6));

    if (Math.abs(this.hoverTarget - this.hoverMix) < 0.001) this.hoverMix = this.hoverTarget;
    const dispersion = Math.max(this.hoverMix, this.handoffMix);
    const drag = Math.exp(-dt * 4.2);
    const scrollDistance = this.scrollVelocity * (1 - drag) / 4.2;
    this.scrollVelocity *= drag;
    const drive = (1 - this.hoverMix) * (1 - this.handoffMix * 0.9);
    this.splineT = (this.splineT + (this.idleSpeed * dt + scrollDistance) * drive + 1) % 1;

    this.smoothedPointer.lerp(this.targetParallax, 1 - Math.exp(-dt * 5.0));

    const pos = this.spline.getPointAt(this.splineT, this.cameraPosition);
    const ahead = this.spline.getPointAt((this.splineT + 0.016) % 1, this.cameraAim);
    const further = this.spline.getPointAt((this.splineT + 0.032) % 1, this.cameraAhead);

    camera.position.copy(pos);
    camera.lookAt(ahead);

    const d1 = this.bankIncoming.subVectors(ahead, pos).normalize();
    const d2 = this.bankOutgoing.subVectors(further, ahead).normalize();
    const crossY = d1.x * d2.z - d1.z * d2.x;
    const bank = THREE.MathUtils.clamp(crossY * 0.18, -0.05, 0.05) * (1 - this.handoffMix);
    const parallax = 1 - this.handoffMix * 0.93;

    camera.rotateY(-this.smoothedPointer.x * 0.08 * parallax);
    camera.rotateX(this.smoothedPointer.y * 0.05 * parallax);
    camera.rotateZ(bank);

    const reach = this.quality * (88 - this.handoffMix * 24);
    const swayPadding = 1.2 + SCATTER_MARGIN * dispersion;
    const stemReach = Math.min(31, reach * 0.4);
    for (const points of this.botanicalSectors) {
      const bounds = points.geometry.boundingSphere;
      const visibleRadius = reach + bounds.radius - SCATTER_MARGIN + swayPadding;
      points.visible = this.sceneFade > 0.031 &&
        bounds.center.distanceToSquared(pos) < visibleRadius * visibleRadius;
      const stems = points.children[0];
      const stemBounds = stems.geometry.boundingSphere;
      const stemRadius = stemReach + stemBounds.radius;
      stems.visible = dispersion < 0.7 &&
        stemBounds.center.distanceToSquared(pos) < stemRadius * stemRadius;
    }

    this.terrainUniforms.uSceneFade.value = this.sceneFade * (1 - this.handoffMix * 0.34);
    this.botanicalUniforms.uTime.value = elapsed;
    this.botanicalUniforms.uPointerNDC.value.lerp(this.pointerNDC, 1 - Math.exp(-dt * 14));
    this.botanicalUniforms.uHoverMix.value = dispersion;
    this.botanicalUniforms.uReach.value = reach;
    this.botanicalUniforms.uSceneFade.value = this.sceneFade;

    this.dustUniforms.uTime.value = elapsed;
    this.dustUniforms.uSceneFade.value = Math.max(this.sceneFade, 0.09);
    this.shaftUniform.uSceneFade.value = this.sceneFade * (1 - this.handoffMix * 0.68);
    this.shaftGroup.visible = this.sceneFade > 0.039;
  }
}
