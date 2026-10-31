// The visuals access secret for the pages: a field under the page header to enter it (kept in
// this browser's local storage), and fetches that send it in the X-Visuals-Access header. The
// pictures are fetched that way too and shown from memory (object URLs): a plain <img src> can't
// send a header, and the secret never goes into a URL.
(function () {
  const KEY = "visualsAccess";
  const listeners = [];
  let secret = "";
  try { secret = localStorage.getItem(KEY) || ""; } catch (e) {}

  const css = document.createElement("style");
  css.textContent = `
    .visuals { display: flex; gap: 8px; align-items: center; flex-wrap: wrap; padding: 8px 16px;
               background: var(--panel); border-bottom: 1px solid var(--line); font-size: 13px; }
    .visuals input { font: inherit; color: inherit; background: var(--bg); border: 1px solid var(--line);
                     border-radius: 5px; padding: 4px 8px; width: min(340px, 100%); }
    .visuals.needed { background: color-mix(in srgb, var(--error) 12%, var(--panel)); }
    .visuals .state { color: var(--muted); }
    .visuals.needed .state { color: var(--error); }`;
  document.head.append(css);

  const bar = document.createElement("form");
  bar.className = "visuals";
  bar.innerHTML = `<label for="visuals-secret"><b>Visuals access</b></label>
    <input id="visuals-secret" type="password" autocomplete="current-password" placeholder="the secret for the pictures">
    <button type="submit">Save</button><button type="button" id="visuals-forget">Forget</button>
    <span class="state"></span>`;
  const input = bar.querySelector("input");
  const state = bar.querySelector(".state");
  input.value = secret;

  function show(text, needed) {
    state.textContent = text;
    bar.classList.toggle("needed", !!needed);
  }

  function set(value) {
    secret = value.trim();
    try { secret ? localStorage.setItem(KEY, secret) : localStorage.removeItem(KEY); } catch (e) {}
    input.value = secret;
    show(secret ? "saved in this browser" : "enter it to see pictures", !secret);
    listeners.forEach((f) => f());
  }

  bar.addEventListener("submit", (e) => { e.preventDefault(); set(input.value); });
  bar.querySelector("#visuals-forget").addEventListener("click", () => set(""));
  document.addEventListener("DOMContentLoaded", () => {
    document.querySelector("header").after(bar);
    show(secret ? "" : "enter it to see pictures", !secret);
  });

  // fetch() with the secret; a 401 / 503 shows in the bar and throws.
  async function visualsFetch(url, options = {}) {
    const headers = Object.assign({}, options.headers, secret ? { "X-Visuals-Access": secret } : {});
    const r = await fetch(url, Object.assign({}, options, { headers }));
    if (r.status === 401 || r.status === 503) {
      let detail = "";
      try { detail = (await r.json()).detail; } catch (e) {}
      show(r.status === 503 ? detail || "the service has no secret set" : secret ? "wrong secret" : "enter it to see pictures", true);
      const error = new Error(r.status === 401 ? "the visuals access secret is missing or wrong" : detail);
      error.visuals = true;
      throw error;
    }
    if (r.ok && bar.classList.contains("needed")) show("", false);
    return r;
  }

  // A picture as an object URL (revoke it with URL.revokeObjectURL when done).
  async function visualsImage(url) {
    const r = await visualsFetch(url);
    if (!r.ok) throw new Error(r.status + " " + r.statusText);
    return URL.createObjectURL(await r.blob());
  }

  window.visuals = { fetch: visualsFetch, image: visualsImage, onChange: (f) => listeners.push(f), has: () => !!secret };
})();
