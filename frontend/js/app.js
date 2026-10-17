"use strict";

/*
 * MileFund frontend.
 *
 * Reads come from the Flask backend (which reads the chain and its event index).
 * Writes are signed by the user's MetaMask wallet and sent straight to the contract;
 * private keys never leave the wallet.
 */

const { ethers } = window;

const state = {
  config: null,
  browserProvider: null,
  signer: null,
  account: null,
  walletChainId: null,
  timeOffset: 0, // chain time minus local time, so countdowns follow the chain clock
  timers: [],
  renderToken: 0,
};

const ERROR_MESSAGES = {
  CampaignNotFound: "This campaign does not exist.",
  InvalidGoal: "The funding goal must be greater than zero.",
  InvalidDuration: "One of the durations is outside the allowed range.",
  InvalidMilestones: "A campaign needs between 1 and 5 milestones.",
  InvalidPercentages: "Milestone shares must each be positive and add up to exactly 100%.",
  InvalidHash: "The description hash is missing.",
  InvalidState: "This action is not available in the campaign's current state.",
  InvalidMilestoneState: "This action is not available for the current milestone.",
  NotCreator: "Only the campaign creator can do this.",
  CreatorCannotContribute: "Creators cannot back their own campaign.",
  ZeroAmount: "Enter an amount greater than zero.",
  AmountTooLarge: "That amount is too large.",
  FundingClosed: "The funding period has ended.",
  FundingStillOpen: "The funding period has not ended yet.",
  SubmissionWindowClosed: "The submission window for this milestone has closed.",
  CreatorStillActive: "The creator's submission window is still open.",
  NotBacker: "Only backers of this campaign can vote.",
  AlreadyVoted: "You have already voted in this round.",
  VotingClosed: "Voting for this milestone has closed.",
  VotingStillOpen: "Voting is still open.",
  NothingToWithdraw: "There are no released funds to withdraw.",
  NothingToRefund: "You have nothing to refund for this campaign.",
  TransferFailed: "The ETH transfer failed.",
  ReentrancyGuardReentrantCall: "Re-entrant call blocked.",
};

const PHASE_LABELS = {
  funding: "Funding",
  "funding-ended": "Funding ended",
  "in-progress": "In progress",
  revising: "Revising milestone",
  voting: "Vote open",
  "vote-ended": "Vote ended",
  "creator-inactive": "Creator inactive",
  completed: "Completed",
  failed: "Failed",
};

const CATEGORIES = ["technology", "education", "community", "environment", "health", "arts", "other"];

// ---------------------------------------------------------------------------
// DOM helpers (user content is always inserted as text, never as HTML)
// ---------------------------------------------------------------------------

function h(tag, props, ...children) {
  const el = document.createElement(tag);
  for (const [key, value] of Object.entries(props || {})) {
    if (value === undefined || value === null || value === false) continue;
    if (key === "class") el.className = value;
    else if (key === "text") el.textContent = value;
    else if (key === "style") Object.assign(el.style, value);
    else if (key === "dataset") Object.assign(el.dataset, value);
    else if (key.startsWith("on")) el.addEventListener(key.slice(2).toLowerCase(), value);
    else if (key === "value") el.value = value;
    else el.setAttribute(key, value === true ? "" : value);
  }
  append(el, children);
  return el;
}

function append(el, children) {
  for (const child of children.flat(Infinity)) {
    if (child === null || child === undefined || child === false) continue;
    el.append(child instanceof Node ? child : document.createTextNode(String(child)));
  }
  return el;
}

const $ = (sel, root = document) => root.querySelector(sel);

function externalLink(href, text) {
  return h("a", { href, target: "_blank", rel: "noopener noreferrer" }, text);
}

function txLink(hash, text) {
  const base = state.config?.explorerUrl;
  const label = text || shortHash(hash);
  return base ? externalLink(`${base}/tx/${hash}`, label) : h("span", { class: "mono", title: hash }, label);
}

function addressLink(address, text) {
  const base = state.config?.explorerUrl;
  const label = text || shortHash(address);
  return base ? externalLink(`${base}/address/${address}`, label) : h("span", { class: "mono", title: address }, label);
}

// ---------------------------------------------------------------------------
// Formatting
// ---------------------------------------------------------------------------

function shortHash(value) {
  return value ? `${value.slice(0, 6)}…${value.slice(-4)}` : "";
}

function fmtEth(wei, digits = 4) {
  const value = ethers.formatEther(BigInt(wei));
  const [whole, frac = ""] = value.split(".");
  const trimmed = frac.slice(0, digits).replace(/0+$/, "");
  return `${trimmed ? `${whole}.${trimmed}` : whole} ETH`;
}

function fmtPct(bps) {
  return `${(Number(bps) / 100).toLocaleString(undefined, { maximumFractionDigits: 2 })}%`;
}

function fmtDuration(seconds) {
  seconds = Math.max(0, Math.floor(seconds));
  const d = Math.floor(seconds / 86400);
  const hr = Math.floor((seconds % 86400) / 3600);
  const m = Math.floor((seconds % 3600) / 60);
  const s = seconds % 60;
  if (d) return `${d}d ${hr}h`;
  if (hr) return `${hr}h ${m}m`;
  if (m) return `${m}m ${s}s`;
  return `${s}s`;
}

function fmtTime(ts) {
  return new Date(ts * 1000).toLocaleString(undefined, { dateStyle: "medium", timeStyle: "short" });
}

function chainNow() {
  return Math.floor(Date.now() / 1000) + state.timeOffset;
}

function syncClock(chainTimestamp) {
  state.timeOffset = chainTimestamp - Math.floor(Date.now() / 1000);
}

function percentOf(part, whole) {
  const w = BigInt(whole);
  if (w === 0n) return 0;
  return Math.min(100, Number((BigInt(part) * 10000n) / w) / 100);
}

function badge(phase) {
  return h("span", { class: `badge ${phase}` }, PHASE_LABELS[phase] || phase);
}

function countdown(deadline, { expired = "ended" } = {}) {
  const el = h("span", { dataset: { deadline: String(deadline), expired } });
  updateCountdown(el);
  return el;
}

function updateCountdown(el) {
  const left = Number(el.dataset.deadline) - chainNow();
  el.textContent = left > 0 ? fmtDuration(left) : el.dataset.expired;
  return left;
}

// ---------------------------------------------------------------------------
// API
// ---------------------------------------------------------------------------

async function api(path, options = {}) {
  const init = { headers: { Accept: "application/json" }, ...options };
  if (options.body !== undefined) {
    init.method = init.method || "POST";
    init.headers["Content-Type"] = "application/json";
    init.body = JSON.stringify(options.body);
  }
  let res;
  try {
    res = await fetch(path, init);
  } catch {
    throw new Error("Cannot reach the MileFund server. Is the backend running?");
  }
  const body = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(body.error || `Request failed (${res.status})`);
  return body;
}

