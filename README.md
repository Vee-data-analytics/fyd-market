# fyd-market

Daily market scraper for the *Free Your Dreams* app. It collects:

- auction and retail cars that pass the car filter
- price points (hammer prices plus retail asking or guide prices), so you can tell whether a bid is a good deal
- sectional-title homes near Cosmo City

```
uv run scrape.py               # real run  -> out/market.json
uv run scrape.py --dry         # offline: parse fixtures/, run checks -> out/market.dry.json
uv run scrape.py --only aucor,mfc
```

Dependencies are declared inline in the `# /// script` header (requests, beautifulsoup4, lxml), so `uv` handles them and there's no venv to manage.

## Output (`out/market.json`)

`generated`, `sources{name:{ok,count,error,raw,rejected,pricePoints,seconds,auctions?}}`,
`cars[]`, `homes[]`, `carPrices[]`, `meta{requests,seconds,filters,warnings}`.
Cars also carry `bodyType` ("Bakkie", "Van/Minibus" or the WBC body type). Some cars also have `lotNumber`, `bids` or `reserveMet`.
`out/pricepoints_history.json` keeps every hammer price seen so far. Aucor archives finished
auctions after a few weeks, so running daily is how the comparables build up.

## Sources

| source | how | gives |
|---|---|---|
| `auction_operation` | HTML: `app.auctionoperation.co.za/home/auctions/upcoming` | **Auction level only** (name, date, yard, lot count, link). Lots are loaded by `POST /auction/lots/search/by/title`, but robots.txt disallows `/auction/lots/search/`, so the scraper doesn't call it. You get 0 cars and a note in `error`. |
| `aucor` | JSON: `live.aucor.com/ajax/lots/<auctionId>?page=N` (20 per page). Auction ids come from the `auction-summary` JSON embedded in `live.aucor.com/` and `/auctions/past` (AuctionMobility platform). robots.txt `Crawl-delay: 10`, which the scraper honours. | Upcoming vehicle lots (title, condition text, keys, image, live/timed bid when present). **Sold lots of recent past auctions become `hammer` price points.** |
| `mfc` | JSON: `onlineforms.nedsecure.co.za/mfc-dealer/nedbank/mfc-servicing/auctions`. This is the API behind mfc.co.za's auction calendar. | MFC's full repo catalogue (about 700 vehicles across Aucor, Burchmores, GoBid, WH, Michael James and the MFC Auction House Gauteng): make, model, year, km, auction date, yard, and **retail guide value** (`priceKind:"retail"`). There are no lot URLs, so `url` points to the auction house. |
| `webuycars_auction` | JSON: `POST appgateway.webuycars.co.za/website-elastic-backend/api/search` with `filter_type:"firstDay"` | WeBuyCars online auction lots, with starting price, reserve met, bid count, branch and province, and full specs |
| `webuycars` | Same API, normal stock, priced R40k–R120k | `retail` price points (asking prices) |
| `privateproperty` | HTML: `/for-sale/gauteng/<area>/<suburb>/<id>?bd=2&tp=700000`, plus up to 40 detail pages | Listings with levy, rates, floor size and listing date (the detail pages supply these) |
| `property24` | HTML: `/for-sale/<suburb>/<city>/gauteng/<id>?sp=pt%3d700000%26bd%3d2` | Tiles: price, beds, baths, size, image. No levy or rates. Nearby-suburb padding is dropped. |
| `igrow` | HTML: `/properties/`, plus detail pages of developments in Randburg/Roodepoort | New-development unit types (from-price, size, estimated levy and rates). Currently everything nearby is above R700k, so 0 kept. |

## Filters

Cars must meet all of these:

- Year 2016–2025.
- Make is one of Toyota, Honda, Nissan, Mazda, Suzuki, Mitsubishi, Subaru, Isuzu, Daihatsu, Lexus, Hyundai, Kia or Volkswagen. VW and M/BENZ aliases are normalised.
- Price is R40k–R80k when it is a bid, estimate or asking price. MFC retail *guides* may go up to R130k (`RETAIL_GUIDE_MAX`), because bank cars usually hammer well below retail.
- Lots with no price are kept with `priceKind:"none"`.

Lots that aren't vehicles (catalogue notes, canopies, golf carts) fail the year+make parse and are dropped. All regions are kept, and the app filters on `region`. Bakkies are kept and tagged.

Homes must be ≤ R700k, 2+ beds, sectional title or a new development, and not in Cosmo City. The list is deduped across sources and capped at 150.

The tunables are constants at the top of `scrape.py`, including the suburb id maps. If a portal renumbers a suburb, take the new id from Private Property's `SiteMap/residential-shapes-0.xml` or from Property24's city page.

## Politeness

- Normal browser User-Agent.
- robots.txt is checked for every URL, and `Crawl-delay` is honoured (at least 1s per host).
- 30s timeout, 2 retries, hard cap of 300 requests per run. A typical run makes about 100–130.
- Each source runs in its own try/except, so one failure is recorded in `sources` and the run carries on.

## How often

Once a day, e.g. 06:30. Most auction catalogues change daily, and Aucor's sold results are only visible for a short window after each sale. More often than daily adds load without adding much data. A typical run takes about 6–9 minutes on this connection, mostly Aucor's 10s crawl delay.

## Known limits

- **No Auction Operation lots** while their robots.txt blocks the lot API. Their auctions are still listed, so the app can link out.
- **Current bids** for Aucor and WBC are pushed over websockets. The JSON only carries a bid once a lot has a timed or absentee bid, so most upcoming lots show `starting bid` or `none`.
- MFC mileage uses placeholders (1, 111, 11111, …), which are turned into `null`. The retail value is a guide, not a price.
- Property24 tiles have no levy or rates. Honeydew Grove/Ridge, Bloubosrand and Sundowner have no P24 ids here, but Private Property covers them.
- Hammer-price history only builds up if the scraper runs regularly.
- The HTML parsers depend on site markup. Run `uv run scrape.py --dry` after a failure. To refresh a fixture, save a fresh page into `fixtures/` under the same name.
