import os, re, json, requests
from typing import List, Dict, Any
from fastapi import FastAPI, Depends, Header, HTTPException, Body
from fastapi.middleware.cors import CORSMiddleware
from dotenv import load_dotenv

load_dotenv()

CAL_API_KEY = os.getenv("CAL_API_KEY", "super-secret-key")

AZURE_OPENAI_ENDPOINT    = (os.getenv("AZURE_OPENAI_ENDPOINT", "") or "").rstrip("/")
AZURE_OPENAI_API_KEY     = os.getenv("AZURE_OPENAI_API_KEY", "")
AZURE_OPENAI_DEPLOYMENT  = os.getenv("AZURE_OPENAI_DEPLOYMENT", "")
AZURE_OPENAI_API_VERSION = os.getenv("AZURE_OPENAI_API_VERSION", "2024-12-01-preview")
AZURE_OPENAI_FORCE_JSON  = os.getenv("AZURE_OPENAI_FORCE_JSON", "true").lower() == "true"

DEFAULT_REGION = "eastus"
FAIL_OPEN = os.getenv("FAIL_OPEN", "true").lower() == "true"

def require_api_key(x_api_key: str = Header(None)):
    if not x_api_key or x_api_key != CAL_API_KEY:
        raise HTTPException(status_code=401, detail="Invalid or missing API key")

app = FastAPI(title="ArchGenie Azure Backend", version="dynamic-costs")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"], allow_credentials=True,
    allow_methods=["*"], allow_headers=["*"],
)

@app.get("/")
def health():
    return {"status": "ok"}

# === Helpers ===
def sanitize_mermaid(src: str) -> str:
    if not src:
        return "graph TD\nA[Internet] --> B[App Service]\nB --> C[Azure SQL]\n"
    s = src.strip()
    header_re = re.compile(r'^(graph|flowchart)\s+(TD|LR)\b', flags=re.I|re.M)
    if header_re.search(s):
        s = header_re.sub("graph TD", s, count=1)
    else:
        s = "graph TD\n" + s
    lines = []
    for line in s.splitlines():
        just = line.strip()
        just = re.sub(r';\s*$', '', just)
        if just:
            lines.append(just)
    s = "\n".join(lines)
    s = re.sub(r'\[([^\]]+)\]', lambda m: "[" + m.group(1).replace("\n"," ").replace(",","") + "]", s)
    s = re.sub(r'[ \t]+', ' ', s)
    if not s.endswith("\n"):
        s += "\n"
    return s

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

# === Azure Pricing API ===
def get_azure_price(service_name: str, sku: str, region: str="eastus") -> float:
    try:
        url = (
            f"https://prices.azure.com/api/retail/prices?"
            f"$filter=serviceName eq '{service_name}' "
            f"and armSkuName eq '{sku}' "
            f"and armRegionName eq '{region}'"
        )
        r = requests.get(url, timeout=20)
        if r.status_code != 200:
            return 0.0
        data = r.json()
        items = data.get("Items", [])
        if not items:
            return 0.0
        return items[0].get("retailPrice", 0.0)
    except Exception:
        return 0.0

# === Parse Terraform for Resources ===
def parse_resources_from_tf(tf: str) -> List[dict]:
    resources = []
    if not tf:
        return resources

    # Simple regex-based extraction for known services
    if "azurerm_app_service" in tf:
        resources.append({"cloud":"azure","service":"app_service","sku":"S1","qty":1})
    if "azurerm_kubernetes_cluster" in tf:
        resources.append({"cloud":"azure","service":"aks","sku":"Standard_D4s_v3","qty":3})
    if "azurerm_sql_server" in tf or "azurerm_sql_database" in tf:
        resources.append({"cloud":"azure","service":"azure_sql","sku":"S0","qty":1})
    if "azurerm_cosmosdb_account" in tf:
        resources.append({"cloud":"azure","service":"cosmosdb","sku":"Standard","qty":1})
    if "azurerm_storage_account" in tf:
        resources.append({"cloud":"azure","service":"storage","sku":"LRS","qty":1})
    if "azurerm_key_vault" in tf:
        resources.append({"cloud":"azure","service":"keyvault","sku":"Standard","qty":1})
    if "azurerm_application_insights" in tf:
        resources.append({"cloud":"azure","service":"app_insights","sku":"Standard","qty":1})

    return resources

