/* ==========================================================================
   MAVRICK — Act II (Smoked-Glass Command Monolith) & Act III (Shatter + Back Sliver)
   Five breathing shards · Per-shard projection · Canvas HUD
   ========================================================================== */

import * as THREE from "three";

const BEVEL_SEGMENTS = 3;
const CURVE_SEGMENTS = 8;
const ARRIVAL_DURATION = 0.58;
const ARRIVAL_STAGGER = 0.045;
const ARRIVAL_END = ARRIVAL_DURATION + 4 * ARRIVAL_STAGGER;
const PEARL = "vec3(0.96, 0.93, 0.86)";
const HUD_MONO = '"Heming", "IBM Plex Mono", monospace';
const HUD_UI = '"Heming", "Manrope", sans-serif';
const HUD_DISPLAY = '"Migha", "OTF Glusp", "Antonio", Georgia, serif';
const HUD_SERIF = '"Instrument Serif", Georgia, serif';
const HOSTS = [
  { name: "Linux", transport: "Unix socket" },
  { name: "Windows", transport: "TCP token" },
  { name: "macOS", transport: "Unix socket" },
];
const LATENCY_LABEL = /^(.*?)(\d+\.\d+)( ms)$/;
const { clamp, lerp, smoothstep } = THREE.MathUtils;

/* --------------------------------------------------------------------------
   2D Projective Homography -> CSS matrix3d(col-major 4x4)
   -------------------------------------------------------------------------- */
export function computeMatrix3d(w, h, corners) {
  const p0 = corners[0];
  const p1 = corners[1];
  const p2 = corners[2];
  const p3 = corners[3];
  const ax = p1.x - p2.x;
  const ay = p1.y - p2.y;
  const bx = p3.x - p2.x;
  const by = p3.y - p2.y;
  const cx = p0.x - p1.x + p2.x - p3.x;
  const cy = p0.y - p1.y + p2.y - p3.y;
  const det = ax * by - bx * ay;
  if (Math.abs(det) < 1e-8) return null;

  const g = (cx * by - bx * cy) / det;
  const k = (ax * cy - cx * ay) / det;
  const xx = (p1.x * (1 + g) - p0.x) / w;
  const xy = (p1.y * (1 + g) - p0.y) / w;
  const yx = (p3.x * (1 + k) - p0.x) / h;
  const yy = (p3.y * (1 + k) - p0.y) / h;
  return `matrix3d(${xx},${xy},0,${g / w},${yx},${yy},0,${k / h},0,0,1,0,${p0.x},${p0.y},0,1)`;
}

function insetPolygon(pts, dist) {
  let area = 0;
  for (let i = 0; i < pts.length; i += 1) {
    const p1 = pts[i];
    const p2 = pts[(i + 1) % pts.length];
    area += p1.x * p2.y - p2.x * p1.y;
  }
  const ordered = area < 0 ? [...pts].reverse() : pts;

  const lines = [];
  for (let i = 0; i < ordered.length; i += 1) {
    const p1 = ordered[i];
    const p2 = ordered[(i + 1) % ordered.length];
    const dx = p2.x - p1.x;
    const dy = p2.y - p1.y;
    const len = Math.hypot(dx, dy) || 1;
    lines.push({
      ax: p1.x + (-dy / len) * dist,
      ay: p1.y + (dx / len) * dist,
      dx: dx / len,
      dy: dy / len,
    });
  }

  const out = [];
  for (let i = 0; i < lines.length; i += 1) {
    const l1 = lines[(i - 1 + lines.length) % lines.length];
    const l2 = lines[i];
    const det = l1.dx * l2.dy - l1.dy * l2.dx;
    if (Math.abs(det) < 1e-6) {
      out.push({ x: l2.ax, y: l2.ay });
      continue;
    }
    const rx = l2.ax - l1.ax;
    const ry = l2.ay - l1.ay;
    const t = (rx * l2.dy - ry * l2.dx) / det;
    out.push({ x: l1.ax + t * l1.dx, y: l1.ay + t * l1.dy });
  }
  return out;
}

function buildRoundedShape(pts, radius) {
  const shape = new THREE.Shape();
  const n = pts.length;
  for (let i = 0; i < n; i += 1) {
    const prev = pts[(i - 1 + n) % n];
    const curr = pts[i];
    const next = pts[(i + 1) % n];

    const v1x = prev.x - curr.x;
    const v1y = prev.y - curr.y;
    const v2x = next.x - curr.x;
    const v2y = next.y - curr.y;

    const len1 = Math.hypot(v1x, v1y) || 1;
    const len2 = Math.hypot(v2x, v2y) || 1;
    const r = Math.min(radius, len1 * 0.34, len2 * 0.34);

    const ax = curr.x + (v1x / len1) * r;
    const ay = curr.y + (v1y / len1) * r;
    const bx = curr.x + (v2x / len2) * r;
    const by = curr.y + (v2y / len2) * r;

    if (i === 0) shape.moveTo(ax, ay);
    else shape.lineTo(ax, ay);
    shape.quadraticCurveTo(curr.x, curr.y, bx, by);
  }
  shape.closePath();
  return shape;
}

function prepareDeformation(geometry, bounds) {
  const position = geometry.getAttribute("position");
  position.setUsage(THREE.DynamicDrawUsage);
  const weights = new Float32Array(position.count * 2);
  for (let i = 0; i < position.count; i += 1) {
    weights[i * 2] = (position.getX(i) - bounds.minX) / bounds.width;
    weights[i * 2 + 1] = (position.getY(i) - bounds.minY) / bounds.height;
  }
  return { position, weights };
}

function deformGeometry(geometry, bounds, sx, sy, shear) {
  const { position, weights } = geometry;
  const positions = position.array;
  for (let i = 0, weight = 0; i < positions.length; i += 3, weight += 2) {
    const x = weights[weight] * bounds.width + bounds.minX;
    const y = weights[weight + 1] * bounds.height + bounds.minY;
    positions[i] = x * sx + y * shear;
    positions[i + 1] = y * sy;
  }
  position.needsUpdate = true;
}

function printLabel(ctx, label, x, y, fontSize) {
  ctx.fillStyle = "rgba(245, 239, 226, 0.69)";
  ctx.font = `500 ${fontSize}px ${HUD_MONO}`;
  ctx.letterSpacing = `${fontSize * 0.14}px`;
  ctx.fillText(label, x, y);
  ctx.letterSpacing = "0px";
}

function etchRule(ctx, x, y, width, height = 1) {
  ctx.fillStyle = "rgba(245, 239, 226, 0.17)";
  ctx.fillRect(x, y, width, height);
}

function createSmokedGlassMaterial(cursorWorldUniform, sweepUniform) {
  const mat = new THREE.MeshPhysicalMaterial({
    color: 0x141418,
    emissive: 0x222226,
    emissiveIntensity: 0.72,
    roughness: 0.33,
    metalness: 0.10,
    clearcoat: 0.78,
    clearcoatRoughness: 0.10,
    iridescence: 0.42,
    iridescenceIOR: 1.25,
    iridescenceThicknessRange: [108, 150],
    envMapIntensity: 0.95,
    transparent: true,
    opacity: 0.94,
  });

  mat.userData.uCursorIntensity = { value: 0.0 };
  mat.userData.uDeformation = { value: new THREE.Vector3(1, 1, 0) };

  mat.onBeforeCompile = (shader) => {
    shader.uniforms.uCursorWorld = cursorWorldUniform;
    shader.uniforms.uCursorIntensity = mat.userData.uCursorIntensity;
    shader.uniforms.uDeformation = mat.userData.uDeformation;
    shader.uniforms.uSweep = sweepUniform;

    shader.vertexShader = shader.vertexShader
      .replace(
        "#include <common>",
        `#include <common>
         uniform vec3 uDeformation;
         varying vec3 vMavWorldPos;
         varying float vMavSweep;`
      )
      .replace(
        "#include <beginnormal_vertex>",
        `#include <beginnormal_vertex>
         objectNormal.x /= uDeformation.x;
         objectNormal.y = (objectNormal.y - objectNormal.x * uDeformation.z) / uDeformation.y;`
      )
      .replace(
        "#include <begin_vertex>",
        `#include <begin_vertex>
         vMavWorldPos = (modelMatrix * vec4(transformed, 1.0)).xyz;
         vec3 cameraSpace = (modelViewMatrix * vec4(transformed, 1.0)).xyz;
         vMavSweep = cameraSpace.x + cameraSpace.y * 0.42;`
      );

    shader.fragmentShader = shader.fragmentShader
      .replace(
        "#include <common>",
        `#include <common>
         uniform vec3 uCursorWorld;
         uniform float uCursorIntensity;
         uniform vec2 uSweep;
         varying vec3 vMavWorldPos;
         varying float vMavSweep;`
      )
      .replace(
        "#include <dithering_fragment>",
        `
         float cursorD = distance(vMavWorldPos, uCursorWorld);
         float sheen = exp(-cursorD * cursorD * 0.38) * uCursorIntensity;
         float band = (vMavSweep - uSweep.x) * 2.9;
         float bevel = smoothstep(0.05, 0.65, 1.0 - abs(normal.z));
         float sweep = exp(-band * band) * bevel * uSweep.y;
         gl_FragColor.rgb += ${PEARL} * (sheen * 0.105 + bevel * 0.027 + sweep * 0.25);
         #include <dithering_fragment>`
      );
  };

  return mat;
}

