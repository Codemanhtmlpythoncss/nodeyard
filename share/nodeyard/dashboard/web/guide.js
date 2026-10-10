// Guide B: setting up and using the Nodeyard dashboard. A page of its own (sidebar › Setup guide), kept next to
// the code so it describes the version that is running. Loaded after app.js.
(function () {
  "use strict";
  const { html } = NY;
  const { V, $, card } = NY.ui;
  const cmd = (text) => html`<div class="cmd">${text}<button class="btn small" data-copy="${text}">Copy</button></div>`;
  const step = (title, body) => card(title, body);

  V.guide = {
    build() {
      const origin = location.origin;
      NY.freshHTML($("#view"), html`<div class="guide">
        ${step("1. Install nodeyard and start the dashboard", html`<p>On the machine that runs (or will run) the Kubernetes control plane, as an admin user:</p>
          ${cmd("curl -fsSL https://raw.githubusercontent.com/Codemanhtmlpythoncss/nodeyard/main/install.sh | sudo bash")}
          ${cmd("sudo nodeyard dashboard start")}
          <p>It needs bash, curl and jq (the installer fetches what is missing) and Python 3.8 or newer for the dashboard. The dashboard listens on port <b>9092</b> on this machine, your LAN and Tailscale. <code>nodeyard dashboard</code> shows its addresses; <code>sudo nodeyard install master</code> / <code>install worker</code> set up the cluster itself.</p>`)}
        ${step("2. Sign in", html`<p>The sign-in password is made when the dashboard starts. Show it, or set your own:</p>
          ${cmd("sudo nodeyard dashboard password")}
          <p>Settings › Password changes it and signs out other browsers. This password is only for the website (and the Mac app's Manage view); it is not the API key.</p>`)}
        ${step("3. The shared server API key", html`<p>One key is used by yardcode, the control API (<code>${origin}/api/v1</code>), the Mac app and every model. See or change it under <b>Settings › One shared API key</b>, or:</p>
          ${cmd("sudo nodeyard ai key --show")}
          <p>If an older split model uses its own key, <b>Use the running model's key everywhere</b> adopts it without restarting the model (<code>sudo nodeyard ai key --adopt-model</code>).</p>`)}
        ${step("4. Devices and hardware", html`<p>Nodes appear as soon as they join the cluster. For temperatures, per-process memory and GPUs, install the read-only node agents (Hardware page, or):</p>
          ${cmd("sudo nodeyard dashboard agent install")}
          <p>A machine that is briefly NotReady shows amber for a minute; a longer outage is red with Kubernetes' own reason.</p>`)}
        ${step("5. Models", html`<p><b>AI › Find models</b> searches Hugging Face for GGUF files and shows whether each fits your free memory. <b>Run split</b> spreads one model over several machines with llama.cpp; <b>Run on Ollama</b> puts a model in Ollama on every node (set Ollama up first on the Models tab). <b>AI › Models</b> loads, unloads, switches and deletes them; switching keeps the old file unless you tick “Also delete”.</p>
          <p><b>Automatic model unloading</b> (Models tab) frees memory from models idle for the time you choose. It is off until you turn it on and never unloads a model that is answering.</p>`)}
        ${step("6. Chat and providers", html`<p><b>AI › Chat</b> talks to the split model or any Ollama model. Both speak the OpenAI API, so other tools can use them too: the model's own endpoint (port 31435) or the key-protected gateway <code>${origin}/api/v1/chat/completions</code> (examples on the API tab). Big models on CPU nodes take a while to read long prompts; the chat shows how far it has got.</p>`)}
        ${step("7. AI skills and Research Mode", html`<p>Chat can use skills automatically: web search and page reading, Wikipedia, arXiv, weather, a calculator, a task list, Python, and files and shell (the last two run as your terminal user and ask before changing anything). Turn them off with <code>/skills off</code> or choose them in the chat's Settings.</p>
          <p><b>AI › Research</b> searches the web from the server, reads the pages, and writes a report that cites only what it read, with every source and the time it was read.</p>`)}
        ${step("8. yardcode and the Mac app", html`<p>yardcode (terminal AI agent) and Nodeyard AI (macOS) use this dashboard's address and the shared API key:</p>
          ${cmd("yardcode login")}
          <p>On a Mac, from the repository: <code>sh scripts/install-macos-ai-app.sh</code>. In the app, the server address is this dashboard (<code>${origin}</code>), not the model port 31435.</p>`)}
        ${step("9. Remote access to devices", html`<p><b>Terminal</b> opens a shell on this server as the terminal user (never root), only over Tailscale or your own network. <b>Commands</b> runs any nodeyard command with its options. <b>Doctor</b> checks for common problems and fixes them with one click. Kubernetes restarts and full rolling reboots are under Nodes › Devices.</p>
          <p>The dashboard has no installer for other software: install packages on a node yourself (over SSH or the Terminal page).</p>`)}
        ${step("10. Public access", html`<p><code>sudo nodeyard public on</code> publishes the dashboard and model API through Tailscale Funnel with HTTPS; the API key is then always required. <code>public off</code> stops it.</p>`)}
        ${step("Troubleshooting", html`<ul>
          <li><b>“Sign in first” everywhere</b>: the session ended or the password changed; sign in again.</li>
          <li><b>A model answers “HTTP 401”</b>: the shared key and the model's key differ: Settings › One shared API key › Use the running model's key everywhere.</li>
          <li><b>Chat says the model isn't ready</b>: check AI › Models. “Unloaded” means it was unloaded (by you or automatic unloading); sending a message or Load brings it back.</li>
          <li><b>A model keeps reloading</b>: Alerts show “ran out of memory” when Kubernetes killed a model server (OOMKilled). Use a smaller context or quant, or more machines.</li>
          <li><b>Downloaded models show “last seen”</b>: that machine couldn't be checked; the list refreshes when it is back.</li>
          <li><b>Web search or research fails</b>: the server needs internet access; a school or work filter can block search sites.</li>
          <li><b>Anything else</b>: <code>sudo nodeyard doctor</code> and <code>journalctl -u nodeyard-dashboard</code>.</li></ul>`)}
        ${step("Updating", html`<p>Update nodeyard (dashboard included) to the latest code on GitHub, then restart the dashboard:</p>
          ${cmd("sudo nodeyard update")}
          ${cmd("sudo nodeyard dashboard start")}
          <p>Update yardcode with <code>yardcode update</code> and the Mac app with <code>sh scripts/install-macos-ai-app.sh</code>. Your settings, chats and keys are kept.</p>`)}
      </div>`);
    },
    update() {},
  };
})();
