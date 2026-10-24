import argparse
import os
from pathlib import Path
from statistics import median

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import pandas as pd
from sqlalchemy import create_engine, text

MIN_PRICE = 100_000_000
MAX_KM = 1_500_000
DB_COLS = ["ad_id","scraped_at","title","query","url","make","model","trim","variant_key",
           "year_jalali","mileage_km","is_zero_km","price_toman","price_valid",
           "price_is_agreed","city","gearbox","fair_price_toman","discount_pct",
           "comparable_count","comparison_basis","underpriced"]


def bool_col(s):
    return s.fillna(False).astype(str).str.strip().str.lower().isin({"true","1","yes","y"})


def load_csv(path):
    df = pd.read_csv(path, encoding="utf-8-sig", low_memory=False)
    required = {"ad_id","variant_key","price_toman","year_jalali","mileage_km"}
    missing = required - set(df.columns)
    if missing:
        raise ValueError("Missing CSV columns: " + ", ".join(sorted(missing)))
    for col in ("price_toman","year_jalali","mileage_km"):
        df[col] = pd.to_numeric(df[col], errors="coerce")
    for col in ("is_zero_km","price_valid","price_is_agreed"):
        if col not in df:
            df[col] = False
        df[col] = bool_col(df[col])
    if "scraped_at" not in df:
        df["scraped_at"] = pd.Timestamp.now(tz="UTC")
    else:
        df["scraped_at"] = pd.to_datetime(df["scraped_at"], utc=True, errors="coerce")
        df["scraped_at"] = df["scraped_at"].fillna(pd.Timestamp.now(tz="UTC"))
    df = df.dropna(subset=["ad_id"]).drop_duplicates("ad_id", keep="last").copy()
    df["price_valid"] &= df.price_toman.ge(MIN_PRICE) & ~df.price_is_agreed & ~df.mileage_km.gt(MAX_KM)
    return df


def estimate_prices(df, min_peers, threshold):
    pool = df.loc[df.price_valid & df.variant_key.notna() & df.year_jalali.notna()]
    groups = {key: group for key, group in pool.groupby("variant_key", sort=False)}
    positions = {idx: pos for pos, idx in enumerate(df.index)}
    fairs, discounts, counts, bases, under = ([None]*len(df),[None]*len(df),[0]*len(df),[None]*len(df),[None]*len(df))
    for idx, item in df.iterrows():
        if not item.price_valid or pd.isna(item.variant_key) or pd.isna(item.year_jalali):
            continue
        peers = groups.get(item.variant_key)
        if peers is None:
            continue
        peers = peers.loc[(peers.ad_id != item.ad_id)
                          & (peers.year_jalali-item.year_jalali).abs().le(1)
                          & peers.is_zero_km.eq(item.is_zero_km)]
        km = item.mileage_km
        if pd.notna(km):
            other_km = peers.mileage_km
            peers = peers.loc[other_km.isna() | (other_km-km).abs().le(max(30_000,float(km)*.30))]
        n=len(peers); pos=positions[idx]; counts[pos]=n
        if n < min_peers:
            continue
        fair=float(median(peers.price_toman.dropna().tolist()))
        pct=(fair-float(item.price_toman))/fair*100 if fair else 0
        fairs[pos], discounts[pos], bases[pos]=round(fair),round(pct,1),"same variant; year ±1; similar mileage"
        under[pos]=float(item.price_toman)<=fair*(1-threshold)
    df["fair_price_toman"]=pd.array(fairs,dtype="Int64")
    df["discount_pct"]=pd.array(discounts,dtype="Float64")
    df["comparable_count"]=pd.array(counts,dtype="Int64")
    df["comparison_basis"]=bases
    df["underpriced"]=pd.array(under,dtype="boolean")
    return df