/* --------------------------------------------------------------------------
   Smoked Glass Monolith Controller
   -------------------------------------------------------------------------- */
export class SmokedGlassMonolith {
  constructor(camera, { anisotropy = 1 } = {}) {
    this.camera = camera;
    this.root = new THREE.Group();
    this.Z_DIST = 14.0;
    this.root.position.set(0, 0, -this.Z_DIST);
    this.camera.add(this.root);

    this.slabGroup = new THREE.Group();
    this.root.add(this.slabGroup);

    this.backSliverGroup = new THREE.Group();
    this.root.add(this.backSliverGroup);

    // Subtle cursor-tracking warm cream bevel light in front of glass
    this.cursorPointLight = new THREE.PointLight(0xf5efe2, 2.8, 16, 1.5);
    this.cursorPointLight.position.set(0, 0, 3.8);
    this.root.add(this.cursorPointLight);

    this.cursorWorldUniform = { value: new THREE.Vector3(0, 0, 0) };
    this.sweepUniform = { value: new THREE.Vector2() };
    this.raycaster = new THREE.Raycaster();
    this.pointerNDC = new THREE.Vector2(9, 9);

    this.isPortrait = false;
    this.S = 0.008;
    this.PANEL_W = 1000;
    this.PANEL_H = 560;
    this.NAV_W = 1000;
    this.NAV_H = 60;
    this.GAP_LOGICAL = 14;

    this.act = 1;
    this.entryMix = 0;
    this.entryTime = 0;
    this.controlsReady = false;
    this.shatterState = "assembled";
    this.shatterTimer = 0;
    this.shatterDuration = 0.72;
    this.reassembleDuration = 0.50;

    this.slabTilt = { rx: 0, ry: 0 };
    this.hoveredIndex = -1;
    this.corner = new THREE.Vector3();
    this.projectedCorners = Array.from({ length: 4 }, () => new THREE.Vector2());
    this.pickMatrix = new THREE.Matrix4();
    this.localRay = new THREE.Ray();
    this.localHit = new THREE.Vector3();
    this.hoverHit = new THREE.Vector3();
    this.facePlane = new THREE.Plane(new THREE.Vector3(0, 0, 1));
    this.layouts = new Map();
    this.anisotropy = Math.min(8, anisotropy);
    this.latencyReel = null;

    this.panelCanvas = document.createElement("canvas");
    this.panelTex = null;

    this.navCanvas = document.createElement("canvas");
    this.navTex = null;

    this.backCanvas = document.createElement("canvas");
    this.backTex = null;

    this.hudData = {
      inputFocused: false,
      query: "open files, vlc and vscode",
      badge: "Instant · 0.039 ms",
      badgeTone: "sage",
      title: "Launched: Files, VLC, VS Code",
      detail: "cosmic-files.desktop · org.videolan.VLC.desktop · code.desktop · 0 LLM calls",
      activePreset: "open files, vlc and vscode",
      primaryBtn: "",
      secondaryBtn: "",
      isDestructive: false,
    };

    this.measuredPresetRects = [];
    this.shards = [];
    this.navShard = null;
    this.backSliver = null;

    this._initMeshes();
    this.slabs = [this.navShard, ...this.shards];
    this.pickableSlabs = [...this.slabs, this.backSliver];
    this.onResize(window.innerWidth, window.innerHeight);
  }

  _initMeshes() {
    for (let i = 0; i < 6; i += 1) {
      const isNav = i === 5;
      const pivot = new THREE.Group();
      this.slabGroup.add(pivot);

      const glassMat = createSmokedGlassMaterial(this.cursorWorldUniform, this.sweepUniform);
      const faceMat = new THREE.MeshBasicMaterial({
        transparent: true,
        toneMapped: false,
        depthWrite: false,
        polygonOffset: true,
        polygonOffsetFactor: -1,
      });

      const bodyMesh = new THREE.Mesh(new THREE.BufferGeometry(), glassMat);
      const faceMesh = new THREE.Mesh(new THREE.BufferGeometry(), faceMat);
      bodyMesh.frustumCulled = false;
      faceMesh.frustumCulled = false;
      bodyMesh.userData.shardIndex = i;
      faceMesh.userData.shardIndex = i;

      pivot.add(bodyMesh);
      pivot.add(faceMesh);

      const record = {
        index: i,
        isNav,
        pivot,
        bodyMesh,
        faceMesh,
        glassMat,
        faceMat,
        cx: 0,
        cy: 0,
        visibleDOM: false,
        arrivalMix: 0,
        arrivalDelay: isNav ? 0.09 : i * ARRIVAL_STAGGER,
        basePos: new THREE.Vector3(0, 0, 0),
        liftZ: 0,
        tipX: 0,
        tipY: 0,
        targetLiftZ: 0,
        targetTipX: 0,
        targetTipY: 0,
        shatterDelay: isNav ? 0 : 0.025 + i * 0.022,
        shatterVel: new THREE.Vector3(),
        shatterRot: new THREE.Vector3(),
        motionOrigin: new THREE.Vector3(),
        motionRotation: new THREE.Euler(),
        motionOpacity: 0,
      };

      if (isNav) this.navShard = record;
      else this.shards.push(record);
    }

    const backGlassMat = createSmokedGlassMaterial(this.cursorWorldUniform, this.sweepUniform);
    const backFaceMat = new THREE.MeshBasicMaterial({
      transparent: true,
      depthWrite: false,
      polygonOffset: true,
      polygonOffsetFactor: -1,
    });
    const backBody = new THREE.Mesh(new THREE.BufferGeometry(), backGlassMat);
    const backFace = new THREE.Mesh(new THREE.BufferGeometry(), backFaceMat);
    backBody.userData.shardIndex = 6;
    backFace.userData.shardIndex = 6;

    this.backSliver = {
      pivot: this.backSliverGroup,
      bodyMesh: backBody,
      faceMesh: backFace,
      glassMat: backGlassMat,
      faceMat: backFaceMat,
      hoverTipX: 0,
      hoverTipY: 0,
    };
    this.backSliverGroup.add(backBody);
    this.backSliverGroup.add(backFace);
    this.backSliverGroup.visible = false;
  }

  /* ------------------------------------------------------------------------
     Single Source of Truth Layout Rectangles (Canvas + Homography DOM Sync)
     ------------------------------------------------------------------------ */
  getLayoutRects() {
    const NW = this.NAV_W;

    const nav = !this.isPortrait
      ? {
          brand: { x: 26, y: 14, w: 130, h: 32 },
          valley: { x: NW * 0.5 - 150, y: 14, w: 84, h: 32 },
          monolith: { x: NW * 0.5 - 54, y: 14, w: 102, h: 32 },
          arch: { x: NW * 0.5 + 60, y: 14, w: 118, h: 32 },
          github: { x: NW - 122, y: 14, w: 96, h: 32 },
        }
      : {
          brand: { x: 20, y: 18, w: 112, h: 32 },
          valley: { x: 158, y: 18, w: 74, h: 32 },
          monolith: { x: 242, y: 18, w: 88, h: 32 },
          arch: { x: 340, y: 18, w: 110, h: 32 },
          github: { x: NW - 110, y: 18, w: 90, h: 32 },
        };

    if (!this.isPortrait) {
      return {
        nav,
        r0: { x: 30, y: 28, w: 530, h: 256 },
        r1: {
          x: 612,
          y: 28,
          w: 360,
          h: 256,
          input: { x: 612, y: 28, w: 298, h: 42 },
          runBtn: { x: 612 + 306, y: 33, w: 48, h: 32 },
          output: { x: 612, y: 84, w: 360, h: 150 },
          primaryBtn: { x: 612, y: 244, w: 144, h: 34 },
          secondaryBtn: { x: 612 + 154, y: 244, w: 118, h: 34 },
        },
        r2: {
          x: 30,
          y: 336,
          w: 396,
          h: 196,
          presetsArea: { x: 30, y: 360, w: 396, h: 83 },
          getMavrick: { x: 30, y: 468, w: 140, h: 42 },
          readArch: { x: 186, y: 468, w: 182, h: 42 },
        },
        r3: { x: 468, y: 344, w: 230, h: 184 },
        r4: { x: 746, y: 344, w: 224, h: 184 },
      };
    }

    return {
      nav,
      r0: { x: 28, y: 24, w: 564, h: 246 },
      r1: {
        x: 28,
        y: 308,
        w: 564,
        h: 228,
        input: { x: 28, y: 308, w: 476, h: 54 },
        runBtn: { x: 28 + 486, y: 314, w: 72, h: 42 },
        output: { x: 28, y: 372, w: 564, h: 120 },
        primaryBtn: { x: 28, y: 494, w: 144, h: 42 },
        secondaryBtn: { x: 28 + 154, y: 494, w: 120, h: 42 },
      },
      r2: {
        x: 28,
        y: 576,
        w: 564,
        h: 162,
        presetsArea: { x: 28, y: 596, w: 564, h: 81 },
        getMavrick: { x: 28, y: 684, w: 172, h: 46 },
        readArch: { x: 216, y: 684, w: 220, h: 46 },
      },
      r3: { x: 28, y: 766, w: 266, h: 88 },
      r4: { x: 326, y: 766, w: 266, h: 88 },
    };
  }

