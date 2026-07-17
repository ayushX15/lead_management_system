"""
Lead validation runner.

For every CSV in VALIDATION_INPUT_DIR:
  1. Offline phone validation (phonenumbers) for every detected phone column.
  2. AI classification of each row into domain_tag / subdomain_tag using the
     shared taxonomy (multi-model rotation, free-tier friendly).
  3. Crash-safe output: saves every VALIDATION_SAVE_EVERY rows and RESUMES a
     previous partial run instead of re-classifying (and re-paying for) rows
     that are already tagged.

Run:  python lead_val/run_validation.py
"""

import os
import sys
import glob

import pandas as pd

# Make lead_val importable no matter where Python was launched from
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from utils.envtools import load_env, ARCHIVED_ROOT

load_env()

from utils.category_classifier import ClassifierClient
from utils.column_detector import detect_columns
from utils.phone_validator import validate_phone

# Rows with these markers are (re)classified; anything else counts as done.
RETRYABLE_TAGS = {"", "Classification Failed", "Processing Error"}
NAME_COL_CANDIDATES = {"business name", "company", "name", "company name"}


def find_name_col(df):
    """Header match is normalized (case, underscores) so company_name works too."""
    for col in df.columns:
        if str(col).strip().lower().replace("_", " ") in NAME_COL_CANDIDATES:
            return col
    return None


def add_phone_validation(df):
    """Offline phonenumbers validation for every detected phone column."""
    try:
        cols_info = detect_columns(df)
    except Exception as e:
        print(f"Column detection failed ({e}); skipping phone validation.")
        return []
    phone_cols = cols_info.get("phone_columns") or []
    added = []
    for col in phone_cols:
        results = [validate_phone(v) for v in df[col]]
        df[f"{col}_valid"] = ["VALID" if r["is_valid_format"] else "INVALID" for r in results]
        df[f"{col}_number_type"] = [r.get("number_type") or "UNKNOWN" for r in results]
        df[f"{col}_carrier_name"] = [r.get("carrier") or "Unknown" for r in results]
        added.extend([f"{col}_valid", f"{col}_number_type", f"{col}_carrier_name"])
    if phone_cols:
        print(f"Phone validation added for columns: {phone_cols}")
    return added


def _safe_save(df, path, final=False):
    try:
        df.to_csv(path, index=False)
    except PermissionError:
        level = "CRITICAL" if final else "Warning"
        print(f"{level}: could not save to {path}. Is the file open in Excel?")


def run():
    rel_in = os.getenv("VALIDATION_INPUT_DIR", "lead_val/input")
    rel_out = os.getenv("VALIDATION_OUTPUT_DIR", "lead_val/output")
    input_dir = rel_in if os.path.isabs(rel_in) else os.path.join(ARCHIVED_ROOT, rel_in)
    output_dir = rel_out if os.path.isabs(rel_out) else os.path.join(ARCHIVED_ROOT, rel_out)
    os.makedirs(output_dir, exist_ok=True)

    save_every = int(os.getenv("VALIDATION_SAVE_EVERY", "25") or "25")

    csv_files = [f for f in glob.glob(os.path.join(input_dir, "*.csv"))
                 if not f.endswith("_validated.csv")]
    if not csv_files:
        print(f"No CSV files found in {input_dir}")
        return

    print("Initializing Category Classifier...")
    classifier = ClassifierClient()

    for filepath in csv_files:
        filename = os.path.basename(filepath)
        print(f"\nProcessing {filename}...")
        output_path = os.path.join(output_dir, f"{os.path.splitext(filename)[0]}_validated.csv")

        try:
            df = pd.read_csv(filepath, dtype=str, keep_default_na=False, low_memory=False)
            original_columns = list(df.columns)

            df["domain_tag"] = ""
            df["subdomain_tag"] = ""

            # ---- resume: reuse tags from a previous partial run
            if os.path.exists(output_path):
                try:
                    prev = pd.read_csv(output_path, dtype=str, keep_default_na=False, low_memory=False)
                    if len(prev) == len(df) and "domain_tag" in prev.columns:
                        df["domain_tag"] = prev["domain_tag"].values
                        df["subdomain_tag"] = prev["subdomain_tag"].values
                        already = int((~df["domain_tag"].isin(RETRYABLE_TAGS)).sum())
                        if already:
                            print(f"Resuming: {already}/{len(df)} rows already classified in a previous run.")
                except Exception as e:
                    print(f"Could not resume from previous output ({e}); starting fresh.")

            phone_val_cols = add_phone_validation(df)

            name_col = find_name_col(df)
            if not name_col:
                print("Note: no business-name column detected; classifying from industry/keywords only.")

            total = len(df)
            success_count = int((~df["domain_tag"].isin(RETRYABLE_TAGS)).sum())
            since_save = 0

            for idx, row in df.iterrows():
                if str(df.at[idx, "domain_tag"]) not in RETRYABLE_TAGS:
                    continue  # already classified (resume)
                try:
                    d = row.to_dict()
                    b_name = str(d.get(name_col, "")) if name_col else f"Row {idx + 1}"
                    industry = str(d.get("industry", ""))
                    keywords = str(d.get("keywords", ""))
                    context = f"Industry: {industry}, Keywords: {keywords}"

                    res = classifier.classify(context, b_name)
                    domain = res.get("domain", "Classification Failed")
                    subdomain = res.get("subdomain", "Classification Failed")
                    if domain != "Classification Failed":
                        success_count += 1
                    df.at[idx, "domain_tag"] = domain
                    df.at[idx, "subdomain_tag"] = subdomain
                except Exception as e:
                    print(f"Error processing row {idx}: {e}")
                    df.at[idx, "domain_tag"] = "Processing Error"
                    df.at[idx, "subdomain_tag"] = "Processing Error"

                # Periodic checkpoint instead of the old full-file write per row
                since_save += 1
                if since_save >= save_every:
                    since_save = 0
                    _safe_save(df, output_path)

                if (idx + 1) % 5 == 0 or (idx + 1) == total:
                    print(f"Processed {idx + 1}/{total} rows - {success_count} successfully classified")

            # ---- final ordered save
            final_columns = original_columns + phone_val_cols + ["domain_tag", "subdomain_tag"]
            final_columns = [c for c in final_columns if c in df.columns]
            _safe_save(df[final_columns], output_path, final=True)
            print(f"Done: {filename} | {success_count}/{total} businesses classified")

        except Exception as e:
            print(f"Fatal error processing file {filename}: {e}")


if __name__ == "__main__":
    run()
