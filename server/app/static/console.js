'use strict';

var DQ = String.fromCharCode(34);
var SQ = String.fromCharCode(39);
var BS = String.fromCharCode(92);
var NL = String.fromCharCode(10);
var AMP = String.fromCharCode(38);
var LT = String.fromCharCode(60);
var GT = String.fromCharCode(62);
var HASH = String.fromCharCode(35);

var ENT = {};
ENT[AMP] = AMP + 'amp;';
ENT[LT] = AMP + 'lt;';
ENT[GT] = AMP + 'gt;';
ENT[DQ] = AMP + 'quot;';
ENT[SQ] = AMP + HASH + '39;';
var ESCRE = new RegExp('[' + AMP + LT + GT + DQ + SQ + ']', 'g');

function $(s) { return document.querySelector(s); }
function esc(s) {
  if (s === undefined || s === null) { return ''; }
  return String(s).replace(ESCRE, function (c) { return ENT[c]; });
}
function toSlash(p) { return String(p || '').split(BS).join('/'); }
function trimTail(p) { var x = String(p || ''); while (x.length > 1 && x.charAt(x.length - 1) === '/') { x = x.slice(0, -1); } return x; }
function parentOf(p) {
  var norm = trimTail(toSlash(p));
  var i = norm.lastIndexOf('/');
  if (i <= 0) { return norm.slice(0, 1) || '/'; }
  return norm.slice(0, i);
}
function joinPath(base, name) {
  var b = trimTail(toSlash(base));
  return (b === '' ? '/' : b + '/') + name;
}

var state = {
  token: localStorage.getItem('cloudctl_token') || '',
  devices: [], cur: null, tab: 'desktop', ws: null, viewPath: ''
};
var TABS = [['desktop', '桌面'], ['shell', '终端'], ['files', '文件'], ['rules', '规则'], ['info', '信息'], ['audit', '审计']];

function api(path, opts) {
  var o = Object.assign({ headers: { 'X-Token': state.token, 'Content-Type': 'application/json' } }, opts || {});
  return fetch(path, o).then(function (res) {
    if (res.status === 401) { showLogin('令牌无效'); throw new Error('unauth'); }
    return res.text().then(function (t) {
      try { return JSON.parse(t); } catch (e) { return { raw: t }; }
    });
  });
}

function cmd(op, args, timeout) {
  return api('/api/devices/' + encodeURIComponent(state.cur.device_id) + '/cmd', {
    method: 'POST',
    body: JSON.stringify({ op: op, args: args || {}, timeout: timeout || 120 })
  });
}

function showLogin(msg) {
  state.token = '';
  localStorage.removeItem('cloudctl_token');
  $('#login').classList.remove('hidden');
  $('#app').classList.add('hidden');
  if (msg) { $('#loginErr').textContent = msg; }
}

function enterApp() {
  $('#login').classList.add('hidden');
  $('#app').classList.remove('hidden');
  connectConsole();
  refresh();
  setInterval(refresh, 8000);
}

function doLogin() {
  var tok = $('#tok').value.trim();
  if (!tok) { return; }
  fetch('/api/login', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ token: tok })
  }).then(function (r) {
    if (!r.ok) { $('#loginErr').textContent = '令牌无效'; return; }
    state.token = tok;
    localStorage.setItem('cloudctl_token', tok);
    enterApp();
  });
}

function connectConsole() {
  var proto = location.protocol === 'https:' ? 'wss' : 'ws';
  var ws = new WebSocket(proto + '://' + location.host + '/ws/console?token=' + encodeURIComponent(state.token));
  state.ws = ws;
  ws.onopen = function () { $('#conn').textContent = '链路：在线'; };
  ws.onclose = function () { $('#conn').textContent = '链路：已断开'; setTimeout(connectConsole, 3000); };
  ws.onmessage = function (ev) {
    var m;
    try { m = JSON.parse(ev.data); } catch (e) { return; }
    if (m.type === 'frame') { renderFrame(m); return; }
    if (m.type === 'device.online' || m.type === 'device.offline' || m.type === 'event') { refresh(); }
  };
}

