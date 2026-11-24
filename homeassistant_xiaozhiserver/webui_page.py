# -*- coding: utf-8 -*-
"""WebUI 页面渲染：纯 HTML 生成，不访问网络与 MCP。"""
from __future__ import annotations

import html
import json
from typing import Any

from . import __version__
from .logging_setup import log_file_path

_PAGE_STYLE = """
body{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,'Helvetica Neue',Arial,sans-serif;margin:0;background:#f5f6f8;color:#1f2328}
header{background:#1f2937;color:#fff;padding:16px 24px}
header h1{margin:0;font-size:18px;font-weight:600}
header .brand{display:flex;align-items:center;gap:10px;flex-wrap:wrap}
header .version{font-size:12px;font-weight:600;color:#bfdbfe;background:#374151;border:1px solid #4b5563;border-radius:999px;padding:2px 8px}
header .sub{margin-top:4px;font-size:12px;color:#cbd5e1}
.ver{font-size:11px;font-weight:600;color:#2563eb;background:#eff6ff;border-radius:999px;padding:1px 7px;margin-left:6px;vertical-align:middle}
.log-ver{color:#38bdf8;margin-right:8px}
main{max-width:1120px;margin:20px auto;padding:0 16px}
section{background:#fff;border:1px solid #e5e7eb;border-radius:8px;padding:16px 20px;margin-bottom:16px}
h2{font-size:15px;margin:0 0 12px;color:#111827}
.section-head{display:flex;align-items:baseline;justify-content:space-between;gap:12px;margin-bottom:12px}
.section-head h2{margin:0}
table{border-collapse:collapse;width:100%;font-size:14px}
th,td{text-align:left;padding:8px 10px;border-bottom:1px solid #eef0f3;vertical-align:top}
table.kv th{width:150px;color:#4b5563;font-weight:500}
table.device-table th{color:#4b5563;font-weight:500;white-space:nowrap}
table.device-table td{font-size:13px}
code,pre{font-family:ui-monospace,SFMono-Regular,Consolas,'Liberation Mono',monospace;font-size:13px}
pre{background:#0f172a;color:#e2e8f0;padding:14px;border-radius:6px;overflow:auto;max-height:520px}
.ok{color:#047857;font-weight:600}.bad{color:#b91c1c;font-weight:600}.muted{color:#6b7280}
.badge{display:inline-block;padding:1px 7px;margin:1px 4px 1px 0;border-radius:999px;background:#eef2ff;color:#3730a3;font-size:12px;white-space:nowrap}
.badge.danger{background:#fee2e2;color:#991b1b}
.toolbar{display:flex;gap:8px;align-items:center;flex-wrap:wrap;margin-bottom:10px}
.toolbar input[type=text]{flex:1;min-width:220px;padding:7px 10px;border:1px solid #d1d5db;border-radius:6px;font:inherit}
.toolbar select,.toolbar button{font:inherit;padding:7px 10px;border:1px solid #d1d5db;border-radius:6px}
.toolbar button{background:#2563eb;color:#fff;border:0;cursor:pointer;width:auto}
.toolbar button:hover{background:#1d4ed8}
.toolbar label.auto{display:inline-flex;align-items:center;gap:5px;font-size:13px;color:#4b5563}
.log-box{background:#0f172a;color:#e2e8f0;border-radius:6px;padding:10px 12px;max-height:360px;overflow:auto;font-family:ui-monospace,SFMono-Regular,Consolas,'Liberation Mono',monospace;font-size:12px;line-height:1.55}
.log-line{white-space:pre-wrap;word-break:break-word;padding:1px 0}
.log-time{color:#94a3b8;margin-right:10px}
.log-level{display:inline-block;min-width:58px;margin-right:8px;font-weight:600}
.log-name{color:#64748b;margin-right:8px}
.log-DEBUG .log-level{color:#94a3b8}
.log-INFO .log-level{color:#93c5fd}
.log-WARNING .log-level{color:#fbbf24}
.log-ERROR .log-level,.log-CRITICAL .log-level{color:#f87171}
details.advanced,details.params{background:#fff;border:1px solid #e5e7eb;border-radius:8px;padding:14px 20px;margin-bottom:16px}
details.params{margin-top:-6px}
details summary{cursor:pointer;font-weight:600;color:#111827}
details[open] summary{margin-bottom:12px}
form{display:grid;gap:10px}
select,textarea,button{font:inherit;padding:8px 10px;border:1px solid #d1d5db;border-radius:6px}
textarea{min-height:120px;font-family:ui-monospace,Consolas,monospace}
button{background:#2563eb;color:#fff;border:0;cursor:pointer;width:120px}
button:hover{background:#1d4ed8}
small{color:#6b7280}
@media (max-width:760px){table.device-table th:nth-child(2),table.device-table td:nth-child(2){display:none}}
"""


