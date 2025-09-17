import os, re, json, requests, csv, subprocess
from typing import List, Dict, Any
from fastapi import FastAPI, Depends, Header, HTTPException, Body
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from dotenv import load_dotenv
from io import StringIO

load_dotenv()

CAL_API_KEY = os.getenv("CAL_API_KEY", "super-secret-key")

# === Azure Config ===
AZURE_OPENAI_ENDPOINT    = (os.getenv("AZURE_OPENAI_ENDPOINT", "") or "").rstrip("/")
AZURE_OPENAI_API_KEY     = os.getenv("AZURE_OPENAI_API_KEY", "")
AZURE_OPENAI_DEPLOYMENT  = os.getenv("AZURE_OPENAI_DEPLOYMENT", "")
AZURE_OPENAI_API_VERSION = os.getenv("AZURE_OPENAI_API_VERSION", "2024-12-01-preview")
AZURE_OPENAI_FORCE_JSON  = os.getenv("AZURE_OPENAI_FORCE_JSON", "true").lower() == "true"

# === AWS Config ===
AWS_MCP_HOST = os.getenv("AWS_MCP_HOST", "127.0.0.1")
AWS_MCP_PORT = os.getenv("AWS_MCP_PORT", "3333")

DEFAULT_REGION = "eastus"
FAIL_OPEN = os.getenv("FAIL_OPEN", "true").lower() == "true"

# === FastAPI App ===
def require_api_key(x_api_key: str = Header(None)):
    if not x_api_key or x_api_key != CAL_API_KEY:
        raise HTTPException(status_code=401, detail="Invalid or missing API key")

app = FastAPI(title="ArchGenie Multi-Cloud Backend", version="multi-cloud")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"], allow_credentials=True,
    allow_methods=["*"], allow_headers=["*"],
)

@app.get("/")
def health():
    return {"status": "ok"}

# -----------------------------------------------------------------
# Common Helpers
# -----------------------------------------------------------------
def sanitize_mermaid(src: str) -> str:
    if not src:
        return "graph TD\nA[Internet] --> B[App]\nB --> C[Database]\n"
    s = src.strip()
    header_re = re.compile(r'^(graph|flowchart)\s+(TD|LR)\b', flags=re.I|re.M)
    if header_re.search(s):
        s = header_re.sub("graph TD", s, count=1)
    else:
        s = "graph TD\n" + s
    lines = []
    for line in s.splitlines():
        just = re.sub(r';\s*$', '', line.strip())
        if just: lines.append(just)
    return "\n".join(lines) + ("\n" if not s.endswith("\n") else "")

def strip_fences(text: str) -> str:
    if not text: return ""
    s = text.strip()
    m = re.match(r"^```.*?\n([\s\S]*?)```$", s)
    return m.group(1).strip() if m else s

def extract_json_or_fences(content: str) -> Dict[str, str]:
    try:
        obj = json.loads(content)
        return {
            "diagram": strip_fences(obj.get("diagram","")),
            "terraform": strip_fences(obj.get("terraform",""))
        }
    except Exception:
        out = {"diagram":"","terraform":""}
        m = re.search(r"```mermaid\s*\n([\s\S]*?)```", content, re.I)
        if m: out["diagram"] = m.group(1).strip()
        m = re.search(r"```(terraform|hcl)\s*\n([\s\S]*?)```", content, re.I)
        if m: out["terraform"] = m.group(2).strip()
        return out

# -----------------------------------------------------------------
# Azure Handlers
# -----------------------------------------------------------------
def aoai_chat(messages: List[Dict[str,str]]) -> Dict[str,Any]:
    if not (AZURE_OPENAI_ENDPOINT and AZURE_OPENAI_API_KEY and AZURE_OPENAI_DEPLOYMENT):
        raise HTTPException(status_code=500, detail="Azure OpenAI not configured")
    url = f"{AZURE_OPENAI_ENDPOINT}/openai/deployments/{AZURE_OPENAI_DEPLOYMENT}/chat/completions?api-version={AZURE_OPENAI_API_VERSION}"
    headers = {"Content-Type":"application/json","api-key":AZURE_OPENAI_API_KEY}
    body = {"messages": messages, "temperature":0.2}
    if AZURE_OPENAI_FORCE_JSON:
        body["response_format"] = {"type":"json_object"}
    r = requests.post(url, headers=headers, data=json.dumps(body), timeout=60)
    if r.status_code>=300: raise HTTPException(status_code=r.status_code, detail=r.text)
    return r.json()

