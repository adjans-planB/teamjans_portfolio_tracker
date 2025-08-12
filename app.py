"""
TeamJans Portfolio Tracker
-------------------------

Flask app to track ASX portfolios with live prices, multiple portfolios,
cash balances, and P/L. Supports SQLite locally and PostgreSQL on Render.
"""

from __future__ import annotations

import os
import time
from datetime import datetime
from typing import Optional, Tuple

import requests
from flask import Flask, redirect, render_template, request, url_for, flash
from sqlalchemy import create_engine, text

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "teamjans-secret")

# Local SQLite path (used only if DATABASE_URL is not set)
DATABASE = os.path.join(os.path.dirname(__file__), "portfolio.db")

# Build SQLAlchemy engine (normalize postgres scheme for SQLAlchemy 2.x)
db_url = os.environ.get("DATABASE_URL")
if db_url:
    if db_url.startswith("postgres://"):
        db_url = db_url.replace("postgres://", "postgresql://", 1)
    engine = create_engine(db_url, future=True)
else:
    engine = create_engine(f"sqlite:///{DATABASE}", future=True)

DB_IS_POSTGRES = engine.url.get_backend_name() == "postgresql"


def init_db() -> None:
    """
    Create tables if they do not exist yet.
    """
    if DB_IS_POSTGRES:
        create_portfolios = (
            "CREATE TABLE IF NOT EXISTS portfolios ("
            "id SERIAL PRIMARY KEY, "
            "name TEXT NOT NULL, "
            "cash_balance NUMERIC DEFAULT 0.0)"
        )
        create_holdings = (
            "CREATE TABLE IF NOT EXISTS holdings ("
            "id SERIAL PRIMARY KEY, "
            "portfolio_id INTEGER NOT NULL REFERENCES portfolios(id), "
            "ticker TEXT NOT NULL, "
            "quantity NUMERIC NOT NULL, "
            "purchase_price NUMERIC NOT NULL, "
            "purchase_date DATE DEFAULT CURRENT_DATE, "
            "sold BOOLEAN DEFAULT FALSE)"
        )
        create_last_quotes = (
            "CREATE TABLE IF NOT EXISTS last_quotes ("
            "ticker TEXT PRIMARY KEY, "
            "price NUMERIC, "
            "prev_close NUMERIC, "
            "\"change\" NUMERIC, "
            "updated_at TIMESTAMPTZ DEFAULT NOW())"
        )
    else:
        create_portfolios = (
            "CREATE TABLE IF NOT EXISTS portfolios ("
            "id INTEGER PRIMARY KEY AUTOINCREMENT, "
            "name TEXT NOT NULL, "
            "cash_balance REAL DEFAULT 0.0)"
        )
        create_holdings = (
            "CREATE TABLE IF NOT EXISTS holdings ("
            "id INTEGER PRIMARY KEY AUTOINCREMENT, "
            "portfolio_id INTEGER NOT NULL, "
            "ticker TEXT NOT NULL, "
            "quantity REAL NOT NULL, "
            "purchase_price REAL NOT NULL, "
            "purchase_date TEXT DEFAULT CURRENT_DATE, "
            "sold INTEGER DEFAULT 0, "
            "FOREIGN KEY (portfolio_id) REFERENCES portfolios (id))"
        )
        create_last_quotes = (
            "CREATE TABLE IF NOT EXISTS last_quotes ("
            "ticker TEXT PRIMARY KEY, "
            "price REAL, "
            "prev_close REAL, "
            "\"change\" REAL, "
            "updated_at TEXT DEFAULT CURRENT_TIMESTAMP)"
        )

    with engine.begin() as conn:
        conn.execute(text(create_portfolios))
        conn.execute(text(create_holdings))
        conn.execute(text(create_last_quotes))


# Ensure tables exist at import time (Flask 3 removed before_first_request)
init_db()

# ----------------------------
# In-memory price cache & rate limit cooldown
# ----------------------------

# { "BHP.AX": (timestamp, (price, prev_close, change)) }
PRICE_CACHE: dict[str, tuple[float, tuple[Optional[float], Optional[float], Optional[float]]]] = {}

# Cache time-to-live (seconds)
PRICE_TTL_SECONDS = 600  # 10 minutes

# Backoff window after a 429 (seconds)
RATE_LIMIT_COOLDOWN = 120
RATE_LIMIT_UNTIL: float = 0.0


