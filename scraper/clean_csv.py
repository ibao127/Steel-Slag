from pathlib import Path
import pandas as pd

# Same "data" folder the scraper writes to (put this file next to the scraper)
data_dir = Path(__file__).resolve().parent / "data"

df = pd.read_csv(data_dir / "articles_all.csv", encoding="utf-8-sig")

# Keep only the articles the scraper kept (no videos, no paywalls)
df_clean = df[df["kept"] == "yes"]

# Drop the bookkeeping columns you no longer need
df_clean = df_clean[["title", "outlet", "date_iso", "tags", "type", "clean_url"]]
df_clean = df_clean.rename(columns={"date_iso": "date", "clean_url": "url"})

df_clean.to_csv(data_dir / "cleaned_articles.csv", index=False, encoding="utf-8-sig")
print(f"{len(df_clean)} of {len(df)} articles kept -> {data_dir / 'cleaned_articles.csv'}")