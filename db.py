"""The environment: a DuckDB database the agent queries but cannot damage.

Two modes, same schema either way:

  REAL   Drop the Kaggle "Brazilian E-Commerce Public Dataset by Olist" CSVs
         into data/olist/ and they are loaded as-is.
  SYNTH  No CSVs? A synthetic dataset with the SAME table and column names is
         generated, so the agent, the eval set and the UI all work today and
         keep working unchanged when the real CSVs arrive.

Tables (8, genuinely relational — joins are the point):
    customers, orders, order_items, products, sellers,
    order_payments, order_reviews, product_category_name_translation
"""

import random
from datetime import datetime, timedelta
from pathlib import Path

import duckdb

ROOT = Path(__file__).resolve().parent
DATA = ROOT / "data"
CSV_DIR = DATA / "olist"
DB_PATH = DATA / "olist.duckdb"

# Kaggle file name -> the shorter table name we expose to the agent.
# Short names cost fewer tokens and read better in generated SQL.
CSV_TABLES = {
    "olist_customers_dataset.csv": "customers",
    "olist_orders_dataset.csv": "orders",
    "olist_order_items_dataset.csv": "order_items",
    "olist_products_dataset.csv": "products",
    "olist_sellers_dataset.csv": "sellers",
    "olist_order_payments_dataset.csv": "order_payments",
    "olist_order_reviews_dataset.csv": "order_reviews",
    "product_category_name_translation.csv": "product_category_name_translation",
}

# ── synthetic-data knobs ────────────────────────────────────────────────────
SEED = 7
N_CUSTOMERS, N_SELLERS, N_PRODUCTS, N_ORDERS = 1200, 60, 200, 3000
START = datetime(2017, 1, 1)
DAYS = 730

STATES = [  # (state, weight) — SP dominates, like the real dataset
    ("SP", 40), ("RJ", 15), ("MG", 12), ("RS", 8),
    ("PR", 8), ("SC", 6), ("BA", 6), ("PE", 5),
]
CITIES = {
    "SP": ["sao paulo", "campinas", "santos"], "RJ": ["rio de janeiro", "niteroi"],
    "MG": ["belo horizonte", "uberlandia"], "RS": ["porto alegre", "caxias do sul"],
    "PR": ["curitiba", "londrina"], "SC": ["florianopolis", "joinville"],
    "BA": ["salvador", "feira de santana"], "PE": ["recife", "olinda"],
}
# (portuguese, english, price_low, price_high, review_bias, ship_days_bias)
CATEGORIES = [
    ("cama_mesa_banho",   "bed_bath_table",     20, 180,  0.0, 0),
    ("beleza_saude",      "health_beauty",      15, 220,  0.3, 0),
    ("esporte_lazer",     "sports_leisure",     25, 350,  0.1, 0),
    ("informatica_acessorios", "computers_accessories", 30, 900, -0.2, 1),
    ("moveis_decoracao",  "furniture_decor",    40, 1200, -1.1, 6),  # the bad one
    ("relogios_presentes", "watches_gifts",     50, 800,  0.2, 0),
    ("telefonia",         "telephony",          20, 600, -0.1, 1),
    ("brinquedos",        "toys",               15, 300,  0.4, 0),
]
PAYMENT_TYPES = [("credit_card", 74), ("boleto", 19), ("voucher", 5), ("debit_card", 2)]
STATUSES = [("delivered", 91), ("shipped", 4), ("canceled", 3), ("processing", 2)]


def _pick(weighted, rng):
    total = sum(w for _, w in weighted)
    r = rng.uniform(0, total)
    upto = 0
    for value, w in weighted:
        upto += w
        if r <= upto:
            return value
    return weighted[-1][0]