  onResize(width, height) {
    const aspect = width / Math.max(height, 1);
    this.isPortrait = aspect < 0.92;

    const visibleH = 2 * this.Z_DIST * Math.tan(THREE.MathUtils.degToRad(this.camera.fov * 0.5));
    const visibleW = visibleH * aspect;

    if (this.isPortrait) {
      this.PANEL_W = 620;
      this.PANEL_H = 880;
      this.NAV_W = 620;
      this.NAV_H = 68;
      this.S = Math.min((visibleW * 0.93) / 620, (visibleH * 0.82) / (68 + 14 + 880));
    } else {
      this.PANEL_W = 1000;
      this.PANEL_H = 560;
      this.NAV_W = 1000;
      this.NAV_H = 60;
      this.S = Math.min((visibleW * 0.74) / 1000, (visibleH * 0.79) / (60 + 14 + 560));
    }

    this.canvasDPR = Math.min(Math.max(window.devicePixelRatio || 1, 1.5), 2);

    this.panelTex = this._resizeHudTexture(this.panelCanvas, this.panelTex, this.PANEL_W, this.PANEL_H);
    this.navTex = this._resizeHudTexture(this.navCanvas, this.navTex, this.NAV_W, this.NAV_H);
    this.backTex = this._resizeHudTexture(this.backCanvas, this.backTex, 132, 46);
    for (const slab of this.pickableSlabs) {
      const texture = slab === this.backSliver ? this.backTex : slab.isNav ? this.navTex : this.panelTex;
      if (!slab.faceMat.map) slab.faceMat.needsUpdate = true;
      slab.faceMat.map = texture;
    }

    this.backSliverGroup.position.set(
      -visibleW * 0.5 + 88 * this.S + 0.36,
      visibleH * 0.5 - 42 * this.S - 0.28,
      0.4
    );

    this._applyLayout();
    this._finishLatencyReel();
    this.redrawAllCanvases();
  }

  _resizeHudTexture(canvas, current, logicalWidth, logicalHeight) {
    const width = Math.round(logicalWidth * this.canvasDPR);
    const height = Math.round(logicalHeight * this.canvasDPR);
    if (current && canvas.width === width && canvas.height === height) return current;
    current?.dispose();
    canvas.width = width;
    canvas.height = height;
    const texture = new THREE.CanvasTexture(canvas);
    texture.colorSpace = THREE.SRGBColorSpace;
    texture.anisotropy = this.anisotropy;
    texture.minFilter = THREE.LinearMipmapLinearFilter;
    texture.magFilter = THREE.LinearFilter;
    return texture;
  }

  _logicalToWorld(lx, ly, refW, refH) {
    return {
      x: (lx - refW * 0.5) * this.S,
      y: (refH * 0.5 - ly) * this.S,
    };
  }

  _computeShardPolygons() {
    const W = this.PANEL_W;
    const H = this.PANEL_H;

    if (!this.isPortrait) {
      const midY_L = 308;
      const midY_R = 321;
      const midYAt = (x) => midY_L + ((midY_R - midY_L) * x) / W;

      const topX0 = 584 + Math.sin(0.8) * 9;
      const topX1 = 590 + Math.cos(1.9) * 9;

      const botX1_top = 446 + Math.sin(2.3) * 8;
      const botX1_bot = 438 + Math.cos(2.9) * 8;

      const botX2_top = 720 + Math.sin(3.5) * 8;
      const botX2_bot = 728 + Math.cos(4.1) * 8;

      return [
        [{ x: 0, y: 0 }, { x: topX0, y: 0 }, { x: topX1, y: midYAt(topX1) }, { x: 0, y: midY_L }],
        [{ x: topX0, y: 0 }, { x: W, y: 0 }, { x: W, y: midY_R }, { x: topX1, y: midYAt(topX1) }],
        [{ x: 0, y: midY_L }, { x: botX1_top, y: midYAt(botX1_top) }, { x: botX1_bot, y: H }, { x: 0, y: H }],
        [{ x: botX1_top, y: midYAt(botX1_top) }, { x: botX2_top, y: midYAt(botX2_top) }, { x: botX2_bot, y: H }, { x: botX1_bot, y: H }],
        [{ x: botX2_top, y: midYAt(botX2_top) }, { x: W, y: midY_R }, { x: W, y: H }, { x: botX2_bot, y: H }],
      ];
    }

    const y1L = 282;
    const y1R = 294;
    const y2L = 552 + Math.sin(1.2) * 6;
    const y2R = 546 + Math.cos(2.1) * 6;
    const y3L = 746 + Math.sin(2.8) * 5;
    const y3R = 752 + Math.cos(3.4) * 5;
    const botX_top = 310 + Math.sin(0.5) * 8;
    const botX_bot = 310 + Math.cos(1.5) * 8;
    const y3At = (x) => y3L + ((y3R - y3L) * x) / W;

    return [
      [{ x: 0, y: 0 }, { x: W, y: 0 }, { x: W, y: y1R }, { x: 0, y: y1L }],
      [{ x: 0, y: y1L }, { x: W, y: y1R }, { x: W, y: y2R }, { x: 0, y: y2L }],
      [{ x: 0, y: y2L }, { x: W, y: y2R }, { x: W, y: y3R }, { x: 0, y: y3L }],
      [{ x: 0, y: y3L }, { x: botX_top, y: y3At(botX_top) }, { x: botX_bot, y: H }, { x: 0, y: H }],
      [{ x: botX_top, y: y3At(botX_top) }, { x: W, y: y3R }, { x: W, y: H }, { x: botX_bot, y: H }],
    ];
  }

  _buildExtrudedShardGeometry(worldPoly, insetWorld, radiusWorld, depthWorld, bevelWorld, mapBounds) {
    const insetPts = insetPolygon(worldPoly, insetWorld);
    const shape = buildRoundedShape(insetPts, radiusWorld);

    const extrudeGeo = new THREE.ExtrudeGeometry(shape, {
      depth: depthWorld,
      bevelEnabled: true,
      bevelThickness: bevelWorld,
      bevelSize: bevelWorld,
      bevelSegments: BEVEL_SEGMENTS,
      curveSegments: CURVE_SEGMENTS,
    });
    extrudeGeo.translate(0, 0, -depthWorld * 0.5);

    const faceGeo = new THREE.ShapeGeometry(shape, CURVE_SEGMENTS);
    faceGeo.translate(0, 0, depthWorld * 0.5 + bevelWorld + 0.002);

    const pos = faceGeo.attributes.position;
    const uv = faceGeo.attributes.uv;
    for (let i = 0; i < pos.count; i += 1) {
      const vx = pos.getX(i);
      const vy = pos.getY(i);
      uv.setXY(i, (vx - mapBounds.minX) / mapBounds.width, (vy - mapBounds.minY) / mapBounds.height);
    }
    uv.needsUpdate = true;

    return {
      extrudeGeo,
      faceGeo,
      insetPts,
      mapBounds,
      faceZ: faceGeo.attributes.position.getZ(0),
    };
  }

