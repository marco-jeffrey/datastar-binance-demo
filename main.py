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
from datastar_py import ServerSentEventGenerator as SSE
from datastar_py.fastapi import datastar_response, DatastarResponse

# Setup logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

import brotli
from starlette.datastructures import Headers
from starlette.types import ASGIApp, Message, Receive, Scope, Send

class BrotliCompressionMiddleware:
    """Compresses HTTP responses with Brotli, including streaming SSE"""
    
    def __init__(self, app: ASGIApp, quality: int = 4, minimum_size: int = 500):
        self.app = app
        self.quality = quality
        self.minimum_size = minimum_size

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        headers = Headers(scope=scope)
        if "br" not in headers.get("accept-encoding", ""):
            await self.app(scope, receive, send)
            return

        # Compress SSE endpoint differently (streaming)
        if scope.get("path") == "/updates":
            await self._handle_sse(scope, receive, send)
        else:
            # Compress regular responses (buffered)
            await self._handle_regular(scope, receive, send)

    async def _handle_sse(self, scope: Scope, receive: Receive, send: Send) -> None:
        """Compress SSE stream chunk by chunk"""
        compressor = brotli.Compressor(quality=self.quality, lgwin=22, mode=brotli.MODE_TEXT)
        headers_sent = False
        
        async def send_compressed(message: Message) -> None:
            nonlocal headers_sent
            
            if message["type"] == "http.response.start":
                # Modify headers to indicate Brotli compression
                headers = list(message["headers"])
                headers = [(k, v) for k, v in headers if k.lower() != b"content-encoding"]
                headers.append((b"content-encoding", b"br"))
                message["headers"] = headers
                headers_sent = True
                await send(message)
                
            elif message["type"] == "http.response.body":
                body = message.get("body", b"")
                more_body = message.get("more_body", False)
                
                if body:
                    # Compress the chunk - process() may buffer internally
                    compressed = compressor.process(body) + compressor.flush()
                    if compressed:
                        await send({
                            "type": "http.response.body",
                            "body": compressed,
                            "more_body": more_body
                        })
                    elif more_body:
                        # If no data yet but more coming, just continue
                        # Don't send empty chunks in the middle of the stream
                        pass
                
                # If this is the last chunk, flush the compressor
                if not more_body:
                    final_data = compressor.finish()
                    if final_data:
                        await send({
                            "type": "http.response.body",
                            "body": final_data,
                            "more_body": False
                        })
                    else:
                        # Send empty final chunk to close the stream
                        await send({
                            "type": "http.response.body",
                            "body": b"",
                            "more_body": False
                        })

        await self.app(scope, receive, send_compressed)

    async def _handle_regular(self, scope: Scope, receive: Receive, send: Send) -> None:
        """Compress regular (non-streaming) responses"""
        body_parts = []
        headers_to_send = None
        
        async def send_buffered(message: Message) -> None:
            nonlocal headers_to_send
            
            if message["type"] == "http.response.start":
                # Store headers, don't send yet
                headers_to_send = message
                
            elif message["type"] == "http.response.body":
                body = message.get("body", b"")
                more_body = message.get("more_body", False)
                
                if body:
                    body_parts.append(body)
                
                # When we have all the body, compress and send
                if not more_body:
                    full_body = b"".join(body_parts)
                    
                    # Only compress if body is large enough
                    if len(full_body) >= self.minimum_size:
                        compressed = brotli.compress(full_body, quality=self.quality)
                        
                        # Modify headers
                        headers = list(headers_to_send["headers"])
                        headers = [(k, v) for k, v in headers if k.lower() not in (b"content-encoding", b"content-length")]
                        headers.append((b"content-encoding", b"br"))
                        headers.append((b"content-length", str(len(compressed)).encode()))
                        headers_to_send["headers"] = headers
                        
                        # Send headers and compressed body
                        await send(headers_to_send)
                        await send({
                            "type": "http.response.body",
                            "body": compressed,
                            "more_body": False
                        })
                    else:
                        # Send uncompressed
                        await send(headers_to_send)
                        await send({
                            "type": "http.response.body",
                            "body": full_body,
                            "more_body": False
                        })

        await self.app(scope, receive, send_buffered)

# Add the middleware to your app

app = FastAPI(title="Binance Dashboard")
app.add_middleware(BrotliCompressionMiddleware)

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



@app.get("/updates")
@datastar_response
async def updates(request: Request) -> DatastarResponse:
    """
    get updates via Server-Sent Events (SSE)
    """
    while True:
        # Get thread-safe copies of queue data
        trades = get_queue_data(trade_queue)
        tickers = get_queue_data(ticker_queue)
        current_ticker = get_latest_ticker()
                
        # Render the template in a threadpool to avoid blocking
        loop = asyncio.get_event_loop()
        fragment = await loop.run_in_executor(
            None,
            lambda: templates.get_template("content.html").render(
                request=request,
                trades=trades,
                tickers=tickers,
                latest_ticker=current_ticker,
                symbol="BTC/USDT"
            )
        )
        yield SSE.patch_elements(fragment, "#content", mode="inner")
        await asyncio.sleep(0.1)


@app.on_event("startup")
async def startup_event():
    """Start WebSocket listener on startup"""
    asyncio.create_task(binance_websocket_listener())
    logger.info("🚀 WebSocket listener started")