function refresh() {
  if (!state.token) { return; }
  api('/api/devices').then(function (d) {
    state.devices = d.devices || [];
    $('#counts').textContent = (d.online || 0) + ' 在线 / ' + (d.count || 0) + ' 合计';
    var box = $('#devices');
    box.innerHTML = '';
    if (!state.devices.length) { box.innerHTML = '<p class=dim style=padding:14px>还没有实例连上来</p>'; }
    state.devices.forEach(function (dev) {
      var el = document.createElement('div');
      el.className = 'dev' + (state.cur && state.cur.device_id === dev.device_id ? ' active' : '');
      el.innerHTML = '<div class=name><span class=dot ' + (dev.online ? 'on' : '') + '></span>' + esc(dev.name || dev.device_id) + '</div>'
        + '<div class=meta>' + esc(dev.os || '') + ' · ' + (dev.online ? '在线' : '离线') + '</div>'
        + '<div class=meta>' + new Date((dev.last_seen || 0) * 1000).toLocaleString() + '</div>';
      el.onclick = function () { openDevice(dev.device_id); };
      box.appendChild(el);
    });
    if (state.cur) {
      var fresh = state.devices.filter(function (x) { return x.device_id === state.cur.device_id; })[0];
      if (fresh) { state.cur = Object.assign({}, state.cur, fresh); }
    }
  });
}

function openDevice(id) {
  api('/api/devices/' + encodeURIComponent(id)).then(function (dev) {
    state.cur = dev;
    renderDetail();
    refresh();
  });
}

function renderDetail() {
  var dev = state.cur;
  if (!dev) { return; }
  var bar = TABS.map(function (t) {
    return '<button data-tab=' + t[0] + ' class=' + (state.tab === t[0] ? 'active' : '') + '>' + t[1] + '</button>';
  }).join('');
  $('#detail').innerHTML = '<div class=card><div class=row><strong>' + esc(dev.name || dev.device_id) + '</strong>'
    + '<span class=dim>' + esc(dev.device_id) + '</span><span class=grow></span>'
    + '<span class=dim>' + (dev.online ? '在线' : '离线') + '</span></div></div>'
    + '<div class=tabs id=tabbar>' + bar + '</div><div id=tabbody></div>';
  Array.prototype.forEach.call(document.querySelectorAll('#tabbar button'), function (b) {
    b.onclick = function () { switchTab(b.getAttribute('data-tab')); };
  });
  if (state.tab === 'desktop') { tabDesktop(); }
  else if (state.tab === 'shell') { tabShell(); }
  else if (state.tab === 'files') { tabFiles(state.viewPath || 'C:/'); }
  else if (state.tab === 'rules') { tabRules(); }
  else if (state.tab === 'info') { tabInfo(); }
  else if (state.tab === 'audit') { tabAudit(); }
}

function switchTab(k) {
  if (state.tab === 'desktop' && k !== 'desktop' && state.cur && state.ws && state.ws.readyState === 1) {
    state.ws.send(JSON.stringify({ type: 'desktop.stop', device_id: state.cur.device_id }));
  }
  state.tab = k;
  renderDetail();
}

function tabDesktop() {
  $('#tabbody').innerHTML = '<div class=card><div class=row style=margin-bottom:8px>'
    + '<button class=primary id=dStart>开始</button><button id=dStop>停止</button>'
    + '<label class=dim>质量 <input id=q type=number value=55 min=20 max=90 style=width:70px></label>'
    + '<label class=dim>帧率 <input id=fps type=number value=8 min=1 max=30 style=width:70px></label>'
    + '</div><canvas id=screen width=1280 height=720></canvas>'
    + '<div class=dim id=fpsInfo style=margin-top:6px></div></div>';
  $('#dStart').onclick = startDesktop;
  $('#dStop').onclick = stopDesktop;
  bindCanvas();
  if (state.cur && state.cur.online && state.ws && state.ws.readyState === 1) { startDesktop(); }
}

