import math
import pandas as pd
from thefuzz import process

DATA_PATH = (
    r"c:\Users\Yohannes\Desktop\tenx education\Weeks\week 10"
    r"\The conversion Engine\data\crunchbase-companies-information.csv"
)
MATCH_THRESHOLD = 85

_df: pd.DataFrame | None = None


def _load() -> pd.DataFrame:
    global _df
    if _df is None:
        _df = pd.read_csv(DATA_PATH, low_memory=False)
        _df["_name_lower"] = _df["name"].fillna("").str.lower()
    return _df


def _clean(val):
    """Convert NaN / float NaN to None for JSON safety."""
    if val is None:
        return None
    try:
        if math.isnan(float(val)):
            return None
    except (TypeError, ValueError):
        pass
    return val


def enrich(company_name: str) -> dict:
    df = _load()
    result = process.extractOne(company_name.lower(), df["_name_lower"].tolist())
    if result is None:
        return {"error": "Company not found in Crunchbase dataset", "match_score": 0}

    matched_name, score = result[0], result[1]
    if score < MATCH_THRESHOLD:
        return {"error": "Company not found in Crunchbase dataset", "match_score": score}

    idx = df[df["_name_lower"] == matched_name].index[0]
    row = df.loc[idx]
    return {
        "name":               _clean(row.get("name")),
        "domain":             _clean(row.get("website")),
        "industry":           _clean(row.get("industries")),
        "employee_count":     _clean(row.get("num_employees")),
        "funding_total":      _clean(row.get("funds_total")),
        "last_funding_stage": _clean(row.get("investment_stage")),
        "last_funding_date":  _clean(row.get("founded_date")),
        "location":           _clean(row.get("location")),
        "description":        _clean(row.get("about")),
        "crunchbase_id":      _clean(row.get("uuid")),
        "match_score":        score,
    }