def _cache_get(ticker: str):
    entry = PRICE_CACHE.get(ticker)
    if not entry:
        return None
    ts, triple = entry
    if time.time() - ts <= PRICE_TTL_SECONDS:
        return triple
    return None


def _cache_set(ticker: str, triple):
    PRICE_CACHE[ticker] = (time.time(), triple)


def _rate_limited() -> bool:
    return time.time() < RATE_LIMIT_UNTIL


def _set_rate_limit_cooldown():
    global RATE_LIMIT_UNTIL
    RATE_LIMIT_UNTIL = time.time() + RATE_LIMIT_COOLDOWN


# ----------------------------
# Persistent quote store (DB)
# ----------------------------
def _db_get_quote(ticker: str):
    with engine.connect() as conn:
        row = (
            conn.execute(
                text("SELECT price, prev_close, \"change\" FROM last_quotes WHERE ticker = :t"),
                {"t": ticker},
            )
            .first()
        )
    if row:
        price, prev_close, change = row
        return (
            float(price) if price is not None else None,
            float(prev_close) if prev_close is not None else None,
            float(change) if change is not None else None,
        )
    return None


def _db_set_quote(ticker: str, triple):
    price, prev_close, change = triple
    if DB_IS_POSTGRES:
        sql = (
            "INSERT INTO last_quotes (ticker, price, prev_close, \"change\", updated_at) "
            "VALUES (:t, :p, :pc, :c, NOW()) "
            "ON CONFLICT (ticker) DO UPDATE SET "
            "price = EXCLUDED.price, prev_close = EXCLUDED.prev_close, "
            "\"change\" = EXCLUDED.\"change\", updated_at = NOW()"
        )
    else:
        # SQLite upsert
        sql = (
            "INSERT INTO last_quotes (ticker, price, prev_close, \"change\", updated_at) "
            "VALUES (:t, :p, :pc, :c, CURRENT_TIMESTAMP) "
            "ON CONFLICT(ticker) DO UPDATE SET "
            "price = excluded.price, prev_close = excluded.prev_close, "
            "\"change\" = excluded.\"change\", updated_at = CURRENT_TIMESTAMP"
        )
    with engine.begin() as conn:
        conn.execute(
            text(sql),
            {"t": ticker, "p": price, "pc": prev_close, "c": change},
        )


# ----------------------------
# Batch quotes fetch via RapidAPI
# ----------------------------
def fetch_quotes_batch(tickers: list[str]) -> dict[str, tuple[Optional[float], Optional[float], Optional[float]]]:
    """
    Fetch quotes for multiple tickers in one call via RapidAPI market/v2/get-quotes.
    Uses cache first; writes successes to DB; falls back to DB on rate limits.
    Returns: {ticker: (price, prev, change)}; values may be None.
    """
    result: dict[str, tuple[Optional[float], Optional[float], Optional[float]]] = {}

    # From cache first
    missing: list[str] = []
    for t in tickers:
        cached = _cache_get(t)
        if cached is not None:
            result[t] = cached
        else:
            missing.append(t)

    # If all covered by cache or we're in cooldown, fill the rest from DB and return
    if not missing or _rate_limited():
        for t in list(missing):
            dbq = _db_get_quote(t)
            if dbq is not None:
                result[t] = dbq
        return result

    rapidapi_key = os.environ.get("RAPIDAPI_KEY")
    if not rapidapi_key:
        # No RapidAPI — use DB fallback for missing
        for t in list(missing):
            dbq = _db_get_quote(t)
            if dbq is not None:
                result[t] = dbq
        return result

    try:
        headers = {
            "x-rapidapi-host": "apidojo-yahoo-finance-v1.p.rapidapi.com",
            "x-rapidapi-key": rapidapi_key,
        }
        market_url = "https://apidojo-yahoo-finance-v1.p.rapidapi.com/market/v2/get-quotes"
        params = {"region": "AU", "symbols": ",".join(missing)}

        resp = requests.get(market_url, headers=headers, params=params, timeout=8)
        resp.raise_for_status()
        data = resp.json()
        print(f"[DEBUG] batch get-quotes: {params['symbols']} -> {data}")

        quotes = data.get("quoteResponse", {}).get("result", [])
        by_symbol = {q.get("symbol"): q for q in quotes if q.get("symbol")}

        for t in missing:
            q = by_symbol.get(t)
            if q:
                price = q.get("regularMarketPrice")
                prev = q.get("regularMarketPreviousClose")
                change = q.get("regularMarketChange")
                triple = (price, prev, change)
                _cache_set(t, triple)
                _db_set_quote(t, triple)
                result[t] = triple
            else:
                dbq = _db_get_quote(t)
                result[t] = dbq if dbq is not None else (None, None, None)

    except requests.HTTPError as e:
        if getattr(e.response, "status_code", None) == 429:
            print("[DEBUG] batch quotes rate-limited (429); entering cooldown")
            _set_rate_limit_cooldown()
            for t in missing:
                if t not in result:
                    dbq = _db_get_quote(t)
                    result[t] = dbq if dbq is not None else (None, None, None)
            return result
        print(f"[DEBUG] batch quotes HTTP error: {e}")
        for t in missing:
            if t not in result:
                dbq = _db_get_quote(t)
                result[t] = dbq if dbq is not None else (None, None, None)
    except Exception as e:
        print(f"[DEBUG] batch quotes error: {e}")
        for t in missing:
            if t not in result:
                dbq = _db_get_quote(t)
                result[t] = dbq if dbq is not None else (None, None, None)

    return result