  _buildSlabGeometries() {
    const S = this.S;
    const inset = (this.GAP_LOGICAL * 0.5 + 2.6) * S;
    const radius = 20 * S;
    const depth = 13 * S;
    const bevel = 2.6 * S;

    const panelWWorld = this.PANEL_W * S;
    const panelHWorld = this.PANEL_H * S;
    const navHWorld = this.NAV_H * S;
    const gapWorld = this.GAP_LOGICAL * S;

    const totalH = navHWorld + gapWorld + panelHWorld;
    const panelCenterY = -(totalH * 0.5) + panelHWorld * 0.5;
    const navCenterY = totalH * 0.5 - navHWorld * 0.5;

    const panelMapBounds = {
      minX: -panelWWorld * 0.5,
      minY: -panelHWorld * 0.5,
      width: panelWWorld,
      height: panelHWorld,
    };

    const panels = this._computeShardPolygons().map((polygon) => {
      const worldPoly = polygon.map((p) => this._logicalToWorld(p.x, p.y, this.PANEL_W, this.PANEL_H));

      let cx = 0;
      let cy = 0;
      worldPoly.forEach((p) => {
        cx += p.x;
        cy += p.y;
      });
      cx /= worldPoly.length;
      cy /= worldPoly.length;

      const localPoly = worldPoly.map((p) => ({ x: p.x - cx, y: p.y - cy }));
      const localBounds = {
        minX: panelMapBounds.minX - cx,
        minY: panelMapBounds.minY - cy,
        width: panelMapBounds.width,
        height: panelMapBounds.height,
      };

      const mesh = this._buildExtrudedShardGeometry(
        localPoly,
        inset,
        radius,
        depth,
        bevel,
        localBounds
      );

      mesh.cx = cx;
      mesh.cy = cy;
      mesh.baseY = panelCenterY + cy;
      mesh.bodyDeformation = prepareDeformation(mesh.extrudeGeo, localBounds);
      mesh.faceDeformation = prepareDeformation(mesh.faceGeo, localBounds);
      return mesh;
    });

    const nav = this._buildRectangularSlab(this.NAV_W, this.NAV_H, inset * 0.4, 16, 10, 2.2);
    nav.baseY = navCenterY;
    const back = this._buildRectangularSlab(132, 46, S, 8, 9, 2);
    return { scale: S, meshes: [nav, ...panels, back] };
  }

  _buildRectangularSlab(width, height, inset, radius, depth, bevel) {
    const S = this.S;
    const bw = width * S;
    const bh = height * S;
    const poly = [
      { x: -bw * 0.5, y: -bh * 0.5 },
      { x: bw * 0.5, y: -bh * 0.5 },
      { x: bw * 0.5, y: bh * 0.5 },
      { x: -bw * 0.5, y: bh * 0.5 },
    ];
    const bounds = { minX: -bw * 0.5, minY: -bh * 0.5, width: bw, height: bh };
    return this._buildExtrudedShardGeometry(poly, inset, radius * S, depth * S, bevel * S, bounds);
  }

  _applyLayout() {
    const initialLayout = this.layouts.size === 0;
    let layout = this.layouts.get(this.isPortrait);
    if (!layout) {
      layout = this._buildSlabGeometries();
      this.layouts.set(this.isPortrait, layout);
    }

    this.geometryRatio = this.S / layout.scale;
    for (let i = 0; i < this.pickableSlabs.length; i += 1) {
      const slab = this.pickableSlabs[i];
      const mesh = layout.meshes[i];
      if (initialLayout) {
        slab.bodyMesh.geometry.dispose();
        slab.faceMesh.geometry.dispose();
      }
      slab.bodyMesh.geometry = mesh.extrudeGeo;
      slab.faceMesh.geometry = mesh.faceGeo;
      slab.bodyMesh.scale.setScalar(this.geometryRatio);
      slab.faceMesh.scale.setScalar(this.geometryRatio);
      slab.pickPolygon = mesh.insetPts;
      slab.faceZ = mesh.faceZ * this.geometryRatio;
      slab.cx = (mesh.cx || 0) * this.geometryRatio;
      slab.cy = (mesh.cy || 0) * this.geometryRatio;
      slab.deformMesh = mesh;
      if (slab !== this.backSliver) {
        const dx = slab.cx - slab.basePos.x;
        const dy = mesh.baseY * this.geometryRatio - slab.basePos.y;
        slab.basePos.x += dx;
        slab.basePos.y += dy;
        slab.pivot.position.x += dx;
        slab.pivot.position.y += dy;
        slab.motionOrigin.x += dx;
        slab.motionOrigin.y += dy;
      }
      if (mesh.bodyDeformation) this._deformSlab(slab, 1, 1, 0);
    }
  }

  /* ------------------------------------------------------------------------
     Offscreen Canvas Drawing + Synchronized Exact Button Coordinates
     ------------------------------------------------------------------------ */
  updateHUDData(data) {
    const oldBadge = this.hudData.badge;
    Object.assign(this.hudData, data);
    // While focused, the browser draws the editable text over the glass.
    if (this.hudData.inputFocused && Reflect.ownKeys(data).length === 1 && "query" in data) return;
    if (oldBadge !== this.hudData.badge) {
      this._finishLatencyReel();
      if (this.controlsReady) this._startLatencyReel(oldBadge);
    }
    this.redrawPanelCanvas();
  }

  _startLatencyReel(oldBadge) {
    const metric = LATENCY_LABEL.exec(this.hudData.badge);
    if (!metric) return;
    const stage = document.querySelector("#shard-dom-1 .shard-abs-stage");
    if (!stage) return;
    const previous = LATENCY_LABEL.exec(oldBadge)?.[2];
    const reel = document.createElement("span");
    reel.setAttribute("aria-hidden", "true");
    reel.style.cssText = "position:absolute;overflow:hidden;pointer-events:none;display:block;font-variant-numeric:tabular-nums;white-space:nowrap";
    const incoming = document.createElement("span");
    incoming.textContent = metric[2];
    incoming.style.display = "block";
    reel.append(incoming);
    if (previous) {
      const outgoing = document.createElement("span");
      outgoing.textContent = previous;
      outgoing.style.cssText = "position:absolute;inset:0";
      reel.append(outgoing);
      outgoing.animate([{ transform: "translateY(0)" }, { transform: "translateY(-105%)" }], {
        duration: 190, easing: "cubic-bezier(0.16, 1, 0.3, 1)", fill: "forwards",
      });
    }
    stage.append(reel);
    this.latencyReel = reel;
    const animation = incoming.animate([{ transform: "translateY(105%)" }, { transform: "translateY(0)" }], {
      duration: 210, easing: "cubic-bezier(0.16, 1, 0.3, 1)", fill: "forwards",
    });
    animation.onfinish = () => {
      if (this.latencyReel !== reel) return;
      this._finishLatencyReel();
      this.redrawPanelCanvas();
    };
  }

  _finishLatencyReel() {
    if (!this.latencyReel) return false;
    this.latencyReel.remove();
    this.latencyReel = null;
    return true;
  }

  redrawAllCanvases() {
    this.redrawNavCanvas();
    this.redrawPanelCanvas();
    this.redrawBackSliverCanvas();
    this.syncDomButtonRects();
  }

  redrawNavCanvas() {
    const ctx = this.navCanvas.getContext("2d");
    const dpr = this.canvasDPR;
    const W = this.NAV_W;
    const H = this.NAV_H;
    const { nav } = this.getLayoutRects();

    ctx.save();
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    ctx.clearRect(0, 0, W, H);

    ctx.textBaseline = "middle";
    ctx.textAlign = "left";
    ctx.fillStyle = "rgba(245, 239, 226, 0.94)";
    ctx.font = `600 ${this.isPortrait ? 20 : 13.5}px "Heming", "Manrope", sans-serif`;
    ctx.fillText("◇  Mavrick", nav.brand.x, nav.brand.y + nav.brand.h * 0.5);

    const drawCenteredNav = (rect, label, active) => {
      ctx.textAlign = "center";
      ctx.fillStyle = active ? "#f5efe2" : "rgba(245, 239, 226, 0.58)";
      ctx.font = `${active ? 600 : 500} ${this.isPortrait ? 20 : 12.5}px "Heming", "Manrope", sans-serif`;
      ctx.fillText(label, rect.x + rect.w * 0.5, rect.y + rect.h * 0.5);
      if (active) {
        ctx.fillStyle = "rgba(245, 239, 226, 0.72)";
        ctx.fillRect(rect.x + rect.w * 0.25, rect.y + rect.h - 4, rect.w * 0.5, 1);
      }
    };

    drawCenteredNav(nav.valley, "Valley", this.act === 1);
    drawCenteredNav(nav.monolith, "Monolith", this.act === 2);
    drawCenteredNav(nav.arch, "Architecture", this.act === 3);

    ctx.textAlign = "center";
    ctx.fillStyle = "rgba(245, 239, 226, 0.88)";
    ctx.font = `500 ${this.isPortrait ? 20 : 12.5}px "Heming", "Manrope", sans-serif`;
    ctx.fillText("GitHub ↗", nav.github.x + nav.github.w * 0.5, nav.github.y + nav.github.h * 0.5);

    ctx.restore();
    if (this.navTex) this.navTex.needsUpdate = true;
  }

  redrawBackSliverCanvas() {
    const ctx = this.backCanvas.getContext("2d");
    const dpr = this.canvasDPR;
    const W = 132;
    const H = 46;
    ctx.save();
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    ctx.clearRect(0, 0, W, H);

    ctx.fillStyle = "#f5efe2";
    ctx.font = '500 13.5px "Heming", "IBM Plex Mono", monospace';
    ctx.textAlign = "center";
    ctx.textBaseline = "middle";
    ctx.fillText("←  Back", W * 0.5, H * 0.5);
    ctx.restore();
    if (this.backTex) this.backTex.needsUpdate = true;
  }

