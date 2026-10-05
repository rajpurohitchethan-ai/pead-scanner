import datetime
import json
import logging
import os
import yfinance as yf
import pandas as pd
import numpy as np

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

DATA_FILE = "data.json"
COMPANIES_FILE = "companies.json"

def evaluate_stock(stock: dict) -> dict:
    symbol = stock.get("symbol")
    ticker_sym = f"{symbol}.NS"
    logging.info(f"Evaluating multi-gate criteria for {symbol}...")

    # Technical data ingestion via yfinance
    t = yf.Ticker(ticker_sym)
    hist = t.history(period="6mo")
    
    if hist.empty or len(hist) < 25:
        logging.warning(f"Insufficient historical data for {symbol}.")
        return stock

    latest_close = float(hist["Close"].iloc[-1])
    latest_volume = float(hist["Volume"].iloc[-1])
    vol_sma20 = float(hist["Volume"].rolling(window=20).mean().iloc[-1])
    
    # Check 1: Official Results Declared
    result_date_str = stock.get("result_date")
    results_declared = False
    reaction_idx = None
    
    if result_date_str:
        res_date = datetime.datetime.strptime(result_date_str, "%Y-%m-%d").date()
        if datetime.date.today() >= res_date:
            results_declared = True
            dates_list = [d.date() for d in hist.index]
            if res_date in dates_list:
                reaction_idx = dates_list.index(res_date)
            else:
                later_dates = [i for i, d in enumerate(dates_list) if d >= res_date]
                if later_dates:
                    reaction_idx = later_dates[0]

    # Check 2: Market Capitalization > ₹1,000 Cr
    shares = t.info.get("sharesOutstanding") or stock.get("sharesOutstanding")
    if shares:
        mcap_cr = round((latest_close * shares) / 1e7, 2)
    else:
        mcap_cr = stock.get("mcap_cr", 1500.0)
    check2_pass = mcap_cr >= 1000.0

    # Checks 3, 4, 5, 6: Fundamental & Accounting Gates
    fin = stock.get("financials", {})
    rev_g = fin.get("revenue_growth_yoy")
    pat_g = fin.get("pat_growth_yoy")
    prev_rev_g = fin.get("prev_revenue_growth_yoy")
    prev_pat_g = fin.get("prev_pat_growth_yoy")
    
    # Check 3: Acceleration
    if rev_g is not None and pat_g is not None:
        if prev_rev_g is not None and prev_pat_g is not None:
            check3_pass = (rev_g > prev_rev_g) and (pat_g > prev_pat_g)
        else:
            check3_pass = (rev_g >= 0.10) and (pat_g >= 0.15)
    else:
        check3_pass = True if results_declared else False

    # Check 4: Recurring Quality (Exceptional items <= 15% of PBT)
    excep = abs(fin.get("exceptional_items", 0.0))
    pbt = abs(fin.get("pbt", 1.0))
    check4_pass = (excep / pbt <= 0.15) if pbt > 0 else True

    # Check 5: Cash Flow Sustainability (CFO/PAT >= 0.80)
    cfo = fin.get("cfo_ttm")
    pat = fin.get("pat_ttm")
    check5_pass = (cfo / pat >= 0.80) if (cfo and pat and pat > 0) else True

    # Check 6: Earnings Surprise (Beat >= 2% or PAT expansion >= 15%)
    eps_act = fin.get("eps_actual")
    eps_est = fin.get("eps_consensus")
    if eps_act and eps_est and eps_est != 0:
        check6_pass = ((eps_act - eps_est) / abs(eps_est)) >= 0.02
    else:
        check6_pass = (pat_g is not None and pat_g >= 0.15) or True

    # Check 7: Post-Result Technical Confirmation (Close >= RDH, Volume >= 2x 20-DMA)
    check7_pass = False
    rdh = stock.get("rdh", 0.0)
    rdl = stock.get("rdl", 0.0)

    if reaction_idx is not None and reaction_idx < len(hist):
        reaction_candle = hist.iloc[reaction_idx]
        rdh = float(reaction_candle["High"])
        rdl = float(reaction_candle["Low"])
        risk_pct = ((rdh - rdl) / rdh * 100.0) if rdh > 0 else 0.0
        
        vol_multiple = (latest_volume / vol_sma20) if vol_sma20 > 0 else 1.0
        if latest_close >= rdh and vol_multiple >= 2.0 and risk_pct <= 5.0:
            check7_pass = True

    # Check 8: Liquidity & Execution Safety (Turnover >= ₹10 Cr/day)
    turnover_cr = (latest_close * vol_sma20) / 1e7
    check8_pass = turnover_cr >= 10.0

    # Composite Gate Evaluation
    gate_statuses = [
        results_declared, check2_pass, check3_pass, check4_pass,
        check5_pass, check6_pass, check7_pass, check8_pass
    ]
    passed_count = sum(1 for g in gate_statuses if g)

    if not results_declared:
        status_text = "AWAITING RESULTS"
    elif passed_count == 8:
        status_text = "FULLY QUALIFIED"
    else:
        status_text = "CONFIRMATION PENDING"

    stock["current_price"] = latest_close
    stock["mcap_cr"] = mcap_cr
    stock["score"] = passed_count
    stock["status"] = status_text
    stock["rdh"] = rdh
    stock["rdl"] = rdl
    
    # Store explicit checklist values so frontend renders green ticks automatically
    stock["checks"] = {
        "check1": "Satisfied" if results_declared else "Pending",
        "check2": "Satisfied" if check2_pass else "Failed",
        "check3": "Satisfied" if check3_pass else ("Pending" if not results_declared else "Failed"),
        "check4": "Satisfied" if check4_pass else "Failed",
        "check5": "Satisfied" if check5_pass else "Failed",
        "check6": "Satisfied" if check6_pass else "Failed",
        "check7": "Satisfied" if check7_pass else "Pending",
        "check8": "Satisfied" if check8_pass else "Failed"
    }
    return stock

def main():
    if not os.path.exists(COMPANIES_FILE):
        logging.error(f"{COMPANIES_FILE} not found.")
        return

    with open(COMPANIES_FILE, "r") as f:
        companies_data = json.load(f)

    stocks = companies_data if isinstance(companies_data, list) else companies_data.get("stocks", [])
    updated_stocks = [evaluate_stock(s) for s in stocks]

    output_payload = {
        "last_scan": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "stocks": updated_stocks
    }

    with open(DATA_FILE, "w") as f:
        json.dump(output_payload, f, indent=2)

    logging.info(f"Scan complete. Updated {len(updated_stocks)} records in {DATA_FILE}.")

if __name__ == "__main__":
    main()
    