# ----------------------------
# Single-ticker fetch (uses cache/DB/cooldown)
# ----------------------------
def get_stock_price(ticker: str) -> Tuple[Optional[float], Optional[float], Optional[float]]:
    """
    Single-ticker fetch with cache + cooldown awareness + DB fallback.
    Returns (current_price, previous_close, change) or (None, None, None).
    """
    # Cache first
    cached = _cache_get(ticker)
    if cached is not None:
        return cached

    # If cooling down after a 429, try DB before giving up
    if _rate_limited():
        dbq = _db_get_quote(ticker)
        return dbq if dbq is not None else (None, None, None)

    rapidapi_key = os.environ.get("RAPIDAPI_KEY")

    # RapidAPI preferred path
    if rapidapi_key:
        # market/v2/get-quotes
        try:
            headers = {
                "x-rapidapi-host": "apidojo-yahoo-finance-v1.p.rapidapi.com",
                "x-rapidapi-key": rapidapi_key,
            }
            market_url = "https://apidojo-yahoo-finance-v1.p.rapidapi.com/market/v2/get-quotes"
            params = {"region": "AU", "symbols": ticker}

            resp = requests.get(market_url, headers=headers, params=params, timeout=8)
            resp.raise_for_status()
            data = resp.json()
            print(f"[DEBUG] market/v2/get-quotes for {ticker}: {data}")

            results = data.get("quoteResponse", {}).get("result", [])
            if results:
                r = results[0]
                price = r.get("regularMarketPrice")
                prev = r.get("regularMarketPreviousClose")
                change = r.get("regularMarketChange")
                if price is not None or prev is not None or change is not None:
                    triple = (price, prev, change)
                    _cache_set(ticker, triple)
                    _db_set_quote(ticker, triple)
                    return triple
        except requests.HTTPError as e:
            if getattr(e.response, "status_code", None) == 429:
                print(f"[DEBUG] quotes 429 for {ticker}; entering cooldown")
                _set_rate_limit_cooldown()
                if cached is not None:
                    return cached
                dbq = _db_get_quote(ticker)
                if dbq is not None:
                    return dbq
            else:
                print(f"[DEBUG] quotes error for {ticker}: {e}")
            if cached is not None:
                return cached
            dbq = _db_get_quote(ticker)
            if dbq is not None:
                return dbq
        except Exception as e:
            print(f"[DEBUG] quotes error for {ticker}: {e}")
            if cached is not None:
                return cached
            dbq = _db_get_quote(ticker)
            if dbq is not None:
                return dbq

        # stock/v2/get-summary
        try:
            summary_url = "https://apidojo-yahoo-finance-v1.p.rapidapi.com/stock/v2/get-summary"
            params2 = {"symbol": ticker, "region": "AU"}

            resp2 = requests.get(summary_url, headers=headers, params=params2, timeout=8)
            resp2.raise_for_status()
            data2 = resp2.json()
            print(f"[DEBUG] stock/v2/get-summary for {ticker}: {data2}")

            price_info = data2.get("price", {}) or {}
            price = (price_info.get("regularMarketPrice") or {}).get("raw")
            prev_close = (price_info.get("regularMarketPreviousClose") or {}).get("raw")
            change = (price_info.get("regularMarketChange") or {}).get("raw")
            if price is not None or prev_close is not None or change is not None:
                triple = (price, prev_close, change)
                _cache_set(ticker, triple)
                _db_set_quote(ticker, triple)
                return triple
        except requests.HTTPError as e:
            if getattr(e.response, "status_code", None) == 429:
                print(f"[DEBUG] summary 429 for {ticker}; entering cooldown")
                _set_rate_limit_cooldown()
                if cached is not None:
                    return cached
                dbq = _db_get_quote(ticker)
                if dbq is not None:
                    return dbq
            else:
                print(f"[DEBUG] summary error for {ticker}: {e}")
            if cached is not None:
                return cached
            dbq = _db_get_quote(ticker)
            if dbq is not None:
                return dbq
        except Exception as e:
            print(f"[DEBUG] summary error for {ticker}: {e}")
            if cached is not None:
                return cached
            dbq = _db_get_quote(ticker)
            if dbq is not None:
                return dbq

    # Public Yahoo fallback
    try:
        url = "https://query1.finance.yahoo.com/v7/finance/quote"
        params = {"symbols": ticker}
        resp = requests.get(
            url,
            params=params,
            headers={"User-Agent": "Mozilla/5.0"},
            timeout=8,
        )
        resp.raise_for_status()
        data = resp.json()
        print(f"[DEBUG] public quote for {ticker}: {data}")

        results = data.get("quoteResponse", {}).get("result", [])
        if results:
            r = results[0]
            triple = (
                r.get("regularMarketPrice"),
                r.get("regularMarketPreviousClose"),
                r.get("regularMarketChange"),
            )
            _cache_set(ticker, triple)
            _db_set_quote(ticker, triple)
            return triple
    except requests.HTTPError as e:
        if getattr(e.response, "status_code", None) == 429:
            print(f"[DEBUG] public 429 for {ticker}; entering cooldown")
            _set_rate_limit_cooldown()
            if cached is not None:
                return cached
            dbq = _db_get_quote(ticker)
            if dbq is not None:
                return dbq
        else:
            print(f"[DEBUG] public quote error for {ticker}: {e}")
        if cached is not None:
            return cached
        dbq = _db_get_quote(ticker)
        if dbq is not None:
            return dbq
    except Exception as e:
        print(f"[DEBUG] public quote error for {ticker}: {e}")
        if cached is not None:
            return cached
        dbq = _db_get_quote(ticker)
        if dbq is not None:
            return dbq

    return None, None, None


