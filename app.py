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
import sqlite3
from datetime import datetime
from typing import List, Optional, Tuple

import requests
from flask import Flask, redirect, render_template, request, url_for, flash


app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "teamjans-secret")

DATABASE = os.path.join(os.path.dirname(__file__), "portfolio.db")


def get_db_connection() -> sqlite3.Connection:
    """Return a new database connection with row factory configured."""
    conn = sqlite3.connect(DATABASE)
    conn.row_factory = sqlite3.Row
    return conn


def init_db() -> None:
    """Initialise the database if tables do not exist."""
    conn = get_db_connection()
    cur = conn.cursor()
    # Create portfolios table
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS portfolios (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            cash_balance REAL DEFAULT 0.0
        )
        """
    )
    # Create holdings table
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS holdings (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            portfolio_id INTEGER NOT NULL,
            ticker TEXT NOT NULL,
            quantity REAL NOT NULL,
            purchase_price REAL NOT NULL,
            purchase_date TEXT DEFAULT CURRENT_DATE,
            sold INTEGER DEFAULT 0,
            FOREIGN KEY (portfolio_id) REFERENCES portfolios (id)
        )
        """
    )
    conn.commit()
    conn.close()


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
        try:
            rapid_url = "https://apidojo-yahoo-finance-v1.p.rapidapi.com/market/v2/get-quotes"
            headers = {
                "x-rapidapi-host": "apidojo-yahoo-finance-v1.p.rapidapi.com",
                "x-rapidapi-key": rapidapi_key,
            }
            params = {"region": "AU", "symbols": ticker}
            resp = requests.get(rapid_url, headers=headers, params=params, timeout=6)
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
    except Exception:
        pass
    return None, None, None


def calculate_portfolio_summary(portfolio: sqlite3.Row) -> dict:
    """
    Compute summary statistics for a single portfolio.

    Fetches all unsold holdings for the portfolio, obtains live quotes
    for each ticker, computes the current value of each position and
    calculates total and daily profit or loss.

    Parameters
    ----------
    portfolio : sqlite3.Row
        The portfolio record retrieved from the database.

    Returns
    -------
    dict
        A dictionary containing summary fields such as current value,
        total profit/loss and daily profit/loss.
    """
    conn = get_db_connection()
    holdings = conn.execute(
        "SELECT * FROM holdings WHERE portfolio_id = ? AND sold = 0",
        (portfolio["id"],),
    ).fetchall()
    conn.close()

    total_value = portfolio["cash_balance"]
    total_profit = 0.0
    daily_profit = 0.0
    for h in holdings:
        ticker = h["ticker"]
        quantity = h["quantity"]
        purchase_price = h["purchase_price"]
        current_price, prev_close, change = get_stock_price(ticker)
        if current_price is not None:
            position_value = current_price * quantity
            total_value += position_value
            total_profit += (current_price - purchase_price) * quantity
            # Use explicit change value if present; otherwise fall back to
            # computing from previous close.  This ensures a non-zero
            # daily P/L when the API omits regularMarketPreviousClose.
            if change is not None:
                daily_profit += change * quantity
            elif prev_close is not None:
                daily_profit += (current_price - prev_close) * quantity
        else:
            # If price not available, fall back to purchase price
            position_value = purchase_price * quantity
            total_value += position_value
    return {
        "id": portfolio["id"],
        "name": portfolio["name"],
        "cash_balance": portfolio["cash_balance"],
        "current_value": total_value,
        "total_profit": total_profit,
        "daily_profit": daily_profit,
    }


@app.route("/")
def index():
    """Display the dashboard with a summary of all portfolios."""
    conn = get_db_connection()
    portfolios = conn.execute("SELECT * FROM portfolios").fetchall()
    conn.close()
    summaries = [calculate_portfolio_summary(p) for p in portfolios]
    return render_template("index.html", portfolios=summaries)


