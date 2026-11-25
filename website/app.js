/* ==========================================================================
   MAVRICK — "Grown, not built." Single-Canvas WebGL Stage Orchestrator
   Three.js 0.169.0 · RoomEnvironment PMREM · ACESFilmic
   ========================================================================== */

import { NightAudioEngine } from "./audio.js";

/* --------------------------------------------------------------------------
   Safe Recursive-Descent AST Evaluator (mirrors mavrick/router.py)
   -------------------------------------------------------------------------- */
function evaluateSafeMath(expr) {
  const text = expr.trim();
  if (!text || text.length > 192 || !/^[0-9.+*/%() \t-]+$/.test(text) || !/[0-9]/.test(text)) {
    return null;
  }
  const tokens = [];
  let i = 0;
  while (i < text.length) {
    const ch = text[i];
    if (ch === " " || ch === "\t") {
      i += 1;
      continue;
    }
    if ((ch >= "0" && ch <= "9") || ch === ".") {
      let num = "";
      let dots = 0;
      while (i < text.length && ((text[i] >= "0" && text[i] <= "9") || text[i] === ".")) {
        if (text[i] === ".") dots += 1;
        if (dots > 1) return { error: "Invalid numeric literal." };
        num += text[i];
        i += 1;
      }
      const val = Number(num);
      if (!Number.isFinite(val)) return { error: "Number out of range." };
      tokens.push({ type: "NUM", value: val });
      continue;
    }
    if (ch === "*" && text[i + 1] === "*") {
      tokens.push({ type: "OP", value: "**" });
      i += 2;
      continue;
    }
    if (ch === "/" && text[i + 1] === "/") {
      tokens.push({ type: "OP", value: "//" });
      i += 2;
      continue;
    }
    if ("+-*/%()".includes(ch)) {
      tokens.push({ type: ch === "(" || ch === ")" ? ch : "OP", value: ch });
      i += 1;
      continue;
    }
    return { error: "Unsupported operator." };
  }

  let pos = 0;
  let nodes = 0;
  const peek = () => tokens[pos] || null;
  const consume = () => tokens[pos++];
  const guard = (depth) => {
    nodes += 1;
    if (nodes > 64 || depth > 20) throw new Error("AST limit exceeded.");
  };

  function parseExpr(d) {
    guard(d);
    let left = parseTerm(d + 1);
    while (peek() && peek().type === "OP" && (peek().value === "+" || peek().value === "-")) {
      const op = consume().value;
      const right = parseTerm(d + 1);
      left = op === "+" ? left + right : left - right;
    }
    return left;
  }

  function parseTerm(d) {
    guard(d);
    let left = parseUnary(d + 1);
    while (peek() && peek().type === "OP" && ["*", "/", "//", "%"].includes(peek().value)) {
      const op = consume().value;
      const right = parseUnary(d + 1);
      if ((op === "/" || op === "//" || op === "%") && right === 0) {
        throw new Error("Cannot divide by zero.");
      }
      if (op === "*") left *= right;
      else if (op === "/") left /= right;
      else if (op === "//") left = Math.floor(left / right);
      else left %= right;
    }
    return left;
  }

  function parseUnary(d) {
    guard(d);
    if (peek() && peek().type === "OP" && (peek().value === "+" || peek().value === "-")) {
      const op = consume().value;
      const val = parseUnary(d + 1);
      return op === "-" ? -val : +val;
    }
    return parsePow(d + 1);
  }

  function parsePow(d) {
    guard(d);
    const base = parsePrimary(d + 1);
    if (peek() && peek().type === "OP" && peek().value === "**") {
      consume();
      const exp = parseUnary(d + 1);
      if (Math.abs(exp) > 128) throw new Error("Exponent limit exceeded.");
      return base ** exp;
    }
    return base;
  }

  function parsePrimary(d) {
    guard(d);
    const t = peek();
    if (!t) throw new Error("Incomplete expression.");
    if (t.type === "NUM") {
      consume();
      return t.value;
    }
    if (t.type === "(") {
      consume();
      const v = parseExpr(d + 1);
      if (!peek() || peek().type !== ")") throw new Error("Mismatched parentheses.");
      consume();
      return v;
    }
    throw new Error("Invalid syntax.");
  }

  try {
    const val = parseExpr(0);
    if (pos < tokens.length || !Number.isFinite(val)) {
      return { error: "Invalid arithmetic expression." };
    }
    const rounded = Math.round(val * 1e10) / 1e10;
    const formatted = new Intl.NumberFormat("en-US", { maximumFractionDigits: 8 }).format(rounded);
    return { raw: String(rounded), formatted };
  } catch (err) {
    return { error: err.message };
  }
}

/* --------------------------------------------------------------------------
   Command State Evaluator
   -------------------------------------------------------------------------- */
const DOWNLOAD_PLAN = [
  { label: "Scan Downloads", risk: "READ-ONLY" },
  { label: "Organize files", risk: "MUTATING" },
];

