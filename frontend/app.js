mermaid.initialize({ startOnLoad: false });

function showToast(msg) {
  const t = document.getElementById('toast');
  if (!t) return;
  t.textContent = msg;
  t.style.display = 'block';
  setTimeout(() => { t.style.display = 'none'; }, 3000);
}

async function generate() {
  const prompt = document.getElementById('prompt').value;

  try {
    const res = await fetch('/mcp/azure/diagram-tf', {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
        'x-api-key': 'super-secret-key'
      },
      body: JSON.stringify({
        app_name: "Azure app",
        prompt,
        region: "eastus"
      })
    });

    if (!res.ok) {
      throw new Error(`Backend error: ${res.status}`);
    }

    const data = await res.json();

    // === Terraform code ===
    document.getElementById('terraform').textContent = data.terraform || '';

    // === Diagram (decode newlines for Mermaid) ===
    if (data.diagram) {
      const diagramText = data.diagram.replace(/\\n/g, "\n");
      try {
        mermaid.render('theGraph', diagramText, (svgCode) => {
          document.getElementById('diagram').innerHTML = svgCode;
        });
      } catch (err) {
        console.error("Mermaid parse error:", err);
        document.getElementById('diagram').innerHTML = `<pre>${diagramText}</pre>`;
        showToast("⚠️ Diagram could not be parsed, showing raw text.");
      }
    } else {
      document.getElementById('diagram').innerHTML = "<em>No diagram received</em>";
    }

    // === Cost summary ===
    if (data.cost) {
      const cost = data.cost;
      let html = `<p><b>Total:</b> ${cost.currency} ${cost.total_estimate}</p>`;
      if (cost.items && cost.items.length > 0) {
        html += '<ul>';
        cost.items.forEach(i => {
          html += `<li>${i.service} (${i.sku}) x${i.qty}: ${i.monthly}</li>`;
        });
        html += '</ul>';
      }
      document.getElementById('cost').innerHTML = html;
    } else {
      document.getElementById('cost').innerHTML = "<em>No cost data</em>";
    }

    // Save for download actions
    window._lastDiagram = data.diagram;
    window._lastTF = data.terraform;
    window._lastCost = data.cost;

  } catch (err) {
    console.error(err);
    showToast("❌ Failed to generate architecture");
  }
}

// === Button bindings ===
document.getElementById('generateBtn').onclick = generate;

document.getElementById('downloadTF').onclick = () => {
  const blob = new Blob([window._lastTF || ''], { type: 'text/plain' });
  const a = document.createElement('a');
  a.href = URL.createObjectURL(blob);
  a.download = 'main.tf';
  a.click();
  showToast('Terraform downloaded');
};

document.getElementById('copyTF').onclick = () => {
  if (window._lastTF) {
    navigator.clipboard.writeText(window._lastTF);
    showToast('Terraform copied to clipboard');
  }
};

document.getElementById('downloadCSV').onclick = () => {
  const cost = window._lastCost || {};
  let csv = "service,sku,qty,monthly\\n";
  (cost.items || []).forEach(i => {
    csv += `${i.service},${i.sku},${i.qty},${i.monthly}\\n`;
  });
  const blob = new Blob([csv], { type: 'text/csv' });
  const a = document.createElement('a');
  a.href = URL.createObjectURL(blob);
  a.download = 'cost.csv';
  a.click();
  showToast('Cost breakdown downloaded');
};

document.getElementById('downloadDiagram').onclick = () => {
  const svg = document.querySelector('#diagram svg');
  if (!svg) { showToast('No diagram'); return; }
  const xml = new XMLSerializer().serializeToString(svg);
  const canvas = document.createElement('canvas');
  const ctx = canvas.getContext('2d');
  const img = new Image();
  const blob = new Blob([xml], { type: 'image/svg+xml;charset=utf-8' });
  const url = URL.createObjectURL(blob);
  img.onload = () => {
    canvas.width = img.width;
    canvas.height = img.height;
    ctx.drawImage(img, 0, 0);
    URL.revokeObjectURL(url);
    canvas.toBlob(b => {
      const a = document.createElement('a');
      a.href = URL.createObjectURL(b);
      a.download = 'diagram.png';
      a.click();
      showToast('Diagram downloaded');
    });
  };
  img.src = url;
};