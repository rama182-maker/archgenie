import os
import re
import json
import time
import random
import requests
from typing import List, Dict, Any, Tuple, Optional
from fastapi import FastAPI, Depends, Header, HTTPException, Body
from fastapi.middleware.cors import CORSMiddleware
from dotenv import load_dotenv

# ============== Config ==============
load_dotenv()

CAL_API_KEY = os.getenv("CAL_API_KEY", "super-secret-key")

AZURE_OPENAI_ENDPOINT    = (os.getenv("AZURE_OPENAI_ENDPOINT", "") or "").rstrip("/")
AZURE_OPENAI_API_KEY     = os.getenv("AZURE_OPENAI_API_KEY", "")
AZURE_OPENAI_DEPLOYMENT  = os.getenv("AZURE_OPENAI_DEPLOYMENT", "")
AZURE_OPENAI_API_VERSION = os.getenv("AZURE_OPENAI_API_VERSION", "2024-12-01-preview")
AZURE_OPENAI_FORCE_JSON  = os.getenv("AZURE_OPENAI_FORCE_JSON", "true").lower() == "true"

USE_LIVE_AZURE_PRICES = os.getenv("USE_LIVE_AZURE_PRICES", "true").lower() == "true"
HOURS_PER_MONTH = float(os.getenv("HOURS_PER_MONTH", "730"))
DEFAULT_REGION = os.getenv("DEFAULT_REGION", "eastus")
DEFAULT_APPGW_CAPACITY_UNITS = int(os.getenv("DEFAULT_APPGW_CAPACITY_UNITS", "1"))
DEFAULT_SQL_COMPUTE_ONLY     = os.getenv("DEFAULT_SQL_COMPUTE_ONLY", "true").lower() == "true"
DEFAULT_LB_RULES             = int(os.getenv("DEFAULT_LB_RULES", "2"))
DEFAULT_LB_DATA_GB           = float(os.getenv("DEFAULT_LB_DATA_GB", "100"))

FAIL_OPEN = os.getenv("FAIL_OPEN", "true").lower() == "true"

# ============== FastAPI ==============
def require_api_key(x_api_key: str = Header(None)):
    if not x_api_key or x_api_key != CAL_API_KEY:
        raise HTTPException(status_code=401, detail="Invalid or missing API key")

app = FastAPI(title="ArchGenie Azure-Only Backend (Ultra-Safe Mermaid)", version="8.1.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"], allow_credentials=True,
    allow_methods=["*"], allow_headers=["*"],
)

@app.get("/")
def health():
    return {"status": "ok", "message": "Azure-only backend alive"}

# ============== HTTP helpers ==============
def _sleep_backoff(attempt: int, base: float = 0.5, cap: float = 8.0):
    time.sleep(min(cap, base * (2 ** attempt)) * (0.5 + random.random() / 2.0))

def http_post_json(url: str, headers: Dict[str, str], body: dict, max_retries: int = 3, timeout: int = 60):
    last_err = None
    for attempt in range(max_retries + 1):
        try:
            resp = requests.post(url, headers=headers, data=json.dumps(body), timeout=timeout)
            if resp.status_code < 300:
                return resp
            if resp.status_code in (408, 409, 429, 500, 502, 503, 504):
                last_err = resp
                if attempt < max_retries:
                    _sleep_backoff(attempt)
                    continue
            return resp
        except Exception as e:
            last_err = e
            if attempt < max_retries:
                _sleep_backoff(attempt)
                continue
            raise
    if isinstance(last_err, requests.Response):
        return last_err
    raise HTTPException(status_code=502, detail=f"POST failed: {last_err}")

def http_get_json(url: str, params: Optional[dict] = None, max_retries: int = 3, timeout: int = 30):
    last_err = None
    for attempt in range(max_retries + 1):
        try:
            resp = requests.get(url, params=params, timeout=timeout)
            if resp.status_code < 300:
                return resp
            if resp.status_code in (408, 409, 429, 500, 502, 503, 504):
                last_err = resp
                if attempt < max_retries:
                    _sleep_backoff(attempt)
                    continue
            return resp
        except Exception as e:
            last_err = e
            if attempt < max_retries:
                _sleep_backoff(attempt)
                continue
            raise
    if isinstance(last_err, requests.Response):
        return last_err
    raise HTTPException(status_code=502, detail=f"GET failed: {last_err}")