def calculate_portfolio_summary(portfolio: dict) -> dict:
    """
    Compute summary statistics for a single portfolio:
      cash_balance (float),
      positions_value (float) — open holdings only,
      net_worth (float) = cash + positions_value,
      total_profit (float) since purchase,
      daily_profit (float) since previous close.
    """
    unsold_val = False if DB_IS_POSTGRES else 0
    with engine.connect() as conn:
        holdings = (
            conn.execute(
                text("SELECT * FROM holdings WHERE portfolio_id = :pid AND sold = :sold"),
                {"pid": portfolio["id"], "sold": unsold_val},
            )
            .mappings()
            .all()
        )

    # Batch fetch quotes once for all tickers in this portfolio
    tickers = [h["ticker"] for h in holdings]
    quotes_map = fetch_quotes_batch(tickers)

    positions_value = 0.0
    total_profit = 0.0
    daily_profit = 0.0

    for h in holdings:
        ticker = h["ticker"]
        quantity = float(h["quantity"])
        purchase_price = float(h["purchase_price"])
        current_price, prev_close, change = quotes_map.get(ticker, (None, None, None))

        if current_price is not None:
            effective_price = float(current_price)
        elif prev_close is not None:
            effective_price = float(prev_close)
        else:
            effective_price = purchase_price

        position_value = effective_price * quantity
        positions_value += position_value
        total_profit += (effective_price - purchase_price) * quantity

        if change is not None:
            daily_profit += float(change) * quantity
        elif current_price is not None and prev_close is not None:
            daily_profit += (float(current_price) - float(prev_close)) * quantity

    cash_val = portfolio.get("cash_balance")
    cash_float = float(cash_val) if cash_val is not None else 0.0
    net_worth = cash_float + positions_value

    return {
        "id": portfolio["id"],
        "name": portfolio["name"],
        "cash_balance": cash_float,
        "positions_value": positions_value,
        "net_worth": net_worth,
        "total_profit": total_profit,
        "daily_profit": daily_profit,
    }


