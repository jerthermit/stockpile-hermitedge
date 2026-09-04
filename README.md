<p align="center">
  <img src="frontend/public/app-logo.png" alt="Stockpile" width="72" />
</p>

<h1 align="center">Stockpile</h1>

<p align="center"><strong>Location-aware inventory control for busy stockrooms.</strong></p>
<p align="center">Receiving · Locations · Handoffs · Counts · Order checks · AI review</p>

Stockpile is a location-aware AI inventory system customized around how each client receives, stores, moves, sells, and checks stock.

Built for growing supply businesses.

## What it does

- Shows every item, quantity, photo, and exact rack or bin.
- Adds new products or identifies existing ones through barcode scanners.
- Records deliveries, opening stock, locations, and staff badge sign-offs.
- Tracks transfers and confirms handoffs at the receiving point.
- Handles sales, returns, damaged stock, reservations, and physical counts.
- Blocks duplicate, unauthorized, invalid, and below-zero movements.
- Keeps every correction without erasing the original history.
- Opens investigations for missing or misplaced stock.
- Checks order exports from Shopee, Lazada, and other selling apps against recorded stock removals.
- Imports products and opening stock from CSV or Google Sheets exports.
- Uses AI to read delivery documents and prioritize unresolved mismatches.

## Stock flow

```text
Receive → Place → Move → Sell / reserve / return / damage → Count → Reconcile
```

## How it stays reliable

- The Stockpile database holds the official inventory record.
- Every accepted action changes inventory once and records who did it, where, and when.
- Owners control protected changes while staff handle daily stock work.
- Corrections preserve the original action instead of rewriting it.
- AI reviews and explains records; inventory changes remain rule-based.

## Architecture

```text
Barcode scanner / browser
           ↓
        Next.js
           ↓
        FastAPI ─────→ Together AI
           ↓
 PostgreSQL / SQLite
```

**Stack:** Next.js, TypeScript, FastAPI, Pydantic, PostgreSQL, SQLite, Together AI, Vercel, and Railway.

## Run locally

### Backend

```bash
cd backend
python3.11 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
uvicorn main:app --reload
```

The API runs at `http://localhost:8000`.

### Frontend

```bash
cd frontend
npm ci
cp .env.example .env.local
npm run dev
```

Open `http://localhost:3000`.

Configure the API URL, authentication secret, database connection, and demo identities in the copied environment files. AI review requires a server-side `TOGETHER_API_KEY`.

## Verification

```bash
cd backend
pip install -r requirements-dev.txt
python -m pytest tests

cd ../frontend
npm run typecheck
npm run build
```

## Deployment

- Deploy `frontend/` to Vercel and set `NEXT_PUBLIC_API_URL` to the Railway API ending in `/api/v1`.
- Deploy `backend/` to Railway with PostgreSQL, a strong `STOCKPILE_AUTH_SECRET`, and the exact Vercel origin in `FRONTEND_URL`.
- Mount persistent storage for product images and point `STOCKPILE_PRODUCT_IMAGE_DIR` to it.
- Set `STOCKPILE_SEED_DEMO_DATA=true` only for the synthetic portfolio demo.
- Store the Together AI key and every other credential in deployment environment variables.

## Project context

The public build reconstructs workflows from previously commissioned client work.

Built by [Emman at Hermit Edge](https://hermitedge.com).

## Copyright

Copyright © 2026 Emman Ermitaño. All rights reserved.