  _drawWrappedText(ctx, text, x, y, maxWidth, lineHeight) {
    const words = text.split(" ");
    let line = "";
    let curY = y;
    for (let i = 0; i < words.length; i += 1) {
      const test = line ? `${line} ${words[i]}` : words[i];
      if (ctx.measureText(test).width > maxWidth && line) {
        ctx.fillText(line, x, curY);
        line = words[i];
        curY += lineHeight;
      } else {
        line = test;
      }
    }
    if (line) ctx.fillText(line, x, curY);
  }

  redrawPanelCanvas() {
    const ctx = this.panelCanvas.getContext("2d");
    const dpr = this.canvasDPR;
    const W = this.PANEL_W;
    const H = this.PANEL_H;
    const rects = this.getLayoutRects();

    ctx.save();
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    ctx.clearRect(0, 0, W, H);

    const { r0, r1, r2, r3, r4 } = rects;

    ctx.textRendering = "optimizeLegibility";
    this._drawPaletteIntroduction(ctx, r0);

    // Region 1: Interactive Command Bar, Live Result & Exact Action Buttons
    ctx.fillStyle = "rgba(5, 5, 7, 0.76)";
    ctx.strokeStyle = "rgba(245, 239, 226, 0.33)";
    ctx.lineWidth = 1;
    ctx.beginPath();
    ctx.roundRect(r1.x, r1.y, r1.w, r1.input.h, 6);
    ctx.fill();
    ctx.stroke();

    ctx.fillStyle = this.hudData.isDestructive ? "#d15b53" : "#dcd9cf";
    ctx.font = '600 14px "Heming", "IBM Plex Mono", monospace';
    ctx.fillText("◇", r1.x + 14, r1.y + (this.isPortrait ? 17 : 13));

    ctx.fillStyle = "#f5efe2";
    ctx.font = `500 ${this.isPortrait ? 21 : 15}px ${HUD_MONO}`;
    const maxQueryWidth = r1.input.w - 44;
    const query = this.hudData.query || "Type command…";
    let queryClip = query;
    while (queryClip.length > 1 && ctx.measureText(queryClip).width > maxQueryWidth) {
      queryClip = "…" + queryClip.slice(queryClip.startsWith("…") ? 2 : 1);
    }
    if (!this.hudData.inputFocused) ctx.fillText(queryClip, r1.x + 36, r1.y + (this.isPortrait ? 14 : 12));

    // Run button matching r1.runBtn
    ctx.fillStyle = "rgba(245, 239, 226, 0.14)";
    ctx.beginPath();
    ctx.roundRect(r1.runBtn.x, r1.runBtn.y, r1.runBtn.w, r1.runBtn.h, 4);
    ctx.fill();
    ctx.fillStyle = "#f5efe2";
    ctx.font = `600 ${this.isPortrait ? 18 : 11}px "Heming", "IBM Plex Mono", monospace`;
    ctx.textAlign = "center";
    ctx.textBaseline = "middle";
    ctx.fillText("Run", r1.runBtn.x + r1.runBtn.w * 0.5, r1.runBtn.y + r1.runBtn.h * 0.5);

    // Status Badge
    ctx.textAlign = "left";
    ctx.textBaseline = "top";
    const toneColor =
      this.hudData.badgeTone === "crimson"
        ? "#d15b53"
        : this.hudData.badgeTone === "amber"
        ? "#c7ac82"
        : "#dcd9cf";
    const toneFill =
      this.hudData.badgeTone === "crimson"
        ? "rgba(209, 91, 83, 0.15)"
        : this.hudData.badgeTone === "amber"
        ? "rgba(199, 172, 130, 0.13)"
        : "rgba(220, 217, 207, 0.10)";

    const badgeSize = this.isPortrait ? 16 : 10.5;
    const badgeHeight = this.isPortrait ? 28 : 24;
    ctx.font = `600 ${badgeSize}px ${HUD_MONO}`;
    const metric = LATENCY_LABEL.exec(this.hudData.badge);
    const metricStart = metric ? ctx.measureText(metric[1]).width : 0;
    let numberWidth = 0;
    let digitWidth = 0;
    if (metric) {
      for (let digit = 0; digit < 10; digit += 1) digitWidth = Math.max(digitWidth, ctx.measureText(digit).width);
      numberWidth = (metric[2].length - 1) * digitWidth + ctx.measureText(".").width;
    }
    const labelWidth = metric ? metricStart + numberWidth + ctx.measureText(metric[3]).width :
      ctx.measureText(this.hudData.badge).width;
    const badgeW = Math.min(r1.w, labelWidth + 20);
    ctx.fillStyle = toneFill;
    ctx.strokeStyle = toneColor;
    ctx.beginPath();
    ctx.roundRect(r1.output.x, r1.output.y, badgeW, badgeHeight, 3);
    ctx.fill();
    ctx.stroke();

    ctx.fillStyle = toneColor;
    const badgeX = r1.output.x + 10;
    const badgeY = r1.output.y + 6;
    if (metric) {
      ctx.fillText(metric[1], badgeX, badgeY);
      const digitsX = badgeX + metricStart;
      if (this.latencyReel) {
        const style = this.latencyReel.style;
        style.left = `${digitsX}px`;
        style.top = `${badgeY}px`;
        style.height = `${badgeSize * 1.2}px`;
        style.width = `${numberWidth + 2}px`;
        style.font = `600 ${badgeSize}px/${badgeSize * 1.2}px ${HUD_MONO}`;
        style.color = toneColor;
      } else {
        let x = digitsX;
        for (const char of metric[2]) {
          const charWidth = ctx.measureText(char).width;
          const advance = char === "." ? charWidth : digitWidth;
          ctx.fillText(char, x + (advance - charWidth) * 0.5, badgeY);
          x += advance;
        }
      }
      ctx.fillText(metric[3], digitsX + numberWidth, badgeY);
    } else {
      ctx.fillText(this.hudData.badge, badgeX, badgeY);
    }

    ctx.fillStyle = "#f5efe2";
    ctx.font = `600 ${this.isPortrait ? 22 : 17}px "Heming", "Manrope", sans-serif`;
    this._drawWrappedText(ctx, this.hudData.title, r1.output.x, r1.output.y + 34, r1.w, 22);

    ctx.fillStyle = "rgba(245, 239, 226, 0.76)";
    ctx.font = `400 ${this.isPortrait ? 16 : 11.5}px "Heming", "IBM Plex Mono", monospace`;
    if (this.hudData.plan) {
      this.hudData.plan.forEach((step, index) => {
        const x = r1.output.x;
        const y = r1.output.y + 64 + index * 23;
        const label = `${index + 1}. ${step.label}`;
        ctx.fillStyle = "#dcd9cf";
        ctx.fillText(label, x, y);
        const risk = `[${step.risk}]`;
        const left = x + ctx.measureText(label).width + 14;
        const riskWidth = ctx.measureText(risk).width + 12;
        const ink = step.risk === "MUTATING" ? "#c7ac82" : "#dcd9cf";
        ctx.strokeStyle = ink;
        ctx.beginPath();
        ctx.roundRect(left, y - 3, riskWidth, 18, 3);
        ctx.stroke();
        ctx.fillStyle = ink;
        ctx.fillText(risk, left + 6, y);
      });
    } else {
      this._drawWrappedText(ctx, this.hudData.detail, r1.output.x, r1.output.y + 82, r1.w, 18);
    }

    // Contextual Action Buttons on Shard 1 (Primary + Secondary)
    const drawShard1Btn = (box, label, variant) => {
      if (!label) return;
      if (variant === "crimson") {
        ctx.fillStyle = "#a83831";
        ctx.strokeStyle = "#d15b53";
      } else if (variant === "primary") {
        ctx.fillStyle = "#f5efe2";
        ctx.strokeStyle = "#f5efe2";
      } else {
        ctx.fillStyle = "rgba(5, 5, 7, 0.72)";
        ctx.strokeStyle = "rgba(245, 239, 226, 0.45)";
      }
      ctx.beginPath();
      ctx.roundRect(box.x, box.y, box.w, box.h, 4);
      ctx.fill();
      ctx.stroke();

      ctx.fillStyle = variant === "primary" ? "#050507" : "#f5efe2";
      ctx.font = `600 ${this.isPortrait ? 17 : 11.5}px "Heming", "IBM Plex Mono", monospace`;
      ctx.textAlign = "center";
      ctx.textBaseline = "middle";
      ctx.fillText(label, box.x + box.w * 0.5, box.y + box.h * 0.5);
    };

    if (this.hudData.primaryBtn) {
      drawShard1Btn(
        r1.primaryBtn,
        this.hudData.primaryBtn,
        this.hudData.isDestructive ? "secondary" : "primary"
      );
    }
    if (this.hudData.secondaryBtn) {
      drawShard1Btn(
        r1.secondaryBtn,
        this.hudData.secondaryBtn,
        this.hudData.isDestructive ? "crimson" : "secondary"
      );
    }

    // Presets and actions share the canvas bounds used by the live DOM buttons.
    ctx.textAlign = "left";
    ctx.textBaseline = "top";
    printLabel(ctx, "TRY A COMMAND", r2.x, r2.y, this.isPortrait ? 15 : 11);
    etchRule(ctx, r2.x, r2.getMavrick.y - (this.isPortrait ? 7 : 17), r2.w);
    ctx.textBaseline = "middle";
    const presets = [
      "open files, vlc and vscode",
      "245 * 18.5",
      "organize downloads",
      "purge apt cache",
    ];
    this.measuredPresetRects = [];
    let px = r2.presetsArea.x;
    let py = r2.presetsArea.y;
    const presetHeight = this.isPortrait ? 36 : 32;
    ctx.font = `500 ${this.isPortrait ? 18 : 12}px ${HUD_MONO}`;

    presets.forEach((p) => {
      const pw = Math.ceil(ctx.measureText(p).width + 22);
      if (px + pw > r2.presetsArea.x + r2.presetsArea.w) {
        px = r2.presetsArea.x;
        py += presetHeight + 8;
      }
      this.measuredPresetRects.push({ x: px, y: py, w: pw, h: presetHeight, preset: p });

      const active = this.hudData.activePreset === p;
      ctx.fillStyle = active ? "rgba(245, 239, 226, 0.16)" : "rgba(5, 5, 7, 0.58)";
      ctx.strokeStyle = active ? "rgba(245, 239, 226, 0.78)" : "rgba(245, 239, 226, 0.22)";
      ctx.beginPath();
      ctx.roundRect(px, py, pw, presetHeight, 4);
      ctx.fill();
      ctx.stroke();

      ctx.fillStyle = active ? "#f5efe2" : "rgba(245, 239, 226, 0.78)";
      ctx.fillText(p, px + 11, py + presetHeight * 0.5);
      px += pw + 8;
    });

    // Get Mavrick Pill matching r2.getMavrick
    ctx.fillStyle = "#f5efe2";
    ctx.beginPath();
    ctx.roundRect(r2.getMavrick.x, r2.getMavrick.y, r2.getMavrick.w, r2.getMavrick.h, 21);
    ctx.fill();
    ctx.fillStyle = "#050507";
    ctx.font = `600 ${this.isPortrait ? 20 : 13.5}px "Heming", "Manrope", sans-serif`;
    ctx.textAlign = "center";
    ctx.fillText(
      "Get Mavrick",
      r2.getMavrick.x + r2.getMavrick.w * 0.5,
      r2.getMavrick.y + r2.getMavrick.h * 0.5
    );

    // Read architecture → matching r2.readArch
    ctx.fillStyle = "#f5efe2";
    ctx.font = `500 ${this.isPortrait ? 20 : 13.5}px "Heming", "Manrope", sans-serif`;
    ctx.textAlign = "center";
    ctx.fillText(
      "Read architecture →",
      r2.readArch.x + r2.readArch.w * 0.5,
      r2.readArch.y + r2.readArch.h * 0.5
    );

    this._drawPlatformAndRuntime(ctx, r3, r4);

    ctx.restore();
    if (this.panelTex) this.panelTex.needsUpdate = true;
  }

