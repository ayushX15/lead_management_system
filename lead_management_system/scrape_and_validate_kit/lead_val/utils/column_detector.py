import pandas as pd  # type: ignore

def detect_columns(df: pd.DataFrame) -> dict:
    phone_keywords = ['phone', 'contact', 'mobile', 'number', 'tel', 'cell']
    website_keywords = ['website', 'url', 'site', 'web', 'domain', 'link']
    
    phone_columns = []
    website_column = None
    website_max_score = 0
    
    for col in df.columns:
        col_lower = col.lower()
        
        # Phone scoring
        phone_score = sum(1 for kw in phone_keywords if kw in col_lower)
        # Avoid false positives for contact names/emails if possible, but collect all phone-like
        if phone_score > 0 and 'name' not in col_lower and 'email' not in col_lower and 'designation' not in col_lower:
            phone_columns.append((col, phone_score))
            
        # Website scoring
        web_score = sum(1 for kw in website_keywords if kw in col_lower)
        if web_score > website_max_score:
            website_max_score = web_score
            website_column = col
            
    # Social scoring (one pass after collection or just iterate)
    linkedin_founder_col = None
    linkedin_company_col = None
    facebook_col = None
    twitter_col = None
    
    for col in df.columns:
        cl = col.lower()
        if 'linkedin' in cl:
            if any(k in cl for k in ['founder', 'ceo', 'person', 'profile']):
                linkedin_founder_col = col
            elif any(k in cl for k in ['company', 'business']):
                linkedin_company_col = col
            elif not linkedin_company_col:  # fallback if not specified
                linkedin_company_col = col
                
        if 'facebook' in cl:
            facebook_col = col
            
        if 'twitter' in cl or 'x url' in cl:
            twitter_col = col

    if not phone_columns:
        # Not fatal: classification can still run, only phone validation is skipped
        print("Warning: no phone column detected - phone validation will be skipped for this run")

    # Sort phone columns by score descending, though we return all of them
    phone_columns.sort(key=lambda x: x[1], reverse=True)
    phone_cols_extracted = [col for col, score in phone_columns]
    
    if not website_column:
        print("Warning: No website column detected — website classification will be skipped for this run")
        
    return {
        'phone_columns': phone_cols_extracted,
        'website_column': website_column,
        'linkedin_founder_col': linkedin_founder_col,
        'linkedin_company_col': linkedin_company_col,
        'facebook_col': facebook_col,
        'twitter_col': twitter_col
    }

if __name__ == "__main__":
    # Verification checkpoint
    import os
    sample_path = os.path.join(os.path.dirname(__file__), "..", "input", "sample_test.csv")
    if os.path.exists(sample_path):
        print(f"Testing on {sample_path}")
        df = pd.read_csv(sample_path)
        print("Columns:", list(df.columns))
        result = detect_columns(df)
        print("Detection Result:", result)
    else:
        print("Sample CSV not found for testing.")