function evaluateStaticCommand(rawQuery) {
  const q = rawQuery.trim();
  const lower = q.toLowerCase();

  if (lower === "open files, vlc and vscode") {
    return {
      query: q,
      badge: "Instant · 0.039 ms",
      badgeTone: "sage",
      title: "Launched: Files, VLC, VS Code",
      detail: "cosmic-files.desktop · org.videolan.VLC.desktop · code.desktop · 0 LLM calls",
      activePreset: "open files, vlc and vscode",
      primaryBtn: "",
      secondaryBtn: "",
      isDestructive: false,
      mode: "instant",
    };
  }

  if (lower === "organize downloads" || lower === "organize downloads and archive pdfs") {
    return {
      query: q,
      badge: "2-step WorkflowPlan · [MUTATING]",
      badgeTone: "amber",
      title: "Review 2 steps · Organize Downloads",
      detail: DOWNLOAD_PLAN.map((step, index) => `${index + 1}. ${step.label} [${step.risk}]`).join(" · "),
      plan: DOWNLOAD_PLAN,
      activePreset: "organize downloads",
      primaryBtn: "Run 2 steps",
      secondaryBtn: "Cancel",
      isDestructive: false,
      mode: "workflow_review",
    };
  }

  if (lower === "purge apt cache" || lower === "clean cache") {
    return {
      query: q,
      badge: "Policy Gate · [DESTRUCTIVE]",
      badgeTone: "crimson",
      title: "Purge APT & thumbnail caches? (212.8 MB)",
      detail: "/var/cache/apt/archives · ~/.cache/thumbnails · Focus locked on Cancel",
      activePreset: "purge apt cache",
      primaryBtn: "Cancel",
      secondaryBtn: "Clean cache",
      isDestructive: true,
      mode: "destructive_review",
    };
  }

  const math = evaluateSafeMath(q);
  if (math && !math.error) {
    return {
      query: q,
      badge: "Instant AST · 0.008 ms",
      badgeTone: "sage",
      title: `Instant AST · ${math.formatted}`,
      detail: `${q} = ${math.raw} · Recursive-descent AST evaluator · 0 LLM calls`,
      activePreset: lower === "245 * 18.5" ? "245 * 18.5" : "",
      primaryBtn: `Copy ${math.raw}`,
      secondaryBtn: "",
      isDestructive: false,
      mode: "calculator",
      calcRaw: math.raw,
    };
  }
  if (math && math.error) {
    return {
      query: q,
      badge: "AST Guard · Rejected",
      badgeTone: "amber",
      title: math.error,
      detail: "Arithmetic bounds: max 64 AST nodes, depth 20, exponent 128.",
      activePreset: "",
      primaryBtn: "",
      secondaryBtn: "",
      isDestructive: false,
      mode: "error",
    };
  }

  const appMatch = lower.match(/^(open|launch|start|run)\s+(.+)$/);
  if (appMatch) {
    const known = {
      files: "Files",
      vlc: "VLC",
      vscode: "VS Code",
      code: "VS Code",
      firefox: "Firefox",
      brave: "Brave",
      terminal: "Terminal",
      spotify: "Spotify",
    };
    const parts = appMatch[2].split(/\s*(?:,|[&+]|\band\b)\s*/).map((s) => s.trim()).filter(Boolean);
    const hits = parts.map((p) => known[p]).filter(Boolean);
    if (hits.length) {
      return {
        query: q,
        badge: "Instant · 0.039 ms",
        badgeTone: "sage",
        title: `Launched: ${hits.join(", ")}`,
        detail: "Resolved locally from cached .desktop entries · Zero shell=True · 0 LLM calls",
        activePreset: "",
        primaryBtn: "",
        secondaryBtn: "",
        isDestructive: false,
        mode: "instant",
      };
    }
  }

  return {
    query: q,
    badge: "Schema Router · 25 Tools",
    badgeTone: "sage",
    title: `Typed Proposal for "${q || "empty"}"`,
    detail: "Validated by schema.py (ActionType allowlist) → Risk Policy Gate → ActionExecutor",
    activePreset: "",
    primaryBtn: "",
    secondaryBtn: "",
    isDestructive: false,
    mode: "fallback",
  };
}

/* --------------------------------------------------------------------------
   Clipboard & Modal Helpers
   -------------------------------------------------------------------------- */
async function copyToClipboard(text) {
  if (navigator.clipboard && window.isSecureContext) {
    try {
      await navigator.clipboard.writeText(text);
      return true;
    } catch (_) {
      /* Fallback below */
    }
  }
  const ta = document.createElement("textarea");
  ta.value = text;
  ta.setAttribute("readonly", "");
  ta.style.position = "fixed";
  ta.style.opacity = "0";
  document.body.appendChild(ta);
  ta.select();
  let ok = false;
  try {
    ok = document.execCommand("copy");
  } catch (_) {
    ok = false;
  }
  document.body.removeChild(ta);
  return ok;
}

function initModal() {
  const modal = document.getElementById("download-modal");
  const closeBtn = document.getElementById("modal-close-btn");
  const openTriggers = [
    document.getElementById("open-download-btn"),
    document.getElementById("glass-get-mavrick"),
    document.getElementById("act3-download-btn"),
    ...document.querySelectorAll("[data-open-download]"),
  ].filter(Boolean);

  if (!modal) return;

  let lastTrigger = null;
  let inertSections = [];
  const openModal = (trigger) => {
    lastTrigger = trigger || null;
    inertSections = Array.from(document.querySelectorAll("body > section, body > main")).filter((section) => !section.inert);
    for (const section of inertSections) section.inert = true;
    modal.hidden = false;
    if (closeBtn) closeBtn.focus();
  };
  const closeModal = () => {
    modal.hidden = true;
    for (const section of inertSections) section.inert = false;
    if (lastTrigger && typeof lastTrigger.focus === "function") {
      lastTrigger.focus();
    }
  };

  openTriggers.forEach((btn) => btn.addEventListener("click", () => openModal(btn)));
  if (closeBtn) closeBtn.addEventListener("click", closeModal);
  modal.addEventListener("click", (e) => {
    if (e.target === modal) closeModal();
  });
  document.addEventListener("keydown", (e) => {
    if (modal.hidden) return;
    if (e.key === "Escape") closeModal();
    if (e.key !== "Tab") return;
    const controls = modal.querySelectorAll("button, a[href]");
    const first = controls[0];
    const last = controls[controls.length - 1];
    if (e.shiftKey && document.activeElement === first) {
      e.preventDefault();
      last.focus();
    } else if (!e.shiftKey && document.activeElement === last) {
      e.preventDefault();
      first.focus();
    }
  });

  const copyTimers = new WeakMap();
  modal.querySelectorAll(".modal-copy-btn[data-copy]").forEach((btn) => {
    const idleLabel = btn.textContent;
    btn.addEventListener("click", async () => {
      const copied = await copyToClipboard(btn.dataset.copy);
      btn.textContent = copied ? "Copied" : "Retry copy";
      window.clearTimeout(copyTimers.get(btn));
      copyTimers.set(btn, window.setTimeout(() => { btn.textContent = idleLabel; }, 1500));
    });
  });
}