// ---------------------------------------------------------------------------
// Toasts
// ---------------------------------------------------------------------------

function toast({ type = "info", title, body, timeout = 0 }) {
  const close = h("button", { type: "button", "aria-label": "Dismiss" }, "×");
  const titleEl = h("span");
  const bodyEl = h("div", { class: "toast-body" });
  const el = h("div", { class: `toast ${type}`, role: "status" }, h("div", { class: "toast-title" }, titleEl, close), bodyEl);
  let timer;
  const handle = {
    update(next) {
      if (next.type) el.className = `toast ${next.type}`;
      if (next.title !== undefined) titleEl.textContent = next.title;
      if (next.body !== undefined) bodyEl.replaceChildren(...[next.body].flat().filter(Boolean));
      clearTimeout(timer);
      if (next.timeout) timer = setTimeout(handle.close, next.timeout);
      return handle;
    },
    close() {
      el.remove();
    },
  };
  close.addEventListener("click", handle.close);
  $("#toasts").append(el);
  return handle.update({ title, body, timeout });
}

// ---------------------------------------------------------------------------
// Wallet
// ---------------------------------------------------------------------------

function hasWallet() {
  return typeof window.ethereum !== "undefined";
}

function onCorrectNetwork() {
  return state.walletChainId === state.config.chainId;
}

async function connectWallet() {
  if (!hasWallet()) {
    toast({
      type: "error",
      title: "MetaMask not found",
      body: ["Install the MetaMask browser extension, then reload this page. ", externalLink("https://metamask.io/download/", "Get MetaMask")],
    });
    return false;
  }
  try {
    await window.ethereum.request({ method: "eth_requestAccounts" });
    await refreshWallet();
    return Boolean(state.account);
  } catch (e) {
    toast({ type: "error", title: "Wallet connection failed", body: friendlyError(e), timeout: 6000 });
    return false;
  }
}

async function refreshWallet() {
  if (!hasWallet()) return updateWalletUI();
  state.browserProvider = new ethers.BrowserProvider(window.ethereum);
  const accounts = await window.ethereum.request({ method: "eth_accounts" });
  state.walletChainId = Number(await window.ethereum.request({ method: "eth_chainId" }));
  state.account = accounts[0] ? accounts[0].toLowerCase() : null;
  state.signer = state.account ? await state.browserProvider.getSigner(accounts[0]) : null;
  updateWalletUI();
}

function updateWalletUI() {
  const button = $("#wallet-button");
  const netBadge = $("#network-badge");
  const banner = $("#network-banner");
  if (!state.account) {
    button.textContent = hasWallet() ? "Connect wallet" : "Install MetaMask";
    button.title = "";
    netBadge.classList.add("hidden");
    banner.classList.add("hidden");
    return;
  }
  button.textContent = shortHash(state.account);
  button.title = `${state.account}\nClick to copy`;
  netBadge.classList.remove("hidden");
  if (onCorrectNetwork()) {
    netBadge.className = "network-badge ok";
    netBadge.textContent = state.config.networkName;
    banner.classList.add("hidden");
  } else {
    netBadge.className = "network-badge bad";
    netBadge.textContent = "Wrong network";
    banner.replaceChildren(
      `MileFund runs on ${state.config.networkName}. Your wallet is on chain ${state.walletChainId}.`,
      h("button", { class: "btn btn-sm", type: "button", onclick: switchNetwork }, `Switch to ${state.config.networkName}`)
    );
    banner.classList.remove("hidden");
  }
}

async function switchNetwork() {
  const chainIdHex = ethers.toQuantity(state.config.chainId);
  try {
    await window.ethereum.request({ method: "wallet_switchEthereumChain", params: [{ chainId: chainIdHex }] });
  } catch (e) {
    if (e.code !== 4902 || state.config.chainId !== 31337) {
      toast({ type: "error", title: "Could not switch network", body: friendlyError(e), timeout: 6000 });
      return;
    }
    // The local Anvil chain is not known to MetaMask by default, so offer to add it.
    await window.ethereum.request({
      method: "wallet_addEthereumChain",
      params: [{ chainId: chainIdHex, chainName: "Anvil Local", rpcUrls: ["http://127.0.0.1:8545"], nativeCurrency: { name: "Ether", symbol: "ETH", decimals: 18 } }],
    });
  }
}

async function requireReadyWallet() {
  if (!state.account && !(await connectWallet())) throw new Error("Connect your wallet to continue.");
  if (!onCorrectNetwork()) throw new Error(`Switch your wallet to ${state.config.networkName} first.`);
}

function writeContract() {
  return new ethers.Contract(state.config.contractAddress, state.config.abi, state.signer);
}

// ---------------------------------------------------------------------------
// Errors and transactions
// ---------------------------------------------------------------------------

const contractInterface = () => new ethers.Interface(state.config.abi);

function revertName(e) {
  if (e?.revert?.name) return e.revert.name;
  const candidates = [e?.data, e?.info?.error?.data, e?.error?.data, e?.info?.error?.data?.data];
  for (const data of candidates) {
    if (typeof data !== "string" || !data.startsWith("0x") || data.length < 10) continue;
    try {
      const parsed = contractInterface().parseError(data);
      if (parsed) return parsed.name;
    } catch {
      /* not one of our errors */
    }
  }
  return null;
}

function friendlyError(e) {
  if (!e) return "Unknown error";
  if (e.code === "ACTION_REJECTED" || e.code === 4001 || e.info?.error?.code === 4001) return "You rejected the request in MetaMask.";
  if (e.code === "INSUFFICIENT_FUNDS") return "Your wallet does not have enough ETH to cover the amount plus gas.";
  const name = revertName(e);
  if (name) return ERROR_MESSAGES[name] || `The contract rejected the transaction (${name}).`;
  if (e.code === "NETWORK_ERROR") return "Network error. Check your wallet's connection.";
  return e.shortMessage || e.reason || e.message || String(e);
}

/**
 * Sign and send a contract transaction, reporting each stage to the user and to the backend
 * (which records the final status in the user's history).
 */
