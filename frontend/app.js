// Mermaid init (we render manually via API)
mermaid.initialize({ startOnLoad: false, securityLevel: 'loose' }); // use loose to allow classDefs

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

let lastSvg = '';
let lastDiagram = '';
let lastTf = '';
let lastCost = null;
let lastConfluence = '';

function lastMileSanitize(diagram) {
  if (!diagram) return '';

  // strip ```mermaid fences
  diagram = diagram.replace(/^```mermaid\s*/i, '').replace(/```$/, '');

  // 🔥 ALWAYS normalize compact Mermaid (prompt case)
  diagram = diagram
    // ensure graph header is isolated
    .replace(/^graph\s+(TD|LR|TB)/, 'graph $1\n')

    // force subgraph blocks to multiline
    .replace(/subgraph\s+/g, '\nsubgraph ')
    .replace(/\sdirection\s+/g, '\n  direction ')
    .replace(/\send\s+/g, '\nend\n')

    // break node chains
    .replace(/\]\s*(?=[A-Za-z0-9_]+\[)/g, ']\n')
    .replace(/\)\s*(?=[A-Za-z0-9_]+\()/g, ')\n')

    // clean spacing
    .replace(/\s-->\s+/g, ' --> ')
    .replace(/\s+/g, ' ');

  return diagram.trim();
}

async function callMcp() {
  const appName  = appNameInput.value.trim() || '3-tier web app';
  const prompt   = promptInput.value.trim();
  const region   = regionInput.value.trim();
  const apiKey   = apiKeyInput.value.trim();
  const provider = providerInput ? providerInput.value.toLowerCase() : "azure";

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

    if (!res.ok) {
      const errText = await res.text();
      diagramHost.innerHTML = `<div class="error">${escapeHtml(errText)}</div>`;
      throw new Error(`Backend error (${res.status})`);
    }

    const data = await res.json();
    lastDiagram    = (data.diagram || '').trim();
    lastTf         = (data.terraform || '').trim();
    lastCost       = data.cost || null;
    lastConfluence = (data.confluence_doc || '').trim();

    const safeDiagram = lastMileSanitize(lastDiagram);
    await renderMermaidToSvg(safeDiagram);
    renderTerraform(lastTf);
    renderPricing(lastCost);
    renderConfluence(lastConfluence);

    statusEl.textContent = 'Done.';
  } catch (e) {
    console.error(e);
    statusEl.textContent = e.message || 'Request failed.';
    diagramHost.innerHTML = `<div class="error">❌ Unable to render diagram</div>`;
  } finally {
    btnGenerate.disabled = false;
  }
}

async function renderMermaidToSvg(diagramText) {
  const id = 'arch-' + Math.random().toString(36).slice(2, 9);

  const cleaned = diagramText
    .split("\n")
    .filter(line => !line.trim().startsWith("classDef") && !line.trim().startsWith("style"))
    .join("\n");

  try {
    const { svg } = await mermaid.render(id, cleaned);
    lastSvg = svg;
    diagramHost.innerHTML = svg;
    diagramHost.querySelector('svg')?.setAttribute('width', '100%');
  } catch (err) {
    console.error('Mermaid render error', err);
    diagramHost.innerHTML = `<div class="mermaid">${cleaned}</div>`;
    mermaid.contentLoaded();
  }
}

function renderTerraform(tf) { tfOut.value = tf || ''; }

function renderPricing(costObj) {
  if (!costObj || !Array.isArray(costObj.items) || !costObj.items.length) {
    pricingDiv.innerHTML = '<p class="muted">No cost data.</p>';
    return;
  }
  const rows = costObj.items.map(it => `
      <tr>
        <td>${String(it.cloud || '')}</td>
        <td>${String(it.resource || it.service || '')}</td>
        <td>${escapeHtml(String(it.sku || ''))}</td>
        <td>${escapeHtml(String(it.region || ''))}</td>
        <td style="text-align:right">${it.qty}</td>
        <td style="text-align:right">$${Number(it.unit_monthly || 0).toFixed(2)}</td>
        <td style="text-align:right">$${Number(it.monthly || 0).toFixed(2)}</td>
      </tr>`).join('');
  const total = Number(costObj.total_estimate || 0).toFixed(2);
  pricingDiv.innerHTML = `
    <table>
      <thead>
        <tr>
          <th>Cloud</th><th>Resource</th><th>SKU</th><th>Region</th>
          <th style="text-align:right">Qty</th>
          <th style="text-align:right">Unit/Month</th>
          <th style="text-align:right">Monthly</th>
        </tr>
      </thead>
      <tbody>${rows}</tbody>
      <tfoot>
        <tr>
          <td colspan="6" style="text-align:right">Total (${costObj.currency || 'USD'})</td>
          <td style="text-align:right"><b>$${total}</b></td>
        </tr>
      </tfoot>
    </table>`;
}

