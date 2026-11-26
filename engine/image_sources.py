"""Accuracy-first image sourcing waterfall. Read-only; attaches nothing.

Tier order (also confidence order):
  0 curated media_index  -> 1 Icecat exact MPN -> 2 SerpAPI Google Images
  -> 3 Exa (manufacturer pages) -> 4 Google CSE (disabled unless enabled)

Identity evidence, strongest first:
  exact MPN in image URL > exact MPN in title > manufacturer host + model
  family + MPN on source page. Family-only is REJECTED (G1 vs G2 safety).

fetch_url may carry credentials (Icecat content_token); source_url is always
sanitised because product.metadata is PUBLIC via storefront GraphQL.
"""
import logging
import re
import time
from urllib.parse import urlparse

from . import image_identity as ident
from . import image_lookup as lookup
from . import icecat_client as icecat

logger = logging.getLogger(__name__)

ENABLE_GOOGLE_CSE = False          # 403 ACCESS_DENIED until billing enabled
MIN_SIDE = 400
ICON_HINTS = ("icon", "logo", "favicon", "sprite", "spacer", "pixel",
              "1x1", "banner", "badge", "flag-", "arrow", "button")


def _alnum(value):
    return re.sub(r"[^A-Z0-9]", "", str(value or "").upper())


def score_candidate(url, title, page_url, brand_norm, tokens, mpn_variants):
    """Return (confidence, reason). 0.0 means rejected."""
    path = (urlparse(url).path or "").lower()
    if any(h in path for h in ICON_HINTS):
        return 0.0, "icon_or_logo"

    au, at, ap = _alnum(url), _alnum(title), _alnum(page_url)
    mpns = [_alnum(m) for m in mpn_variants if m]
    mpn_url = any(m and m in au for m in mpns)
    mpn_title = any(m and m in at for m in mpns)
    mpn_page = any(m and m in ap for m in mpns)
    family = bool(ident.token_hits(url, tokens) or ident.token_hits(title, tokens))
    is_mfr = ident.host_class(url, brand_norm) == "manufacturer"

    if mpn_url:
        base, why = 0.90, "mpn_in_image_url"
    elif mpn_title:
        base, why = 0.86, "mpn_in_title"
    elif is_mfr and family and mpn_page:
        base, why = 0.72, "mfr_family+mpn_on_page"
    elif is_mfr and family:
        base, why = 0.66, "mfr_family_only"
    elif family and mpn_page:
        base, why = 0.62, "family+mpn_on_page"
    else:
        return 0.0, "no_identity_evidence"

    if is_mfr:
        base += 0.04
    if mpn_page and not (mpn_url or mpn_title):
        base += 0.03
    return min(base, 0.97), why


def _mpn_variants(identity):
    return [k for k, _ in ident.mpn_candidates(
        identity["mpn"], identity.get("primary_category"))]


def _derivations(identity):
    return [d for _, d in ident.mpn_candidates(
        identity["mpn"], identity.get("primary_category"))]


# ------------------------------------------------------------------ tier 1
def _icecat(identity, mpn_variants):
    if not icecat.configured():
        return [], "icecat_unconfigured"
    bn = identity["brand_norm"]
    for key, deriv in ident.mpn_candidates(identity["mpn"],
                                           identity.get("primary_category")):
        try:
            data = icecat.get_datasheet(brand=identity["brand"], product_code=key)
        except icecat.IcecatMiss:
            continue
        except icecat.IcecatError as exc:
            if exc.status_code in icecat.RESTRICTED:
                return [], "icecat_restricted"
            return [], "icecat_error"
        except Exception:
            return [], "icecat_error"

        meta = icecat.identities(data)
        returned = ident.norm_brand(meta.get("brand"))
        if returned and bn and returned != bn:
            return [], "icecat_brand_mismatch"
        conf = 0.95 + (0.04 if ident.token_hits(meta.get("title"),
                                                identity["model_tokens"]) else 0.0)
        if deriv == "derived_base":
            conf = min(conf, 0.90)
        out = []
        for u in icecat.image_urls(data)[:3]:
            out.append({"tier": "icecat", "confidence": round(conf, 2),
                        "reason": "icecat_exact_key" + ("+derived" if deriv != "exact" else ""),
                        "fetch_url": u, "source_url": ident.sanitize_url(u),
                        "title": meta.get("title", ""), "source_page": "",
                        "matched_mpn": key, "mpn_derivation": deriv})
        return (out, "icecat") if out else ([], "icecat_no_images")
    return [], "icecat_miss"


