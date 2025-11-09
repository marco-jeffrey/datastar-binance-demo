# Binance WebSocket Dashboard

A real-time cryptocurrency dashboard built with FastAPI and [Datastar](https://data-star.dev/), streaming live BTC/USDT trades and ticker data from Binance WebSocket API.

## Features

- 🔴 **Real-time WebSocket streaming** from Binance
- ⚡ **Server-Sent Events (SSE)** for live UI updates via Datastar
- 🗜️ **Brotli compression** for both regular and streaming responses
- 🧵 **Thread-safe data queues** with automatic size management
- 📊 **Live trade feed** and 24hr ticker statistics
- 🎨 **Responsive dashboard** with Jinja2 templates

## Architecture

### Data Flow
```
Binance WebSocket → Thread-safe Queues → SSE Stream → Browser (Datastar)
```

### Components

- **WebSocket Listener**: Persistent connection to Binance streaming API
- **Thread-safe Queues**: Deque-based buffers (max 20 items) for trades and tickers
- **SSE Endpoint**: Datastar-powered streaming updates at 10Hz
- **Brotli Middleware**: Custom ASGI middleware for efficient compression

### Key Technologies

- **FastAPI**: Modern async web framework
- **Datastar**: Reactive SSE-based frontend library
- **websockets**: Async WebSocket client
- **Brotli**: High-compression ratio for streaming data
- **uv**: Fast Python package manager

## Installation

```bash
# Clone the repository
# Install dependencies with uv
uv sync
```

## Usage

```bash
# Start the development server
uv run uvicorn main:app --reload
```

The dashboard will be available at `http://localhost:8000`

## API Endpoints

### `GET /`
Renders the main dashboard with initial data.

**Response**: HTML page with embedded Jinja2 template

### `GET /updates`
Server-Sent Events stream for real-time updates.

**Response**: Continuous SSE stream with Datastar patch events
- Update frequency: 100ms (10Hz)
- Content: Rendered HTML fragments
- Compression: Brotli (streaming)