# ============== AOAI ==============
def _aoai_configured() -> bool:
    return bool(AZURE_OPENAI_ENDPOINT and AZURE_OPENAI_API_KEY and AZURE_OPENAI_DEPLOYMENT)

def aoai_chat(messages: List[Dict[str, Any]], temperature: float = 0.2) -> Dict[str, Any]:
    if not _aoai_configured():
        raise HTTPException(status_code=500, detail="Azure OpenAI not configured")
    url = (
        f"{AZURE_OPENAI_ENDPOINT}/openai/deployments/"
        f"{AZURE_OPENAI_DEPLOYMENT}/chat/completions?api-version={AZURE_OPENAI_API_VERSION}"
    )
    headers = {"Content-Type": "application/json", "api-key": AZURE_OPENAI_API_KEY}
    body = {"messages": messages, "temperature": temperature}
    if AZURE_OPENAI_FORCE_JSON:
        body["response_format"] = {"type": "json_object"}
    resp = http_post_json(url, headers=headers, body=body, max_retries=3, timeout=60)
    if resp.status_code >= 300:
        raise HTTPException(status_code=resp.status_code, detail=resp.text)
    return resp.json()

# ============== Text helpers ==============
def strip_fences(text: str) -> str:
    if not text: return ""
    s = text.strip()
    for lang in ("mermaid", "hcl", "terraform", "json"):
        m = re.match(rf"^```{lang}\s*\n([\s\S]*?)```$", s, flags=re.IGNORECASE)
        if m: return m.group(1).strip()
    m = re.match(r"^```\s*\n?([\s\S]*?)```$", s)
    if m: return m.group(1).strip()
    return s

def extract_json_or_fences(content: str) -> Dict[str, Any]:
    if not content:
        return {"diagram": "", "terraform": ""}
    try:
        obj = json.loads(content)
        return {
            "diagram": strip_fences(obj.get("diagram", "")),
            "terraform": strip_fences(obj.get("terraform", "")),
        }
    except Exception:
        pass
    out = {"diagram": "", "terraform": ""}
    m = re.search(r"```mermaid\s*\n([\s\S]*?)```", content, flags=re.IGNORECASE)
    if m: out["diagram"] = m.group(1).strip()
    m = re.search(r"```(terraform|hcl)\s*\n([\s\S]*?)```", content, flags=re.IGNORECASE)
    if m: out["terraform"] = m.group(2).strip()
    return out

# ============== Normalize & Pricing ==============
_price_cache: Dict[str, Tuple[Any, float]] = {}

def cache_get(key: str):
    v = _price_cache.get(key)
    if not v: return None
    val, exp = v
    return val if exp > time.time() else None

def cache_put(key: str, value, ttl_sec: int = 3600):
    _price_cache[key] = (value, time.time() + ttl_sec)

_REGION_NAME_MAP = {
    "eastus": "US East",
    "eastus2": "US East 2",
    "centralus": "US Central",
    "westus": "US West",
    "westus2": "US West 2",
    "southcentralus": "US South Central",
    "northcentralus": "US North Central",
    "westeurope": "EU West",
    "northeurope": "EU North",
}
def region_variants(region: str) -> List[str]:
    if not region: return []
    r = region.strip()
    variants = set([r, r.lower()])
    mapped = _REGION_NAME_MAP.get(r.lower())
    if mapped:
        variants.add(mapped)
    r_sp = r.replace("-", " ")
    variants.add(r_sp)
    variants.add(r_sp.title())
    if r.lower().endswith("us") and len(r) > 2:
        variants.add(r[:-2].title() + " US")
    return list(variants)

