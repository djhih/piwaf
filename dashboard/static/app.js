// PiWAF 前端：每 5 秒打 /api/stats，用 Chart.js 在瀏覽器端渲染。
const COL = { red:'#ff5c5c', grn:'#34d399', blu:'#5b9dff', amb:'#fbbf24',
              vio:'#a78bfa', cyn:'#22d3ee', mut:'#8b98a9' };
const PALETTE = [COL.red, COL.amb, COL.blu, COL.vio, COL.cyn, COL.grn, COL.mut];
Chart.defaults.color = '#8b98a9';
Chart.defaults.borderColor = '#2a3548';
Chart.defaults.font.family = 'system-ui,"Noto Sans CJK TC",sans-serif';

const charts = {};
function bar(id, horizontal) {
  return new Chart(document.getElementById(id), {
    type: 'bar',
    data: { labels: [], datasets: [{ data: [], backgroundColor: COL.blu }] },
    options: {
      indexAxis: horizontal ? 'y' : 'x',
      plugins: { legend: { display: false } },
      scales: { x: { grid: { display: !horizontal } }, y: { grid: { display: horizontal } } },
      maintainAspectRatio: false,
    },
  });
}
function initCharts() {
  charts.cat = bar('c-cat');
  charts.rules = bar('c-rules', true);
  charts.ep = bar('c-ep', true);
  charts.score = bar('c-score');
  charts.outcome = new Chart(document.getElementById('c-outcome'), {
    type: 'doughnut',
    data: { labels: ['Blocked', 'Passed'], datasets: [{ data: [0, 0],
            backgroundColor: [COL.red, COL.grn] }] },
    options: { maintainAspectRatio: false, plugins: { legend: { position: 'bottom' } } },
  });
  charts.time = new Chart(document.getElementById('c-time'), {
    type: 'line',
    data: { labels: [], datasets: [
      { label: '總數', data: [], borderColor: COL.blu, tension: .25, pointRadius: 0 },
      { label: '被擋', data: [], borderColor: COL.red, tension: .25, pointRadius: 0 } ] },
    options: { maintainAspectRatio: false, plugins: { legend: { position: 'bottom' } },
               scales: { x: { ticks: { maxTicksLimit: 8 } } } },
  });
}

function setBar(ch, items, key) {
  ch.data.labels = items.map(d => d.label ?? d.score);
  ch.data.datasets[0].data = items.map(d => d.n);
  ch.data.datasets[0].backgroundColor = key === 'cat'
    ? items.map((_, i) => PALETTE[i % PALETTE.length]) : COL.blu;
  ch.update();
}

function renderHealth(h) {
  const el = document.getElementById('health');
  if (!h || h.status === 'unknown') return;
  el.className = h.status;             // ok / warn / critical
  const m = h.metrics || {};
  const label = { ok: '系統正常，可負荷目前流量', warn: '負載偏高，請留意',
                  critical: '⚠ 負載過高，可能無法負荷！' }[h.status] || '';
  const reasons = (h.reasons && h.reasons.length) ? '　—　' + h.reasons.join('；') : '';
  document.getElementById('h-text').textContent = label + reasons;
  document.getElementById('h-metrics').textContent =
    `${m.req_per_min ?? 0} req/min · load ${m.load1 ?? '-'}/${m.ncpu ?? '-'}` +
    ` · RAM 剩 ${m.mem_avail_pct ?? '-'}% · swap ${m.swap_used_pct ?? '-'}%`;
}

let filtersLoaded = false;
function loadFilters(f) {
  if (filtersLoaded || !f) return;
  const fill = (id, vals) => {
    const el = document.getElementById(id);
    vals.forEach(v => { const o = document.createElement('option'); o.value = v; o.textContent = v; el.appendChild(o); });
  };
  fill('f-mode', f.modes || []);
  fill('f-scenario', f.scenarios || []);
  filtersLoaded = true;
}

async function refresh() {
  const mode = document.getElementById('f-mode').value;
  const scenario = document.getElementById('f-scenario').value;
  let d;
  try {
    d = await (await fetch(`/api/stats?mode=${encodeURIComponent(mode)}&scenario=${encodeURIComponent(scenario)}`)).json();
  } catch (e) { return; }

  document.getElementById('stamp').textContent = '更新於 ' + new Date().toLocaleTimeString();
  renderHealth(d.health);
  loadFilters(d.filters);

  if (d.empty) {
    document.getElementById('empty').style.display = 'block';
    document.getElementById('board').style.display = 'none';
    return;
  }
  document.getElementById('empty').style.display = 'none';
  document.getElementById('board').style.display = 'block';

  const k = d.kpi, pct = n => k.total ? Math.round(n / k.total * 100) + '%' : '0%';
  document.getElementById('k-total').textContent = k.total;
  document.getElementById('k-blocked').textContent = `${k.blocked} (${pct(k.blocked)})`;
  document.getElementById('k-would').textContent = `${k.would_block} (${pct(k.would_block)})`;
  document.getElementById('k-would-l').textContent = `分數≥${d.threshold}（本來會擋）`;
  document.getElementById('k-attacks').textContent = `${k.attacks} (${pct(k.attacks)})`;

  setBar(charts.cat, d.categories, 'cat');
  setBar(charts.rules, d.top_rules);
  setBar(charts.ep, d.top_endpoints);
  setBar(charts.score, d.scores.map(s => ({ label: s.score, n: s.n })));

  charts.outcome.data.datasets[0].data = [d.outcome.blocked, d.outcome.passed];
  charts.outcome.update();

  charts.time.data.labels = d.timeline.map(r => r.t.replace('T', ' '));
  charts.time.data.datasets[0].data = d.timeline.map(r => r.total);
  charts.time.data.datasets[1].data = d.timeline.map(r => r.blocked);
  charts.time.update();

  const tb = document.querySelector('#t-modes tbody');
  tb.innerHTML = d.modes.map(m =>
    `<tr><td>${m.mode}</td><td>${m.requests}</td><td>${m.blocked}</td><td>${m.would_block}</td><td>${m.avg_score ?? '-'}</td></tr>`
  ).join('');
}

initCharts();
document.getElementById('f-mode').addEventListener('change', refresh);
document.getElementById('f-scenario').addEventListener('change', refresh);
refresh();
setInterval(refresh, 5000);