def _generate(con: duckdb.DuckDBPyConnection) -> None:
    """Build a synthetic Olist-shaped dataset. Deterministic (seeded), so the
    gold answers in the eval set stay valid across rebuilds."""
    rng = random.Random(SEED)
    uid = lambda p, i: f"{p}_{i:06d}"  # noqa: E731

    # customers
    customers = []
    for i in range(N_CUSTOMERS):
        st = _pick(STATES, rng)
        customers.append((uid("cust", i), uid("uniq", i % (N_CUSTOMERS - 100)),
                          rng.randint(1000, 99999), rng.choice(CITIES[st]), st))

    # sellers
    sellers = []
    for i in range(N_SELLERS):
        st = _pick(STATES, rng)
        sellers.append((uid("sell", i), rng.randint(1000, 99999),
                        rng.choice(CITIES[st]), st))

    # products
    products, prod_meta = [], []
    for i in range(N_PRODUCTS):
        pt, _en, lo, hi, rbias, sbias = CATEGORIES[i % len(CATEGORIES)]
        price = round(rng.uniform(lo, hi), 2)
        products.append((uid("prod", i), pt, rng.randint(20, 60), rng.randint(100, 3000),
                         rng.randint(1, 6), rng.randint(100, 20000),
                         rng.randint(10, 100), rng.randint(5, 60), rng.randint(5, 60)))
        prod_meta.append((uid("prod", i), price, rbias, sbias))

    # orders / items / payments / reviews
    orders, items, payments, reviews = [], [], [], []
    for i in range(N_ORDERS):
        oid, cid = uid("order", i), uid("cust", rng.randrange(N_CUSTOMERS))
        status = _pick(STATUSES, rng)
        purchase = START + timedelta(days=rng.randrange(DAYS),
                                     hours=rng.randrange(24), minutes=rng.randrange(60))
        approved = purchase + timedelta(hours=rng.randint(1, 30))
        estimated = purchase + timedelta(days=rng.randint(10, 30))

        n_items = rng.choices([1, 2, 3], weights=[70, 22, 8])[0]
        ship_bias, review_bias = 0, 0.0
        order_total = 0.0
        for k in range(n_items):
            pid, price, rbias, sbias = prod_meta[rng.randrange(N_PRODUCTS)]
            freight = round(rng.uniform(5, 40), 2)
            ship_bias = max(ship_bias, sbias)
            review_bias = min(review_bias, rbias) if rbias < 0 else review_bias
            order_total += price + freight
            items.append((oid, k + 1, pid, uid("sell", rng.randrange(N_SELLERS)),
                          (approved + timedelta(days=3)).strftime("%Y-%m-%d %H:%M:%S"),
                          price, freight))

        carrier = delivered = None
        if status in ("delivered", "shipped"):
            carrier = approved + timedelta(days=rng.randint(1, 4))
        if status == "delivered":
            # most arrive early; the biased category runs late
            offset = rng.randint(-12, 3) + ship_bias + rng.choice([0, 0, 0, 4])
            delivered = estimated + timedelta(days=offset, hours=rng.randrange(24))

        fmt = lambda d: d.strftime("%Y-%m-%d %H:%M:%S") if d else None  # noqa: E731
        orders.append((oid, cid, status, fmt(purchase), fmt(approved),
                       fmt(carrier), fmt(delivered), fmt(estimated)))

        ptype = _pick(PAYMENT_TYPES, rng)
        inst = rng.randint(1, 10) if ptype == "credit_card" else 1
        payments.append((oid, 1, ptype, inst, round(order_total, 2)))

        if status == "delivered" and rng.random() < 0.82:
            # Score reflects lateness and the order's worst category bias, so
            # "which category is worst reviewed / slowest" has a real signal
            # to find rather than pure noise.
            late = delivered and delivered > estimated
            base = rng.choices([5, 4, 3, 2, 1], weights=[57, 19, 8, 6, 10])[0]
            adj = round(review_bias) if rng.random() < 0.6 else 0
            score = max(1, min(5, base + adj - (2 if late else 0)))
            reviews.append((uid("rev", i), oid, score, None, None,
                            fmt(delivered + timedelta(days=1)),
                            fmt(delivered + timedelta(days=2))))

    con.execute("""
        CREATE TABLE customers (customer_id VARCHAR, customer_unique_id VARCHAR,
            customer_zip_code_prefix INTEGER, customer_city VARCHAR, customer_state VARCHAR);
        CREATE TABLE sellers (seller_id VARCHAR, seller_zip_code_prefix INTEGER,
            seller_city VARCHAR, seller_state VARCHAR);
        CREATE TABLE products (product_id VARCHAR, product_category_name VARCHAR,
            product_name_lenght INTEGER, product_description_lenght INTEGER,
            product_photos_qty INTEGER, product_weight_g INTEGER,
            product_length_cm INTEGER, product_height_cm INTEGER, product_width_cm INTEGER);
        CREATE TABLE orders (order_id VARCHAR, customer_id VARCHAR, order_status VARCHAR,
            order_purchase_timestamp TIMESTAMP, order_approved_at TIMESTAMP,
            order_delivered_carrier_date TIMESTAMP, order_delivered_customer_date TIMESTAMP,
            order_estimated_delivery_date TIMESTAMP);
        CREATE TABLE order_items (order_id VARCHAR, order_item_id INTEGER,
            product_id VARCHAR, seller_id VARCHAR, shipping_limit_date TIMESTAMP,
            price DOUBLE, freight_value DOUBLE);
        CREATE TABLE order_payments (order_id VARCHAR, payment_sequential INTEGER,
            payment_type VARCHAR, payment_installments INTEGER, payment_value DOUBLE);
        CREATE TABLE order_reviews (review_id VARCHAR, order_id VARCHAR,
            review_score INTEGER, review_comment_title VARCHAR,
            review_comment_message VARCHAR, review_creation_date TIMESTAMP,
            review_answer_timestamp TIMESTAMP);
        CREATE TABLE product_category_name_translation (
            product_category_name VARCHAR, product_category_name_english VARCHAR);
    """)
    con.executemany("INSERT INTO customers VALUES (?,?,?,?,?)", customers)
    con.executemany("INSERT INTO sellers VALUES (?,?,?,?)", sellers)
    con.executemany("INSERT INTO products VALUES (?,?,?,?,?,?,?,?,?)", products)
    con.executemany("INSERT INTO orders VALUES (?,?,?,?,?,?,?,?)", orders)
    con.executemany("INSERT INTO order_items VALUES (?,?,?,?,?,?,?)", items)
    con.executemany("INSERT INTO order_payments VALUES (?,?,?,?,?)", payments)
    con.executemany("INSERT INTO order_reviews VALUES (?,?,?,?,?,?,?)", reviews)
    con.executemany("INSERT INTO product_category_name_translation VALUES (?,?)",
                    [(pt, en) for pt, en, *_ in CATEGORIES])