def azure_retail_prices_fetch(filter_str: str, limit: int = 100) -> list:
    base = "https://prices.azure.com/api/retail/prices"
    params = {"api-version": "2023-01-01-preview", "$filter": filter_str}
    out = []
    url = base
    tries = 0
    while True:
        tries += 1
        r = http_get_json(url, params=params if url == base else None, max_retries=2, timeout=30)
        if r.status_code >= 300:
            return []
        j = r.json()
        items = j.get("Items") or []
        out.extend(items)
        if len(out) >= limit:
            return out[:limit]
        next_link = j.get("NextPageLink")
        if not next_link or tries > 20:
            break
        url = next_link
        params = None
    return out

def monthly_from_retail(item: Dict[str, Any]) -> float:
    price = float(item.get("retailPrice") or 0.0)
    uom = (item.get("unitOfMeasure") or "").lower()
    if "hour" in uom:
        return round(price * HOURS_PER_MONTH, 2)
    return round(price, 2)

def price_items(items: List[dict]) -> dict:
    currency = "USD"
    notes: List[str] = []
    total = 0.0
    out_items = []

    def azure_price_for_app_service_sku(sku: str, region: str) -> Optional[float]:
        key = f"az.appservice.{region}.{sku}"
        c = cache_get(key)
        if c is not None:
            return c
        service_candidates = ["App Service","App Service Linux","Azure App Service","App Service Plans","Azure App Service Plans"]
        best_price = None
        for reg in region_variants(region):
            for svc in service_candidates:
                flt = f"serviceName eq '{svc}' and skuName eq '{sku}' and armRegionName eq '{reg}' and retailPrice ne 0"
                items = azure_retail_prices_fetch(flt, limit=60) or []
                if not items:
                    alt = f"contains(productName, 'App Service') and skuName eq '{sku}' and armRegionName eq '{reg}' and retailPrice ne 0"
                    items = azure_retail_prices_fetch(alt, limit=60) or []
                hourly = [x for x in items if 'hour' in (x.get('unitOfMeasure','').lower())]
                pool = hourly or items
                for it in pool:
                    m = monthly_from_retail(it)
                    if best_price is None or (m and m < best_price):
                        best_price = m
        if best_price is not None:
            cache_put(key, best_price)
        return best_price

    def azure_price_for_sql(sku: str, region: str) -> Optional[float]:
        key = f"az.sql.{region}.{sku}"
        c = cache_get(key)
        if c is not None:
            return c
        best_price = None
        bad_words = ["backup", "storage", "io", "data processed", "per gb", "gb-month"]
        good_words = ["dtu", "vcore", "compute"]
        def is_compute_meter(it):
            meter = (it.get("meterName") or "").lower()
            if any(b in meter for b in bad_words):
                return False
            return any(g in meter for g in good_words) or sku.lower() in meter
        for reg in region_variants(region):
            flt1 = f"serviceName eq 'SQL Database' and skuName eq '{sku}' and armRegionName eq '{reg}' and retailPrice ne 0"
            items = azure_retail_prices_fetch(flt1, limit=200) or []
            if not items:
                flt2 = f"contains(productName, 'SQL Database') and skuName eq '{sku}' and armRegionName eq '{reg}' and retailPrice ne 0"
                items = azure_retail_prices_fetch(flt2, limit=200) or []
            if not items:
                flt3 = f"serviceName eq 'SQL Database' and contains(meterName, '{sku}') and armRegionName eq '{reg}' and retailPrice ne 0"
                items = azure_retail_prices_fetch(flt3, limit=200) or []
            hourly = [x for x in items if 'hour' in (x.get('unitOfMeasure','').lower())]
            pool = hourly or items
            pool = [x for x in pool if is_compute_meter(x)] if DEFAULT_SQL_COMPUTE_ONLY else pool
            for it in pool:
                m = monthly_from_retail(it)
                if best_price is None or (m and m < best_price):
                    best_price = m
        if best_price is not None:
            cache_put(key, best_price)
        return best_price

    def azure_price_for_storage_lrs_per_gb(region: str) -> Optional[float]:
        key = f"az.storage.lrs.{region}"
        c = cache_get(key)
        if c is not None:
            return c
        best = None
        for reg in region_variants(region):
            flt = f"serviceName eq 'Storage' and armRegionName eq '{reg}' and contains(skuName, 'LRS') and retailPrice ne 0"
            items = azure_retail_prices_fetch(flt, limit=80) or []
            for it in items:
                m = monthly_from_retail(it)  # per GB-month
                if best is None or (m and m < best):
                    best = m
        if best is not None:
            cache_put(key, best)
        return best

    def azure_price_for_appgw_wafv2_components(region: str) -> Optional[Dict[str, float]]:
        key = f"az.appgw.wafv2.components.{region}"
        cached = cache_get(key)
        if cached:
            return cached
        best_base = None
        best_cu = None
        def is_base(it):
            meter = (it.get("meterName") or "").lower()
            uom = (it.get("unitOfMeasure") or "").lower()
            if "gb" in uom:
                return False
            return ("gateway" in meter or "waf v2" in meter or "app gateway" in meter) and "capacity" not in meter
        def is_cu(it):
            meter = (it.get("meterName") or "").lower()
            uom = (it.get("unitOfMeasure") or "").lower()
            return "capacity unit" in meter and "hour" in uom
        for reg in region_variants(region):
            flt = f"serviceName eq 'Application Gateway' and armRegionName eq '{reg}' and retailPrice ne 0"
            items = azure_retail_prices_fetch(flt, limit=200) or []
            hourly = [x for x in items if 'hour' in (x.get('unitOfMeasure','').lower())]
            for it in hourly:
                m = monthly_from_retail(it)
                if is_base(it):
                    if best_base is None or (m and m < best_base):
                        best_base = m
                elif is_cu(it):
                    if best_cu is None or (m and m < best_cu):
                        best_cu = m
        if best_base is None and best_cu is None:
            return None
        components = {"base_monthly": round(best_base or 0.0, 2), "capacity_unit_monthly": round(best_cu or 0.0, 2)}
        cache_put(key, components, ttl_sec=3600)
        return components

    for it in items:
        cloud   = it.get("cloud","").lower()
        service = it.get("service","").lower()
        sku     = it.get("sku","")
        qty     = int(it.get("qty", 1) or 1)
        region  = it.get("region") or DEFAULT_REGION
        size_gb = float(it.get("size_gb", 0) or 0)
        hours   = float(it.get("hours", HOURS_PER_MONTH) or HOURS_PER_MONTH)
        unit_monthly: Optional[float] = None
        if cloud == "azure" and USE_LIVE_AZURE_PRICES:
            try:
                if service == "app_service":
                    unit_monthly = azure_price_for_app_service_sku(sku, region)
                elif service == "azure_sql":
                    unit_monthly = azure_price_for_sql(sku, region)
                elif service == "storage":
                    per_gb = azure_price_for_storage_lrs_per_gb(region)
                    if per_gb is not None:
                        unit_monthly = per_gb * (size_gb if size_gb > 0 else 100.0)
                elif service == "app_gateway":
                    comps = azure_price_for_appgw_wafv2_components(region)
                    if comps:
                        cu = int(it.get("capacity_units") or it.get("size_gb") or DEFAULT_APPGW_CAPACITY_UNITS)
                        unit_monthly = comps["base_monthly"] + cu * comps["capacity_unit_monthly"]
                elif service == "vm":
                    unit_monthly = 0.0  # optional: add VM pricing
                elif service == "lb":
                    unit_monthly = 0.0  # optional: add LB pricing
                elif service == "aks":
                    notes.append("AKS control plane free; worker node VM costs not included.")
                    unit_monthly = 0.0
            except Exception as e:
                notes.append(f"Lookup failed for {cloud}:{service}:{sku} in {region}: {e}")
                unit_monthly = None
        if unit_monthly is None:
            unit_monthly = 0.0
        monthly = float(unit_monthly)
        if hours and hours != HOURS_PER_MONTH and monthly > 0:
            monthly = monthly * (hours / HOURS_PER_MONTH)
        monthly = round(monthly * qty, 2)
        total += monthly
        out_line = {
            "cloud": cloud, "service": service, "sku": sku,
            "qty": qty, "region": region,
            "size_gb": size_gb if size_gb > 0 else None,
            "hours": hours if hours and hours != HOURS_PER_MONTH else None,
            "unit_monthly": round(unit_monthly, 2),
            "monthly": monthly
        }
        out_items.append(out_line)

    return {"currency": currency, "total_estimate": round(total, 2), "items": out_items, "notes": notes}