# ------------------------------------------------------------------ tier 2
def serpapi_images(query, api_key, ijn=0, gl="au", hl="en"):
    import requests
    r = requests.get("https://serpapi.com/search",
        params={"engine": "google_images", "q": query, "api_key": api_key,
                "gl": gl, "hl": hl, "ijn": ijn, "safe": "active"},
        timeout=(5, 40))
    if r.status_code != 200:
        raise RuntimeError("serpapi HTTP %s: %s" % (r.status_code, r.text[:200]))
    d = r.json()
    return d.get("images_results") or d.get("image_results") or []


def _search_queries(identity):
    """Exact supplier SKU first, then the strongest extracted model identifier."""
    brand = (identity.get("brand") or "").strip()
    mpn = (identity.get("mpn") or "").strip()
    queries = []

    if brand and mpn:
        queries.append(f'"{brand}" "{mpn}"')

    # Identity extraction prioritises supplier-SKU components. Use the
    # strongest model-like component as a targeted fallback query.
    model_tokens = identity.get("model_tokens") or []
    model = next(
        (
            token for token in model_tokens
            if len(str(token)) >= 5
            and any(ch.isdigit() for ch in str(token))
        ),
        "",
    )

    if brand and model:
        fallback = f'"{brand}" "{model}"'
        if fallback not in queries:
            queries.append(fallback)

    return queries


def _visual_rank(title, url):
    """Order primary product photographs ahead of secondary technical views."""
    text = ((title or "") + " " + (url or "")).lower()
    for separator in ("-", "_", "%20"):
        text = text.replace(separator, " ")

    rank = 0
    primary_terms = (
        "front view", "front", "hero", "three quarter",
        "3 quarter", "angled view", "product image",
        "main image", "product photo", "studio product",
        "packshot",
    )
    secondary_terms = (
        "ports and slots", "ports slots", "port and slot",
        "dimension", "diagram", "exploded", "schematic",
        "materials design", "feature video", "specification",
        "technical drawing", "internal view",
        "rear view", "bottom view",
    )

    for term in primary_terms:
        if term in text:
            rank += 4

    for term in secondary_terms:
        if term in text:
            rank -= 8

    for term in ("ports", "connectors"):
        if term in text:
            rank -= 3

    return rank


def _photo_search_queries(identity):
    """Try photographic views first, retaining exact identity queries."""
    original_queries = list(_search_queries(identity) or [])
    queries = []

    for original in original_queries:
        query = str(original).strip()
        if not query:
            continue

        for suffix in (
            "product photo",
            "front view",
            "three quarter view",
        ):
            candidate = f"{query} {suffix}"
            if candidate not in queries:
                queries.append(candidate)

        if query not in queries:
            queries.append(query)

    return queries[:12]



def _serpapi(identity, api_key, mpn_variants, limit=3):
    queries = _photo_search_queries(identity)
    had_response = False

    for q in queries:
        try:
            items = serpapi_images(q, api_key)
            had_response = True
        except Exception as exc:
            logger.warning("serpapi failed: %s", exc)
            continue

        scored = []
        for it in items:
            url = it.get("original") or ""
            if not url:
                continue
            if it.get("unsafe") is True:
                continue

            w = it.get("original_width") or 0
            h = it.get("original_height") or 0
            if w and h and min(w, h) < MIN_SIDE:
                continue

            page = it.get("link") or ""
            if not (
                ident.token_hits(page, identity["model_tokens"])
                or any(
                    ident._alnum(m)
                    and ident._alnum(m) in ident._alnum(page)
                    for m in mpn_variants
                )
            ):
                continue

            conf, why, matched, deriv = ident.image_evidence(
                url,
                "",
                identity["brand_norm"],
                identity["model_tokens"],
                list(zip(mpn_variants, _derivations(identity))),
            )
            if conf < ident.ACCEPT_THRESHOLD:
                continue

            scored.append((_visual_rank(it.get("title", ""), url), conf, why, it, w, h, matched, deriv))

        scored.sort(key=lambda x: (-x[0], -x[1], -(x[4] * x[5])))
        out = []

        for visual_rank, conf, why, it, w, h, matched, deriv in scored[:limit]:
            out.append({
                "tier": "serpapi",
                "confidence": round(conf, 2),
                "reason": why,
                "fetch_url": it["original"],
                "source_url": ident.sanitize_url(it["original"]),
                "title": it.get("title", ""),
                "source_page": ident.sanitize_url(it.get("link") or ""),
                "matched_mpn": matched or identity["mpn"],
                "mpn_derivation": deriv or "exact",
                "width": w,
                "height": h,
            })

        if out:
            return out, "serpapi"

    return [], ("serpapi_no_match" if had_response else "serpapi_error")