@app.route("/")
def index():
    """Dashboard with summaries of all portfolios."""
    with engine.connect() as conn:
        portfolios = conn.execute(text("SELECT * FROM portfolios")).mappings().all()
    summaries = [calculate_portfolio_summary(p) for p in portfolios]
    return render_template("index.html", portfolios=summaries)


@app.route("/healthz")
def healthz():
    # Cheap health endpoint for uptime monitors — no DB or API calls
    return "ok", 200


@app.route("/portfolio/<int:portfolio_id>")
def view_portfolio(portfolio_id: int):
    """Display a single portfolio with holdings and actions."""
    unsold_val = False if DB_IS_POSTGRES else 0
    sold_val = True if DB_IS_POSTGRES else 1

    with engine.connect() as conn:
        portfolio = (
            conn.execute(
                text("SELECT * FROM portfolios WHERE id = :pid"),
                {"pid": portfolio_id},
            )
            .mappings()
            .first()
        )
        if portfolio is None:
            flash("Portfolio not found.", "danger")
            return redirect(url_for("index"))

        holdings = (
            conn.execute(
                text("SELECT * FROM holdings WHERE portfolio_id = :pid AND sold = :sold"),
                {"pid": portfolio_id, "sold": unsold_val},
            )
            .mappings()
            .all()
        )
        sold_holdings = (
            conn.execute(
                text("SELECT * FROM holdings WHERE portfolio_id = :pid AND sold = :sold"),
                {"pid": portfolio_id, "sold": sold_val},
            )
            .mappings()
            .all()
        )

    # Batch fetch quotes for visible positions
    tickers = [h["ticker"] for h in holdings]
    quotes_map = fetch_quotes_batch(tickers)

    holding_rows = []
    for h in holdings:
        current_price, prev_close, change = quotes_map.get(h["ticker"], (None, None, None))

        if current_price is not None:
            effective_price = float(current_price)
        elif prev_close is not None:
            effective_price = float(prev_close)
        else:
            effective_price = float(h["purchase_price"])

        qty = float(h["quantity"])
        cost = float(h["purchase_price"])

        metrics = {
            "id": h["id"],
            "ticker": h["ticker"],
            "quantity": h["quantity"],
            "purchase_price": h["purchase_price"],
            "current_price": current_price,
            "prev_close": prev_close,
            "value": effective_price * qty,
            "profit_total": (effective_price - cost) * qty,
            "profit_daily": None,
        }

        if change is not None:
            metrics["profit_daily"] = float(change) * qty
        elif current_price is not None and prev_close is not None:
            metrics["profit_daily"] = (float(current_price) - float(prev_close)) * qty

        holding_rows.append(metrics)

    summary = calculate_portfolio_summary(portfolio)
    return render_template(
        "portfolio.html",
        portfolio=portfolio,
        holdings=holding_rows,
        summary=summary,
        sold_holdings=sold_holdings,
    )


@app.route("/create_portfolio", methods=["POST"])
def create_portfolio():
    """Create a new portfolio."""
    name = request.form.get("name", "").strip()
    if not name:
        flash("Portfolio name is required.", "danger")
        return redirect(url_for("index"))

    with engine.begin() as conn:
        conn.execute(text("INSERT INTO portfolios (name) VALUES (:name)"), {"name": name})

    flash(f"Portfolio '{name}' created successfully.", "success")
    return redirect(url_for("index"))


