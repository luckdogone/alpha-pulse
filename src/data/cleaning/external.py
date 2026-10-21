from urllib.parse import urlsplit

from data.errors import DataError


def clean_news(raw: dict, domains: list[str]) -> list[dict]:
    if "articles" not in raw:
        raise DataError("GDELT returned no article result envelope")
    articles, seen = [], set()
    for item in raw["articles"]:
        url = item.get("url", "")
        parsed = urlsplit(url)
        host = (parsed.hostname or "").lower()
        if parsed.scheme != "https" or not any(
            host == d or host.endswith("." + d) for d in domains
        ):
            continue
        if url in seen:
            continue
        seen.add(url)
        articles.append(
            {
                "title": item.get("title", "")[:350],
                "url": url,
                "publisher": host,
                "indexed_at": item.get("seendate"),
                "published_at": None,
                "coverage": "search index headline only; article body not verified",
            }
        )
    return articles


def clean_liquidations(raw: dict) -> list[dict]:
    if str(raw.get("code")) != "0":
        raise DataError("CoinGlass rejected request; check key, symbol, and plan interval access")
    data = raw.get("data", [])
    if not isinstance(data, list):
        raise DataError("invalid CoinGlass response")
    return data


def clean_onchain_adapter(raw) -> tuple[int | None, dict]:
    # Configured adapters must provide their own provider observation timestamp.
    observed = raw.get("observed_at") if isinstance(raw, dict) else None
    if observed is not None and (not isinstance(observed, int) or observed < 1_000_000_000_000):
        raise DataError("on-chain adapter observed_at must be UTC milliseconds")
    summary = raw.get("summary", {}) if isinstance(raw, dict) else {}
    if not isinstance(summary, dict):
        raise DataError("on-chain adapter summary must be an object")
    if len(str(summary)) > 4000:
        summary = {"note": "large summary archived; use read_evidence"}
    return observed, summary
