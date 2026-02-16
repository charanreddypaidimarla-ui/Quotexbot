import asyncio
import time
from typing import Optional, Dict, Any
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
import uvicorn

try:
    from pyquotex.stable_api import Quotex
    from pyquotex.utils.processor import process_candles
except ImportError:
    raise RuntimeError("Error: pyquotex not found! Ensure it is installed via requirements.txt")

app = FastAPI(title="Quotex Signal API")

# Global bot instance to keep the websocket alive
bot_client: Optional[Quotex] = None

class LoginRequest(BaseModel):
    email: str
    password: str

async def get_high_payout_pairs() -> list:
    """Extracts pairs with >= 90% payout from Quotex."""
    if not bot_client:
        return []
        
    payment_data = bot_client.get_payment()
    all_assets = await bot_client.get_all_assets()
    
    if not payment_data or not all_assets:
        return []

    high_payout_names = []
    for asset_name, asset_data in payment_data.items():
        try:
            if not isinstance(asset_data, dict) or not asset_data.get('open', False):
                continue
            
            profit_data = asset_data.get('profit', {})
            payout = float(profit_data.get('1M', 0)) if isinstance(profit_data, dict) else 0
            
            if payout >= 90.0:
                high_payout_names.append({'name': asset_name, 'payout': payout})
        except:
            continue

    # Map display names to actual asset codes
    pairs_with_codes = []
    for pair_info in high_payout_names:
        display_clean = pair_info['name'].replace('(OTC)', '').replace('(otc)', '').strip().replace('/', '').replace(' ', '').upper()
        
        for asset_code in list(all_assets.keys())[:100]:
            asset_code_clean = str(asset_code).replace('_otc', '').replace('_OTC', '').upper()
            
            if display_clean in asset_code_clean or asset_code_clean in display_clean:
                pairs_with_codes.append({
                    'asset_code': asset_code,
                    'payout': pair_info['payout'],
                    'display_name': pair_info['name']
                })
                break
                
    # Sort by highest payout and return top 10
    pairs_with_codes.sort(key=lambda x: x['payout'], reverse=True)
    return pairs_with_codes[:10]

async def analyze_pair(asset_code: str, display_name: str) -> Optional[Dict[str, Any]]:
    """Analyzes a single pair and generates signal logic, S&R, etc."""
    try:
        candles = await bot_client.get_candles(asset_code, time.time(), 3600, 60)
        
        if not candles or len(candles) < 30:
            return None
        
        if not candles[0].get("open"):
            candles = process_candles(candles, 60)
            
        closes = [float(c.get('close', 0)) for c in candles]
        highs = [float(c.get('max', c.get('high', closes[i]))) for i, c in enumerate(candles)]
        lows = [float(c.get('min', c.get('low', closes[i]))) for i, c in enumerate(candles)]
        
        current_price = closes[-1]
        
        # Calculate Support & Resistance (Local min/max of last 20 candles)
        resistance = max(highs[-20:])
        support = min(lows[-20:])
        
        # Moving averages
        ma20 = sum(closes[-20:]) / 20
        ma5  = sum(closes[-5:]) / 5
        
        # RSI Calculation
        gains, losses = [], []
        for i in range(-15, -1):
            diff = closes[i+1] - closes[i]
            if diff >= 0: gains.append(diff)
            else: losses.append(abs(diff))
            
        avg_gain = sum(gains)/14 if gains else 0.0001
        avg_loss = sum(losses)/14 if losses else 0.0001
        rs = avg_gain / avg_loss
        rsi = 100 - (100 / (1 + rs))
        
        # Volatility filter
        if (max(closes[-10:]) - min(closes[-10:])) < 0.00005:
            return None
            
        score = 0
        logic = []
        
        # Trend check
        if ma5 > ma20:
            score += 40
            logic.append("MA5 crossed above MA20 (Short-term Uptrend)")
        else:
            score -= 40
            logic.append("MA5 crossed below MA20 (Short-term Downtrend)")
            
        # RSI Check
        if rsi < 40:
            score += 40
            logic.append(f"RSI is oversold ({rsi:.1f}), indicating potential reversal upward")
        elif rsi > 60:
            score -= 40
            logic.append(f"RSI is overbought ({rsi:.1f}), indicating potential reversal downward")
            
        # Distance from MA
        if abs(current_price - ma20) / ma20 < 0.002:
            score += 20 if score > 0 else -20
            logic.append("Price is testing the 20 MA baseline")

        # Compile Signal
        if score >= 60:
            direction = "CALL 🟢"
            confidence = min(score, 98) # Cap at 98%
        elif score <= -60:
            direction = "PUT 🔴"
            confidence = min(abs(score), 98)
        else:
            return None # Not a strong enough signal

        return {
            "pair": display_name,
            "direction": direction,
            "confidence": f"{confidence}%",
            "time_frame": "1 Minute",
            "current_price": round(current_price, 5),
            "support": round(support, 5),
            "resistance": round(resistance, 5),
            "logic": " | ".join(logic)
        }
        
    except Exception as e:
        print(f"Error analyzing {asset_code}: {e}")
        return None

@app.post("/login")
async def login(req: LoginRequest):
    """Endpoint to authenticate the bot."""
    global bot_client
    try:
        if bot_client:
            await bot_client.close()
            
        bot_client = Quotex(email=req.email, password=req.password, lang="en")
        check, reason = await bot_client.connect()
        
        if check:
            bot_client.set_account_mode("PRACTICE") # Change to REAL if needed
            return {"status": "success", "message": "Successfully connected to Quotex websocket."}
        else:
            bot_client = None
            raise HTTPException(status_code=401, detail=f"Login failed: {reason}")
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/get_signal")
async def get_signal():
    """Endpoint your Telegram bot will call to get the best current signal."""
    if not bot_client:
        raise HTTPException(status_code=401, detail="Bot is not logged in. Call /login first.")
        
    pairs = await get_high_payout_pairs()
    if not pairs:
        return {"status": "error", "message": "Could not find any high payout pairs currently open."}
        
    best_signal = None
    highest_confidence = 0
    
    # Analyze pairs to find the absolute best setup right now
    for pair in pairs:
        signal = await analyze_pair(pair['asset_code'], pair['display_name'])
        
        if signal:
            conf_int = int(signal['confidence'].replace('%', ''))
            if conf_int > highest_confidence:
                highest_confidence = conf_int
                best_signal = signal
                
            # If we find a 90%+ confidence setup, return it immediately to save time
            if highest_confidence >= 90:
                break
                
        # Small delay to prevent rate-limiting while fetching candles
        await asyncio.sleep(0.5)
        
    if best_signal:
        return {"status": "success", "data": best_signal}
    else:
        return {"status": "waiting", "message": "No strong setups found right now. Try again in 1 minute."}

if __name__ == "__main__":
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=True)
  