async function sendTx({ action, campaignId = null, label, send }) {
  try {
    await requireReadyWallet();
  } catch (e) {
    toast({ type: "error", title: label, body: e.message, timeout: 6000 });
    return null;
  }
  const t = toast({ type: "pending", title: label, body: "Confirm the transaction in MetaMask…" });
  let tx;
  try {
    // ethers estimates gas first, so a transaction that would revert is caught here,
    // before the user pays for it, and the custom error is decoded.
    tx = await send(writeContract());
  } catch (e) {
    t.update({ type: "error", body: friendlyError(e), timeout: 8000 });
    return null;
  }
  api("/api/transactions", { body: { hash: tx.hash, from: state.account, action, campaignId } }).catch(() => {});
  t.update({ body: ["Submitted. Waiting for confirmation… ", txLink(tx.hash, "View transaction")] });
  try {
    const receipt = await tx.wait();
    t.update({ type: "success", body: [`Confirmed in block ${receipt.blockNumber}. `, txLink(tx.hash, "View transaction")], timeout: 8000 });
    return receipt;
  } catch (e) {
    t.update({ type: "error", body: [friendlyError(e), " ", txLink(tx.hash, "View transaction")], timeout: 10000 });
    return null;
  }
}

/** Sign a metadata document with MetaMask and store it on the backend. Returns its hash. */
async function publishMetadata(doc) {
  const content = JSON.stringify(doc);
  const hash = ethers.id(content); // keccak256 of the UTF-8 bytes, same as the backend
  const signature = await state.signer.signMessage(`MileFund metadata\nHash: ${hash}`);
  const res = await api("/api/metadata", { body: { content, signature } });
  if (res.hash !== hash) throw new Error("The server computed a different hash. Aborting.");
  return hash;
}

// ---------------------------------------------------------------------------
// Routing
// ---------------------------------------------------------------------------