def normalize_to_items(ask: str = "", diagram: str = "", tf: str = "", region: Optional[str] = None) -> List[dict]:
    region = region or DEFAULT_REGION
    items: List[dict] = []
    blob = f"{ask}\n{diagram}\n{tf}".lower()
    def add(cloud, service, sku, qty=1, size_gb=None):
        d = {"cloud": cloud, "service": service, "sku": sku, "qty": max(1, int(qty)), "region": region}
        if size_gb is not None: d["size_gb"] = float(size_gb)
        items.append(d)
    if re.search(r"\bapp service\b|\bweb app\b", blob): add("azure", "app_service", "S1", qty=2)
    if re.search(r"\b(mssql|azure sql|sql database)\b", blob): add("azure", "azure_sql", "S0", qty=1)
    if re.search(r"\bapplication gateway\b|\bapp gateway\b|\bapp gw\b", blob): add("azure", "app_gateway", "WAF_v2", qty=1)
    if "aks" in blob: add("azure", "aks", "standard", qty=1)
    return items

# ============== Ultra-safe diagram builder ==============
def build_mermaid_from_items(items: List[dict]) -> str:
    # Deterministic minimal 3-tier with optional extras
    have_agw  = any(i["service"] == "app_gateway" for i in items)
    have_sql  = any(i["service"] == "azure_sql" for i in items)
    have_aks  = any(i["service"] == "aks" for i in items)

    lines = ["graph TD"]
    lines.append("IN[Internet] --> FD[Azure Front Door]")
    if have_agw:
        lines.append("FD --> AGW[Azure Application Gateway (WAF v2)]")
        lines.append("AGW --> WEB[Web Tier - Azure App Service]")
    else:
        lines.append("FD --> WEB[Web Tier - Azure App Service]")
    lines.append("WEB --> APP[Application Tier - Azure App Service]")

    if have_aks:
        lines.append("APP --> AKS[Azure Kubernetes Service]")

    if have_sql:
        lines.append("APP --> SQL[Azure SQL Database]")

    diagram = "\n".join(lines) + "\n"
    return diagram

