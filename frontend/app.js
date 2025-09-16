mermaid.initialize({ startOnLoad: false });

function showToast(msg) {
  const t = document.getElementById('toast');
  t.textContent = msg;
  t.style.display = 'block';
  setTimeout(()=>{ t.style.display='none'; }, 3000);
}

async function generate() {
  const prompt = document.getElementById('prompt').value;
  const res = await fetch('/mcp/azure/diagram-tf', {
    method:'POST',
    headers:{'Content-Type':'application/json','x-api-key':'super-secret-key'},
    body: JSON.stringify({app_name:"Azure app", prompt, region:"eastus"})
  });
  const data = await res.json();
  document.getElementById('terraform').textContent = data.terraform;
  // Render diagram
  mermaid.render('theGraph', data.diagram, svgCode => {
    document.getElementById('diagram').innerHTML = svgCode;
  });
  // Cost summary
  const cost = data.cost;
  let html = `<p><b>Total:</b> ${cost.currency} ${cost.total_estimate}</p>`;
  html += '<ul>'; cost.items.forEach(i=>{ html += `<li>${i.service} ${i.sku}: ${i.monthly}</li>`; }); html+='</ul>';
  document.getElementById('cost').innerHTML = html;
  window._lastDiagram = data.diagram;
  window._lastTF = data.terraform;
  window._lastCost = cost;
}

document.getElementById('generateBtn').onclick = generate;

document.getElementById('downloadTF').onclick = ()=>{
  const blob = new Blob([window._lastTF||''],{type:'text/plain'});
  const a=document.createElement('a'); a.href=URL.createObjectURL(blob); a.download='main.tf'; a.click();
  showToast('Terraform downloaded');
};

document.getElementById('copyTF').onclick = ()=>{
  navigator.clipboard.writeText(window._lastTF||''); showToast('Terraform copied to clipboard');
};

document.getElementById('downloadCSV').onclick = ()=>{
  const cost = window._lastCost||{};
  let csv="service,sku,qty,monthly\n";
  (cost.items||[]).forEach(i=>{ csv += `${i.service},${i.sku},${i.qty},${i.monthly}\n`; });
  const blob = new Blob([csv],{type:'text/csv'});
  const a=document.createElement('a'); a.href=URL.createObjectURL(blob); a.download='cost.csv'; a.click();
  showToast('Cost breakdown downloaded');
};

document.getElementById('downloadDiagram').onclick = ()=>{
  const svg = document.querySelector('#diagram svg');
  if(!svg){ showToast('No diagram'); return; }
  const xml = new XMLSerializer().serializeToString(svg);
  const canvas = document.createElement('canvas');
  const ctx = canvas.getContext('2d');
  const img = new Image();
  const blob = new Blob([xml],{type:'image/svg+xml;charset=utf-8'});
  const url = URL.createObjectURL(blob);
  img.onload = ()=>{
    canvas.width=img.width; canvas.height=img.height;
    ctx.drawImage(img,0,0);
    URL.revokeObjectURL(url);
    canvas.toBlob(b=>{
      const a=document.createElement('a');
      a.href=URL.createObjectURL(b); a.download='diagram.png'; a.click();
      showToast('Diagram downloaded');
    });
  };
  img.src=url;
};