def _page(title: str, body: str, script: str = "") -> str:
    return (
        "<!doctype html><html lang='zh-CN'><head><meta charset='utf-8'>"
        "<meta name='viewport' content='width=device-width,initial-scale=1'>"
        "<title>" + html.escape(title) + "</title><style>" + _PAGE_STYLE + "</style></head>"
        "<body><header><div class='brand'><h1>homeassistant-xiaozhiserver · WebUI</h1>"
        "<span class='version'>v" + html.escape(__version__) + "</span></div>"
        "<div class='sub'>设备总览 · 服务日志 · 高级工具调试</div></header>"
        "<main>" + body + "</main>" + script + "</body></html>"
    )


def _render_device_rows(data: dict[str, Any]) -> str:
    if not data.get("ok"):
        return "<tr><td colspan='6' class='muted'>" + html.escape(str(data.get("message") or "加载失败")) + "</td></tr>"
    rows = data.get("rows") or []
    if not rows:
        return "<tr><td colspan='6' class='muted'>没有可控制设备。</td></tr>"
    out: list[str] = []
    for row in rows:
        actions = row.get("actions") or []
        badges = "".join("<span class='badge'>" + html.escape(str(action)) + "</span>" for action in actions)
        if not badges:
            badges = "<span class='muted'>—</span>"
        danger = " <span class='badge danger'>危险</span>" if row.get("dangerous") else ""
        state = html.escape(str(row.get("state") or "unknown"))
        unit = str(row.get("unit") or "")
        if unit:
            state += " " + html.escape(unit)
        if str(row.get("state")) == "unavailable":
            state = "<span class='bad'>" + state + "</span>"
        elif str(row.get("state")) == "unknown":
            state = "<span class='muted'>" + state + "</span>"
        out.append(
            "<tr>"
            "<td>" + html.escape(str(row.get("area") or "未分配区域")) + "</td>"
            "<td>" + html.escape(str(row.get("device") or "未命名设备")) + "</td>"
            "<td>" + html.escape(str(row.get("type") or "未知")) + danger
            + "<br><small>" + html.escape(str(row.get("domain") or "")) + "</small></td>"
            "<td>" + html.escape(str(row.get("name") or ""))
            + "<br><small><code>" + html.escape(str(row.get("entity_id") or "")) + "</code></small></td>"
            "<td>" + state + "</td>"
            "<td>" + badges + "</td>"
            "</tr>"
        )
    return "".join(out)


def _render_log_entries(entries: list[dict[str, str]]) -> str:
    if not entries:
        return "<div class='muted'>暂无日志。</div>"
    out: list[str] = []
    for entry in entries:
        level = str(entry.get("level") or "INFO").upper()
        if level not in ("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"):
            level = "INFO"
        level = html.escape(level)
        version = str(entry.get("version") or "")
        version_html = ("<span class='log-ver'>v" + html.escape(version) + "</span>") if version else ""
        message = html.escape(str(entry.get("message") or "")).replace("\n", "<br>")
        out.append(
            "<div class='log-line log-" + level + "'>"
            "<span class='log-time'>" + html.escape(str(entry.get("time") or "")) + "</span>"
            "<span class='log-level'>" + level + "</span>"
            + version_html +
            "<span class='log-name'>[" + html.escape(str(entry.get("logger") or "")) + "]</span>"
            "<span class='log-msg'>" + message + "</span>"
            "</div>"
        )
    return "".join(out)


