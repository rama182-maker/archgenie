import os, re, json, requests, csv, boto3, hcl2, yaml
from typing import List, Dict, Any
from io import StringIO
from fastapi import FastAPI, Depends, Header, HTTPException, Body
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from dotenv import load_dotenv

load_dotenv()

CAL_API_KEY = os.getenv("CAL_API_KEY", "super-secret-key")

# === Azure OpenAI Config ===
AZURE_OPENAI_ENDPOINT    = (os.getenv("AZURE_OPENAI_ENDPOINT", "") or "").rstrip("/")
AZURE_OPENAI_API_KEY     = os.getenv("AZURE_OPENAI_API_KEY", "")
AZURE_OPENAI_DEPLOYMENT  = os.getenv("AZURE_OPENAI_DEPLOYMENT", "")
AZURE_OPENAI_API_VERSION = os.getenv("AZURE_OPENAI_API_VERSION", "2024-12-01-preview")
AZURE_OPENAI_FORCE_JSON  = os.getenv("AZURE_OPENAI_FORCE_JSON", "true").lower() == "true"

DEFAULT_REGION = "eastus"
FAIL_OPEN = os.getenv("FAIL_OPEN", "true").lower() == "true"

# === Load Service Mappings ===
with open("mappings.yaml", "r") as f:
    SERVICE_MAPPINGS = yaml.safe_load(f)

# === FastAPI App ===
def require_api_key(x_api_key: str = Header(None)):
    if not x_api_key or x_api_key != CAL_API_KEY:
        raise HTTPException(status_code=401, detail="Invalid or missing API key")

app = FastAPI(title="ArchGenie Multi-Cloud Backend", version="aoai-only")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"], allow_credentials=True,
    allow_methods=["*"], allow_headers=["*"],
)

@app.get("/")
def health():
    return {"status": "ok"}

# -----------------------------------------------------------------
# Helpers
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
    if not text:
        return ""
    s = text.strip()
    # Match terraform, hcl, or generic code fences
    m = re.match(r"^```(?:terraform|hcl)?\s*\n([\s\S]*?)```$", s, re.I)
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
        m = re.search(r"```(terraform|hcl)?\s*\n([\s\S]*?)```", content, re.I)
        if m: out["terraform"] = m.group(2).strip()
        return out

def parse_tf_resources(tf: str) -> List[dict]:
    if not tf.strip():
        return []
    resources = []
    try:
        obj = hcl2.load(StringIO(tf))
        for res in obj.get("resource", []):
            for rtype, blocks in res.items():
                for name, attrs in blocks.items():
                    resources.append({"type": rtype, "name": name, "attrs": attrs})
    except Exception as e:
        print("⚠️ Error parsing TF:", e)
    return resources

# -----------------------------------------------------------------
# Azure OpenAI Chat
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

# -----------------------------------------------------------------
# Pricing APIs
# -----------------------------------------------------------------
def get_azure_price(service_name: str, sku: str, region: str="eastus") -> float:
    try:
        url = (
            f"https://prices.azure.com/api/retail/prices?"
            f"$filter=serviceName eq '{service_name}' and armSkuName eq '{sku}' and armRegionName eq '{region}'"
        )
        r = requests.get(url, timeout=20)
        if r.status_code != 200: return 0.0
        data = r.json()
        items = data.get("Items", [])
        return items[0].get("retailPrice", 0.0) if items else 0.0
    except Exception:
        return 0.0

def get_aws_price(service_code: str, key: str, value: str) -> float:
    client = boto3.client("pricing", region_name="us-east-1")
    try:
        resp = client.get_products(
            ServiceCode=service_code,
            Filters=[{"Type": "TERM_MATCH", "Field": key, "Value": value}],
            MaxResults=1
        )
        if not resp["PriceList"]: return 0.0
        terms = json.loads(resp["PriceList"][0])
        od = list(terms["terms"]["OnDemand"].values())[0]
        price_dims = list(od["priceDimensions"].values())[0]
        return float(price_dims["pricePerUnit"]["USD"])
    except Exception as e:
        print("⚠️ AWS pricing error:", e)
        return 0.0

