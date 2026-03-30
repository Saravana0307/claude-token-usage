#!/usr/bin/env python3
"""
Fetch Claude model pricing from Anthropic's pricing page and cache to ~/.ctu/pricing.json.
Falls back to ~/.ctu/pricing_defaults.json if fetch fails.
Run automatically (background) when pricing cache is stale, or manually: python3 fetch-pricing.py --force
"""

import json, re, sys, urllib.request, urllib.error
from pathlib import Path
from datetime import datetime, timezone

CTU_DIR      = Path.home() / ".ctu"
PRICING_FILE = CTU_DIR / "pricing.json"
DEFAULTS     = CTU_DIR / "pricing_defaults.json"
# Anthropic's pricing page uses JS rendering — try multiple sources
PRICING_URLS = [
    # OpenRouter exposes Anthropic pricing via JSON API (reliable, no JS rendering)
    "https://openrouter.ai/api/v1/models",
    # Anthropic pricing page (may work if static HTML)
    "https://www.anthropic.com/pricing",
]
STALE_DAYS   = 7
FORCE        = "--force" in sys.argv


def load_defaults():
    if DEFAULTS.exists():
        return json.loads(DEFAULTS.read_text()).get("models", {})
    return {}


def is_stale():
    if not PRICING_FILE.exists():
        return True
    try:
        data = json.loads(PRICING_FILE.read_text())
        fetched = data.get("fetched_at")
        if not fetched:
            return True
        age = (datetime.now(timezone.utc) - datetime.fromisoformat(fetched)).days
        return age >= STALE_DAYS
    except Exception:
        return True


def fetch_openrouter():
    """Fetch Anthropic model pricing from OpenRouter's public model list."""
    req = urllib.request.Request(
        PRICING_URLS[0],
        headers={"User-Agent": "claude-token-usage/1.0", "HTTP-Referer": "https://github.com/ctu"}
    )
    with urllib.request.urlopen(req, timeout=10) as resp:
        payload = json.loads(resp.read().decode("utf-8"))

    models = {}
    for item in payload.get("data", []):
        model_id = item.get("id", "")
        if not model_id.startswith("anthropic/claude"):
            continue
        pricing = item.get("pricing", {})
        # OpenRouter pricing is per-token (not per million), convert
        try:
            inp = float(pricing.get("prompt",     0)) * 1_000_000
            out = float(pricing.get("completion", 0)) * 1_000_000
            # OpenRouter may not break out cache — derive from typical ratios
            cr  = round(inp * 0.1,  4)
            cw  = round(inp * 1.25, 4)
            if inp > 0 and out > 0:
                # Strip "anthropic/" prefix for our key
                key = model_id.replace("anthropic/", "").lower()
                models[key] = [inp, out, cr, cw]
        except Exception:
            pass
    return models


def fetch_and_parse():
    """Try OpenRouter first, fall back to Anthropic pricing page HTML."""
    # Strategy 1: OpenRouter JSON API (most reliable)
    try:
        models = fetch_openrouter()
        if len(models) >= 3:
            return models
    except Exception as e:
        print(f"OpenRouter fetch failed: {e}", file=sys.stderr)

    # Strategy 2: Anthropic pricing page (JS-rendered, may fail)
    models = {}
    try:
        req = urllib.request.Request(
            PRICING_URLS[1],
            headers={"User-Agent": "claude-token-usage/1.0"}
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            html = resp.read().decode("utf-8")

        # Try JSON-LD / Next.js data
        for blob in re.findall(r'<script[^>]*type=["\']application/json["\'][^>]*>(.*?)</script>', html, re.DOTALL):
            try:
                _extract_from_json(json.loads(blob), models)
            except Exception:
                pass

        if not models:
            next_data = re.search(r'__NEXT_DATA__\s*=\s*(\{.*?\})\s*</script>', html, re.DOTALL)
            if next_data:
                try:
                    _extract_from_json(json.loads(next_data.group(1)), models)
                except Exception:
                    pass
    except Exception as e:
        print(f"Anthropic page fetch failed: {e}", file=sys.stderr)

    return models


def _extract_from_json(obj, models, depth=0):
    """Recursively search a parsed JSON object for model pricing data."""
    if depth > 8:
        return
    if isinstance(obj, dict):
        # Look for model + price keys
        model = obj.get("model") or obj.get("id") or obj.get("name", "")
        if isinstance(model, str) and model.startswith("claude-"):
            inp = obj.get("input") or obj.get("inputPrice") or obj.get("input_price")
            out = obj.get("output") or obj.get("outputPrice") or obj.get("output_price")
            if inp and out:
                try:
                    cr = float(obj.get("cacheRead") or obj.get("cache_read") or float(inp) * 0.1)
                    cw = float(obj.get("cacheWrite") or obj.get("cache_write") or float(inp) * 1.25)
                    models[model.lower()] = [float(inp), float(out), cr, cw]
                except Exception:
                    pass
        for v in obj.values():
            _extract_from_json(v, models, depth + 1)
    elif isinstance(obj, list):
        for item in obj:
            _extract_from_json(item, models, depth + 1)


def _assign_prices(model, prices, models):
    if len(prices) >= 2:
        inp, out = prices[0], prices[1]
        cr  = prices[2] if len(prices) > 2 else round(inp * 0.1, 4)
        cw  = prices[3] if len(prices) > 3 else round(inp * 1.25, 4)
        models[model] = [inp, out, cr, cw]


def save(models):
    CTU_DIR.mkdir(parents=True, exist_ok=True)
    data = {
        "fetched_at": datetime.now(timezone.utc).isoformat(),
        "source": PRICING_URLS[0],
        "models": models
    }
    PRICING_FILE.write_text(json.dumps(data, indent=2))


def main():
    if not FORCE and not is_stale():
        sys.exit(0)  # Not stale, nothing to do

    defaults = load_defaults()

    try:
        models = fetch_and_parse()
        if len(models) < 3:
            # Too few models found — scraping likely failed, use defaults
            raise ValueError(f"Only {len(models)} models found, likely parse failure")
        # Merge: fetched takes priority, fill gaps from defaults
        merged = {**defaults, **models}
        save(merged)
        print(f"Pricing updated: {len(merged)} models", file=sys.stderr)
    except Exception as e:
        print(f"Pricing fetch failed ({e}), using defaults", file=sys.stderr)
        # Save defaults with null fetched_at so we retry next time
        if defaults:
            save(defaults)


if __name__ == "__main__":
    main()