  _drawPaletteIntroduction(ctx, { x, y, w }) {
    const compact = this.isPortrait;
    ctx.textAlign = "left";
    ctx.textBaseline = "top";
    printLabel(ctx, "LOCAL · POLICY-GATED", x, y, compact ? 15 : 11);

    ctx.fillStyle = "#f5efe2";
    ctx.textBaseline = "alphabetic";
    ctx.font = `700 ${compact ? 58 : 59}px ${HUD_DISPLAY}`;
    ctx.letterSpacing = "-0.03em";
    ctx.fillText("Desktop", x, y + 75, w);
    ctx.letterSpacing = "0px";
    ctx.font = `italic 400 ${compact ? 64 : 66}px ${HUD_SERIF}`;
    ctx.fillText("command palette.", x, y + 132, w);

    ctx.textBaseline = "top";
    ctx.fillStyle = "rgba(245, 239, 226, 0.80)";
    ctx.font = `400 ${compact ? 21 : 15}px ${HUD_UI}`;
    this._drawWrappedText(ctx,
      "Launch apps, calculate, and approve multi-step workflows.",
      x, y + (compact ? 145 : 151), w - 14, compact ? 25 : 21
    );

    const lineY = y + (compact ? 198 : 188);
    etchRule(ctx, x, lineY, w);
    const baseline = y + (compact ? 238 : 244);
    ctx.textBaseline = "alphabetic";
    ctx.fillStyle = "#f5efe2";
    ctx.font = `500 ${compact ? 37 : 42}px ${HUD_MONO}`;
    ctx.fillText("0.039", x, baseline);
    const unitX = x + ctx.measureText("0.039").width + 10;
    ctx.font = `500 ${compact ? 17 : 15}px ${HUD_MONO}`;
    ctx.fillText("ms", unitX, baseline);
    printLabel(ctx, "MULTI-APP LOOKUP", unitX + (compact ? 62 : 57), baseline, compact ? 14 : 11);
    ctx.textBaseline = "top";
  }

  _drawPlatformAndRuntime(ctx, hosts, runtime) {
    const compact = this.isPortrait;
    const labelSize = compact ? 14 : 11;
    ctx.textAlign = "left";
    ctx.textBaseline = "top";
    printLabel(ctx, "PLATFORMS", hosts.x, hosts.y + 5, labelSize);
    printLabel(ctx, "CORE RUNTIME", runtime.x, runtime.y + 5, labelSize);
    const ruleOffset = compact ? 25 : 27;
    etchRule(ctx, hosts.x, hosts.y + ruleOffset, hosts.w);
    etchRule(ctx, runtime.x, runtime.y + ruleOffset, runtime.w);

    ctx.fillStyle = "#f5efe2";
    if (compact) {
      ctx.font = `600 21px ${HUD_UI}`;
      ctx.fillText("Linux · Windows · macOS", hosts.x, hosts.y + 38, hosts.w);
      ctx.fillStyle = "rgba(245, 239, 226, 0.72)";
      ctx.font = `400 16px ${HUD_UI}`;
      ctx.fillText("Local CLI & daemon", hosts.x, hosts.y + 65);
    } else {
      for (let i = 0; i < HOSTS.length; i += 1) {
        const row = HOSTS[i];
        const rowY = hosts.y + 43 + i * 43;
        ctx.textAlign = "left";
        ctx.fillStyle = "#f5efe2";
        ctx.font = `600 18px ${HUD_UI}`;
        ctx.fillText(row.name, hosts.x, rowY);
        ctx.textAlign = "right";
        ctx.fillStyle = "rgba(245, 239, 226, 0.72)";
        ctx.font = `400 11px ${HUD_MONO}`;
        ctx.fillText(row.transport, hosts.x + hosts.w, rowY + 5);
        etchRule(ctx, hosts.x, rowY + 31, hosts.w);
      }
    }

    ctx.textAlign = "left";
    ctx.textBaseline = "alphabetic";
    const mid = runtime.x + runtime.w * 0.48;
    const second = mid + (compact ? 16 : 17);
    etchRule(ctx, mid, runtime.y + (compact ? 35 : 44), 1, compact ? 51 : 96);
    ctx.fillStyle = "#f5efe2";
    ctx.font = `500 ${compact ? 32 : 52}px ${HUD_MONO}`;
    ctx.fillText("25", runtime.x, runtime.y + (compact ? 67 : 104));
    ctx.fillText("0", second, runtime.y + (compact ? 67 : 104));
    ctx.fillStyle = "rgba(245, 239, 226, 0.76)";
    ctx.font = `500 ${compact ? 15 : 13}px ${HUD_UI}`;
    ctx.fillText("Typed tools", runtime.x, runtime.y + (compact ? 88 : 130));
    ctx.fillText("pip dependencies", second, runtime.y + (compact ? 88 : 130), runtime.x + runtime.w - second);
  }