# ------------------------------------------------------------------ tier 2
def _scrappa(identity, api_key, base_url, mpn_variants, limit=3):
    """Search Scrappa Google Images with a strict two-request product budget.

    Scrappa credentials are sent only in the X-API-KEY header. They are
    never placed in URLs, candidate metadata, or log messages.
    """
    import requests

    base_url = (base_url or "https://scrappa.co/api").strip().rstrip("/")
    if not base_url.startswith("https://"):
        return [], "scrappa_invalid_base_url"

    raw_queries = _search_queries(identity)
    if not raw_queries:
        return [], "scrappa_no_query"

    # One exact brand+MPN query and one photographic refinement.
    exact_query = raw_queries[0].strip()
    queries = [exact_query, exact_query + " product photo"][:2]
    had_response = False

    for query in queries:
        try:
            response = requests.get(
                base_url + "/images",
                params={"q": query},
                headers={"X-API-KEY": api_key, "Accept": "application/json"},
                timeout=(5, 30),
            )
            if response.status_code != 200:
                raise RuntimeError("Scrappa HTTP %s" % response.status_code)

            payload = response.json()
            if isinstance(payload, list):
                items = payload
            elif isinstance(payload, dict):
                items = payload.get("results") or []
            else:
                items = []

            if not isinstance(items, list):
                items = []

            had_response = True

        except Exception as exc:
            # Do not log exception text: HTTP exceptions can contain URLs
            # or request details. Never log credentials or response bodies.
            logger.warning("scrappa request failed (%s)", type(exc).__name__)
            continue

        scored = []
        for item in items:
            if not isinstance(item, dict):
                continue

            image_url = (item.get("original") or "").strip()
            page_url = (item.get("link") or "").strip()
            title = str(item.get("title") or "")

            if not image_url or not page_url:
                continue

            # Both the image and its evidence page must be public HTTP(S).
            if not lookup.is_public_url(image_url):
                continue
            if not lookup.is_public_url(page_url):
                continue

            # A matching model/MPN must occur on the source page.
            if not (
                ident.token_hits(page_url, identity["model_tokens"])
                or any(
                    ident._alnum(m)
                    and ident._alnum(m) in ident._alnum(page_url)
                    for m in mpn_variants
                )
            ):
                continue

            try:
                width = int(item.get("original_width") or 0)
                height = int(item.get("original_height") or 0)
            except (TypeError, ValueError):
                width, height = 0, 0

            if width and height and min(width, height) < MIN_SIDE:
                continue

            confidence, reason, matched, derivation = ident.image_evidence(
                image_url,
                title,
                identity["brand_norm"],
                identity["model_tokens"],
                list(zip(mpn_variants, _derivations(identity))),
            )
            if confidence < ident.ACCEPT_THRESHOLD:
                continue

            scored.append((
                _visual_rank(title, image_url),
                confidence,
                width * height,
                reason,
                image_url,
                page_url,
                title,
                width,
                height,
                matched,
                derivation,
            ))

        scored.sort(key=lambda row: (-row[0], -row[1], -row[2]))

        output = []
        seen = set()

        for row in scored:
            _, confidence, _, reason, image_url, page_url, title, width, height, matched, derivation = row
            safe_image_url = ident.sanitize_url(image_url)
            safe_page_url = ident.sanitize_url(page_url)

            if not safe_image_url or safe_image_url in seen:
                continue
            if not safe_page_url:
                continue

            seen.add(safe_image_url)
            output.append({
                "tier": "scrappa",
                "confidence": round(confidence, 2),
                "reason": reason,
                "fetch_url": image_url,
                "source_url": safe_image_url,
                "title": title,
                "source_page": safe_page_url,
                "matched_mpn": matched or identity["mpn"],
                "mpn_derivation": derivation or "exact",
                "width": width,
                "height": height,
            })

            if len(output) >= limit:
                break

        if output:
            return output, "scrappa"

    return [], ("scrappa_no_match" if had_response else "scrappa_error")


# ------------------------------------------------------------------ tier 3
def exa_search(query, api_key, num=5):
    import requests
    r = requests.post("https://api.exa.ai/search",
        headers={"x-api-key": api_key, "Content-Type": "application/json"},
        json={"query": query, "type": "auto", "numResults": num,
              "contents": {"text": True, "extras": {"imageLinks": 5}}},
        timeout=(5, 40))
    if r.status_code != 200:
        raise RuntimeError("exa HTTP %s: %s" % (r.status_code, r.text[:200]))
    return r.json().get("results") or []