/* --------------------------------------------------------------------------
   Act III Interactive 5-Stage Safety Pipeline Inspector
   -------------------------------------------------------------------------- */
const PIPELINE_STAGES = {
  input: {
    module: "mavrick/router.py · mavrick/policy.py",
    title: "01 · Local Router & Secret Scrubbing",
    desc: "Before hitting any inference provider socket, route_local() checks installed .desktop IDs, multi-app launches, AST math, and saved workflows.json slugs. Outbound prompts and logs pass through redact_secrets().",
    items: [
      "• route_local() resolves multi-app launches in ~0.039 ms (0 LLM calls)",
      "• SECRET_PATTERNS scrubs API keys, bearer tokens, and GitHub tokens",
      "• Host-scoped keys transmit solely over HTTPS to official provider hosts",
    ],
  },
  schema: {
    module: "mavrick/schema.py",
    title: "02 · Strict 25-Tool Schema Allowlist",
    desc: "Model outputs are treated as untrusted data. parse_model_response() validates strict JSON against 25 allowlisted ActionType enums while eliminating shell=True everywhere.",
    items: [
      "• FORBIDDEN_CHARS_PATTERN rejects ; & | ` $ ( ) < > \\ !",
      "• Application and terminal launches reject free-form argv injection",
      "• Multi-step workflows bounded to 1–8 typed steps (MAX_WORKFLOW_STEPS = 8)",
    ],
  },
  policy: {
    module: "mavrick/policy.py · mavrick/ui/window.py",
    title: "03 · Mechanical Risk Tiers & Focus Lock",
    desc: "Every action is classified into READ_ONLY, REVERSIBLE, PRIVILEGED, or DESTRUCTIVE. Privileged and destructive gates cannot be bypassed by trusted_actions, and initial review focus locks onto Cancel.",
    items: [
      "• ui/window.py:_show_review() executes self.cancel_btn.grab_focus()",
      "• Overlapping file paths across workflow steps block during preflight",
      "• PRIVILEGED package operations spawn interactive terminals (no silent sudo)",
    ],
  },
  exec: {
    module: "mavrick/tools/files.py · mavrick/ipc.py",
    title: "04 · Inode-Pinned Execution & 256-Bit HMAC IPC",
    desc: "File moves walk parent directories with O_NOFOLLOW, pin the entry inode, and invoke native no-replace syscalls. Local TCP IPC verifies 256-bit random tokens with constant-time hmac.compare_digest().",
    items: [
      "• Linux renameat2(..., RENAME_NOREPLACE) · macOS renameatx_np(..., RENAME_EXCL)",
      "• 256-bit token (secrets.token_hex(32)) bound strictly to 127.0.0.1",
      "• 2 MiB bounded NDJSON framing with disconnect cancellation",
    ],
  },
  undo: {
    module: "mavrick/history.py · mavrick/tools/files.py",
    title: "05 · Fsynced Batch Undo & Private 0700/0600 State",
    desc: "Pending move receipts are written and fsynced alongside directory descriptors prior to renaming. Pressing Ctrl+Z rolls back workflow batches in strict reverse order without overwriting conflicts.",
    items: [
      "• write_undo_journal() calls os.fsync() on temp file + parent directory",
      "• Mode 0700 directories & 0600 files (history.json, audit.log, config.json)",
      "• Reverse-order restoration preserves conflicted entries",
    ],
  },
};

function initSafetyPipeline() {
  const tabs = Array.from(document.querySelectorAll(".pipe-tab[data-stage]"));
  const card = document.getElementById("pipeline-spec-card");
  if (!tabs.length || !card) return;

  function renderStage(key) {
    const data = PIPELINE_STAGES[key];
    if (!data) return;
    tabs.forEach((t) => {
      const active = t.getAttribute("data-stage") === key;
      t.classList.toggle("is-active", active);
      t.setAttribute("aria-selected", active ? "true" : "false");
    });
    const listHtml = data.items.map((line) => `<li>${line}</li>`).join("");
    card.innerHTML = `
      <div class="pipe-spec-top">
        <div class="pipe-spec-title">${data.title}</div>
        <code class="inline-code">${data.module}</code>
      </div>
      <p class="pipe-spec-desc">${data.desc}</p>
      <ul class="pipe-spec-list">${listHtml}</ul>
    `;
  }

  tabs.forEach((tab) => {
    tab.addEventListener("click", () => renderStage(tab.getAttribute("data-stage")));
  });
  renderStage("input");
}

/* --------------------------------------------------------------------------
   Static Fallback (No-WebGL or Reduced Motion)
   -------------------------------------------------------------------------- */