  syncDomButtonRects() {
    const rects = this.getLayoutRects();
    const setRect = (el, box) => {
      if (!el || !box) return;
      el.style.left = `${box.x}px`;
      el.style.top = `${box.y}px`;
      el.style.width = `${box.w}px`;
      el.style.height = `${box.h}px`;
    };

    // Nav Slab buttons
    setRect(document.getElementById("nav-hit-brand"), rects.nav.brand);
    setRect(document.getElementById("nav-hit-valley"), rects.nav.valley);
    setRect(document.getElementById("nav-hit-monolith"), rects.nav.monolith);
    setRect(document.getElementById("nav-hit-arch"), rects.nav.arch);
    setRect(document.getElementById("nav-hit-github"), rects.nav.github);

    // Shard 0, 3, 4 selectable regions
    setRect(document.getElementById("shard-copy-0"), rects.r0);
    setRect(document.getElementById("shard-copy-3"), rects.r3);
    setRect(document.getElementById("shard-copy-4"), rects.r4);

    // Shard 1 interactive controls
    const input = document.getElementById("glass-cmd-input");
    setRect(input, rects.r1.input);
    if (input) input.style.fontSize = this.isPortrait ? "21px" : "";
    setRect(document.getElementById("glass-cmd-run"), rects.r1.runBtn);
    setRect(document.getElementById("glass-cmd-output"), rects.r1.output);
    setRect(document.getElementById("glass-act-primary"), rects.r1.primaryBtn);
    setRect(document.getElementById("glass-act-secondary"), rects.r1.secondaryBtn);

    // Shard 2 preset pills + CTAs
    const presetEls = Array.from(document.querySelectorAll("#shard-dom-2 .glass-preset-btn"));
    this.measuredPresetRects.forEach((box, idx) => {
      if (presetEls[idx]) setRect(presetEls[idx], box);
    });
    setRect(document.getElementById("glass-get-mavrick"), rects.r2.getMavrick);
    setRect(document.getElementById("glass-read-arch"), rects.r2.readArch);
  }

  /* ------------------------------------------------------------------------
     State Transitions: Act I <-> Act II <-> Act III
     ------------------------------------------------------------------------ */
  setAct(nextAct) {
    const prevAct = this.act;
    this.act = nextAct;
    this.controlsReady = false;
    if (this._finishLatencyReel()) this.redrawPanelCanvas();
    this.redrawNavCanvas();

    if (nextAct === 1) {
      this.shatterState = "assembled";
      this.backSliverGroup.visible = false;
    } else if (nextAct === 2) {
      this.backSliverGroup.visible = false;
      if (prevAct === 3) {
        this.entryTime = ARRIVAL_END;
        for (const slab of this.slabs) {
          slab.motionOrigin.copy(slab.pivot.position);
          slab.motionRotation.copy(slab.pivot.rotation);
          slab.motionOpacity = this.shatterState === "shattered" ? 0 : slab.faceMat.opacity;
          slab.pivot.visible = true;
          slab.arrivalMix = 1;
        }
        this.shatterState = "reassembling";
        this.shatterTimer = 0;
      } else {
        this.shatterState = "assembled";
      }
    } else if (nextAct === 3) {
      this.backSliverGroup.visible = prevAct !== 2;
      if (prevAct === 2) {
        this.shatterState = "shattering";
        this.shatterTimer = 0;
        this._assignShatterVelocities();
      } else {
        this._resetShardTransforms();
        this._assignShatterVelocities();
        for (const slab of this.slabs) this._placeShatteredSlab(slab, 1);
        this.shatterState = "shattered";
        this.shatterTimer = this.shatterDuration;
      }
    }
  }

  _resetShardTransforms() {
    this.slabs.forEach((s) => {
      if (!s) return;
      s.liftZ = 0;
      s.tipX = 0;
      s.tipY = 0;
      s.pivot.position.copy(s.basePos);
      s.pivot.rotation.set(0, 0, 0);
      s.glassMat.opacity = 0.94;
      s.faceMat.opacity = 1.0;
    });
  }

  _assignShatterVelocities() {
    const S = this.S;
    this.slabs.forEach((s, idx) => {
      s.motionOrigin.copy(s.pivot.position);
      s.motionRotation.copy(s.pivot.rotation);
      s.motionOpacity = s.faceMat.opacity;
      const dirX = (s.basePos.x || (idx % 2 ? 0.6 : -0.6)) * 1.4 + (Math.random() - 0.5) * 1.2;
      const dirY = s.basePos.y * 0.9 - 1.1;
      const zSpeed = (420 + Math.random() * 380) * S;
      s.shatterVel.set(dirX * 1.9, dirY * 1.7, zSpeed);
      s.shatterRot.set(
        (Math.random() < 0.5 ? -1 : 1) * (2.0 + Math.random() * 1.6),
        (Math.random() < 0.5 ? -1 : 1) * (2.0 + Math.random() * 1.6),
        (Math.random() - 0.5) * 2.2
      );
    });
  }

  _placeShatteredSlab(slab, progress) {
    const ease = progress * progress * progress;
    const gravity = -920 * this.S * 0.04;
    slab.pivot.position.set(
      slab.motionOrigin.x + slab.shatterVel.x * ease * 1.4,
      slab.motionOrigin.y + slab.shatterVel.y * ease * 1.2 + 7 * gravity * progress * progress,
      slab.motionOrigin.z + slab.shatterVel.z * ease * 1.3
    );
    slab.pivot.rotation.set(
      slab.motionRotation.x + slab.shatterRot.x * ease,
      slab.motionRotation.y + slab.shatterRot.y * ease,
      slab.motionRotation.z + slab.shatterRot.z * ease
    );
    const faded = slab.motionOpacity * (1 - smoothstep(progress, 0.48, 1));
    slab.glassMat.opacity = 0.94 * faded;
    slab.faceMat.opacity = faded;
  }

  setPointerNDC(x, y) {
    this.pointerNDC.set(x, y);
  }

