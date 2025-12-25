"""
FastAPI server for Retail Flipper web UI.
Exposes endpoints to run reports and view results.
"""
import os
import subprocess
import csv
import json
from datetime import datetime
from pathlib import Path
from typing import Optional, Dict, List, Any
from fastapi import FastAPI, HTTPException, UploadFile, File
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse, JSONResponse, Response
from pydantic import BaseModel

app = FastAPI(title="Retail Flipper API")

# Mount static files for web frontend
web_dir = Path(__file__).parent / "web"
if web_dir.exists():
    app.mount("/web", StaticFiles(directory=str(web_dir)), name="web")
    # Serve index.html at root
    @app.get("/")
    async def read_root():
        index_path = web_dir / "index.html"
        if index_path.exists():
            return FileResponse(str(index_path))
        return {"message": "Web UI not found"}


class RunReportRequest(BaseModel):
    mode: str = "highticket"
    category: str = "Tools,Electronics"
    limit: int = 120
    brands: Optional[str] = None
    shipping_flat: float = 14.99
    outdir: str = "data/reports"


@app.get("/api/health")
async def health():
    """Health check endpoint."""
    return {"status": "ok"}


@app.post("/api/run-report")
async def run_report(request: RunReportRequest):
    """
    Run the report command via subprocess and return results.
    """
    from datetime import datetime
    
    # Generate run_id
    run_id = datetime.now().strftime('%Y-%m-%d_%H%M%S')
    date_stamp = datetime.now().strftime('%Y-%m-%d')
    
    # Build command
    cmd = [
        "python", "run.py", "report",
        "--mode", request.mode,
        "--category", request.category,
        "--limit", str(request.limit),
        "--shipping-flat", str(request.shipping_flat),
        "--outdir", request.outdir,
        "--allow-empty"
    ]
    
    if request.brands:
        cmd.extend(["--brands", request.brands])
    
    # Run subprocess
    try:
        process = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
            universal_newlines=True
        )
        
        # Collect output
        stdout_lines = []
        for line in process.stdout:
            stdout_lines.append(line)
            # Keep only last 100 lines to avoid huge responses
            if len(stdout_lines) > 100:
                stdout_lines.pop(0)
        
        process.wait()
        return_code = process.returncode
        
        stdout_tail = "".join(stdout_lines)
        
        if return_code != 0:
            return JSONResponse(
                status_code=400,
                content={
                    "ok": False,
                    "error": f"Process exited with code {return_code}",
                    "stdout_tail": stdout_tail,
                    "run_id": run_id
                }
            )
        
        # Determine output files
        files = {
            "passed": os.path.join(request.outdir, f"passed-{date_stamp}.csv"),
            "nearmiss": os.path.join(request.outdir, f"nearmiss-{date_stamp}.csv"),
            "all": os.path.join(request.outdir, f"all-{date_stamp}.csv")
        }
        
        return {
            "ok": True,
            "run_id": run_id,
            "stdout_tail": stdout_tail,
            "files": files
        }
        
    except Exception as e:
        return JSONResponse(
            status_code=400,
            content={
                "ok": False,
                "error": str(e),
                "stdout_tail": "",
                "run_id": run_id
            }
        )


@app.get("/api/upload-template")
async def upload_template():
    """
    Return a CSV template file for deal uploads.
    """
    template = "title,price,url,store,category,sku,image_url\n"
    template += "Milwaukee M18 Fuel Drill Kit,129.99,https://example.com/drill,Home Depot,Power Tools,12345,https://example.com/image.jpg\n"
    template += "Dewalt 20V Circular Saw,89.99,https://example.com/saw,Lowes,Power Tools,67890,\n"
    
    return Response(
        content=template,
        media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=deal-upload-template.csv"}
    )