function initCommandDemo(prefix, root, {
  updateTexture = () => {}, playChime = () => {}, isActive = () => true,
} = {}) {
  const glassForm = document.getElementById(`${prefix}-cmd-form`);
  const glassInput = document.getElementById(`${prefix}-cmd-input`);
  const glassOutBadge = document.getElementById(`${prefix}-out-badge`);
  const glassOutTitle = document.getElementById(`${prefix}-out-title`);
  const glassOutDetail = document.getElementById(`${prefix}-out-detail`);
  const glassActPrimary = document.getElementById(`${prefix}-act-primary`);
  const glassActSecondary = document.getElementById(`${prefix}-act-secondary`);
  const glassPresetBtns = Array.from(root.querySelectorAll("[data-preset]"));

  let hudState = evaluateStaticCommand("open files, vlc and vscode");
  let wfTimers = [];

  function clearWfTimers() {
    while (wfTimers.length) window.clearTimeout(wfTimers.pop());
  }

  function pushGlassHUD(nextState) {
    hudState = { ...hudState, ...nextState };
    if (glassInput && typeof nextState.query === "string") glassInput.value = nextState.query;
    if (glassOutBadge) {
      glassOutBadge.textContent = hudState.badge;
      glassOutBadge.parentElement.dataset.tone = hudState.badgeTone;
    }
    if (glassOutTitle) glassOutTitle.textContent = hudState.title;
    if (glassOutDetail) {
      glassOutDetail.textContent = hudState.detail;
      if (prefix === "static" && hudState.plan) {
        glassOutDetail.replaceChildren();
        for (const [index, step] of hudState.plan.entries()) {
          const row = document.createElement("div");
          const badge = document.createElement("span");
          badge.className = "workflow-risk";
          badge.dataset.risk = step.risk;
          badge.textContent = `[${step.risk}]`;
          row.append(`${index + 1}. ${step.label} `, badge);
          glassOutDetail.append(row);
        }
      }
    }
    glassPresetBtns.forEach((b) => {
      b.classList.toggle("is-active", (b.getAttribute("data-preset") || "") === hudState.activePreset);
    });
    updateTexture(hudState);
    if (glassActPrimary) {
      glassActPrimary.textContent = hudState.primaryBtn;
      glassActPrimary.hidden = !hudState.primaryBtn;
    }
    if (glassActSecondary) {
      glassActSecondary.textContent = hudState.secondaryBtn;
      glassActSecondary.hidden = !hudState.secondaryBtn;
    }

    if (hudState.isDestructive && isActive() && glassActPrimary) {
      glassActPrimary.focus({ preventScroll: true });
    }
  }

  function applyGlassCommand(cmd) {
    clearWfTimers();
    const res = evaluateStaticCommand(cmd);
    pushGlassHUD({ plan: null, ...res });
    playChime(res.isDestructive ? 0.82 : 1.3);
  }

  function runGlassWorkflow() {
    clearWfTimers();
    DOWNLOAD_PLAN.forEach((step, index) => {
      const position = `Step ${index + 1} of ${DOWNLOAD_PLAN.length}`;
      const t = window.setTimeout(() => {
        pushGlassHUD({
          badge: `Running · ${position}`,
          plan: null,
          badgeTone: "amber",
          title: `${position} · [${step.risk}] ${step.label}`,
          detail: "Simulating the workflow in the browser…",
          primaryBtn: "",
          secondaryBtn: "",
          mode: "workflow_running",
        });
      }, index * 380);
      wfTimers.push(t);
    });

    const doneTimer = window.setTimeout(() => {
      pushGlassHUD({
        badge: "2/2 Completed · Simulation",
        badgeTone: "sage",
        title: "Organized 4 files · Batch ready to undo",
        detail: "Press Ctrl+Z or choose Undo Batch.",
        primaryBtn: "Undo Batch",
        secondaryBtn: "",
        mode: "workflow_done",
      });
      playChime(1.45);
    }, DOWNLOAD_PLAN.length * 380 + 60);
    wfTimers.push(doneTimer);
  }

  function undoGlassWorkflow() {
    clearWfTimers();
    const rev = [
      { n: 2, text: "Undo Step 2 of 2 · Restoring categorized downloads…" },
      { n: 1, text: "Undo Step 1 of 2 · list_files (read-only · 0 moves)" },
    ];
    rev.forEach((s, idx) => {
      const t = window.setTimeout(() => {
        pushGlassHUD({
          badge: "Batch Undo · Reverse (2 → 1)",
          badgeTone: "amber",
          title: s.text,
          detail: "Simulating rollback and preserving conflicts…",
          primaryBtn: "",
          secondaryBtn: "",
          mode: "workflow_undoing",
        });
      }, idx * 340);
      wfTimers.push(t);
    });

    const doneTimer = window.setTimeout(() => {
      pushGlassHUD({
        badge: "Undo Verified · 4 restored",
        badgeTone: "sage",
        title: "Batch Undo complete (Step 2 → 1)",
        detail: "4 files restored in the simulation.",
        primaryBtn: "Run 2 steps",
        secondaryBtn: "",
        mode: "workflow_review",
      });
      playChime(1.2);
    }, rev.length * 340 + 60);
    wfTimers.push(doneTimer);
  }

  if (glassActPrimary) {
    glassActPrimary.addEventListener("click", async () => {
      if (hudState.mode === "calculator" && hudState.calcRaw) {
        const copiedState = hudState;
        const copied = await copyToClipboard(hudState.calcRaw);
        if (hudState !== copiedState) return;
        pushGlassHUD({
          badge: copied ? "Copied to clipboard" : "Clipboard unavailable",
          primaryBtn: copied ? "Copied" : "Retry copy",
        });
        const feedbackState = hudState;
        window.setTimeout(() => {
          if (hudState === feedbackState) pushGlassHUD({ badge: copiedState.badge, primaryBtn: copiedState.primaryBtn });
        }, 1500);
      } else if (hudState.mode === "workflow_review") {
        runGlassWorkflow();
      } else if (hudState.mode === "workflow_done") {
        undoGlassWorkflow();
      } else if (hudState.mode === "destructive_review") {
        pushGlassHUD({
          badge: "Cancelled · Safe Abort",
          badgeTone: "sage",
          title: "Cache purge aborted by user",
          detail: "0 bytes deleted · /var/cache/apt/archives untouched",
          primaryBtn: "",
          secondaryBtn: "",
          isDestructive: false,
          mode: "cancelled",
        });
      }
    });
  }

  if (glassActSecondary) {
    glassActSecondary.addEventListener("click", () => {
      if (hudState.mode === "workflow_review") {
        pushGlassHUD({
          badge: "Cancelled · 0 moves",
          plan: null,
          badgeTone: "sage",
          title: "Workflow cancelled before Step 1",
          detail: "Files in ~/Downloads remain untouched.",
          primaryBtn: "",
          secondaryBtn: "",
          mode: "cancelled",
        });
      } else if (hudState.mode === "destructive_review") {
        pushGlassHUD({
          badge: "Sandbox · Simulated Purge",
          badgeTone: "amber",
          title: "212.8 MB simulated reclaim verified",
          detail: "Browser sandbox only — real filesystem untouched.",
          primaryBtn: "",
          secondaryBtn: "",
          isDestructive: false,
          mode: "destructive_done",
        });
      }
    });
  }

  if (glassForm && glassInput) {
    glassForm.addEventListener("submit", (e) => {
      e.preventDefault();
      applyGlassCommand(glassInput.value);
    });
    glassInput.addEventListener("focus", () => updateTexture({ inputFocused: true }));
    glassInput.addEventListener("blur", () => updateTexture({ inputFocused: false }));
    glassInput.addEventListener("input", () => {
      hudState = { ...hudState, query: glassInput.value };
      updateTexture({ query: glassInput.value });
    });
  }

  glassPresetBtns.forEach((btn) => {
    btn.addEventListener("click", () => {
      applyGlassCommand(btn.getAttribute("data-preset") || "");
    });
  });

  document.addEventListener("keydown", (e) => {
    if (isActive() && document.getElementById("download-modal").hidden && (e.ctrlKey || e.metaKey) && e.key.toLowerCase() === "z" && hudState.mode === "workflow_done") {
      e.preventDefault();
      undoGlassWorkflow();
    }
  });

  root.addEventListener("keydown", (event) => {
    if (event.key === "Escape" && hudState.isDestructive && document.getElementById("download-modal").hidden) {
      event.preventDefault();
      glassActPrimary.click();
    }
  });
  pushGlassHUD(hudState);
}

