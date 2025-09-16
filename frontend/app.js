mermaid.initialize({ startOnLoad: false });

async function generate() {
  const appName = document.getElementById('appName').value || "Azure app";
  const prompt = document.getElementById('prompt').value || "";
  const region = document.getElementById('region').value || "eastus";
  const apiKey = document.getElementById('apiKey').value || "super-secret-key";

  document.getElementById('status').textContent = "loading...";

  try {
    const res = await fetch('/mcp/azure/diagram-tf', {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
        'x-api-key': apiKey
      },
      body: JSON.stringify({ app_name: appName, prompt, region })
    });

    if (!res.ok) throw new Error(`Backend error: ${res.status}`);
    const data = await res.json();

    // === Terraform ===
    document.getElementById('terraform').textContent = data.terraform || "";

    // === Diagram (newline fix) ===
    if (data.diagram) {
      const diagramText = data.diagram.replace(/\\n/g, "\n");
      mermaid.render('theGraph', diagramText, (svgCode) => {
        document.getElementById('diagram').innerHTML = svgCode;
      });
    } else {
      document.getElementById('diagram').innerHTML = "<em>No diagram received</em>";
    }

    // === Cost ===
    if (data.cost) {
      const cost = data.cost;
      let html = `<p><b>Total:</b> ${cost.currency} ${cost.total_estimate}</p>`;
      if (cost.items && cost.items.length > 0) {
        html += "<ul>";
        cost.items.forEach(i => {
          html += `<li>${i.service} (${i.sku}) x${i.qty}: ${i.monthly}</li>`;
        });
        html += "</ul>";
      }
      document.getElementById('cost').innerHTML = html;
    } else {
      document.getElementById('cost').innerHTML = "<em>No cost data</em>";
    }

    // Save for download
    window._lastTF = data.terraform || "";
    window._lastCost = data.cost || {};

    document.getElementById('status').textContent = "ready";

  } catch (err) {
    console.error(err);
    document.getElementById('status').textContent = "error";
  }
}

document.getElementById('generateBtn').onclick = generate;

// === Download / Copy helpers ===
document.getElementById('downloadTF').onclick = () => {
  const blob = new Blob([window._lastTF || ""], { type: 'text/plain' });
  const a = document.createElement('a');
  a.href = URL.createObjectURL(blob);
  a.download = 'main.tf';
  a.click();
};

document.getElementById('copyTF').onclick = () => {
  if (window._lastTF) navigator.clipboard.writeText(window._lastTF);
};

document.getElementById('downloadCSV').onclick = () => {
  const cost = window._lastCost || {};
  let csv = "service,sku,qty,monthly\n";
  (cost.items || []).forEach(i => {
    csv += `${i.service},${i.sku},${i.qty},${i.monthly}\n`;
  });
  const blob = new Blob([csv], { type: 'text/csv' });
  const a = document.createElement('a');
  a.href = URL.createObjectURL(blob);
  a.download = 'cost.csv';
  a.click();
};