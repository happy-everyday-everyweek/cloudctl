(function () {
  "use strict";
  var TOKEN_KEY = "cloudctl_dev_token";
  var S = { token: localStorage.getItem(TOKEN_KEY) || "", timer: null, streaming: false };

  function $(id) { return document.getElementById(id); }

  function setOut(el, val) {
    el.textContent = typeof val === "string" ? val : JSON.stringify(val, null, 2);
  }

  async function api(path, opts) {
    opts = opts || {};
    var headers = { "Content-Type": "application/json" };
    if (S.token) { headers["X-Token"] = S.token; }
    var res = await fetch(path, {
      method: opts.method || "GET",
      headers: headers,
      body: opts.body ? JSON.stringify(opts.body) : undefined,
      credentials: "same-origin"
    });
    var text = await res.text();
    var data;
    try { data = JSON.parse(text); } catch (e) { data = { ok: false, err: text }; }
    if (res.status === 401) { showLogin("令牌无效或未登录"); }
    return data;
  }

  function withToken(path) {
    if (!S.token) { return path; }
    var sep = path.indexOf("?") >= 0 ? "&" : "?";
    return path + sep + "token=" + encodeURIComponent(S.token);
  }

  function showLogin(msg) {
    $("login").hidden = false;
    $("app").hidden = true;
    $("dot").className = "dot";
    $("conn").textContent = msg || "未登录";
    if (S.timer) { clearInterval(S.timer); S.timer = null; }
  }

  async function enter() {
    $("login").hidden = true;
    $("app").hidden = false;
    await refreshStatus();
    if (S.timer) { clearInterval(S.timer); }
    S.timer = setInterval(refreshStatus, 8000);
  }

  async function login() {
    var tok = $("token").value.trim();
    if (!tok) { return; }
    var res = await fetch(withToken("/api/dev/login"), { credentials: "same-origin" });
    if (!res.ok) { showLogin("令牌无效"); return; }
    S.token = tok;
    localStorage.setItem(TOKEN_KEY, tok);
    await enter();
  }

  async function refreshStatus() {
    var r = await api("/api/dev/status");
    if (!r || !r.ok) { return; }
    var d = r.data || {};
    $("dot").className = d.listening ? "dot on" : "dot";
    $("conn").textContent = (d.listening ? "监听中 " : "未监听 ") + (d.bind || "-") + ":" + (d.port || "-") + " | 请求 " + (d.requests || 0) + " | 错误 " + (d.errors || 0) + " | 鉴权 " + (d.auth || "-");
  }

  function bind() {
    $("btnLogin").onclick = login;
    $("token").onkeydown = function (e) { if (e.key === "Enter") { login(); } };
    $("btnReload").onclick = async function () { await refreshStatus(); setOut($("out"), (await api("/api/dev/info"))); };

    $("btnInfo").onclick = async function () { setOut($("out"), await api("/api/dev/info")); };
    $("btnSysinfo").onclick = async function () { setOut($("out"), await api("/api/dev/cmd", { method: "POST", body: { op: "sys.info" } })); };
    $("btnLog").onclick = async function () { setOut($("out"), await api("/api/dev/log?lines=300")); };
    $("btnScan").onclick = async function () { setOut($("out"), await api("/api/dev/scan", { method: "POST", body: {} })); };
    $("btnSync").onclick = async function () { setOut($("out"), await api("/api/dev/sync", { method: "POST", body: { args: { limit: 200 } } })); };

    $("btnCmd").onclick = async function () {
      var args = {};
      var raw = $("args").value.trim();
      if (raw) {
        try { args = JSON.parse(raw); } catch (e) { setOut($("cmdOut"), "参数不是合法 JSON：" + e.message); return; }
      }
      var p = $("argpath").value.trim();
      if (p) { args.path = p; }
      var op = $("op").value;
      var out = await api("/api/dev/cmd", { method: "POST", body: { op: op, args: args, timeout: 300 } });
      setOut($("cmdOut"), out);
    };

    $("btnRules").onclick = async function () {
      var rules;
      try { rules = JSON.parse($("rules").value || "{}"); } catch (e) { setOut($("rulesOut"), "规则不是合法 JSON：" + e.message); return; }
      var body = { rules: rules, version: parseInt($("ver").value || "1", 10) };
      setOut($("rulesOut"), await api("/api/dev/rules", { method: "POST", body: body }));
    };

    $("btnShot").onclick = function () {
      var q = $("quality").value || 55;
      var m = $("monitor").value || 0;
      $("view").src = withToken("/api/dev/frame.jpg?q=" + q + "&monitor=" + m);
      $("streamHint").textContent = "已取单帧";
    };

    $("btnStream").onclick = function () {
      var img = $("view");
      if (S.streaming) {
        img.removeAttribute("src");
        S.streaming = false;
        $("btnStream").textContent = "开始推流";
        $("streamHint").textContent = "已停止";
        return;
      }
      var q = $("quality").value || 55;
      var m = $("monitor").value || 0;
      var fps = $("fps").value || 5;
      img.src = withToken("/api/dev/stream.mjpg?fps=" + fps + "&q=" + q + "&monitor=" + m);
      S.streaming = true;
      $("btnStream").textContent = "停止推流";
      $("streamHint").textContent = "MJPEG 推流中";
    };
  }

  document.addEventListener("DOMContentLoaded", function () {
    bind();
    $("token").value = S.token;
    if (S.token) { enter(); } else { showLogin("请输入设备令牌"); }
  });
})();
