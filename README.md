# Achilles FPL Platform — Standalone MVP

This is an independent platform built from scratch so it can run alongside the existing Streamlit Achilles app. It does not modify or depend on the current app.

## Goals
- 40-manager Achilles league
- Direct FPL API integration
- Separate competition pages
- Editable admin configuration
- Independent deployment path
- Safe fallback: the current Streamlit app remains untouched

## Run locally
```bash
pip install -r requirements.txt
uvicorn main:app --reload
```
Then open http://127.0.0.1:8000

## Configuration
Edit `data/settings.json` or use `/admin`. The normal editable areas include league ID, season, manager count, entry fee, competition switches and European group assignments.

## Deployment
This MVP is designed for Render/Railway/Fly.io-style deployment. Start command:
`uvicorn main:app --host 0.0.0.0 --port $PORT`

## Architecture
FastAPI backend + Jinja2 server-rendered UI + JSON configuration. A later phase can replace the JSON store with PostgreSQL and add authentication without changing the public page structure.