def get_azure_price(service_name: str, sku: str, region: str="eastus") -> float:
    try:
        url = (
            f"https://prices.azure.com/api/retail/prices?"
            f"$filter=serviceName eq '{service_name}' "
            f"and armSkuName eq '{sku}' "
            f"and armRegionName eq '{region}'"
        )
        r = requests.get(url, timeout=20)
        if r.status_code != 200: return 0.0
        data = r.json()
        items = data.get("Items", [])
        return items[0].get("retailPrice", 0.0) if items else 0.0
    except Exception:
        return 0.0

def parse_azure_resources(tf: str) -> List[dict]:
    resources = []
    if not tf: return resources
    if "azurerm_app_service" in tf:
        resources.append({"cloud":"azure","service":"app_service","sku":"S1","qty":1})
    if "azurerm_kubernetes_cluster" in tf:
        resources.append({"cloud":"azure","service":"aks","sku":"Standard_D4s_v3","qty":3})
    if "azurerm_sql_" in tf:
        resources.append({"cloud":"azure","service":"azure_sql","sku":"S0","qty":1})
    if "azurerm_storage_account" in tf:
        resources.append({"cloud":"azure","service":"storage","sku":"LRS","qty":1})
    return resources

def price_azure(resources: List[dict], region: str) -> dict:
    total = 0.0
    out = []
    for it in resources:
        service = it["service"]
        sku = it["sku"]
        qty = it.get("qty",1)
        monthly = 0.0
        if service == "app_service":
            p = get_azure_price("App Service", sku, region)
            monthly = (p*730)*qty if p else 50.0*qty
        elif service == "aks":
            p = get_azure_price("Virtual Machines", sku, region)
            monthly = (p*730)*qty if p else 100.0*qty
        elif service == "azure_sql":
            p = get_azure_price("SQL Database", sku, region)
            monthly = (p*730)*qty if p else 75.0*qty
        elif service == "storage":
            p = get_azure_price("Storage", sku, region)
            monthly = p*qty if p else 10.0*qty
        total += monthly
        out.append({**it,"region":region,"unit_monthly":round(monthly/qty,2),"monthly":round(monthly,2)})
    return {"currency":"USD","total_estimate":round(total,2),"items":out}

# -----------------------------------------------------------------
# AWS Handlers
# -----------------------------------------------------------------
def aws_mcp_query(app_name: str, extra: str, region: str) -> Dict[str,str]:
    prompt = f"Create AWS architecture for {app_name}. Extra: {extra}. Region: {region}."
    cmd = [
        "npm", "exec", "mcp-proxy",
        "uvx", "awslabs.aws-diagram-mcp-server",
        "--host", AWS_MCP_HOST, "--port", AWS_MCP_PORT,
        "--input", prompt
    ]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        raise HTTPException(status_code=500, detail=f"AWS MCP error: {result.stderr}")
    return extract_json_or_fences(result.stdout)

def parse_aws_resources(tf: str) -> List[dict]:
    resources = []
    if not tf: return resources
    if "aws_instance" in tf:
        resources.append({"cloud":"aws","service":"ec2","sku":"t3.medium","qty":1})
    if "aws_s3_bucket" in tf:
        resources.append({"cloud":"aws","service":"s3","sku":"standard","qty":1})
    if "aws_rds_instance" in tf:
        resources.append({"cloud":"aws","service":"rds","sku":"db.t3.medium","qty":1})
    if "aws_lambda_function" in tf:
        resources.append({"cloud":"aws","service":"lambda","sku":"1M req","qty":1})
    return resources

def price_aws(resources: List[dict], region: str) -> dict:
    total = 0.0
    out = []
    for it in resources:
        service = it["service"]; qty = it.get("qty",1)
        monthly = 0.0
        if service=="ec2": monthly = 30.0*qty
        elif service=="s3": monthly = 5.0*qty
        elif service=="rds": monthly = 50.0*qty
        elif service=="lambda": monthly = 1.0*qty
        total += monthly
        out.append({**it,"region":region,"unit_monthly":monthly,"monthly":monthly})
    return {"currency":"USD","total_estimate": round(total,2),"items":out}