function revealPage() {
  document.body.classList.add("is-loaded");
  document.getElementById("loader")?.classList.add("is-ready");
}

function initPrecisionCursor() {
  const cursor = document.getElementById("precision-cursor");
  if (!cursor) return { update() {}, setFrameDriven() {} };

  const eligible = matchMedia("(hover: hover) and (pointer: fine) and (prefers-reduced-motion: no-preference)");
  const editable = "input, textarea, select, [contenteditable], .glass-selectable-copy";
  const interactive = "a[href], button, [role='button'], [role='tab']";
  let x = 0, y = 0, targetX = 0, targetY = 0;
  let visible = false;
  let frameDriven = false;
  let moving = false;

  function setVisible(next) {
    if (visible === next) return;
    visible = next;
    cursor.classList.toggle("is-visible", next);
    document.body.classList.toggle("has-precision-cursor", next);
    if (!next) cursor.classList.remove("is-pressed");
  }

  function paint() {
    cursor.style.transform = `translate3d(${x}px, ${y}px, 0)`;
  }

  function onPointer(event) {
    const target = event.target;
    if (!eligible.matches || event.pointerType !== "mouse" ||
        !document.body.classList.contains("is-loaded") ||
        !(target instanceof Element) || target.closest(editable)) {
      setVisible(false);
      return;
    }

    targetX = event.clientX;
    targetY = event.clientY;
    if (!visible || !frameDriven) {
      x = targetX;
      y = targetY;
      paint();
    }
    moving = frameDriven;
    cursor.classList.toggle("is-interactive", Boolean(target.closest(interactive)));
    setVisible(true);
  }

  window.addEventListener("pointermove", onPointer, { passive: true });
  window.addEventListener("pointerover", onPointer, { passive: true });
  document.documentElement.addEventListener("pointerleave", () => setVisible(false));
  eligible.addEventListener("change", () => setVisible(false));
  document.addEventListener("visibilitychange", () => {
    if (document.hidden) setVisible(false);
  });
  window.addEventListener("pointerdown", (event) => {
    if (visible && event.button === 0) cursor.classList.add("is-pressed");
  }, { passive: true });
  window.addEventListener("pointerup", () => cursor.classList.remove("is-pressed"), { passive: true });
  window.addEventListener("pointercancel", () => setVisible(false), { passive: true });
  window.addEventListener("blur", () => setVisible(false));

  return {
    setFrameDriven(value) {
      frameDriven = value;
      setVisible(false);
    },
    update(dt) {
      if (!visible || !moving) return;
      const mix = 1 - Math.exp(-dt * 75);
      x += (targetX - x) * mix;
      y += (targetY - y) * mix;
      if (Math.abs(targetX - x) + Math.abs(targetY - y) < 0.2) {
        x = targetX;
        y = targetY;
        moving = false;
      }
      paint();
    },
  };
}