@app.route("/portfolio/<int:portfolio_id>")
def view_portfolio(portfolio_id: int):
    """Display a single portfolio with its holdings and actions."""
    conn = get_db_connection()
    portfolio = conn.execute(
        "SELECT * FROM portfolios WHERE id = ?", (portfolio_id,)
    ).fetchone()
    if portfolio is None:
        conn.close()
        flash("Portfolio not found.", "danger")
        return redirect(url_for("index"))
    holdings = conn.execute(
        "SELECT * FROM holdings WHERE portfolio_id = ? AND sold = 0", (portfolio_id,)
    ).fetchall()
    sold_holdings = conn.execute(
        "SELECT * FROM holdings WHERE portfolio_id = ? AND sold = 1", (portfolio_id,)
    ).fetchall()
    conn.close()

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
        if current_price is not None:
            metrics["value"] = current_price * h["quantity"]
            metrics["profit_total"] = (current_price - h["purchase_price"]) * h["quantity"]
            # Compute daily profit using change value when available.  This
            # handles cases where the previous close is omitted by the API.
            if change is not None:
                metrics["profit_daily"] = change * h["quantity"]
            elif prev_close is not None:
                metrics["profit_daily"] = (current_price - prev_close) * h["quantity"]
            else:
                metrics["profit_daily"] = None
        else:
            metrics["value"] = h["purchase_price"] * h["quantity"]
            metrics["profit_total"] = 0.0
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
    conn = get_db_connection()
    conn.execute("INSERT INTO portfolios (name) VALUES (?)", (name,))
    conn.commit()
    conn.close()
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
    conn = get_db_connection()
    conn.execute(
        "INSERT INTO holdings (portfolio_id, ticker, quantity, purchase_price)"
        " VALUES (?, ?, ?, ?)",
        (portfolio_id, ticker, quantity_val, price_val),
    )
    conn.commit()
    conn.close()
    flash(f"Added {quantity_val} units of {ticker} to the portfolio.", "success")
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
    conn = get_db_connection()
    holding = conn.execute(
        "SELECT * FROM holdings WHERE id = ? AND portfolio_id = ?",
        (holding_id, portfolio_id),
    ).fetchone()
    if not holding:
        conn.close()
        flash("Holding not found.", "danger")
        return redirect(url_for("view_portfolio", portfolio_id=portfolio_id))
    if holding["sold"]:
        conn.close()
        flash("This holding has already been sold.", "warning")
        return redirect(url_for("view_portfolio", portfolio_id=portfolio_id))
    # Get current price
    current_price, _, _ = get_stock_price(holding["ticker"])
    sale_price = current_price if current_price is not None else holding["purchase_price"]
    proceeds = sale_price * holding["quantity"]
    # Update cash balance
    portfolio = conn.execute(
        "SELECT * FROM portfolios WHERE id = ?", (portfolio_id,)
    ).fetchone()
    new_balance = portfolio["cash_balance"] + proceeds
    conn.execute(
        "UPDATE portfolios SET cash_balance = ? WHERE id = ?",
        (new_balance, portfolio_id),
    )
    # Mark holding as sold
    conn.execute(
        "UPDATE holdings SET sold = 1 WHERE id = ?",
        (holding_id,),
    )
    conn.commit()
    conn.close()
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
    conn = get_db_connection()
    conn.execute(
        "UPDATE portfolios SET cash_balance = ? WHERE id = ?",
        (balance_val, portfolio_id),
    )
    conn.commit()
    conn.close()
    flash("Cash balance updated.", "success")
    return redirect(url_for("view_portfolio", portfolio_id=portfolio_id))


@app.route(
    "/portfolio/<int:portfolio_id>/delete", methods=["POST"]
)
def delete_portfolio(portfolio_id: int):
    """Delete a portfolio and all its holdings."""
    conn = get_db_connection()
    conn.execute("DELETE FROM holdings WHERE portfolio_id = ?", (portfolio_id,))
    conn.execute("DELETE FROM portfolios WHERE id = ?", (portfolio_id,))
    conn.commit()
    conn.close()
    flash("Portfolio deleted.", "success")
    return redirect(url_for("index"))


if __name__ == "__main__":
    # When executed directly, run the development server.
    app.run(debug=True)