# -----------------------------------------------------------------
# Confluence Builder
# -----------------------------------------------------------------
def make_confluence_doc(app_name: str, diagram: str, terraform: str, cost: dict, cloud: str) -> str:
    lines = []
    lines.append(f"h1. {app_name} – {cloud.upper()} Architecture Documentation\n")
    lines.append("h2. Architecture Diagram")
    lines.append("{code:mermaid}")
    lines.append(diagram.strip())
    lines.append("{code}\n")
    lines.append("h2. Terraform Code")
    lines.append("{code}")
    lines.append(terraform.strip() or "# (no terraform)")
    lines.append("{code}\n")
    lines.append("h2. Estimated Monthly Cost")
    if cost and cost.get("items"):
        lines.append("|| Cloud || Service || SKU || Qty || Unit/Month || Monthly ||")
        for it in cost["items"]:
            lines.append(f"| {it.get('cloud','')} | {it.get('service','')} | {it.get('sku','')} | {it.get('qty',1)} | ${it.get('unit_monthly',0)} | ${it.get('monthly',0)} |")
        lines.append(f"*Total ({cost.get('currency','USD')}):* ${cost.get('total_estimate',0)}")
    else:
        lines.append("No cost data available.")
    return "\n".join(lines)

# -----------------------------------------------------------------
# Main Endpoints
# -----------------------------------------------------------------
@app.post("/mcp/{provider}/diagram-tf")
def generate(provider: str, payload: dict = Body(...), _=Depends(require_api_key)):
    app_name = payload.get("app_name","3-tier web app")
    extra = payload.get("prompt","")
    region = payload.get("region", DEFAULT_REGION)

    provider = provider.lower()
    if provider not in ["azure","aws"]:
        raise HTTPException(status_code=400, detail="Invalid provider. Use azure or aws.")

    try:
        if provider == "azure":
            system = (
                "You are ArchGenie's Azure MCP. Generate a detailed Azure reference architecture "
                "diagram in Mermaid and Terraform. Return JSON ONLY with keys: "
                '{"diagram": "Mermaid code", "terraform": "Terraform HCL"}'
            )
            user = f"Create Azure architecture for {app_name}. Extra: {extra}. Region: {region}."
            result = aoai_chat([{"role":"system","content":system},{"role":"user","content":user}])
            content = result["choices"][0]["message"]["content"]
            parsed = extract_json_or_fences(content)
        else:
            parsed = aws_mcp_query(app_name, extra, region)

        diagram = sanitize_mermaid(parsed.get("diagram",""))
        tf = strip_fences(parsed.get("terraform",""))
        if not tf.strip(): tf = "# Terraform failed; check backend logs"

    except Exception:
        if not FAIL_OPEN: raise
        diagram = "graph TD\nA[Internet] --> B[App]\nB --> C[Database]\n"
        tf = "# Terraform failed; check backend logs"

    # Pricing
    if provider == "azure":
        resources = parse_azure_resources(tf)
        cost = price_azure(resources, region)
    else:
        resources = parse_aws_resources(tf)
        cost = price_aws(resources, region)

    confluence_doc = make_confluence_doc(app_name, diagram, tf, cost, provider)
    return {"diagram":diagram,"terraform":tf,"cost":cost,"confluence_doc":confluence_doc}

@app.post("/mcp/{provider}/cost-csv")
def cost_csv(provider: str, payload: dict = Body(...), _=Depends(require_api_key)):
    app_name = payload.get("app_name","3-tier web app")
    region = payload.get("region", DEFAULT_REGION)
    tf = payload.get("terraform","")

    if provider=="azure":
        resources = parse_azure_resources(tf)
        cost = price_azure(resources, region)
    elif provider=="aws":
        resources = parse_aws_resources(tf)
        cost = price_aws(resources, region)
    else:
        raise HTTPException(status_code=400, detail="Invalid provider")

    output = StringIO()
    writer = csv.writer(output)
    writer.writerow(["Cloud","Service","SKU","Region","Qty","Unit/Month (USD)","Monthly (USD)"])
    for it in cost["items"]:
        writer.writerow([
            it.get("cloud",""),
            it.get("service",""),
            it.get("sku",""),
            it.get("region",""),
            it.get("qty",1),
            it.get("unit_monthly",0),
            it.get("monthly",0),
        ])
    writer.writerow([]); writer.writerow(["Total","","","","","",cost.get("total_estimate",0)])
    output.seek(0)

    return StreamingResponse(
        output,
        media_type="text/csv",
        headers={"Content-Disposition": f"attachment; filename={app_name.replace(' ','_')}_{provider}_costs.csv"}
    )