def build(force: bool = False) -> str:
    """Create the database if needed. Returns 'real' or 'synthetic'."""
    DATA.mkdir(exist_ok=True)
    csvs = list(CSV_DIR.glob("*.csv")) if CSV_DIR.exists() else []
    mode = "real" if csvs else "synthetic"

    if DB_PATH.exists() and not force:
        return mode
    if DB_PATH.exists():
        DB_PATH.unlink()

    con = duckdb.connect(str(DB_PATH))
    try:
        if csvs:
            for fname, table in CSV_TABLES.items():
                path = CSV_DIR / fname
                if path.exists():
                    con.execute(
                        f"CREATE TABLE {table} AS SELECT * FROM read_csv_auto(?)",
                        [str(path)],
                    )
        else:
            _generate(con)
    finally:
        con.close()
    return mode


def connect() -> duckdb.DuckDBPyConnection:
    """READ-ONLY connection. The agent writes SQL; it must not be able to
    change the data — the database is the oracle we grade against."""
    build()
    return duckdb.connect(str(DB_PATH), read_only=True)


if __name__ == "__main__":
    mode = build(force=True)
    con = connect()
    print(f"built {DB_PATH.name} ({mode} data)")
    for (t,) in con.execute("SHOW TABLES").fetchall():
        n = con.execute(f"SELECT count(*) FROM {t}").fetchone()[0]
        print(f"  {t:<40} {n:>7,} rows")