def _render_index(status: dict[str, Any], devices: dict[str, Any],
                  logs: list[dict[str, str]], config: dict[str, Any]) -> str:
    ha = status["ha"]
    if not ha["configured"]:
        ha_text = "<span class='bad'>未配置</span>"
    elif ha["reachable"] is True:
        ha_text = "<span class='ok'>正常</span> · " + html.escape(str(ha["url"]))
    elif ha["reachable"] is False:
        ha_text = "<span class='bad'>不可用</span>"
        if ha["error"]:
            ha_text += "（" + html.escape(str(ha["error"])) + "）"
        ha_text += " · " + html.escape(str(ha["url"]))
    else:
        ha_text = "<span class='muted'>检测中</span> · " + html.escape(str(ha["url"]))
    endpoint = status["mcp_endpoint"]
    endpoint_text = html.escape(endpoint["display"]) if endpoint["configured"] else "<span class='bad'>未配置</span>"
    bridge = status.get("bridge") or {}
    if not bridge.get("configured"):
        bridge_text = "<span class='muted'>未配置</span>"
    elif bridge.get("connected"):
        bridge_text = "<span class='ok'>已连接</span>"
        if bridge.get("connected_for_seconds") is not None:
            bridge_text += " · " + str(bridge["connected_for_seconds"]) + " 秒"
    elif bridge.get("available"):
        bridge_text = "<span class='bad'>未连接</span>"
        if bridge.get("last_error"):
            bridge_text += "（" + html.escape(str(bridge["last_error"])) + "）"
    else:
        bridge_text = "<span class='muted'>状态未知</span>"

    status_rows = "".join(
        "<tr><th>" + html.escape(key) + "</th><td>" + value + "</td></tr>"
        for key, value in [
            ("版本", html.escape(status["version"])),
            ("启动时间", html.escape(status["started_at"])),
            ("运行时长", str(status["uptime_seconds"]) + " 秒"),
            ("HA 连通性", ha_text),
            ("MCP 接入点", endpoint_text),
            ("Bridge 连接", bridge_text),
            ("运行形态", html.escape(status["target"])),
        ]
    )
    param_items = [
        item for item in config["items"]
        if item["key"] not in ("HA_URL", "HA_TOKEN", "MCP_ENDPOINT")
    ]
    param_rows = "".join(
        "<tr><th>" + html.escape(item["key"]) + "</th><td>" + html.escape(str(item["value"])) + "</td></tr>"
        for item in param_items
    )
    options = "".join(
        "<option value='" + html.escape(tool["name"]) + "'>" + html.escape(tool["name"])
        + " — " + html.escape(tool["description"]) + "</option>"
        for tool in status["tools"]
    )
    examples = "".join(
        "<tr><th>" + html.escape(tool["name"]) + "</th><td><code>"
        + html.escape(tool["example"]) + "</code></td></tr>"
        for tool in status["tools"]
    )

    if devices.get("ok"):
        device_summary = "共 " + str(devices.get("total", 0)) + " 个可控实体"
        if devices.get("truncated"):
            device_summary += "（已截断，可用工具调试台按条件查询）"
    else:
        device_summary = str(devices.get("message") or "加载失败")

    body = (
        "<section><h2>运行状态</h2><table class='kv'>" + status_rows + "</table></section>"
        "<details class='params'><summary>运行参数（只读）</summary>"
        "<table class='kv'>" + param_rows + "</table>"
        "<p><small>" + html.escape(config["note"]) + "</small></p></details>"
        "<section><div class='section-head'><h2>可调用设备</h2>"
        "<span id='device-summary' class='muted'>" + html.escape(device_summary) + "</span></div>"
        "<div class='toolbar'>"
        "<input id='device-filter' type='text' placeholder='按区域、设备、类型、实体过滤…'>"
        "<button type='button' onclick='loadDevices()'>刷新设备</button>"
        "</div>"
        "<table class='device-table'><thead><tr>"
        "<th>区域</th><th>设备</th><th>类型</th><th>实体</th><th>状态</th><th>可执行动作</th>"
        "</tr></thead><tbody id='device-body'>" + _render_device_rows(devices) + "</tbody></table>"
        "<p><small>仅列出可控制实体；默认不展示门铃、自动化、热水壶、中枢网关与媒体。同一设备下与灯具同名且重复的开关会自动隐藏，只保留灯具。危险设备（门锁、安防、车库门等）操作仍需二次确认。</small></p></section>"
        "<section><div class='section-head'><h2>服务日志<span class='ver'>v" + html.escape(__version__) + "</span></h2>"
        "<span class='muted'>WebUI 与 bridge 共享日志，最近 200 条</span></div>"
        "<div class='toolbar'>"
        "<select id='log-level'><option value='ALL'>全部级别</option><option value='INFO'>INFO</option>"
        "<option value='WARNING'>WARNING</option><option value='ERROR'>ERROR</option>"
        "<option value='DEBUG'>DEBUG</option></select>"
        "<button type='button' onclick='loadLogs()'>刷新日志</button>"
        "<label class='auto'><input id='log-auto' type='checkbox' checked>自动刷新（5 秒）</label>"
        "</div>"
        "<div id='log-box' class='log-box'>" + _render_log_entries(logs) + "</div>"
        "<p><small>日志文件：" + html.escape(log_file_path()) + "</small></p></section>"
        "<details class='advanced'><summary>高级：工具调试台</summary>"
        "<p><small>一般不需要；设备列表与日志已覆盖日常使用。</small></p>"
        "<form method='post' action='/tool'>"
        "<label>选择工具<select name='name'>" + options + "</select></label>"
        "<label>参数（JSON 对象）<textarea name='params' spellcheck='false'>{}</textarea></label>"
        "<button type='submit'>调用</button>"
        "</form>"
        "<p><small>参数示例：</small></p><table>" + examples + "</table></details>"
    )
    return _page("homeassistant-xiaozhiserver WebUI", body, _INDEX_SCRIPT)