function startDesktop() {
  if (!state.cur || !state.ws || state.ws.readyState !== 1) { return; }
  state.ws.send(JSON.stringify({
    type: 'desktop.start', device_id: state.cur.device_id,
    args: { fps: parseInt($('#fps').value, 10) || 8, quality: parseInt($('#q').value, 10) || 55 }
  }));
}

function stopDesktop() {
  if (state.cur && state.ws && state.ws.readyState === 1) {
    state.ws.send(JSON.stringify({ type: 'desktop.stop', device_id: state.cur.device_id }));
  }
}

var lastTick = 0, frameCount = 0;
function renderFrame(m) {
  var cv = $('#screen');
  if (!cv) { return; }
  var img = new Image();
  img.onload = function () {
    if (cv.width !== m.w || cv.height !== m.h) { cv.width = m.w; cv.height = m.h; }
    cv.getContext('2d').drawImage(img, 0, 0);
    frameCount++;
    var now = performance.now();
    if (!lastTick) { lastTick = now; }
    if (now - lastTick > 1000) {
      $('#fpsInfo').textContent = '实际 ' + frameCount + ' fps · ' + m.w + 'x' + m.h + ' · seq ' + m.seq;
      frameCount = 0; lastTick = now;
    }
  };
  img.src = 'data:image/jpeg;base64,' + m.jpeg;
}

function bindCanvas() {
  var cv = $('#screen');
  if (!cv) { return; }
  function send(events) {
    if (state.ws && state.ws.readyState === 1) {
      state.ws.send(JSON.stringify({ type: 'desktop.input', device_id: state.cur.device_id, events: events }));
    }
  }
  function pos(e) {
    var r = cv.getBoundingClientRect();
    return { x: Math.round((e.clientX - r.left) * cv.width / r.width), y: Math.round((e.clientY - r.top) * cv.height / r.height) };
  }
  function btn(b) { return b === 2 ? 'right' : (b === 1 ? 'middle' : 'left'); }
  var last = 0;
  cv.onmousemove = function (e) { var n = Date.now(); if (n - last < 40) { return; } last = n; var p = pos(e); send([{ k: 'move', x: p.x, y: p.y }]); };
  cv.onmousedown = function (e) { var p = pos(e); send([{ k: 'move', x: p.x, y: p.y }, { k: 'down', btn: btn(e.button) }]); };
  cv.onmouseup = function (e) { var p = pos(e); send([{ k: 'move', x: p.x, y: p.y }, { k: 'up', btn: btn(e.button) }]); };
  cv.oncontextmenu = function (e) { e.preventDefault(); };
  cv.onwheel = function (e) { e.preventDefault(); send([{ k: 'wheel', dy: -e.deltaY }]); };
}

function typing() {
  var a = document.activeElement;
  return !!a && (a.tagName === 'INPUT' || a.tagName === 'TEXTAREA');
}

document.addEventListener('keydown', function (e) {
  if (state.tab !== 'desktop' || !state.ws || state.ws.readyState !== 1 || !state.cur || typing()) { return; }
  e.preventDefault();
  state.ws.send(JSON.stringify({ type: 'desktop.input', device_id: state.cur.device_id, events: [{ k: 'key_down', key: e.key }] }));
});

document.addEventListener('keyup', function (e) {
  if (state.tab !== 'desktop' || !state.ws || state.ws.readyState !== 1 || !state.cur || typing()) { return; }
  state.ws.send(JSON.stringify({ type: 'desktop.input', device_id: state.cur.device_id, events: [{ k: 'key_up', key: e.key }] }));
});

$('#btnLogin').onclick = doLogin;
$('#tok').addEventListener('keydown', function (e) { if (e.key === 'Enter') { doLogin(); } });
$('#btnRefresh').onclick = refresh;

if (state.token) { enterApp(); }