@app.post("/api/upload-deals")
async def upload_deals(file: UploadFile = File(...)):
    """
    Upload and parse CSV file, returning normalized items.
    Ingestion + validation only (no eBay analysis).
    """
    if not file:
        return JSONResponse(
            status_code=400,
            content={
                "ok": False,
                "message": "No file uploaded"
            }
        )
    
    max_rows = 200
    
    # Counters
    rows_read = 0
    accepted = 0
    skipped = 0
    skipped_missing_title = 0
    skipped_missing_price = 0
    skipped_bad_price = 0
    capped = False
    
    items = []
    
    try:
        # Read file content
        contents = await file.read()
        text = contents.decode('utf-8')
        
        # Parse CSV
        reader = csv.DictReader(text.splitlines())
        
        for row in reader:
            rows_read += 1
            
            # Normalize keys: strip whitespace, remove BOM, lowercase
            def norm_key(k):
                return (k or "").strip().lstrip("\ufeff").lower()
            
            # Normalize keys and values
            clean = {norm_key(k): (v.strip() if isinstance(v, str) else v) for k, v in row.items() if k is not None}
            
            # Extract title (required)
            title = None
            for title_key in ['title', 'name', 'item', 'product', 'item_name']:
                if title_key in clean and clean[title_key]:
                    title = str(clean[title_key]).strip()
                    break
            
            if not title:
                skipped += 1
                skipped_missing_title += 1
                continue
            
            # Extract price (required)
            price = None
            price_field_exists = False
            for price_key in ['price', 'cost', 'buy_price', 'sale_price', 'purchase_price', 'woot_price']:
                if price_key in clean:
                    price_field_exists = True
                    price_str = str(clean[price_key]).strip() if clean[price_key] else ""
                    if price_str:
                        try:
                            # Remove $ and commas, convert to float
                            price = float(price_str.replace('$', '').replace(',', '').strip())
                            if price > 0:
                                break
                        except (ValueError, TypeError):
                            pass
            
            if not price_field_exists:
                skipped += 1
                skipped_missing_price += 1
                continue
            
            if price is None or price <= 0:
                skipped += 1
                skipped_bad_price += 1
                continue
            
            # Check if we've hit the limit
            if accepted >= max_rows:
                capped = True
                break
            
            # Extract optional fields
            url = None
            for url_key in ['url', 'link', 'source_url', 'woot_url', 'product_url']:
                if url_key in clean and clean[url_key]:
                    url = str(clean[url_key]).strip()
                    break
            
            store = None
            for store_key in ['store', 'merchant', 'seller', 'retailer']:
                if store_key in clean and clean[store_key]:
                    store = str(clean[store_key]).strip()
                    break
            
            category = None
            for cat_key in ['category', 'categories', 'cat']:
                if cat_key in clean and clean[cat_key]:
                    category = str(clean[cat_key]).strip()
                    break
            
            sku = None
            if 'sku' in clean and clean['sku']:
                sku = str(clean['sku']).strip()
            
            image_url = None
            for img_key in ['image_url', 'image', 'imageurl', 'img_url', 'picture_url']:
                if img_key in clean and clean[img_key]:
                    image_url = str(clean[img_key]).strip()
                    break
            
            # Create normalized item
            item = {
                "title": title,
                "price": float(price),
                "url": url,
                "store": store,
                "category": category,
                "sku": sku,
                "image_url": image_url
            }
            
            items.append(item)
            accepted += 1
        
        return {
            "ok": True,
            "summary": {
                "rows_read": rows_read,
                "accepted": accepted,
                "skipped": skipped,
                "skipped_missing_title": skipped_missing_title,
                "skipped_missing_price": skipped_missing_price,
                "skipped_bad_price": skipped_bad_price,
                "capped": capped
            },
            "items": items
        }
        
    except csv.Error as e:
        return JSONResponse(
            status_code=400,
            content={
                "ok": False,
                "message": f"CSV parse error: {str(e)}"
            }
        )
    except Exception as e:
        return JSONResponse(
            status_code=400,
            content={
                "ok": False,
                "message": str(e)
            }
        )


@app.post("/api/analyze-upload")
async def analyze_upload(file: UploadFile = File(...), shipping_flat: float = 14.99):
    """
    Upload CSV file, save it, and run analysis via subprocess.
    """
    from datetime import datetime
    
    # Generate run_id
    run_id = datetime.now().strftime('%Y-%m-%d_%H%M%S')
    date_stamp = datetime.now().strftime('%Y-%m-%d')
    
    # Create uploads directory
    uploads_dir = Path("data/uploads")
    uploads_dir.mkdir(parents=True, exist_ok=True)
    
    # Save uploaded file
    infile = uploads_dir / f"{run_id}.csv"
    try:
        contents = await file.read()
        with open(infile, 'wb') as f:
            f.write(contents)
    except Exception as e:
        return JSONResponse(
            status_code=400,
            content={
                "ok": False,
                "error": f"Failed to save uploaded file: {str(e)}",
                "run_id": run_id
            }
        )
    
    # Build command
    cmd = [
        "python", "run.py", "upload",
        "--infile", str(infile),
        "--mode", "highticket",
        "--shipping-flat", str(shipping_flat),
        "--outdir", "data/reports",
        "--allow-empty"
    ]
    
    # Run subprocess
    try:
        process = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
            universal_newlines=True
        )
        
        # Collect output (keep last ~200 lines)
        stdout_lines = []
        for line in process.stdout:
            stdout_lines.append(line)
            if len(stdout_lines) > 200:
                stdout_lines.pop(0)
        
        process.wait()
        return_code = process.returncode
        
        stdout_tail = "".join(stdout_lines)
        
        if return_code != 0:
            return JSONResponse(
                status_code=400,
                content={
                    "ok": False,
                    "error": f"Process exited with code {return_code}",
                    "stdout_tail": stdout_tail,
                    "run_id": run_id
                }
            )
        
        # Determine output files
        files = {
            "passed": os.path.join("data/reports", f"passed-{date_stamp}.csv"),
            "nearmiss": os.path.join("data/reports", f"nearmiss-{date_stamp}.csv"),
            "all": os.path.join("data/reports", f"all-{date_stamp}.csv")
        }
        
        return {
            "ok": True,
            "run_id": run_id,
            "stdout_tail": stdout_tail,
            "files": files
        }
        
    except Exception as e:
        return JSONResponse(
            status_code=400,
            content={
                "ok": False,
                "error": str(e),
                "stdout_tail": "",
                "run_id": run_id
            }
        )