# === Pricing Calculator ===
def price_items(resources: List[dict], region: str) -> dict:
    total = 0.0
    out = []

    for it in resources:
        service = it["service"]
        sku = it["sku"]
        qty = it.get("qty",1)

        unit_price = 0.0
        monthly = 0.0

        if service == "app_service":
            unit_price = get_azure_price("App Service", sku, region)
            monthly = (unit_price * 730) * qty if unit_price else 50.0 * qty

        elif service == "aks":
            unit_price = get_azure_price("Virtual Machines", sku, region)
            monthly = (unit_price * 730) * qty if unit_price else 100.0 * qty

        elif service == "azure_sql":
            unit_price = get_azure_price("SQL Database", sku, region)
            monthly = (unit_price * 730) * qty if unit_price else 75.0 * qty

        elif service == "cosmosdb":
            unit_price = get_azure_price("Azure Cosmos DB", sku, region)
            monthly = unit_price * qty if unit_price else 25.0 * qty

        elif service == "storage":
            unit_price = get_azure_price("Storage", sku, region)
            monthly = unit_price * qty if unit_price else 10.0 * qty

        elif service == "keyvault":
            unit_price = get_azure_price("Key Vault", sku, region)
            monthly = unit_price * qty if unit_price else 5.0 * qty

        elif service == "app_insights":
            unit_price = get_azure_price("Application Insights", sku, region)
            monthly = unit_price * qty if unit_price else 10.0 * qty

        total += monthly
        out.append({
            **it,
            "region": region,
            "unit_monthly": round(unit_price * 730,2) if unit_price else 0.0,
            "monthly": round(monthly,2)
        })

    return {"currency":"USD","total_estimate": round(total,2), "items": out}

# === Confluence doc builder ===
def make_confluence_doc(app_name: str, diagram: str, terraform: str, cost: dict) -> str:
    lines = []
    lines.append(f"h1. {app_name} – Architecture Documentation\n")
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

# === Main endpoint ===
@app.post("/mcp/azure/diagram-tf")
def azure_mcp(payload: dict = Body(...), _=Depends(require_api_key)):
    app_name = payload.get("app_name", "3-tier web app")
    extra = payload.get("prompt", "")
    region = payload.get("region", DEFAULT_REGION)

    system = (
        "You are ArchGenie's Azure MCP. "
        "Generate a detailed Azure reference architecture diagram in Mermaid. "
        "Use subgraphs for tiers (Networking, Web, App, Data, Monitoring). "
        "Use proper Azure resource names (e.g., 'Azure Application Gateway', 'Azure App Service', 'Azure SQL Database'). "
        "Return JSON ONLY with keys: "
        '{"diagram": "Mermaid code", "terraform": "Terraform HCL"}'
    )
    user = f"Create Azure architecture for {app_name}. Extra: {extra}. Region: {region}."

    try:
        result = aoai_chat([{"role":"system","content":system},{"role":"user","content":user}])
        content = result["choices"][0]["message"]["content"]
        parsed = extract_json_or_fences(content)

        diagram = sanitize_mermaid(parsed.get("diagram", ""))
        if not diagram.strip():
            diagram = "graph TD\nA[Internet] --> B[App Service]\nB --> C[Azure SQL]\n"

        tf = strip_fences(parsed.get("terraform",""))
        if not tf.strip():
            tf = "# Terraform failed; check backend logs"

    except Exception:
        if not FAIL_OPEN:
            raise
        diagram = "graph TD\nA[Internet] --> B[App Service]\nB --> C[Azure SQL]\n"
        tf = "# Terraform failed; check backend logs"

    # Extract resources from Terraform
    resources = parse_resources_from_tf(tf)
    cost = price_items(resources, region)
    confluence_doc = make_confluence_doc(app_name, diagram, tf, cost)

    return {
        "diagram": diagram,
        "terraform": tf,
        "cost": cost,
        "confluence_doc": confluence_doc
    }