def _render_tool_result(name: str, params: str, result: Any, status: int) -> str:
    pretty = json.dumps(result, ensure_ascii=False, indent=2, default=str)
    ok = not (isinstance(result, dict) and result.get("ok") is False)
    badge = ("<span class='ok'>成功</span>" if status == 200 and ok
             else "<span class='bad'>失败（HTTP " + str(status) + "）</span>")
    body = (
        "<section><h2>调用结果 " + badge + "</h2>"
        "<table><tr><th>工具</th><td><code>" + html.escape(name) + "</code></td></tr>"
        "<tr><th>参数</th><td><code>" + html.escape(params) + "</code></td></tr></table></section>"
        "<section><h2>返回</h2><pre>" + html.escape(pretty) + "</pre></section>"
        "<p><a href='/'>← 返回首页</a></p>"
    )
    return _page("调用结果", body)


_INDEX_SCRIPT = r"""
<script>
function cell(tag, text, cls) {
  var node = document.createElement(tag);
  if (cls) node.className = cls;
  node.textContent = text == null ? '' : String(text);
  return node;
}
function showMessage(tbody, text) {
  tbody.innerHTML = '';
  var tr = document.createElement('tr');
  var td = cell('td', text, 'muted');
  td.colSpan = 6;
  tr.appendChild(td);
  tbody.appendChild(tr);
}
async function loadDevices() {
  var tbody = document.getElementById('device-body');
  var summary = document.getElementById('device-summary');
  showMessage(tbody, '加载中…');
  try {
    var resp = await fetch('/api/devices', {cache: 'no-store'});
    var data = await resp.json();
    tbody.innerHTML = '';
    if (!data.ok) {
      showMessage(tbody, data.message || '加载失败');
      summary.textContent = '';
      return;
    }
    if (!data.rows || !data.rows.length) {
      showMessage(tbody, '没有可控制设备。');
      summary.textContent = '共 0 个可控实体';
      return;
    }
    for (var i = 0; i < data.rows.length; i++) {
      var r = data.rows[i];
      var tr = document.createElement('tr');
      tr.appendChild(cell('td', r.area || '未分配区域'));
      tr.appendChild(cell('td', r.device || '未命名设备'));
      var typeTd = document.createElement('td');
      typeTd.appendChild(cell('span', r.type || r.domain || '未知'));
      if (r.dangerous) typeTd.appendChild(cell('span', '危险', 'badge danger'));
      typeTd.appendChild(document.createElement('br'));
      typeTd.appendChild(cell('small', r.domain || ''));
      tr.appendChild(typeTd);
      var entTd = document.createElement('td');
      entTd.appendChild(cell('div', r.name || r.entity_id || ''));
      entTd.appendChild(cell('small', r.entity_id || '', 'muted'));
      tr.appendChild(entTd);
      var stateText = (r.state || 'unknown') + (r.unit ? ' ' + r.unit : '');
      var stateCls = r.state === 'unavailable' ? 'bad' : (r.state === 'unknown' ? 'muted' : '');
      tr.appendChild(cell('td', stateText, stateCls));
      var actTd = document.createElement('td');
      if (r.actions && r.actions.length) {
        for (var j = 0; j < r.actions.length; j++) actTd.appendChild(cell('span', r.actions[j], 'badge'));
      } else {
        actTd.appendChild(cell('span', '—', 'muted'));
      }
      tr.appendChild(actTd);
      tbody.appendChild(tr);
    }
    summary.textContent = '共 ' + data.total + ' 个可控实体' + (data.truncated ? '（已截断）' : '');
    applyDeviceFilter();
  } catch (err) {
    showMessage(tbody, '加载设备失败：' + err);
    summary.textContent = '';
  }
}
function applyDeviceFilter() {
  var input = document.getElementById('device-filter');
  if (!input) return;
  var q = (input.value || '').trim().toLowerCase();
  var rows = document.querySelectorAll('#device-body tr');
  for (var i = 0; i < rows.length; i++) {
    rows[i].style.display = (!q || rows[i].textContent.toLowerCase().indexOf(q) >= 0) ? '' : 'none';
  }
}
async function loadLogs() {
  var box = document.getElementById('log-box');
  var level = document.getElementById('log-level').value;
  try {
    var resp = await fetch('/api/logs?limit=200&level=' + encodeURIComponent(level), {cache: 'no-store'});
    var data = await resp.json();
    box.innerHTML = '';
    if (!data.ok) {
      box.appendChild(cell('div', data.message || '加载日志失败', 'muted'));
      return;
    }
    if (!data.entries || !data.entries.length) {
      box.appendChild(cell('div', '暂无日志。', 'muted'));
      return;
    }
    for (var i = 0; i < data.entries.length; i++) {
      var e = data.entries[i];
      var line = document.createElement('div');
      line.className = 'log-line log-' + (e.level || 'INFO');
      line.appendChild(cell('span', e.time || '', 'log-time'));
      line.appendChild(cell('span', e.level || '', 'log-level'));
      if (e.version) line.appendChild(cell('span', 'v' + e.version, 'log-ver'));
      line.appendChild(cell('span', '[' + (e.logger || '') + ']', 'log-name'));
      line.appendChild(cell('span', e.message || '', 'log-msg'));
      box.appendChild(line);
    }
    box.scrollTop = box.scrollHeight;
  } catch (err) {
    box.innerHTML = '';
    box.appendChild(cell('div', '加载日志失败：' + err, 'muted'));
  }
}
var filterInput = document.getElementById('device-filter');
if (filterInput) filterInput.addEventListener('input', applyDeviceFilter);
var levelSelect = document.getElementById('log-level');
if (levelSelect) levelSelect.addEventListener('change', loadLogs);
setInterval(function () {
  var auto = document.getElementById('log-auto');
  if (auto && auto.checked) loadLogs();
}, 5000);
</script>
"""


