/* IMOTH Cancellations — shared behaviour (no dependencies).
   Sidebar collapse + mobile drawer, confirmation dialog, double-submit protection,
   clickable table rows, dismissible alerts. */
(function () {
  "use strict";
  const root = document.documentElement;

  function store(key, value) {
    try {
      if (value === undefined) return window.localStorage.getItem(key);
      window.localStorage.setItem(key, value);
    } catch (e) { return null; }  // private mode / blocked storage: just don't remember
    return null;
  }

  /* ---- Sidebar ---- */
  if (store("imoth.sidebar") === "collapsed") root.classList.add("sidebar-collapsed");
  document.addEventListener("click", function (e) {
    const collapse = e.target.closest("[data-sidebar-collapse]");
    if (collapse) {
      const collapsed = root.classList.toggle("sidebar-collapsed");
      collapse.setAttribute("aria-expanded", String(!collapsed));
      store("imoth.sidebar", collapsed ? "collapsed" : "open");
    }
    const open = e.target.closest("[data-nav-open]");
    if (open) { root.classList.add("nav-open"); open.setAttribute("aria-expanded", "true"); }
    if (e.target.closest("[data-nav-close]")) {
      root.classList.remove("nav-open");
      document.querySelectorAll("[data-nav-open]").forEach(b => b.setAttribute("aria-expanded", "false"));
    }
  });
  document.addEventListener("keydown", function (e) {
    if (e.key === "Escape") root.classList.remove("nav-open");
  });

  /* ---- Confirmation dialog (replaces window.confirm) ---- */
  const dlg = document.getElementById("confirm-dialog");
  function askConfirm(message, opts) {
    opts = opts || {};
    if (!dlg || typeof dlg.showModal !== "function") return Promise.resolve(window.confirm(message));
    dlg.querySelector("[data-confirm-title]").textContent = opts.title || "Please confirm";
    dlg.querySelector("[data-confirm-message]").textContent = message;
    const ok = dlg.querySelector("[data-confirm-ok]");
    ok.textContent = opts.ok || "Confirm";
    ok.className = "btn " + (opts.danger ? "btn-danger" : "btn-primary");
    dlg.querySelector(".modal__icon").classList.toggle("modal__icon--danger", !!opts.danger);
    return new Promise(function (resolve) {
      function done(result) {
        dlg.removeEventListener("close", onClose);
        ok.removeEventListener("click", onOk);
        if (dlg.open) dlg.close();
        resolve(result);
      }
      function onOk() { done(true); }
      function onClose() { done(false); }
      ok.addEventListener("click", onOk);
      dlg.addEventListener("close", onClose);
      dlg.showModal();
      ok.focus();
    });
  }
  window.imothConfirm = askConfirm;

  function confirmOpts(el) {
    return { title: el.dataset.confirmTitle, ok: el.dataset.confirmOk, danger: el.dataset.confirmDanger !== undefined };
  }

  // Forms: <form data-confirm="Send this?">  — the clicked submit button is preserved.
  document.addEventListener("submit", function (e) {
    const form = e.target;
    if (!form.matches("form[data-confirm]") || form.dataset.confirmed === "1") return;
    e.preventDefault();
    const submitter = e.submitter;
    askConfirm(form.dataset.confirm, confirmOpts(form)).then(function (yes) {
      if (!yes) return;
      form.dataset.confirmed = "1";
      if (submitter && submitter.name) {
        const hidden = Object.assign(document.createElement("input"), { type: "hidden", name: submitter.name, value: submitter.value });
        form.appendChild(hidden);
      }
      lockForm(form, submitter);
      HTMLFormElement.prototype.submit.call(form);
    });
  }, true);

  // Buttons: <button data-confirm-click="Dismiss?">
  document.addEventListener("click", function (e) {
    const btn = e.target.closest("[data-confirm-click]");
    if (!btn || btn.dataset.confirmed === "1") return;
    e.preventDefault();
    askConfirm(btn.dataset.confirmClick, confirmOpts(btn)).then(function (yes) {
      if (!yes) return;
      btn.dataset.confirmed = "1";
      btn.click();
    });
  }, true);

  /* ---- Double-submit protection ---- */
  function lockForm(form, submitter) {
    if (form.method.toLowerCase() === "get") return;
    // Defer so the clicked button's name/value is still included in the submission.
    setTimeout(function () {
      form.querySelectorAll("button[type=submit], button:not([type]), input[type=submit]").forEach(function (b) {
        b.disabled = true;
      });
      if (submitter && submitter.dataset.busyLabel) {
        submitter.classList.add("is-busy");
        const label = submitter.querySelector("[data-label]");
        if (label) label.textContent = submitter.dataset.busyLabel;
      }
    }, 0);
  }
  document.addEventListener("submit", function (e) {
    if (e.defaultPrevented) return;
    lockForm(e.target, e.submitter);
  });
  // Re-enable if the user comes back with the browser's Back button.
  window.addEventListener("pageshow", function (e) {
    if (!e.persisted) return;
    document.querySelectorAll("form button[disabled]").forEach(b => { b.disabled = false; b.classList.remove("is-busy"); });
  });

  /* ---- Clickable rows ---- */
  document.addEventListener("click", function (e) {
    const row = e.target.closest("tr[data-href]");
    if (!row || e.target.closest("a, button, input, select, label")) return;
    if (e.metaKey || e.ctrlKey) window.open(row.dataset.href, "_blank");
    else window.location.href = row.dataset.href;
  });

  /* ---- Dismissible alerts ---- */
  document.addEventListener("click", function (e) {
    const close = e.target.closest("[data-dismiss-alert]");
    if (close) close.closest(".alert").remove();
  });
})();