function renderConfluence(text) {
  if (!confluenceBox) return;
  confluenceBox.value = text || 'No Confluence documentation available.';
  btnCopyConf.style.display = text ? 'inline-block' : 'none';
  btnDlConf.style.display   = text ? 'inline-block' : 'none';
}

function escapeHtml(s) {
  return (s || '').replace(/[&<>"]/g, c => (
    {'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]
  ));
}

// === Buttons ===
btnSvg.addEventListener('click', () => {
  if (!lastSvg) return;
  const blob = new Blob([lastSvg], { type: 'image/svg+xml;charset=utf-8' });
  const a = document.createElement('a');
  a.href = URL.createObjectURL(blob);
  a.download = 'archgenie-diagram.svg';
  a.click(); URL.revokeObjectURL(a.href);
});

btnPng.addEventListener('click', async () => {
  if (!lastSvg) return;
  const svgEl = new DOMParser().parseFromString(lastSvg, 'image/svg+xml').documentElement;
  const svgText = new XMLSerializer().serializeToString(svgEl);
  const canvas = document.createElement('canvas');
  const bbox = diagramHost.querySelector('svg')?.getBBox?.();
  const width = Math.max(1024, (bbox?.width || 1024));
  const height = Math.max(768, (bbox?.height || 768));
  canvas.width = width; canvas.height = height;
  const ctx = canvas.getContext('2d');
  const img = new Image();
  img.onload = () => {
    ctx.drawImage(img, 0, 0);
    const a = document.createElement('a');
    a.href = canvas.toDataURL('image/png');
    a.download = 'archgenie-diagram.png';
    a.click();
  };
  img.src = 'data:image/svg+xml;charset=utf-8,' + encodeURIComponent(svgText);
});

btnCopyTf.addEventListener('click', async () => {
  try {
    await navigator.clipboard.writeText(tfOut.value || '');
    btnCopyTf.textContent = 'Copied!';
    setTimeout(() => btnCopyTf.textContent = 'Copy', 1000);
  } catch(e) { console.error(e); }
});

btnDlTf.addEventListener('click', () => {
  const blob = new Blob([tfOut.value || ''], { type: 'text/plain;charset=utf-8' });
  const a = document.createElement('a');
  a.href = URL.createObjectURL(blob);
  a.download = 'main.tf';
  a.click(); URL.revokeObjectURL(a.href);
});

btnCopyConf.addEventListener('click', async () => {
  try {
    await navigator.clipboard.writeText(confluenceBox.value || '');
    btnCopyConf.textContent = 'Copied!';
    setTimeout(() => btnCopyConf.textContent = 'Copy', 1000);
  } catch(e) { console.error(e); }
});

btnDlConf.addEventListener('click', () => {
  const blob = new Blob([confluenceBox.value || ''], { type: 'text/plain;charset=utf-8' });
  const a = document.createElement('a');
  a.href = URL.createObjectURL(blob);
  a.download = 'archgenie_confluence.txt';
  a.click(); URL.revokeObjectURL(a.href);
});

btnCsv?.addEventListener('click', async () => {
  const appName  = appNameInput.value.trim() || 'archgenie-app';
  const region   = regionInput.value.trim() || 'eastus';
  const provider = providerInput ? providerInput.value.toLowerCase() : "azure";

  const payload = {
    app_name: appName,
    region: region,
    terraform: tfOut.value || ""
  };

  try {
    const resp = await fetch(`/api/mcp/${provider}/cost-csv`, {
      method: 'POST',
      headers: { "Content-Type": "application/json", "x-api-key": apiKeyInput.value.trim() },
      body: JSON.stringify(payload)
    });

    if (!resp.ok) throw new Error("Failed to download CSV");

    const blob = await resp.blob();
    const a = document.createElement('a');
    a.href = URL.createObjectURL(blob);
    a.download = `${appName.replace(/\s+/g,'_')}_${provider}_costs.csv`;
    a.click();
    URL.revokeObjectURL(a.href);

  } catch (err) {
    alert("Error downloading CSV: " + err.message);
  }
});

btnGenerate.addEventListener('click', callMcp);