  /* ------------------------------------------------------------------------
     Frame Update & Per-Shard matrix3d Homography Projection
     ------------------------------------------------------------------------ */
  update(dt, elapsed, width, height, domPlanes) {
    const direction = this.act === 2 ? 1 : this.act === 1 ? -1.33 : 0;
    this.entryTime = clamp(this.entryTime + dt * direction, 0, ARRIVAL_END);
    this.entryMix = this.entryTime / ARRIVAL_END;
    this.slabGroup.visible = this.entryTime > 0 && this.shatterState !== "shattered";
    const sweep = this.sweepUniform.value;
    const sweepWidth = (this.PANEL_W * 0.5 + this.PANEL_H * 0.21) * this.S;
    sweep.x = lerp(-sweepWidth - 1, sweepWidth + 1, this.entryMix);
    sweep.y = this.act === 2 && this.shatterState === "assembled" ? Math.sin(this.entryMix * Math.PI) : 0;
    if (!this.slabGroup.visible && !this.backSliverGroup.visible) {
      this.hoveredIndex = -1;
      this._syncPerShardHomography(width, height, domPlanes);
      return;
    }

    // Cursor PointLight tracking across glass bevels
    const lightDamp = 1 - Math.exp(-dt * 6.0);
    const pointerInside = Math.abs(this.pointerNDC.x) <= 1 && Math.abs(this.pointerNDC.y) <= 1;
    const lx = pointerInside ? this.pointerNDC.x * this.PANEL_W * 0.45 * this.S : 0;
    const ly = pointerInside ? this.pointerNDC.y * this.PANEL_H * 0.45 * this.S : 0;
    this.cursorPointLight.position.x += (lx - this.cursorPointLight.position.x) * lightDamp;
    this.cursorPointLight.position.y += (ly - this.cursorPointLight.position.y) * lightDamp;
    this.cursorPointLight.intensity = this.act === 2 ? 0.46 : 0.24;

    const tiltDamp = 1 - Math.exp(-dt / 0.3);
    const targetRx = this.act === 2 && pointerInside ? -this.pointerNDC.y * 0.035 : 0;
    const targetRy = this.act === 2 && pointerInside ? this.pointerNDC.x * 0.055 : 0;
    this.slabTilt.rx += (targetRx - this.slabTilt.rx) * tiltDamp;
    this.slabTilt.ry += (targetRy - this.slabTilt.ry) * tiltDamp;
    this.slabGroup.rotation.set(this.slabTilt.rx, this.slabTilt.ry, 0);

    this.camera.updateMatrixWorld(true);

    this._pickSlab();
    const hoverDamp = 1 - Math.exp(-dt * 11.0);
    const allSlabs = this.slabs;

    if (this.shatterState === "assembled") {
      for (const s of allSlabs) {
        const isHovered = s.index === this.hoveredIndex;
        const maxLift = s.isNav ? 32 * this.S : 50 * this.S;
        s.targetLiftZ = isHovered ? maxLift : 0;

        if (isHovered) {
          const localHit = this.hoverHit;
          const nx = clamp(localHit.x / (220 * this.S), -1, 1);
          const ny = clamp(localHit.y / (140 * this.S), -1, 1);
          s.targetTipY = -nx * 0.12;
          s.targetTipX = ny * 0.10;
        } else {
          s.targetTipX = 0;
          s.targetTipY = 0;
        }

        s.liftZ += (s.targetLiftZ - s.liftZ) * hoverDamp;
        s.tipX += (s.targetTipX - s.tipX) * hoverDamp;
        s.tipY += (s.targetTipY - s.tipY) * hoverDamp;

        const arrival = clamp((this.entryTime - s.arrivalDelay) / ARRIVAL_DURATION, 0, 1);
        const eased = 1 - (1 - arrival) ** 4;
        const retreat = 1 - eased;
        s.arrivalMix = eased;
        s.pivot.visible = arrival > 0;
        s.pivot.position.set(
          s.basePos.x * (1 + retreat * 0.36),
          s.basePos.y - retreat * (s.isNav ? 0.9 : 1.48),
          s.liftZ - retreat * (s.isNav ? 11.0 : 15.0)
        );
        s.pivot.rotation.set(s.tipX + retreat * 0.13, s.tipY - retreat * 0.08, 0);
        s.faceMat.opacity = smoothstep(eased, 0.02, 0.76);
        s.glassMat.opacity = 0.94 * s.faceMat.opacity;

        if (!s.isNav && s.pivot.visible) {
          const phase = elapsed * (0.53 + s.index * 0.061) + s.index * 1.07;
          const breathe = Math.sin(phase);
          const lift = clamp(s.liftZ / maxLift, 0, 1);
          this._deformSlab(
            s,
            1 + eased * (breathe * 0.011 + lift * 0.007),
            1 + eased * (Math.sin(phase + 0.9) * 0.010 + lift * 0.005),
            eased * (Math.cos(phase) * 0.009 + lift * 0.007)
          );
        }

        const targetIrid = isHovered ? 0.85 : 0.42;
        const targetEnv = isHovered ? 1.28 : 0.95;
        const targetCursor = isHovered ? 1.0 : 0.20;
        s.glassMat.iridescence += (targetIrid - s.glassMat.iridescence) * hoverDamp;
        s.glassMat.envMapIntensity += (targetEnv - s.glassMat.envMapIntensity) * hoverDamp;
        s.glassMat.userData.uCursorIntensity.value +=
          (targetCursor - s.glassMat.userData.uCursorIntensity.value) * hoverDamp;
      }
    } else if (this.shatterState === "shattering") {
      this.shatterTimer += dt;
      let done = true;
      for (const s of allSlabs) {
        const localT = Math.max(0, (this.shatterTimer - s.shatterDelay) / (this.shatterDuration - s.shatterDelay));
        const clamped = Math.min(localT, 1);
        if (clamped < 1) done = false;
        this._placeShatteredSlab(s, clamped);
      }
      if (done) {
        this.shatterState = "shattered";
        this.backSliverGroup.visible = true;
      }
    } else if (this.shatterState === "reassembling") {
      this.shatterTimer += dt;
      const p = Math.min(this.shatterTimer / this.reassembleDuration, 1);
      const easeOut = 1 - Math.pow(1 - p, 3);
      const inv = 1 - easeOut;
      for (const s of allSlabs) {
        s.pivot.position.lerpVectors(s.motionOrigin, s.basePos, easeOut);
        s.pivot.rotation.set(s.motionRotation.x * inv, s.motionRotation.y * inv, s.motionRotation.z * inv);
        s.faceMat.opacity = lerp(s.motionOpacity, 1, easeOut);
        s.glassMat.opacity = 0.94 * s.faceMat.opacity;
      }
      if (p >= 1) {
        this.shatterState = "assembled";
        this._resetShardTransforms();
      }
    }

    if (this.backSliverGroup.visible && this.backSliver) {
      const hovered = this.hoveredIndex === 6;
      const targetTipX = hovered ? this.hoverHit.y / (23 * this.S) * 0.10 : 0.02 * Math.sin(elapsed * 0.9);
      const targetTipY = hovered ? -this.hoverHit.x / (66 * this.S) * 0.14 : 0.03 * Math.cos(elapsed * 0.7);
      this.backSliver.hoverTipX += (targetTipX - this.backSliver.hoverTipX) * hoverDamp;
      this.backSliver.hoverTipY += (targetTipY - this.backSliver.hoverTipY) * hoverDamp;
      this.backSliverGroup.rotation.set(this.backSliver.hoverTipX, this.backSliver.hoverTipY, -0.02);
      this.backSliver.glassMat.iridescence += ((hovered ? 0.85 : 0.42) - this.backSliver.glassMat.iridescence) * hoverDamp;
    }

    if (this.slabGroup.visible) this.slabGroup.updateMatrixWorld(true);
    if (this.backSliverGroup.visible) this.backSliverGroup.updateMatrixWorld(true);
    this._syncPerShardHomography(width, height, domPlanes);
  }

  _deformSlab(slab, sx, sy, shear) {
    const { bodyDeformation, faceDeformation, mapBounds } = slab.deformMesh;
    slab.glassMat.userData.uDeformation.value.set(sx, sy, shear);
    deformGeometry(bodyDeformation, mapBounds, sx, sy, shear);
    deformGeometry(faceDeformation, mapBounds, sx, sy, shear);
  }

  _pickSlab() {
    this.hoveredIndex = -1;
    if (Math.abs(this.pointerNDC.x) > 1 || Math.abs(this.pointerNDC.y) > 1) return;
    if (this.act !== 3 && (this.act !== 2 || this.shatterState !== "assembled")) return;

    this.raycaster.setFromCamera(this.pointerNDC, this.camera);
    let nearest = Infinity;
    for (const slab of this.pickableSlabs) {
      const isBack = slab === this.backSliver;
      if (isBack !== (this.act === 3)) continue;
      if (!slab.pivot.visible || (!isBack && slab.arrivalMix < 0.82)) continue;

      this.pickMatrix.copy(slab.pivot.matrixWorld).invert();
      this.localRay.copy(this.raycaster.ray).applyMatrix4(this.pickMatrix);
      this.facePlane.constant = -slab.faceZ;
      if (!this.localRay.intersectPlane(this.facePlane, this.localHit)) continue;

      const polygon = slab.pickPolygon;
      const deform = slab.glassMat.userData.uDeformation.value;
      const y = this.localHit.y / deform.y;
      const x = (this.localHit.x - y * deform.z) / deform.x;
      const px = x / this.geometryRatio;
      const py = y / this.geometryRatio;
      let inside = false;
      for (let i = 0, j = polygon.length - 1; i < polygon.length; j = i++) {
        const a = polygon[i];
        const b = polygon[j];
        if ((a.y > py) !== (b.y > py) &&
            px < (b.x - a.x) * (py - a.y) / (b.y - a.y) + a.x) inside = !inside;
      }
      if (!inside) continue;

      const distance = this.localRay.origin.distanceToSquared(this.localHit);
      if (distance >= nearest) continue;
      nearest = distance;
      this.hoverHit.copy(this.localHit);
      this.hoveredIndex = isBack ? 6 : slab.index;
      this.cursorWorldUniform.value.copy(this.localHit).applyMatrix4(slab.pivot.matrixWorld);
    }
  }

  _projectPlane(el, slab, logicalW, logicalH, width, height) {
    const halfW = logicalW * this.S * 0.5;
    const halfH = logicalH * this.S * 0.5;
    const cx = slab.cx || 0;
    const cy = slab.cy || 0;
    const deform = slab.glassMat.userData.uDeformation.value;
    for (let i = 0; i < 4; i += 1) {
      const x = i === 0 || i === 3 ? -halfW : halfW;
      const y = i < 2 ? halfH : -halfH;
      const lx = x - cx;
      const ly = y - cy;
      const v = this.corner.set(lx * deform.x + ly * deform.z, ly * deform.y, slab.faceZ)
        .applyMatrix4(slab.pivot.matrixWorld).project(this.camera);
      if (v.z > 1) return;
      this.projectedCorners[i].set((v.x * 0.5 + 0.5) * width, (0.5 - v.y * 0.5) * height);
    }
    const transform = computeMatrix3d(logicalW, logicalH, this.projectedCorners);
    if (transform) el.style.transform = transform;
  }

  _syncPerShardHomography(width, height, domPlanes) {
    const { navDomEl, shardEls, backBtnEl } = domPlanes;
    const showControls = this.act === 2 && this.shatterState === "assembled";
    this.controlsReady = showControls && this.shards[1].arrivalMix >= 0.72;
    if (navDomEl) this._syncPlane(navDomEl, this.navShard, this.NAV_W, this.NAV_H, width, height, showControls);
    for (let i = 0; i < shardEls.length; i += 1) {
      const el = shardEls[i];
      if (el) this._syncPlane(el, this.shards[i], this.PANEL_W, this.PANEL_H, width, height, showControls);
    }

    if (this.act === 3 && backBtnEl && this.backSliverGroup.visible) {
      this._projectPlane(backBtnEl, this.backSliver, 132, 46, width, height);
    }
  }

  _syncPlane(el, slab, logicalW, logicalH, width, height, showControls) {
    const visible = showControls && slab.arrivalMix >= 0.72;
    if (visible !== slab.visibleDOM) {
      slab.visibleDOM = visible;
      el.style.visibility = visible ? "visible" : "hidden";
    }
    if (visible) this._projectPlane(el, slab, logicalW, logicalH, width, height);
  }
}
