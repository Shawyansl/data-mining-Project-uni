#!/usr/bin/env python3
"""Pandas baseline analysis and PostgreSQL loader for Sheypoor property CSVs."""
from __future__ import annotations
import argparse, os
from pathlib import Path
from statistics import median
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import pandas as pd
from sqlalchemy import create_engine, text

NUMERIC = ["area_sqm", "rooms", "year_built", "sale_price_toman", "deposit_toman", "monthly_rent_toman", "daily_rent_toman"]
DB_COLS = ["ad_id","scraped_at","title","url","listing_type","property_type","city","neighborhood","area_sqm","rooms","year_built","sale_price_toman","deposit_toman","monthly_rent_toman","daily_rent_toman","price_agreed","posted_text","sale_price_per_sqm","deposit_per_sqm","monthly_rent_per_sqm","fair_price_toman","fair_deposit_toman","fair_monthly_rent_toman","comparable_count","comparison_basis","price_anomaly","anomaly_score"]

def bool_col(series):
    return series.fillna(False).astype(str).str.strip().str.lower().isin({"true","1","yes","y"})

def load_csv(path):
    df = pd.read_csv(path, encoding="utf-8-sig", low_memory=False)
    required = {"ad_id","listing_type","area_sqm"}
    missing = required - set(df.columns)
    if missing: raise ValueError("Missing CSV columns: " + ", ".join(sorted(missing)))
    for col in NUMERIC:
        if col not in df: df[col] = pd.NA
        df[col] = pd.to_numeric(df[col], errors="coerce")
    for col in ("title","url","property_type","city","neighborhood","posted_text"):
        if col not in df: df[col] = pd.NA
    if "price_agreed" not in df: df["price_agreed"] = False
    df["price_agreed"] = bool_col(df["price_agreed"])
    if "scraped_at" not in df: df["scraped_at"] = pd.Timestamp.now(tz="UTC")
    else:
        df["scraped_at"] = pd.to_datetime(df["scraped_at"], utc=True, errors="coerce")
        df["scraped_at"] = df["scraped_at"].fillna(pd.Timestamp.now(tz="UTC"))
    df["ad_id"] = df.ad_id.astype("string")
    df["listing_type"] = df.listing_type.astype("string").str.lower()
    df = df.dropna(subset=["ad_id"]).drop_duplicates("ad_id", keep="last").copy()
    df = df.loc[df.listing_type.isin(["sale","rent"]) & df.area_sqm.gt(0)].copy()
    df["sale_price_toman"] = df.sale_price_toman.where(df.sale_price_toman.gt(0))
    df["deposit_toman"] = df.deposit_toman.where(df.deposit_toman.ge(1_000_000))
    df["monthly_rent_toman"] = df.monthly_rent_toman.where(df.monthly_rent_toman.ge(100_000))
    df.loc[df.price_agreed, ["sale_price_toman","deposit_toman","monthly_rent_toman"]] = pd.NA
    df.loc[df.listing_type.eq("rent"), "sale_price_toman"] = pd.NA
    df.loc[df.listing_type.eq("sale"), ["deposit_toman","monthly_rent_toman"]] = pd.NA
    df["sale_price_per_sqm"] = df.sale_price_toman / df.area_sqm
    df["deposit_per_sqm"] = df.deposit_toman / df.area_sqm
    df["monthly_rent_per_sqm"] = df.monthly_rent_toman / df.area_sqm
    return df