def charts(df, out):
    out.mkdir(parents=True,exist_ok=True)
    valid=df.loc[df.price_valid & df.price_toman.notna()].copy()
    summary=(valid.groupby("variant_key",dropna=False)
        .agg(listing_count=("ad_id","count"),median_price_toman=("price_toman","median"),
             min_price_toman=("price_toman","min"),max_price_toman=("price_toman","max"))
        .reset_index().sort_values("median_price_toman"))
    summary.to_csv(out/"variant_summary.csv",index=False,encoding="utf-8-sig")
    if not summary.empty:
        fig,ax=plt.subplots(figsize=(11,max(5,.45*len(summary))))
        labels=[f"{r.variant_key} (n={int(r.listing_count)})" for r in summary.itertuples()]
        ax.barh(labels,summary.median_price_toman/1e9,color="#1f78b4")
        ax.set_xlabel("Median asking price (billion Toman)"); ax.set_title("Median price by vehicle variant")
        ax.grid(axis="x",alpha=.25); fig.tight_layout()
        fig.savefig(out/"01_median_price_by_variant.png",dpi=160); plt.close(fig)
    valid["price_billion_toman"]=valid.price_toman/1e9
    scatter=valid.dropna(subset=["mileage_km","price_billion_toman"])
    if not scatter.empty:
        fig,ax=plt.subplots(figsize=(10,6))
        for make,g in scatter.groupby("make",dropna=False):
            ax.scatter(g.mileage_km/1000,g.price_billion_toman,label=str(make or "Unknown"),alpha=.8)
        ax.set_xlabel("Mileage (thousand km)"); ax.set_ylabel("Asking price (billion Toman)")
        ax.set_title("Asking price versus mileage"); ax.grid(alpha=.25); ax.legend(title="Make",fontsize=8)
        fig.tight_layout(); fig.savefig(out/"02_price_vs_mileage.png",dpi=160); plt.close(fig)
    year=valid.dropna(subset=["year_jalali"]).groupby("year_jalali").size().sort_index()
    if not year.empty:
        fig,ax=plt.subplots(figsize=(10,5)); ax.bar(year.index.astype(int).astype(str),year.values,color="#33a02c")
        ax.set_xlabel("Jalali model year"); ax.set_ylabel("Valid-price listing count")
        ax.set_title("Sample count by model year"); ax.grid(axis="y",alpha=.25); fig.tight_layout()
        fig.savefig(out/"03_sample_count_by_year.png",dpi=160); plt.close(fig)
    counts={"Listings":len(df),"Valid prices":int(df.price_valid.sum()),
            "Fair-price estimates":int(df.fair_price_toman.notna().sum()),
            "Underpriced candidates":int(df.underpriced.fillna(False).sum())}
    fig,ax=plt.subplots(figsize=(9,5)); ax.bar(list(counts),list(counts.values()),color=["#6baed6","#31a354","#756bb1","#e6550d"])
    ax.set_ylabel("Listing count"); ax.set_title("Data and estimate coverage"); ax.grid(axis="y",alpha=.25)
    fig.tight_layout(); fig.savefig(out/"04_data_and_estimate_coverage.png",dpi=160); plt.close(fig)
    pd.DataFrame([{"metric":k,"value":v} for k,v in counts.items()]).to_csv(out/"coverage_summary.csv",index=False,encoding="utf-8-sig")


def write_db(df,url):
    for col in DB_COLS:
        if col not in df: df[col]=pd.NA
    engine=create_engine(url,pool_pre_ping=True)
    try:
        df[DB_COLS].to_sql("incoming_listings",engine,if_exists="replace",index=False,method="multi",chunksize=500)
        with engine.begin() as c:
            c.execute(text("""CREATE TABLE IF NOT EXISTS car_listing_snapshots (
              ad_id TEXT NOT NULL,scraped_at TIMESTAMPTZ NOT NULL,title TEXT,query TEXT,url TEXT,
              make TEXT,model TEXT,trim TEXT,variant_key TEXT,year_jalali INTEGER,mileage_km BIGINT,
              is_zero_km BOOLEAN,price_toman BIGINT,price_valid BOOLEAN,price_is_agreed BOOLEAN,
              city TEXT,gearbox TEXT,fair_price_toman BIGINT,discount_pct DOUBLE PRECISION,
              comparable_count INTEGER,comparison_basis TEXT,underpriced BOOLEAN,
              PRIMARY KEY(ad_id,scraped_at))"""))
            c.execute(text("""INSERT INTO car_listing_snapshots
              SELECT ad_id,scraped_at,title,query,url,make,model,trim,variant_key,year_jalali,
              mileage_km,is_zero_km,price_toman,price_valid,price_is_agreed,city,gearbox,
              fair_price_toman,discount_pct,comparable_count,comparison_basis,underpriced
              FROM incoming_listings ON CONFLICT(ad_id,scraped_at) DO NOTHING"""))
            c.execute(text("""CREATE OR REPLACE VIEW car_listings_current AS
              SELECT DISTINCT ON(ad_id) * FROM car_listing_snapshots ORDER BY ad_id,scraped_at DESC"""))
            c.execute(text("DROP TABLE incoming_listings"))
    finally:
        engine.dispose()


def main():
    p=argparse.ArgumentParser(description="Pandas analysis and PostgreSQL loader for Bama CSV")
    p.add_argument("--csv",type=Path,default=Path("/input/cars_test.csv"))
    p.add_argument("--reports-dir",type=Path,default=Path("/reports"))
    p.add_argument("--min-peers",type=int,default=2)
    p.add_argument("--underpriced-threshold",type=float,default=.15)
    a=p.parse_args()
    if a.min_peers<1 or not 0<=a.underpriced_threshold<1: p.error("invalid peer count or threshold")
    if not a.csv.exists(): p.error(f"CSV not found: {a.csv}")
    df=estimate_prices(load_csv(a.csv),a.min_peers,a.underpriced_threshold)
    charts(df,a.reports_dir)
    db=os.environ.get("DATABASE_URL")
    if not db: p.error("DATABASE_URL is required")
    write_db(df,db)
    print(f"Listings: {len(df)} | valid prices: {int(df.price_valid.sum())} | estimates: {int(df.fair_price_toman.notna().sum())} | underpriced: {int(df.underpriced.fillna(False).sum())}")
    print(f"Pandas charts: {a.reports_dir}")


if __name__=="__main__":
    main()
