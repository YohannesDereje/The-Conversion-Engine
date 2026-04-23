import math
import pandas as pd
from thefuzz import process

DATA_PATH = (
    r"c:\Users\Yohannes\Desktop\tenx education\Weeks\week 10"
    r"\The conversion Engine\data\layoffs_fyi.csv"
)
MATCH_THRESHOLD = 85

_df: pd.DataFrame | None = None


def _load() -> pd.DataFrame:
    global _df
    if _df is None:
        _df = pd.read_csv(DATA_PATH, low_memory=False)
        _df["_name_lower"] = _df["Company"].fillna("").str.lower()
    return _df


def _clean(val):
    if val is None:
        return None
    try:
        if math.isnan(float(val)):
            return None
    except (TypeError, ValueError):
        pass
    return val


def check_layoffs(company_name: str) -> dict:
    df = _load()
    result = process.extractOne(company_name.lower(), df["_name_lower"].tolist())
    if result is None:
        return {"detected": False}

    matched_name, score = result[0], result[1]
    if score < MATCH_THRESHOLD:
        return {"detected": False}

    # Multiple layoff events may exist — return the most recent one
    company_rows = df[df["_name_lower"] == matched_name].copy()
    if company_rows.empty:
        company_rows = df.iloc[[idx]]

    # Sort by Date descending to get the most recent event
    try:
        company_rows = company_rows.sort_values("Date", ascending=False)
    except Exception:
        pass

    row = company_rows.iloc[0]
    return {
        "detected":           True,
        "date":               _clean(row.get("Date")),
        "headcount_reduction": _clean(row.get("# Laid Off")),
        "percentage_cut":     _clean(row.get("%")),
        "source_url":         _clean(row.get("Source")),
        "match_score":        score,
    }