def estimate_and_flag(df, min_peers, anomaly_threshold):
    fairs, deposits, rents = [None]*len(df), [None]*len(df), [None]*len(df)
    counts, bases, flags, scores = [0]*len(df), [None]*len(df), [None]*len(df), [None]*len(df)
    pos = {idx:i for i,idx in enumerate(df.index)}
    group_cols = ["listing_type","property_type","city","neighborhood"]
    def norm(value):
        return "<missing>" if pd.isna(value) else str(value).strip().casefold()
    groups = {tuple(norm(v) for v in (key if isinstance(key,tuple) else (key,))):g
              for key,g in df.groupby(group_cols,dropna=False,sort=False)}
    for idx,row in df.iterrows():
        i=pos[idx]
        key=tuple(norm(row[c]) for c in group_cols)
        peers=groups.get(key,df.iloc[0:0])
        peers=peers.loc[peers.ad_id.ne(row.ad_id)]
        if pd.notna(row.rooms): peers=peers.loc[peers.rooms.isna() | peers.rooms.eq(row.rooms)]
        peers=peers.loc[peers.area_sqm.between(row.area_sqm*.75,row.area_sqm*1.25)]
        col="sale_price_per_sqm" if row.listing_type=="sale" else "monthly_rent_per_sqm"
        peers=peers.loc[peers[col].notna()]
        counts[i]=len(peers)
        if len(peers)>=min_peers:
            unit=float(median(peers[col].astype(float).tolist()))
            estimate=round(unit*float(row.area_sqm))
            bases[i]="same city/neighborhood/type; area ±25%; rooms matched when available"
            if row.listing_type=="sale": fairs[i]=estimate
            else:
                rents[i]=estimate
                dp=peers.deposit_per_sqm.dropna()
                if len(dp)>=min_peers: deposits[i]=round(float(median(dp.astype(float).tolist()))*float(row.area_sqm))
        vals=pd.to_numeric(peers[col],errors="coerce").dropna().tolist()
        own=row.get(col)
        if pd.notna(own) and len(vals)>=max(4,min_peers):
            center=median(vals); mad=median([abs(v-center) for v in vals])
            score=0.6745*(float(own)-center)/mad if mad else (0.0 if own==center else float("inf"))
            scores[i]=round(score,2) if abs(score)!=float("inf") else None
            flags[i]=abs(score)>=anomaly_threshold if mad else (own!=center)
    df["fair_price_toman"]=pd.array(fairs,dtype="Int64")
    df["fair_deposit_toman"]=pd.array(deposits,dtype="Int64")
    df["fair_monthly_rent_toman"]=pd.array(rents,dtype="Int64")
    df["comparable_count"]=pd.array(counts,dtype="Int64")
    df["comparison_basis"]=bases
    df["price_anomaly"]=pd.array(flags,dtype="boolean")
    df["anomaly_score"]=pd.array(scores,dtype="Float64")
    return df

def make_charts(df,out):
    out.mkdir(parents=True,exist_ok=True)
    sale=df.loc[df.sale_price_per_sqm.notna()]
    rent=df.loc[df.monthly_rent_per_sqm.notna()]
    rows=[]
    for typ,part,col in [("sale",sale,"sale_price_toman"),("rent",rent,"monthly_rent_toman")]:
        for key,g in part.groupby(["city","neighborhood"],dropna=False):
            rows.append({"listing_type":typ,"city":key[0],"neighborhood":key[1],"listing_count":len(g),"median_total_toman":g[col].median(),"median_per_sqm_toman":g[col.replace("_toman","_per_sqm")].median()})
    summary=pd.DataFrame(rows)
    summary.to_csv(out/"real_estate_neighborhood_summary.csv",index=False,encoding="utf-8-sig")
    for typ, color in (("sale", "#277da1"), ("rent", "#43aa8b")):
        top=summary.loc[summary.listing_type.eq(typ)].sort_values("listing_count",ascending=False).head(20).copy()
        if top.empty: continue
        labels=[f"{r.city or 'نامشخص'} / {r.neighborhood or 'نامشخص'} ({int(r.listing_count)})" for r in top.itertuples()]
        fig,ax=plt.subplots(figsize=(12,max(5,.35*len(top))))
        ax.barh(labels[::-1],(top.median_per_sqm_toman/1e6).tolist()[::-1],color=color)
        measure="Sale price" if typ=="sale" else "Monthly rent"
        ax.set_xlabel(f"Median {measure} per sqm (million Toman)")
        ax.set_title(f"Neighborhood {typ} indicators")
        ax.grid(axis="x",alpha=.25); fig.tight_layout()
        fig.savefig(out/f"real_estate_{typ}_neighborhoods.png",dpi=160); plt.close(fig)
    coverage=pd.DataFrame([{"metric":"Listings","value":len(df)},{"metric":"Sale listings with price","value":int(sale.sale_price_toman.notna().sum())},{"metric":"Rent listings with monthly rent","value":int(rent.monthly_rent_toman.notna().sum())},{"metric":"Listings with estimate","value":int(df.fair_price_toman.notna().sum()+df.fair_monthly_rent_toman.notna().sum())},{"metric":"Anomaly candidates","value":int(df.price_anomaly.fillna(False).sum())}])
    coverage.to_csv(out/"real_estate_coverage_summary.csv",index=False,encoding="utf-8-sig")
    fig,ax=plt.subplots(figsize=(9,5)); ax.bar(coverage.metric,coverage.value,color=["#4d908e","#577590","#43aa8b","#f9c74f","#f94144"])
    ax.set_ylabel("Listing count"); ax.set_title("Real-estate data and estimate coverage"); ax.tick_params(axis="x",rotation=15); ax.grid(axis="y",alpha=.25)
    fig.tight_layout(); fig.savefig(out/"real_estate_coverage.png",dpi=160); plt.close(fig)