def _exa(identity, api_key, mpn_variants, limit=3):
    had_response = False

    for q in _photo_search_queries(identity):
        query = q.replace('"', '') + " official product images"
        try:
            results = exa_search(query, api_key)
            had_response = True
        except Exception as exc:
            logger.warning("exa failed: %s", exc)
            continue

        scored = []
        for res in results:
            page = res.get("url") or ""
            title = res.get("title") or ""

            if not (
                ident.token_hits(page, identity["model_tokens"])
                or any(
                    ident._alnum(m)
                    and ident._alnum(m) in ident._alnum(page)
                    for m in mpn_variants
                )
            ):
                continue

            urls = []
            if res.get("image"):
                urls.append((res["image"], ""))

            for ri in ((res.get("extras") or {}).get("richImageLinks") or []):
                if isinstance(ri, dict) and ri.get("url"):
                    urls.append((ri["url"], ri.get("alt") or ""))

            for u in ((res.get("extras") or {}).get("imageLinks") or []):
                if isinstance(u, str):
                    urls.append((u, ""))

            for u, alt in urls:
                conf, why, matched, deriv = ident.image_evidence(
                    u,
                    alt,
                    identity["brand_norm"],
                    identity["model_tokens"],
                    list(zip(mpn_variants, _derivations(identity))),
                )
                if conf >= ident.ACCEPT_THRESHOLD:
                    scored.append((_visual_rank(title, u), conf, why, u, title, page, matched, deriv))

        scored.sort(key=lambda x: (-x[0], -x[1]))
        out, seen = [], set()

        for visual_rank, conf, why, u, title, page, matched, deriv in scored:
            su = ident.sanitize_url(u)
            if su in seen:
                continue
            seen.add(su)
            out.append({
                "tier": "exa",
                "confidence": round(conf, 2),
                "reason": why,
                "fetch_url": u,
                "source_url": su,
                "title": title,
                "source_page": ident.sanitize_url(page),
                "matched_mpn": matched or identity["mpn"],
                "mpn_derivation": deriv or "exact",
            })
            if len(out) >= limit:
                break

        if out:
            return out, "exa"

    return [], ("exa_no_match" if had_response else "exa_error")




# ------------------------------------------------------------------ entry
def candidates(identity, allow_search=True, sleep_s=0.0, keys=None):
    """Return (candidates_sorted_desc, outcomes_list). No writes."""
    if keys is None:
        keys = provider_keys()
    outcomes = []
    mpn_variants = _mpn_variants(identity)

    try:
        index = lookup.load_index()
    except Exception:
        index = {}
    for key in mpn_variants:
        if key and key.upper() in index:
            u = index[key.upper()]
            outcomes.append("index")
            return [{"tier": "index", "confidence": 0.99, "reason": "curated_key",
                     "fetch_url": u, "source_url": ident.sanitize_url(u),
                     "title": "", "source_page": "", "matched_mpn": key,
                     "mpn_derivation": "exact"}], outcomes

    found, outcome = _icecat(identity, mpn_variants)
    outcomes.append(outcome)
    if found:
        return found, outcomes

    if not allow_search:
        outcomes.append("search_disabled")
        return [], outcomes

    if keys.get("scrappa"):
        found, outcome = _scrappa(
            identity,
            keys["scrappa"],
            keys.get("scrappa_base") or "https://scrappa.co/api",
            mpn_variants,
        )
        outcomes.append(outcome)
        if found:
            return found, outcomes
        if sleep_s:
            time.sleep(sleep_s)

    if keys.get("serp"):
        found, outcome = _serpapi(identity, keys["serp"], mpn_variants)
        outcomes.append(outcome)
        if found:
            return found, outcomes
        if sleep_s:
            time.sleep(sleep_s)

    if keys.get("exa"):
        found, outcome = _exa(identity, keys["exa"], mpn_variants)
        outcomes.append(outcome)
        if found:
            return found, outcomes

    if ENABLE_GOOGLE_CSE:
        cfg = lookup.api_config()
        if cfg.get("google_key") and cfg.get("google_cx"):
            logger.info("google CSE tier enabled but currently 403")
            outcomes.append("cse_skipped")

    outcomes.append("no_candidate")
    return [], outcomes


def provider_keys():
    cfg = lookup.load_env_file()
    import os
    def k(name):
        return os.environ.get(name) or cfg.get(name) or ""
    return {
        "serp": k("SERP_API_KEY"),
        "exa": k("EXA_API_KEY"),
        "scrappa": k("SCRAPPA_API_KEY") or k("SCRAPPA_KEY"),
        "scrappa_base": k("SCRAPPA_API_BASE_URL") or "https://scrappa.co/api",
    }
