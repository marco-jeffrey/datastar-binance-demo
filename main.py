from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from collections import deque
import asyncio
import websockets
import json
import threading
import logging
from typing import List, Dict, Any
from datetime import datetime

# Setup logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = FastAPI(title="Binance Dashboard")
templates = Jinja2Templates(directory="templates")

# Add custom Jinja2 filter
def timestamp_to_time(timestamp):
    """Convert timestamp to readable time format"""
    try:
        dt = datetime.fromtimestamp(int(timestamp))
        return dt.strftime("%H:%M:%S")
    except (ValueError, TypeError):
        return "--:--:--"

# Register the custom filter
templates.env.filters['timestamp_to_time'] = timestamp_to_time

# ============================================
# Thread-Safe Data Queues
# ============================================

# Thread-safe deques with max length of 20 items each
trade_queue: deque = deque(maxlen=20)
ticker_queue: deque = deque(maxlen=20)
queue_lock = threading.Lock()

# Latest ticker cache for quick access
latest_ticker: Dict[str, Any] = None

def add_to_queue(queue: deque, data: Dict[str, Any]):
    """Thread-safe addition to queue"""
    with queue_lock:
        queue.appendleft(data)

def get_queue_data(queue: deque) -> List[Dict[str, Any]]:
    """Thread-safe retrieval of queue data as list"""
    with queue_lock:
        return list(queue)

def get_latest_ticker() -> Dict[str, Any] | None:
    """Get the most recent ticker data"""
    with queue_lock:
        return ticker_queue[0] if ticker_queue else None


# ============================================
# Binance WebSocket Listener
# ============================================

BINANCE_WS_URL = "wss://stream.binance.com:9443/ws"
STREAM_NAMES = ["btcusdt@ticker", "btcusdt@trade"]

async def binance_websocket_listener():
    """
    Persistent WebSocket connection that streams data into queues
    """
    while True:
        try:
            # Connect to multiple streams
            stream_params = "/".join(STREAM_NAMES)
            ws_url = f"{BINANCE_WS_URL}/{stream_params}"
            
            logger.info(f"🔌 Connecting to Binance: {ws_url}")
            
            async with websockets.connect(ws_url) as websocket:
                logger.info("✅ Connected to Binance WebSocket")
                
                while True:
                    try:
                        message = await websocket.recv()
                        data = json.loads(message)
                        
                        # Route data to appropriate queue based on event type
                        if data['e'] == 'trade':
                            add_to_queue(trade_queue, data)
                            logger.debug(f"📥 Trade queued: {data['t']}")
                            
                        elif data['e'] == '24hrTicker':
                            add_to_queue(ticker_queue, data)
                            logger.debug(f"📊 Ticker queued: {data['E']}")
                    
                    except websockets.exceptions.ConnectionClosed as cc:
                        logger.warning(f"WebSocket closed: {cc.code} - {cc.reason}")
                        break
                    except Exception as e:
                        logger.error(f"Error processing message: {e}")
                        await asyncio.sleep(1)
        
        except Exception as e:
            logger.error(f"Connection error: {e}")
            logger.info("⏳ Reconnecting in 5 seconds...")
            await asyncio.sleep(5)


# ============================================
# Routes
# ============================================

@app.get("/", response_class=HTMLResponse)
async def dashboard(request: Request):
    """
    Main dashboard route - renders Jinja2 template with current queue data
    """
    # Get thread-safe copies of queue data
    trades = get_queue_data(trade_queue)
    tickers = get_queue_data(ticker_queue)
    current_ticker = get_latest_ticker()
    
    logger.info(f"📊 Dashboard loaded: {len(trades)} trades, {len(tickers)} tickers")
    
    return templates.TemplateResponse(
        "index.html",
        {
            "request": request,
            "trades": trades,
            "tickers": tickers,
            "latest_ticker": current_ticker,
            "symbol": "BTC/USDT"
        }
    )


@app.on_event("startup")
async def startup_event():
    """Start WebSocket listener on startup"""
    asyncio.create_task(binance_websocket_listener())
    logger.info("🚀 WebSocket listener started")


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)