# ============== Public Endpoint (Azure only) ==============
@app.post("/mcp/azure/diagram-tf")
def azure_mcp(payload: dict = Body(...), _=Depends(require_api_key)):
    app_name = payload.get("app_name", "3-tier web app")
    extra = payload.get("prompt") or ""
    region = payload.get("region") or DEFAULT_REGION
    system = (
        "You are ArchGenie's Azure MCP.\n"
        "Return ONLY a single JSON object with keys:\n"
        '{\n'
        '  "diagram": "Mermaid code (ignored by server, but keep it short)",\n'
        '  "terraform": "Valid Terraform HCL for Azure (resource group, app service plan, web apps, sql, etc.)"\n'
        '}\n'
        "Do not write explanations, backticks, or any other keys. JSON only."
    )
    user = (
        f"Create an Azure architecture for: {app_name}.\n"
        f"Extra requirements: {extra}\n"
        f"Region: {region}\n"
        "Output JSON only."
    )
    tf = ""
    try:
        result = aoai_chat([{"role": "system", "content": system}, {"role": "user", "content": user}], temperature=0.2)
        content = result["choices"][0]["message"]["content"]
        parsed = extract_json_or_fences(content)
        tf_raw      = (parsed.get("terraform") or "").strip()
        if not tf_raw:
            raise ValueError("Model missing terraform")
        tf = strip_fences(tf_raw)
    except Exception as e:
        if not FAIL_OPEN:
            raise
        tf = "# Terraform generation failed-open; check backend logs"

    # Build diagram deterministically so Mermaid v10 always parses
    items = normalize_to_items(ask=extra or app_name, diagram="", tf=tf, region=region)
    diagram = build_mermaid_from_items(items)

    # Cost (best effort)
    cost = price_items(items)

    return {"diagram": diagram, "terraform": tf, "cost": cost}
