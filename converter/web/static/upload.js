/* The upload screen of the hosted converter.

   It owns exactly one job: get one .accdt file to /api/convert and hand the
   result to the viewer, which is the same component the offline single-file
   report runs. Nothing about the converter itself is shipped to the browser -
   the page receives a conversion result, never a translator. */
(function () {
  "use strict";

  var viewer = window.__ACCESS_VIEWER__;
  var landing = document.getElementById("landing");
  var app = document.getElementById("app");
  var limits = { max_upload_bytes: 48 * 1024 * 1024, accepted_suffixes: [".accdt"], requires_token: false };
  var busy = false;
  var errorMessage = null;
  var token = "";

  function t(path, args) { return viewer.t(path, args); }

  function esc(text) {
    return String(text === null || text === undefined ? "" : text)
      .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;");
  }

  function megabytes(bytes) { return Math.floor(bytes / (1024 * 1024)); }

  function renderLanding() {
    var languageOptions = viewer.languages.map(function (language) {
      return '<option value="' + esc(language.code) + '"' +
        (language.code === viewer.currentLanguage() ? " selected" : "") + ">" +
        esc(language.label) + "</option>";
    }).join("");

    var body = [];
    body.push('<div class="lead"><div>');
    body.push("<h1>" + esc(t("ui.uploadTitle")) + "</h1>");
    body.push('<p class="muted">' + esc(t("ui.subtitleWeb")) + "</p></div>");
    body.push('<label class="langpick"><span class="visually-hidden">' +
      esc(t("ui.language")) + '</span><select id="landinglang">' +
      languageOptions + "</select></label></div>");

    if (errorMessage) {
      body.push('<div class="note bad"><strong>' + esc(t("ui.uploadErrorTitle")) +
        "</strong><br>" + esc(errorMessage) + "</div>");
    }

    if (busy) {
      body.push('<div class="card busy"><span class="spinner"></span>' +
        esc(t("ui.converting")) + "</div>");
    } else {
      if (limits.requires_token) {
        body.push('<div class="tokenrow"><input id="token" type="password" ' +
          'autocomplete="off" placeholder="Access token" value="' + esc(token) +
          '"></div>');
      }
      body.push('<div class="dropzone" id="dropzone" tabindex="0" role="button">');
      body.push('<div class="prompt">' + esc(t("ui.uploadPrompt")) + "</div>");
      body.push("<button type=\"button\" id=\"pick\">" + esc(t("ui.uploadButton")) + "</button>");
      body.push('<div class="limit">' +
        esc(t("ui.sizeLimit", { mb: megabytes(limits.max_upload_bytes) })) + "</div>");
      body.push('<input type="file" id="file" accept=".accdt">');
      body.push("</div>");
    }

    body.push('<div class="card"><h3 style="margin-top:0">' +
      esc(t("ui.privacyTitle")) + "</h3><p>" + esc(t("ui.privacyBody")) +
      '</p><p class="muted">' + esc(t("ui.privacyOffline")) + "</p></div>");
    body.push('<div class="card"><h3 style="margin-top:0">' +
      esc(t("ui.whatIsAccdt")) + "</h3><p>" + esc(t("ui.whatIsAccdtBody")) + "</p></div>");

    landing.innerHTML = body.join("");
    wireLanding();
  }

  function wireLanding() {
    var picker = document.getElementById("landinglang");
    if (picker) {
      picker.addEventListener("change", function (event) {
        viewer.setLanguage(event.target.value);
      });
    }
    var tokenField = document.getElementById("token");
    if (tokenField) {
      tokenField.addEventListener("input", function (event) {
        token = event.target.value;
      });
    }
    var zone = document.getElementById("dropzone");
    var input = document.getElementById("file");
    if (!zone || !input) { return; }

    zone.addEventListener("click", function () { input.click(); });
    zone.addEventListener("keydown", function (event) {
      if (event.key === "Enter" || event.key === " ") {
        event.preventDefault();
        input.click();
      }
    });
    input.addEventListener("change", function () {
      if (input.files && input.files[0]) { send(input.files[0]); }
    });
    ["dragenter", "dragover"].forEach(function (type) {
      zone.addEventListener(type, function (event) {
        event.preventDefault();
        zone.classList.add("over");
      });
    });
    ["dragleave", "drop"].forEach(function (type) {
      zone.addEventListener(type, function (event) {
        event.preventDefault();
        zone.classList.remove("over");
      });
    });
    zone.addEventListener("drop", function (event) {
      var files = event.dataTransfer && event.dataTransfer.files;
      if (files && files[0]) { send(files[0]); }
    });
  }

  function send(file) {
    errorMessage = null;
    var suffix = ("." + file.name.split(".").pop()).toLowerCase();
    if (limits.accepted_suffixes.indexOf(suffix) < 0) {
      errorMessage = t("ui.sizeLimit", { mb: megabytes(limits.max_upload_bytes) });
      renderLanding();
      return;
    }
    if (file.size > limits.max_upload_bytes) {
      errorMessage = t("ui.sizeLimit", { mb: megabytes(limits.max_upload_bytes) });
      renderLanding();
      return;
    }

    busy = true;
    renderLanding();

    var form = new FormData();
    form.append("file", file, file.name);
    var headers = {};
    if (token) { headers["X-Access-Token"] = token; }

    fetch("/api/convert", { method: "POST", body: form, headers: headers })
      .then(function (response) {
        return response.json().then(function (payload) {
          return { ok: response.ok, payload: payload };
        }).catch(function () {
          return { ok: false, payload: null };
        });
      })
      .then(function (result) {
        busy = false;
        if (!result.ok || !result.payload || result.payload.error) {
          errorMessage = result.payload && result.payload.error
            ? result.payload.error.message
            : "HTTP error";
          renderLanding();
          return;
        }
        show(result.payload);
      })
      .catch(function (error) {
        busy = false;
        errorMessage = String(error && error.message ? error.message : error);
        renderLanding();
      });
  }

  function show(payload) {
    landing.hidden = true;
    app.hidden = false;
    document.getElementById("brandtitle").textContent = payload.meta.file_name;
    viewer.start(payload);
    document.getElementById("another").textContent = t("ui.uploadAnother");
    document.getElementById("another").onclick = function () {
      /* The result is dropped from the page as well as from the server: going
         back to the upload screen leaves nothing of the previous database in
         this tab beyond what the browser has already discarded. */
      app.hidden = true;
      landing.hidden = false;
      renderLanding();
    };
  }

  /* Re-render the landing screen when the viewer's language picker changes. */
  window.__ACCESS_ON_LANGUAGE__ = function () {
    if (!landing.hidden) { renderLanding(); }
    var another = document.getElementById("another");
    if (another && !app.hidden) { another.textContent = t("ui.uploadAnother"); }
  };

  fetch("/api/limits")
    .then(function (response) { return response.json(); })
    .then(function (payload) { limits = payload; renderLanding(); })
    .catch(function () { renderLanding(); });

  renderLanding();
})();
