// Train AI: train your own language model from scratch on this server, and learn every piece of maths it uses.
// trainapi.py does the work (hardware, data checks, the trainer process); every lesson widget below computes its
// numbers here in the page, with the same formulas the trainer uses. Loaded after app.js.
(function () {
  "use strict";
  const { esc, html, raw, setHTML } = NY;
  const U = NY.ui;
  const { S, V, $, toast, getJSON, postJSON, store } = U;
  const TABS = [["learn", "1 · Learn"], ["data", "2 · Your data"], ["train", "3 · Train"], ["inside", "4 · Inside the model"], ["chat", "5 · Chat"]];
  const TR = {
    tab: store.get("train.tab", "learn"), lesson: +store.get("train.lesson", 0) || 0, ov: null, hours: +store.get("train.hours", 1) || 1,
    sel: JSON.parse(store.get("train.sel", "[]") || "[]"), check: null, form: null, model: "", resume: false, status: null, since: 0,
    lines: [], pollT: 0, ovT: 0, chat: { model: "", history: [], mode: "chat", temperature: 0.7, top_k: 40, top_p: 0.9, busy: false },
    explain: null, exModel: "", exPrompt: "User: Hello, who are you?", exBusy: false, upload: null, ovPollT: 0,
    mp: { file: "", speakers: null, speaker: "", context: 3 },
    edited: null, preset: store.get("train.preset", "") || "",
    q: (() => { try { return JSON.parse(store.get("train.quality", "") || "null"); } catch (_) { return null; } })() || { on: false, preset: "starter", prompts: [], pass: 1, target_loss: "", give_up: 10 },
    machines: JSON.parse(store.get("train.machines", "[]") || "[]"), share: store.get("train.share", "1") !== "0", together: store.get("train.together", "share") || "share",
  };

  // ---------- small maths helpers (the same formulas as the trainer) ----------
  const f = (x, p = 4) => (x == null || !isFinite(x) ? "–" : Math.abs(x) >= 1e5 || (Math.abs(x) < 1e-3 && x !== 0) ? (+x).toExponential(2) : (+(+x).toPrecision(p)).toString());
  const n0 = (x) => (x == null || !isFinite(x) ? "–" : Math.round(x).toLocaleString());
  const gb = (b) => (b >= 1073741824 ? (b / 1073741824).toFixed(1) + " GB" : (b / 1048576).toFixed(0) + " MB");
  const dur = (s) => (!isFinite(s) ? "–" : s < 60 ? Math.round(s) + " s" : s < 3600 ? Math.round(s / 60) + " min" : s < 86400 * 2 ? (s / 3600).toFixed(1) + " h" : s < 86400 * 365 * 2 ? Math.round(s / 86400) + " days" : n0(s / 86400 / 365) + " years");
  const erf = (x) => { const s = Math.sign(x), a = Math.abs(x), t = 1 / (1 + 0.3275911 * a);
    return s * (1 - (((((1.061405429 * t - 1.453152027) * t) + 1.421413741) * t - 0.284496736) * t + 0.254829592) * t * Math.exp(-a * a)); };
  const gelu = (x) => 0.5 * x * (1 + erf(x / Math.SQRT2));
  const softmax = (v) => { const m = Math.max(...v.filter(isFinite)); const e = v.map((x) => (isFinite(x) ? Math.exp(x - m) : 0)); const s = e.reduce((a, b) => a + b, 0); return e.map((x) => x / s); };
  const rng = (seed) => () => { seed |= 0; seed = (seed + 0x6d2b79f5) | 0; let t = Math.imul(seed ^ (seed >>> 15), 1 | seed); t = (t + Math.imul(t ^ (t >>> 7), 61 | t)) ^ t; return ((t ^ (t >>> 14)) >>> 0) / 4294967296; };
  const randn = (r) => Math.sqrt(-2 * Math.log(r() + 1e-12)) * Math.cos(2 * Math.PI * r());
  const mat = (rows, cols, r, scale = 0.5) => Array.from({ length: rows }, () => Array.from({ length: cols }, () => +(randn(r) * scale).toFixed(2)));
  const mul = (A, B) => A.map((row) => B[0].map((_, j) => row.reduce((s, a, k) => s + a * B[k][j], 0)));
  const T_ = (A) => A[0].map((_, j) => A.map((r) => r[j]));
  const params = (V, T, d, L) => V * d + T * d + L * (12 * d * d + 13 * d) + 2 * d + d * V + V;
  const actBytes = (B, T, d, L, h, V) => 4 * B * T * (L * (34 * d + 5 * h * T) + 3 * V);
  const lrAt = (step, total, peak, warm) => (step <= warm ? peak * step / Math.max(1, warm) : peak * (0.1 + 0.9 * 0.5 * (1 + Math.cos(Math.PI * Math.min(1, (step - warm) / Math.max(1, total - warm))))));

  // ---------- drawing ----------
  function cssv(name, fb) { return getComputedStyle(document.documentElement).getPropertyValue(name).trim() || fb; }
  function chart(cv, series, o = {}) {
    if (!cv) return;
    const dpr = window.devicePixelRatio || 1, w = cv.clientWidth || 300, h = cv.clientHeight || 160;
    cv.width = w * dpr; cv.height = h * dpr;
    const c = cv.getContext("2d"); c.scale(dpr, dpr); c.clearRect(0, 0, w, h);
    c.font = "11px system-ui, sans-serif";
    const xs = [], ys = [];
    series.forEach((s) => s.pts.forEach((p) => { if (isFinite(p[1]) && isFinite(p[0])) { xs.push(p[0]); ys.push(p[1]); } }));
    if (o.mark) { xs.push(o.mark[0]); ys.push(o.mark[1]); }
    if (!xs.length) { c.fillStyle = cssv("--faint", "#888"); c.fillText(o.empty || "No data yet", 12, h / 2); return; }
    let x0 = o.x0 ?? Math.min(...xs), x1 = o.x1 ?? Math.max(...xs), y0 = o.y0 ?? Math.min(...ys), y1 = o.y1 ?? Math.max(...ys);
    if (x1 === x0) x1 = x0 + 1; if (y1 === y0) { y1 += 1; y0 -= 1; }
    if (o.y0 == null) { const p = (y1 - y0) * 0.06; y0 -= p; y1 += p; }
    const L = 48, R = 10, Tp = 10, Bt = 22, X = (x) => L + (x - x0) / (x1 - x0) * (w - L - R), Y = (y) => Tp + (1 - (y - y0) / (y1 - y0)) * (h - Tp - Bt);
    c.strokeStyle = cssv("--line", "#333"); c.fillStyle = cssv("--faint", "#888"); c.lineWidth = 1;
    for (let i = 0; i <= 4; i++) { const y = y0 + (y1 - y0) * i / 4; c.beginPath(); c.moveTo(L, Y(y)); c.lineTo(w - R, Y(y)); c.stroke(); c.fillText(f(y, 3), 4, Y(y) + 4); }
    c.fillText(f(x0, 4), L, h - 6); const lx = f(x1, 4); c.fillText(lx, w - R - c.measureText(lx).width, h - 6);
    if (o.xlabel) c.fillText(o.xlabel, (w - c.measureText(o.xlabel).width) / 2, h - 6);
    series.forEach((s) => {
      c.strokeStyle = s.color; c.lineWidth = s.width || 1.6; c.setLineDash(s.dash || []); c.globalAlpha = s.alpha || 1; c.beginPath();
      let on = false; s.pts.forEach((p) => { if (!isFinite(p[1])) { on = false; return; } if (on) c.lineTo(X(p[0]), Y(p[1])); else { c.moveTo(X(p[0]), Y(p[1])); on = true; } });
      c.stroke(); c.setLineDash([]); c.globalAlpha = 1;
      if (s.dots) { c.fillStyle = s.color; s.pts.forEach((p) => { c.beginPath(); c.arc(X(p[0]), Y(p[1]), 2.6, 0, 7); c.fill(); }); }
    });
    if (o.mark) { c.fillStyle = cssv("--bad", "#f66"); c.beginPath(); c.arc(X(o.mark[0]), Y(o.mark[1]), 5, 0, 7); c.fill(); }
  }
  const heat = (M, o = {}) => {
    const flat = M.flat().filter((x) => isFinite(x) && x > -1e8); const mx = Math.max(1e-9, ...flat.map(Math.abs));
    return raw('<table class="tr-heat">' + (o.cols ? "<tr><th></th>" + o.cols.map((c) => "<th>" + esc(c) + "</th>").join("") + "</tr>" : "") +
      M.map((row, i) => "<tr>" + (o.rows ? "<th>" + esc(o.rows[i]) + "</th>" : "") + row.map((x) => {
        if (!isFinite(x) || x <= -1e8) return '<td class="masked" title="masked: a token can\'t look at later tokens">–∞</td>';
        const a = Math.min(1, Math.abs(x) / mx), col = x >= 0 ? "124,140,255" : "248,113,113";
        return '<td style="background:rgba(' + col + "," + (0.08 + a * 0.6).toFixed(2) + ')">' + esc(f(x, 3)) + "</td>";
      }).join("") + "</tr>").join("") + "</table>");
  };
  const vec = (v, n = 8) => raw('<span class="tr-vec">[' + v.slice(0, n).map((x) => esc(f(x, 3))).join(", ") + (v.length > n ? ", …" : "") + "]</span>");
  const eq = (s) => raw('<div class="tr-eq">' + s + "</div>");
  const keyTable = (rows) => raw('<table class="tr-key"><tbody>' + rows.map((r) => "<tr><td class=\"sym\">" + r[0] + "</td><td>" + r[1] + "</td>" + (r[2] != null ? '<td class="val">' + r[2] + "</td>" : "<td></td>") + "</tr>").join("") + "</tbody></table>");
  const slider = (path, label, min, max, step, value, note) => html`<label class="tr-sl"><span>${raw(label)} <b>${f(value, 4)}</b></span>
    <input type="range" min="${min}" max="${max}" step="${step}" value="${value}" data-tr-in="${path}">${note ? html`<span class="faint small">${note}</span>` : ""}</label>`;

  // ---------- lessons ----------
  const L = {
    neuron: { x1: 1, x2: -0.5, w1: 0.8, w2: 0.3, b: 0.1 },
    bpe: { text: "the cat sat on the mat. the cat ate the rat.", merges: 6 },
    attn: { seed: 7, focus: 1 },
    norm: { v: [2, -1, 0.5, 3.5] },
    ce: { logits: [2.0, 1.0, 0.5, -1.0, 0.2], target: 0 },
    gd: { w: -1, lr: 0.05, x: 2, y: 3, hist: [] },
    adam: { lr: 0.01, b1: 0.9, b2: 0.95, wd: 0.1, eps: 1e-8, w0: 0.5, noise: 0.5, seed: 4 },
    sched: { peak: 0.001, warmup: 100, steps: 2000, at: 500 },
    size: { V: 2048, T: 128, d: 192, L: 4, h: 6, B: 32 },
    samp: { temp: 1, k: 0, p: 1 },
    multi: { a: 800, b: 300, c: 0, N: 20, link: 100, round: 120 },
  };
  const WORDS = [" the", " cat", " sat", " on", " mat"];

  const LESSONS = [
    { id: "big", title: "The big picture", body: () => html`
      <p>A language model is one function with millions of adjustable numbers (<b>weights</b>). Give it some text and it returns, for every possible next piece of text, a probability. <b>Training</b> nudges the weights so that the piece that really came next gets a higher probability. That's all — everything else is how to do that well.</p>
      <div class="tr-flow">${["Text", "Tokens (numbers)", "Embeddings (vectors)", "Transformer blocks ×L", "Logits (a score per token)", "Softmax (probabilities)", "Pick the next token"].map((s, i) => html`<span class="tr-flow-step">${s}</span>${i < 6 ? raw('<span class="tr-flow-arrow">→</span>') : ""}`)}</div>
      <h4>One training step</h4>
      <ol class="tr-steps"><li><b>Forward:</b> run a batch of text windows through the model; at every position it predicts the next token.</li>
        <li><b>Loss:</b> measure how wrong it was: the average of −ln(probability it gave the right token).</li>
        <li><b>Backward:</b> calculus (the chain rule) gives, for every weight, how much the loss would change if that weight moved a little: the <b>gradient</b>.</li>
        <li><b>Update:</b> move every weight a small step against its gradient (AdamW decides the step size per weight).</li></ol>
      <p>Repeat thousands of times. Lessons 2–13 open each box with its exact maths and live numbers; tab 4 shows the real values inside <i>your</i> model.</p>
      ${eq("w ← w − η · ∂ℒ/∂w")}
      ${keyTable([["w", "any one weight"], ["η (eta)", "learning rate: the step size"], ["ℒ", "the loss: how wrong the model was"], ["∂ℒ/∂w", "the gradient: how the loss changes when w changes"]])}` },
    { id: "neuron", title: "A neuron", body: () => {
      const s = L.neuron, z = s.w1 * s.x1 + s.w2 * s.x2 + s.b, a = gelu(z), relu = Math.max(0, z);
      const pts = []; for (let x = -4; x <= 4; x += 0.1) pts.push([x, gelu(x)]);
      return html`<p>The basic unit: multiply each input by a weight, add them up, add a bias, then bend the result with an <b>activation function</b>. Without the bend, stacking layers would still be one straight-line function.</p>
      ${eq("z = w₁x₁ + w₂x₂ + b &nbsp;&nbsp; a = GELU(z) = z · Φ(z)")}
      <div class="tr-two"><div>${slider("neuron.x1", "input x₁", -3, 3, 0.1, s.x1)}${slider("neuron.x2", "input x₂", -3, 3, 0.1, s.x2)}${slider("neuron.w1", "weight w₁", -2, 2, 0.05, s.w1)}${slider("neuron.w2", "weight w₂", -2, 2, 0.05, s.w2)}${slider("neuron.b", "bias b", -2, 2, 0.05, s.b)}</div>
      <div><div class="tr-calc">z = ${f(s.w1)}×${f(s.x1)} + ${f(s.w2)}×${f(s.x2)} + ${f(s.b)}<br>&nbsp;&nbsp;= ${f(s.w1 * s.x1)} + ${f(s.w2 * s.x2)} + ${f(s.b)} = <b>${f(z)}</b><br>
        GELU(z) = ${f(z)} × Φ(${f(z)}) = ${f(z)} × ${f(0.5 * (1 + erf(z / Math.SQRT2)))} = <b>${f(a)}</b><br><span class="faint">(ReLU would give max(0, z) = ${f(relu)})</span></div>
        <canvas class="tr-cv" data-tr-chart="gelu"></canvas></div></div>
      ${keyTable([["xᵢ", "inputs: in a transformer, the numbers describing a token", f(s.x1) + ", " + f(s.x2)], ["wᵢ", "weights: learned", f(s.w1) + ", " + f(s.w2)], ["b", "bias: learned shift", f(s.b)], ["z", "pre-activation", f(z)], ["Φ(z)", "chance a standard normal is below z (0 to 1)", f(0.5 * (1 + erf(z / Math.SQRT2)))], ["a", "the neuron's output", f(a)]])}
      <p class="muted small">A layer is many neurons at once, written as a matrix: <code>a = GELU(xW + b)</code>. Your model's feed-forward layers have 4·d neurons each.</p>`;
    }, draw: () => { const p = []; for (let x = -4; x <= 4; x += 0.1) p.push([x, gelu(x)]); const z = L.neuron.w1 * L.neuron.x1 + L.neuron.w2 * L.neuron.x2 + L.neuron.b;
      chart($('[data-tr-chart="gelu"]'), [{ pts: p, color: cssv("--accent", "#7c8cff") }], { mark: [z, gelu(z)], xlabel: "z → GELU(z)" }); } },
    { id: "bpe", title: "Tokens: byte-pair encoding", body: () => {
      const s = L.bpe; const steps = bpeDemo(s.text, s.merges);
      return html`<p>The model works on numbers, so text is cut into <b>tokens</b>. The text is first split into word pieces (a word keeps the space in front of it). BPE starts with every single byte as a token (256 of them) and, inside those pieces, repeatedly <b>merges the most frequent neighbouring pair</b> into a new token, until the vocabulary has V entries. Common words become one token; rare words are spelled from pieces, so nothing is ever unknown.</p>
      <label class="tr-sl"><span>Text to tokenise</span><input class="input" data-tr-in="bpe.text" value="${s.text}"></label>
      ${slider("bpe.merges", "number of merges", 0, 20, 1, s.merges)}
      <table class="tr-key"><tr><th>Merge</th><th>Most frequent pair</th><th>Count</th><th>New token</th><th>Tokens left</th></tr>
      ${steps.log.map((m, i) => html`<tr><td>${i + 1}</td><td><code>${vis(m.a)}</code> + <code>${vis(m.b)}</code></td><td>${m.count}</td><td>#${256 + i} = <code>${vis(m.a + m.b)}</code></td><td>${m.left}</td></tr>`)}</table>
      <div class="tr-toks">${steps.tokens.map((t) => html`<span class="tr-tok">${vis(t)}</span>`)}</div>
      <p class="muted small">${s.text.length} characters → ${steps.tokens.length} tokens (${f(s.text.length / Math.max(1, steps.tokens.length), 3)} characters per token). Your real tokenizer learns thousands of merges from your whole dataset; tab 4 shows how it splits any text.</p>`;
    } },
    { id: "embed", title: "Embeddings and positions", body: () => {
      const r = rng(11), d = 6, E = mat(5, d, r, 0.6), P = mat(5, d, r, 0.3);
      return html`<p>Each token ID picks one row of the <b>token embedding matrix</b> E (V rows × d columns): a list of d numbers that describes that token. Attention alone can't tell order, so a second learned matrix P adds a row for each <b>position</b>. Both start random and are learned like every other weight.</p>
      ${eq("x<sub>t</sub> = E[token<sub>t</sub>] + P[t]")}
      <table class="tr-key"><tr><th>t</th><th>token</th><th>E[token] (d=${d})</th><th>+ P[t]</th><th>= x<sub>t</sub></th></tr>
      ${WORDS.map((w, t) => html`<tr><td>${t}</td><td><code>${vis(w)}</code></td><td>${vec(E[t])}</td><td>${vec(P[t])}</td><td>${vec(E[t].map((x, i) => x + P[t][i]))}</td></tr>`)}</table>
      ${keyTable([["V", "vocabulary size: rows of E", "your setting: " + n0(curSet().vocab_size)], ["d", "width: numbers per token", "your setting: " + curSet().dim], ["T", "context: rows of P, the longest text it can see", "your setting: " + curSet().context], ["E", "token embedding matrix, V×d weights", n0(curSet().vocab_size * curSet().dim) + " weights"], ["P", "position embedding matrix, T×d weights", n0(curSet().context * curSet().dim) + " weights"]])}
      <p class="muted small">After training, tokens used in similar ways end up with similar rows: that's how “meaning” is stored. (Numbers here are random for illustration; tab 4 shows your model's real rows.)</p>`;
    } },
    { id: "attn", title: "Attention", body: () => {
      const A = attnDemo();
      return html`<p>Attention lets each token gather information from earlier tokens. Every token makes three vectors with learned matrices: a <b>query</b> (what am I looking for?), a <b>key</b> (what do I contain?) and a <b>value</b> (what do I pass on?). The score between two tokens is their query·key dot product; softmax turns the scores in a row into weights that add to 1; the output is the weighted sum of values. A token may only look backwards (the <b>causal mask</b>), or training would be cheating.</p>
      ${eq("Q = XW<sub>Q</sub>, K = XW<sub>K</sub>, V = XW<sub>V</sub> &nbsp;&nbsp; A = softmax( QKᵀ ⁄ √d<sub>h</sub> + M ) &nbsp;&nbsp; out = A·V")}
      ${slider("attn.focus", "how sharply it focuses (scales W<sub>Q</sub>)", 0, 4, 0.1, L.attn.focus)}
      <div class="tr-two"><div><h4>Scores QKᵀ/√d<sub>h</sub> + mask</h4>${heat(A.scores, { rows: WORDS.slice(0, 4), cols: WORDS.slice(0, 4) })}</div>
        <div><h4>Weights A = softmax per row</h4>${heat(A.weights, { rows: WORDS.slice(0, 4), cols: WORDS.slice(0, 4) })}</div></div>
      <div class="tr-calc">Row “${vis(WORDS[3])}”: scores ${A.scores[3].map((x) => f(x, 3)).join(", ")} → e<sup>s</sup> ${A.scores[3].map((x) => f(Math.exp(x), 3)).join(", ")} → ÷ ${f(A.scores[3].reduce((a, x) => a + Math.exp(x), 0), 4)} → ${A.weights[3].map((x) => f(x, 3)).join(", ")} (sum 1)<br>
        out = ${A.weights[3].map((wt, j) => f(wt, 2) + "·v" + j).join(" + ")} = ${vec(A.out[3])}</div>
      ${keyTable([["X", "token vectors (after layer norm), T×d"], ["W<sub>Q</sub>, W<sub>K</sub>, W<sub>V</sub>", "learned d×d matrices (3d² weights + 3d biases)"], ["h", "number of heads: attention done h times in parallel on slices of size d<sub>h</sub> = d/h", "yours: " + curSet().heads + " heads × " + Math.floor(curSet().dim / curSet().heads)], ["√d<sub>h</sub>", "keeps scores from growing with the slice size, so softmax doesn't saturate"], ["M", "mask: 0 for allowed, −∞ for future tokens (e<sup>−∞</sup> = 0)"], ["W<sub>O</sub>", "output matrix that mixes the heads back together (d² + d weights)"]])}`;
    } },
    { id: "block", title: "Layer norm, feed-forward, residuals", body: () => {
      const v = L.norm.v, mu = v.reduce((a, b) => a + b, 0) / v.length, va = v.reduce((a, b) => a + (b - mu) ** 2, 0) / v.length, nv = v.map((x) => (x - mu) / Math.sqrt(va + 1e-5));
      return html`<p>A transformer block is attention followed by a small two-layer network applied to each token separately. Two tricks make deep stacks trainable: <b>layer norm</b> rescales each token's vector to mean 0 and variance 1 before each part, and a <b>residual connection</b> adds each part's output to its input instead of replacing it, so information (and gradients) can flow straight through.</p>
      ${eq("h = x + Attention(LN₁(x)) &nbsp;&nbsp; y = h + W₂ · GELU(W₁ · LN₂(h) + b₁) + b₂")}
      ${eq("LN(x) = γ ⊙ (x − μ) ⁄ √(σ² + ε) + β")}
      <div class="tr-two"><div>${v.map((x, i) => slider("norm.v." + i, "x" + "₀₁₂₃"[i], -5, 5, 0.1, x))}</div>
      <div class="tr-calc">μ = (${v.map((x) => f(x)).join(" + ")}) ÷ 4 = <b>${f(mu)}</b><br>σ² = mean of (x − μ)² = <b>${f(va)}</b><br>√(σ² + 0.00001) = ${f(Math.sqrt(va + 1e-5))}<br>normalised = ${vec(nv)}<br><span class="faint">then × γ, + β (learned, start at 1 and 0)</span></div></div>
      ${keyTable([["μ, σ²", "mean and variance of one token's d numbers"], ["ε", "0.00001: avoids dividing by zero"], ["γ, β", "learned scale and shift, d each (2d weights per norm)"], ["W₁, b₁", "d → 4d: “expands” each token (4d² + 4d weights)", "yours: " + n0(4 * curSet().dim ** 2 + 4 * curSet().dim)], ["W₂, b₂", "4d → d: back down (4d² + d weights)"], ["L", "how many blocks are stacked", "yours: " + curSet().layers]])}
      <p class="muted small">Per block: attention 4d² + 4d, feed-forward 8d² + 5d, two norms 4d — total 12d² + 13d weights. Lesson 10 adds them up for your model.</p>`;
    } },
    { id: "ce", title: "Logits, softmax and the loss", body: () => {
      const s = L.ce, p = softmax(s.logits), loss = -Math.log(p[s.target]);
      return html`<p>The last layer turns each token's vector into one score per vocabulary entry: the <b>logits</b>. Softmax makes them probabilities. The <b>cross-entropy loss</b> is −ln of the probability the model gave to the token that actually came next: 0 if it was certain and right, large if it was confident and wrong.</p>
      ${eq("p<sub>i</sub> = e<sup>z<sub>i</sub></sup> ⁄ Σ<sub>j</sub> e<sup>z<sub>j</sub></sup> &nbsp;&nbsp; ℒ = −ln p<sub>target</sub> &nbsp;&nbsp; ∂ℒ/∂z<sub>i</sub> = p<sub>i</sub> − [i = target]")}
      <div class="tr-two"><div>${s.logits.map((z, i) => slider("ce.logits." + i, "logit z for “" + esc(WORDS[i].trim()) + "”", -4, 6, 0.1, z))}
        <label class="tr-sl"><span>The real next token</span><select class="select" data-tr-in="ce.target">${raw(WORDS.map((w, i) => '<option value="' + i + '"' + (i === s.target ? " selected" : "") + ">" + esc(w.trim()) + "</option>").join(""))}</select></label></div>
      <div><table class="tr-key"><tr><th>token</th><th>z</th><th>e<sup>z</sup></th><th>p</th><th>gradient p − y</th></tr>
        ${s.logits.map((z, i) => html`<tr class="${i === s.target ? "hl" : ""}"><td>${WORDS[i].trim()}</td><td>${f(z)}</td><td>${f(Math.exp(z))}</td><td><span class="tr-pbar" style="width:${Math.round(p[i] * 80)}px"></span>${f(p[i], 3)}</td><td>${f(p[i] - (i === s.target ? 1 : 0), 3)}</td></tr>`)}</table>
        <div class="tr-calc">ℒ = −ln(${f(p[s.target], 4)}) = <b>${f(loss, 4)}</b><br>Perplexity e<sup>ℒ</sup> = ${f(Math.exp(loss), 3)}: as unsure as picking among ${f(Math.exp(loss), 3)} equally likely tokens.<br>Guessing at random among V tokens gives ℒ = ln V (ln ${n0(curSet().vocab_size)} = ${f(Math.log(curSet().vocab_size), 4)} for your vocabulary): that's where training starts.</div></div></div>
      <p class="muted small">The gradient p − y is beautifully simple: raise the right token's logit, lower the others in proportion to how much probability they stole. Backpropagation carries this back through every layer.</p>`;
    } },
    { id: "gd", title: "Backpropagation and gradient descent", body: () => {
      const s = L.gd, pred = s.w * s.x, loss = (pred - s.y) ** 2, g = 2 * s.x * (pred - s.y);
      return html`<p>Fit one weight so that w·x matches y. The loss is the squared error. The <b>chain rule</b> gives the gradient: how the loss changes with w. Gradient descent steps against it. In the real model, backpropagation does exactly this for millions of weights at once, layer by layer from the output back to the input.</p>
      ${eq("ℒ(w) = (w·x − y)² &nbsp;&nbsp; ∂ℒ/∂w = ∂ℒ/∂ŷ · ∂ŷ/∂w = 2(w·x − y) · x &nbsp;&nbsp; w ← w − η·∂ℒ/∂w")}
      <div class="tr-two"><div>${slider("gd.lr", "learning rate η", 0.005, 0.3, 0.005, s.lr, "Try 0.26 and above: the steps overshoot and the loss explodes. That is what a NaN loss in training is.")}
        <div class="tr-calc">x = ${s.x}, y = ${s.y}, w = <b>${f(s.w)}</b><br>prediction ŷ = ${f(s.w)}×${s.x} = ${f(pred)}<br>ℒ = (${f(pred)} − ${s.y})² = <b>${f(loss)}</b><br>∂ℒ/∂w = 2×(${f(pred - s.y)})×${s.x} = <b>${f(g)}</b><br>next w = ${f(s.w)} − ${s.lr}×${f(g)} = <b>${f(s.w - s.lr * g)}</b></div>
        <div class="row wrap"><button class="btn primary small" data-tr-act="gd-step">Take one step</button><button class="btn small" data-tr-act="gd-10">10 steps</button><button class="btn small" data-tr-act="gd-reset">Reset</button></div></div>
      <div><canvas class="tr-cv" data-tr-chart="gd"></canvas><p class="faint small">The curve is the loss for every w; the red dot is where you are; the dots are your steps.</p></div></div>`;
    }, draw: () => { const s = L.gd, pts = []; for (let w = -3; w <= 5; w += 0.05) pts.push([w, (w * s.x - s.y) ** 2]);
      chart($('[data-tr-chart="gd"]'), [{ pts, color: cssv("--accent", "#7c8cff") }, { pts: s.hist.map((w) => [w, (w * s.x - s.y) ** 2]), color: cssv("--warn", "#fb2"), dots: true, width: 1 }],
        { mark: [s.w, (s.w * s.x - s.y) ** 2], y0: 0, y1: 60, x0: -3, x1: 5, xlabel: "w" }); } },
    { id: "adam", title: "AdamW: the optimiser", body: () => {
      const s = L.adam, rows = adamDemo();
      return html`<p>Plain gradient descent uses one step size for every weight. <b>Adam</b> keeps two running averages per weight — of the gradient (m, its direction) and of the squared gradient (v, its typical size) — and divides one by the square root of the other, so every weight moves about η per step whatever the scale of its gradient. The <b>W</b> adds weight decay: every weight shrinks slightly each step, which keeps them small and helps the model generalise. Your trainer uses exactly this, with β₂ = 0.95.</p>
      ${eq("m ← β₁m + (1−β₁)g &nbsp;&nbsp; v ← β₂v + (1−β₂)g² &nbsp;&nbsp; m̂ = m⁄(1−β₁<sup>t</sup>) &nbsp; v̂ = v⁄(1−β₂<sup>t</sup>)")}
      ${eq("w ← w − η·( m̂ ⁄ (√v̂ + ε) + λ·w )")}
      <div class="tr-two"><div>${slider("adam.lr", "learning rate η", 0.001, 0.1, 0.001, s.lr)}${slider("adam.b1", "β₁", 0, 0.99, 0.01, s.b1)}${slider("adam.b2", "β₂", 0.5, 0.999, 0.001, s.b2)}${slider("adam.wd", "weight decay λ", 0, 0.5, 0.01, s.wd)}${slider("adam.noise", "how noisy the gradients are", 0, 2, 0.05, s.noise)}</div>
      <div><p class="faint small">Minimising (w − 2)² with noisy gradients, starting at w = ${s.w0}:</p>
      <div class="tr-scroll"><table class="tr-key"><tr><th>t</th><th>g</th><th>m</th><th>v</th><th>m̂</th><th>v̂</th><th>step</th><th>w</th></tr>
      ${rows.map((r) => html`<tr><td>${r.t}</td><td>${f(r.g, 3)}</td><td>${f(r.m, 3)}</td><td>${f(r.v, 3)}</td><td>${f(r.mh, 3)}</td><td>${f(r.vh, 3)}</td><td>${f(r.step, 3)}</td><td><b>${f(r.w, 4)}</b></td></tr>`)}</table></div></div></div>
      ${keyTable([["g", "this step's gradient for the weight (after clipping)"], ["m", "first moment: running average of g (momentum)", "β₁ = " + s.b1], ["v", "second moment: running average of g²", "β₂ = " + s.b2], ["m̂, v̂", "bias-corrected: m and v start at 0, so early on they're divided by (1 − β<sup>t</sup>) to undo that"], ["ε", "10⁻⁸: avoids dividing by zero"], ["λ", "weight decay", "yours: " + curSet().weight_decay], ["clip", "before all this the whole gradient is scaled down if its length (norm) is over 1.0, so one bad batch can't throw the model off"]])}
      <p class="muted small">AdamW stores m and v for every weight: that's why training needs 16 bytes per weight (weight, gradient, m, v at 4 bytes each) while just running the model needs 4.</p>`;
    } },
    { id: "sched", title: "The learning-rate schedule", body: () => {
      const s = L.sched;
      return html`<p>The learning rate isn't fixed. <b>Warm-up</b> raises it from 0 over the first steps, because AdamW's averages are still unreliable then and big early steps can wreck the random start. Then <b>cosine decay</b> lowers it smoothly to 10% of the peak, so late updates fine-tune instead of jumping about. If validation stops improving, your trainer also multiplies the peak by 0.7.</p>
      ${eq("η(t) = η<sub>peak</sub>·t ⁄ t<sub>warm</sub> &nbsp;(t ≤ t<sub>warm</sub>) &nbsp;&nbsp; η(t) = η<sub>peak</sub>·(0.1 + 0.9·½(1 + cos(π·progress)))")}
      <div class="tr-two"><div>${slider("sched.peak", "peak η", 0.0001, 0.003, 0.0001, s.peak)}${slider("sched.warmup", "warm-up steps", 0, 1000, 10, s.warmup)}${slider("sched.steps", "total steps", 200, 20000, 100, s.steps)}${slider("sched.at", "look at step t", 0, s.steps, 1, Math.min(s.at, s.steps))}
        <div class="tr-calc">${s.at <= s.warmup ? html`t ≤ warm-up: η = ${f(s.peak)} × ${s.at} ÷ ${s.warmup} = <b>${f(lrAt(s.at, s.steps, s.peak, s.warmup))}</b>`
          : html`progress = (${s.at} − ${s.warmup}) ÷ (${s.steps} − ${s.warmup}) = ${f((s.at - s.warmup) / Math.max(1, s.steps - s.warmup), 3)}<br>η = ${f(s.peak)} × (0.1 + 0.9 × ½ × (1 + cos(π × ${f((s.at - s.warmup) / Math.max(1, s.steps - s.warmup), 3)}))) = <b>${f(lrAt(s.at, s.steps, s.peak, s.warmup))}</b>`}</div>
        <button class="btn small" data-tr-act="sched-mine">Use my training settings</button></div>
      <div><canvas class="tr-cv" data-tr-chart="sched"></canvas></div></div>`;
    }, draw: () => { const s = L.sched, pts = []; for (let t = 0; t <= s.steps; t += Math.max(1, Math.round(s.steps / 300))) pts.push([t, lrAt(t, s.steps, s.peak, s.warmup)]);
      chart($('[data-tr-chart="sched"]'), [{ pts, color: cssv("--accent2", "#2de") }], { mark: [s.at, lrAt(s.at, s.steps, s.peak, s.warmup)], y0: 0, xlabel: "step" }); } },
    { id: "size", title: "Counting weights, memory and time", body: () => {
      const s = L.size, d = s.d, N = params(s.V, s.T, d, s.L), act = actBytes(s.B, s.T, d, s.L, s.h, s.V), fl = 6 * N * s.B * s.T, sp = (TR.ov && TR.ov.recommend.speed) || 1e10;
      return html`<p>Every number on this page comes from these formulas. Change the shape and watch what it costs.</p>
      <div class="tr-two"><div>${slider("size.V", "vocabulary V", 256, 32768, 256, s.V)}${slider("size.T", "context T", 16, 1024, 16, s.T)}${slider("size.d", "width d", 32, 1024, 32, s.d)}${slider("size.L", "layers L", 1, 24, 1, s.L)}${slider("size.h", "heads h", 1, 16, 1, s.h)}${slider("size.B", "batch B", 1, 128, 1, s.B)}
        <button class="btn small" data-tr-act="size-mine">Use my training settings</button></div>
      <div class="tr-calc">token embedding V·d = ${n0(s.V * d)}<br>position embedding T·d = ${n0(s.T * d)}<br>blocks L·(12d² + 13d) = ${s.L}×${n0(12 * d * d + 13 * d)} = ${n0(s.L * (12 * d * d + 13 * d))}<br>final norm 2d = ${n0(2 * d)}<br>output layer d·V + V = ${n0(d * s.V + s.V)}<br>
        <b>N = ${n0(N)} weights</b> (${f(100 * 2 * s.V * d / N, 3)}% are the two vocabulary tables)<hr>
        weights + gradients + AdamW m, v: 16 × N = ${gb(16 * N)}<br>activations kept for backward: 4·B·T·(L·(34d + 5hT) + 3V) = ${gb(act)}<br><b>training memory ≈ ${gb(16 * N + act)}</b> · just running it: ${gb(4 * N)}<hr>
        work per step: 6·N·B·T = ${f(fl, 3)} FLOPs<br>at this machine's ~${f(sp, 2)} FLOP/s: <b>${dur(fl / sp)} per step</b>, ${dur(fl / sp * 1000)} per 1,000 steps<br>${d % s.h ? html`<span class="bad-t">d must divide by h: ${d} ÷ ${s.h} isn't whole</span>` : html`head size d/h = ${d / s.h}`}</div></div>
      ${eq("N = V·d + T·d + L·(12d² + 13d) + 2d + d·V + V &nbsp;&nbsp; FLOPs/step ≈ 6·N·B·T")}
      <p class="muted small">Why 6? Each weight does one multiply and one add per token going forward (2), and the backward pass needs the gradient for both the weight and its input (4).</p>`;
    } },
    { id: "samp", title: "Generating text: sampling", body: () => {
      const s = L.samp, base = [3.0, 2.4, 1.9, 1.2, 0.6, 0.1, -0.5, -1.2], names = [" the", " a", " my", " this", " one", " cheese", " purple", " zebra"];
      let z = base.map((x) => x / Math.max(0.01, s.temp)); const order = z.map((x, i) => [x, i]).sort((a, b) => b[0] - a[0]);
      if (s.k > 0) { const cut = order[Math.min(s.k, order.length) - 1][0]; z = z.map((x) => (x >= cut ? x : -Infinity)); }
      let p = softmax(z);
      if (s.p < 1) { const so = p.map((x, i) => [x, i]).sort((a, b) => b[0] - a[0]); let c = 0; const keep = new Set(); so.forEach(([x, i]) => { if (c < s.p) keep.add(i); c += x; }); p = p.map((x, i) => (keep.has(i) ? x : 0)); const t = p.reduce((a, b) => a + b, 0); p = p.map((x) => x / t); }
      return html`<p>To write, the model predicts the next token's probabilities, one token is <b>drawn at random</b> from them, added to the text, and the whole thing repeats. Three knobs reshape the probabilities first:</p>
      ${eq("p<sub>i</sub> = softmax(z<sub>i</sub> ⁄ temperature) &nbsp; then keep the top-k, then the smallest set holding top-p of the probability")}
      <div class="tr-two"><div>${slider("samp.temp", "temperature", 0.05, 2.5, 0.05, s.temp, "Low: safe and repetitive. High: creative, then nonsense.")}${slider("samp.k", "top-k (0 = off)", 0, 8, 1, s.k)}${slider("samp.p", "top-p", 0.05, 1, 0.05, s.p)}</div>
      <div><p class="faint small">“I'd like to eat …” — the next word:</p>${names.map((nm, i) => html`<div class="tr-prow"><span>${nm.trim()}</span><span class="tr-pbar" style="width:${Math.round(p[i] * 220)}px"></span><b>${f(p[i] * 100, 3)}%</b></div>`)}</div></div>
      <p class="muted small">Your chat uses temperature 0.7, top-k 40, top-p 0.9 and a repetition penalty of 1.15 (logits of tokens used recently are divided by 1.15) by default.</p>`;
    } },
    { id: "multi", title: "Training on several machines", body: () => {
      const s = L.multi, sp = [s.a, s.b, s.c].filter((x) => x > 0).sort((x, y) => y - x), lead = sp[0] || 1, others = sp.slice(1);
      const N = s.N * 1e6, bytes = 4 * N * 2 * others.length, sync = bytes * 8 / (s.link * 1e6), eff = s.round / (s.round + sync);
      const share = sp.map((x) => x / sp.reduce((a, b) => a + b, 0)), combined = (lead + others.reduce((a, b) => a + b, 0)) * 1e9 * eff;
      const perStep = 4 * N * 2 * others.length * 8 / (s.link * 1e6);
      return html`<p>One model can't be split across your machines like a pizza: every training step needs every weight. What <i>can</i> be shared is the <b>reading</b>. Each machine trains its own copy on different random windows of your text for a round (about 2 minutes), then the copies are <b>averaged</b> and every machine carries on from the average. This is called <b>local SGD</b> (or federated averaging): the same idea as big labs' data-parallel training, but swapping weights every few hundred steps instead of every step, because a home network is far slower than the links between datacentre GPUs.</p>
      ${eq("w = Σ<sub>i</sub> a<sub>i</sub>·w<sub>i</sub> ⁄ Σ<sub>i</sub> a<sub>i</sub> &nbsp;&nbsp; a<sub>i</sub> = tokens machine i read this round = steps<sub>i</sub> · B · T")}
      ${keyTable([["w<sub>i</sub>", "machine i's copy of one weight after its round"], ["a<sub>i</sub>", "how much text it read: a faster machine does more steps in the same 2 minutes, so it counts for more"], ["w", "the averaged weight every machine starts the next round from"]])}
      <div class="tr-two"><div>${slider("multi.a", "machine A, GFLOP/s", 10, 3000, 10, s.a)}${slider("multi.b", "machine B, GFLOP/s", 0, 3000, 10, s.b)}${slider("multi.c", "machine C, GFLOP/s (0 = none)", 0, 3000, 10, s.c)}
        ${slider("multi.N", "model, millions of weights", 1, 200, 1, s.N)}${slider("multi.link", "network, Mbit/s", 10, 1000, 10, s.link)}${slider("multi.round", "round length, seconds", 10, 600, 10, s.round)}</div>
      <div class="tr-calc">steps each machine does per round ∝ its speed: ${sp.map((x, i) => "ABC"[i] + " " + f(share[i] * 100, 3) + "%").join(" · ")}<br>
        weights swapped per round: the lead fetches ${others.length} cop${others.length === 1 ? "y" : "ies"} and sends the average back: 2 × ${others.length} × 4 bytes × ${n0(N)} = ${gb(bytes)}<br>
        at ${s.link} Mbit/s: ${dur(sync)} of swapping per ${dur(s.round)} round → ${f(eff * 100, 3)}% of the time is training<br>
        <b>combined ≈ ${f(combined / 1e9, 4)} GFLOP/s</b> vs ${f(lead, 4)} for the fastest alone (${f(combined / 1e9 / lead, 3)}×)<br>
        swapping every step instead (classic data-parallel) would cost ${dur(perStep)} per step${perStep > 1 ? html` — <span class="bad-t">slower than the step itself</span>` : ""}</div></div>
      <p class="muted small">Two rules fall out of the numbers. A machine much slower than the fastest adds little (a Pi next to a GPU adds about 1%) but every round still waits to swap with it, so the Train tab leaves out machines under a quarter of the fastest's speed. And a bigger model means more bytes per swap, so the network matters more as the model grows. Before round 1 one machine trains alone for the warm-up, so every copy starts from the same weights (copies started from different random weights can't be averaged: their neurons wouldn't line up).</p>`;
    } },
    { id: "scale", title: "Why it isn't Qwen (yet)", body: () => {
      const ov = TR.ov, sp = ov ? ov.recommend.speed : 1e10, q = { N: 494e6, D: 18e12 }, qc = 6 * q.N * q.D;
      const r = ov ? ov.recommend : null;
      return html`<p>Small open models like Qwen use the same recipe you're running: tokens, embeddings, attention blocks, cross-entropy, AdamW, warm-up and cosine decay. The difference is <b>scale</b>, and training cost is simple arithmetic:</p>
      ${eq("compute C ≈ 6 · N · D &nbsp;&nbsp; (N weights, D training tokens)")}
      <div class="tr-calc">Qwen2.5-0.5B: N = 494 million weights, D ≈ 18 trillion tokens<br>C = 6 × 4.94·10⁸ × 1.8·10¹³ = <b>${f(qc, 3)} FLOPs</b><br>
        This machine trains at about ${f(sp, 2)} FLOP/s → <b>${dur(qc / sp)}</b> of non-stop training.<br>
        ${r ? html`Your recommended model: N = ${n0(r.parameters)}, ${n0(r.tokens)} tokens of data → C ≈ ${f(6 * r.parameters * r.settings.steps * r.settings.batch_size * r.settings.context, 3)} FLOPs, about ${dur(r.seconds)}.` : ""}</div>
      ${r ? html`<p><b>What to expect from your model:</b> ${r.expect.level}</p>` : ""}
      <h4>What makes a small model less stupid</h4>
      <ol class="tr-steps"><li><b>More and better text.</b> The single biggest lever. Clean, varied, well-written data beats clever settings. Remove junk (menus, code you don't want, repeated boilerplate).</li>
        <li><b>Conversation data for a chatbot.</b> A model only learns to answer if it sees thousands of User/Assistant examples. Tick “learn only the replies”.</li>
        <li><b>Train for the right length.</b> Watch validation loss: when it stops falling, stop (more steps only memorise).</li>
        <li><b>The fastest machine.</b> The Train tab picks it for you (a GPU is 10–100× faster than a Pi's CPU) and lets similar machines share the work.</li>
        <li><b>Size the model to the data.</b> About 20 tokens of text per weight is compute-optimal; with less, a smaller model generalises better.</li></ol>
      <p class="muted small">Honest scale: 10–30 million weights trained on a few hundred MB of good text gives readable paragraphs and simple, style-matched replies — a big step up, but not an assistant that knows the world. Everything that model knows came from your data.</p>`;
    } },
  ];

  function vis(t) { return String(t).replace(/ /g, "␣").replace(/\n/g, "⏎"); }
  function bpeDemo(text, merges) {
    // Same as bpe_tokenizer.py: split into word pieces first (a word keeps its leading space), merge only inside a piece.
    let words = (text.slice(0, 300).match(/ ?[A-Za-z]+| ?[0-9]+| ?[^\sA-Za-z0-9]+|\s+/g) || []).map((w) => Array.from(w)); const log = [];
    const count = () => words.reduce((s, w) => s + w.length, 0);
    for (let m = 0; m < merges; m++) {
      const c = new Map(); words.forEach((w) => { for (let i = 0; i + 1 < w.length; i++) { const k = w[i] + "\u0000" + w[i + 1]; c.set(k, (c.get(k) || 0) + 1); } });
      let best = null, bc = 1; c.forEach((v, k) => { if (v > bc || (v === bc && best !== null && k < best)) { best = k; bc = v; } });
      if (!best || bc < 2) break;
      const [a, b] = best.split("\u0000");
      words = words.map((w) => { const out = []; for (let i = 0; i < w.length; i++) { if (i + 1 < w.length && w[i] === a && w[i + 1] === b) { out.push(a + b); i++; } else out.push(w[i]); } return out; });
      log.push({ a, b, count: bc, left: count() });
    }
    return { tokens: words.flat(), log };
  }
  function attnDemo() {
    const r = rng(L.attn.seed), d = 4, X = mat(4, d, r, 1), Wq = mat(d, d, r, 0.7).map((row) => row.map((x) => x * L.attn.focus)), Wk = mat(d, d, r, 0.7), Wv = mat(d, d, r, 0.7);
    const Q = mul(X, Wq), K = mul(X, Wk), Vv = mul(X, Wv), scale = 1 / Math.sqrt(d);
    const scores = mul(Q, T_(K)).map((row, i) => row.map((x, j) => (j > i ? -Infinity : x * scale)));
    const weights = scores.map(softmax);
    return { scores, weights, out: mul(weights, Vv) };
  }
  function adamDemo() {
    const s = L.adam, r = rng(s.seed); let w = s.w0, m = 0, v = 0; const rows = [];
    for (let t = 1; t <= 12; t++) {
      const g = 2 * (w - 2) + randn(r) * s.noise;
      m = s.b1 * m + (1 - s.b1) * g; v = s.b2 * v + (1 - s.b2) * g * g;
      const mh = m / (1 - s.b1 ** t), vh = v / (1 - s.b2 ** t), step = s.lr * (mh / (Math.sqrt(vh) + s.eps) + s.wd * w);
      w -= step; rows.push({ t, g, m, v, mh, vh, step, w });
    }
    return rows;
  }

  // ---------- data ----------
  const DATA_SOURCES = [
    ["Your own writing", "Notes, documents, emails you wrote, your chat exports. The model will sound like you. Export as .txt."],
    ["Project Gutenberg (gutenberg.org)", "70,000+ public-domain books as plain text. Download “Plain Text UTF-8”, delete the licence header and footer. Great for fluent English."],
    ["Wikipedia", "dumps.wikimedia.org has every article. Use a tool such as wikiextractor to turn the XML dump into plain text; Simple English Wikipedia is a good small start (~200 MB)."],
    ["Open conversation datasets", "For a chatbot: search Hugging Face for chat datasets with a licence that allows your use (e.g. OpenAssistant oasst1, Apache-2.0). Download the JSON/JSONL file: the formats below are read directly."],
    ["Write Q&A yourself", "Even a few hundred hand-written User/Assistant pairs about your homelab teach the model your topic and the reply format."],
    ["Your cluster's own docs", "README files, man pages, nodeyard's docs: technical text about the things you care about."],
  ];
  const FORMATS = [
    ["Plain text (.txt, .md)", "Any text. The model learns to continue writing in its style.", "It was a bright cold day in April, and the clocks were striking thirteen.\nWinston Smith, his chin nuzzled into his breast…"],
    ["Conversations in text (.txt)", "Lines starting User: and Assistant:, a blank line between conversations.", "User: What is a k3s node?\nAssistant: A machine that has joined the cluster and runs pods.\n\nUser: How do I see my nodes?\nAssistant: Run kubectl get nodes."],
    ["Chat messages (.jsonl)", "One JSON object per line. The usual format for chat datasets.", '{"messages": [{"role": "user", "content": "Hi!"}, {"role": "assistant", "content": "Hello! How can I help?"}]}\n{"messages": [{"role": "user", "content": "What is 2+2?"}, {"role": "assistant", "content": "4."}]}'],
    ["Instructions (.jsonl)", "instruction (+ optional input) and output. Alpaca-style datasets.", '{"instruction": "Summarise this.", "input": "The cat sat on the mat all day.", "output": "A cat stayed on a mat."}'],
    ["Prompt/completion or Q&A (.jsonl)", "prompt + completion, or question + answer.", '{"prompt": "Capital of France?", "completion": "Paris."}\n{"question": "Boiling point of water?", "answer": "100 °C at sea level."}'],
    ["Other datasets (.jsonl / .json)", "ShareGPT-style conversations ({from, value}) and {\"text\": …} records also work; a .json file holding a list of these is fine too.", '{"conversations": [{"from": "human", "value": "Hello"}, {"from": "gpt", "value": "Hi there!"}]}\n{"text": "Any plain text record."}'],
  ];

  const Q_PRESETS = {
    starter: { label: "Chat basics (starter chat set)", prompts: [["Hello!", "hello, hi"], ["What is 12 plus 7?", "19"], ["Spell cat.", "C-A-T"], ["What day comes after Friday?", "Saturday"], ["What is the opposite of hot?", "cold"]] },
    shakespeare: { label: "Sounds like Shakespeare", prompts: [["What news, my lord?", "lord, thou, thee, thy, sir"], ["Wilt thou come with me?", "thee, thou, ay, nay, will"], ["Good morrow, sweet lady.", "morrow, sir, lord, good"], ["Why dost thou weep?", "thou, my, I"]] },
    stories: { label: "Writes a story (TinyStories)", prompts: [["Once upon a time, there was a little", "she, he, they, was"], ["The dog wanted to", "the, and"]] },
    own: { label: "My own", prompts: [["", ""]] },
  };
  function qPrompts() { if (!TR.q.prompts.length) TR.q.prompts = Q_PRESETS[TR.q.preset || "starter"].prompts.map(([p, e]) => ({ prompt: p, expect: e })); return TR.q.prompts; }
  function saveQ() { store.set("train.quality", JSON.stringify(TR.q)); }
  function recSettings(ov) { return Object.assign({ save_every: 500, device: "auto", assistant_only: false, dropout: 0 }, (ov || TR.ov).recommend.settings); }
  function markEdited() { TR.edited = Object.assign({}, TR.form); }
  function resetSettings() { TR.edited = null; TR.preset = ""; store.set("train.preset", ""); TR.form = recSettings(); paint(); toast("Settings configured for this hardware and the training data you ticked."); }
  function loadPreset(name) {
    const p = (TR.ov.presets || []).find((x) => x.name === name); if (!p) return resetSettings();
    TR.preset = name; store.set("train.preset", name); TR.edited = Object.assign({}, p.settings); TR.form = Object.assign(recSettings(), p.settings); paint(); toast("Loaded “" + name + "”.");
  }
  function presetChanged() {
    const p = TR.preset && (TR.ov.presets || []).find((x) => x.name === TR.preset); if (!p) return false;
    return Object.keys(p.settings).some((k) => String(p.settings[k]) !== String(TR.form[k]));
  }
  function curSet() { return TR.form || (TR.ov && TR.ov.recommend.settings) || { vocab_size: 2048, context: 128, dim: 192, layers: 4, heads: 6, batch_size: 32, steps: 2000, learning_rate: 0.001, warmup: 100, weight_decay: 0.1, eval_every: 100 }; }

  // ---------- loading ----------
  async function loadOverview() {
    try {
      const q = "?hours=" + TR.hours + "&data=" + encodeURIComponent(TR.sel.join("|")) + "&machines=" + encodeURIComponent(TR.machines.join("|"));
      const r = await getJSON("/api/train" + q);
      if (!r.ok) { toast(r.error || "Couldn't read the trainer."); return; }
      TR.ov = r;
      const names = new Set(r.datasets.map((d) => d.name)); TR.sel = TR.sel.filter((n) => names.has(n));
      // Settings follow the recommendation for this hardware and data until you edit one or load saved settings.
      const saved = TR.preset && (r.presets || []).find((p) => p.name === TR.preset);
      if (TR.preset && !saved) { TR.preset = ""; store.set("train.preset", ""); }
      if (!TR.edited && saved) TR.edited = Object.assign({}, saved.settings);
      if (!TR.form || !TR.edited) TR.form = Object.assign(recSettings(r), TR.edited || {});
      if (!TR.chat.model && r.models.length) TR.chat.model = r.models[r.models.length - 1].name;
      if (!TR.exModel && r.models.length) TR.exModel = r.models[r.models.length - 1].name;
      const mnames = new Set(r.machines.map((m) => m.name)); TR.machines = TR.machines.filter((n) => mnames.has(n));
      // While a machine is starting or installing PyTorch, keep reading its progress.
      clearTimeout(TR.ovPollT);
      const fetching = (r.catalog || []).some((d) => d.job && d.job.state === "downloading");
      if ((fetching || r.machines.some((m) => /starting|installing|checking/.test(m.worker.state))) && S.view === "train") TR.ovPollT = setTimeout(loadOverview, fetching ? 1500 : 3000);
    } catch (e) { /* signed out */ }
    paint();
  }
  async function poll() {
    clearTimeout(TR.pollT);
    if (S.view !== "train") return;
    try {
      const r = await getJSON("/api/train/status?since=" + TR.since);
      if (r.ok) {
        const was = TR.status && TR.status.running;
        TR.status = r; TR.lines = TR.lines.concat(r.lines).slice(-1500); TR.since = r.next;
        if (was && !r.running) { toast("Training finished."); loadOverview(); }
        paintLive();
      }
    } catch (e) { /* offline */ }
    TR.pollT = setTimeout(poll, TR.status && TR.status.running ? 1500 : 6000);
  }
  async function post(url, body) {
    try { const r = await postJSON(url, body); if (!r.ok) { toast(r.error || "That didn't work."); return null; } return r; }
    catch (e) { toast(e.message || "That didn't work."); return null; }
  }

  // ---------- painting ----------
  function paint() {
    const root = $("#tr-root"); if (!root || S.view !== "train") return;
    const body = TR.tab === "learn" ? learnTab() : TR.tab === "data" ? dataTab() : TR.tab === "train" ? trainTab() : TR.tab === "inside" ? insideTab() : chatTab();
    setHTML(root, html`<div class="tr-tabs" role="tablist">${TABS.map(([id, label]) => html`<button role="tab" class="tr-tab ${TR.tab === id ? "on" : ""}" aria-selected="${TR.tab === id}" data-tr-tab="${id}">${label}</button>`)}</div>
      <div class="tr-body">${body}</div>`);
    drawAll();
  }
  function drawAll() {
    if (TR.tab === "learn") { const ls = LESSONS[TR.lesson]; if (ls.draw) ls.draw(); }
    if (TR.tab === "train") paintLive();
  }

  function learnTab() {
    const ls = LESSONS[TR.lesson];
    return html`<div class="tr-learn"><nav class="tr-lessons" aria-label="Lessons">${LESSONS.map((l, i) => html`<button class="${i === TR.lesson ? "on" : ""}" data-tr-lesson="${i}"><span>${i + 1}</span>${l.title}</button>`)}
        <button class="${TR.lesson === -1 ? "on" : ""}" data-tr-act="glossary"><span>∑</span>Every variable</button></nav>
      <section class="card tr-lesson" id="tr-lesson"><h2 class="tr-h">${TR.lesson + 1}. ${ls.title}</h2>${ls.body()}
        <div class="row tr-nav">${TR.lesson > 0 ? html`<button class="btn" data-tr-lesson="${TR.lesson - 1}">← ${LESSONS[TR.lesson - 1].title}</button>` : ""}<span class="spacer"></span>
          ${TR.lesson < LESSONS.length - 1 ? html`<button class="btn primary" data-tr-lesson="${TR.lesson + 1}">${LESSONS[TR.lesson + 1].title} →</button>` : html`<button class="btn primary" data-tr-tab="data">Next: your data →</button>`}</div></section></div>
      ${glossary()}`;
  }
  function glossary() {
    const s = curSet(), N = params(s.vocab_size, s.context, s.dim, s.layers), r = TR.ov && TR.ov.recommend;
    const last = TR.status && TR.status.last, val = TR.status && TR.status.val.length ? TR.status.val[TR.status.val.length - 1].loss : null;
    return html`<section class="card" id="tr-glossary"><h2 class="tr-h">Every variable, with your values</h2>
      <p class="muted small">The settings column follows the Train tab; the live column fills in while a model trains.</p>
      ${keyTable([
        ["V", "vocabulary size: how many different tokens", n0(s.vocab_size)], ["T", "context length: tokens the model sees at once", s.context], ["d", "width: numbers describing each token", s.dim],
        ["L", "layers (transformer blocks)", s.layers], ["h", "attention heads", s.heads], ["d<sub>h</sub> = d/h", "size of each head", f(s.dim / s.heads)], ["4d", "feed-forward hidden size", 4 * s.dim],
        ["N", "total weights: V·d + T·d + L·(12d² + 13d) + 2d + d·V + V", n0(N)], ["B", "batch size: text windows per step", s.batch_size], ["B·T", "tokens per step", n0(s.batch_size * s.context)],
        ["η", "peak learning rate", s.learning_rate], ["t<sub>warm</sub>", "warm-up steps", s.warmup], ["steps", "updates to make", n0(s.steps)], ["β₁, β₂", "AdamW moment decay rates", "0.9, 0.95"], ["ε", "AdamW's divide-by-zero guard", "10⁻⁸"],
        ["λ", "weight decay", s.weight_decay], ["p<sub>drop</sub>", "dropout: share of values zeroed at random while training", s.dropout || 0], ["clip", "maximum gradient norm", "1.0"], ["ε<sub>LN</sub>", "layer norm's guard", "10⁻⁵"], ["16·N", "training memory for weights, gradients, m and v", gb(16 * N)],
        ["6·N·B·T", "FLOPs per step", f(6 * N * s.batch_size * s.context, 3)], ["ln V", "loss of a random guess (where training starts)", f(Math.log(s.vocab_size), 4)],
        ["tokens", "your data, in tokens (estimate)", r ? n0(r.tokens) : "–"],
        ["ℒ<sub>train</sub>", "latest training loss", last ? f(last.loss, 4) : "–"], ["ℒ<sub>val</sub>", "latest validation loss (on held-out 5% of your text)", f(val, 4)],
        ["e<sup>ℒ</sup>", "perplexity: how many tokens it's choosing between", val != null ? f(Math.exp(val), 4) : "–"], ["η(t)", "the learning rate right now", last ? f(last.lr, 3) : "–"],
        ["tokens/s", "training speed right now", last ? n0(last.tps) : "–"],
      ])}</section>`;
  }

  function dataTab() {
    const ov = TR.ov, c = TR.check;
    return html`<div class="grid tr-grid2">
      ${readyMade()}
      <section class="card"><h2 class="tr-h">Where to get training data</h2>
        <ul class="tr-list">${DATA_SOURCES.map(([a, b]) => html`<li><b>${a}</b><span>${b}</span></li>`)}</ul>
        <p class="muted small">Only train on text you have the right to use. More is better: under 1 MB the model mostly memorises; 10–500 MB is a good range for this hardware. Clean it: remove headers, navigation text and duplicates.</p></section>
      <section class="card"><h2 class="tr-h">Add your data</h2>
        <label class="tr-drop"><input type="file" multiple accept=".txt,.md,.jsonl,.json" data-tr-act="files"><b>Choose files</b> or drop them here<span class="faint small">.txt, .md, .jsonl or .json — any size, uploaded in pieces</span></label>
        ${TR.upload ? html`<div class="tr-up"><span>${TR.upload.name}</span><span class="tr-progress"><i style="width:${Math.round(TR.upload.frac * 100)}%"></i></span><span>${Math.round(TR.upload.frac * 100)}%</span></div>` : ""}
        <details class="tr-paste"><summary>…or paste text</summary>
          <input class="input" id="tr-paste-name" placeholder="name, e.g. my-notes.txt" value="pasted.txt">
          <textarea class="input" id="tr-paste-text" rows="6" placeholder="Paste text, User:/Assistant: conversations, or JSONL lines"></textarea>
          <button class="btn" data-tr-act="paste">Save as training data</button></details>
        <h3 class="tr-h3">Your files <span class="faint small">tick the ones to train on</span></h3>
        ${!ov ? html`<div class="empty small"><div class="spin"></div></div>` : !ov.datasets.length ? html`<div class="empty small">No training data yet.</div>`
          : html`<div class="tr-files">${ov.datasets.map((d) => html`<div class="tr-file ${c && c.name === d.name ? "on" : ""}"><label class="row"><input type="checkbox" data-tr-sel="${d.name}" ${TR.sel.includes(d.name) ? "checked" : ""}> <b>${d.name}</b></label>
            <span class="faint small">${gb(d.size)}</span><span class="spacer"></span><button class="btn small" data-tr-check="${d.name}">Check</button><button class="btn small danger" data-tr-del="${d.name}">Delete</button></div>`)}</div>`}
        ${TR.sel.length ? html`<button class="btn primary mt" data-tr-tab="train">Train on ${TR.sel.length} file${TR.sel.length > 1 ? "s" : ""} →</button>` : ""}</section>
      ${c ? html`<section class="card tr-span2"><h2 class="tr-h">Check: ${c.name}</h2>
        <div class="tr-stats">${[["Format", { text: "plain text", chat: "conversations in text", jsonl: "JSONL", json: "JSON list" }[c.format] || c.format], ["Records", n0(c.records)], ["Skipped", n0(c.skipped)], ["Characters", n0(c.chars)], ["Words", n0(c.words)], ["Tokens (est.)", n0(c.tokens_est)]].map(([a, b]) => html`<div><span>${a}</span><b>${b}</b></div>`)}</div>
        ${c.advice.map(([lvl, t]) => html`<div class="tr-adv ${lvl}">${t}</div>`)}
        ${c.problems.length ? html`<details><summary>${c.problems.length} problem${c.problems.length > 1 ? "s" : ""}</summary><ul class="small">${c.problems.map((p) => html`<li>${p}</li>`)}</ul></details>` : ""}
        ${c.examples && c.examples.length ? html`<h3 class="tr-h3">How each record is converted</h3>${c.examples.map((x) => html`<div class="tr-conv"><pre class="tr-pre">${x.raw}</pre><span class="tr-arrow">→</span>${x.text == null ? html`<div class="tr-adv warn">Skipped: no field it knows (see “How to format it”).</div>` : html`<pre class="tr-pre">${x.text}</pre>`}</div>`)}` : ""}
        <h3 class="tr-h3">What the model will read <span class="faint small">first 2,500 characters, each part labelled</span></h3>
        ${legend(c.chat)}<pre class="tr-pre tr-anno">${annotate(c.preview, c.chat && TR.form && TR.form.assistant_only)}</pre>
        <div class="row wrap mt">${/\.train\.txt$/.test(c.name) ? "" : html`<button class="btn small" data-tr-act="convert">Save it as ${c.name.replace(/\.(txt|md|jsonl|json)$/i, "")}.train.txt</button><span class="faint small">a copy in exactly the format above: open it, edit it, train on it</span>`}</div></section>` : ""}
      ${guide()}
      ${multiparty()}
      ${anatomy()}
      <section class="card tr-span2"><h2 class="tr-h">How to format it</h2>
        <p class="muted small">The format is detected from what's inside the file. Everything chat-shaped becomes <code>User: …</code> / <code>Assistant: …</code> turns, which is what the Chat tab sends the model.</p>
        <div class="tr-formats">${FORMATS.map(([a, b, ex]) => html`<div class="tr-format"><b>${a}</b><span class="muted small">${b}</span><pre class="tr-pre">${ex}</pre></div>`)}</div></section></div>`;
  }

  function readyMade() {
    const cat = (TR.ov && TR.ov.catalog) || [];
    return html`<section class="card tr-span2"><h2 class="tr-h">Ready-made datasets <span class="right faint small">one click: added to your files below</span></h2>
      <div class="tr-cat">${cat.map((d) => { const j = d.job, busy = j && j.state === "downloading";
        return html`<div class="tr-catitem"><div class="row"><b>${d.name}</b><span class="spacer"></span><span class="faint small">${gb(d.size)} · ${d.format}</span></div>
          <p class="small">${d.what}</p><p class="small"><b>Good for:</b> ${d.good_for}</p>
          <p class="faint small">Source: ${d.source} · Licence: ${d.licence}</p>
          ${busy ? html`<div class="tr-up"><span>${d.id === "starter-chat" ? "Writing…" : "Downloading…"}</span><span class="tr-progress"><i style="width:${Math.round(100 * Math.min(1, j.got / Math.max(1, j.total)))}%"></i></span><span>${gb(j.got)}</span></div>`
            : j && j.state === "failed" ? html`<div class="tr-adv bad">${j.error}</div>` : ""}
          <div class="row">${d.have ? html`<span class="good-t small">✓ ${d.file} is in your files</span><button class="btn small" data-tr-check="${d.file}">Check</button>${TR.sel.includes(d.file) ? "" : html`<button class="btn small" data-tr-use="${d.file}">Train on it</button>`}`
            : html`<button class="btn small primary" data-tr-get="${d.id}" ${busy ? "disabled" : ""}>${d.id === "starter-chat" ? "Add it" : "Download"}</button>`}</div></div>`; })}</div>
      <p class="faint small">Mixing is fine: tick several files and they're joined with a blank line between them. A good first run: the starter chat set alone (minutes). A better model: TinyStories + the starter chat set.</p></section>`;
  }
  function guide() {
    const steps = [
      ["Decide what the model should do", "Continue text in a style (stories, your notes, a book) → plain text. Answer like a chatbot → conversations. A model learns only the shape of what it sees: no conversations, no answering."],
      ["Collect text you're allowed to use", "Your own writing, public-domain books, datasets whose licence allows training (the ready-made ones above say theirs). Keep the source and licence next to it."],
      ["Clean it", "Remove what you don't want repeated: menus, cookie notices, page numbers, headers and footers, broken characters, duplicate lines or records. Every character is something the model will try to imitate."],
      ["Put it in one of the formats below", "Plain text needs nothing. For conversations write “User:” and “Assistant:” lines with a blank line between conversations, or one JSON object per line (.jsonl). Most downloaded datasets are already in a format this page reads."],
      ["Keep each example short", "The model sees T tokens at a time (the context, often 128–256 tokens ≈ 450–900 characters) from random places in the text. A conversation longer than that is only ever seen in pieces, so keep each one to a few exchanges."],
      ["Make it big enough", "Rule of thumb: about 20 tokens of text per weight (≈ 70 characters per weight). 1 MB ≈ 280,000 tokens trains a ~15,000-weight model properly; 100 MB ≈ 28 million tokens suits ~1.4 million weights. Less text than that, and a smaller model does better."],
      ["Check it here", "Upload it and press Check: you see the format found, records skipped and why, and exactly what the model will read, labelled."],
      ["Train, then read the validation loss", "If validation stops falling while training loss keeps falling, the model is memorising: add more (or more varied) text rather than more steps."],
    ];
    return html`<section class="card tr-span2"><h2 class="tr-h">Make your own dataset, step by step</h2><ol class="tr-steps">${steps.map(([a, b]) => html`<li><b>${a}.</b> ${b}</li>`)}</ol></section>`;
  }
  function multiparty() {
    const ov = TR.ov, mp = TR.mp, files = ov ? ov.datasets.filter((d) => /\.(txt|md)$/i.test(d.name)) : [];
    return html`<section class="card tr-span2"><h2 class="tr-h">Conversations with more than two people</h2>
      <p class="small">Group chats, plays, Discord or forum threads have many speakers, but the model plays one role: <b>Assistant</b>. So pick <b>one voice for it to learn</b>, and turn everyone else's lines into what it replies to:</p>
      <div class="tr-conv"><pre class="tr-pre">Sam: anyone up?
Alex: yep
Jo: me too, what's up
Sam: game tonight?</pre><span class="tr-arrow">→</span><pre class="tr-pre">User: Sam: anyone up?
Alex: yep
Jo: me too, what's up
Assistant: game tonight?</pre></div>
      <ol class="tr-steps small">
        <li><b>Choose the speaker</b> whose voice you want: one example per thing they said. (A play: one character; your chats: you.) Several speakers make several datasets; train on one, or on all of them for a model that can be anyone.</li>
        <li><b>Context = the few lines before it, with names</b> (“Alex: …”), so the model knows who said what and that it's answering a group. 2–4 lines is plenty: more is cut off by the context window anyway.</li>
        <li><b>Merge one person's consecutive lines</b> into one turn: chats split a thought over several messages.</li>
        <li><b>Clean it:</b> drop system lines (“joined the group”, “&lt;Media omitted&gt;”), stage directions, timestamps; remove or replace phone numbers, addresses and anything private. Only use chats where the people involved would be fine with it.</li>
        <li><b>Don't pair every line with the next one blindly.</b> The next line in a play or group chat is often not an answer to the line before it, and each line then appears twice (once as a question, once as a reply), which doubles the text without adding anything.${ov && ov.datasets.some((d) => /pairs/i.test(d.name)) ? " Your “pairs” file is built that way." : ""} For style rather than chat, plain text with the names left in (“ROMEO: …”) is often better: the model learns to write whole scenes.</li></ol>
      <h3 class="tr-h3">Do it for you</h3>
      ${!files.length ? html`<p class="faint small">Add a .txt file with lines like “NAME: what they said” (a script, or a WhatsApp/Signal export) and it appears here.</p>` : html`
      <div class="row wrap"><select class="select" data-tr-act="mp-file"><option value="">Choose a script or chat file…</option>${raw(files.map((d) => '<option' + (mp.file === d.name ? " selected" : "") + ' value="' + esc(d.name) + '">' + esc(d.name) + "</option>").join(""))}</select>
        ${mp.speakers ? (mp.speakers.length ? html`<select class="select" data-tr-act="mp-speaker">${raw(mp.speakers.map((x) => '<option' + (mp.speaker === x.name ? " selected" : "") + ' value="' + esc(x.name) + '">' + esc(x.name) + " (" + x.turns + " turns)</option>").join(""))}</select>
          <label class="tr-field inline"><span>context lines</span><input class="input" type="number" min="1" max="8" value="${mp.context}" data-tr-act="mp-context" style="width:70px"></label>
          <button class="btn primary small" data-tr-act="mp-make">Make chat pairs</button>` : html`<span class="warn-t small">No “NAME: text” lines found in it.</span>`) : ""}</div>`}</section>`;
  }
  function anatomy() {
    const parts = [
      ["ra-mark", "User:", "Role marker", "Starts a person's turn. The trainer finds turns by it: with “learn only the replies” on, every token from here to the next “Assistant:” gets weight 0 in the loss (read, but never graded). The Chat tab writes it in front of your message."],
      ["ra-user", " What is a node?", "The person's message", "Context: the model reads it to predict the reply. Without “learn only the replies” the model also learns to write questions like it."],
      ["ra-sep", "⏎", "New line", "Ends the line. It's a token too: the model learns where lines end."],
      ["ra-mark", "Assistant:", "Role marker", "Starts the reply. The Chat tab ends its prompt with “Assistant:”, so after training, the most likely continuation is a reply. From here the loss weight is 1."],
      ["ra-asst", " A machine in the cluster that runs pods.", "The reply", "What the model learns to write. Every token is a prediction graded by cross-entropy: −ln(probability it gave the real next token)."],
      ["ra-sep", "⏎⏎", "Blank line", "Ends the conversation. Replies stop when the model writes a blank line or a new “User:”, so ending every reply this way teaches it when to stop."],
    ];
    return html`<section class="card tr-span2"><h2 class="tr-h">Anatomy of one training example</h2>
      <pre class="tr-pre tr-anno big">${parts.map(([cls, t]) => raw('<span class="' + cls + '">' + esc(t) + "</span>" + (t === "⏎" ? "\n" : "")))}</pre>
      <div class="tr-scroll">${keyTable(parts.map(([cls, t, name, what]) => ['<span class="' + cls + '">' + esc(t.trim() || t) + "</span>", "<b>" + esc(name) + "</b> — " + esc(what)]))}</div>
      <p class="muted small">Everything above becomes tokens: the tokenizer learns “User”, “:”, “Assistant” and the newlines as pieces like any word, and the model learns what tends to follow each. Plain text has no markers: every token is graded, and the model learns to continue it. JSON files are converted to this layout before training (see “How each record is converted” when you check one).</p></section>`;
  }
  function legend(chat) {
    return chat ? html`<p class="small tr-legend"><span class="ra-mark">role marker</span> <span class="ra-user">person's turn${TR.form && TR.form.assistant_only ? " (read, not graded)" : ""}</span> <span class="ra-asst">reply (learned)</span> <span class="ra-sep">⏎⏎ end of conversation</span></p>`
      : html`<p class="small tr-legend"><span class="ra-asst">text (every token learned)</span> <span class="ra-sep">⏎⏎ blank line</span></p>`;
  }
  function annotate(text, userMuted) {
    let role = "asst", out = "";
    const span = (cls, t) => '<span class="' + cls + '">' + esc(t) + "</span>";
    for (const line of String(text).split("\n")) {
      const m = /^(\s*)(User|Assistant|System):/.exec(line);
      if (m) { role = m[2] === "Assistant" ? "asst" : "user"; out += span("ra-mark", m[1] + m[2] + ":") + span(role === "user" ? "ra-user" : "ra-asst", line.slice(m[0].length)); }
      else if (!line.trim()) { out += span("ra-sep", "⏎"); role = "asst"; }
      else out += span(role === "user" ? "ra-user" : "ra-asst", line);
      out += "\n";
    }
    return raw(out);
  }
  function trainTab() {
    const ov = TR.ov; if (!ov) return html`<div class="empty"><div class="spin"></div>Reading this machine's hardware…</div>`;
    const hw = ov.hardware, r = ov.recommend, s = TR.form, run = TR.status ? TR.status.running : ov.running;
    const N = params(+s.vocab_size, +s.context, +s.dim, +s.layers), act = actBytes(+s.batch_size, +s.context, +s.dim, +s.layers, +s.heads, +s.vocab_size), fl = 6 * N * s.batch_size * s.context;
    const field = (k, label, help, attrs = "") => html`<label class="tr-field"><span>${raw(label)}</span><input class="input" data-tr-set="${k}" value="${s[k]}" ${raw(attrs)}><span class="faint small">${raw(help)}</span></label>`;
    return html`<div class="grid tr-grid2">
      ${machinesCard()}
      <section class="card"><h2 class="tr-h">Recommended for ${ov.plan.share.length > 1 ? "these machines" : ov.plan.lead || "this hardware"}</h2>
        <label class="tr-field"><span>How long can it train?</span><select class="select" data-tr-act="hours">${raw([[0.25, "15 minutes"], [1, "1 hour"], [3, "3 hours"], [8, "overnight (8 h)"], [24, "a day"], [72, "3 days"]].map(([v, l]) => '<option value="' + v + '"' + (+v === +TR.hours ? " selected" : "") + ">" + l + "</option>").join(""))}</select></label>
        <p class="small">${TR.sel.length ? html`For <b>${TR.sel.join(", ")}</b>:` : html`<span class="warn-t">No data chosen yet</span> — <a href="#" data-tr-tab="data">choose it in step 2</a>.`} a <b>${n0(r.parameters)}</b>-weight model, about <b>${dur(r.seconds)}</b>, using ~${gb(r.memory)}. Limited by <b>${r.limit}</b>.</p>
        ${bigger(r)}
        <div class="tr-why">${r.why.map((w) => html`<details><summary><b>${w.name}</b><span>${w.value}</span></summary><div class="tr-eq small">${w.formula}</div><div class="tr-calc small">${w.numbers}</div><p class="muted small">${w.explain}</p></details>`)}</div>
        <p class="small"><b>Expect:</b> ${r.expect.level}</p>
        ${TR.edited ? html`<button class="btn" data-tr-act="use-rec">Use these settings</button>` : html`<p class="faint small">These are in the settings below.</p>`}</section>
      <section class="card tr-span2"><h2 class="tr-h">Settings</h2>
        <div class="row wrap tr-presets">
          <button class="btn small primary" data-tr-act="reset-settings">Config for hardware and training dataset</button>
          <button class="btn small" data-tr-act="save-settings">Save settings preset</button>
          <label class="tr-field inline"><span>Saved presets</span><select class="select" data-tr-act="preset">
            <option value="">${(ov.presets || []).length ? "— choose a preset —" : "none saved yet"}</option>${raw((ov.presets || []).map((p) => '<option value="' + esc(p.name) + '"' + (TR.preset === p.name ? " selected" : "") + ">" + esc(p.name) + "</option>").join(""))}</select></label>
          ${TR.preset ? html`<button class="btn small danger" data-tr-act="delete-preset">Delete preset</button>` : ""}
          <span class="spacer"></span>
          <span class="small ${TR.edited && (!TR.preset || presetChanged()) ? "warn-t" : "faint"}">${TR.preset ? (presetChanged() ? "“" + TR.preset + "”, changed since saved" : "using “" + TR.preset + "”") : TR.edited ? "edited, not saved" : "configured for this hardware and data"}</span></div>
        <p class="faint small">${TR.edited ? "Recommended would be: " + n0(params(r.settings.vocab_size, r.settings.context, r.settings.dim, r.settings.layers)) + " weights (V " + n0(r.settings.vocab_size) + ", T " + r.settings.context + ", d " + r.settings.dim + ", L " + r.settings.layers + "), " + n0(r.settings.steps) + " steps." : "Worked out from this machine's hardware and the data you ticked (see “Recommended” above for every reason). Change anything; Save keeps it for next time."}</p>
        <div class="tr-form">
          ${field("vocab_size", "Vocabulary V", "tokens the BPE tokenizer learns (≥ 256)")}${field("context", "Context T", "tokens it sees at once")}${field("dim", "Width d", "numbers per token; must divide by heads")}
          ${field("layers", "Layers L", "transformer blocks")}${field("heads", "Heads h", "parallel attention lookups")}${field("batch_size", "Batch B", "windows averaged per update")}
          ${field("steps", TR.q.on ? "Steps (maximum)" : "Steps", TR.q.on ? "the most it may take; it stops sooner when good enough" : "updates to make")}${field("learning_rate", "Learning rate η", "peak step size (1e-3 is a good start)")}${field("warmup", "Warm-up", "steps to ramp η up from 0")}
          ${field("weight_decay", "Weight decay λ", "AdamW's shrink per step")}${field("eval_every", "Check every", "steps between validation checks")}${field("dropout", "Dropout", "share of values zeroed while training (0–0.5); stops memorising small data")}${field("save_every", "Save every", "steps between checkpoints (0 = only at the end)")}
          <label class="tr-field"><span>Device</span><select class="select" data-tr-set="device">${raw(["auto", "cpu", "cuda", "mps"].map((d) => '<option' + (s.device === d ? " selected" : "") + ">" + d + "</option>").join(""))}</select><span class="faint small">auto: each machine uses its GPU when it has one</span></label>
          <label class="tr-field"><span>Learn only the replies</span><label class="row"><input type="checkbox" data-tr-set="assistant_only" ${s.assistant_only ? "checked" : ""}> on conversations, no loss on User: turns</label><span class="faint small">for chatbots: it learns to answer, not to ask</span></label></div>
        <div class="tr-calc small">N = ${n0(N)} weights · training memory ≈ 16·N + activations = ${gb(16 * N)} + ${gb(act)} = <b>${gb(16 * N + act)}</b> · ${f(fl, 3)} FLOPs per step · ≈ <b>${dur(fl * s.steps / r.speed)}</b> at the estimated speed${+s.dim % +s.heads ? html` · <span class="bad-t">width must divide by heads</span>` : ""}${16 * N + act > (hw.gpu && s.device !== "cpu" ? hw.gpu.vram : hw.ram_free) ? html` · <span class="bad-t">more than the free memory!</span>` : ""}</div>
        <div class="row wrap mt">
          <label class="tr-field inline"><span>Model</span><select class="select" data-tr-act="model-pick"><option value="">New model…</option>${raw(ov.models.map((m) => '<option value="' + esc(m.name) + '"' + (TR.resume && TR.model === m.name ? " selected" : "") + ">Continue " + esc(m.name) + "</option>").join(""))}</select></label>
          ${TR.resume ? "" : html`<input class="input" data-tr-act="model-name" placeholder="name (optional)" value="${TR.model}" style="width:180px">`}
          <span class="spacer"></span>
          ${run ? html`<button class="btn danger" data-tr-act="stop" ${TR.status && TR.status.job && TR.status.job.stop ? "disabled" : ""}>${TR.status && TR.status.job && TR.status.job.stop ? "Stopping…" : "Stop and save"}</button>` : html`<button class="btn primary" data-tr-act="start" ${ov.plan.lead && TR.sel.length ? "" : "disabled"}>${TR.resume ? "Continue training" : "Start training"}${ov.plan.lead ? " on " + (TR.together !== "fastest" && ov.plan.share.length > 1 ? ov.plan.share.length + " machines" + (TR.together === "race" ? " (race)" : "") : ov.plan.lead) : ""}</button>`}</div>
        ${TR.resume ? html`<p class="faint small">Continuing keeps the saved model's shape and tokenizer (V, T, d, L, h) and AdamW's running averages; the other settings apply. The learning rate schedule starts again, so a lower peak (about 30% of the first run's) stops the first steps from undoing what it already learned.</p>` : ""}</section>
      ${stopWhen()}
      <section class="card tr-span2"><h2 class="tr-h">Live <span class="right" id="tr-live-chip"></span></h2><div id="tr-live"></div>
        <div class="tr-charts"><div><h4>Loss (lower is better)</h4><canvas class="tr-cv tall" id="tr-loss"></canvas><p class="faint small"><span class="tr-sw a"></span>training (smoothed) <span class="tr-sw b"></span>validation <span class="tr-sw c"></span>random guess ln V</p></div>
          <div><h4>Learning rate η(t)</h4><canvas class="tr-cv" id="tr-lr"></canvas><h4>Gradient norm (before clipping)</h4><canvas class="tr-cv short" id="tr-grad"></canvas></div></div>
        <div id="tr-qlive"></div>
        <details class="mt"><summary>Training log</summary><pre class="tr-pre tr-log" id="tr-logbox"></pre></details></section>
      ${savedModels()}</div>`;
  }
  function bigger(r) {
    const b = r.bigger; if (!b) return "";
    const hrs = (h) => (h < 1 ? Math.round(h * 60) + " min" : h + " h");
    return html`<details class="tr-bigger" ${r.limit === "compute" ? "open" : ""}><summary class="small"><b>How to get a bigger model</b> ${r.limit === "compute" ? html`<span class="warn-t">— limited by compute: time × speed</span>` : r.limit === "data" ? html`<span class="faint">— limited by your text: add more</span>` : html`<span class="faint">— limited by memory</span>`}</summary>
      <p class="faint small">A model can only learn as much as the arithmetic it gets: weights ≈ √(speed × time ÷ 120). Past that, a bigger model would stay half-trained and do worse.</p>
      <div class="tr-scroll"><table class="tr-key"><tr><th>Train for</th><th>Model</th><th>Limited by</th></tr>${b.times.map((t) => html`<tr class="${+t.hours === +TR.hours ? "hl" : ""}"><td>${hrs(t.hours)}</td><td>${n0(t.parameters)} weights</td><td class="small faint">${t.limit}</td></tr>`)}</table></div>
      ${b.faster.map((m) => html`<div class="tr-adv ${m.usable ? "ok" : "warn"} small"><b>${m.name}</b>${m.gpu ? " (" + m.gpu + ")" : ""} is ~${f(m.speed / (r.speed || 1), 2)}× faster: in ${hrs(TR.hours)} it could train <b>${n0(m.parameters)} weights</b>${m.usable ? " — tick it under “Where it trains”." : html` — but it's ${m.reason}. Get it back online (check its kubelet, cable and power: <code>sudo k3s kubectl describe node ${m.name}</code>) and it's picked automatically.`}</div>`)}</details>`;
  }
  function stopWhen() {
    const q = TR.q, ps = qPrompts();
    return html`<section class="card tr-span2"><h2 class="tr-h">Stop when</h2>
      <div class="seg"><button class="${q.on ? "" : "on"}" data-tr-act="q-off">after the steps above</button><button class="${q.on ? "on" : ""}" data-tr-act="q-on">it's good enough</button></div>
      ${!q.on ? html`<p class="faint small">Trains for exactly the steps set above.</p>` : html`
      <p class="small">At every validation check it asks the model these prompts (temperature 0.3) and grades each reply: <b>it passes if the reply contains any of the expected words</b>. Training stops as soon as ${q.target_loss !== "" ? "the pass rate and the target loss are both reached" : "enough pass"}, or when validation hasn't improved for ${q.give_up} checks in a row (then it tells you what's missing). The steps above become the <b>maximum</b>, so set them high.</p>
      <div class="row wrap"><label class="tr-field inline"><span>Prompts</span><select class="select" data-tr-act="q-preset">${raw(Object.entries(Q_PRESETS).map(([k, v]) => '<option value="' + k + '"' + (q.preset === k ? " selected" : "") + ">" + esc(v.label) + "</option>").join(""))}</select></label></div>
      <div class="tr-scroll"><table class="tr-key tr-qtable"><tr><th>Test prompt (what you'd type in Chat)</th><th>Pass if the reply contains any of</th><th></th></tr>
        ${ps.map((p, i) => html`<tr><td><input class="input" data-tr-q="${i}" data-k="prompt" value="${p.prompt}" placeholder="e.g. What is a node?"></td><td><input class="input" data-tr-q="${i}" data-k="expect" value="${p.expect}" placeholder="words, comma separated (empty: just show it)"></td><td><button class="btn small" data-tr-qdel="${i}" aria-label="remove">✕</button></td></tr>`)}</table></div>
      <div class="row wrap mt"><button class="btn small" data-tr-act="q-add">Add a prompt</button><span class="spacer"></span>
        <label class="tr-field inline"><span>pass when</span><select class="select" data-tr-act="q-pass">${raw([[1, "all pass"], [0.8, "80% pass"], [0.6, "60% pass"], [0.5, "half pass"]].map(([v, l]) => '<option value="' + v + '"' + (+q.pass === v ? " selected" : "") + ">" + l + "</option>").join(""))}</select></label>
        <label class="tr-field inline"><span>and validation loss ≤</span><input class="input" data-tr-act="q-target" value="${q.target_loss}" placeholder="optional" style="width:90px"></label>
        <label class="tr-field inline"><span>give up after</span><input class="input" type="number" min="3" max="100" data-tr-act="q-giveup" value="${q.give_up}" style="width:70px"><span class="faint small">checks with no progress</span></label></div>
      <p class="faint small">Expected words test <i>what</i> it says, not how well: pick words a good reply must contain (“19”, “Saturday”) or that mark the style you want (“thou”). The check runs every “Check every” steps, so set that to a few hundred.</p>`}</section>`;
  }
  function raceLive(st) {
    const j = st.job || {}; if (j.mode !== "race") return "";
    if (!j.results) return html`<p class="small">Racing: ${(j.machines || []).join(", ")} each train their own copy. The chart shows ${j.lead}; the others' lines are in the log, marked [name].</p>`;
    return html`<h4>Race result</h4><div class="tr-scroll"><table class="tr-key"><tr><th>Machine</th><th>Best validation loss</th><th>Steps</th><th></th></tr>
      ${j.results.map((r) => html`<tr class="${r.winner ? "hl" : ""}"><td><b>${r.machine}</b></td><td>${f(r.validation_loss, 4)}</td><td>${n0(r.steps)}</td><td>${r.winner ? html`<span class="good-t">kept</span>` : ""}</td></tr>`)}</table></div>`;
  }
  function qualityLive(st) {
    const ql = st.quality_last; if (!st.job.until_good && !ql) return "";
    const sc = (st.quality || []).filter((x) => x.score != null);
    return html`<div class="tr-quality"><h4>Test replies${ql ? " at step " + n0(ql.step) : ""}${ql && ql.score != null ? html` · <b>${f(ql.score * 100, 3)}% pass</b>` : ""}</h4>
      ${st.verdict ? html`<div class="tr-adv ${/^Good/.test(st.verdict) ? "good" : "warn"}">${st.verdict}</div>` : ""}
      ${ql ? html`<div class="tr-scroll"><table class="tr-key"><tr><th></th><th>Prompt</th><th>Reply</th><th>Wanted</th></tr>${ql.results.map((r) => html`<tr><td>${r.ok == null ? "·" : r.ok ? html`<span class="good-t">✓</span>` : html`<span class="bad-t">✗</span>`}</td><td class="small">${r.prompt}</td><td class="small"><i>${r.reply || "(nothing)"}</i></td><td class="small faint">${(r.expect || []).join(", ")}</td></tr>`)}</table></div>` : html`<p class="faint small">The first check runs at the first validation.</p>`}
      ${sc.length > 1 ? html`<canvas class="tr-cv short" id="tr-qchart"></canvas>` : ""}</div>`;
  }
  function machinesCard() {
    const ov = TR.ov, p = ov.plan, inPlan = new Set(TR.together !== "fastest" ? p.share : p.share.slice(0, 1));
    const stateText = (w) => ({ off: "off", starting: "starting", "checking PyTorch": "checking PyTorch", "installing PyTorch": "installing PyTorch", ready: "ready", failed: "can't train" })[w.state] || w.state;
    const lead = ov.machines.find((m) => m.name === p.lead);
    return html`<section class="card"><h2 class="tr-h">Where it trains <span class="right faint small">${ov.mode === "cluster" ? "your cluster, fastest first" : "no cluster here: this machine"}</span></h2>
      ${!p.lead ? html`<div class="tr-adv bad">${p.notes[0]}</div>` : ""}
      <div class="tr-scroll"><table class="tr-key tr-machines"><tr><th></th><th>Machine</th><th>Hardware</th><th>Speed</th><th>Trainer</th></tr>
      ${ov.machines.map((m) => html`<tr class="${inPlan.has(m.name) ? "hl" : ""} ${m.usable ? "" : "off"}">
        <td>${ov.mode === "cluster" ? html`<input type="checkbox" data-tr-machine="${m.name}" ${TR.machines.length ? (TR.machines.includes(m.name) ? "checked" : "") : (p.share.includes(m.name) ? "checked" : "")} ${m.usable ? "" : "disabled"} aria-label="use ${m.name}">` : ""}</td>
        <td><b>${m.name}</b>${m.name === p.lead ? html` <span class="chip small">fastest</span>` : ""}</td>
        <td class="small">${m.gpu ? m.gpu.name + " · " + gb(m.gpu.vram) : n0(m.cores) + " CPU cores"} · ${gb(m.ram_total)} RAM</td>
        <td class="small">${m.usable ? "~" + f(m.speed / 1e9, 3) + " GFLOP/s" + (m.measured ? " (measured)" : " (estimate)") : "–"}</td>
        <td class="small">${m.usable ? html`<span class="${m.worker.state === "ready" ? "good-t" : m.worker.state === "failed" ? "bad-t" : ""}">${stateText(m.worker)}</span>${m.worker.state !== "ready" && m.worker.detail ? html`<div class="faint">${m.worker.detail}</div>` : ""}` : html`<span class="faint">${m.reason}</span>`}</td></tr>`)}</table></div>
      ${lead && /starting|installing|checking/.test(lead.worker.state) ? html`<div class="tr-adv ok">Getting <b>${lead.name}</b> ready: PyTorch installs itself there the first time (${lead.gpu ? "about 3 GB for the GPU version" : "about 200 MB"}), then stays. You can start training now: it begins as soon as it's ready.</div>
        ${lead.worker.log.length ? html`<details><summary class="small">Install progress</summary><pre class="tr-pre tr-log">${lead.worker.log.join("\n")}</pre></details>` : ""}` : ""}
      ${p.share.length > 1 ? html`<h3 class="tr-h3">How ${p.share.join(" + ")} work together</h3>
        <div class="seg tr-together">${[["share", "Share one model"], ["race", "Race copies, keep the best"], ["fastest", "Fastest only"]].map(([k, l]) => html`<button class="${TR.together === k ? "on" : ""}" data-tr-together="${k}">${l}</button>`)}</div>
        <p class="faint small">${TR.together === "share" ? "One model, trained faster: each machine trains a copy for ~2 minutes, the copies' weights are averaged, repeat (~" + f(p.speed / 1e9, 3) + " GFLOP/s together)."
          : TR.together === "race" ? "Each machine trains the whole model on its own, with a different random seed (and so different random windows of text). When " + p.lead + " finishes, the others stop where they are; the copy with the lowest validation loss is saved. Different seeds really do end at different losses: racing picks the luckiest."
          : "Only " + p.lead + " trains; the others stay idle."}</p>` : ov.mode === "cluster" ? html`<p class="faint small">Tick more machines to share one model between them or race copies.</p>` : ""}
      ${p.notes.filter((n) => p.lead).map((n) => html`<p class="faint small">${n}</p>`)}
      <p class="faint small">Training runs on ${ov.mode === "cluster" ? "the chosen nodes, each in a pod (namespace nodeyard-train)" : "this machine, in a separate worker process"}; models are copied back here. Lesson 13 explains how sharing works.</p></section>`;
  }
  function savedModels() {
    const ms = TR.ov ? TR.ov.models : [];
    return html`<section class="card tr-span2"><h2 class="tr-h">Saved models</h2>${!ms.length ? html`<div class="empty small">None yet: train one above.</div>` :
      html`<div class="tr-scroll"><table class="tr-key"><tr><th>Name</th><th>Weights</th><th>Shape</th><th>Steps</th><th>Validation loss</th><th>Trained on</th><th></th></tr>
      ${ms.map((m) => html`<tr><td><b>${m.name}</b></td><td>${n0(m.parameters)}</td><td>V ${n0(m.vocabulary)} · T ${m.context} · d ${m.width} · L ${m.layers} · h ${m.heads}</td><td>${n0(m.trained_steps)}</td>
        <td>${f(m.validation_loss, 4)}${m.validation_loss ? html` <span class="faint">(perplexity ${f(Math.exp(m.validation_loss), 3)})</span>` : ""}</td><td class="small">${(m.data_files || []).join(", ")}</td>
        <td class="row"><button class="btn small" data-tr-chatwith="${m.name}">Chat</button><button class="btn small" data-tr-inspect="${m.name}">Inspect</button><button class="btn small danger" data-tr-delmodel="${m.name}">Delete</button></td></tr>`)}</table></div>`}</section>`;
  }

  function paintLive() {
    if (TR.tab !== "train") return;
    const st = TR.status, live = $("#tr-live"); if (!live) return;
    const chip = $("#tr-live-chip");
    if (!st || !st.job || !st.job.model) { setHTML(live, html`<div class="empty small">Nothing has trained yet. Choose data, check the settings, press Start.</div>`); drawLive(); return; }
    const last = st.last || {}, j = st.job, val = st.val.length ? st.val[st.val.length - 1].loss : null, V0 = +(j.settings || {}).vocab_size || 2048;
    const total = +j.steps || 0, frac = total && last.step ? Math.min(1, (last.step - (+(j.base_steps || 0))) / total) : last.of ? last.local / last.of : 0;
    const eta = last.sps && total ? Math.max(0, total * (1 - frac)) / last.sps : last.sps ? (last.of - last.local) / last.sps : null;
    const where = (j.machines || []).join(" + "), rounds = j.mode === "shared" && j.rounds ? " · round " + j.round + " of " + j.rounds : j.mode === "race" ? " · race" : "";
    if (chip) setHTML(chip, st.running ? html`<span class="chip good"><i class="dot good"></i>${j.phase && j.phase !== "training" ? j.phase : "training"} ${j.model}${where ? " on " + where : ""}${rounds}</span>` : html`<span class="chip">${j.code === 0 ? "finished" : j.code == null ? "stopped" : "failed (exit " + j.code + ")"} · ${j.model}</span>`);
    setHTML(live, html`<div class="tr-progress big"><i style="width:${(frac * 100).toFixed(1)}%"></i></div>
      <div class="tr-stats">${[["Step", last.local ? (total ? n0(Math.round(frac * total)) + " / " + n0(total) : n0(last.local) + " / " + n0(last.of)) : j.phase || "starting…"], ["Time left", st.running && eta != null ? dur(eta) : "–"], ["Training loss", f(last.loss, 4)], ["Validation loss", f(val, 4)],
        ["Perplexity e^ℒ", val != null ? f(Math.exp(val), 4) : "–"], ["Learning rate", f(last.lr, 3)], ["Speed", last.tps ? n0(last.tps) + " tokens/s" : "–"], ["Total steps", n0(last.step)]].map(([a, b]) => html`<div><span>${a}</span><b>${b}</b></div>`)}</div>
      ${val != null ? html`<p class="small">Right now the model is about as unsure as picking among <b>${f(Math.exp(val), 3)}</b> equally likely tokens for each next token (a random guess would be ${n0(V0)}: ln ${n0(V0)} = ${f(Math.log(V0), 4)}). ${st.val.length > 2 && st.val[st.val.length - 1].loss > Math.min(...st.val.map((v) => v.loss)) * 1.02 ? "Validation is rising above its best: the model is starting to memorise; more steps won't help much." : ""}</p>` : ""}`);
    const qbox = $("#tr-qlive"); if (qbox) setHTML(qbox, html`${raceLive(st)}${qualityLive(st)}`);
    const box = $("#tr-logbox"); if (box) { const atEnd = box.scrollTop + box.clientHeight >= box.scrollHeight - 8; box.textContent = TR.lines.slice(-400).join("\n"); if (atEnd) box.scrollTop = box.scrollHeight; }
    drawLive();
  }
  function drawLive() {
    const st = TR.status; const tr = st ? st.train : [], vl = st ? st.val : [];
    let e = null; const sm = tr.map((p) => { e = e == null ? p.loss : e * 0.6 + p.loss * 0.4; return [p.step, e]; });
    const V0 = st && st.job && st.job.settings ? +st.job.settings.vocab_size || 2048 : +curSet().vocab_size;
    const xs = tr.map((p) => p.step);
    chart($("#tr-loss"), [{ pts: tr.map((p) => [p.step, p.loss]), color: cssv("--accent", "#7c8cff"), alpha: 0.35, width: 1 }, { pts: sm, color: cssv("--accent", "#7c8cff"), width: 2 },
      { pts: vl.map((p) => [p.step, p.loss]), color: cssv("--good", "#3d9"), width: 2, dots: true }, { pts: xs.length ? [[xs[0], Math.log(V0)], [xs[xs.length - 1], Math.log(V0)]] : [], color: cssv("--faint", "#888"), dash: [4, 4], width: 1 }],
      { empty: "The loss appears here once training starts.", xlabel: "step" });
    chart($("#tr-lr"), [{ pts: tr.map((p) => [p.step, p.lr]), color: cssv("--accent2", "#2de") }], { y0: 0, empty: "–" });
    const qs = st ? (st.quality || []).filter((x) => x.score != null) : [];
    if ($("#tr-qchart")) chart($("#tr-qchart"), [{ pts: qs.map((x) => [x.step, x.score * 100]), color: cssv("--good", "#3d9"), dots: true }], { y0: 0, xlabel: "step · % of test prompts passing" });
    chart($("#tr-grad"), [{ pts: (st ? st.grad : []).map((p) => [p.local, p.norm]), color: cssv("--warn", "#fb2"), dots: true }], { y0: 0, empty: "–" });
  }

  function workerNote(what) {
    const ov = TR.ov; if (!ov || !ov.machines.length) return "";
    if (ov.machines.some((m) => m.usable && m.worker.state === "ready")) return "";
    const lead = ov.machines.find((m) => m.name === ov.plan.lead);
    if (!lead) return html`<div class="tr-adv bad">${ov.plan.notes[0]}</div>`;
    return html`<div class="tr-adv ok">${what} runs on <b>${lead.name}</b>, which is ${lead.worker.state === "failed" ? "unable to: " + lead.worker.detail : "getting ready (PyTorch installs itself the first time; " + (lead.worker.detail || "starting") + ")"}.</div>`;
  }
  function insideTab() {
    const ov = TR.ov, ms = ov ? ov.models : [], x = TR.explain;
    return html`${workerNote("This")}<section class="card"><h2 class="tr-h">Inside the model: every value of one real step</h2>
      <p class="small">This runs your text through a saved model <b>by hand</b> — each matrix multiply written out — and checks the result against PyTorch. Then it runs the backward pass and one AdamW update. These are the actual numbers training uses. (Vectors show the first ${x ? x.show_dims : 8} of d numbers; statistics cover all of them.)</p>
      <div class="row wrap"><select class="select" data-tr-act="ex-model"><option value="">A tiny untrained model (no training needed)</option>${raw(ms.map((m) => '<option value="' + esc(m.name) + '"' + (TR.exModel === m.name ? " selected" : "") + ">" + esc(m.name) + "</option>").join(""))}</select>
        <input class="input grow" data-tr-act="ex-prompt" value="${TR.exPrompt}" placeholder="Text to run through the model">
        <button class="btn primary" data-tr-act="explain" ${TR.exBusy ? "disabled" : ""}>${TR.exBusy ? "Working…" : "Run it"}</button></div></section>
      ${x ? explainView(x) : ""}`;
  }
  function explainView(x) {
    const toks = x.tokens.map((t) => vis(t.text)), c = x.config, hd = x.head_dim;
    return html`<section class="card"><h2 class="tr-h">1 · Tokens</h2><div class="tr-toks">${x.tokens.map((t) => html`<span class="tr-tok"><b>${vis(t.text)}</b><small>#${t.id}</small></span>`)}</div>
        <p class="faint small">${x.trained ? "Your model" : "Untrained demo model"}: V ${n0(c.vocabulary)} · T ${c.context} · d ${c.width} · L ${c.layers} · h ${c.heads} (head size ${hd})${c.trained_steps ? " · trained " + n0(c.trained_steps) + " steps" : ""}.</p></section>
      <section class="card"><h2 class="tr-h">2 · Embeddings: x<sub>t</sub> = E[token] + P[t]</h2><div class="tr-scroll"><table class="tr-key"><tr><th>t</th><th>token</th><th>E[token]</th><th>P[t]</th><th>x<sub>t</sub></th></tr>
        ${x.tokens.map((t, i) => html`<tr><td>${i}</td><td><code>${toks[i]}</code></td><td>${vec(x.embedding.token_rows[i])}</td><td>${vec(x.embedding.position_rows[i])}</td><td>${vec(x.embedding.sum_rows[i])}</td></tr>`)}</table></div>
        <p class="faint small">E is ${x.embedding.token_matrix_shape.join(" × ")}, P is ${x.embedding.position_matrix_shape.join(" × ")}.</p></section>
      ${x.layers.map((ly) => html`<section class="card"><h2 class="tr-h">3 · Block ${ly.index + 1} of ${c.layers}</h2>
        <h4>Layer norm 1 (last token “${toks[toks.length - 1]}”)</h4>
        <div class="tr-calc small">μ per token: ${ly.norm1.mean.map((v) => f(v, 3)).join(", ")}<br>σ² per token: ${ly.norm1.variance.map((v) => f(v, 3)).join(", ")}<br>(x − μ)/√(σ² + ${ly.norm1.eps}) = ${vec(ly.norm1.normalized_last)}<br>γ = ${vec(ly.norm1.gamma)} β = ${vec(ly.norm1.beta)}<br>output = ${vec(ly.norm1.out_last)}</div>
        ${ly.attention.heads.map((hh, hi) => html`<h4>Attention head ${hi + 1}: scores (QKᵀ × ${ly.attention.scale}) and weights (softmax)</h4>
          <div class="tr-two"><div class="tr-scroll">${heat(hh.scores, { rows: toks, cols: toks })}</div><div class="tr-scroll">${heat(hh.weights, { rows: toks, cols: toks })}</div></div>
          <details><summary class="small">Q, K, V vectors for every token (head ${hi + 1})</summary><div class="tr-scroll"><table class="tr-key"><tr><th>token</th><th>q</th><th>k</th><th>v</th></tr>${toks.map((tk, i) => html`<tr><td><code>${tk}</code></td><td>${vec(hh.q[i])}</td><td>${vec(hh.k[i])}</td><td>${vec(hh.v[i])}</td></tr>`)}</table></div></details>`)}
        ${c.heads > ly.attention.heads.length ? html`<p class="faint small">(${c.heads - ly.attention.heads.length} more heads not shown.)</p>` : ""}
        <div class="tr-calc small">attention output (last token, after W<sub>O</sub>) = ${vec(ly.attention.out_last)}<br>+ residual → ${vec(ly.attention.residual_last)}</div>
        <h4>Feed-forward (${ly.feed_forward.hidden_size} neurons)</h4>
        <div class="tr-calc small">xW₁ + b₁ = ${vec(ly.feed_forward.pre_last)}<br>GELU(…) = ${vec(ly.feed_forward.gelu_last)} · ${f(ly.feed_forward.active_fraction * 100, 3)}% of neurons positive<br>·W₂ + b₂ = ${vec(ly.feed_forward.out_last)}<br>+ residual → ${vec(ly.feed_forward.residual_last)}</div></section>`)}
      <section class="card"><h2 class="tr-h">4 · Output: what comes after “${toks[toks.length - 1]}”?</h2>
        <table class="tr-key"><tr><th>token</th><th>logit z</th><th>p = e<sup>z − ${f(x.output.logsumexp_last, 4)}</sup></th></tr>${x.output.top_next.map((t) => html`<tr><td><code>${vis(t.text)}</code> #${t.id}</td><td>${f(t.logit, 4)}</td><td><span class="tr-pbar" style="width:${Math.round(t.probability * 160)}px"></span>${f(t.probability * 100, 3)}%</td></tr>`)}</table>
        <p class="faint small">Logits: ${x.output.logits_shape.join(" × ")} (a score for every vocabulary entry at every position). ln Σe<sup>z</sup> = ${f(x.output.logsumexp_last, 5)}. Hand-written maths vs PyTorch: largest difference ${f(x.check_max_difference, 2)} (rounding only).</p></section>
      <section class="card"><h2 class="tr-h">5 · Loss at every position</h2><table class="tr-key"><tr><th>after</th><th>real next token</th><th>p it gave</th><th>−ln p</th></tr>
        ${x.loss.per_position.map((p) => html`<tr><td><code>${toks[p.position]}</code></td><td><code>${vis(p.target_text)}</code></td><td>${f(p.probability, 4)}</td><td>${f(p.loss, 4)}</td></tr>`)}</table>
        <div class="tr-calc">ℒ = mean = <b>${f(x.loss.mean, 5)}</b> · random guess ln V = ${f(x.loss.random_guess, 5)} · perplexity e<sup>ℒ</sup> = ${f(Math.exp(x.loss.mean), 4)}</div></section>
      <section class="card"><h2 class="tr-h">6 · Backward: the gradient of every weight</h2><p class="small">Total ${n0(x.total_parameters)} weights. “sample” is the single weight with the largest gradient in that tensor.</p>
        <div class="tr-scroll"><table class="tr-key"><tr><th>tensor</th><th>shape</th><th>count</th><th>mean ± std</th><th>‖∂ℒ/∂w‖</th><th>sample w</th><th>its ∂ℒ/∂w</th></tr>
        ${x.parameters.map((p) => html`<tr><td><code>${p.name}</code></td><td>${p.shape.join("×")}</td><td>${n0(p.count)}</td><td>${f(p.value.mean, 3)} ± ${f(p.value.std, 3)}</td><td>${f(p.grad_norm, 3)}</td><td>${f(p.sample.value, 4)}</td><td>${f(p.sample.grad, 4)}</td></tr>`)}</table></div></section>
      ${adamView(x.adamw)}`;
  }
  function adamView(a) {
    return html`<section class="card"><h2 class="tr-h">7 · One AdamW update, with every number</h2><p class="small">Weight <code>${a.name}[${a.index}]</code>, first step (t = 1, m and v start at 0):</p>
      <div class="tr-calc">gradient norm ‖g‖ = ${f(a.grad_norm, 5)} → clip scale = min(1, 1/‖g‖) = ${f(a.clip_scale, 5)}<br>g = ${f(a.raw_grad, 5)} × ${f(a.clip_scale, 5)} = <b>${f(a.g, 5)}</b><br>
        m = β₁·0 + (1 − ${a.beta1})·g = <b>${f(a.m, 5)}</b><br>v = β₂·0 + (1 − ${a.beta2})·g² = <b>${f(a.v, 5)}</b><br>
        m̂ = m ÷ (1 − ${a.beta1}¹) = ${f(a.m_hat, 5)} &nbsp; v̂ = v ÷ (1 − ${a.beta2}¹) = ${f(a.v_hat, 5)}<br>
        step = η·m̂/(√v̂ + ε) = ${a.lr} × ${f(a.m_hat, 5)} ÷ (${f(Math.sqrt(a.v_hat), 5)} + ${a.eps}) = <b>${f(a.step, 5)}</b><br>
        decay = η·λ·w = ${a.lr} × ${a.weight_decay} × ${f(a.w, 5)} = ${f(a.decay, 5)}<br>
        w = ${f(a.w, 6)} − ${f(a.step, 5)} − ${f(a.decay, 5)} = <b>${f(a.w_new, 6)}</b></div>
      <p class="muted small">On the first step m̂/√v̂ = g/|g| = ±1, so every weight moves by exactly η: that's why warm-up starts η near 0. Later, m̂/√v̂ shrinks where gradients disagree from step to step.</p></section>`;
  }

  function chatTab() {
    const ov = TR.ov, ms = ov ? ov.models : [], c = TR.chat;
    if (!ms.length) return html`<section class="card"><div class="empty"><b>No trained model yet</b>Train one in step 3, then talk to it here.<div class="mt"><button class="btn primary" data-tr-tab="train">Go to training</button></div></div></section>`;
    return html`${workerNote("Chat")}<section class="card tr-chat"><div class="row wrap"><select class="select" data-tr-act="chat-model">${raw(ms.map((m) => '<option value="' + esc(m.name) + '"' + (c.model === m.name ? " selected" : "") + ">" + esc(m.name) + " (" + n0(m.parameters) + " weights, loss " + f(m.validation_loss, 3) + ")</option>").join(""))}</select>
        <div class="seg"><button class="${c.mode === "chat" ? "on" : ""}" data-tr-act="mode-chat">Chat</button><button class="${c.mode === "continue" ? "on" : ""}" data-tr-act="mode-continue">Continue text</button></div>
        <span class="spacer"></span><button class="btn small" data-tr-act="chat-clear">Clear</button></div>
      <div class="tr-sliders">${slider("chat.temperature", "temperature", 0.1, 1.5, 0.05, c.temperature)}${slider("chat.top_k", "top-k", 0, 200, 1, c.top_k)}${slider("chat.top_p", "top-p", 0.1, 1, 0.05, c.top_p)}</div>
      <div class="tr-msgs" id="tr-msgs">${c.history.length ? c.history.map((m) => html`<div class="tr-msg ${m.role}"><b>${m.role === "user" ? "You" : "Model"}</b><div>${m.content}</div></div>`) : html`<div class="empty small">${c.mode === "chat" ? "Say something. The model sees “User: …\\nAssistant:” and writes the reply." : "Type the start of some text; the model continues it."}</div>`}
        ${c.busy ? html`<div class="tr-msg assistant"><b>Model</b><div><span class="spin"></span></div></div>` : ""}</div>
      <div class="row"><textarea class="input grow" id="tr-ask" rows="2" placeholder="${c.mode === "chat" ? "Message your model…" : "Start of the text…"}"></textarea><button class="btn primary" data-tr-act="send" ${c.busy ? "disabled" : ""}>Send</button></div>
      <p class="faint small">Replies come only from what this model learned from your data. Short, odd or repetitive answers mean it needs more data or training (see lesson 14).</p></section>`;
  }

  // ---------- actions ----------
  function setPath(path, value) {
    const parts = path.split("."); let o = parts[0] === "chat" ? TR.chat : L[parts[0]];
    for (let i = 1; i < parts.length - 1; i++) o = o[parts[i]];
    const k = parts[parts.length - 1]; o[k] = typeof o[k] === "number" ? +value : value;
  }
  async function uploadFiles(files) {
    for (const file of files) {
      let name = file.name.replace(/[^A-Za-z0-9 ._-]/g, "_").replace(/^[^A-Za-z0-9]+/, "").slice(-80);
      if (!/\.(txt|md|jsonl|json)$/i.test(name)) { toast(file.name + ": use .txt, .md, .jsonl or .json"); continue; }
      const id = Math.random().toString(36).slice(2, 12) + Date.now().toString(36), CH = 3 * 1024 * 1024, pieces = Math.max(1, Math.ceil(file.size / CH));
      TR.upload = { name, frac: 0 }; paint();
      for (let i = 0; i < pieces; i++) {
        const b64 = await new Promise((res, rej) => { const r = new FileReader(); r.onload = () => res(String(r.result).split(",")[1] || ""); r.onerror = rej; r.readAsDataURL(file.slice(i * CH, (i + 1) * CH)); });
        const r = await post("/api/train/upload", { id, name, index: i, data: b64, done: i === pieces - 1 });
        if (!r) { TR.upload = null; paint(); return; }
        TR.upload.frac = (i + 1) / pieces; paint();
      }
      TR.upload = null; if (!TR.sel.includes(name)) TR.sel.push(name); saveSel(); toast("Uploaded " + name);
      await loadOverview(); await checkData(name);
    }
  }
  function saveSel() { store.set("train.sel", JSON.stringify(TR.sel)); }
  async function checkData(name) {
    try { const r = await getJSON("/api/train/check?name=" + encodeURIComponent(name)); if (r.ok) TR.check = r.check; else toast(r.error); } catch (e) { toast("Couldn't check it."); }
    paint();
  }

  document.addEventListener("click", async (e) => {
    if (S.view !== "train" || !e.target.closest("#tr-root")) return;
    const t = e.target.closest("[data-tr-tab],[data-tr-lesson],[data-tr-act],[data-tr-check],[data-tr-del],[data-tr-delmodel],[data-tr-chatwith],[data-tr-inspect],[data-tr-get],[data-tr-use],[data-tr-qdel],[data-tr-together]");
    if (!t || t.tagName === "SELECT" || t.tagName === "INPUT" && t.type !== "button") return;
    const d = t.dataset;
    if (d.trTab) { e.preventDefault(); TR.tab = d.trTab; store.set("train.tab", TR.tab); paint(); window.scrollTo(0, 0); return; }
    if (d.trLesson != null) { TR.lesson = +d.trLesson; store.set("train.lesson", TR.lesson); paint(); const el = $("#tr-lesson"); if (el) el.scrollIntoView({ block: "start" }); return; }
    if (d.trCheck) return checkData(d.trCheck);
    if (d.trTogether) { TR.together = d.trTogether; store.set("train.together", TR.together); paint(); return; }
    if (d.trQdel != null) { qPrompts().splice(+d.trQdel, 1); saveQ(); paint(); return; }
    if (d.trGet) { if (await post("/api/train/datasets/get", { id: d.trGet })) loadOverview(); return; }
    if (d.trUse) { if (!TR.sel.includes(d.trUse)) TR.sel.push(d.trUse); saveSel(); toast(d.trUse + " ticked for training."); loadOverview(); return; }
    if (d.trDel) { if (confirm("Delete " + d.trDel + "?")) { if (await post("/api/train/data/delete", { name: d.trDel })) { if (TR.check && TR.check.name === d.trDel) TR.check = null; TR.sel = TR.sel.filter((x) => x !== d.trDel); saveSel(); loadOverview(); } } return; }
    if (d.trDelmodel) { if (confirm("Delete the model " + d.trDelmodel + "? This can't be undone.")) { if (await post("/api/train/model/delete", { name: d.trDelmodel })) loadOverview(); } return; }
    if (d.trChatwith) { TR.chat.model = d.trChatwith; TR.chat.history = []; TR.tab = "chat"; store.set("train.tab", "chat"); paint(); return; }
    if (d.trInspect) { TR.exModel = d.trInspect; TR.tab = "inside"; store.set("train.tab", "inside"); paint(); return; }
    const a = d.trAct;
    if (a === "glossary") { const el = $("#tr-glossary"); if (el) el.scrollIntoView({ block: "start", behavior: "smooth" }); }
    else if (a === "gd-step" || a === "gd-10") { for (let i = 0; i < (a === "gd-10" ? 10 : 1); i++) { const s = L.gd; s.hist.push(s.w); s.w = s.w - s.lr * 2 * s.x * (s.w * s.x - s.y); if (!isFinite(s.w) || Math.abs(s.w) > 1e6) { s.w = NaN; break; } } paint(); }
    else if (a === "gd-reset") { L.gd.w = -1; L.gd.hist = []; paint(); }
    else if (a === "sched-mine") { const s = curSet(); Object.assign(L.sched, { peak: +s.learning_rate, warmup: +s.warmup, steps: +s.steps, at: Math.round(s.steps / 3) }); paint(); }
    else if (a === "size-mine") { const s = curSet(); Object.assign(L.size, { V: +s.vocab_size, T: +s.context, d: +s.dim, L: +s.layers, h: +s.heads, B: +s.batch_size }); paint(); }
    else if (a === "paste") { const name = $("#tr-paste-name").value.trim(), text = $("#tr-paste-text").value; const r = await post("/api/train/paste", { name, text }); if (r) { if (!TR.sel.includes(r.name)) TR.sel.push(r.name); saveSel(); toast("Saved " + r.name); await loadOverview(); checkData(r.name); } }
    else if (a === "q-on" || a === "q-off") { TR.q.on = a === "q-on"; saveQ(); paint(); }
    else if (a === "q-add") { qPrompts().push({ prompt: "", expect: "" }); saveQ(); paint(); }
    else if (a === "mp-make") {
      const r = await post("/api/train/multiparty", { name: TR.mp.file, speaker: TR.mp.speaker, context: TR.mp.context, names: true });
      if (r) { toast("Made " + r.name + ": " + n0(r.examples) + " examples from " + n0(r.turns) + " turns"); if (!TR.sel.includes(r.name)) TR.sel.push(r.name); saveSel(); await loadOverview(); checkData(r.name); }
    }
    else if (a === "convert") { const r = await post("/api/train/convert", { name: TR.check.name }); if (r) { toast("Saved " + r.name + " (" + n0(r.records) + " records)"); await loadOverview(); checkData(r.name); } }
    else if (a === "use-rec" || a === "reset-settings") resetSettings();
    else if (a === "save-settings") {
      let name = prompt("Save these settings as a preset. Name (an existing name is replaced):", TR.preset || (TR.sel[0] ? TR.sel[0].replace(/\.[a-z]+$/i, "") + " settings" : ""));
      if (!name || !(name = name.trim())) return;
      const r = await post("/api/train/presets/save", { name, settings: TR.form, note: TR.sel.join(", ") });
      if (r) { TR.ov.presets = r.presets; TR.preset = name; store.set("train.preset", name); TR.edited = Object.assign({}, TR.form); paint(); toast("Saved “" + name + "”."); }
    }
    else if (a === "delete-preset") {
      if (confirm("Delete the saved settings “" + TR.preset + "”?")) { const r = await post("/api/train/presets/delete", { name: TR.preset }); if (r) { TR.ov.presets = r.presets; resetSettings(); } }
    }
    else if (a === "start") {
      const body = { data: TR.sel, settings: TR.form, resume: TR.resume, model: TR.resume ? TR.model : (TR.model || "").trim() || undefined, machines: TR.machines.length ? TR.machines : undefined, share: TR.together !== "fastest", together: TR.together,
        quality: TR.q.on ? { on: true, prompts: qPrompts().filter((p) => p.prompt.trim()).map((p) => ({ prompt: p.prompt, expect: String(p.expect).split(",").map((x) => x.trim()).filter(Boolean) })), pass: TR.q.pass, target_loss: TR.q.target_loss, give_up: TR.q.give_up } : undefined };
      const r = await post("/api/train/start", body);
      if (r) { toast("Training " + r.model); TR.lines = []; TR.since = 0; TR.status = null; TR.model = TR.resume ? TR.model : ""; await loadOverview(); poll(); }
    }
    else if (a === "stop") { if (await post("/api/train/stop", {})) toast("Stopping: it saves after the current step."); }
    else if (a === "explain") {
      TR.exBusy = true; paint();
      const r = await post("/api/train/explain", { model: TR.exModel, prompt: TR.exPrompt, tokens: 10 });
      TR.exBusy = false; if (r) TR.explain = r.explain; paint();
    }
    else if (a === "mode-chat" || a === "mode-continue") { TR.chat.mode = a.slice(5); TR.chat.history = []; paint(); }
    else if (a === "chat-clear") { TR.chat.history = []; paint(); }
    else if (a === "send") { sendChat(); }
  });
  async function sendChat() {
    const box = $("#tr-ask"), text = box ? box.value.trim() : "", c = TR.chat;
    if (!text || c.busy) return;
    const history = c.history.slice(); c.history.push({ role: "user", content: text }); c.busy = true; paint();
    const r = await post("/api/train/chat", { model: c.model, prompt: text, mode: c.mode, history: c.mode === "chat" ? history : [], temperature: c.temperature, top_k: c.top_k, top_p: c.top_p });
    c.busy = false; c.history.push({ role: "assistant", content: r ? (r.reply || "(nothing)") : "(no reply)" }); paint();
    const m = $("#tr-msgs"); if (m) m.scrollTop = m.scrollHeight;
  }
  document.addEventListener("keydown", (e) => { if (S.view === "train" && e.target.id === "tr-ask" && e.key === "Enter" && !e.shiftKey) { e.preventDefault(); sendChat(); } });
  document.addEventListener("input", (e) => {
    if (S.view !== "train" || !e.target.closest("#tr-root")) return;
    const t = e.target, d = t.dataset;
    if (d.trIn) { setPath(d.trIn, t.value); if (d.trIn.startsWith("chat.")) { const b = t.closest(".tr-sl").querySelector("b"); if (b) b.textContent = f(+t.value, 4); return; } paint(); }
    else if (d.trSet && t.type !== "checkbox" && t.tagName !== "SELECT") { const v = t.value.trim(); TR.form[d.trSet] = v === "" || isNaN(+v) ? v : +v; markEdited(); clearTimeout(TR.formT); TR.formT = setTimeout(() => { const ae = document.activeElement && document.activeElement.dataset.trSet; paint(); if (ae) { const el = $('[data-tr-set="' + ae + '"]'); if (el) { el.focus(); const n = el.value.length; try { el.setSelectionRange(n, n); } catch (_) { /* number inputs */ } } } }, 400); }
    else if (d.trQ != null) { qPrompts()[+d.trQ][d.k] = t.value; saveQ(); }
    else if (d.trAct === "q-target") { TR.q.target_loss = t.value.trim(); saveQ(); }
    else if (d.trAct === "q-giveup") { TR.q.give_up = Math.max(3, Math.min(100, +t.value || 10)); saveQ(); }
    else if (d.trAct === "ex-prompt") TR.exPrompt = t.value;
    else if (d.trAct === "model-name") TR.model = t.value;
  });
  document.addEventListener("change", async (e) => {
    if (S.view !== "train" || !e.target.closest("#tr-root")) return;
    const t = e.target, d = t.dataset;
    if (d.trMachine) {
      if (!TR.machines.length) TR.machines = TR.ov.plan.share.slice();
      if (t.checked) { if (!TR.machines.includes(d.trMachine)) TR.machines.push(d.trMachine); } else TR.machines = TR.machines.filter((x) => x !== d.trMachine);
      store.set("train.machines", JSON.stringify(TR.machines)); TR.form = null; loadOverview(); return;
    }
    if (d.trSel) { if (t.checked) { if (!TR.sel.includes(d.trSel)) TR.sel.push(d.trSel); } else TR.sel = TR.sel.filter((x) => x !== d.trSel); saveSel(); loadOverview(); }
    else if (d.trSet === "assistant_only") { TR.form.assistant_only = t.checked; markEdited(); paint(); }
    else if (d.trSet === "device") { TR.form.device = t.value; markEdited(); paint(); }
    else if (d.trAct === "files") { uploadFiles(Array.from(t.files || [])); t.value = ""; }
    else if (d.trAct === "hours") { TR.hours = +t.value; store.set("train.hours", TR.hours); TR.form = null; loadOverview(); }
    else if (d.trAct === "model-pick") {
      TR.resume = !!t.value; TR.model = t.value;
      const m = TR.ov.models.find((x) => x.name === t.value);
      if (m) { TR.form.learning_rate = +((m.learning_rate || 1e-3) * 0.3).toPrecision(2); markEdited(); toast("Learning rate lowered to " + TR.form.learning_rate + " for continuing (see the note below Start)."); }
      paint();
    }
    else if (d.trAct === "ex-model") TR.exModel = t.value;
    else if (d.trAct === "mp-file") {
      TR.mp.file = t.value; TR.mp.speakers = null; TR.mp.speaker = "";
      if (t.value) { try { const r = await getJSON("/api/train/speakers?name=" + encodeURIComponent(t.value)); if (r.ok) { TR.mp.speakers = r.speakers; TR.mp.speaker = r.speakers.length ? r.speakers[0].name : ""; } else toast(r.error); } catch (_) { toast("Couldn't read it."); } }
      paint();
    }
    else if (d.trAct === "mp-speaker") TR.mp.speaker = t.value;
    else if (d.trAct === "preset") { if (t.value) loadPreset(t.value); else resetSettings(); }
    else if (d.trAct === "q-preset") { TR.q.preset = t.value; TR.q.prompts = []; qPrompts(); saveQ(); paint(); }
    else if (d.trAct === "q-pass") { TR.q.pass = +t.value; saveQ(); paint(); }
    else if (d.trAct === "mp-context") TR.mp.context = Math.max(1, Math.min(8, +t.value || 3));
    else if (d.trAct === "chat-model") { TR.chat.model = t.value; TR.chat.history = []; paint(); }
    else if (d.trIn === "ce.target") { L.ce.target = +t.value; paint(); }
  });
  document.addEventListener("dragover", (e) => { if (S.view === "train" && e.target.closest && e.target.closest(".tr-drop")) e.preventDefault(); });
  document.addEventListener("drop", (e) => { const z = S.view === "train" && e.target.closest && e.target.closest(".tr-drop"); if (z) { e.preventDefault(); uploadFiles(Array.from(e.dataTransfer.files || [])); } });
  window.addEventListener("resize", () => { if (S.view === "train") drawAll(); });

  V.train = {
    build() { NY.freshHTML($("#view"), html`<div id="tr-root"></div>`); paint(); loadOverview(); TR.since = 0; TR.lines = []; poll(); },
    update() { },
  };
})();