@app.get("/api/latest")
async def get_latest(type: str = "passed"):
    """
    Get the latest CSV file for the specified type (passed/nearmiss/all).
    Returns parsed CSV as JSON.
    """
    if type not in ["passed", "nearmiss", "all"]:
        raise HTTPException(status_code=400, detail="type must be one of: passed, nearmiss, all")
    
    # Find the newest matching CSV file
    reports_dir = Path("data/reports")
    if not reports_dir.exists():
        raise HTTPException(status_code=404, detail="Reports directory not found")
    
    pattern = f"{type}-*.csv"
    matching_files = list(reports_dir.glob(pattern))
    
    if not matching_files:
        return {
            "file": None,
            "items": []
        }
    
    # Get the newest file
    latest_file = max(matching_files, key=lambda p: p.stat().st_mtime)
    
    # Parse CSV
    items = []
    try:
        with open(latest_file, 'r', encoding='utf-8') as f:
            reader = csv.DictReader(f)
            for row in reader:
                items.append(row)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error reading CSV: {str(e)}")
    
    return {
        "file": str(latest_file),
        "items": items
    }


@app.post("/api/analyze-upload")
async def analyze_upload(file: UploadFile = File(...)):
    """
    Analyze uploaded CSV file through eBay arbitrage engine.
    """
    from datetime import datetime
    import sys
    
    # Default shipping flat
    shipping_flat = 14.99
    
    # Import analysis functions from run.py
    sys.path.insert(0, str(Path(__file__).parent))
    try:
        from run import (
            search_ebay_sold_browse,
            calculate_metrics,
            evaluate_deal,
            build_query_confidence,
            is_non_flippable,
            EBAY_FEE_PCT,
            PAYMENT_FEE_PCT,
            MIN_PROFIT,
            MIN_ROI,
            MIN_SOLD_COUNT
        )
    except ImportError as e:
        return JSONResponse(
            status_code=500,
            content={
                "ok": False,
                "error": f"Failed to import analysis functions: {str(e)}"
            }
        )
    
    # Mode settings (highticket)
    mode = 'highticket'
    scan_min_net_profit = 12.0
    scan_min_net_roi = 0.10
    scan_min_sold_comps = 6
    
    run_id = datetime.now().strftime('%Y-%m-%d_%H%M%S')
    
    try:
        # Read and parse CSV
        contents = await file.read()
        text = contents.decode('utf-8')
        reader = csv.DictReader(text.splitlines())
        
        # Normalize column names (case-insensitive)
        normalized_deals = []
        skipped = 0
        
        for row in reader:
            # Normalize keys to lowercase for matching
            row_lower = {k.lower().strip(): v for k, v in row.items()}
            
            # Extract required fields
            title = None
            price = None
            
            # Try various column name variations
            for title_key in ['title', 'name', 'item', 'product', 'item_name']:
                if title_key in row_lower and row_lower[title_key]:
                    title = str(row_lower[title_key]).strip()
                    break
            
            for price_key in ['price', 'cost', 'buy_price', 'sale_price', 'purchase_price', 'woot_price']:
                if price_key in row_lower and row_lower[price_key]:
                    try:
                        price = float(str(row_lower[price_key]).replace('$', '').replace(',', '').strip())
                        break
                    except (ValueError, TypeError):
                        continue
            
            # Skip if required fields missing
            if not title or not price or price <= 0:
                skipped += 1
                continue
            
            # Extract optional fields
            url = None
            for url_key in ['url', 'link', 'source_url', 'woot_url', 'product_url']:
                if url_key in row_lower and row_lower[url_key]:
                    url = str(row_lower[url_key]).strip()
                    break
            
            store = None
            for store_key in ['store', 'merchant', 'seller', 'retailer']:
                if store_key in row_lower and row_lower[store_key]:
                    store = str(row_lower[store_key]).strip()
                    break
            
            category = None
            for cat_key in ['category', 'categories', 'cat']:
                if cat_key in row_lower and row_lower[cat_key]:
                    category = str(row_lower[cat_key]).strip()
                    break
            
            normalized_deals.append({
                'title': title,
                'buy_price': price,
                'url': url,
                'store': store,
                'category': category
            })
        
        # Cap to 200 rows
        if len(normalized_deals) > 200:
            normalized_deals = normalized_deals[:200]
        
        # Analyze each deal
        passed_items = []
        nearmiss_items = []
        all_items = []
        
        for deal in normalized_deals:
            title = deal['title']
            buy_price = deal['buy_price']
            url = deal.get('url')
            category = deal.get('category')
            
            # Apply basic filters
            if is_non_flippable(title, None, category):
                result = {
                    'title': title,
                    'woot_price': buy_price,
                    'expected_sale': None,
                    'net_profit': None,
                    'net_roi': None,
                    'comps': 0,
                    'status': 'skipped',
                    'reason': 'SKIP_NONFLIPPABLE',
                    'woot_url': url,
                    'category': category
                }
                all_items.append(result)
                continue
            
            if buy_price < 20.00:
                result = {
                    'title': title,
                    'woot_price': buy_price,
                    'expected_sale': None,
                    'net_profit': None,
                    'net_roi': None,
                    'comps': 0,
                    'status': 'skipped',
                    'reason': 'SKIP_LOW_ASP',
                    'woot_url': url,
                    'category': category
                }
                all_items.append(result)
                continue
            
            # Build query and search eBay
            confidence_info = build_query_confidence(title)
            normalized_query = confidence_info['query']
            
            ebay_result = search_ebay_sold_browse(normalized_query, no_cache=False)
            
            sold_count = ebay_result.get('sold_count', 0)
            avg_price = ebay_result.get('avg_price', 0.0)
            median_price = ebay_result.get('median_price', 0.0)
            trimmed_count = ebay_result.get('trimmed_count', sold_count)
            expected_sale = median_price if median_price > 0 else avg_price
            
            # Calculate metrics
            if expected_sale > 0 and trimmed_count >= scan_min_sold_comps:
                metrics = calculate_metrics(
                    buy_price=buy_price,
                    expected_sale_price=expected_sale,
                    trimmed_count=trimmed_count,
                    min_profit=scan_min_net_profit,
                    min_roi=scan_min_net_roi,
                    min_sold_comps=scan_min_sold_comps,
                    ebay_fee_pct=EBAY_FEE_PCT,
                    payment_fee_pct=PAYMENT_FEE_PCT,
                    shipping_flat=shipping_flat
                )
                
                net_profit = metrics['net_profit']
                net_roi = metrics['net_roi']
                passed = metrics['passed']
                status = metrics['status']
                fail_reason = metrics.get('fail_reason')
                
                result = {
                    'title': title,
                    'woot_price': buy_price,
                    'expected_sale': expected_sale,
                    'net_profit': net_profit,
                    'net_roi': net_roi,
                    'comps': trimmed_count,
                    'status': status,
                    'reason': fail_reason or '',
                    'woot_url': url,
                    'category': category
                }
                
                all_items.append(result)
                
                # Categorize
                if passed:
                    passed_items.append(result)
                elif trimmed_count >= scan_min_sold_comps and net_profit > 0:
                    # Near miss: has comps and positive profit but didn't meet thresholds
                    nearmiss_items.append(result)
            else:
                # No comps or low confidence
                result = {
                    'title': title,
                    'woot_price': buy_price,
                    'expected_sale': None,
                    'net_profit': None,
                    'net_roi': None,
                    'comps': trimmed_count,
                    'status': 'failed',
                    'reason': 'LOW_CONFIDENCE_COMPS' if trimmed_count < scan_min_sold_comps else 'NO_SOLD_COMPS',
                    'woot_url': url,
                    'category': category
                }
                all_items.append(result)
        
        return {
            "ok": True,
            "summary": {
                "total": len(normalized_deals) + skipped,
                "skipped": skipped,
                "analyzed": len(normalized_deals),
                "passed": len(passed_items),
                "nearmiss": len(nearmiss_items),
                "all": len(all_items)
            },
            "items": {
                "passed": passed_items,
                "nearmiss": nearmiss_items,
                "all": all_items
            }
        }
        
    except Exception as e:
        return JSONResponse(
            status_code=400,
            content={
                "ok": False,
                "error": str(e)
            }
        )

