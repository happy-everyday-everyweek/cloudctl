'use strict';

function tabShell() {
  $('#tabbody').innerHTML = '<div class=card><div class=row>'
    + '<input id=shellCmd class=path placeholder=输入命令，回车执行>'
    + '<button class=primary id=btnRun>执行</button></div>'
    + '<pre id=shellOut style=margin-top:10px></pre></div>';
  $('#btnRun').onclick = runShell;
  $('#shellCmd').addEventListener('keydown', function (e) { if (e.key === 'Enter') { runShell(); } });
}

function runShell() {
  var cmdline = $('#shellCmd').value.trim();
  if (!cmdline) { return; }
  var out = $('#shellOut');
  out.textContent += '> ' + cmdline + NL;
  cmd('shell.exec', { cmd: cmdline, timeout_s: 120 }).then(function (r) {
    if (r.ok) { out.textContent += (r.data.stdout || '') + (r.data.stderr || ''); }
    else { out.textContent += '[失败] ' + (r.err || r.detail || '') + NL; }
    out.scrollTop = out.scrollHeight;
  });
}

function tabFiles(path) {
  $('#tabbody').innerHTML = '<div class=card><div class=row>'
    + '<input id=fp class=path placeholder=C:/ 或 /home>'
    + '<button class=primary id=btnLs>浏览</button><button id=btnMk>新建目录</button></div>'
    + '<div id=flist style=margin-top:10px></div></div>';
  $('#fp').value = path || 'C:/';
  $('#btnLs').onclick = function () { listFiles($('#fp').value); };
  $('#btnMk').onclick = mkDir;
  $('#fp').addEventListener('keydown', function (e) { if (e.key === 'Enter') { listFiles($('#fp').value); } });
  listFiles($('#fp').value);
}

function listFiles(path) {
  if (!path) { return; }
  state.viewPath = path;
  cmd('file.list', { path: path }).then(function (r) {
    var box = $('#flist');
    if (!r.ok) { box.innerHTML = '<p class=dim>读取失败：' + esc(r.err || r.detail || '') + '</p>'; return; }
    var d = r.data;
    box.innerHTML = '';
    if (d.is_file) { box.innerHTML = '<p class=dim>' + esc(d.path) + ' · ' + d.size_h + '</p>'; return; }
    var table = document.createElement('table');
    table.innerHTML = '<tr><th>名称</th><th>大小</th><th>修改时间</th><th></th></tr>';
    var up = document.createElement('tr');
    var upA = document.createElement('a');
    upA.href = '#';
    upA.textContent = '..';
    upA.onclick = function (ev) { ev.preventDefault(); listFiles(parentOf(d.path)); };
    var upTd = document.createElement('td');
    upTd.appendChild(upA);
    up.appendChild(upTd);
    up.appendChild(document.createElement('td'));
    up.appendChild(document.createElement('td'));
    up.appendChild(document.createElement('td'));
    table.appendChild(up);
    (d.entries || []).forEach(function (e) {
      var tr = document.createElement('tr');
      var nameTd = document.createElement('td');
      if (e.is_dir) {
        var a = document.createElement('a');
        a.href = '#';
        a.textContent = '[目录] ' + e.name;
        a.onclick = function (ev) { ev.preventDefault(); listFiles(e.path); };
        nameTd.appendChild(a);
      } else {
        nameTd.textContent = e.name;
      }
      var sizeTd = document.createElement('td');
      sizeTd.className = 'dim';
      sizeTd.textContent = e.size_h || '';
      var mtTd = document.createElement('td');
      mtTd.className = 'dim';
      mtTd.textContent = new Date((e.mtime || 0) * 1000).toLocaleString();
      var actTd = document.createElement('td');
      var del = document.createElement('button');
      del.className = 'danger';
      del.textContent = '删除';
      del.onclick = function () { delPath(e.path); };
      actTd.appendChild(del);
      tr.appendChild(nameTd);
      tr.appendChild(sizeTd);
      tr.appendChild(mtTd);
      tr.appendChild(actTd);
      table.appendChild(tr);
    });
    box.appendChild(table);
  });
}

function mkDir() {
  var name = prompt('新目录名（建在当前路径下）');
  if (!name) { return; }
  cmd('file.mkdir', { path: joinPath(state.viewPath, name) }).then(function () { listFiles(state.viewPath); });
}

function delPath(p) {
  if (!confirm('确定删除 ' + p + ' ？')) { return; }
  cmd('file.delete', { path: p }).then(function (r) {
    if (!r.ok) { alert('删除失败：' + (r.err || r.detail || '')); }
    listFiles(state.viewPath);
  });
}

function tabRules() {
  api('/api/devices/' + encodeURIComponent(state.cur.device_id)).then(function (r) {
    var rules = r.rules || {};
    delete rules.version;
    $('#tabbody').innerHTML = '<div class=card><div class=row style=margin-bottom:8px>'
      + '<label class=dim>作用范围 <select id=scope><option value=device>仅本机</option>'
      + '<option value=group>本分组</option><option value=global>全部</option></select></label>'
      + '<span class=grow></span><button class=primary id=btnSave>保存并下发</button></div>'
      + '<textarea id=rulesJson style=width:100%;height:340px;font-family:monospace></textarea>'
      + '<p class=dim>保存后版本号自增并推送给在线实例，离线实例上线时自动拉取</p></div>';
    $('#rulesJson').value = JSON.stringify(rules, null, 2);
    $('#btnSave').onclick = saveRules;
  });
}

function saveRules() {
  var data;
  try { data = JSON.parse($('#rulesJson').value); } catch (e) { alert('JSON 格式错误：' + e.message); return; }
  api('/api/devices/' + encodeURIComponent(state.cur.device_id) + '/rules', {
    method: 'POST',
    body: JSON.stringify({ scope: $('#scope').value, rules: data })
  }).then(function (r) { alert('已保存 version=' + r.version + '，下发 ' + r.pushed + ' 台'); });
}

function tabInfo() {
  cmd('agent.info', {}, 30).then(function (r) {
    $('#tabbody').innerHTML = '<div class=card><pre>' + esc(JSON.stringify(r, null, 2)) + '</pre></div>';
  });
}

function tabAudit() {
  api('/api/audit?device_id=' + encodeURIComponent(state.cur.device_id) + '&limit=80').then(function (r) {
    var rows = (r.audit || []).map(function (a) {
      return '<tr><td class=dim>' + new Date(a.ts * 1000).toLocaleTimeString() + '</td><td>' + esc(a.op)
        + '</td><td>' + (a.ok ? 'ok' : 'fail') + '</td><td class=dim>' + esc(String(a.result || '').slice(0, 120)) + '</td></tr>';
    }).join('');
    $('#tabbody').innerHTML = '<div class=card><table><tr><th>时间</th><th>命令</th><th>结果</th><th>内容</th></tr>' + rows + '</table></div>';
  });
}
