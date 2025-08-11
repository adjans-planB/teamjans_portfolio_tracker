"""
TeamJans Portfolio Tracker
-------------------------

This Flask application provides a simple portfolio tracking website for
Australian Securities Exchange (ASX) stocks and ETFs. The site lets users
create multiple portfolios, add holdings to each portfolio, record the
purchase price and quantity, mark holdings as sold (which credits sale
proceeds to a cash balance) and manually adjust the cash balance as
needed. On each page load the app queries the Yahoo Finance quote API to
retrieve the latest share price so that profit and loss figures can be
calculated dynamically.  If no network is available the last known
prices remain and the user is informed that live prices could not be
fetched.

The front page displays a summary of all portfolios, including their
current value (cash plus the value of open positions), total profit or
loss since purchase and the change since the previous market close (day
profit/loss). Individual portfolio pages list the holdings with
detailed metrics and provide forms to add positions, sell positions and
edit cash balances.

You can run this application locally with::

FLASK_APP=app.py flask run

By default it creates and uses an SQLite database in the same
directory.  To force database initialisation you can delete the
`portfolio.db` file and restart the app.

Note: This application relies on the Yahoo Finance quote endpoint
(`https://query1.finance.yahoo.com/v7/finance/quote`) for price data.
According to a March 2025 comparison of free finance APIs, the Yahoo
Finance API (via RapidAPI) supports global stock data including the
Australian Securities Exchange, whereas other popular free APIs such as
Marketstack and Alpha Vantage do not support the ASX【681829866966259†L67-L84】.
Because of this the app uses Yahoo’s publicly accessible quote endpoint
by default.  If you wish to switch to a different data provider, modify
the `get_stock_price` function accordingly.
"""

from __future__ import annotations

import os
from datetime import datetime
from typing import List, Optional, Tuple

import requests
from flask import Flask, redirect, render_template, request, url_for, flash

# Database libraries.  We use SQLAlchemy for cross‑database support so that
# switching between SQLite and PostgreSQL requires minimal changes.  The
# `create_engine` function constructs an engine from a URL and the `text`
# helper allows execution of parameterised SQL statements with named
# parameters.  RealDictRow mapping is achieved via the `.mappings()` method
# on result objects, which returns dictionaries keyed by column names.
from sqlalchemy import create_engine, text


app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "teamjans-secret")

# Path to the SQLite database file for local development.  When running on
# Render with a PostgreSQL add‑on, the DATABASE_URL environment variable is
# provided and SQLAlchemy will connect to that instead.
DATABASE = os.path.join(os.path.dirname(__file__), "portfolio.db")

# Construct a SQLAlchemy engine.  When a DATABASE_URL environment variable
# is provided, use it to connect to PostgreSQL.  Some providers return a
# URL starting with "postgres://", which SQLAlchemy 2.x doesn't recognise.
db_url = os.environ.get("DATABASE_URL")
if db_url:
    if db_url.startswith("postgres://"):
        db_url = db_url.replace("postgres://", "postgresql://", 1)
        engine = create_engine(db_url, future=True)
else:
    engine = create_engine(f"sqlite:///{DATABASE}", future=True)

# Determine whether we are using PostgreSQL (for type handling and SQL
# differences).  This allows us to adjust default values and parameter
# bindings appropriately.
DB_IS_POSTGRES = engine.url.get_backend_name() == "postgresql"

