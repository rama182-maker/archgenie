import os, re, json, requests
from typing import List, Dict, Any
from fastapi import FastAPI, Depends, Header, HTTPException, Body
from fastapi.middleware.cors import CORSMiddleware

# === Config ===
CAL_API_KEY = os.getenv("CAL_API_KEY", "super-secret-key")
AZURE_OPENAI_ENDPOINT    = (os.getenv("AZURE_OPENAI_ENDPOINT", "") or "").rstrip("/")
AZURE_OPENAI_API_KEY     = os.getenv("AZURE_OPENAI_API_KEY", "")
AZURE_OPENAI_DEPLOYMENT  = os.getenv("AZURE_OPENAI_DEPLOYMENT", "")
AZURE_OPENAI_API_VERSION = os.getenv("AZURE_OPENAI_API_VERSION", "2024-12-01-preview")

DEFAULT_REGION = "eastus"
FAIL_OPEN = True

def require_api_key(x_api_key: str = Header(None)):
    if not x_api_key or x_api_key != CAL_API_KEY:
        raise HTTPException(status_code=401, detail="Invalid or missing API key")

app = FastAPI(title="ArchGenie Azure Backend", version="1.0")
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
        if just.endswith(";"): just = just[:-1]
        if just: lines.append(just)
    s = "\n".join(lines)
    s = re.sub(r'\[([^\]]+)\]', lambda m: "["+m.group(1).replace("\n"," ").replace(",","")+"]", s)
    return s + "\n"

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
    body = {"messages": messages, "temperature":0.2, "response_format":{"type":"json_object"}}
    r = requests.post(url, headers=headers, data=json.dumps(body), timeout=60)
    if r.status_code>=300: raise HTTPException(status_code=r.status_code, detail=r.text)
    return r.json()

# === Costing ===
def normalize_to_items(diagram:str,tf:str)->List[dict]:
    blob = f"{diagram}\n{tf}".lower()
    items=[]
    if "app service" in blob: items.append({"cloud":"azure","service":"app_service","sku":"S1","qty":2})
    if "sql" in blob: items.append({"cloud":"azure","service":"azure_sql","sku":"S0","qty":1})
    if "storage" in blob: items.append({"cloud":"azure","service":"storage","sku":"LRS","qty":1})
    return items

def price_items(items: List[dict]) -> dict:
    total = 0.0
    out = []
    for it in items:
        if it["service"]=="app_service":
            monthly = 50.0 * it.get("qty",1)
        elif it["service"]=="azure_sql":
            monthly = 75.0 * it.get("qty",1)
        elif it["service"]=="storage":
            monthly = 10.0 * it.get("qty",1)
        else:
            monthly = 20.0 * it.get("qty",1)
        total += monthly
        out.append({**it,"monthly":monthly})
    return {"currency":"USD","total_estimate": round(total,2), "items": out}

# === Endpoint ===
@app.post("/mcp/azure/diagram-tf")
def azure_mcp(payload: dict = Body(...), _=Depends(require_api_key)):
    app_name = payload.get("app_name", "3-tier web app")
    extra = payload.get("prompt", "")
    region = payload.get("region", DEFAULT_REGION)

    system = (
        "You are ArchGenie's Azure MCP. "
        "Return JSON ONLY with keys: "
        '{"diagram": "Mermaid code", "terraform": "Terraform HCL"}'
    )
    user = f"Create Azure architecture for {app_name}. Extra: {extra}. Region: {region}."

    try:
        result = aoai_chat([{"role":"system","content":system},{"role":"user","content":user}])
        content = result["choices"][0]["message"]["content"]
        parsed = extract_json_or_fences(content)

        # Use AOAI diagram if valid, else fallback
        diagram = sanitize_mermaid(parsed.get("diagram", ""))
        if not diagram.strip():
            diagram = "graph TD\nA[Internet] --> B[App Service]\nB --> C[Azure SQL]\n"

        # Use AOAI terraform if valid, else fallback
        tf = strip_fences(parsed.get("terraform", ""))
        if not tf.strip():
            tf = "resource \"azurerm_resource_group\" \"example\" {\n  name     = \"example-rg\"\n  location = \"eastus\"\n}\n"

    except Exception:
        if not FAIL_OPEN:
            raise
        diagram = "graph TD\nA[Internet] --> B[App Service]\nB --> C[Azure SQL]\n"
        tf = "resource \"azurerm_resource_group\" \"example\" {\n  name     = \"example-rg\"\n  location = \"eastus\"\n}\n"

    items = normalize_to_items(diagram, tf)
    cost = price_items(items)

    return {"diagram": diagram, "terraform": tf, "cost": cost}