#!/usr/bin/env python3
"""qwen-vision direction logger (observe-only). Reads charts every ~180s, logs per-asset direction
with ts + the Binance close at read-time, so calls can be SCORED against the actual 15m resolution."""
import json, time, os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from src.analysis.model_factory import ModelFactory, _BINANCE_SYMBOL
import urllib.request
OUT = "data/calibration/qwen_vision_reads.jsonl"
ASSETS = ['BTC','SOL','ETH','XRP','BNB','DOGE']
def spot(sym):
    try:
        u=f"https://api.binance.com/api/v3/ticker/price?symbol={sym}"
        return float(json.load(urllib.request.urlopen(u,timeout=8))['price'])
    except Exception: return None
prov = ModelFactory.create('qwen_vision')
while True:
    now=int(time.time())
    for a in ASSETS:
        sym=_BINANCE_SYMBOL.get(a)
        try: r=prov.predict_direction(a, {}, horizon_min=15)
        except Exception: r=None
        if r:
            row={"ts":now,"asset":a,"sym":sym,"dir":r.get("dir"),"conf":r.get("conf"),
                 "why":r.get("why"),"spot_at_read":spot(sym),"provider":"qwen_vision"}
            with open(OUT,"a") as f: f.write(json.dumps(row)+"\n")
    time.sleep(180)