# -----------------------------------------------------------------
# Cost Estimator (YAML-driven)
# -----------------------------------------------------------------
def estimate_cost(provider: str, tf: str, region: str) -> dict:
    resources = parse_tf_resources(tf)
    mapping = SERVICE_MAPPINGS.get(provider, {})
    total, items = 0.0, []

    for res in resources:
        rtype, attrs = res["type"], res["attrs"]
        if rtype not in mapping:
            continue  # skip if not in YAML

        cfg = mapping[rtype]
        # Extract SKU from TF attributes
        sku = None
        if cfg.get("attr"):
            parts = cfg["attr"].split(".")
            val = attrs
            for p in parts:
                if isinstance(val, dict):
                    val = val.get(p)
            sku = val

        qty = int(attrs.get("count", 1))

        # Lookup pricing
        unit_price = 0.0
        if provider == "azure":
            unit_price = get_azure_price(cfg["service"], sku, region)
        elif provider == "aws":
            unit_price = get_aws_price(cfg["service_code"], cfg["key"], sku)

        # Compute monthly
        monthly = 0.0
        if cfg["billing"] == "hourly":
            monthly = unit_price * 730 * qty
        elif cfg["billing"] == "per_gb":
            size = int(attrs.get("storage_gb", 100))
            monthly = unit_price * size * qty
        else:
            monthly = unit_price * qty

        total += monthly
        items.append({
            "cloud": provider,
            "resource": rtype,
            "sku": sku,
            "qty": qty,
            "region": region,
            "unit_monthly": round(monthly/qty, 2) if qty else 0,
            "monthly": round(monthly, 2)
        })

    return {"currency":"USD", "total_estimate": round(total, 2), "items": items}

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
        lines.append("|| Cloud || Resource || SKU || Qty || Unit/Month || Monthly ||")
        for it in cost["items"]:
            lines.append(f"| {it.get('cloud','')} | {it.get('resource','')} | {it.get('sku','')} | {it.get('qty',1)} | ${it.get('unit_monthly',0)} | ${it.get('monthly',0)} |")
        lines.append(f"*Total ({cost.get('currency','USD')}):* ${cost.get('total_estimate',0)}")
    else:
        lines.append("No cost data available.")
    return "\n".join(lines)

# -----------------------------------------------------------------
# Endpoints
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
        # Unified AOAI flow for both Azure and AWS
        system = (
            f"You are ArchGenie's {provider.upper()} MCP. "
            f"Generate a detailed {provider.upper()} reference architecture "
            "diagram in Mermaid and Terraform. Return JSON ONLY with keys: "
            '{"diagram": "Mermaid code", "terraform": "Terraform HCL"}'
        )
        user = f"Create {provider.upper()} architecture for {app_name}. Extra: {extra}. Region: {region}."
        result = aoai_chat([{"role":"system","content":system},{"role":"user","content":user}])
        content = result["choices"][0]["message"]["content"]
        print(f"AOAI raw response ({provider.upper()}):", content)  # DEBUG
        parsed = extract_json_or_fences(content)
        diagram = sanitize_mermaid(parsed.get("diagram",""))
        tf = strip_fences(parsed.get("terraform",""))
        if not tf.strip():
            tf = "# Terraform generation failed; check backend logs"

    except Exception:
        if not FAIL_OPEN: raise
        diagram = "graph TD\nA[Internet] --> B[App]\nB --> C[Database]\n"
        tf = "# Terraform generation failed; check backend logs"

    cost = estimate_cost(provider, tf, region)
    confluence_doc = make_confluence_doc(app_name, diagram, tf, cost, provider)

    return {"diagram":diagram,"terraform":tf,"cost":cost,"confluence_doc":confluence_doc}

@app.post("/mcp/{provider}/cost-csv")
def cost_csv(provider: str, payload: dict = Body(...), _=Depends(require_api_key)):
    app_name = payload.get("app_name","3-tier web app")
    region = payload.get("region", DEFAULT_REGION)
    tf = payload.get("terraform","")

    cost = estimate_cost(provider, tf, region)
    output = StringIO()
    writer = csv.writer(output)
    writer.writerow(["Cloud","Resource","SKU","Region","Qty","Unit/Month (USD)","Monthly (USD)"])
    for it in cost["items"]:
        writer.writerow([
            it.get("cloud",""),
            it.get("resource",""),
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