// ✅ Setup
const el = (id) => document.getElementById(id);
const appNameInput = el('appName');
const promptInput  = el('prompt');
const regionInput  = el('region');
const apiKeyInput  = el('apiKey');
const providerInput= el('provider');
const btnGenerate  = el('btnGenerate');
const statusEl     = el('status');
const diagramHost  = el('diagramHost');
const providerLabel= el('providerLabel');
const btnSvg       = el('btnSvg');
const btnPng       = el('btnPng');
const tfOut        = el('tfOut');
const btnCopyTf    = el('btnCopyTf');
const btnDlTf      = el('btnDlTf');
const pricingDiv   = el('pricing');
const confluenceBox= el('confluenceDoc');
const btnCopyConf  = el('btnCopyConfluence');
const btnDlConf    = el('btnDlConfluence');
const btnCsv       = el('btnCsv');

let lastSvg = '', lastDiagram = '', lastTf = '', lastCost = null, lastConfluence = '';

function escapeHtml(s) {
  return (s || '').replace(/[&<>"]/g, c => (
    {'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]
  ));
}

async function callMcp() {
  const appName  = appNameInput.value.trim() || '3-tier web app';
  const prompt   = promptInput.value.trim();
  const region   = regionInput.value.trim();
  const apiKey   = apiKeyInput.value.trim();
  const provider = providerInput.value.toLowerCase();

  if (!apiKey) {
    statusEl.textContent = 'Please enter your x-api-key.';
    return;
  }

  btnGenerate.disabled = true;
  statusEl.textContent = `Generating (${provider})...`;
  providerLabel.textContent = provider.toUpperCase();

  try {
    const body = { app_name: appName, prompt };
    if (region) body.region = region;

    const res = await fetch(`/api/mcp/${provider}/diagram-tf`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', 'x-api-key': apiKey },
      body: JSON.stringify(body)
    });

    if (!res.ok) throw new Error(`Backend error (${res.status})`);
    const data = await res.json();

    lastDiagram    = (data.diagram || '').trim();
    lastTf         = (data.terraform || '').trim();
    lastCost       = data.cost || null;
    lastConfluence = (data.confluence_doc || '').trim();

    await renderMermaidToSvg(lastDiagram);
    renderTerraform(lastTf);
    renderPricing(lastCost);
    renderConfluence(lastConfluence);

    statusEl.textContent = 'Done.';
  } catch (e) {
    console.error(e);
    statusEl.textContent = e.message || 'Request failed.';
    diagramHost.innerHTML = `<pre>${escapeHtml(lastDiagram || '(no diagram)')}</pre>`;
  } finally {
    btnGenerate.disabled = false;
  }
}

// ✅ Render diagram visually using Mermaid
async function renderMermaidToSvg(diagramText) {
  const id = 'arch-' + Math.random().toString(36).slice(2, 9);
  try {
    let diagram = diagramText.trim();
    if (!diagram.toLowerCase().startsWith('graph') && !diagram.toLowerCase().startsWith('flowchart')) {
      diagram = 'flowchart LR\n' + diagram;
    }
    const { svg } = await mermaid.render(id, diagram);
    diagramHost.innerHTML = svg;

    const svgEl = diagramHost.querySelector('svg');
    if (svgEl) {
      svgEl.setAttribute('width', '100%');
      svgEl.style.maxHeight = '600px';
    }
    lastSvg = svg;
  } catch (err) {
    console.error('Mermaid render error', err);
    diagramHost.innerHTML = `<pre class="mermaid">${escapeHtml(diagramText)}</pre>`;
  }
}

function renderTerraform(tf) { tfOut.value = tf || ''; }

function renderPricing(costObj) {
  if (!costObj || !Array.isArray(costObj.items)) {
    pricingDiv.innerHTML = '<p class="muted">No cost data.</p>';
    return;
  }
  const rows = costObj.items.map(it => `
      <tr>
        <td>${it.cloud}</td>
        <td>${it.resource}</td>
        <td>${it.sku || ''}</td>
        <td>${it.region || ''}</td>
        <td style="text-align:right">${it.qty}</td>
        <td style="text-align:right">$${(it.unit_monthly || 0).toFixed(2)}</td>
        <td style="text-align:right">$${(it.monthly || 0).toFixed(2)}</td>
      </tr>`).join('');
  const total = (costObj.total_estimate || 0).toFixed(2);
  pricingDiv.innerHTML = `
    <table>
      <thead><tr>
        <th>Cloud</th><th>Resource</th><th>SKU</th><th>Region</th>
        <th style="text-align:right">Qty</th>
        <th style="text-align:right">Unit/Month</th>
        <th style="text-align:right">Monthly</th>
      </tr></thead>
      <tbody>${rows}</tbody>
      <tfoot><tr>
        <td colspan="6" style="text-align:right">Total (${costObj.currency || 'USD'})</td>
        <td style="text-align:right">$${total}</td>
      </tr></tfoot>
    </table>`;
}

function renderConfluence(text) {
  if (!confluenceBox) return;
  confluenceBox.value = text || 'No Confluence documentation available.';
  btnCopyConf.style.display = text ? 'inline-block' : 'none';
  btnDlConf.style.display   = text ? 'inline-block' : 'none';
}

// ✅ Event Listeners
btnGenerate.addEventListener('click', callMcp);
btnCopyTf.addEventListener('click', async () => {
  await navigator.clipboard.writeText(tfOut.value || '');
  btnCopyTf.textContent = 'Copied!';
  setTimeout(() => btnCopyTf.textContent = 'Copy', 1000);
});
btnDlTf.addEventListener('click', () => {
  const blob = new Blob([tfOut.value], { type: 'text/plain' });
  const a = document.createElement('a');
  a.href = URL.createObjectURL(blob);
  a.download = 'main.tf';
  a.click();
});
btnSvg.addEventListener('click', () => {
  if (!lastSvg) return;
  const blob = new Blob([lastSvg], { type: 'image/svg+xml' });
  const a = document.createElement('a');
  a.href = URL.createObjectURL(blob);
  a.download = 'archgenie-diagram.svg';
  a.click();
});
btnPng.addEventListener('click', async () => {
  if (!lastSvg) return;
  const svgEl = new DOMParser().parseFromString(lastSvg, 'image/svg+xml').documentElement;
  const canvas = document.createElement('canvas');
  const bbox = diagramHost.querySelector('svg')?.getBBox?.() || {width:1024,height:768};
  canvas.width = bbox.width; canvas.height = bbox.height;
  const ctx = canvas.getContext('2d');
  const img = new Image();
  img.onload = () => {
    ctx.drawImage(img, 0, 0);
    const a = document.createElement('a');
    a.href = canvas.toDataURL('image/png');
    a.download = 'archgenie-diagram.png';
    a.click();
  };
  img.src = 'data:image/svg+xml;charset=utf-8,' + encodeURIComponent(lastSvg);
});
btnCsv.addEventListener('click', async () => {
  const appName = appNameInput.value.trim() || 'archgenie-app';
  const region = regionInput.value.trim() || 'eastus';
  const provider = providerInput.value.toLowerCase();
  const payload = { app_name: appName, region, terraform: tfOut.value };
  const resp = await fetch(`/api/mcp/${provider}/cost-csv`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json', 'x-api-key': apiKeyInput.value.trim() },
    body: JSON.stringify(payload)
  });
  const blob = await resp.blob();
  const a = document.createElement('a');
  a.href = URL.createObjectURL(blob);
  a.download = `${appName.replace(/\s+/g,'_')}_${provider}_costs.csv`;
  a.click();
});