async function prepareHudFonts() {
  const fonts = document.fonts;
  if (!fonts) return;
  await Promise.allSettled([
    fonts.load('700 48px Migha'),
    fonts.load('700 48px "OTF Glusp"'),
    fonts.load('700 48px Antonio'),
    fonts.load('400 16px Heming', "Mavrick 0.039 ms"),
    fonts.load('500 16px Heming', "Mavrick 0.039 ms"),
    fonts.load('600 16px Heming', "Mavrick 0.039 ms"),
    fonts.load('400 16px Manrope'),
    fonts.load('500 16px Manrope'),
    fonts.load('600 16px Manrope'),
    fonts.load('400 16px "IBM Plex Mono"', "0.039 ms"),
    fonts.load('500 16px "IBM Plex Mono"', "0.039 ms"),
    fonts.load('600 16px "IBM Plex Mono"', "0.039 ms"),
  ]);
  await fonts.ready;
}

function enableStaticFallback(cursor) {
  if (document.body.classList.contains("is-fallback")) return;
  document.body.classList.add("is-fallback");
  cursor.setFrameDriven(false);
  const fallback = document.getElementById("static-fallback");
  fallback.hidden = false;

  for (const overlay of document.querySelectorAll(".act-overlay")) overlay.inert = true;
  initCommandDemo("static", fallback);
  revealPage();
}

/* --------------------------------------------------------------------------
   Main WebGL Stage Initialization
   -------------------------------------------------------------------------- */