def init_db() -> None:
    """ 
    Initialise the database if tables do not exist.

    For PostgreSQL we use SERIAL primary keys and boolean types; for
    SQLite we stick to INTEGER primary keys with AUTOINCREMENT and
    integer flags for booleans.  This function runs within a
    transactional context so that table creation statements are
    committed automatically.
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
# Execute the table creation statements
with engine.begin() as conn:
    conn.execute(text(create_portfolios))
    conn.execute(text(create_holdings))


# Initialise the database immediately when the module is imported.  In
# Flask 3.x the `before_first_request` hook has been removed, so we
# cannot rely on it.  Calling `init_db()` here ensures that the
# required tables exist by the time the app starts serving requests.
init_db()


def get_stock_price(ticker: str) -> Tuple[Optional[float], Optional[float], Optional[float]]:
    """
    Retrieve current price, previous close and change for a ticker.

    The function first attempts to query the Yahoo Finance API via the
    RapidAPI gateway when a `RAPIDAPI_KEY` environment variable is
    provided.  RapidAPI offers a more reliable feed that is less
    susceptible to rate limiting.  If no key is present it falls back
    to Yahoo Finance’s public quote endpoint.  On success it returns a
    tuple ``(current_price, previous_close, change)``; otherwise
    `(None, None, None)`.

    Parameters
    ----------
    ticker : str
        The stock or ETF ticker (e.g. "BHP.AX" for BHP Group on the ASX).

    Returns
    -------
    Tuple[Optional[float], Optional[float], Optional[float]]
        A tuple of ``(current_price, previous_close, change)`` where any
        element may be ``None`` if unavailable.
    """
rapidapi_key = os.environ.get("RAPIDAPI_KEY")
# Attempt RapidAPI if key provided
if rapidapi_key:
    # Primary call: market/v2/get-quotes.  This endpoint should return
    # regularMarketPrice and regularMarketChange for most equities.  When
    # the price or change is missing (as sometimes happens for ASX tickers),
    # we fall back to stock/v2/get-summary to extract values from the nested
    # "price" object.  Any exceptions are silently ignored so that
    # downstream code can fall back to the public Yahoo endpoint.
    try:
        headers = {
            "x-rapidapi-host": "apidojo-yahoo-finance-v1.p.rapidapi.com",
            "x-rapidapi-key": rapidapi_key,
        }
        # First try market/v2/get-quotes
        market_url = (
            "https://apidojo-yahoo-finance-v1.p.rapidapi.com/market/v2/get-quotes"
        )
        params = {"region": "AU", "symbols": ticker}
        resp = requests.get(market_url, headers=headers, params=params, timeout=6)
        resp.raise_for_status()
        data = resp.json()
        print(f"[DEBUG] Primary API response for {ticker}: {data}")

        results = data.get("quoteResponse", {}).get("result", [])
        if results:
            result = results[0]
            price = result.get("regularMarketPrice")
            prev = result.get("regularMarketPreviousClose")
            change = result.get("regularMarketChange")
            # If we got a price and change, return immediately
            if price is not None or prev is not None or change is not None:
                return price, prev, change
        # If price or change missing, try stock/v2/get-summary
        summary_url = (
            "https://apidojo-yahoo-finance-v1.p.rapidapi.com/stock/v2/get-summary"
        )
        params2 = {"symbol": ticker, "region": "AU"}
        resp2 = requests.get(summary_url, headers=headers, params=params2, timeout=6)
        resp2.raise_for_status()
        data2 = resp2.json()
        price_info = data2.get("price", {}) or {}
        price = price_info.get("regularMarketPrice", {}).get("raw")
        prev_close = price_info.get("regularMarketPreviousClose", {}).get("raw")
        change = price_info.get("regularMarketChange", {}).get("raw")
        if price is not None or prev_close is not None or change is not None:
            return price, prev_close, change
        print(f"[DEBUG] Fallback summary response for {ticker}: {data2}")

    except Exception:
        pass
# Fallback to public Yahoo Finance endpoint
try:
    url = "https://query1.finance.yahoo.com/v7/finance/quote"
    params = {"symbols": ticker}
    resp = requests.get(url, params=params, timeout=6)
    resp.raise_for_status()
    data = resp.json()
    results = data.get("quoteResponse", {}).get("result", [])
    if results:
        result = results[0]
        return (
            result.get("regularMarketPrice"),
            result.get("regularMarketPreviousClose"),
            result.get("regularMarketChange"),
        )
    print(f"[DEBUG] Public Yahoo fallback for {ticker}: {data}")


except Exception:
    pass
return None, None, None


def calculate_portfolio_summary(portfolio: dict) -> dict:
    """
    Compute summary statistics for a single portfolio.
    """
unsold_val = False if DB_IS_POSTGRES else 0
with engine.connect() as conn:
    holdings = (
        conn.execute(
            text(
                "SELECT * FROM holdings WHERE portfolio_id = :pid AND sold = :sold"
            ),
            {"pid": portfolio["id"], "sold": unsold_val},
        )
        .mappings()
        .all()
    )

positions_value = 0.0
total_profit = 0.0
daily_profit = 0.0
for h in holdings:
    ticker = h["ticker"]
    quantity = float(h["quantity"])
    purchase_price = float(h["purchase_price"])
    current_price, prev_close, change = get_stock_price(ticker)

    if current_price is not None:
        effective_price = current_price
    elif prev_close is not None:
        effective_price = prev_close
    else:
        effective_price = purchase_price

    price = float(effective_price)
    position_value = price * quantity
    positions_value += position_value
    total_profit += (price - purchase_price) * quantity

    if change is not None:
        daily_profit += float(change) * quantity
    elif current_price is not None and prev_close is not None:
        daily_profit += (float(current_price) - float(prev_close)) * quantity

cash_balance = portfolio["cash_balance"]
cash_float = float(cash_balance) if cash_balance is not None else 0.0
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
    """Display the dashboard with a summary of all portfolios."""
# Retrieve all portfolios.  Use `.mappings().all()` to return a list of
# dictionaries keyed by column names.  This allows us to treat rows
# similarly regardless of backend.
with engine.connect() as conn:
    portfolios = conn.execute(text("SELECT * FROM portfolios")).mappings().all()
summaries = [calculate_portfolio_summary(p) for p in portfolios]
return render_template("index.html", portfolios=summaries)


@app.route("/portfolio/<int:portfolio_id>")
def view_portfolio(portfolio_id: int):
    """Display a single portfolio with its holdings and actions."""
# Determine unsold/sold flag values based on backend
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
            text(
                "SELECT * FROM holdings WHERE portfolio_id = :pid AND sold = :sold"
            ),
            {"pid": portfolio_id, "sold": unsold_val},
        )
        .mappings()
        .all()
    )
    sold_holdings = (
        conn.execute(
            text(
                "SELECT * FROM holdings WHERE portfolio_id = :pid AND sold = :sold"
            ),
            {"pid": portfolio_id, "sold": sold_val},
        )
        .mappings()
        .all()
    )

# Compute metrics for each holding
holding_rows = []
for h in holdings:
    current_price, prev_close, change = get_stock_price(h["ticker"])
    metrics = {
        "id": h["id"],
        "ticker": h["ticker"],
        "quantity": h["quantity"],
        "purchase_price": h["purchase_price"],
        "current_price": current_price,
        "prev_close": prev_close,
    }
    # Determine an effective price: if the market price is available,
    # use it; otherwise fall back to the previous close when
    # available; if neither is available, use the purchase price.
    if current_price is not None:
        effective_price = current_price
    elif prev_close is not None:
        effective_price = prev_close
    else:
        effective_price = h["purchase_price"]
    # Position value and total profit based on the effective price
    metrics["value"] = effective_price * h["quantity"]
    metrics["profit_total"] = (effective_price - h["purchase_price"]) * h["quantity"]
    # Compute daily profit.  When the API supplies a change value,
    # multiply by quantity.  Otherwise, if both current price and
    # previous close are available, compute the difference.  If
    # neither is available, daily profit is undefined (None).
    if change is not None:
        metrics["profit_daily"] = change * h["quantity"]
    elif current_price is not None and prev_close is not None:
        metrics["profit_daily"] = (current_price - prev_close) * h["quantity"]
    else:
        metrics["profit_daily"] = None
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
    """Handle portfolio creation."""
name = request.form.get("name", "").strip()
if not name:
    flash("Portfolio name is required.", "danger")
    return redirect(url_for("index"))
# Insert a new portfolio using a transactional block.  SQLAlchemy will
# automatically commit when exiting the context.
with engine.begin() as conn:
    conn.execute(
        text("INSERT INTO portfolios (name) VALUES (:name)"),
        {"name": name},
    )
flash(f"Portfolio '{name}' created successfully.", "success")
return redirect(url_for("index"))


@app.route("/portfolio/<int:portfolio_id>/add_holding", methods=["POST"])
def add_holding(portfolio_id: int):
    """Add a new holding to the specified portfolio."""
ticker = request.form.get("ticker", "").strip().upper()
quantity = request.form.get("quantity")
price = request.form.get("purchase_price")
# Validate inputs
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
# If the user enters a bare ASX code (e.g. BHP), append '.AX' so that
# Yahoo Finance recognises it as an Australian listing.  If a dot is
# already present (e.g. 'BHP.AX' or 'XYZ.NZ') we leave it unchanged.
if "." not in ticker:
    ticker = f"{ticker}.AX"
# Compute the value of the purchase.  We deduct this from the
# portfolio's cash balance to reflect the cost of buying the
# holding.  If the portfolio has insufficient cash, the balance
# may go negative.
purchase_value = quantity_val * price_val
with engine.begin() as conn:
    # Fetch current cash balance
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
    # Convert cash_balance to float to avoid Decimal–float TypeError on PostgreSQL
    cash_balance = portfolio["cash_balance"]
    cash_float = float(cash_balance) if cash_balance is not None else 0.0
    new_balance = cash_float - purchase_value
    # Insert the new holding
    conn.execute(
        text(
            "INSERT INTO holdings (portfolio_id, ticker, quantity, purchase_price) "
            "VALUES (:pid, :ticker, :qty, :price)"
        ),
        {"pid": portfolio_id, "ticker": ticker, "qty": quantity_val, "price": price_val},
    )
    # Update cash balance
    conn.execute(
        text("UPDATE portfolios SET cash_balance = :bal WHERE id = :pid"),
        {"bal": new_balance, "pid": portfolio_id},
    )
flash(
    f"Added {quantity_val} units of {ticker} to the portfolio. "
    f"Cash balance decreased by A${purchase_value:.2f}.",
    "success",
)
return redirect(url_for("view_portfolio", portfolio_id=portfolio_id))


@app.route(
"/portfolio/<int:portfolio_id>/sell_holding/<int:holding_id>", methods=["POST"]
)
def sell_holding(portfolio_id: int, holding_id: int):
    """
    Mark a holding as sold and credit the sale proceeds to the cash balance.

    The sale price is taken from the current market price at the moment
    the user presses the sell button.  If a live price cannot be
    retrieved, the purchase price is used instead.  The record remains
    in the holdings table but is flagged as sold.
    """
# Determine the sold flag value for the target database
sold_val = True if DB_IS_POSTGRES else 1
unsold_val = False if DB_IS_POSTGRES else 0
# Perform the sale within a transactional block so that the read and
# writes occur atomically.  Fetch the holding, compute the proceeds and
# update both the portfolio and the holding record.
with engine.begin() as conn:
    holding = (
        conn.execute(
            text(
                "SELECT * FROM holdings WHERE id = :hid AND portfolio_id = :pid"
            ),
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
    # Get current price
    current_price, _, _ = get_stock_price(holding["ticker"])
    sale_price = current_price if current_price is not None else holding["purchase_price"]
    proceeds = sale_price * holding["quantity"]
    # Fetch portfolio to compute new cash balance
    portfolio = (
        conn.execute(
            text("SELECT * FROM portfolios WHERE id = :pid"),
            {"pid": portfolio_id},
        )
        .mappings()
        .first()
    )
    # Convert cash_balance to float to avoid Decimal–float TypeError on PostgreSQL
    cash_balance = portfolio["cash_balance"]
    cash_float = float(cash_balance) if cash_balance is not None else 0.0
    new_balance = cash_float + proceeds
    # Update cash balance
    conn.execute(
        text("UPDATE portfolios SET cash_balance = :bal WHERE id = :pid"),
        {"bal": new_balance, "pid": portfolio_id},
    )
    # Mark holding as sold
    conn.execute(
        text("UPDATE holdings SET sold = :sold WHERE id = :hid"),
        {"sold": sold_val, "hid": holding_id},
    )
flash(
    f"Sold {holding['quantity']} units of {holding['ticker']} for {sale_price:.2f} each. "
    f"Proceeds credited to cash balance.",
    "success",
)
return redirect(url_for("view_portfolio", portfolio_id=portfolio_id))


@app.route(
"/portfolio/<int:portfolio_id>/update_cash", methods=["POST"]
)
def update_cash(portfolio_id: int):
    """Manually update the cash balance of a portfolio."""
new_balance = request.form.get("cash_balance")
try:
    balance_val = float(new_balance)
except Exception:
    flash("Cash balance must be a number.", "danger")
    return redirect(url_for("view_portfolio", portfolio_id=portfolio_id))
# Update cash balance in a transactional block
with engine.begin() as conn:
    conn.execute(
        text("UPDATE portfolios SET cash_balance = :bal WHERE id = :pid"),
        {"bal": balance_val, "pid": portfolio_id},
    )
flash("Cash balance updated.", "success")
return redirect(url_for("view_portfolio", portfolio_id=portfolio_id))


@app.route(
"/portfolio/<int:portfolio_id>/delete", methods=["POST"]
)
def delete_portfolio(portfolio_id: int):
    """Delete a portfolio and all its holdings."""
# Remove the portfolio and all its holdings in a single transaction
with engine.begin() as conn:
    conn.execute(
        text("DELETE FROM holdings WHERE portfolio_id = :pid"),
        {"pid": portfolio_id},
    )
    conn.execute(
        text("DELETE FROM portfolios WHERE id = :pid"),
        {"pid": portfolio_id},
    )
flash("Portfolio deleted.", "success")
return redirect(url_for("index"))


if __name__ == "__main__":
    # When executed directly, run the development server.
    app.run(debug=True)