/* ---- Dropzones: show the chosen file, flag wrong type/size before upload ---- */
(function () {
  "use strict";
  function fmt(bytes) {
    return bytes > 1048576 ? (bytes / 1048576).toFixed(1) + " MB" : Math.max(1, Math.round(bytes / 1024)) + " KB";
  }
  document.querySelectorAll("[data-dropzone]").forEach(function (zone) {
    const input = zone.querySelector("input[type=file]");
    const field = zone.closest(".field");
    const pill = field.querySelector("[data-file-pill]");
    const err = field.querySelector("[data-file-error]");
    const allowed = (zone.dataset.ext || "").split(",").map(s => s.trim().toLowerCase()).filter(Boolean);
    const maxBytes = parseFloat(zone.dataset.maxMb || "0") * 1048576;
    function update() {
      const f = input.files && input.files[0];
      pill.hidden = !f;
      err.hidden = true;
      pill.classList.remove("is-invalid");
      if (!f) return;
      pill.querySelector("[data-file-name]").textContent = f.name;
      pill.querySelector("[data-file-size]").textContent = fmt(f.size);
      const ext = (f.name.split(".").pop() || "").toLowerCase();
      let problem = "";
      if (allowed.length && allowed.indexOf(ext) === -1) problem = "." + ext + " files are not accepted. Allowed: " + allowed.join(", ") + ".";
      else if (maxBytes && f.size > maxBytes) problem = "This file is larger than " + zone.dataset.maxMb + " MB.";
      if (problem) { err.textContent = problem; err.hidden = false; pill.classList.add("is-invalid"); }
      zone.dispatchEvent(new CustomEvent("filechange", { bubbles: true }));
    }
    input.addEventListener("change", update);
    ["dragenter", "dragover"].forEach(ev => zone.addEventListener(ev, () => zone.classList.add("is-over")));
    ["dragleave", "drop"].forEach(ev => zone.addEventListener(ev, () => zone.classList.remove("is-over")));
    pill.querySelector("[data-file-clear]").addEventListener("click", function () { input.value = ""; update(); });
  });
})();

/* ---- Password fields: show / hide what was typed ---- */
(function () {
  "use strict";
  const svg = (paths) => '<svg class="icon" width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" ' +
    'stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true" focusable="false">' + paths + "</svg>";
  const EYE = svg('<path d="M2 12s3-7 10-7 10 7 10 7-3 7-10 7-10-7-10-7Z"/><circle cx="12" cy="12" r="3"/>');
  const EYE_OFF = svg('<path d="M9.88 9.88a3 3 0 1 0 4.24 4.24"/><path d="M10.73 5.08A10.43 10.43 0 0 1 12 5c7 0 10 7 10 7a13.16 13.16 0 0 1-1.67 2.68"/>' +
    '<path d="M6.61 6.61A13.53 13.53 0 0 0 2 12s3 7 10 7a9.74 9.74 0 0 0 5.39-1.61"/><line x1="2" x2="22" y1="2" y2="22"/>');
  document.querySelectorAll("input[type=password]").forEach(function (input) {
    const wrap = document.createElement("div");
    wrap.className = "password-wrap";
    input.parentNode.insertBefore(wrap, input);
    wrap.appendChild(input);
    const btn = document.createElement("button");
    btn.type = "button";
    btn.className = "password-toggle";
    function show(visible) {
      input.type = visible ? "text" : "password";
      btn.innerHTML = visible ? EYE_OFF : EYE;
      btn.setAttribute("aria-label", visible ? "Hide password" : "Show password");
      btn.title = btn.getAttribute("aria-label");
      btn.setAttribute("aria-pressed", visible ? "true" : "false");
    }
    btn.addEventListener("click", function () { show(input.type === "password"); input.focus(); });
    // Never submit or leave the page with the password showing.
    if (input.form) input.form.addEventListener("submit", function () { show(false); });
    window.addEventListener("pagehide", function () { show(false); });
    show(false);
    wrap.appendChild(btn);
  });
})();