async function initWebGLStage(cursor) {
  const motionPreference = matchMedia("(prefers-reduced-motion: reduce)");
  const canvas = document.getElementById("stage-canvas");
  if (!canvas || motionPreference.matches) {
    enableStaticFallback(cursor);
    return;
  }

  const contextOptions = { antialias: true, powerPreference: "high-performance", alpha: false };
  const context = canvas.getContext("webgl2", contextOptions);
  if (!context) {
    enableStaticFallback(cursor);
    return;
  }

  const [THREE, { RoomEnvironment }, { NightBotanicalValley }, { SmokedGlassMonolith }] = await Promise.all([
    import("three"),
    import("three/addons/environments/RoomEnvironment.js"),
    import("./valley.js"),
    import("./glass.js"),
    prepareHudFonts(),
  ]);

  if (motionPreference.matches || context.isContextLost()) {
    enableStaticFallback(cursor);
    return;
  }

  const renderer = new THREE.WebGLRenderer({ canvas, context, ...contextOptions });
  let width = window.innerWidth;
  let height = Math.max(window.innerHeight, 1);
  let displayDpr = Math.min(window.devicePixelRatio || 1, 2);
  const qualityScale = [1, 1, 0.85, 0.71, 0.60];
  const botanicalQuality = [1, 0.88, 0.80, 0.72, 0.65];
  let qualityTier = 0;
  renderer.setDrawingBufferSize(width, height, displayDpr);
  renderer.toneMapping = THREE.ACESFilmicToneMapping;
  renderer.toneMappingExposure = 1.02;
  renderer.outputColorSpace = THREE.SRGBColorSpace;

  const scene = new THREE.Scene();
  const backdrop = new THREE.Color(0x050507);
  scene.background = backdrop;
  scene.fog = new THREE.FogExp2(0x050507, 0.021);

  const camera = new THREE.PerspectiveCamera(
    45,
    width / height,
    0.1,
    240
  );
  scene.add(camera);

  const pmrem = new THREE.PMREMGenerator(renderer);
  const room = new RoomEnvironment();
  const environment = pmrem.fromScene(room, 0.04);
  room.dispose();
  pmrem.dispose();
  scene.environment = environment.texture;
  scene.environmentIntensity = 0.44;

  const keyLight = new THREE.DirectionalLight(0xf7f3eb, 2.10);
  keyLight.position.set(4.5, 6.0, 9.0);
  keyLight.layers.set(1);
  camera.add(keyLight);

  const rimLight = new THREE.DirectionalLight(0xf5efe2, 0.85);
  rimLight.position.set(-5.0, -3.5, 4.0);
  rimLight.layers.set(1);
  camera.add(rimLight);

  const valley = new NightBotanicalValley(scene);
  const monolith = new SmokedGlassMonolith(camera, {
    anisotropy: Math.min(8, renderer.capabilities.getMaxAnisotropy()),
  });
  monolith.root.traverse((object) => object.layers.set(1));
  const audio = new NightAudioEngine();

  const domPlanes = {
    navDomEl: document.getElementById("nav-dom-overlay"),
    shardEls: [
      document.getElementById("shard-dom-0"),
      document.getElementById("shard-dom-1"),
      document.getElementById("shard-dom-2"),
      document.getElementById("shard-dom-3"),
      document.getElementById("shard-dom-4"),
    ],
    backBtnEl: document.getElementById("act3-back-btn"),
  };

  const act1Overlay = document.getElementById("act1-overlay");
  const act2Overlay = document.getElementById("act2-overlay");
  const act3Overlay = document.getElementById("act3-overlay");
  const enterBtn = document.getElementById("enter-monolith");
  const audioBtn = document.getElementById("audio-toggle-btn");
  const audioIconMuted = document.getElementById("audio-icon-muted");
  const audioIconUnmuted = document.getElementById("audio-icon-unmuted");
  const modal = document.getElementById("download-modal");
  const stageEvents = new AbortController();
  const { signal } = stageEvents;
  const hoverMedia = matchMedia("(hover: hover)");

  let currentAct = 1;
  let revealArchitecture = false;
  let pendingFocus = null;
  act2Overlay.inert = true;
  act3Overlay.inert = true;
  initCommandDemo("glass", act2Overlay, {
    updateTexture: (data) => monolith.updateHUDData(data),
    playChime: (pitch) => audio.playGlassChime(pitch),
    isActive: () => currentAct === 2 && !signal.aborted,
  });

  function setAct(nextAct) {
    if (nextAct === currentAct) return;
    const prevAct = currentAct;
    currentAct = nextAct;
    document.body.dataset.act = String(nextAct);

    [
      { id: 1, el: act1Overlay },
      { id: 2, el: act2Overlay },
      { id: 3, el: act3Overlay },
    ].forEach(({ id, el }) => {
      if (!el) return;
      const active = id === nextAct && !(prevAct === 2 && nextAct === 3);
      el.inert = !active;
      el.classList.toggle("is-active", active);
      el.setAttribute("aria-hidden", active ? "false" : "true");
    });

    revealArchitecture = prevAct === 2 && nextAct === 3;
    pendingFocus = nextAct === 1 ? enterBtn : nextAct === 2 ? act2Overlay : domPlanes.backBtnEl;
    if (nextAct === 3) act3Overlay.scrollTop = 0;
    valley.startHoverTransition(false);
    valley.setAct(nextAct);
    monolith.setAct(nextAct);
    audio.setHoverState(false);
    audio.setAct(nextAct);

    if (prevAct === 2 && nextAct === 3) {
      audio.playShatter();
    } else if (nextAct === 2) {
      audio.playGlassChime(prevAct === 3 ? 1.15 : 1.0);
    }
  }

  document.querySelectorAll("[data-go-act]").forEach((btn) => {
    btn.addEventListener("click", () => {
      const target = Number(btn.getAttribute("data-go-act"));
      if (target >= 1 && target <= 3) setAct(target);
    }, { signal });
  });

  if (enterBtn) {
    const updateEnterHover = () => {
      const active = currentAct === 1 && (enterBtn.matches(":focus-visible") ||
        (hoverMedia.matches && enterBtn.matches(":hover")));
      valley.startHoverTransition(active);
      audio.setHoverState(active);
    };
    for (const event of ["pointerenter", "pointerleave", "focus", "blur"]) {
      enterBtn.addEventListener(event, updateEnterHover, { signal });
    }
    enterBtn.addEventListener("click", () => setAct(2), { signal });
  }

  const readArchBtn = document.getElementById("glass-read-arch");
  if (readArchBtn) readArchBtn.addEventListener("click", () => setAct(3), { signal });
  if (domPlanes.backBtnEl) domPlanes.backBtnEl.addEventListener("click", () => setAct(2), { signal });

  if (audioBtn) {
    audioBtn.addEventListener("click", async () => {
      const playing = await audio.toggle();
      audioBtn.setAttribute("aria-pressed", playing ? "true" : "false");
      if (audioIconMuted) audioIconMuted.hidden = playing;
      if (audioIconUnmuted) audioIconUnmuted.hidden = !playing;
    }, { signal });
  }

  window.addEventListener("pointermove", (event) => {
    if (event.pointerType === "touch" || !modal.hidden) return;
    const nx = event.clientX / Math.max(width, 1) * 2 - 1;
    const ny = 1 - event.clientY / height * 2;
    valley.setPointerNDC(nx, ny);
    monolith.setPointerNDC(nx, ny);
  }, { passive: true, signal });

  function resetStagePointer() {
    valley.setPointerNDC(9, 9);
    monolith.setPointerNDC(9, 9);
  }
  document.documentElement.addEventListener("pointerleave", resetStagePointer, { signal });
  window.addEventListener("blur", resetStagePointer, { signal });

  window.addEventListener("wheel", (event) => {
    if (currentAct !== 1 || !modal.hidden || event.ctrlKey) return;
    const unit = event.deltaMode === 1 ? 16 : event.deltaMode === 2 ? height : 1;
    valley.addScrollImpulse(event.deltaY * unit);
  }, { passive: true, signal });

  let lastTouchY = null;
  window.addEventListener("touchstart", (event) => {
    lastTouchY = event.touches.length === 1 ? event.touches[0].clientY : null;
    resetStagePointer();
  }, { passive: true, signal });
  window.addEventListener("touchmove", (event) => {
    if (currentAct !== 1 || !modal.hidden || event.touches.length !== 1 || lastTouchY === null) return;
    const dy = lastTouchY - event.touches[0].clientY;
    lastTouchY = event.touches[0].clientY;
    valley.addScrollImpulse(dy * 2.4);
  }, { passive: true, signal });
  window.addEventListener("touchend", () => { lastTouchY = null; }, { passive: true, signal });
  window.addEventListener("touchcancel", () => { lastTouchY = null; }, { passive: true, signal });

  function renderDpr() {
    // Keep at least one bitmap pixel per CSS pixel on ordinary displays.
    return Math.max(Math.min(1, displayDpr), displayDpr * qualityScale[qualityTier]);
  }

  function setRenderScale() {
    const dpr = renderDpr();
    renderer.setDrawingBufferSize(width, height, dpr);
    valley.onResize(width, height, dpr);
  }

  function handleResize() {
    const nextWidth = window.innerWidth;
    const nextHeight = Math.max(window.innerHeight, 1);
    const nextDpr = Math.min(window.devicePixelRatio || 1, 2);
    if (width === nextWidth && height === nextHeight && displayDpr === nextDpr) return;

    width = nextWidth;
    height = nextHeight;
    displayDpr = nextDpr;
    camera.aspect = width / height;
    camera.updateProjectionMatrix();
    setRenderScale();
    monolith.onResize(width, height);
    syncDomPlaneSizes();
  }

  function syncDomPlaneSizes() {
    domPlanes.navDomEl.style.width = `${monolith.NAV_W}px`;
    domPlanes.navDomEl.style.height = `${monolith.NAV_H}px`;
    for (const el of domPlanes.shardEls) {
      el.style.width = `${monolith.PANEL_W}px`;
      el.style.height = `${monolith.PANEL_H}px`;
    }
  }
  let resizePending = false;
  let rafId = 0;
  let ready = false;
  let lastTime = 0;
  let elapsed = 0;
  let frameAverage = 16.67;
  let pressureTime = 0;
  let headroomTime = 0;
  let resizeGrace = 12;
  let lastHovered = -1;

  window.addEventListener("resize", () => { resizePending = true; }, { passive: true, signal });

  function sampleRendering(frameMs) {
    if (resizeGrace > 0) {
      resizeGrace -= 1;
      return;
    }
    const previousDpr = renderDpr();
    const sample = Math.min(frameMs, 85);
    frameAverage += (sample - frameAverage) * 0.075;
    pressureTime = frameAverage > 18 ? pressureTime + sample : 0;
    headroomTime = frameAverage < 16.85 ? headroomTime + sample : 0;
    if (pressureTime > 280 && qualityTier < qualityScale.length - 1) {
      qualityTier += 1;
    } else if (headroomTime > 6200 && qualityTier > 0) {
      qualityTier -= 1;
    } else {
      return;
    }

    if (previousDpr !== renderDpr()) setRenderScale();
    valley.setQuality(botanicalQuality[qualityTier]);
    pressureTime = 0;
    headroomTime = 0;
    resizeGrace = 12;
  }

  function renderStage() {
    camera.layers.set(0);
    renderer.render(scene, camera);
    if (!monolith.slabGroup.visible && !monolith.backSliverGroup.visible) return;

    // Give the camera-mounted glass independent depth against the valley.
    camera.layers.set(1);
    scene.background = null;
    renderer.autoClear = false;
    renderer.clearDepth();
    renderer.render(scene, camera);
    renderer.autoClear = true;
    scene.background = backdrop;
  }

  function tick(now) {
    if (document.hidden || signal.aborted) {
      rafId = 0;
      return;
    }
    const frameMs = Math.max(0, now - lastTime);
    const dt = Math.min(frameMs * 0.001, 0.12);
    lastTime = now;
    elapsed += dt;

    if (resizePending) {
      resizePending = false;
      resizeGrace = 12;
      handleResize();
    }
    sampleRendering(frameMs);
    valley.update(dt, elapsed, camera, currentAct);
    monolith.update(dt, elapsed, width, height, domPlanes);
    cursor.update(dt);
    if (lastHovered !== monolith.hoveredIndex) {
      lastHovered = monolith.hoveredIndex;
      if (modal.hidden) audio.playGlassHover(lastHovered);
    }

    valley.group.visible = currentAct !== 3 || revealArchitecture || valley.sceneFade > 0.035;
    renderStage();
    if (revealArchitecture && monolith.shatterState === "shattered") {
      revealArchitecture = false;
      act3Overlay.inert = false;
      act3Overlay.classList.add("is-active");
      act3Overlay.setAttribute("aria-hidden", "false");
    }
    if (pendingFocus && (currentAct === 1 || (currentAct === 3 && !revealArchitecture) ||
      (currentAct === 2 && monolith.controlsReady))) {
      pendingFocus.focus({ preventScroll: true });
      pendingFocus = null;
    }
    rafId = window.requestAnimationFrame(tick);
  }

  function startFrames() {
    if (document.hidden || signal.aborted || !ready || rafId) return;
    lastTime = performance.now();
    resizeGrace = 12;
    rafId = window.requestAnimationFrame(tick);
  }

  function stopFrames() {
    window.cancelAnimationFrame(rafId);
    rafId = 0;
  }

  document.addEventListener("visibilitychange", () => {
    audio.setHidden(document.hidden);
    if (document.hidden) stopFrames();
    else startFrames();
  }, { signal });

  function useFallback() {
    stopFrames();
    stageEvents.abort();
    audio.setHidden(true);
    environment.dispose();
    renderer.dispose();
    enableStaticFallback(cursor);
  }
  canvas.addEventListener("webglcontextlost", useFallback, { once: true, signal });
  motionPreference.addEventListener("change", (event) => {
    if (event.matches) useFallback();
  }, { signal });

  syncDomPlaneSizes();
  setRenderScale();
  valley.update(0.016, 0, camera, 1);
  monolith.update(0.016, 0, width, height, domPlanes);

  // Precompile both lighting configurations and upload the first HUD textures under the loader.
  const warmLayer = async (layer) => {
    camera.layers.set(layer);
    if (typeof renderer.compileAsync === "function" && renderer.extensions.has("KHR_parallel_shader_compile")) {
      await renderer.compileAsync(scene, camera);
    } else {
      renderer.compile(scene, camera);
    }
  };
  try {
    await warmLayer(0);
    if (signal.aborted) return;
    await warmLayer(1);
    if (signal.aborted) return;
    if (!document.hidden) {
      monolith.slabGroup.visible = true;
      monolith.backSliverGroup.visible = true;
      renderStage();
      monolith.slabGroup.visible = false;
      monolith.backSliverGroup.visible = false;
      renderStage();
    }
  } catch {
    if (!signal.aborted) useFallback();
    return;
  }

  cursor.setFrameDriven(true);
  ready = true;
  act1Overlay.inert = false;
  revealPage();
  startFrames();
}

/* --------------------------------------------------------------------------
   Boot
   -------------------------------------------------------------------------- */
document.addEventListener("DOMContentLoaded", () => {
  initModal();
  initSafetyPipeline();
  const cursor = initPrecisionCursor();
  initWebGLStage(cursor).catch(() => enableStaticFallback(cursor));
});