const routes = [
  [/^#?\/?$/, () => renderHome()],
  [/^#\/create$/, () => renderCreate()],
  [/^#\/campaign\/(\d+)$/, (m) => renderCampaign(Number(m[1]))],
  [/^#\/me$/, () => renderMe()],
];

function clearTimers() {
  state.timers.forEach(clearInterval);
  state.timers = [];
}

async function render() {
  clearTimers();
  const token = ++state.renderToken;
  const hash = location.hash || "#/";
  document.querySelectorAll("[data-nav]").forEach((a) => a.classList.toggle("active", a.getAttribute("href") === hash));
  const view = $("#view");
  for (const [re, fn] of routes) {
    const m = hash.match(re);
    if (!m) continue;
    try {
      const content = await fn(m);
      if (token !== state.renderToken) return; // a newer navigation won the race
      view.replaceChildren(content);
    } catch (e) {
      if (token !== state.renderToken) return;
      view.replaceChildren(errorState(e.message));
    }
    startCountdowns();
    return;
  }
  view.replaceChildren(errorState("Page not found."));
}

function rerender() {
  // Keep the scroll position when refreshing the current view after a transaction.
  const y = window.scrollY;
  return render().then(() => window.scrollTo(0, y));
}

function startCountdowns() {
  let reloaded = false;
  state.timers.push(
    setInterval(() => {
      document.querySelectorAll("[data-deadline]").forEach((el) => {
        const wasRunning = el.textContent !== el.dataset.expired;
        const left = updateCountdown(el);
        // When a deadline passes, re-render once so newly available actions appear.
        if (left <= 0 && wasRunning && el.dataset.refresh === "1" && !reloaded) {
          reloaded = true;
          setTimeout(rerender, 1500);
        }
      });
    }, 1000)
  );
}

function errorState(message) {
  return h("div", { class: "card empty" }, h("h2", {}, "Something went wrong"), h("p", { class: "muted" }, message), h("a", { class: "btn", href: "#/" }, "Back to campaigns"));
}

// ---------------------------------------------------------------------------
// View: explore campaigns
// ---------------------------------------------------------------------------

const FILTERS = [
  ["all", "All"],
  ["Funding", "Funding"],
  ["Active", "Active"],
  ["Completed", "Completed"],
  ["Failed", "Failed"],
];

async function renderHome() {
  const data = await api("/api/campaigns");
  syncClock(data.now);
  const grid = h("div", { class: "campaign-grid" });
  const tabs = h("div", { class: "tabs", role: "tablist" });
  let filter = "all";

  const draw = () => {
    const items = data.campaigns.filter((c) => filter === "all" || c.state === filter);
    grid.replaceChildren(...items.map(campaignCard));
    if (!items.length) grid.replaceChildren(h("div", { class: "card empty" }, h("p", { class: "muted" }, "No campaigns here yet.")));
    tabs.querySelectorAll(".tab").forEach((t) => t.classList.toggle("active", t.dataset.filter === filter));
  };
  for (const [key, label] of FILTERS) {
    tabs.append(h("button", { class: "tab", type: "button", dataset: { filter: key }, onclick: () => ((filter = key), draw()) }, label));
  }
  draw();

  return h(
    "div",
    {},
    h(
      "div",
      { class: "hero" },
      h("div", {}, h("h1", {}, "Back projects. Release funds only when milestones are delivered."), h("p", { class: "muted" }, "Contributions are locked in a smart contract. Creators are paid one milestone at a time, after backers vote to approve the work. If a project stalls, backers reclaim what has not been released.")),
      h("a", { class: "btn btn-primary", href: "#/create" }, "Start a campaign")
    ),
    tabs,
    grid
  );
}

function campaignCard(c) {
  const pct = percentOf(c.totalRaised, c.goal);
  let timing;
  if (c.phase === "funding") timing = h("span", {}, countdown(c.deadline), " left");
  else if (c.state === "Active") timing = h("span", {}, `Milestone ${c.currentMilestone + 1} of ${c.milestoneCount}`);
  else timing = h("span", {}, `${c.milestoneCount} milestone${c.milestoneCount > 1 ? "s" : ""}`);

  return h(
    "a",
    { class: "card campaign-card", href: `#/campaign/${c.id}` },
    h("div", { class: "row" }, h("span", { class: "chip" }, c.category || "uncategorised"), h("span", { class: "spacer" }), badge(c.phase)),
    h("h3", {}, c.title || `Campaign #${c.id}`),
    h("p", {}, c.summary || "Description unavailable."),
    h("div", { class: "progress", role: "progressbar", "aria-valuenow": String(pct), "aria-valuemin": "0", "aria-valuemax": "100" }, h("span", { style: { width: `${pct}%` } })),
    h("div", { class: "card-meta" }, h("span", {}, h("strong", {}, fmtEth(c.totalRaised)), ` of ${fmtEth(c.goal)}`), h("span", {}, `${c.backerCount} backer${c.backerCount === 1 ? "" : "s"}`)),
    h("div", { class: "card-meta" }, timing, h("span", {}, `#${c.id}`))
  );
}

// ---------------------------------------------------------------------------
// View: create campaign
// ---------------------------------------------------------------------------

const UNITS = [
  ["60", "minutes"],
  ["3600", "hours"],
  ["86400", "days"],
];

function durationField(name, labelText, defaultValue, defaultUnit, hint) {
  return h(
    "div",
    { class: "field" },
    h("label", { for: `${name}-value` }, labelText),
    h(
      "div",
      { class: "inline-input" },
      h("input", { id: `${name}-value`, name: `${name}Value`, type: "number", min: "1", step: "1", value: defaultValue, required: true }),
      h("select", { name: `${name}Unit`, "aria-label": `${labelText} unit` }, UNITS.map(([v, l]) => h("option", { value: v, selected: v === defaultUnit }, l)))
    ),
    h("div", { class: "hint" }, hint),
    h("div", { class: "field-error", dataset: { errorFor: `${name}Value` } })
  );
}

function renderCreate() {
  const k = state.config.constants;
  const milestoneList = h("div");
  const pctTotal = h("span", { class: "pct-total" });
  const alertBox = h("div", { class: "form-alert hidden", role: "alert" });
  const steps = h("ol", { class: "steps hidden" }, h("li", {}, "1. Sign description"), h("li", {}, "2. Upload to server"), h("li", {}, "3. Confirm transaction"));
  const submit = h("button", { class: "btn btn-primary", type: "submit" }, "Create campaign");

  const recalc = () => {
    const values = [...milestoneList.querySelectorAll("[name=pct]")].map((i) => Number(i.value) || 0);
    const total = Math.round(values.reduce((a, b) => a + b, 0) * 100) / 100;
    pctTotal.textContent = `Total: ${total}%`;
    pctTotal.className = `pct-total ${total === 100 ? "ok" : "bad"}`;
    milestoneList.querySelectorAll(".milestone-editor").forEach((ed, i) => {
      $(".milestone-index", ed).textContent = `Milestone ${i + 1}`;
      $("[data-remove]", ed).disabled = milestoneList.children.length === 1;
    });
    addButton.disabled = milestoneList.children.length >= k.maxMilestones;
  };

  const addMilestone = (title = "", description = "", pct = "") => {
    const ed = h(
      "div",
      { class: "milestone-editor" },
      h(
        "div",
        { class: "milestone-editor-head" },
        h("strong", { class: "milestone-index" }),
        h("button", { class: "btn btn-link small", type: "button", dataset: { remove: "1" }, onclick: () => (ed.remove(), recalc()) }, "Remove")
      ),
      h(
        "div",
        { class: "grid-2" },
        h("div", { class: "field" }, h("label", {}, "Title"), h("input", { name: "msTitle", maxlength: "80", value: title, placeholder: "e.g. Working prototype" })),
        h("div", { class: "field" }, h("label", {}, "Share of funds (%)"), h("input", { name: "pct", type: "number", min: "0.01", max: "100", step: "0.01", value: pct, oninput: recalc }))
      ),
      h("div", { class: "field" }, h("label", {}, "What will be delivered"), h("textarea", { name: "msDescription", maxlength: "500", rows: "2", placeholder: "Concrete, verifiable deliverable backers will judge" }, description))
    );
    milestoneList.append(ed);
    recalc();
  };
  const addButton = h("button", { class: "btn btn-sm", type: "button", onclick: () => addMilestone() }, "+ Add milestone");

  const form = h(
    "form",
    { class: "stack", novalidate: true },
    h(
      "section",
      { class: "card" },
      h("h2", {}, "About the project"),
      h("div", { class: "field" }, h("label", { for: "title" }, "Title"), h("input", { id: "title", name: "title", maxlength: "80", placeholder: "Solar charging kiosks for the campus" }), h("div", { class: "field-error", dataset: { errorFor: "title" } })),
      h("div", { class: "field" }, h("label", { for: "summary" }, "One-line summary"), h("input", { id: "summary", name: "summary", maxlength: "200" }), h("div", { class: "field-error", dataset: { errorFor: "summary" } })),
      h("div", { class: "field" }, h("label", { for: "description" }, "Full description"), h("textarea", { id: "description", name: "description", maxlength: "3000", rows: "6" }), h("div", { class: "field-error", dataset: { errorFor: "description" } })),
      h("div", { class: "field" }, h("label", { for: "category" }, "Category"), h("select", { id: "category", name: "category" }, CATEGORIES.map((c) => h("option", { value: c }, c[0].toUpperCase() + c.slice(1)))))
    ),
    h(
      "section",
      { class: "card" },
      h("h2", {}, "Funding and governance"),
      h("div", { class: "field" }, h("label", { for: "goal" }, "Funding goal (ETH)"), h("input", { id: "goal", name: "goal", inputmode: "decimal", placeholder: "0.5" }), h("div", { class: "hint" }, "If the goal is not reached by the deadline, every backer can claim a full refund."), h("div", { class: "field-error", dataset: { errorFor: "goal" } })),
      h(
        "div",
        { class: "grid-3" },
        durationField("funding", "Funding period", "5", "60", "How long contributions are accepted."),
        durationField("voting", "Voting period", "3", "60", "How long each milestone vote stays open."),
        durationField("window", "Submission window", "30", "60", "Time you have to deliver each milestone.")
      ),
      h("p", { class: "action-note" }, `A milestone passes when approvals outweigh rejections and at least ${fmtPct(k.quorumBps)} of the raised funds vote. Each milestone can be submitted up to ${k.maxAttempts} times.`)
    ),
    h("section", { class: "card" }, h("div", { class: "card-title" }, h("h2", {}, "Milestones"), pctTotal), milestoneList, h("div", { class: "row" }, addButton, h("span", { class: "hint" }, `Up to ${k.maxMilestones} milestones. Shares must add up to 100%.`))),
    h("div", { class: "card" }, alertBox, h("div", { class: "row" }, submit, h("span", { class: "muted small" }, "You will sign the description, then confirm one transaction.")), steps)
  );

  addMilestone("Prototype", "", "30");
  addMilestone("Pilot deployment", "", "30");
  addMilestone("Full launch", "", "40");

  form.addEventListener("submit", async (ev) => {
    ev.preventDefault();
    alertBox.classList.add("hidden");
    const result = validateCreateForm(form);
    if (!result.ok) {
      alertBox.textContent = result.errors[0];
      alertBox.classList.remove("hidden");
      return;
    }
    try {
      await requireReadyWallet();
    } catch (e) {
      alertBox.textContent = e.message;
      alertBox.classList.remove("hidden");
      return;
    }
    submit.disabled = true;
    steps.classList.remove("hidden");
    const setStep = (i) => [...steps.children].forEach((li, j) => (li.className = j < i ? "done" : j === i ? "doing" : ""));
    try {
      setStep(0);
      const doc = { type: "campaign", author: state.account, createdAt: chainNow(), ...result.meta };
      const metadataHash = await publishMetadata(doc);
      setStep(2);
      const v = result.chain;
      const receipt = await sendTx({
        action: "createCampaign",
        label: "Create campaign",
        send: (c) => c.createCampaign(v.goal, v.funding, v.voting, v.window, v.percentages, metadataHash),
      });
      if (!receipt) return setStep(-1);
      setStep(3);
      const iface = contractInterface();
      const created = receipt.logs.map((l) => { try { return iface.parseLog(l); } catch { return null; } }).find((l) => l?.name === "CampaignCreated");
      location.hash = created ? `#/campaign/${created.args.campaignId}` : "#/";
    } catch (e) {
      setStep(-1);
      alertBox.textContent = friendlyError(e);
      alertBox.classList.remove("hidden");
    } finally {
      submit.disabled = false;
    }
  });

  return h("div", {}, h("div", { class: "hero" }, h("div", {}, h("h1", {}, "Start a campaign"), h("p", { class: "muted" }, "Describe your project, set a funding goal and split the funds into milestones. Backers release each part only after reviewing your progress."))), form);
}

function validateCreateForm(form) {
  const k = state.config.constants;
  const errors = [];
  form.querySelectorAll(".field-error").forEach((e) => (e.textContent = ""));
  form.querySelectorAll(".invalid").forEach((e) => e.classList.remove("invalid"));
  const fail = (input, message) => {
    errors.push(message);
    input.classList.add("invalid");
    const slot = form.querySelector(`[data-error-for="${input.name}"]`);
    if (slot && !slot.textContent) slot.textContent = message;
  };
  const text = (name, lo, hi, label) => {
    const input = form.elements[name];
    const value = input.value.trim();
    if (value.length < lo || value.length > hi) fail(input, `${label} must be ${lo}-${hi} characters.`);
    return value;
  };

  const meta = {
    title: text("title", 3, 80, "Title"),
    summary: text("summary", 10, 200, "Summary"),
    description: text("description", 20, 3000, "Description"),
    category: form.elements.category.value,
    milestones: [],
  };

  const goalInput = form.elements.goal;
  let goal = 0n;
  if (!/^\d+(\.\d{1,18})?$/.test(goalInput.value.trim())) fail(goalInput, "Enter the goal as a positive ETH amount, e.g. 0.5.");
  else {
    goal = ethers.parseEther(goalInput.value.trim());
    if (goal === 0n) fail(goalInput, "The goal must be greater than zero.");
    if (goal >= 2n ** 128n) fail(goalInput, "The goal is too large.");
  }

  const seconds = (name, max, label) => {
    const input = form.elements[`${name}Value`];
    const n = Number(input.value);
    const total = n * Number(form.elements[`${name}Unit`].value);
    if (!Number.isInteger(n) || n <= 0) fail(input, `${label} must be a whole number.`);
    else if (total < k.minDuration || total > max) fail(input, `${label} must be between ${fmtDuration(k.minDuration)} and ${fmtDuration(max)}.`);
    return total;
  };
  const funding = seconds("funding", k.maxFundingDuration, "Funding period");
  const voting = seconds("voting", k.maxVotingDuration, "Voting period");
  const window_ = seconds("window", k.maxMilestoneWindow, "Submission window");

  const percentages = [];
  form.querySelectorAll(".milestone-editor").forEach((ed, i) => {
    const t = $("[name=msTitle]", ed);
    const d = $("[name=msDescription]", ed);
    const p = $("[name=pct]", ed);
    if (t.value.trim().length < 3) fail(t, `Milestone ${i + 1}: title must be at least 3 characters.`);
    if (d.value.trim().length < 10) fail(d, `Milestone ${i + 1}: describe the deliverable in at least 10 characters.`);
    const bps = Math.round(Number(p.value) * 100);
    if (!(bps > 0)) fail(p, `Milestone ${i + 1}: share must be greater than 0%.`);
    percentages.push(bps);
    meta.milestones.push({ title: t.value.trim(), description: d.value.trim() });
  });
  if (percentages.reduce((a, b) => a + b, 0) !== 10000) errors.push("Milestone shares must add up to exactly 100%.");

  return { ok: errors.length === 0, errors, meta, chain: { goal, funding, voting, window: window_, percentages } };
}

// ---------------------------------------------------------------------------
// View: campaign detail
// ---------------------------------------------------------------------------

async function renderCampaign(id) {
  const viewer = state.account ? `?viewer=${state.account}` : "";
  const [c, activity] = await Promise.all([api(`/api/campaigns/${id}${viewer}`), api(`/api/campaigns/${id}/activity`)]);
  syncClock(c.now);

  // Auto-refresh while something time-sensitive is happening.
  if (!["completed", "failed"].includes(c.phase)) state.timers.push(setInterval(rerender, 15000));

  return h(
    "div",
    {},
    h(
      "div",
      { class: "detail-head" },
      h("div", { class: "row" }, h("a", { href: "#/" }, "← All campaigns"), h("span", { class: "chip" }, c.category || "")),
      h("div", { class: "row" }, h("h1", {}, c.title || `Campaign #${c.id}`), badge(c.phase)),
      h("div", { class: "row muted small" }, h("span", {}, "Created by ", addressLink(c.creator)), verificationBadge(c))
    ),
    h(
      "div",
      { class: "detail" },
      h(
        "div",
        { class: "stack" },
        h("section", { class: "card" }, h("h2", {}, "About"), h("p", { class: "description" }, c.description || "The off-chain description for this campaign is unavailable or failed verification.")),
        h("section", { class: "card" }, h("h2", {}, "Milestones"), milestoneTimeline(c)),
        h("section", { class: "card" }, h("h2", {}, "Activity"), activityList(activity.events))
      ),
      h("aside", { class: "sidebar stack" }, fundingPanel(c), actionPanel(c), contractPanel(c))
    )
  );
}

function verificationBadge(c) {
  if (!c.metadataContent) return h("span", { class: "unverified" }, "⚠ Description not verified");
  // Recompute the hash in the browser rather than trusting the server's verdict.
  const ok = ethers.id(c.metadataContent) === c.metadataHash;
  const el = h("span", { class: ok ? "verified" : "unverified", title: `On-chain hash ${c.metadataHash}` }, ok ? "✓ Description matches on-chain hash" : "⚠ Description hash mismatch");
  // With a wallet on the right network, also read the hash directly from the chain.
  if (ok && state.browserProvider && onCorrectNetwork()) {
    new ethers.Contract(state.config.contractAddress, state.config.abi, state.browserProvider)
      .getCampaign(c.id)
      .then((onchain) => {
        if (onchain.metadataHash === c.metadataHash) el.textContent = "✓ Description verified on-chain via your wallet";
        else {
          el.className = "unverified";
          el.textContent = "⚠ Server and chain disagree";
        }
      })
      .catch(() => {});
  }
  return el;
}

function fundingPanel(c) {
  const pct = percentOf(c.totalRaised, c.goal);
  const rows = [
    ["Goal", fmtEth(c.goal)],
    ["Backers", String(c.backerCount)],
    ["Released to creator", fmtEth(c.totalReleased)],
    ["Still locked", fmtEth(BigInt(c.totalRaised) - BigInt(c.totalReleased))],
  ];
  let timing = null;
  if (c.phase === "funding") timing = h("p", { class: "action-note" }, "Funding closes in ", h("strong", {}, countdown(c.deadline)), ` (${fmtTime(c.deadline)})`);
  return h(
    "section",
    { class: "card" },
    h("div", { class: "big-number" }, fmtEth(c.totalRaised)),
    h("div", { class: "muted small" }, `raised · ${pct}% of goal`),
    h("div", { class: "progress", style: { margin: "10px 0" } }, h("span", { style: { width: `${pct}%` } })),
    timing,
    rows.map(([k, v]) => h("div", { class: "stat-row" }, h("span", {}, k), h("span", {}, v)))
  );
}

function contractPanel(c) {
  return h(
    "section",
    { class: "card small" },
    h("h3", {}, "On-chain record"),
    h("div", { class: "stat-row" }, h("span", {}, "Campaign ID"), h("span", {}, `#${c.id}`)),
    h("div", { class: "stat-row" }, h("span", {}, "Contract"), h("span", {}, addressLink(state.config.contractAddress))),
    h("div", { class: "stat-row" }, h("span", {}, "Voting period"), h("span", {}, fmtDuration(c.votingDuration))),
    h("div", { class: "stat-row" }, h("span", {}, "Submission window"), h("span", {}, fmtDuration(c.milestoneWindow))),
    h("div", { class: "stat-row" }, h("span", {}, "Metadata hash"), h("span", { class: "mono", title: c.metadataHash }, shortHash(c.metadataHash)))
  );
}

function milestoneTimeline(c) {
  const quorumWei = (BigInt(c.totalRaised) * BigInt(state.config.constants.quorumBps)) / 10000n;
  return h(
    "ol",
    { class: "timeline" },
    c.milestones.map((m) => {
      const isCurrent = c.state === "Active" && m.index === c.currentMilestone;
      const share = (BigInt(c.totalRaised) * BigInt(m.percentageBps)) / 10000n;
      const cls = ["milestone", m.state.toLowerCase(), isCurrent ? "current" : ""].join(" ");
      const children = [
        h("span", { class: "milestone-dot" }, m.state === "Approved" ? "✓" : String(m.index + 1)),
        h("h3", {}, m.title || `Milestone ${m.index + 1}`, h("span", { class: `badge ${m.state.toLowerCase()}` }, m.state), isCurrent ? h("span", { class: "badge" }, "Current") : null),
        h("div", { class: "muted small" }, `${fmtPct(m.percentageBps)} of funds`, c.state !== "Funding" ? ` · ${fmtEth(share)}` : "", m.attempts ? ` · attempt ${m.attempts} of ${state.config.constants.maxAttempts}` : ""),
        m.description ? h("p", {}, m.description) : null,
      ];

      if (m.attempts > 0) {
        const yes = BigInt(m.yesWeight);
        const no = BigInt(m.noWeight);
        const total = BigInt(c.totalRaised);
        const yesPct = percentOf(yes, total);
        const noPct = percentOf(no, total);
        const turnoutOk = yes + no >= quorumWei;
        children.push(
          h("div", { class: "vote-bar", "aria-label": "Vote results" }, h("span", { class: "yes", style: { width: `${yesPct}%` } }), h("span", { class: "no", style: { width: `${noPct}%` } })),
          h(
            "div",
            { class: "vote-legend" },
            h("span", {}, `Approve ${fmtEth(yes)} (${yesPct}%)`),
            h("span", {}, `Reject ${fmtEth(no)} (${noPct}%)`),
            h("span", {}, turnoutOk ? "Quorum reached" : `Quorum ${fmtPct(state.config.constants.quorumBps)} not yet reached`)
          )
        );
        if (m.state === "Voting") {
          const cd = countdown(m.voteDeadline, { expired: "closed, awaiting tally" });
          cd.dataset.refresh = "1";
          children.push(h("p", { class: "small" }, "Voting ends in ", h("strong", {}, cd)));
        }
      }
      if (m.proof) {
        const ok = m.proofContent && ethers.id(m.proofContent) === m.proofHash;
        children.push(
          h(
            "div",
            { class: "proof" },
            h("div", { class: "row small" }, h("strong", {}, "Creator's proof of delivery"), h("span", { class: ok ? "verified" : "unverified" }, ok ? "✓ matches on-chain hash" : "⚠ hash mismatch")),
            h("p", {}, m.proof.summary),
            m.proof.links?.length ? h("ul", {}, m.proof.links.map((l) => h("li", {}, externalLink(l, l)))) : null
          )
        );
      } else if (m.attempts > 0) {
        children.push(h("div", { class: "proof muted" }, "Proof document unavailable or failed verification."));
      }
      return h("li", { class: cls }, children);
    })
  );
}

function activityList(events) {
  if (!events.length) return h("p", { class: "muted" }, "No on-chain activity indexed yet.");
  const describe = (e) => {
    const who = e.actor ? addressLink(e.actor) : null;
    const a = e.args;
    switch (e.event) {
      case "CampaignCreated": return [who, ` created the campaign with a goal of ${fmtEth(a.goal)}`];
      case "Contributed": return [who, ` contributed ${fmtEth(a.amount)}`];
      case "CampaignStateChanged": return [`Campaign became ${["Funding", "Active", "Completed", "Failed"][a.state]}`];
      case "MilestoneSubmitted": return [`Milestone ${a.milestoneIndex + 1} submitted for review (attempt ${a.attempt})`];
      case "Voted": return [who, ` voted to ${a.approve ? "approve" : "reject"} milestone ${a.milestoneIndex + 1} (weight ${fmtEth(a.weight)})`];
      case "MilestoneFinalized": return [`Milestone ${a.milestoneIndex + 1} ${a.approved ? `approved, ${fmtEth(a.releasedAmount)} released` : "rejected"}`];
      case "FundsWithdrawn": return [who, ` withdrew ${fmtEth(a.amount)}`];
      case "Refunded": return [who, ` was refunded ${fmtEth(a.amount)}`];
      default: return [e.event];
    }
  };
  return h("ul", { class: "activity" }, events.map((e) => h("li", {}, h("span", {}, describe(e)), h("span", { class: "muted small" }, fmtTime(e.time), " · ", txLink(e.txHash, "tx")))));
}

function actionPanel(c) {
  const v = c.viewer;
  const panel = h("section", { class: "card action-group" }, h("h3", {}, "Actions"));
  const add = (...nodes) => append(panel, nodes);
  const button = (label, opts, cls = "btn btn-primary btn-block") => h("button", { class: cls, type: "button", onclick: opts }, label);
  const run = (action, label, send) => async () => {
    const receipt = await sendTx({ action, campaignId: c.id, label, send });
    if (receipt) {
      await rerender();
      setTimeout(rerender, 5000); // pick up the indexer's copy of the new events
    }
  };

  if (!state.account) {
    add(h("p", { class: "action-note" }, "Connect your wallet to back, vote or claim funds."), button("Connect wallet", connectWallet));
    return panel;
  }

  if (v && (BigInt(v.contribution) > 0n || v.isCreator)) {
    add(
      h("div", { class: "stat-row" }, h("span", {}, "Your role"), h("span", {}, v.isCreator ? "Creator" : "Backer")),
      !v.isCreator ? h("div", { class: "stat-row" }, h("span", {}, "Your contribution"), h("span", {}, fmtEth(v.contribution))) : null,
      !v.isCreator && c.totalRaised !== "0" ? h("div", { class: "stat-row" }, h("span", {}, "Voting weight"), h("span", {}, `${percentOf(v.contribution, c.totalRaised)}%`)) : null
    );
  }

  let any = false;
  const section = (...nodes) => {
    any = true;
    add(h("hr", { class: "divider" }), ...nodes);
  };

  switch (c.phase) {
    case "funding":
      if (v.isCreator) {
        section(h("p", { class: "action-note" }, "You can cancel while funding is open. Every backer will be able to reclaim their contribution."), button("Cancel campaign", run("cancelCampaign", "Cancel campaign", (k) => k.cancelCampaign(c.id)), "btn btn-danger btn-block"));
      } else {
        section(contributeForm(c, run));
      }
      break;
    case "funding-ended":
      section(h("p", { class: "action-note" }, BigInt(c.totalRaised) >= BigInt(c.goal) ? "The goal was reached. Finalize to start the milestone phase." : "The goal was not reached. Finalize so backers can claim refunds."), button("Finalize funding", run("finalizeFunding", "Finalize funding", (k) => k.finalizeFunding(c.id))));
      break;
    case "in-progress":
    case "revising":
      if (v.isCreator) section(proofForm(c, run));
      else {
        const cd = countdown(c.submissionDeadline, { expired: "now" });
        cd.dataset.refresh = "1";
        section(h("p", { class: "action-note" }, "Waiting for the creator to submit milestone ", h("strong", {}, String(c.currentMilestone + 1)), ". Submission window closes in ", h("strong", {}, cd), "."));
      }
      break;
    case "creator-inactive":
      section(h("p", { class: "action-note" }, "The creator missed the submission window. Anyone can mark the campaign as failed so backers can reclaim the locked funds."), button("Mark campaign as failed", run("failInactiveCampaign", "Mark campaign as failed", (k) => k.failInactiveCampaign(c.id)), "btn btn-danger btn-block"));
      break;
    case "voting":
      if (BigInt(v.contribution) > 0n && !v.hasVotedCurrent) {
        section(
          h("p", { class: "action-note" }, `Review the proof for milestone ${c.currentMilestone + 1}, then vote. Your vote is weighted by your contribution.`),
          h("div", { class: "grid-2" }, button("Approve", run("vote", "Approve milestone", (k) => k.vote(c.id, true)), "btn btn-success btn-block"), button("Reject", run("vote", "Reject milestone", (k) => k.vote(c.id, false)), "btn btn-danger btn-block"))
        );
      } else if (v.hasVotedCurrent) {
        section(h("p", { class: "action-note" }, "✓ You have voted in this round."));
      } else {
        section(h("p", { class: "action-note" }, v.isCreator ? "Backers are reviewing your submission." : "Only backers can vote on milestones."));
      }
      break;
    case "vote-ended":
      section(h("p", { class: "action-note" }, `Voting on milestone ${c.currentMilestone + 1} has closed. Anyone can tally the result.`), button("Tally vote", run("finalizeMilestone", "Tally milestone vote", (k) => k.finalizeMilestone(c.id))));
      break;
    default:
      break;
  }

  if (v.isCreator && BigInt(c.withdrawable) > 0n) {
    section(h("p", { class: "action-note" }, `${fmtEth(c.withdrawable)} has been approved for you.`), button(`Withdraw ${fmtEth(c.withdrawable)}`, run("withdraw", "Withdraw funds", (k) => k.withdraw(c.id))));
  }
  if (c.state === "Failed" && BigInt(v.refundable) > 0n) {
    section(h("p", { class: "action-note" }, "This campaign failed. You can reclaim your share of the funds that were never released."), button(`Claim refund of ${fmtEth(v.refundable)}`, run("claimRefund", "Claim refund", (k) => k.claimRefund(c.id))));
  }
  if (!any) add(h("p", { class: "action-note" }, c.state === "Completed" ? "This campaign is complete. All milestones were approved." : c.state === "Failed" ? "This campaign has failed. Backers with a remaining balance can claim refunds." : "No actions are available to you right now."));
  return panel;
}

function contributeForm(c, run) {
  const input = h("input", { id: "amount", inputmode: "decimal", placeholder: "0.1", "aria-describedby": "amount-error" });
  const error = h("div", { class: "field-error", id: "amount-error" });
  const submit = h("button", { class: "btn btn-primary btn-block", type: "submit" }, "Back this campaign");
  const form = h(
    "form",
    { novalidate: true },
    h("div", { class: "field" }, h("label", { for: "amount" }, "Amount (ETH)"), input, error, h("div", { class: "hint" }, "Your contribution becomes your voting weight on every milestone.")),
    submit
  );
  form.addEventListener("submit", async (ev) => {
    ev.preventDefault();
    error.textContent = "";
    input.classList.remove("invalid");
    const raw = input.value.trim();
    const invalid = (msg) => {
      error.textContent = msg;
      input.classList.add("invalid");
    };
    if (!/^\d+(\.\d{1,18})?$/.test(raw)) return invalid("Enter a positive amount with up to 18 decimals.");
    const wei = ethers.parseEther(raw);
    if (wei === 0n) return invalid("The amount must be greater than zero.");
    if (state.browserProvider && state.account) {
      const balance = await state.browserProvider.getBalance(state.account).catch(() => null);
      if (balance !== null && wei >= balance) return invalid(`Your balance is ${fmtEth(balance)}, which must also cover gas.`);
    }
    submit.disabled = true;
    await run("contribute", `Contribute ${fmtEth(wei)}`, (k) => k.contribute(c.id, { value: wei }))();
    submit.disabled = false;
  });
  return form;
}

function proofForm(c, run) {
  const m = c.milestones[c.currentMilestone];
  const summary = h("textarea", { id: "proof-summary", rows: "4", maxlength: "3000", placeholder: "What was delivered, and how backers can check it" });
  const links = h("textarea", { id: "proof-links", rows: "2", placeholder: "https://github.com/…\nhttps://demo.example.com" });
  const error = h("div", { class: "field-error" });
  const submit = h("button", { class: "btn btn-primary btn-block", type: "submit" }, m.state === "Rejected" ? "Resubmit for review" : "Submit for review");
  const cd = countdown(c.submissionDeadline, { expired: "now" });
  cd.dataset.refresh = "1";
  const form = h(
    "form",
    { novalidate: true },
    h("p", { class: "action-note" }, m.state === "Rejected" ? `Backers rejected attempt ${m.attempts}. Address their concerns and resubmit (${state.config.constants.maxAttempts - m.attempts} attempt(s) left).` : `Deliver milestone ${c.currentMilestone + 1}: ${m.title || ""}`, " Window closes in ", h("strong", {}, cd), "."),
    h("div", { class: "field" }, h("label", { for: "proof-summary" }, "Proof of delivery"), summary),
    h("div", { class: "field" }, h("label", { for: "proof-links" }, "Evidence links (one per line, optional)"), links),
    error,
    submit
  );
  form.addEventListener("submit", async (ev) => {
    ev.preventDefault();
    error.textContent = "";
    const text = summary.value.trim();
    const urls = links.value.split("\n").map((s) => s.trim()).filter(Boolean);
    if (text.length < 20) return (error.textContent = "Describe the delivery in at least 20 characters.");
    if (urls.length > 5) return (error.textContent = "Add at most 5 links.");
    for (const u of urls) {
      let ok = false;
      try {
        ok = ["http:", "https:"].includes(new URL(u).protocol);
      } catch { /* invalid URL */ }
      if (!ok) return (error.textContent = `Not a valid http(s) link: ${u}`);
    }
    submit.disabled = true;
    try {
      await requireReadyWallet();
      const hash = await publishMetadata({ type: "milestone-proof", author: state.account, createdAt: chainNow(), campaignId: c.id, milestoneIndex: c.currentMilestone, summary: text, links: urls });
      await run("submitMilestone", `Submit milestone ${c.currentMilestone + 1}`, (k) => k.submitMilestone(c.id, hash))();
    } catch (e) {
      error.textContent = friendlyError(e);
    } finally {
      submit.disabled = false;
    }
  });
  return form;
}

// ---------------------------------------------------------------------------
// View: my activity
// ---------------------------------------------------------------------------

const ACTION_LABELS = {
  createCampaign: "Create campaign",
  contribute: "Contribute",
  finalizeFunding: "Finalize funding",
  cancelCampaign: "Cancel campaign",
  submitMilestone: "Submit milestone",
  vote: "Vote",
  finalizeMilestone: "Tally vote",
  failInactiveCampaign: "Fail inactive campaign",
  withdraw: "Withdraw",
  claimRefund: "Claim refund",
};

async function renderMe() {
  if (!state.account) {
    return h("div", { class: "card empty" }, h("h2", {}, "My activity"), h("p", { class: "muted" }, "Connect your wallet to see your campaigns, contributions and transaction history."), h("button", { class: "btn btn-primary", type: "button", onclick: async () => (await connectWallet()) && render() }, "Connect wallet"));
  }
  const [history, list] = await Promise.all([api(`/api/history/${state.account}`), api("/api/campaigns?limit=100")]);
  syncClock(list.now);
  const mine = list.campaigns.filter((c) => c.creator === state.account);
  const backedIds = new Set(history.events.filter((e) => e.event === "Contributed").map((e) => e.campaignId));
  const backed = list.campaigns.filter((c) => backedIds.has(c.id));
  if (history.transactions.some((t) => t.status === "pending")) state.timers.push(setInterval(rerender, 5000));

  const txTable = history.transactions.length
    ? h(
        "div",
        { class: "table-wrap" },
        h(
          "table",
          {},
          h("thead", {}, h("tr", {}, ["Time", "Action", "Campaign", "Status", "Gas used", "Transaction"].map((t) => h("th", {}, t)))),
          h(
            "tbody",
            {},
            history.transactions.map((t) =>
              h(
                "tr",
                {},
                h("td", {}, fmtTime(t.submitted_at)),
                h("td", {}, ACTION_LABELS[t.action] || t.action),
                h("td", {}, t.campaign_id !== null ? h("a", { href: `#/campaign/${t.campaign_id}` }, `#${t.campaign_id}`) : "–"),
                h("td", {}, h("span", { class: `badge ${t.status}` }, t.status), t.error ? h("div", { class: "field-error" }, ERROR_MESSAGES[t.error] || t.error) : null),
                h("td", {}, t.gas_used ? t.gas_used.toLocaleString() : "–"),
                h("td", {}, txLink(t.hash))
              )
            )
          )
        )
      )
    : h("p", { class: "muted" }, "No transactions sent from this browser yet.");

  return h(
    "div",
    { class: "stack" },
    h("div", { class: "hero" }, h("div", {}, h("h1", {}, "My activity"), h("p", { class: "muted mono" }, state.account))),
    h("section", { class: "card" }, h("h2", {}, "Transaction history"), h("p", { class: "action-note" }, "Status of every transaction you sent through MileFund, including failed ones."), txTable),
    h("section", { class: "card" }, h("h2", {}, "On-chain events involving you"), activityList(history.events)),
    h("section", {}, h("h2", {}, `Campaigns I created (${mine.length})`), mine.length ? h("div", { class: "campaign-grid" }, mine.map(campaignCard)) : h("p", { class: "muted" }, "None yet.")),
    h("section", {}, h("h2", {}, `Campaigns I backed (${backed.length})`), backed.length ? h("div", { class: "campaign-grid" }, backed.map(campaignCard)) : h("p", { class: "muted" }, "None yet."))
  );
}

// ---------------------------------------------------------------------------
// Boot
// ---------------------------------------------------------------------------

async function boot() {
  try {
    state.config = await api("/api/config");
  } catch (e) {
    $("#view").replaceChildren(errorState(e.message));
    return;
  }
  $("#footer-contract").replaceChildren(`${state.config.networkName} · contract `, addressLink(state.config.contractAddress));

  $("#wallet-button").addEventListener("click", async () => {
    if (!state.account) {
      if (await connectWallet()) rerender();
      return;
    }
    await navigator.clipboard?.writeText(state.account).catch(() => {});
    toast({ title: "Address copied", body: state.account, timeout: 2500 });
  });

  if (hasWallet()) {
    window.ethereum.on?.("accountsChanged", async () => {
      await refreshWallet();
      render();
    });
    window.ethereum.on?.("chainChanged", async () => {
      await refreshWallet();
      render();
    });
    await refreshWallet().catch(() => {});
  } else {
    updateWalletUI();
  }

  window.addEventListener("hashchange", render);
  render();
}

document.addEventListener("DOMContentLoaded", boot);
