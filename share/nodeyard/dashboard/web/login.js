// The sign-in page. The password is sent once over a same-origin POST and never stored by this page.
(function () {
  "use strict";
  var form = document.getElementById("form"), key = document.getElementById("key"), err = document.getElementById("error"),
      go = document.getElementById("go"), peek = document.getElementById("peek");

  fetch("/api/auth", { cache: "no-store" }).then(function (r) { return r.json(); }).then(function (a) {
    if (a && (!a.enabled || a.authenticated)) location.replace("/");
  }).catch(function () { /* stay on the page */ });

  peek.addEventListener("click", function () {
    var show = key.type === "password";
    key.type = show ? "text" : "password";
    peek.textContent = show ? "Hide" : "Show";
    key.focus();
  });

  form.addEventListener("submit", function (e) {
    e.preventDefault();
    err.textContent = "";
    go.disabled = true;
    go.textContent = "Checking…";
    fetch("/api/login", {
      method: "POST", cache: "no-store",
      headers: { "Content-Type": "application/json", "X-Nodeyard": "1" },
      body: JSON.stringify({ password: key.value }),
    }).then(function (r) { return r.json().then(function (j) { return { status: r.status, body: j }; }); }).then(function (res) {
      if (res.body && res.body.ok) { location.replace("/"); return; }
      err.textContent = (res.body && res.body.error) || "Couldn't sign in.";
      form.classList.remove("shake"); void form.offsetWidth; form.classList.add("shake");
      key.select();
    }).catch(function () {
      err.textContent = "Couldn't reach the dashboard server.";
    }).then(function () {
      go.disabled = false;
      go.textContent = "Sign in";
    });
  });
})();