def write_db(df,url):
    for col in DB_COLS:
        if col not in df: df[col]=pd.NA
    engine=create_engine(url,pool_pre_ping=True)
    try:
        df[DB_COLS].to_sql("real_estate_incoming",engine,if_exists="replace",index=False,method="multi",chunksize=500)
        with engine.begin() as c:
            c.execute(text("""CREATE TABLE IF NOT EXISTS real_estate_listing_snapshots (
              ad_id TEXT NOT NULL,scraped_at TIMESTAMPTZ NOT NULL,title TEXT,url TEXT,listing_type TEXT,
              property_type TEXT,city TEXT,neighborhood TEXT,area_sqm DOUBLE PRECISION,rooms INTEGER,
              year_built INTEGER,sale_price_toman BIGINT,deposit_toman BIGINT,monthly_rent_toman BIGINT,
              daily_rent_toman BIGINT,price_agreed BOOLEAN,posted_text TEXT,sale_price_per_sqm DOUBLE PRECISION,
              deposit_per_sqm DOUBLE PRECISION,monthly_rent_per_sqm DOUBLE PRECISION,fair_price_toman BIGINT,
              fair_deposit_toman BIGINT,fair_monthly_rent_toman BIGINT,comparable_count INTEGER,
              comparison_basis TEXT,price_anomaly BOOLEAN,anomaly_score DOUBLE PRECISION,
              PRIMARY KEY(ad_id,scraped_at))"""))
            c.execute(text("""INSERT INTO real_estate_listing_snapshots
              SELECT ad_id,scraped_at,title,url,listing_type,property_type,city,neighborhood,area_sqm,rooms,
              year_built,sale_price_toman,deposit_toman,monthly_rent_toman,daily_rent_toman,price_agreed,
              posted_text,sale_price_per_sqm,deposit_per_sqm,monthly_rent_per_sqm,fair_price_toman,
              fair_deposit_toman,fair_monthly_rent_toman,comparable_count,comparison_basis,price_anomaly,
              anomaly_score FROM real_estate_incoming ON CONFLICT(ad_id,scraped_at) DO NOTHING"""))
            c.execute(text("""CREATE OR REPLACE VIEW real_estate_listings_current AS
              SELECT DISTINCT ON(ad_id) * FROM real_estate_listing_snapshots ORDER BY ad_id,scraped_at DESC"""))
            c.execute(text("DROP TABLE real_estate_incoming"))
    finally: engine.dispose()

def main():
    p=argparse.ArgumentParser(description="Pandas analysis and PostgreSQL loader for Sheypoor property CSV")
    p.add_argument("--csv",type=Path,default=Path("/input/real_estate/sheypoor_properties.csv"))
    p.add_argument("--reports-dir",type=Path,default=Path("/reports/real_estate"))
    p.add_argument("--min-peers",type=int,default=2)
    p.add_argument("--anomaly-threshold",type=float,default=3.5)
    a=p.parse_args()
    if a.min_peers<1 or a.anomaly_threshold<=0: p.error("min-peers and anomaly-threshold must be positive")
    if not a.csv.exists(): p.error(f"CSV not found: {a.csv}")
    df=load_csv(a.csv)
    if df.empty: p.error("CSV contains no supported sale or monthly-rent listings with a positive area")
    df=estimate_and_flag(df,a.min_peers,a.anomaly_threshold)
    make_charts(df,a.reports_dir)
    db=os.environ.get("DATABASE_URL")
    if not db: p.error("DATABASE_URL is required")
    write_db(df,db)
    print(f"Listings: {len(df)} | sale prices: {int(df.sale_price_toman.notna().sum())} | monthly rents: {int(df.monthly_rent_toman.notna().sum())} | estimates: {int(df.fair_price_toman.notna().sum()+df.fair_monthly_rent_toman.notna().sum())} | anomaly candidates: {int(df.price_anomaly.fillna(False).sum())}")
    print(f"Pandas reports: {a.reports_dir}")

if __name__=="__main__": main()
