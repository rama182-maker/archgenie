// Mermaid init (we render manually via API)
mermaid.initialize({ startOnLoad: false, securityLevel: 'strict' });

const el = (id) => document.getElementById(id);
const appNameInput = el('appName');
const promptInput  = el('prompt');
const regionInput  = el('region');
const apiKeyInput  = el('apiKey');
const btnGenerate  = el('btnGenerate');
const statusEl     = el('status');
const diagramHost  = el('diagramHost');
const tfHost       = el('terraformHost');
const costHost     = el('costHost');
const confluenceBox= el('confluenceDoc');
const copyBtn      = el('btnCopyConfluence');
const dlBtn        = el('btnDlConfluence');   // 🔹 added

btnGenerate.addEventListener('click', async () => {
  statusEl.textContent = "Generating...";
  diagramHost.innerHTML = "";
  tfHost.innerHTML = "";
  costHost.innerHTML = "";
  confluenceBox.value = "";
  copyBtn.style.display = "none";
  if (dlBtn) dlBtn.style.display = "none";   // 🔹 reset

  try {
    const payload = {
      resources: JSON.parse(promptInput.value || "[]"),
      region: regionInput.value || "eastus"
    };

    const resp = await fetch("http://localhost:8000/generate", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });

    const data = await resp.json();
    statusEl.textContent = "Done.";

    // Diagram
    if (data.diagram) {
      diagramHost.innerHTML = `<h2>Architecture Diagram</h2><div class="mermaid">${escapeHtml(data.diagram)}</div>`;
      mermaid.init(undefined, diagramHost.querySelectorAll(".mermaid"));
    } else {
      diagramHost.textContent = "No diagram generated.";
    }

    // Terraform
    if (data.terraform) {
      tfHost.innerHTML = `<h2>Terraform Code</h2><pre class="code-block">${escapeHtml(data.terraform)}</pre>`;
    }

    // Cost table
    if (data.cost && Object.keys(data.cost).length > 0) {
      let html = "<h2>Cost Estimates</h2><table><thead><tr><th>Resource</th><th>Monthly Cost</th></tr></thead><tbody>";
      for (const [res, val] of Object.entries(data.cost)) {
        html += `<tr><td>${escapeHtml(res)}</td><td>${escapeHtml(val)}</td></tr>`;
      }
      html += "</tbody></table>";
      costHost.innerHTML = html;
    }

    // Confluence doc
    if (data.confluence_doc) {
      confluenceBox.value = data.confluence_doc;
      copyBtn.style.display = "inline-block";
      if (dlBtn) dlBtn.style.display = "inline-block";  // 🔹 show download
    } else {
      confluenceBox.value = "No Confluence documentation available.";
      copyBtn.style.display = "none";
      if (dlBtn) dlBtn.style.display = "none";
    }

  } catch (err) {
    statusEl.textContent = "Error: " + err.message;
  }
});

function copyConfluence() {
  confluenceBox.select();
  document.execCommand("copy");
  alert("Confluence documentation copied!");
}

// 🔹 new function for download
function downloadConfluence() {
  const blob = new Blob([confluenceBox.value], { type: "text/plain" });
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  a.download = "archgenie_confluence.txt";
  a.click();
  URL.revokeObjectURL(url);
}

function escapeHtml(text) {
  if (!text) return "";
  return text.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
}