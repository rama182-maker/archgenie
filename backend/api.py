import os, re, json, time, random, requests
from typing import List, Dict, Any, Optional
from fastapi import FastAPI, Depends, Header, HTTPException, Body
from fastapi.middleware.cors import CORSMiddleware

CAL_API_KEY = os.getenv("CAL_API_KEY", "super-secret-key")

AZURE_OPENAI_ENDPOINT    = (os.getenv("AZURE_OPENAI_ENDPOINT", "") or "").rstrip("/")
AZURE_OPENAI_API_KEY     = os.getenv("AZURE_OPENAI_API_KEY", "")
AZURE_OPENAI_DEPLOYMENT  = os.getenv("AZURE_OPENAI_DEPLOYMENT", "")
AZURE_OPENAI_API_VERSION = os.getenv("AZURE_OPENAI_API_VERSION", "2024-12-01-preview")

HOURS_PER_MONTH = 730
DEFAULT_REGION = "eastus"

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

def sanitize_mermaid(src: str) -> str:
    if not src: return "graph TD\nA[Frontend] --> B[Backend]\nB --> C[Database]\n"
    s = src.strip()
    header_re = re.compile(r'^(graph|flowchart)\s+(TD|LR)\b', flags=re.IGNORECASE|re.MULTILINE)
    if header_re.search(s):
        s = header_re.sub("graph TD", s, count=1)
    else:
        s = "graph TD\n" + s
    lines = []
    for line in s.splitlines():
        just = line.strip()
        if just.endswith(";"):
            just = just[:-1]
        lines.append(just)
    s = "\n".join(lines)
    s = re.sub(r'\[([^\]]+)\]', lambda m: "[" + m.group(1).replace("\n", " ").replace(",", "") + "]", s)
    return s + "\n"

@app.post("/mcp/azure/diagram-tf")
def azure_mcp(payload: dict = Body(...), _=Depends(require_api_key)):
    app_name = payload.get("app_name","Azure app")
    extra = payload.get("prompt","")
    region = payload.get("region", DEFAULT_REGION)
    # Normally call AOAI here; stubbed for demo
    diagram_raw = f"""graph TD
    A[Internet] --> B[Azure Front Door]
    B --> C[Azure App Gateway]
    C --> D[Web App Service]
    D --> E[App Service Backend]
    E --> F[Azure SQL Database]
    """
    terraform = f"""resource "azurerm_resource_group" "example" {{
  name = "example-rg"
  location = "{region}"
}}
"""
    cost = { "currency":"USD", "total_estimate": 123.45,
             "items":[{"cloud":"azure","service":"app_service","sku":"S1","qty":2,"monthly":75.0}]}
    return {"diagram": sanitize_mermaid(diagram_raw), "terraform": terraform, "cost": cost}