@app.route("/portfolio/<int:portfolio_id>/add_holding", methods=["POST"])
def add_holding(portfolio_id: int):
    """Add a new holding and deduct purchase cost from cash."""
    ticker = request.form.get("ticker", "").strip().upper()
    quantity = request.form.get("quantity")
    price = request.form.get("purchase_price")

    try:
        quantity_val = float(quantity)
        price_val = float(price)
        if quantity_val <= 0 or price_val <= 0:
            raise ValueError
    except Exception:
        flash("Quantity and purchase price must be positive numbers.", "danger")
        return redirect(url_for("view_portfolio", portfolio_id=portfolio_id))

    if not ticker:
        flash("Ticker code is required.", "danger")
        return redirect(url_for("view_portfolio", portfolio_id=portfolio_id))

    if "." not in ticker:
        ticker = f"{ticker}.AX"

    purchase_value = quantity_val * price_val

    with engine.begin() as conn:
        portfolio = (
            conn.execute(
                text("SELECT cash_balance FROM portfolios WHERE id = :pid"),
                {"pid": portfolio_id},
            )
            .mappings()
            .first()
        )
        if portfolio is None:
            flash("Portfolio not found.", "danger")
            return redirect(url_for("view_portfolio", portfolio_id=portfolio_id))

        cash = portfolio["cash_balance"]
        cash_float = float(cash) if cash is not None else 0.0
        new_balance = cash_float - purchase_value

        conn.execute(
            text(
                "INSERT INTO holdings (portfolio_id, ticker, quantity, purchase_price) "
                "VALUES (:pid, :ticker, :qty, :price)"
            ),
            {"pid": portfolio_id, "ticker": ticker, "qty": quantity_val, "price": price_val},
        )
        conn.execute(
            text("UPDATE portfolios SET cash_balance = :bal WHERE id = :pid"),
            {"bal": new_balance, "pid": portfolio_id},
        )

    flash(
        f"Added {quantity_val} units of {ticker}. Cash decreased by A${purchase_value:.2f}.",
        "success",
    )
    return redirect(url_for("view_portfolio", portfolio_id=portfolio_id))


@app.route("/portfolio/<int:portfolio_id>/sell_holding/<int:holding_id>", methods=["POST"])
def sell_holding(portfolio_id: int, holding_id: int):
    """Mark a holding as sold and credit proceeds to cash."""
    sold_val = True if DB_IS_POSTGRES else 1

    with engine.begin() as conn:
        holding = (
            conn.execute(
                text("SELECT * FROM holdings WHERE id = :hid AND portfolio_id = :pid"),
                {"hid": holding_id, "pid": portfolio_id},
            )
            .mappings()
            .first()
        )
        if not holding:
            flash("Holding not found.", "danger")
            return redirect(url_for("view_portfolio", portfolio_id=portfolio_id))
        if holding["sold"]:
            flash("This holding has already been sold.", "warning")
            return redirect(url_for("view_portfolio", portfolio_id=portfolio_id))

        current_price, _, _ = get_stock_price(holding["ticker"])
        sale_price = float(current_price) if current_price is not None else float(holding["purchase_price"])
        proceeds = sale_price * float(holding["quantity"])

        portfolio = (
            conn.execute(
                text("SELECT * FROM portfolios WHERE id = :pid"),
                {"pid": portfolio_id},
            )
            .mappings()
            .first()
        )
        cash = portfolio["cash_balance"]
        cash_float = float(cash) if cash is not None else 0.0
        new_balance = cash_float + proceeds

        conn.execute(
            text("UPDATE portfolios SET cash_balance = :bal WHERE id = :pid"),
            {"bal": new_balance, "pid": portfolio_id},
        )
        conn.execute(
            text("UPDATE holdings SET sold = :sold WHERE id = :hid"),
            {"sold": sold_val, "hid": holding_id},
        )

    flash(
        f"Sold {holding['quantity']} {holding['ticker']} at A${sale_price:.2f}. Proceeds credited.",
        "success",
    )
    return redirect(url_for("view_portfolio", portfolio_id=portfolio_id))


@app.route("/portfolio/<int:portfolio_id>/update_cash", methods=["POST"])
def update_cash(portfolio_id: int):
    """Manually update the cash balance."""
    new_balance = request.form.get("cash_balance")
    try:
        balance_val = float(new_balance)
    except Exception:
        flash("Cash balance must be a number.", "danger")
        return redirect(url_for("view_portfolio", portfolio_id=portfolio_id))

    with engine.begin() as conn:
        conn.execute(
            text("UPDATE portfolios SET cash_balance = :bal WHERE id = :pid"),
            {"bal": balance_val, "pid": portfolio_id},
        )

    flash("Cash balance updated.", "success")
    return redirect(url_for("view_portfolio", portfolio_id=portfolio_id))


@app.route("/portfolio/<int:portfolio_id>/delete", methods=["POST"])
def delete_portfolio(portfolio_id: int):
    """Delete a portfolio and all its holdings."""
    with engine.begin() as conn:
        conn.execute(text("DELETE FROM holdings WHERE portfolio_id = :pid"), {"pid": portfolio_id})
        conn.execute(text("DELETE FROM portfolios WHERE id = :pid"), {"pid": portfolio_id})
    flash("Portfolio deleted.", "success")
    return redirect(url_for("index"))


if __name__ == "__main__":
    app.run(debug=True)
